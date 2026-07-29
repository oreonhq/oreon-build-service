"""BuildRequires oracle: rpmspec / rpmbuild -br with disk-cached nosrc."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from oreon_build.core.spec_bcond import default_build_defines, parse_spec_bconds
from oreon_build.services.dep_plan.types import BrEval
from oreon_build.services.dynamic_brs import (
    evaluate_generate_buildrequires,
    merge_buildrequires,
    rpmspec_query_buildrequires,
    spec_has_generate_buildrequires,
    spec_uses_cargo_generate_buildrequires,
)

logger = logging.getLogger(__name__)

_DISK_CACHE_VERSION = 5


def _cache_root() -> Path:
    raw = (os.getenv("OREON_DYNBR_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".cache" / "oreon-build" / "dynbr"


def _hash_key(spec_text: str, defines: dict[str, str] | None, local_paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    h.update(f"v{_DISK_CACHE_VERSION}\n".encode())
    h.update((spec_text or "").encode("utf-8", errors="replace"))
    for k in sorted((defines or {}).keys()):
        h.update(f"{k}={(defines or {})[k]}\n".encode())
    for p in sorted(local_paths, key=lambda x: str(x)):
        h.update(str(p.name).encode())
        try:
            st = p.stat()
            h.update(f"{st.st_size}:{st.st_mtime_ns}\n".encode())
            h.update(p.read_bytes()[:65536])
        except Exception:
            h.update(b"missing")
    return h.hexdigest()


def _disk_get(key: str) -> BrEval | None:
    path = _cache_root() / f"{key}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    brs = data.get("buildrequires")
    if not isinstance(brs, list):
        return None
    return BrEval(
        buildrequires=[str(x) for x in brs],
        complete=bool(data.get("complete")),
        error=str(data.get("error") or ""),
        pass_kind=str(data.get("pass_kind") or "cached"),
    )


def _disk_put(key: str, result: BrEval) -> None:
    root = _cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "buildrequires": list(result.buildrequires),
                    "complete": bool(result.complete),
                    "error": result.error or "",
                    "pass_kind": result.pass_kind or "final",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        logger.debug("dynbr disk cache write failed", exc_info=True)


def evaluate_package_brs(
    spec_text: str,
    *,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    local_source_paths: list[Path] | None = None,
    allow_source_download: bool = True,
    force_refresh: bool = False,
) -> BrEval:
    """Authoritative BR list for one spec (disk-cached when complete).

    Uses the spec's default bconds so plan BRs match what mock builddep sees.
    """
    bcond = parse_spec_bconds(
        spec_text, distro_macros=distro_macros, target_arches=target_arches
    )
    defines = default_build_defines(bcond)
    locals_ = [p for p in (local_source_paths or []) if p and p.is_file()]
    arch_defines = dict(defines)
    if target_arches:
        arch_defines["__target_arches__"] = ",".join(sorted(target_arches))
    static = rpmspec_query_buildrequires(spec_text, defines=defines)

    if not spec_has_generate_buildrequires(spec_text):
        return BrEval(buildrequires=static, complete=True, pass_kind="final")

    key = _hash_key(spec_text, arch_defines, locals_)
    if not force_refresh:
        hit = _disk_get(key)
        if hit is not None:
            return hit

    dyn = evaluate_generate_buildrequires(
        spec_text,
        defines=defines,
        local_source_paths=locals_,
        allow_source_download=allow_source_download,
        target_arches=target_arches,
    )
    merged = merge_buildrequires(static, dyn.buildrequires)
    if dyn.complete:
        out = BrEval(
            buildrequires=merged,
            complete=True,
            error=dyn.error or "",
            pass_kind="final",
        )
        _disk_put(key, out)
        return out

    cargo = spec_uses_cargo_generate_buildrequires(spec_text)
    if cargo:
        return BrEval(
            buildrequires=merged,
            complete=False,
            error=dyn.error or "cargo buildreqs nosrc missing",
            pass_kind="final",
        )
    # golang/py: static pass-1 schedules generate tools; re-enrich later
    return BrEval(
        buildrequires=merged,
        complete=False,
        error=dyn.error or "generate nosrc pending tools",
        pass_kind="pass1_static",
    )


def evaluate_many_brs(
    items: list[tuple[str, str, list[Path] | None]],
    *,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    max_workers: int = 6,
    force_refresh: bool = False,
) -> dict[str, BrEval]:
    """Parallel BR eval. items = [(pkg, spec_text, local_paths), ...]."""
    out: dict[str, BrEval] = {}
    if not items:
        return out
    workers = max(1, min(max_workers, len(items)))

    def _one(pkg: str, text: str, locals_: list[Path] | None) -> tuple[str, BrEval]:
        return pkg, evaluate_package_brs(
            text,
            distro_macros=distro_macros,
            target_arches=target_arches,
            local_source_paths=locals_,
            force_refresh=force_refresh,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(_one, pkg, text, locals_)
            for pkg, text, locals_ in items
        ]
        for fut in as_completed(futs):
            try:
                pkg, result = fut.result()
                out[pkg] = result
            except Exception:
                logger.warning("BR eval worker failed", exc_info=True)
    return out


def clear_br_disk_cache() -> None:
    root = _cache_root()
    if not root.is_dir():
        return
    for path in root.glob("*.json"):
        try:
            path.unlink()
        except Exception:
            pass
