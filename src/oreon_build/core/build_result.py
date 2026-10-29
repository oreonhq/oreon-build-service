# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re

_ARCH_EXCLUDED_RE = re.compile(
    r"(?:error:\s*)?architecture\s+is\s+not\s+included\s*:\s*\S+",
    re.IGNORECASE,
)


def is_architecture_excluded_error(msg: str | None) -> bool:
    if not msg:
        return False
    return bool(_ARCH_EXCLUDED_RE.search(msg))


def find_architecture_excluded_line(msg: str | None) -> str | None:
    if not msg:
        return None
    m = _ARCH_EXCLUDED_RE.search(msg)
    if not m:
        return None
    start = msg.rfind("\n", 0, m.start()) + 1
    end = msg.find("\n", m.end())
    if end < 0:
        end = len(msg)
    return msg[start:end].strip()


def prepare_build_error_message(
    msg: str | None,
    *,
    limit: int = 2000,
    fallback: str = "build failed",
) -> str:
    text = msg if isinstance(msg, str) else ""
    hit = find_architecture_excluded_line(text)
    if hit:
        return hit[:limit]
    body = text.strip()
    if not body:
        return fallback
    if len(body) <= limit:
        return body
    return body[-limit:]


def normalize_terminal_build_status(status: str | None, error_message: str | None) -> str:
    st = (status or "").strip().lower()
    if st == "failed" and is_architecture_excluded_error(error_message):
        return "skipped"
    if st == "blocked":
        return "blocked"
    return st
