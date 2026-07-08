from __future__ import annotations

import re
from typing import List


def parse_chain_build_spec(raw: str) -> list[list[str]]:
    s = (raw or "").strip()
    if not s:
        raise ValueError("Chain spec is empty")

    if ":" in s:
        parts = re.split(r"\s*:\s*", s)
        groups: list[list[str]] = []
        for part in parts:
            chunk = part.strip()
            if not chunk:
                continue
            names = [p for p in re.split(r"\s+", chunk) if p]
            if names:
                groups.append(_dedupe_preserve_order(names))
        if not groups:
            raise ValueError("Chain spec has no package names")
        return groups

    names = [p for p in re.split(r"\s+", s) if p]
    if not names:
        raise ValueError("Chain spec has no package names")
    return [[n] for n in _dedupe_preserve_order(names)]


def _dedupe_preserve_order(names: List[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out
