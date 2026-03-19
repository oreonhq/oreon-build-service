#!/usr/bin/env bash
# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Start Oreon Build Service (API + scheduler) in the background.
# Run from repo root. Stop with ./stop.sh

set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

if [ ! -d .venv ]; then
  echo "No .venv found. Run ./scripts/deploy.sh first."
  exit 1
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

# Load env
[ -f .env ] && set -a && source .env && set +a

# Start PostgreSQL if not running (same logic as scripts/deploy.sh)
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

# Wait for PostgreSQL to accept connections
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

# Logs
LOG_DIR="${OREON_LOG_DIR:-.oreon-logs}"
mkdir -p "$LOG_DIR"

# API
nohup uvicorn oreon_build.api.main:app --host 0.0.0.0 --port 8000 \
  > "$LOG_DIR/oreon-api.log" 2>&1 &
echo $! > .oreon-api.pid
echo "API started (PID $(cat .oreon-api.pid)), http://localhost:8000"

# Scheduler
nohup oreon-scheduler \
  > "$LOG_DIR/oreon-scheduler.log" 2>&1 &
echo $! > .oreon-scheduler.pid
echo "Scheduler started (PID $(cat .oreon-scheduler.pid))"

# Watchdog (security advisory dashboard)
WATCHDOG_URL_VAL="${WATCHDOG_URL:-http://localhost:8001}"

# Extract port from common forms:
# - "8001"
# - "http://localhost:8001"
# - "localhost:8001"
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

# Show actual logs (non-blocking)
LOG_SHOW_LINES="${OREON_LOG_SHOW_LINES:-200}"
sleep 1

echo "---- Tail: API ($LOG_SHOW_LINES lines) ----"
tail -n "$LOG_SHOW_LINES" "$LOG_DIR/oreon-api.log" 2>/dev/null || true
echo "---- Tail: Scheduler ($LOG_SHOW_LINES lines) ----"
tail -n "$LOG_SHOW_LINES" "$LOG_DIR/oreon-scheduler.log" 2>/dev/null || true
echo "---- Tail: Watchdog ($LOG_SHOW_LINES lines) ----"
tail -n "$LOG_SHOW_LINES" "$LOG_DIR/oreon-watchdog.log" 2>/dev/null || true

echo "Done. Run ./stop.sh to stop."
