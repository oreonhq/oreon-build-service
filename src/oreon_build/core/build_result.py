from __future__ import annotations

import re

_ARCH_EXCLUDED_RE = re.compile(
    r"architecture\s+is\s+not\s+included",
    re.IGNORECASE,
)


def is_architecture_excluded_error(msg: str | None) -> bool:
    if not msg:
        return False
    return bool(_ARCH_EXCLUDED_RE.search(msg))


def normalize_terminal_build_status(status: str | None, error_message: str | None) -> str:
    st = (status or "").strip().lower()
    if st == "failed" and is_architecture_excluded_error(error_message):
        return "skipped"
    return st
