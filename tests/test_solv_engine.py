from __future__ import annotations

from oreon_build.services.solv_engine import (
    OverlaySolvable,
    _parse_missing_from_problems,
    _req_to_dep_id,
    _rel_flags,
    solv_available,
)


def test_solv_available() -> None:
    assert solv_available() is True


def test_dep_to_cap_keeps_rich_deps() -> None:
    from oreon_build.services.solv_engine import _dep_to_cap

    rich = "(crate(rand_jitter/default) >= 0.1.0 with crate(rand_jitter/default) < 0.2.0~)"
    assert _dep_to_cap(rich) == rich
    assert _dep_to_cap("foo >= 1.2") == "foo"
    assert _dep_to_cap("requested perl(DDP)") == "perl(DDP)"

    texts = [
        "nothing provides requested askalono-cli",
        "nothing provides requested trivy",
    ]
    miss = _parse_missing_from_problems(texts)
    assert miss == {"askalono-cli", "trivy"}
    dump = [
        "nothing provides libfoo.so.1 needed by kalgebra",
        "Installed: gcc-1 systemd-1 cups-1 curl-1 coreutils-1 bash-1",
    ]
    got = _parse_missing_from_problems(dump)
    assert got == set()
    assert "gcc-1" not in got
    assert "systemd-1" not in got


def test_resolve_never_inflates_miss_to_all_reqs() -> None:
    from oreon_build.services.solv_engine import SolvArchEngine, OverlaySolvable

    eng = SolvArchEngine(
        releasename="oreon-11-rp1",
        branches=["testing"],
        arch="x86_64",
    )
    # empty overlay on real/empty pool: missing should be only the requested caps
    # that truly have no providers, not every BR when problem text is weird.
    eng.load()
    # force path: unparseable-looking problems handled by ruleinfo; if only
    # one cap is missing, miss set must not become {a,b,c}
    r = eng.resolve(["definitely-not-a-real-cap-xyzzy-1012"])
    assert r.ok is False
    assert "definitely-not-a-real-cap-xyzzy-1012" in r.missing_provides
    assert len(r.missing_provides) == 1


def test_has_provider_sees_overlay_and_sources() -> None:
    from oreon_build.services.solv_engine import SolvArchEngine, OverlaySolvable

    eng = SolvArchEngine(
        releasename="oreon-11-rp1",
        branches=["testing"],
        arch="x86_64",
    )
    eng.set_overlay(
        {
            "perl-Data-Printer": OverlaySolvable(
                source_name="perl-Data-Printer",
                provides=frozenset({"perl(DDP)", "perl-Data-Printer"}),
            )
        }
    )
    assert eng.has_provider("perl(DDP)") is True
    assert "perl-Data-Printer" in eng.sources_providing("perl(DDP)")


def test_overlay_runtime_requires_are_transactional() -> None:
    from oreon_build.services.solv_engine import SolvArchEngine

    eng = SolvArchEngine(
        releasename="oreon-11-rp1",
        branches=["testing"],
        arch="x86_64",
    )
    eng.set_overlay(
        {
            "planned-devel": OverlaySolvable(
                source_name="planned-devel",
                provides=frozenset({"planned-devel"}),
                requires=frozenset({"missing-runtime-cap"}),
            )
        }
    )

    result = eng.resolve(["planned-devel"])

    assert result.ok is False
    assert "missing-runtime-cap" in result.missing_provides
    assert result.broken_binaries == {"planned-devel"}


def test_rel_flags_ge() -> None:
    class _S:
        REL_EQ = 2
        REL_GT = 1
        REL_LT = 4

    assert _rel_flags(_S, ">=") == 3


def test_overlay_clear_actually_removes_provides() -> None:
    """set_overlay({}) must not leave prior overlay caps installable."""
    from oreon_build.services.repo_capability_index import clear_repo_capability_cache
    from oreon_build.services.repo_solver import (
        OverlayPackage,
        clear_repo_solver_cache,
        get_repo_solver,
    )

    clear_repo_solver_cache()
    clear_repo_capability_cache()
    solver = get_repo_solver(
        "oreon-11-rp1",
        "test-2",
        ["x86_64"],
        extra_branches=["testing"],
    )
    solver.set_overlay({})
    solver.warm()
    assert solver.resolve(["go-vendor-tools"]).ok is False

    solver.set_overlay(
        {
            "go-vendor-tools": OverlayPackage(
                source_name="go-vendor-tools",
                provides=frozenset({"go-vendor-tools"}),
            )
        }
    )
    assert solver.resolve(["go-vendor-tools"]).ok is True

    solver.set_overlay({})
    assert solver.resolve(["go-vendor-tools"]).ok is False


def test_req_to_dep_id_parses_rich_crate_deps() -> None:
    import solv

    pool = solv.Pool()
    rich = "(crate(quickcheck) >= 1.0.0 with crate(quickcheck) < 2.0.0~)"
    dep_id = _req_to_dep_id(pool, solv, rich)
    assert dep_id is not None
    job = pool.Job(solv.Job.SOLVER_INSTALL | solv.Job.SOLVER_SOLVABLE_PROVIDES, dep_id)
    assert "crate(quickcheck)" in str(job)
