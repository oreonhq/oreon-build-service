from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from oreon_build.core.rpm_requires import requirement_check_capabilities
from oreon_build.services.dep_plan.errors import classify_misses, root_coverage_errors
from oreon_build.services.dep_plan.order import topo_layers
from oreon_build.services.dep_plan.types import PackageNode
from oreon_build.services.repo_capability_index import RepoCapabilityIndex
from oreon_build.services.repo_solver import OverlayPackage, SolveResult


class FakeMultiArchSolver:
    """Hermetic installability oracle for install_oracle tests."""

    def __init__(
        self,
        available: set[str] | None = None,
        *,
        repo_index: RepoCapabilityIndex | None = None,
        provider_sources: dict[str, set[str]] | None = None,
    ) -> None:
        self.available = set(available or ())
        self._overlay: dict[str, frozenset[str]] = {}
        self._repo_index = repo_index or RepoCapabilityIndex()
        self._provider_sources = {
            k: set(v) for k, v in (provider_sources or {}).items()
        }

    def set_overlay(self, packages: dict) -> None:
        self._overlay = {
            k: frozenset(getattr(v, "provides", None) or ())
            for k, v in (packages or {}).items()
        }

    def warm(self) -> None:
        return None

    def close(self) -> None:
        return None

    def capability_index(self) -> RepoCapabilityIndex:
        return self._repo_index

    def has_provider(self, requirement: str) -> bool:
        raw = (requirement or "").strip()
        if not raw:
            return False
        if raw in self.available:
            return True
        for provs in self._overlay.values():
            if raw in provs:
                return True
        if self._repo_index.has_capability(raw):
            return True
        return raw in self._provider_sources

    def sources_providing(self, requirement: str) -> set[str]:
        raw = (requirement or "").strip()
        out: set[str] = set()
        if raw in self._provider_sources:
            out |= self._provider_sources[raw]
        out |= set(self._repo_index.owner_sources_for_capability(raw) or ())
        return out

    def resolve(self, reqs) -> SolveResult:
        caps = set(self.available)
        for provs in self._overlay.values():
            caps |= set(provs)
        missing: set[str] = set()
        for r in reqs:
            raw = (r or "").strip()
            if not raw:
                continue
            if raw in caps or self.has_provider(raw):
                continue
            leaves = requirement_check_capabilities(raw) or [raw]
            if leaves and all(
                leaf in caps or self.has_provider(leaf) for leaf in leaves
            ):
                continue
            missing.add(raw)
        return SolveResult(
            ok=not missing,
            missing_provides=missing,
            problems="" if not missing else "nothing provides " + " | ".join(sorted(missing)),
        )


def test_topo_layers_provider_before_consumer() -> None:
    layers, cyc = topo_layers(
        {"a", "b", "c"},
        {"a": {"b"}, "b": {"c"}, "c": set()},
    )
    flat = [n for layer in layers for n in layer]
    assert not cyc
    assert flat.index("c") < flat.index("b") < flat.index("a")


def test_topo_layers_cycle_break() -> None:
    layers, cyc = topo_layers(
        {"biber", "perl-A", "perl-B"},
        {"biber": {"perl-A"}, "perl-A": {"perl-B"}, "perl-B": {"perl-A"}},
    )
    assert cyc
    flat = [n for layer in layers for n in layer]
    assert flat.index("perl-A") < flat.index("biber") or flat.index("perl-B") < flat.index("biber")


def test_topo_layers_cycle_does_not_duplicate_leaves() -> None:
    # go-vendor-tools <-> trivy cycle with rust-askalono-cli as a zero-indegree leaf
    layers, cyc = topo_layers(
        {"go-vendor-tools", "golist", "rust-askalono-cli", "sbctl", "trivy"},
        {
            "go-vendor-tools": {"rust-askalono-cli", "trivy"},
            "golist": {"go-vendor-tools"},
            "rust-askalono-cli": set(),
            "sbctl": {"golist"},
            "trivy": {"go-vendor-tools"},
        },
    )
    assert cyc
    flat = [n for layer in layers for n in layer]
    assert len(flat) == len(set(flat))
    assert flat.count("rust-askalono-cli") == 1


def test_classify_miss_import_vs_path() -> None:
    nodes = {
        "golist": PackageNode(
            name="golist",
            action="block",
            brs=["go-vendor-tools"],
            br_complete=True,
            br_evaluated=True,
            reason="missing hard buildrequires: go-vendor-tools",
        ),
        "go-vendor-tools": PackageNode(
            name="go-vendor-tools",
            action="block",
            brs=["askalono-cli"],
            br_complete=True,
            br_evaluated=True,
            reason="missing hard buildrequires: askalono-cli",
        ),
    }
    pe, imps, fetch = classify_misses(
        root="golist",
        nodes=nodes,
        solve=SolveResult(ok=False, missing_provides={"go-vendor-tools"}),
        brs=["go-vendor-tools"],
        provider_map={"go-vendor-tools": {"go-vendor-tools"}},
        repo_index=RepoCapabilityIndex(),
        package_names={"golist", "go-vendor-tools"},
    )
    assert any("go-vendor-tools" in e for e in pe)
    assert "go-vendor-tools" not in imps

    pe2, imps2, _ = classify_misses(
        root="golist",
        nodes={"golist": nodes["golist"]},
        solve=SolveResult(ok=False, missing_provides={"askalono-cli"}),
        brs=["askalono-cli"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"golist"},
    )
    assert "askalono-cli" in imps2
    assert not pe2 or "askalono-cli" in " ".join(imps2)


def test_classify_repo_provider_never_toasts_import() -> None:
    """perl(DDP)-class: solv miss but pool already Provides → never import."""

    class _Install:
        def has_provider(self, req: str) -> bool:
            return req == "perl(DDP)"

        def sources_providing(self, req: str) -> set[str]:
            return {"perl-Data-Printer"} if req == "perl(DDP)" else set()

    pe, imps, fetch = classify_misses(
        root="perl-Foo",
        nodes={"perl-Foo": PackageNode(name="perl-Foo", action="block", br_complete=True)},
        solve=SolveResult(ok=False, missing_provides={"perl(DDP)"}),
        brs=["perl(DDP)", "perl(Strict)"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"perl-Foo"},
        install=_Install(),
    )
    assert "perl(DDP)" not in imps
    assert "perl-Data-Printer" in fetch


def test_classify_distgit_perl_parent_not_import() -> None:
    pe, imps, fetch = classify_misses(
        root="perl-Git-CPAN-Patch",
        nodes={
            "perl-Git-CPAN-Patch": PackageNode(
                name="perl-Git-CPAN-Patch",
                action="block",
                br_complete=True,
            )
        },
        solve=SolveResult(
            ok=False,
            missing_provides={
                "perl(MooseX::App::Command)",
                "perl(DBIx::Class::Core)",
            },
        ),
        brs=["perl(MooseX::App::Command)", "perl(DBIx::Class::Core)"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={
            "perl-Git-CPAN-Patch",
            "perl-MooseX-App",
            "perl-DBIx-Class",
        },
    )
    assert not imps
    assert "perl-MooseX-App" in fetch
    assert "perl-DBIx-Class" in fetch


def test_providers_map_perl_ddp_via_metacpan(monkeypatch: pytest.MonkeyPatch) -> None:
    from oreon_build.services.dep_plan.providers import providers_for_requirement
    import oreon_build.services.perl_resolve as pr

    monkeypatch.setattr(pr, "metacpan_distribution_for_module", lambda m: "Data-Printer")
    got = providers_for_requirement(
        "perl(DDP)",
        provider_map={},
        package_names={"perl-Data-Printer", "perl-Git-CPAN-Patch"},
    )
    assert got == {"perl-Data-Printer"}


def test_classify_mono_xbuild_fetches_not_imports() -> None:
    pe, imps, fetch = classify_misses(
        root="sbctl",
        nodes={"sbctl": PackageNode(name="sbctl", action="block", br_complete=True)},
        solve=SolveResult(ok=False, missing_provides={"mono(xbuild)"}),
        brs=["mono(xbuild)"],
        provider_map={"mono(xbuild)": {"mono"}},
        repo_index=RepoCapabilityIndex(),
        package_names={"sbctl", "mono"},
    )
    assert "mono(xbuild)" not in imps
    assert "mono" in fetch


def test_classify_mono_xbuild_undeclared_is_not_guessed() -> None:
    pe, imps, fetch = classify_misses(
        root="sbctl",
        nodes={"sbctl": PackageNode(name="sbctl", action="block", br_complete=True)},
        solve=SolveResult(ok=False, missing_provides={"mono(xbuild)"}),
        brs=["mono(xbuild)"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"sbctl", "mono"},
    )
    assert "mono" not in fetch


def test_classify_spurious_version_miss_ignored() -> None:
    pe, imps, fetch = classify_misses(
        root="sbctl",
        nodes={"sbctl": PackageNode(name="sbctl", action="block", br_complete=True)},
        solve=SolveResult(ok=False, missing_provides={"4-0-2", "mono(xbuild)"}),
        brs=["mono(xbuild)"],
        provider_map={"mono(xbuild)": {"mono"}},
        repo_index=RepoCapabilityIndex(),
        package_names={"sbctl", "mono"},
    )
    assert "4-0-2" not in imps
    assert "mono" in fetch


def test_classify_mvn_gav_version_not_toast_as_4_0_2() -> None:
    pe, imps, fetch = classify_misses(
        root="sbctl",
        nodes={"sbctl": PackageNode(name="sbctl", action="block", br_complete=True)},
        solve=SolveResult(
            ok=False,
            missing_provides={"mvn(org.apache.commons:commons-lang3:4.0.2)"},
        ),
        brs=["mvn(org.apache.commons:commons-lang3:4.0.2)"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"sbctl", "commons-lang3"},
    )
    assert "4-0-2" not in imps
    assert "commons-lang3" in fetch


def test_classify_tex_subpackage_fetches_collection() -> None:
    pe, imps, fetch = classify_misses(
        root="sbctl",
        nodes={"sbctl": PackageNode(name="sbctl", action="block", br_complete=True)},
        solve=SolveResult(
            ok=False,
            missing_provides={"tex(a4wide.sty)", "texlive-tabulary"},
        ),
        brs=[],
        provider_map={
            "texlive-a4wide": {"texlive-collection-latexextra"},
            "texlive-tabulary": {"texlive-collection-latexextra"},
        },
        repo_index=RepoCapabilityIndex(),
        package_names={"sbctl", "texlive-collection-latexextra"},
    )
    assert not imps
    assert "texlive-collection-latexextra" in fetch


def test_classify_retired_hal_devel_not_import() -> None:
    pe, imps, fetch = classify_misses(
        root="kdelibs",
        nodes={"kdelibs": PackageNode(name="kdelibs", action="block", br_complete=True)},
        solve=SolveResult(ok=False, missing_provides={"hal-devel"}),
        brs=["hal-devel"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"kdelibs"},
    )
    assert "hal" not in imps
    assert "hal-devel" not in imps
    assert any("hal-devel" in e for e in pe)


def test_classify_solve_ok_no_imports() -> None:
    pe, imps, fetch = classify_misses(
        root="x",
        nodes={"x": PackageNode(name="x", action="build", br_complete=True)},
        solve=SolveResult(ok=True),
        brs=["perl(DDP)", "tex(a4wide.sty)"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"x"},
    )
    assert not imps
    assert not pe
    assert not fetch


def test_classify_never_inflates_misses_to_all_brs() -> None:
    pe, imps, fetch = classify_misses(
        root="x",
        nodes={"x": PackageNode(name="x", action="block", br_complete=True, reason="depsolve failed")},
        solve=SolveResult(ok=False, missing_provides=set(), problems="mysterious failure"),
        brs=["a", "b", "c", "d"],
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"x"},
    )
    assert not imps
    assert not fetch


def test_root_coverage_errors() -> None:
    nodes = {
        "sbctl": PackageNode(name="sbctl", action="block", reason="blocked"),
        "rust-x": PackageNode(name="rust-x", action="build"),
    }
    errs = root_coverage_errors(["sbctl"], nodes, ["sbctl: blocked"])
    assert errs
    assert "sbctl" in " ".join(errs)


def test_br_oracle_keeps_default_check_gated_brs() -> None:
    from oreon_build.services.dep_plan import br_oracle

    spec = """
Name: go-vendor-tools
Version: 1
Release: 1
Summary: x
License: MIT
%bcond check 1
%if %{with check}
BuildRequires: askalono-cli
BuildRequires: trivy
%endif
BuildRequires: python3-devel
%description
x
%prep
%build
%install
"""
    br = br_oracle.evaluate_package_brs(spec)
    assert br.complete
    assert "askalono-cli" in br.buildrequires
    assert "trivy" in br.buildrequires
    assert "python3-devel" in br.buildrequires


def test_crates_io_static_url_rewrite() -> None:
    from oreon_build.services.dynamic_brs import _crates_io_static_url

    assert (
        _crates_io_static_url(
            "https://crates.io/api/v1/crates/winnow/0.5.40/download#/winnow-0.5.40.crate"
        )
        == "https://static.crates.io/crates/winnow/winnow-0.5.40.crate"
    )
    assert _crates_io_static_url("https://example.com/foo.crate") is None


def test_nosrc_disk_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oreon_build.core.spec_bcond import SpecBcondInfo
    from oreon_build.services.dep_plan import br_cache, br_oracle
    from oreon_build.services.dep_plan.types import BrEval

    monkeypatch.setenv("OREON_BR_SQLITE_PATH", str(tmp_path / "br_cache.sqlite3"))
    br_cache._conn = None
    calls = {"n": 0}

    def fake_eval(spec_text, **kwargs):
        calls["n"] += 1
        return type(
            "R",
            (),
            {
                "buildrequires": ["cargo-rpm-macros", "(crate(foo) >= 1 with crate(foo) < 2)"],
                "complete": True,
                "error": "",
            },
        )()

    spec = "%generate_buildrequires\n%cargo_generate_buildrequires\nName: rust-x\n"
    empty = SpecBcondInfo(defaults={})
    with (
        patch.object(br_oracle, "spec_has_generate_buildrequires", return_value=True),
        patch.object(br_oracle, "spec_uses_cargo_generate_buildrequires", return_value=True),
        patch.object(br_oracle, "evaluate_generate_buildrequires", side_effect=fake_eval),
        patch.object(br_oracle, "parse_spec_bconds", return_value=empty),
    ):
        a = br_oracle.evaluate_package_brs(spec, pkg="rust-x")
        b = br_oracle.evaluate_package_brs(spec, pkg="rust-x")
    br_cache._conn = None
    assert a.complete and b.complete
    assert calls["n"] == 1
    assert isinstance(a, BrEval)
    assert "(crate(foo)" in " ".join(a.buildrequires)


def test_install_oracle_caches_by_overlay() -> None:
    from oreon_build.services.dep_plan.install_oracle import InstallOracle

    solver = FakeMultiArchSolver()
    oracle = InstallOracle(solver)
    oracle.set_overlay({})
    r1 = oracle.resolve(["foo"])
    assert not r1.ok
    solver.available.add("foo")
    # cache still misses until overlay fingerprint changes
    r2 = oracle.resolve(["foo"])
    assert not r2.ok
    oracle.set_overlay(
        {"x": OverlayPackage(source_name="x", provides=frozenset({"foo"}))}
    )
    assert oracle.resolve(["foo"]).ok
