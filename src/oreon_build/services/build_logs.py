# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
from pathlib import Path

_NAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,180}$")
_LOG_SUFFIXES = {".log", ".txt", ".json"}
_BARE_NAMES = {"available_pkgs", "installed_pkgs"}
_STAGE_ORDER = {"srpm": 0, "resolve": 1, "rebuild": 2}
_FILE_ORDER = {
    "mock_output.log": 0,
    "root.log": 1,
    "state.log": 2,
    "dnf5.log": 3,
    "dnf.log": 4,
    "yum.log": 5,
    "build.log": 6,
    "hw_info.log": 7,
    "installed_pkgs.log": 8,
    "available_pkgs.log": 9,
    "available_pkgs": 10,
    "procenv.log": 11,
    "showrc.log": 12,
    "mock_config.log": 13,
}


def safe_log_name(name: str) -> str | None:
    raw = (name or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        return None
    parts = [p for p in raw.split("/") if p != ""]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    if len(parts) > 6:
        return None
    if not all(_NAME_PART.fullmatch(p) for p in parts):
        return None
    return "/".join(parts)


def is_result_log(path: Path) -> bool:
    name = path.name
    if not name or name.startswith("."):
        return False
    low = name.lower()
    if low.endswith(".rpm") or low.endswith(".rpm.sig"):
        return False
    if path.suffix.lower() in _LOG_SUFFIXES:
        return True
    return name in _BARE_NAMES


def iter_log_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    try:
        paths = sorted(root.rglob("*"))
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for p in paths:
        try:
            if not p.is_file():
                continue
            resolved = p.resolve()
        except OSError:
            continue
        if resolved != root_resolved and root_resolved not in resolved.parents:
            continue
        if not is_result_log(p):
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        safe = safe_log_name(rel)
        if safe is None:
            continue
        found.append((safe, p))
    return found


def sort_log_names(names: list[str]) -> list[str]:
    def key(name: str) -> tuple:
        if "/" in name:
            stage, base = name.split("/", 1)
        else:
            stage, base = "", name
        return (
            _STAGE_ORDER.get(stage, 50),
            stage,
            _FILE_ORDER.get(base, len(_FILE_ORDER)),
            base,
        )

    return sorted(names, key=key)
