from __future__ import annotations

import io
import tarfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from oreon_build.core.rpm_requires import requirement_check_capabilities
from oreon_build.services.build_planner import chain_spec_root_packages, plan_automatic_chain
from oreon_build.services.dep_plan.errors import classify_misses, root_coverage_errors
from oreon_build.services.dep_plan.order import topo_layers
from oreon_build.services.dep_plan.types import PackageNode
from oreon_build.services.distgit_order import (
    DistgitProvidesIndex,
    _expand_chain_spec_closure,
    _tarball_spec_member_index,
)
from oreon_build.services.repo_capability_index import RepoCapabilityIndex, clear_repo_capability_cache
from oreon_build.services.repo_solver import OverlayPackage, SolveResult


class FakeMultiArchSolver:
    """Hermetic installability oracle for planner tests."""

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


def _tarball(releasename: str, specs: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for pkg, body in specs.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=f"repo-{releasename}/{releasename}/{pkg}/{pkg}.spec")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_sparse_closure(specs: dict[str, str]):
    async def inner(roots, *, releasename, owner, repo, distgit_branch, headers, **kwargs):
        tarball = _tarball(releasename, specs)
        all_names, members = _tarball_spec_member_index(tarball, releasename)
        return _expand_chain_spec_closure(
            roots,
            all_names,
            tarball,
            releasename,
            members=members,
        )

    return inner


def _provides_index(specs: dict[str, str]) -> DistgitProvidesIndex:
    caps: dict[str, set[str]] = {}
    for name, body in specs.items():
        caps.setdefault(name, set()).add(name)
        for line in body.splitlines():
            if line.lower().startswith("provides:"):
                cap = line.split(":", 1)[1].strip()
                if cap:
                    caps.setdefault(cap, set()).add(name)
    return DistgitProvidesIndex(
        package_names=frozenset(specs),
        capability_providers={k: frozenset(v) for k, v in caps.items()},
    )


@contextmanager
def _plan_patches(
    specs: dict[str, str],
    *,
    repo: RepoCapabilityIndex,
    available: set[str] | None = None,
    provider_sources: dict[str, set[str]] | None = None,
):
    async def _fake_provides(*_a, **_k):
        return _provides_index(specs)

    with (
        patch(
            "oreon_build.services.dep_plan.planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.dep_plan.planner.get_repo_solver",
            return_value=FakeMultiArchSolver(
                available,
                repo_index=repo,
                provider_sources=provider_sources,
            ),
        ),
        patch(
            "oreon_build.services.dep_plan.planner.get_distgit_provides_index_cached",
            side_effect=_fake_provides,
        ),
        patch(
            "oreon_build.services.dep_plan.expand._download_distgit_local_sources",
            return_value=[],
        ),
    ):
        yield


_SPECS = {
    "a": """Name: a
Version: 1
Release: 1
Summary: a
License: MIT
BuildRequires: b
Provides: a
%description
x
%prep
%build
%install
%files
""",
    "b": """Name: b
Version: 1
Release: 1
Summary: b
License: MIT
BuildRequires: c
Provides: b
%description
x
%prep
%build
%install
%files
""",
    "c": """Name: c
Version: 1
Release: 1
Summary: c
License: MIT
Provides: c
%description
x
%prep
%build
%install
%files
""",
}


@pytest.mark.asyncio
async def test_planner_hard_fails_without_libsolv() -> None:
    clear_repo_capability_cache()

    def _boom(*_a, **_k):
        raise RuntimeError("python-solv bindings unavailable")

    with patch(
        "oreon_build.services.dep_plan.planner.get_repo_solver",
        side_effect=_boom,
    ):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["a"],
            architectures=["x86_64"],
            headers={},
        )
    assert not plan.layers
    assert not plan.blocking_errors
    assert any("libsolv" in e.lower() for e in plan.errors)

    clear_repo_capability_cache()
    with _plan_patches(_SPECS, repo=RepoCapabilityIndex()):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["a"],
            architectures=["x86_64"],
            headers={},
        )
    built = {slot.name for layer in plan.layers for slot in layer}
    assert built == {"c", "b", "a"}
    assert plan.chain_spec.index("c") < plan.chain_spec.index("b") < plan.chain_spec.index("a")


@pytest.mark.asyncio
async def test_planner_skips_repo_satisfied_deps() -> None:
    clear_repo_capability_cache()
    repo = RepoCapabilityIndex(
        source_names={"b", "c"},
        binary_names={"b", "c"},
        provides={"b", "c"},
    )
    with _plan_patches(_SPECS, repo=repo, available={"b", "c"}):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["a"],
            architectures=["x86_64"],
            headers={},
        )
    built = {slot.name for layer in plan.layers for slot in layer}
    assert built == {"a"}
    assert "b" not in built


@pytest.mark.asyncio
async def test_planner_import_when_no_distgit_provider() -> None:
    clear_repo_capability_cache()
    specs = {
        "foo": (
            "Name: foo\nVersion: 1\nRelease: 1\nSummary: foo\nLicense: MIT\n"
            "BuildRequires: missing-lib\nProvides: foo\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
    }
    with _plan_patches(specs, repo=RepoCapabilityIndex(provides={"glibc"}, binary_names={"glibc"})):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["foo"],
            architectures=["x86_64"],
            headers={},
        )
    assert "missing-lib" in " ".join(plan.blocking_errors)
    assert "foo" not in {slot.name for layer in plan.layers for slot in layer}


@pytest.mark.asyncio
async def test_sbctl_shaped_expand_order_and_root_gate() -> None:
    """golist / go-vendor-tools / askalono-cli / sbctl hermetic fixture."""
    clear_repo_capability_cache()
    specs = {
        "sbctl": (
            "Name: sbctl\nVersion: 1\nRelease: 1\nSummary: s\nLicense: MIT\n"
            "BuildRequires: golist\nBuildRequires: askalono-cli\nProvides: sbctl\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
        "golist": (
            "Name: golist\nVersion: 1\nRelease: 1\nSummary: g\nLicense: MIT\n"
            "BuildRequires: go-vendor-tools\nProvides: golist\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
        "go-vendor-tools": (
            "Name: go-vendor-tools\nVersion: 1\nRelease: 1\nSummary: v\nLicense: MIT\n"
            "BuildRequires: askalono-cli\nProvides: go-vendor-tools\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
        "askalono-cli": (
            "Name: askalono-cli\nVersion: 1\nRelease: 1\nSummary: a\nLicense: MIT\n"
            "Provides: askalono-cli\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
    }
    with _plan_patches(specs, repo=RepoCapabilityIndex()):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["sbctl"],
            architectures=["x86_64"],
            headers={},
        )
    flat = [slot.name for layer in plan.layers for slot in layer]
    assert "sbctl" in flat
    assert flat.index("askalono-cli") < flat.index("go-vendor-tools")
    assert flat.index("go-vendor-tools") < flat.index("golist")
    assert flat.index("golist") < flat.index("sbctl")
    assert "sbctl" in plan.package_plans


@pytest.mark.asyncio
async def test_root_uncovered_when_orphans_only() -> None:
    """996-style: if root blocks, plan errors must surface uncovered root."""
    clear_repo_capability_cache()
    specs = {
        "sbctl": (
            "Name: sbctl\nVersion: 1\nRelease: 1\nSummary: s\nLicense: MIT\n"
            "BuildRequires: totally-missing-cap\nProvides: sbctl\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
        "rust-clap_builder": (
            "Name: rust-clap_builder\nVersion: 1\nRelease: 1\nSummary: r\nLicense: MIT\n"
            "Provides: rust-clap_builder\n"
            "%description\nx\n%prep\n%build\n%install\n%files\n"
        ),
    }
    with _plan_patches(specs, repo=RepoCapabilityIndex()):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["sbctl"],
            architectures=["x86_64"],
            headers={},
        )
    assert "sbctl" not in plan.package_plans
    assert plan.errors or plan.blocking_errors


def test_chain_spec_root_packages() -> None:
    assert chain_spec_root_packages("a b : c") == ["a", "b", "c"]


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
        provider_map={},
        repo_index=RepoCapabilityIndex(),
        package_names={"sbctl", "mono"},
    )
    assert "mono(xbuild)" not in imps
    assert "mono" in fetch


def test_classify_spurious_version_miss_ignored() -> None:
    pe, imps, fetch = classify_misses(
        root="sbctl",
        nodes={"sbctl": PackageNode(name="sbctl", action="block", br_complete=True)},
        solve=SolveResult(ok=False, missing_provides={"4-0-2", "mono(xbuild)"}),
        brs=["mono(xbuild)"],
        provider_map={},
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
    from oreon_build.services.dep_plan import br_oracle
    from oreon_build.services.dep_plan.types import BrEval

    monkeypatch.setenv("OREON_DYNBR_CACHE_DIR", str(tmp_path))
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
        a = br_oracle.evaluate_package_brs(spec)
        b = br_oracle.evaluate_package_brs(spec)
    assert a.complete and b.complete
    assert calls["n"] == 1
    assert isinstance(a, BrEval)
    assert "(crate(foo)" in " ".join(a.buildrequires)


def test_cargo_incomplete_blocks_in_decide() -> None:
    from oreon_build.services.dep_plan.expand import _decide_action
    from oreon_build.services.dep_plan.install_oracle import InstallOracle

    node = PackageNode(
        name="rust-bstr",
        has_generate=True,
        br_complete=False,
        br_evaluated=True,
        brs=["cargo-rpm-macros"],
        br_error="cargo buildreqs nosrc missing",
        spec_text="%cargo_generate_buildrequires\n",
    )
    install = InstallOracle(FakeMultiArchSolver())
    with patch(
        "oreon_build.services.dep_plan.expand.spec_uses_cargo_generate_buildrequires",
        return_value=True,
    ):
        _decide_action(
            "rust-bstr",
            node,
            roots={"rust-bstr"},
            force_rebuild=set(),
            repo_index=RepoCapabilityIndex(),
            install=install,
            distro_macros={},
            target_arches={"x86_64"},
        )
    assert node.action == "block"
    assert "cargo" in node.reason.lower() or "generate" in node.reason.lower()


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
