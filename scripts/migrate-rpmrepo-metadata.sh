#!/usr/bin/env bash
# Repodata refresh across active repo prefixes (createrepo_c + upload repodata/ only).
# Progress is printed per prefix as [done/total] with OK, SKIP, or FAIL.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  echo "Missing .venv in $ROOT_DIR"
  echo "Run scripts/deploy.sh first."
  exit 1
fi

# Load controller env so DB/R2/signing settings match production.
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

export PATH="${HOME}/.local/bin:${PATH}"
export OREON_CREATEREPO_WORKERS="${OREON_CREATEREPO_WORKERS:-50}"
CRE_TOOL="${OREON_CREATEREPO_BIN:-createrepo_c}"
if ! command -v "$CRE_TOOL" &>/dev/null; then
  echo "createrepo_c not found (install e.g. dnf install createrepo_c, or set OREON_CREATEREPO_BIN)"
  exit 1
fi

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python venv binary missing at $PYTHON_BIN"
  exit 1
fi

ARCHES_RAW="${MIGRATE_ARCHES:-x86_64,aarch64}"
PARALLEL_RAW="${MIGRATE_PARALLEL:-50}"
DRY_RUN="${MIGRATE_DRY_RUN:-0}"

case "$PARALLEL_RAW" in
  ''|*[!0-9]*)
    PARALLEL=2
    ;;
  *)
    PARALLEL="$PARALLEL_RAW"
    ;;
esac
if [ "$PARALLEL" -lt 1 ]; then
  PARALLEL=1
fi

PREFIX_FILE="$(mktemp)"
SUCCESS_FILE="$(mktemp)"
SKIP_FILE="$(mktemp)"
FAIL_FILE="$(mktemp)"
LOG_DIR="${ROOT_DIR}/.oreon-logs/migrate-repodata-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"

cleanup() {
  rm -f "$PREFIX_FILE" "$SUCCESS_FILE" "$SKIP_FILE" "$FAIL_FILE"
}
trap cleanup EXIT

ARCHES="$ARCHES_RAW" "$PYTHON_BIN" - <<'PY' > "$PREFIX_FILE"
import asyncio
import os
from sqlalchemy import select

from oreon_build.core.repo_branch import r2_publish_branch
from oreon_build.db.session import async_session_maker
from oreon_build.models import Release, Repository
from oreon_build.services.r2 import repo_r2_prefix

arches = [a.strip() for a in os.environ.get("ARCHES", "x86_64,aarch64").split(",") if a.strip()]

async def main() -> None:
    prefixes: set[str] = set()
    async with async_session_maker() as db:
        rrepo = await db.execute(select(Repository.r2_prefix))
        for p in rrepo.scalars().all():
            if not p:
                continue
            base = p.rstrip("/")
            for arch in arches:
                if base.endswith("/" + arch):
                    prefixes.add(base)
                    break

        rels = await db.execute(select(Release).where(Release.is_active.is_(True)))
        for rel in rels.scalars().all():
            dev = r2_publish_branch(rel)
            for ch in (dev, "testing", "stable"):
                for arch in arches:
                    prefixes.add(repo_r2_prefix(rel.releasename, ch, arch).rstrip("/"))

    for p in sorted(prefixes):
        print(p)

asyncio.run(main())
PY

TOTAL="$(wc -l < "$PREFIX_FILE" | tr -d ' ')"
if [ "$TOTAL" = "0" ]; then
  echo "No prefixes found for arches: $ARCHES_RAW"
  exit 0
fi

echo "Starting migration for $TOTAL prefixes"
echo "Arches: $ARCHES_RAW"
echo "Parallel workers: $PARALLEL"
echo "Logs: $LOG_DIR"
if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run mode is ON"
fi
echo

run_one() {
  local idx="$1"
  local total="$2"
  local prefix="$3"
  local safe_name
  local log_path
  local start_ts
  local elapsed
  local rc
  local state

  safe_name="$(echo "$prefix" | tr '/ ' '__')"
  log_path="${LOG_DIR}/${idx}-${safe_name}.log"
  start_ts="$(date +%s)"

  if [ "$DRY_RUN" = "1" ]; then
    echo "[$idx/$total] DRYRUN $prefix"
    echo "$prefix" >> "$SKIP_FILE"
    return 0
  fi

  echo "[$idx/$total] START  $prefix"

  : > "$log_path"
  PREFIX="$prefix" OREON_REPO_PROGRESS_STDOUT=1 OREON_REPO_PROGRESS_INTERVAL_SEC=1 "$PYTHON_BIN" -u - <<'PY' > "$log_path" 2>&1 &
import os
from oreon_build.services.publisher import update_repodata_at_prefix

prefix = os.environ["PREFIX"]
print(f"START prefix={prefix}", flush=True)
ok = update_repodata_at_prefix(prefix)
print(f"END prefix={prefix} result={ok}", flush=True)
raise SystemExit(0 if ok else 10)
PY
  local py_pid=$!
  local last_progress=""
  while kill -0 "$py_pid" 2>/dev/null; do
    local current
    current="$(awk '/^PROGRESS /{line=$0} END{print line}' "$log_path")"
    if [ -n "$current" ] && [ "$current" != "$last_progress" ]; then
      echo "[$idx/$total] ${current#PROGRESS }"
      last_progress="$current"
    fi
    sleep 1
  done
  wait "$py_pid"
  rc=$?
  elapsed="$(( $(date +%s) - start_ts ))"

  if [ "$rc" -eq 0 ]; then
    state="OK"
    echo "$prefix" >> "$SUCCESS_FILE"
  elif [ "$rc" -eq 10 ]; then
    state="SKIP"
    echo "$prefix" >> "$SKIP_FILE"
  else
    state="FAIL"
    echo "$prefix" >> "$FAIL_FILE"
  fi

  echo "[$idx/$total] $state   $prefix (${elapsed}s) log=${log_path}"
  return 0
}

active_jobs=0
idx=0
while IFS= read -r prefix; do
  idx="$((idx + 1))"
  run_one "$idx" "$TOTAL" "$prefix" &
  active_jobs="$((active_jobs + 1))"

  if [ "$active_jobs" -ge "$PARALLEL" ]; then
    wait -n || true
    active_jobs="$((active_jobs - 1))"
  fi
done < "$PREFIX_FILE"

wait || true

ok_count="$(wc -l < "$SUCCESS_FILE" | tr -d ' ')"
skip_count="$(wc -l < "$SKIP_FILE" | tr -d ' ')"
fail_count="$(wc -l < "$FAIL_FILE" | tr -d ' ')"

echo
echo "Migration complete"
echo "OK:   $ok_count"
echo "SKIP: $skip_count"
echo "FAIL: $fail_count"

if [ "$fail_count" -gt 0 ]; then
  echo
  echo "Failed prefixes"
  cat "$FAIL_FILE"
  exit 1
fi

exit 0
