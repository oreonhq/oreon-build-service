"""BuildRequires oracle: rpmspec / rpmbuild -br with sqlite-cached results."""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

_MP_SPAWN_CTX = multiprocessing.get_context("spawn")

from oreon_build.core.spec_bcond import default_build_defines, parse_spec_bconds
from oreon_build.services.dep_plan import br_cache
from oreon_build.services.dep_plan.types import BrEval
from oreon_build.services.dynamic_brs import (
    evaluate_generate_buildrequires,
    merge_buildrequires,
    rpmspec_query_buildrequires,
    spec_has_generate_buildrequires,
    spec_uses_cargo_generate_buildrequires,
)

logger = logging.getLogger(__name__)

_DISK_CACHE_VERSION = 7


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


def _disk_get(pkg: str, key: str) -> BrEval | None:
    hit = br_cache.get(pkg, key)
    if hit is None:
        return None
    return BrEval(
        buildrequires=hit["buildrequires"],
        complete=hit["complete"],
        error=hit["error"],
        pass_kind=hit["pass_kind"],
    )


def _disk_put(pkg: str, key: str, result: BrEval, *, provides: list[str] | None = None) -> None:
    br_cache.put(
        pkg,
        key,
        buildrequires=list(result.buildrequires),
        provides=provides,
        complete=bool(result.complete),
        error=result.error or "",
        pass_kind=result.pass_kind or "final",
    )


def evaluate_package_brs(
    spec_text: str,
    *,
    pkg: str = "",
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    local_source_paths: list[Path] | None = None,
    allow_source_download: bool = True,
    force_refresh: bool = False,
    extra_defines: dict[str, str] | None = None,
) -> BrEval:
    """Authoritative BR list for one spec (sqlite-cached when complete).

    Uses the spec's default bconds so plan BRs match what mock builddep sees.

    Fast path: specs without %generate_buildrequires never touch rpmbuild -br
    at all, `rpmspec -q --buildrequires` is the whole story and is already
    what a full `rpmspec --parse` would give us for BRs, minus the overhead
    of dumping the entire expanded spec.
    """
    bcond = parse_spec_bconds(
        spec_text, distro_macros=distro_macros, target_arches=target_arches
    )
    defines = default_build_defines(bcond)
    if extra_defines:
        defines = {**defines, **{str(k): str(v) for k, v in extra_defines.items() if str(k).strip()}}
    locals_ = [p for p in (local_source_paths or []) if p and p.is_file()]
    arch_defines = dict(defines)
    if target_arches:
        arch_defines["__target_arches__"] = ",".join(sorted(target_arches))
    static = rpmspec_query_buildrequires(spec_text, defines=defines)

    if not spec_has_generate_buildrequires(spec_text) and not spec_uses_cargo_generate_buildrequires(
        spec_text
    ):
        return BrEval(buildrequires=static, complete=True, pass_kind="final")

    cache_pkg = pkg or "_"
    key = _hash_key(spec_text, arch_defines, locals_)
    if not force_refresh:
        hit = _disk_get(cache_pkg, key)
        if hit is not None:
            return hit

    dyn = evaluate_generate_buildrequires(
        spec_text,
        defines=defines,
        local_source_paths=locals_,
        allow_source_download=allow_source_download,
        target_arches=target_arches,
        static_brs=static,
    )
    merged = merge_buildrequires(static, dyn.buildrequires)
    if dyn.complete:
        out = BrEval(
            buildrequires=merged,
            complete=True,
            error=dyn.error or "",
            pass_kind="final",
        )
        from oreon_build.services.distgit_order import _lightweight_provides_from_spec_text

        try:
            provides = sorted(_lightweight_provides_from_spec_text(pkg or "pkg", spec_text))
        except Exception:
            provides = None
        _disk_put(cache_pkg, key, out, provides=provides)
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


def _eval_one_br(
    pkg: str,
    text: str,
    locals_: list[Path] | None,
    *,
    distro_macros: dict[str, str] | None,
    target_arches: set[str] | None,
    force_refresh: bool,
) -> tuple[str, BrEval]:
    return pkg, evaluate_package_brs(
        text,
        pkg=pkg,
        distro_macros=distro_macros,
        target_arches=target_arches,
        local_source_paths=locals_,
        force_refresh=force_refresh,
    )


def evaluate_many_brs(
    items: list[tuple[str, str, list[Path] | None]],
    *,
    distro_macros: dict[str, str] | None = None,
    target_arches: set[str] | None = None,
    max_workers: int | None = None,
    force_refresh: bool = False,
    use_processes: bool = False,
) -> dict[str, BrEval]:
    """Parallel BR eval. items = [(pkg, spec_text, local_paths), ...].

    Threads by default. subprocess.run() (rpmbuild -br) already releases
    the GIL while the child runs, so threads get real I/O concurrency here
    without forking. Forking a process pool out of this API process is
    unsafe: it's a long-lived asyncio/SQLAlchemy/asyncpg service, and
    os.fork()'ing while a DB connection pool and event loop are live in
    another thread duplicates their sockets into the child. Garbage
    collection or finalizers in the child can then call shutdown()/close()
    on those duplicated sockets, which tears down the *shared* underlying
    TCP connection and takes out the parent's live DB connections with it
    (that's what caused the "TimeoutError getting a DB connection" lockup).
    use_processes=True is left available for callers that can guarantee no
    live DB pool/event loop in this process, using a spawn context (not
    fork) so it never inherits the parent's open file descriptors at all.
    """
    out: dict[str, BrEval] = {}
    if not items:
        return out
    workers = max(1, max_workers if max_workers is not None else (os.cpu_count() or 1))
    workers = min(workers, len(items))

    def _run(executor_cls: type, **extra: Any) -> dict[str, BrEval]:
        result: dict[str, BrEval] = {}
        with executor_cls(max_workers=workers, **extra) as pool:
            futs = [
                pool.submit(
                    _eval_one_br,
                    pkg,
                    text,
                    locals_,
                    distro_macros=distro_macros,
                    target_arches=target_arches,
                    force_refresh=force_refresh,
                )
                for pkg, text, locals_ in items
            ]
            for fut in as_completed(futs):
                try:
                    pkg, br_result = fut.result()
                    result[pkg] = br_result
                except Exception:
                    logger.warning("BR eval worker failed", exc_info=True)
        return result

    if use_processes:
        try:
            return _run(ProcessPoolExecutor, mp_context=_MP_SPAWN_CTX)
        except Exception:
            logger.warning("BR eval process pool unavailable, falling back to threads", exc_info=True)
    return _run(ThreadPoolExecutor)


def clear_br_disk_cache() -> None:
    br_cache.clear()
