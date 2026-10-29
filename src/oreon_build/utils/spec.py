# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import re
from typing import NamedTuple


class SpecInfo(NamedTuple):
    name: str | None
    summary: str | None


def parse_spec_content(content: bytes) -> SpecInfo:
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return SpecInfo(None, None)
    name: str | None = None
    summary: str | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            i += 1
            continue
        if re.match(r"^\s*Name\s*:", line, re.IGNORECASE):
            _, _, rest = line.partition(":")
            val = rest.strip()
            if val:
                name = val
            i += 1
            continue
        if re.match(r"^\s*Summary\s*:", line, re.IGNORECASE):
            _, _, rest = line.partition(":")
            val = rest.strip()
            i += 1
            while i < len(lines) and lines[i].startswith((" ", "\t")) and not lines[i].strip().startswith("%"):
                val += " " + lines[i].strip()
                i += 1
            if val:
                summary = val.strip()
            continue
        i += 1
    return SpecInfo(name, summary)


def name_from_srpm_filename(filename: str) -> str | None:
    fn = (filename or "").strip().lower()
    if not fn.endswith(".src.rpm"):
        return None
    base = fn[:-8]
    m = re.match(r"^(.+?)-\d+\.\d+.*-\d+.*$", base)
    if m:
        return m.group(1)
    return base if base else None
