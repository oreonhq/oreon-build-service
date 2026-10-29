# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import time

from oreon_build.services.distgit_order import (
    _PROVIDES_INDEX_CACHE,
    _PROVIDES_INDEX_MEM_TTL_SEC,
    _TARBALL_CACHE_TTL_SEC,
    _tarball_cache,
    prune_distgit_memory_caches,
)
from oreon_build.services.repo_solver import (
    _SOLVER_CACHE,
    _SOLVER_CACHE_TTL_SEC,
    clear_repo_solver_cache,
    get_repo_solver,
)


class _Idx:
    package_names = frozenset()
    capability_providers: dict = {}


def test_prune_keeps_newest_provides_index_only() -> None:
    _PROVIDES_INDEX_CACHE.clear()
    _tarball_cache.clear()
    try:
        old = time.monotonic() - _PROVIDES_INDEX_MEM_TTL_SEC - 5
        now = time.monotonic()
        _PROVIDES_INDEX_CACHE[("o", "r", "b", "rel", "old")] = (old, _Idx())
        _PROVIDES_INDEX_CACHE[("o", "r", "b", "rel", "mid")] = (now - 10, _Idx())
        _PROVIDES_INDEX_CACHE[("o", "r", "b", "rel", "new")] = (now, _Idx())
        _tarball_cache[("o", "r", "b", "old")] = (
            time.monotonic() - _TARBALL_CACHE_TTL_SEC - 1,
            b"old",
        )
        _tarball_cache[("o", "r", "b", "new")] = (time.monotonic(), b"new")
        prune_distgit_memory_caches()
        assert list(_PROVIDES_INDEX_CACHE) == [("o", "r", "b", "rel", "new")]
        assert list(_tarball_cache) == [("o", "r", "b", "new")]
    finally:
        _PROVIDES_INDEX_CACHE.clear()
        _tarball_cache.clear()


def test_get_repo_solver_closes_previous_on_replace(monkeypatch) -> None:
    clear_repo_solver_cache()
    closed: list[object] = []

    class FakeSolver:
        def __init__(self, *a, **k):
            pass

        def close(self):
            closed.append(self)

        def warm(self):
            pass

        def _get_base(self):
            return None

    class FakeMulti:
        def __init__(self, solvers):
            self.solvers = solvers

        def close(self):
            closed.append(self)
            for s in self.solvers:
                s.close()

        def warm(self):
            pass

    monkeypatch.setattr(
        "oreon_build.services.repo_solver.OreonRepoSolver", FakeSolver
    )
    monkeypatch.setattr(
        "oreon_build.services.repo_solver.MultiArchRepoSolver", FakeMulti
    )
    monkeypatch.setattr(
        "oreon_build.services.solv_engine.solv_available", lambda: True
    )

    s1 = get_repo_solver("oreon-11-rp1", "testing", ["x86_64"])
    key = ("oreon-11-rp1", ("testing",), ("x86_64",))
    assert s1 is _SOLVER_CACHE[key][1]
    ts, old = _SOLVER_CACHE[key]
    _SOLVER_CACHE[key] = (ts - _SOLVER_CACHE_TTL_SEC - 1, old)
    s2 = get_repo_solver("oreon-11-rp1", "testing", ["x86_64"])
    assert s2 is not old
    assert old in closed
    clear_repo_solver_cache()
