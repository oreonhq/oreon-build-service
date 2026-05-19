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

_wait_gone() {
  local pid="$1"
  local n=0
  while kill -0 "$pid" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -ge 10 ]; then
      return 1
    fi
    sleep 0.3
  done
  return 0
}

_kill_tree() {
  local pid="$1"
  local sig="$2"
  [ -z "$pid" ] || [ "$pid" -le 1 ] 2>/dev/null && return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  # Children first (deepest descendants).
  local child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    _kill_tree "$child" "$sig"
  done
  kill "-$sig" "$pid" 2>/dev/null || true
}

_stop_pid_file() {
  local name="$1"
  local file="$2"
  if [ ! -f "$file" ]; then
    return 0
  fi
  local pid
  pid=$(cat "$file" 2>/dev/null || true)
  rm -f "$file"
  if [ -z "$pid" ]; then
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  echo "Stopping $name (PID $pid)..."
  _kill_tree "$pid" TERM
  if ! _wait_gone "$pid"; then
    echo "  $name still running, sending SIGKILL"
    _kill_tree "$pid" KILL
    _wait_gone "$pid" || true
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "  WARNING: could not stop $name (PID $pid)"
  else
    echo "Stopped $name"
  fi
}

_pids_on_port() {
  local port="$1"
  if command -v ss &>/dev/null; then
    ss -ltnp 2>/dev/null | awk -v port=":$port" '
      $4 ~ port {
        while (match($0, /pid=([0-9]+)/, m)) {
          print m[1]
          $0 = substr($0, RSTART + RLENGTH)
        }
      }
    '
    return 0
  fi
  if command -v lsof &>/dev/null; then
    lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null
  fi
}

_kill_oreon_on_port() {
  local port="$1"
  local needle="$2"
  local pids
  pids="$(_pids_on_port "$port")"
  [ -z "$pids" ] && return 0

  for pid in $pids; do
    local args
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$args" in
      *"$needle"*)
        echo "Stopping port $port listener (PID $pid)..."
        _kill_tree "$pid" TERM
        if ! _wait_gone "$pid"; then
          echo "  still running, sending SIGKILL"
          _kill_tree "$pid" KILL
          _wait_gone "$pid" || true
        fi
        ;;
      *)
        echo "Port $port in use by unrelated PID $pid (not killed): $args"
        ;;
    esac
  done
}

_stop_pid_file "Repodata refresh" .oreon-repodata-refresh.pid
_stop_pid_file "API" .oreon-api.pid
_stop_pid_file "Scheduler" .oreon-scheduler.pid
_stop_pid_file "Watchdog" .oreon-watchdog.pid

# Fallback when pid files are stale or the parent was nohup'd and the real listener is a child.
_kill_oreon_on_port 8000 "oreon_build.api.main:app"
_kill_oreon_on_port 8001 "oreon_build.watchdog.main:app"

# Last resort: any oreon uvicorn still listening after a stuck chain prepare.
for pid in $(pgrep -f 'uvicorn oreon_build\.(api|watchdog)\.main:app' 2>/dev/null || true); do
  if kill -0 "$pid" 2>/dev/null; then
    echo "Stopping leftover uvicorn (PID $pid)..."
    _kill_tree "$pid" KILL
  fi
done

rm -f .oreon-repodata-refresh.pid .oreon-api.pid .oreon-scheduler.pid .oreon-watchdog.pid 2>/dev/null || true

if ss -tlnp 2>/dev/null | grep -q ':8000 '; then
  echo "WARNING: port 8000 still in use. Run: kill -9 \$(ss -ltnp | grep ':8000' | grep -oP 'pid=\\K[0-9]+')"
else
  echo "Stopped."
fi
