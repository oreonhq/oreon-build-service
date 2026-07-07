#!/usr/bin/env bash

set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

DO_REGEN=0
for _arg in "$@"; do
  case "$_arg" in
    --regen) DO_REGEN=1 ;;
    -h|--help)
      echo "Usage: $0 [--regen]"
      echo "  --regen   After starting services, run full repodata refresh in the background (parallel jobs, see MIGRATE_PARALLEL)."
      exit 0
      ;;
  esac
done

if [ ! -d .venv ]; then
  echo "No .venv found. Run ./scripts/deploy.sh first."
  exit 1
fi

if [ -f .oreon-repodata-refresh.pid ] && kill -0 "$(cat .oreon-repodata-refresh.pid)" 2>/dev/null; then
  _oreon_old_rep="$(cat .oreon-repodata-refresh.pid 2>/dev/null || true)"
  echo "Stale repodata refresh pid file (PID $_oreon_old_rep not running). Removing."
  rm -f .oreon-repodata-refresh.pid
fi

if [ -f .oreon-api.pid ] && kill -0 "$(cat .oreon-api.pid)" 2>/dev/null; then
  echo "API already running (PID $(cat .oreon-api.pid)). Run ./stop.sh first."
  exit 1
fi
if [ -f .oreon-scheduler.pid ] && kill -0 "$(cat .oreon-scheduler.pid)" 2>/dev/null; then
  echo "Scheduler already running (PID $(cat .oreon-scheduler.pid)). Run ./stop.sh first."
  exit 1
fi
if [ -f .oreon-watchdog.pid ] && kill -0 "$(cat .oreon-watchdog.pid)" 2>/dev/null; then
  echo "Watchdog already running (PID $(cat .oreon-watchdog.pid)). Run ./stop.sh first."
  exit 1
fi

[ -f .env ] && set -a && source .env && set +a

if command -v systemctl &>/dev/null; then
  for svc in postgresql-16 postgresql-15 postgresql-14 postgresql; do
    if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${svc}.service"; then
      if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "Starting $svc..."
        sudo systemctl start "$svc" || true
        sleep 2
      fi
      break
    fi
  done
fi

for i in 1 2 3 4 5; do
  if python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 5432)); s.close()" 2>/dev/null; then
    break
  fi
  if [ "$i" -eq 5 ]; then
    echo "PostgreSQL not reachable on 127.0.0.1:5432. Start it with: sudo systemctl start postgresql"
    exit 1
  fi
  sleep 1
done

source .venv/bin/activate

echo "DB unclog preflight (safe idle session cleanup)..."
set +e
python -u "$ROOT/scripts/db_unclog_safe_idles.py"
_db_unclog_rc=$?
set -e
if [ "$_db_unclog_rc" -eq 3 ]; then
  echo "DB unclog: no connection slots available for app role; attempting PostgreSQL restart fallback..."
  _pg_restarted=0
  if command -v systemctl &>/dev/null; then
    for svc in postgresql-16 postgresql-15 postgresql-14 postgresql; do
      if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${svc}.service"; then
        if systemctl restart "$svc" 2>/dev/null || sudo systemctl restart "$svc" 2>/dev/null; then
          _pg_restarted=1
          sleep 3
        fi
        break
      fi
    done
  fi
  if [ "$_pg_restarted" -eq 1 ]; then
    set +e
    python -u "$ROOT/scripts/db_unclog_safe_idles.py"
    _db_unclog_rc=$?
    set -e
  fi
fi

if [ "$_db_unclog_rc" -ne 0 ]; then
  if [ "${OREON_START_DB_UNCLOG_STRICT:-0}" = "1" ]; then
    echo "DB unclog failed and OREON_START_DB_UNCLOG_STRICT=1, aborting start."
    exit 1
  fi
  echo "WARNING: DB unclog failed; continuing startup. Set OREON_START_DB_UNCLOG_STRICT=1 to fail fast."
fi

if ! command -v createrepo_c &>/dev/null; then
  echo "WARNING: createrepo_c not on PATH. Install it for repodata refresh (e.g. dnf install createrepo_c)."
fi

LOG_DIR="${OREON_LOG_DIR:-.oreon-logs}"
mkdir -p "$LOG_DIR"

nohup uvicorn oreon_build.api.main:app --host 0.0.0.0 --port 8000 \
  > "$LOG_DIR/oreon-api.log" 2>&1 &
echo $! > .oreon-api.pid
echo "API started (PID $(cat .oreon-api.pid)), http://localhost:8000"

nohup oreon-scheduler \
  > "$LOG_DIR/oreon-scheduler.log" 2>&1 &
echo $! > .oreon-scheduler.pid
echo "Scheduler started (PID $(cat .oreon-scheduler.pid))"

WATCHDOG_URL_VAL="${WATCHDOG_URL:-http://localhost:8001}"

if [[ "$WATCHDOG_URL_VAL" =~ ^[0-9]+$ ]]; then
  WATCHDOG_PORT="$WATCHDOG_URL_VAL"
elif [[ "$WATCHDOG_URL_VAL" =~ :([0-9]+) ]]; then
  WATCHDOG_PORT="${BASH_REMATCH[1]}"
else
  WATCHDOG_PORT=8001
fi

nohup uvicorn oreon_build.watchdog.main:app --host 0.0.0.0 --port "$WATCHDOG_PORT" \
  > "$LOG_DIR/oreon-watchdog.log" 2>&1 &
echo $! > .oreon-watchdog.pid
echo "Oreon Watchdog started (PID $(cat .oreon-watchdog.pid)), http://localhost:${WATCHDOG_PORT}"

LOG_SHOW_LINES="${OREON_LOG_SHOW_LINES:-200}"
sleep 1

echo "---- Tail: API ($LOG_SHOW_LINES lines) ----"
tail -n "$LOG_SHOW_LINES" "$LOG_DIR/oreon-api.log" 2>/dev/null || true
echo "---- Tail: Scheduler ($LOG_SHOW_LINES lines) ----"
tail -n "$LOG_SHOW_LINES" "$LOG_DIR/oreon-scheduler.log" 2>/dev/null || true
echo "---- Tail: Watchdog ($LOG_SHOW_LINES lines) ----"
tail -n "$LOG_SHOW_LINES" "$LOG_DIR/oreon-watchdog.log" 2>/dev/null || true

echo "Done. Run ./stop.sh to stop."

if [ "$DO_REGEN" -eq 1 ]; then
  echo "Starting background repodata refresh (logs: $LOG_DIR/repodata-refresh.log, parallel=${MIGRATE_PARALLEL:-4})..."
  nohup env MIGRATE_PARALLEL="${MIGRATE_PARALLEL:-4}" bash -c "cd \"$ROOT\" && exec bash ./scripts/migrate-rpmrepo-metadata.sh" \
    >> "$LOG_DIR/repodata-refresh.log" 2>&1 &
  echo $! > "$ROOT/.oreon-repodata-refresh.pid"
  echo "Repodata refresh PID $(cat "$ROOT/.oreon-repodata-refresh.pid")"
else
  echo "Repodata refresh not started (use ./start.sh --regen to queue it)."
fi
