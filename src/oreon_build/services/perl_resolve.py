# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Map perl(Module) caps to distgit perl-* sources (parent walk + metacpan)."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMO: dict[str, str | None] = {}
_DISK_TTL_SEC = 7 * 24 * 3600


def _cache_dir() -> Path:
    raw = (os.environ.get("OREON_METACPAN_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".oreon" / "metacpan-cache"


def perl_module_parent_packages(module: str) -> list[str]:
    """perl(Foo::Bar::Baz) → [perl-Foo-Bar-Baz, perl-Foo-Bar, perl-Foo]."""
    mod = (module or "").strip()
    if not mod or "%" in mod or " " in mod:
        return []
    parts = [p for p in mod.split("::") if p]
    if not parts:
        return []
    out: list[str] = []
    for i in range(len(parts), 0, -1):
        out.append("perl-" + "-".join(parts[:i]))
    return out


def _disk_get(module: str) -> str | None | object:
    path = _cache_dir() / f"{module.replace('::', '__')}.json"
    if not path.is_file():
        return _MISS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts") or 0) > _DISK_TTL_SEC:
            return _MISS
        return data.get("dist")  # may be None
    except Exception:
        return _MISS


def _disk_set(module: str, dist: str | None) -> None:
    root = _cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{module.replace('::', '__')}.json"
    path.write_text(
        json.dumps({"ts": time.time(), "dist": dist}),
        encoding="utf-8",
    )


_MISS = object()


def metacpan_distribution_for_module(module: str) -> str | None:
    """CPAN distribution name for a module, or None. Cached."""
    mod = (module or "").strip()
    if not mod or "%" in mod or " " in mod:
        return None
    if mod in _MEMO:
        return _MEMO[mod]
    hit = _disk_get(mod)
    if hit is not _MISS:
        dist = hit if isinstance(hit, str) or hit is None else None
        _MEMO[mod] = dist  # type: ignore[assignment]
        return dist  # type: ignore[return-value]
    url = f"https://fastapi.metacpan.org/v1/module/{mod}"
    dist: str | None = None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "oreon-build-service/perl-resolve"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        raw = (data.get("distribution") or data.get("dist") or "").strip()
        dist = raw or None
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.debug("metacpan lookup failed for %s: %s", mod, exc)
        dist = None
    _MEMO[mod] = dist
    try:
        _disk_set(mod, dist)
    except Exception:
        pass
    return dist


def distgit_sources_for_perl_module(
    module: str,
    package_names: set[str],
) -> set[str]:
    """Resolve perl module to existing distgit source package names."""
    known = {str(s).strip() for s in (package_names or set()) if str(s).strip()}
    by_lower = {s.lower(): s for s in known}
    out: set[str] = set()
    for cand in perl_module_parent_packages(module):
        hit = by_lower.get(cand.lower())
        if hit:
            return {hit}
    dist = metacpan_distribution_for_module(module)
    if not dist:
        return out
    leaf = dist.replace("::", "-")
    for cand in (
        f"perl-{leaf}",
        f"perl-{leaf.lower()}",
        leaf,
        leaf.lower(),
    ):
        hit = by_lower.get(cand.lower())
        if hit:
            out.add(hit)
    return out
