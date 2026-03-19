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

# Stop Oreon Build Service (API + scheduler) started by ./start.sh

cd "$(dirname "$0")"

stop_pid() {
  local name="$1"
  local file="$2"
  if [ ! -f "$file" ]; then return; fi
  local pid
  pid=$(cat "$file")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    echo "Stopped $name (PID $pid)"
  fi
  rm -f "$file"
}

stop_pid "API"       .oreon-api.pid
stop_pid "Scheduler" .oreon-scheduler.pid
stop_pid "Watchdog"  .oreon-watchdog.pid

# Fallback: if pid files were missing/stale, kill our uvicorn processes by port.
kill_uvicorn_on_port() {
  local port="$1"
  local expected="$2"

  # Extract PIDs listening on the given port.
  local pids
  pids="$(ss -ltnp | awk -v port=":$port" '
    $4 ~ port {
      if (match($0, /pid=([0-9]+)/, m)) print m[1]
    }
  ')"

  if [ -z "$pids" ]; then
    return 0
  fi

  for pid in $pids; do
    # Only kill uvicorn processes that clearly match our app module string.
    local args
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$args" in
      *"$expected"*)
        kill "$pid" 2>/dev/null || true
        ;;
      *)
        # Don't touch unknown processes.
        ;;
    esac
  done
}

kill_uvicorn_on_port 8000 "uvicorn oreon_build.api.main:app"
kill_uvicorn_on_port 8001 "uvicorn oreon_build.watchdog.main:app"

# Clean up pid files even if they weren't present.
rm -f .oreon-api.pid .oreon-scheduler.pid .oreon-watchdog.pid 2>/dev/null || true
echo "Stopped."
