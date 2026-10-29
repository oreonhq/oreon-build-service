# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reload the PID-file-managed API, allowing the old process to drain requests."""
from pathlib import Path
import os
import signal
import socket
import subprocess
import time
import urllib.request


def main():
    root = Path(__file__).resolve().parents[1]
    pid_file = root / ".oreon-api.pid"
    old_pid = int(pid_file.read_text().strip())
    proc = Path(f"/proc/{old_pid}")
    command = [part.decode() for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
    if "oreon_build.api.main:app" not in command:
        raise RuntimeError("PID file does not identify the Oreon API; refusing to signal it")
    if Path(os.readlink(proc / "cwd")).resolve() != root:
        raise RuntimeError("API is running from a different checkout")
    environment = dict(
        part.decode().split("=", 1)
        for part in (proc / "environ").read_bytes().split(b"\0") if part
    )
    port = int(command[command.index("--port") + 1])
    os.kill(old_pid, signal.SIGTERM)
    # Uvicorn closes its listener before waiting for existing requests to drain.
    deadline = time.monotonic() + 30
    while True:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            listening = probe.connect_ex(("127.0.0.1", port)) == 0
        if not listening:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Old API has not released its listener; no replacement started")
        time.sleep(0.25)
    log_dir = Path(environment.get("OREON_LOG_DIR", str(root / ".oreon-logs")))
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    with (log_dir / "oreon-api.log").open("ab", buffering=0) as log:
        replacement = subprocess.Popen(
            command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    pid_file.write_text(f"{replacement.pid}\n")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if replacement.poll() is not None:
            raise RuntimeError("Replacement API exited; inspect oreon-api.log")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as response:
                if response.status == 200:
                    print(f"API reloaded: {old_pid} draining, {replacement.pid} serving")
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Replacement API readiness timed out; inspect oreon-api.log")


if __name__ == "__main__":
    main()
