# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re

_RE_REQUESTED = re.compile(
    r"nothing provides requested\s+(?P<cap>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RE_NEEDED = re.compile(
    r"nothing provides\s+(?P<cap>[^\n]+?)\s+needed by\b",
    re.IGNORECASE,
)
_RE_PLAIN = re.compile(
    r"nothing provides\s+(?!requested\b)(?P<cap>[^\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RE_IS_NEEDED = re.compile(
    r"^\s+(?P<cap>\S+(?:\s*[<>=!]+\s*\S+)?)\s+is needed by\b",
    re.IGNORECASE | re.MULTILINE,
)
_RE_NO_MATCH = re.compile(
    r"No matching package to install:\s*['\"]?(?P<cap>[^'\"\n]+)['\"]?",
    re.IGNORECASE,
)
_RE_PKG_REQUIRES = re.compile(
    r"\bpackage\s+\S+\s+requires\s+(?P<cap>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_+:.@/-]*(?:\([^()]*\))?|/[A-Za-z0-9._+:/@-]+")
_OPERATORS = {"and", "or", "with", "without", "requested", "needed", "by", "package", "requires"}


def _tokens_from_cap(cap: str) -> list[str]:
    text = (cap or "").strip().rstrip(".")
    if not text or "%" in text:
        return []
    if text.lower().startswith("nothing provides"):
        return []
    if text.startswith("/"):
        return [text.split()[0]]
    raw = _TOKEN.findall(text)
    out: list[str] = []
    for candidate in raw:
        token = candidate.strip().rstrip(".")
        if not token or "%" in token:
            continue
        if token.lower() in _OPERATORS:
            continue
        if token in {"<", ">", "=", "<=", ">=", "!=", "=="}:
            continue
        if token[0].isdigit():
            continue
        out.append(token)
    return out


def parse_dnf_missing_provides(text: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    raw = text or ""
    matchers = (_RE_REQUESTED, _RE_NEEDED, _RE_IS_NEEDED, _RE_NO_MATCH, _RE_PKG_REQUIRES, _RE_PLAIN)
    for matcher in matchers:
        for match in matcher.finditer(raw):
            cap = (match.group("cap") or "").strip()
            if matcher is _RE_PLAIN and re.search(r"\bneeded by\b", cap, re.IGNORECASE):
                continue
            for token in _tokens_from_cap(cap):
                if token not in seen:
                    seen.add(token)
                    ordered.append(token)
    return ordered
