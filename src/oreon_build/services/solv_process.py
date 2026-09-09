"""libsolv in a spawn child so SAT cannot freeze the API process GIL."""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from typing import Any

logger = logging.getLogger(__name__)

_RPC_TIMEOUT_SEC = 180.0
_ENGINE_MAX = 2

_lock = threading.RLock()
_proc: Any = None
_in_q: Any = None
_out_q: Any = None
_req_id = 0


def rpc_enabled() -> bool:
    if os.environ.get("OREON_SOLV_WORKER") == "1":
        return False
    if os.environ.get("OREON_SOLV_INPROCESS") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def _spec_key(spec: dict[str, Any]) -> tuple[Any, ...]:
    urls = spec.get("repo_urls") or ()
    flat = tuple((str(x[0]), str(x[1])) for x in urls if x)
    return (
        str(spec.get("releasename") or ""),
        tuple(spec.get("branches") or ()),
        str(spec.get("arch") or "x86_64"),
        flat,
    )


def _solv_worker_main(in_q: Any, out_q: Any) -> None:
    os.environ["OREON_SOLV_WORKER"] = "1"
    os.environ["OREON_SOLV_INPROCESS"] = "1"
    engines: dict[tuple[Any, ...], Any] = {}
    stamps: dict[tuple[Any, ...], float] = {}

    def _get_engine(spec: dict[str, Any], *, force: bool = False) -> Any:
        from oreon_build.services.repo_solver import OreonRepoSolver

        key = _spec_key(spec)
        if force:
            _close_key(spec)
        hit = engines.get(key)
        if hit is not None:
            stamps[key] = time.monotonic()
            return hit
        while len(engines) >= _ENGINE_MAX:
            oldest = min(stamps, key=stamps.get)
            old = engines.pop(oldest, None)
            stamps.pop(oldest, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
        raw_urls = spec.get("repo_urls") or []
        urls = [(str(x[0]), str(x[1])) for x in raw_urls if x]
        eng = OreonRepoSolver(
            releasename=str(spec.get("releasename") or ""),
            branches=list(spec.get("branches") or []),
            arch=str(spec.get("arch") or "x86_64"),
            repo_urls=urls or None,
        )
        eng._get_base()
        engines[key] = eng
        stamps[key] = time.monotonic()
        return eng

    def _close_key(spec: dict[str, Any]) -> None:
        key = _spec_key(spec)
        eng = engines.pop(key, None)
        stamps.pop(key, None)
        if eng is not None:
            try:
                eng.close()
            except Exception:
                pass

    while True:
        msg = in_q.get()
        if not msg:
            continue
        op, req_id, payload = msg
        if op == "stop":
            for eng in list(engines.values()):
                try:
                    eng.close()
                except Exception:
                    pass
            engines.clear()
            stamps.clear()
            out_q.put((req_id, "ok", None))
            break
        try:
            spec = dict((payload or {}).get("spec") or {})
            if op == "warm":
                _get_engine(spec, force=bool((payload or {}).get("force")))
                out_q.put((req_id, "ok", None))
            elif op == "set_overlay":
                eng = _get_engine(spec)
                eng.set_overlay(dict((payload or {}).get("packages") or {}))
                out_q.put((req_id, "ok", None))
            elif op == "resolve":
                eng = _get_engine(spec)
                result = eng.resolve(list((payload or {}).get("requirements") or []))
                out_q.put((req_id, "ok", result))
            elif op == "has_provider":
                eng = _get_engine(spec)
                out_q.put(
                    (req_id, "ok", bool(eng.has_provider(str((payload or {}).get("requirement") or ""))))
                )
            elif op == "sources_providing":
                eng = _get_engine(spec)
                out_q.put(
                    (
                        req_id,
                        "ok",
                        set(eng.sources_providing(str((payload or {}).get("requirement") or ""))),
                    )
                )
            elif op == "source_rpms_providing":
                eng = _get_engine(spec)
                out_q.put(
                    (
                        req_id,
                        "ok",
                        set(eng.source_rpms_providing(str((payload or {}).get("requirement") or ""))),
                    )
                )
            elif op == "close":
                _close_key(spec)
                out_q.put((req_id, "ok", None))
            elif op == "drop_release":
                rel = str(spec.get("releasename") or "")
                for key in list(engines):
                    if not rel or key[0] == rel:
                        _close_key(
                            {
                                "releasename": key[0],
                                "branches": list(key[1]),
                                "arch": key[2],
                            }
                        )
                out_q.put((req_id, "ok", None))
            else:
                out_q.put((req_id, "err", f"unknown op {op}"))
        except Exception:
            out_q.put((req_id, "err", traceback.format_exc()))


def _kill_proc_locked() -> None:
    global _proc, _in_q, _out_q
    proc = _proc
    _proc = None
    _in_q = None
    _out_q = None
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.join(timeout=2)
    except Exception:
        pass


def _ensure_proc_locked() -> None:
    global _proc, _in_q, _out_q
    if _proc is not None and _proc.is_alive():
        return
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    _in_q = ctx.Queue()
    _out_q = ctx.Queue()
    _proc = ctx.Process(target=_solv_worker_main, args=(_in_q, _out_q), daemon=True, name="oreon-solv")
    _proc.start()
    logger.info("solv worker started pid=%s", _proc.pid)


def _rpc(op: str, payload: dict[str, Any], timeout: float = _RPC_TIMEOUT_SEC) -> Any:
    global _req_id
    with _lock:
        _ensure_proc_locked()
        _req_id += 1
        req_id = _req_id
        assert _in_q is not None and _out_q is not None
        _in_q.put((op, req_id, payload))
        try:
            got_id, status, body = _out_q.get(timeout=timeout)
        except Exception:
            logger.exception("solv worker rpc timeout op=%s", op)
            _kill_proc_locked()
            raise RuntimeError(f"solv worker timeout on {op}") from None
        if got_id != req_id:
            logger.error("solv worker rpc mismatch op=%s expected=%s got=%s", op, req_id, got_id)
            _kill_proc_locked()
            raise RuntimeError("solv worker rpc mismatch")
        if status != "ok":
            raise RuntimeError(str(body or "solv worker error"))
        return body


def rpc_warm(spec: dict[str, Any]) -> None:
    _rpc("warm", {"spec": spec})


def rpc_set_overlay(spec: dict[str, Any], packages: dict[str, Any]) -> None:
    _rpc("set_overlay", {"spec": spec, "packages": packages})


def rpc_resolve(spec: dict[str, Any], requirements: list[str]) -> Any:
    return _rpc("resolve", {"spec": spec, "requirements": list(requirements)})


def rpc_has_provider(spec: dict[str, Any], requirement: str) -> bool:
    return bool(_rpc("has_provider", {"spec": spec, "requirement": requirement}))


def rpc_sources_providing(spec: dict[str, Any], requirement: str) -> set[str]:
    return set(_rpc("sources_providing", {"spec": spec, "requirement": requirement}) or ())


def rpc_source_rpms_providing(spec: dict[str, Any], requirement: str) -> set[str]:
    return set(_rpc("source_rpms_providing", {"spec": spec, "requirement": requirement}) or ())


def rpc_close(spec: dict[str, Any]) -> None:
    try:
        _rpc("close", {"spec": spec}, timeout=30)
    except Exception:
        logger.debug("solv worker close failed", exc_info=True)


def rpc_drop_release(releasename: str) -> None:
    if not rpc_enabled():
        return
    try:
        _rpc("drop_release", {"spec": {"releasename": releasename}}, timeout=60)
    except Exception:
        logger.warning("solv worker drop_release failed releasename=%s", releasename, exc_info=True)
