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


def test_parse_no_matching_package_to_install() -> None:
    from oreon_build.services.repo_solver import parse_depsolve_missing

    assert parse_depsolve_missing(
        "No matching package to install: 'python3-matplotlib'\n"
        "Not all dependencies satisfied\n"
    ) == {"python3-matplotlib"}


def test_multiarch_exclusive_ok_when_present_on_some_arch() -> None:
    from oreon_build.services.repo_solver import (
        MultiArchRepoSolver,
        SolveResult,
        is_arch_unavailable_failure,
    )

    assert not is_arch_unavailable_failure(
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
    result = multi.resolve(["sgx-enclave-prebuilt-common"])
    assert result.ok is False
    assert "aarch64" in result.problems

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
    # soft-fail-all must not invent every BR as missing
    assert both_missing.resolve(["does-not-exist"]).missing_provides == {
        "does-not-exist"
    }


def test_multiarch_soft_fail_keeps_accurate_misses() -> None:
    from oreon_build.services.repo_solver import MultiArchRepoSolver, SolveResult

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
                SolveResult(
                    ok=False,
                    missing_provides={"only-this"},
                    problems="No match for argument: only-this",
                ),
            ),
            ArchSolver(
                "aarch64",
                SolveResult(
                    ok=False,
                    missing_provides={"only-this"},
                    problems="No match for argument: only-this",
                ),
            ),
        ]
    )
    r = multi.resolve(["only-this", "also-present-elsewhere"])
    assert r.ok is False
    assert r.missing_provides == {"only-this"}
    assert "also-present-elsewhere" not in r.missing_provides




def test_install_oracle_batch_cache() -> None:
    from oreon_build.services.dep_plan.install_oracle import InstallOracle
    from oreon_build.services.repo_solver import OverlayPackage, SolveResult

    class Fake:
        def __init__(self):
            self.n = 0
            self._overlay = {}

        def set_overlay(self, packages):
            self._overlay = packages or {}

        def resolve(self, reqs):
            self.n += 1
            caps = set()
            for op in self._overlay.values():
                caps |= set(getattr(op, "provides", ()) or ())
            miss = {r for r in reqs if r not in caps}
            return SolveResult(ok=not miss, missing_provides=miss)

        def has_provider(self, requirement: str) -> bool:
            for op in self._overlay.values():
                if requirement in (getattr(op, "provides", ()) or ()):
                    return True
            return False

        def sources_providing(self, requirement: str) -> set[str]:
            return set()

    fake = Fake()
    oracle = InstallOracle(fake)
    oracle.set_overlay({})
    assert not oracle.resolve(["x"]).ok
    assert fake.n == 1
    assert not oracle.resolve(["x"]).ok
    assert fake.n == 1
    oracle.set_overlay(
        {"p": OverlayPackage(source_name="p", provides=frozenset({"x"}))}
    )
    assert oracle.resolve(["x"]).ok
    assert fake.n == 2


def test_providers_map_crate_to_rust() -> None:
    from oreon_build.services.dep_plan.providers import providers_for_requirement

    rich = "(crate(serde/default) >= 1 with crate(serde/default) < 2)"
    got = providers_for_requirement(
        rich,
        provider_map={
            "crate(serde)": {"rust-serde"},
            "crate(serde/default)": {"rust-serde"},
            "rust-serde": {"rust-serde"},
        },
        package_names={"rust-serde"},
    )
    assert "rust-serde" in got


def test_fedora_provides_repo_urls(monkeypatch):
    from oreon_build import config
    from oreon_build.services.repo_solver import fedora_provides_repo_urls

    fake = config.Settings.model_construct(
        layer_provides_catalog="fedora",
        layer_provides_fedora_release=44,
        layer_provides_fedora_base="https://dl.fedoraproject.org/pub/fedora/linux",
    )
    monkeypatch.setattr(config, "get_settings", lambda: fake)
    urls = dict(fedora_provides_repo_urls("x86_64"))
    assert "fedora-44-everything" in urls
    assert urls["fedora-44-everything"].endswith("/releases/44/Everything/x86_64/os")
    assert urls["fedora-44-updates"].endswith("/updates/44/Everything/x86_64")


def test_catalog_oreon_mode_skips_fedora_urls(monkeypatch):
    from oreon_build import config
    from oreon_build.services import repo_solver

    fake = config.Settings.model_construct(
        layer_provides_catalog="oreon",
        layer_provides_fedora_release=44,
        layer_provides_fedora_base="https://dl.fedoraproject.org/pub/fedora/linux",
    )
    monkeypatch.setattr(config, "get_settings", lambda: fake)
    seen = {}

    def fake_get(*args, **kwargs):
        seen["kwargs"] = kwargs
        seen["args"] = args
        return "ok"

    monkeypatch.setattr(repo_solver, "get_repo_solver", fake_get)
    assert repo_solver.get_provides_catalog_solver("oreon-11-rp1", "sidetag", "x86_64") == "ok"
    assert not seen["kwargs"].get("repo_urls")
