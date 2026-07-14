from __future__ import annotations

from oreon_build.services.repo_solver import parse_depsolve_missing


def test_parse_depsolve_missing_range() -> None:
    from oreon_build.services.repo_solver import (
        nevra_to_name,
        parse_depsolve_broken_binaries,
        parse_depsolve_missing,
    )

    text = """
Problem: package perl-Module-Build-2:0.42.34-10.or11.noarch from oreon-testing requires perl(CPAN::Meta::Merge), but none of the providers can be installed
- package perl-CPAN-Meta-2.150010-524.or11.noarch from oreon-dev requires perl(CPAN::Meta::Requirements) >= 2.121, but none of the providers can be installed
- conflicting requests
- nothing provides perl(CPAN::Meta::Requirements::Range) needed by perl-CPAN-Meta-Requirements-2.140-524.or11.noarch from oreon-dev
"""
    miss = parse_depsolve_missing(text)
    assert "perl(CPAN::Meta::Requirements::Range)" in miss
    assert "perl(CPAN::Meta::Merge)" in miss
    assert "perl(CPAN::Meta::Requirements) >= 2.121" in miss
    broken = parse_depsolve_broken_binaries(text)
    assert "perl-CPAN-Meta-Requirements" in broken
    assert "perl-Module-Build" in broken
    assert "perl-CPAN-Meta" in broken
    assert nevra_to_name("perl-CPAN-Meta-Requirements-2.140-524.or11.noarch") == (
        "perl-CPAN-Meta-Requirements"
    )


def test_parse_no_match_argument() -> None:
    from oreon_build.services.repo_solver import parse_depsolve_missing

    assert "perl(Does::Not::Exist)" in parse_depsolve_missing(
        "No match for argument: perl(Does::Not::Exist)\n"
    )


def test_force_uses_solver_missing(monkeypatch) -> None:
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def resolve(self, reqs):
            req = list(reqs)[0]
            if req == "perl(Module::Build)":
                return SolveResult(
                    ok=False,
                    missing_provides={"perl(CPAN::Meta::Requirements::Range)"},
                    problems="nothing provides perl(CPAN::Meta::Requirements::Range)",
                )
            return SolveResult(ok=True)

    repo = RepoCapabilityIndex(
        binary_names={"perl-Module-Build", "perl-CPAN-Meta-Requirements"},
        provides={"perl(Module::Build)", "perl-CPAN-Meta-Requirements"},
        capability_providers={
            "perl(Module::Build)": {"perl-Module-Build"},
            "perl-CPAN-Meta-Requirements": {"perl-CPAN-Meta-Requirements"},
        },
        binary_source={
            "perl-Module-Build": "perl-Module-Build",
            "perl-CPAN-Meta-Requirements": "perl",
        },
        source_names={"perl-Module-Build", "perl", "perl-CPAN-Meta-Requirements"},
    )
    repo.finalize_lookups()
    spec = "Name: app\nVersion: 1\nRelease: 1\nBuildRequires: perl(Module::Build)\nProvides: app\n"
    parsed = {"app": _parse_spec_text(spec, "app.spec")}
    distgit = {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}}
    force, _ok, _bad, _fetch = bp._provider_rebuild_force_sources(
        {"app"},
        parsed,
        {"app": spec},
        repo,
        distgit,
        solver=FakeSolver(),
    )
    assert "perl-CPAN-Meta-Requirements" in force
    assert "perl" not in force


def test_force_rebuilds_uninstallable_needed_by_binary() -> None:
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def resolve(self, reqs):
            return SolveResult(
                ok=False,
                missing_provides={"perl(CPAN::Meta::Requirements) >= 2.121"},
                broken_binaries={"perl-CPAN-Meta-Requirements", "perl-Module-CPANfile"},
                problems=(
                    "package perl-Module-CPANfile-1.1004-24.or11.noarch from oreon-testing "
                    "requires perl(CPAN::Meta::Requirements), but none of the providers can "
                    "be installed\n"
                    "nothing provides perl(CPAN::Meta::Requirements::Range) needed by "
                    "perl-CPAN-Meta-Requirements-2.140-524.or11.noarch from oreon-dev"
                ),
            )

    repo = RepoCapabilityIndex(
        binary_names={"perl-Module-CPANfile", "perl-CPAN-Meta-Requirements"},
        provides={
            "perl(Module::CPANfile)",
            "perl(CPAN::Meta::Requirements)",
            "perl-Module-CPANfile",
            "perl-CPAN-Meta-Requirements",
        },
        capability_providers={
            "perl(Module::CPANfile)": {"perl-Module-CPANfile"},
            "perl(CPAN::Meta::Requirements)": {"perl-CPAN-Meta-Requirements"},
            "perl-Module-CPANfile": {"perl-Module-CPANfile"},
            "perl-CPAN-Meta-Requirements": {"perl-CPAN-Meta-Requirements"},
        },
        binary_source={
            "perl-Module-CPANfile": "perl-Module-CPANfile",
            "perl-CPAN-Meta-Requirements": "perl-CPAN-Meta-Requirements",
        },
        source_names={"perl-Module-CPANfile", "perl-CPAN-Meta-Requirements"},
        binary_requires={
            "perl-Module-CPANfile": {"perl(CPAN::Meta::Requirements)"},
            "perl-CPAN-Meta-Requirements": {"perl(CPAN::Meta::Requirements::Range)"},
        },
    )
    repo.finalize_lookups()
    spec = (
        "Name: app\nVersion: 1\nRelease: 1\n"
        "BuildRequires: perl(Module::CPANfile)\nProvides: app\n"
    )
    parsed = {"app": _parse_spec_text(spec, "app.spec")}
    force, ok, bad, _fetch = bp._provider_rebuild_force_sources(
        {"app"},
        parsed,
        {"app": spec},
        repo,
        {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}},
        solver=FakeSolver(),
    )
    assert not ok
    assert "app" in bad
    assert "perl-CPAN-Meta-Requirements" in force


def test_force_slot_builds_bootstrap_not_block() -> None:
    from oreon_build.services.build_planner import _plan_slots_for_package
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex

    repo = RepoCapabilityIndex(
        source_names={"perl-CPAN-Meta-Requirements"},
        binary_names={"perl-CPAN-Meta-Requirements"},
        provides={"perl-CPAN-Meta-Requirements"},
    )
    repo.finalize_lookups()
    spec = """
%bcond_without cmr_enables_optional_test
Name: perl-CPAN-Meta-Requirements
Version: 2.145
Release: 1
BuildRequires: make
%if %{with cmr_enables_optional_test}
BuildRequires: perl(Test::Perl::Critic)
%endif
Provides: perl(CPAN::Meta::Requirements::Range)
"""
    slot = _plan_slots_for_package(
        "perl-CPAN-Meta-Requirements",
        spec,
        repo,
        force_build=True,
        distro_macros={"oreon": "11"},
    )
    assert slot.action == "build"
    assert slot.build_profile == "bootstrap"
    assert slot.rpmbuild_defines.get("with_cmr_enables_optional_test") == "0"
    assert slot.rpmbuild_defines.get("oreon") == "0"


def test_chain_cover_requires_solver_ok_or_chain_provider() -> None:
    from oreon_build.services.build_planner import _chain_covers_requirement
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def __init__(self, ok: bool, missing: set[str]):
            self._ok = ok
            self._missing = missing

        def resolve(self, reqs):
            return SolveResult(ok=self._ok, missing_provides=set(self._missing))

    repo = RepoCapabilityIndex()
    repo.finalize_lookups()
    distgit = {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}}
    assert not _chain_covers_requirement(
        "perl(Module::Build)",
        repo,
        set(),
        [distgit],
        solver=FakeSolver(False, {"perl(CPAN::Meta::Requirements::Range)"}),
    )
    assert _chain_covers_requirement(
        "perl(Module::Build)",
        repo,
        {"perl-CPAN-Meta-Requirements"},
        [distgit],
        solver=FakeSolver(False, {"perl(CPAN::Meta::Requirements::Range)"}),
    )
    assert _chain_covers_requirement(
        "perl(Module::Build)",
        repo,
        set(),
        [distgit],
        solver=FakeSolver(True, set()),
    )
