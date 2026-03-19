"""Parse RPM .spec file content for Name and Summary."""
import re
from typing import NamedTuple


class SpecInfo(NamedTuple):
    name: str | None
    summary: str | None


def parse_spec_content(content: bytes) -> SpecInfo:
    """Extract Name and Summary from .spec file content. Handles continuation lines."""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return SpecInfo(None, None)
    name: str | None = None
    summary: str | None = None
    # Directive pattern: start of line (or after %global etc.), tag, then value
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip comments and empty lines
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            i += 1
            continue
        # Match "Name:" or "Summary:" at start of logical line (allow leading whitespace)
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
            # Summary can continue on next lines (lines that start with space)
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
    """Derive package name from SRPM filename, e.g. my-pkg-1.0-1.src.rpm -> my-pkg."""
    fn = (filename or "").strip().lower()
    if not fn.endswith(".src.rpm"):
        return None
    # Remove .src.rpm
    base = fn[:-8]
    # Last part is typically version-release; try to strip -X.Y-Z
    # Match -digit.digit-digit or -digit-digit at the end
    m = re.match(r"^(.+?)-\d+\.\d+.*-\d+.*$", base)
    if m:
        return m.group(1)
    # Fallback: use whole base (no version pattern found)
    return base if base else None
