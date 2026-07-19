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


def test_multiarch_exclusive_ok_when_present_on_some_arch() -> None:
    from oreon_build.services.repo_solver import (
        MultiArchRepoSolver,
        SolveResult,
        is_arch_unavailable_failure,
    )

    assert is_arch_unavailable_failure(
        SolveResult(ok=False, missing_provides={"sgx"}, problems="No match for argument: sgx")
    )
    assert not is_arch_unavailable_failure(
        SolveResult(
            ok=False,
            missing_provides={"perl(CPAN::Meta::Requirements::Range)"},
            problems="nothing provides perl(CPAN::Meta::Requirements::Range) needed by "
            "perl-CPAN-Meta-Requirements-2.140-524.or11.noarch",
        )
    )

    class ArchSolver:
        def __init__(self, arch: str, result: SolveResult):
            self.arch = arch
            self._result = result

        def resolve(self, _reqs):
            return self._result

    multi = MultiArchRepoSolver(
        [
            ArchSolver(
                "x86_64",
                SolveResult(ok=True),
            ),
            ArchSolver(
                "aarch64",
                SolveResult(
                    ok=False,
                    missing_provides={"sgx-enclave-prebuilt-common"},
                    problems="No match for argument: sgx-enclave-prebuilt-common",
                ),
            ),
        ]
    )
    assert multi.resolve(["sgx-enclave-prebuilt-common"]).ok is True

    both_missing = MultiArchRepoSolver(
        [
            ArchSolver(
                "x86_64",
                SolveResult(
                    ok=False,
                    missing_provides={"does-not-exist"},
                    problems="No match for argument: does-not-exist",
                ),
            ),
            ArchSolver(
                "aarch64",
                SolveResult(
                    ok=False,
                    missing_provides={"does-not-exist"},
                    problems="No match for argument: does-not-exist",
                ),
            ),
        ]
    )
    assert both_missing.resolve(["does-not-exist"]).ok is False


def test_force_skips_published_installable_providers() -> None:
    """dont rebuild a published provider when dnf can already install that pkg."""
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def resolve(self, reqs):
            return SolveResult(
                ok=False,
                missing_provides={"perl(Foo)"},
                problems="nothing provides perl(Foo)",
            )

        def can_install(self, reqs):
            # cap hole looks unusable, but the provider rpm itself installs fine
            return set(reqs) != {"perl(Foo)"}

    repo = RepoCapabilityIndex(
        binary_names={"perl-Foo"},
        provides={"perl(Module::Build)", "perl(Foo)"},
        capability_providers={
            "perl(Module::Build)": {"perl-Foo"},
            "perl(Foo)": {"perl-Foo"},
        },
        binary_source={"perl-Foo": "perl-Foo"},
        source_names={"perl-Foo", "app"},
    )
    repo.finalize_lookups()
    spec = "Name: app\nVersion: 1\nRelease: 1\nBuildRequires: perl(Module::Build)\nProvides: app\n"
    parsed = {"app": _parse_spec_text(spec, "app.spec")}
    force, ok, bad, _fetch = bp._provider_rebuild_force_sources(
        {"app"},
        parsed,
        {"app": spec},
        repo,
        {"perl(Foo)": {"perl-Foo"}},
        solver=FakeSolver(),
    )
    assert "perl-Foo" not in force
    assert not ok
    assert "app" in bad


def test_force_still_rebuilds_uninstallable_provider() -> None:
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def resolve(self, reqs):
            return SolveResult(
                ok=False,
                missing_provides={"perl(CPAN::Meta::Requirements::Range)"},
                broken_binaries={"perl-CPAN-Meta-Requirements"},
                problems="nothing provides perl(CPAN::Meta::Requirements::Range)",
            )

        def can_install(self, reqs):
            bad = {
                "perl(CPAN::Meta::Requirements::Range)",
                "perl-CPAN-Meta-Requirements",
            }
            return not (set(reqs) & bad)

    repo = RepoCapabilityIndex(
        binary_names={"perl-CPAN-Meta-Requirements"},
        provides={"perl(Module::Build)", "perl(CPAN::Meta::Requirements)"},
        capability_providers={
            "perl(Module::Build)": {"perl-CPAN-Meta-Requirements"},
            "perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"},
        },
        binary_source={"perl-CPAN-Meta-Requirements": "perl-CPAN-Meta-Requirements"},
        source_names={"perl-CPAN-Meta-Requirements", "app"},
    )
    repo.finalize_lookups()
    spec = "Name: app\nVersion: 1\nRelease: 1\nBuildRequires: perl(Module::Build)\nProvides: app\n"
    parsed = {"app": _parse_spec_text(spec, "app.spec")}
    force, ok, bad, _fetch = bp._provider_rebuild_force_sources(
        {"app"},
        parsed,
        {"app": spec},
        repo,
        {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}},
        solver=FakeSolver(),
    )
    assert "perl-CPAN-Meta-Requirements" in force
    assert not ok
    assert "app" in bad


def test_force_skips_optional_test_misses_after_bootstrap() -> None:
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def resolve(self, reqs):
            req_set = set(reqs)
            if "perl(JSON::MaybeXS)" in req_set:
                return SolveResult(
                    ok=False,
                    missing_provides={"perl(JSON::MaybeXS)", "perl(Test::Class)"},
                    problems="No matching package to install: 'perl(JSON::MaybeXS)'",
                )
            return SolveResult(ok=True)

        def can_install(self, reqs):
            return self.resolve(reqs).ok

    repo = RepoCapabilityIndex(
        provides={"perl(ExtUtils::MakeMaker)", "make", "perl-generators"},
        capability_providers={
            "perl(ExtUtils::MakeMaker)": {"perl-ExtUtils-MakeMaker"},
            "perl(JSON::MaybeXS)": {"perl-JSON-MaybeXS"},
            "perl(Test::Class)": {"perl-Test-Class"},
        },
        source_names={"perl-Test-Simple", "perl-JSON-MaybeXS", "perl-Test-Class"},
        binary_names={"perl-Test-Simple"},
        binary_source={"perl-Test-Simple": "perl-Test-Simple"},
    )
    repo.finalize_lookups()
    spec = """
Name: perl-Test-Simple
Version: 1
Release: 1
%bcond_without optional_test
BuildRequires: perl(ExtUtils::MakeMaker)
%if %{with optional_test}
BuildRequires: perl(JSON::MaybeXS)
BuildRequires: perl(Test::Class)
%endif
Provides: perl(Test::More)
"""
    parsed = {"perl-Test-Simple": _parse_spec_text(spec, "pts.spec")}
    force, ok, bad, fetch = bp._provider_rebuild_force_sources(
        {"perl-Test-Simple"},
        parsed,
        {"perl-Test-Simple": spec},
        repo,
        distro_macros={"oreon": "11"},
        solver=FakeSolver(),
    )
    assert "perl-JSON-MaybeXS" not in force
    assert "perl-Test-Class" not in force
    assert "perl-JSON-MaybeXS" not in fetch
    assert ok is True
    assert not bad


def test_truth_blocks_when_broken_pkg_only_requires_missing_caps() -> None:
    """perl-Coro Requires BDB/AIO it will never Provide. Rebuilding Coro is not a repair."""
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def set_overlay(self, _packages):
            return None

        def resolve(self, reqs):
            return SolveResult(
                ok=False,
                missing_provides={
                    "perl(BDB)",
                    "perl(AnyEvent::AIO) >= 1",
                    "perl(IO::AIO) >= 3.1",
                },
                broken_binaries={"perl-Coro"},
                problems=(
                    "nothing provides perl(BDB) needed by perl-Coro-6.570-24.or11.x86_64\n"
                    "nothing provides perl(AnyEvent::AIO) >= 1 needed by perl-Coro-6.570-24.or11.x86_64"
                ),
            )

        def can_install(self, reqs):
            return False

    repo = RepoCapabilityIndex(
        source_names={"perl-Coro", "perl-Coro-Multicore", "perl-AnyEvent-AIO", "perl-IO-AIO"},
        binary_names={"perl-Coro"},
        provides={"perl(Coro)", "perl(Coro::MakeMaker)"},
        capability_providers={
            "perl(Coro)": {"perl-Coro"},
            "perl(AnyEvent::AIO)": {"perl-AnyEvent-AIO"},
            "perl(IO::AIO)": {"perl-IO-AIO"},
        },
        binary_source={"perl-Coro": "perl-Coro"},
    )
    repo.finalize_lookups()
    specs = {
        "perl-Coro-Multicore": (
            "Name: perl-Coro-Multicore\nVersion: 1\nRelease: 1\n"
            "BuildRequires: perl(Coro) >= 6.44\nProvides: perl(Coro::Multicore)\n"
        ),
        "perl-Coro": (
            "Name: perl-Coro\nVersion: 1\nRelease: 1\n"
            "Requires: perl(BDB)\nRequires: perl(AnyEvent::AIO) >= 1\n"
            "Provides: perl(Coro)\nProvides: perl(Coro::BDB)\n"
        ),
    }
    parsed = {n: _parse_spec_text(t, f"{n}.spec") for n, t in specs.items()}
    distgit = {
        "perl(AnyEvent::AIO)": {"perl-AnyEvent-AIO"},
        "perl(IO::AIO)": {"perl-IO-AIO"},
    }
    # no provider for perl(BDB) → must block Multicore; Coro alone is not a repair
    edges, blocked = bp._repo_truth_repair_plan(
        ["perl-Coro-Multicore", "perl-Coro"],
        {"perl-Coro"},
        parsed,
        specs,
        repo,
        distgit,
        None,
        None,
        FakeSolver(),
    )
    assert "perl-Coro-Multicore" in blocked
    assert edges.get("perl-Coro-Multicore") is None

    mapped = bp._map_resolve_failures_to_sources(
        {"perl(BDB)", "perl(AnyEvent::AIO) >= 1", "perl(IO::AIO) >= 3.1"},
        {"perl-Coro"},
        repo,
        bp._merge_provider_maps(bp._provider_map_from_specs(parsed), distgit),
    )
    assert "perl-Coro" not in mapped
    assert "perl-AnyEvent-AIO" in mapped
    assert "perl-IO-AIO" in mapped


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


def test_repo_truth_gate_orders_force_before_consumer() -> None:
    from oreon_build.services.build_planner import _build_layers, PlannedPackage
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex

    repo = RepoCapabilityIndex()
    repo.finalize_lookups()
    specs = {
        "app": "Name: app\nVersion: 1\nRelease: 1\nBuildRequires: make\nProvides: app\n",
        "perl-CPAN-Meta-Requirements": (
            "Name: perl-CPAN-Meta-Requirements\nVersion: 1\nRelease: 1\n"
            "Provides: perl(CPAN::Meta::Requirements::Range)\n"
        ),
    }
    parsed = {n: _parse_spec_text(t, f"{n}.spec") for n, t in specs.items()}
    slots = {
        "app": PlannedPackage("app", "build", "full", {}),
        "perl-CPAN-Meta-Requirements": PlannedPackage(
            "perl-CPAN-Meta-Requirements", "build", "bootstrap", {"with_x": "0"}
        ),
    }
    layers, _cyc, _err = _build_layers(
        ["app", "perl-CPAN-Meta-Requirements"],
        parsed,
        slots,
        repo,
        force_set={"app", "perl-CPAN-Meta-Requirements"},
        repair_edges={"app": {"perl-CPAN-Meta-Requirements"}},
        provider_force={"perl-CPAN-Meta-Requirements"},
    )
    flat = [p.name for layer in layers for p in layer]
    assert flat.index("perl-CPAN-Meta-Requirements") < flat.index("app")


def test_repo_truth_gates_roots_not_skipped_as_force(monkeypatch) -> None:
    """Roots are force_set forever. Truth must still edge them onto provider_force."""
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def set_overlay(self, _packages):
            return None

        def resolve(self, reqs):
            return SolveResult(
                ok=False,
                missing_provides={"perl(CPAN::Meta::Requirements::Range)"},
                broken_binaries={"perl-CPAN-Meta-Requirements"},
                problems="nothing provides perl(CPAN::Meta::Requirements::Range)",
            )

    repo = RepoCapabilityIndex(
        source_names={"perl-CPAN-Meta-Requirements"},
        binary_names={"perl-CPAN-Meta-Requirements", "perl-Module-CPANfile"},
        provides={"perl(Module::CPANfile)", "perl-CPAN-Meta-Requirements"},
        capability_providers={
            "perl(Module::CPANfile)": {"perl-Module-CPANfile"},
            "perl-CPAN-Meta-Requirements": {"perl-CPAN-Meta-Requirements"},
        },
        binary_source={"perl-CPAN-Meta-Requirements": "perl-CPAN-Meta-Requirements"},
    )
    repo.finalize_lookups()
    specs = {
        "perl-ExtUtils-MakeMaker-CPANfile": (
            "Name: perl-ExtUtils-MakeMaker-CPANfile\nVersion: 1\nRelease: 1\n"
            "BuildRequires: perl(Module::CPANfile)\nProvides: perl(ExtUtils::MakeMaker::CPANfile)\n"
        ),
        "perl-CPAN-Meta-Requirements": (
            "Name: perl-CPAN-Meta-Requirements\nVersion: 1\nRelease: 1\n"
            "Provides: perl(CPAN::Meta::Requirements::Range)\n"
        ),
    }
    parsed = {n: _parse_spec_text(t, f"{n}.spec") for n, t in specs.items()}
    edges, blocked = bp._repo_truth_repair_plan(
        ["perl-ExtUtils-MakeMaker-CPANfile", "perl-CPAN-Meta-Requirements"],
        {"perl-CPAN-Meta-Requirements"},
        parsed,
        specs,
        repo,
        {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}},
        None,
        None,
        FakeSolver(),
    )
    assert blocked == set()
    assert edges.get("perl-ExtUtils-MakeMaker-CPANfile") == {"perl-CPAN-Meta-Requirements"}


def test_repo_truth_blocks_when_no_force_repair(monkeypatch) -> None:
    from oreon_build.services import build_planner as bp
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex
    from oreon_build.services.repo_solver import SolveResult

    class FakeSolver:
        def set_overlay(self, _packages):
            return None

        def resolve(self, reqs):
            return SolveResult(
                ok=False,
                missing_provides={"perl(CPAN::Meta::Requirements::Range)"},
                broken_binaries={"perl-CPAN-Meta-Requirements"},
                problems="nothing provides perl(CPAN::Meta::Requirements::Range) "
                "needed by perl-CPAN-Meta-Requirements-2.140-524.or11.noarch",
            )

    repo = RepoCapabilityIndex(
        source_names={"perl-CPAN-Meta-Requirements"},
        binary_names={"perl-CPAN-Meta-Requirements"},
        provides={"perl(Module::CPANfile)", "perl-CPAN-Meta-Requirements"},
        capability_providers={
            "perl(Module::CPANfile)": {"perl-Module-CPANfile"},
            "perl-CPAN-Meta-Requirements": {"perl-CPAN-Meta-Requirements"},
        },
        binary_source={"perl-CPAN-Meta-Requirements": "perl-CPAN-Meta-Requirements"},
    )
    repo.finalize_lookups()
    specs = {
        "app": (
            "Name: app\nVersion: 1\nRelease: 1\n"
            "BuildRequires: perl(Module::CPANfile)\nProvides: app\n"
        ),
    }
    parsed = {n: _parse_spec_text(t, f"{n}.spec") for n, t in specs.items()}
    edges, blocked = bp._repo_truth_repair_plan(
        ["app"],
        set(),
        parsed,
        specs,
        repo,
        {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}},
        None,
        None,
        FakeSolver(),
    )
    assert "app" in blocked
    assert edges == {}

    edges2, blocked2 = bp._repo_truth_repair_plan(
        ["app", "perl-CPAN-Meta-Requirements"],
        {"perl-CPAN-Meta-Requirements"},
        {
            **parsed,
            "perl-CPAN-Meta-Requirements": _parse_spec_text(
                "Name: perl-CPAN-Meta-Requirements\nVersion: 1\nRelease: 1\n"
                "Provides: perl(CPAN::Meta::Requirements::Range)\n",
                "cmr.spec",
            ),
        },
        {
            **specs,
            "perl-CPAN-Meta-Requirements": (
                "Name: perl-CPAN-Meta-Requirements\nVersion: 1\nRelease: 1\n"
                "Provides: perl(CPAN::Meta::Requirements::Range)\n"
            ),
        },
        repo,
        {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}},
        None,
        None,
        FakeSolver(),
    )
    assert blocked2 == set()
    assert edges2.get("app") == {"perl-CPAN-Meta-Requirements"}



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
