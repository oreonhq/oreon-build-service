# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _volatile(path: Path) -> bool:
    try:
        text = str(path.expanduser().resolve())
    except OSError:
        text = str(path)
    return (
        text == "/tmp"
        or text.startswith("/tmp/")
        or text.startswith("/dev/shm")
        or text.startswith("/run/")
    )


def disk_tmpdir(*parts: str) -> Path:
    raw = (
        os.environ.get("OREON_DISK_TMPDIR")
        or os.environ.get("OREON_SCAN_TMPDIR")
        or os.environ.get("OREON_REPO_TMPDIR")
        or ""
    ).strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".oreon" / "tmp"
    if _volatile(base):
        base = Path.home() / ".oreon" / "tmp"
    if parts:
        base = base.joinpath(*parts)
    base.mkdir(parents=True, exist_ok=True)
    return base


def pin_process_tmpdir() -> Path:
    path = disk_tmpdir()
    loc = str(path)
    os.environ["TMPDIR"] = loc
    os.environ["TEMP"] = loc
    os.environ["TMP"] = loc
    tempfile.tempdir = loc
    return path
