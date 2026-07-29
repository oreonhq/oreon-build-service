from __future__ import annotations

import io
import tarfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from oreon_build.core.rpm_requires import requirement_check_capabilities
from oreon_build.services.build_planner import plan_automatic_chain
from oreon_build.services.distgit_order import (
    DistgitProvidesIndex,
    _expand_chain_spec_closure,
    _tarball_spec_member_index,
)
from oreon_build.services.repo_capability_index import RepoCapabilityIndex, clear_repo_capability_cache
from oreon_build.services.repo_solver import SolveResult


class FakeMultiArchSolver:
    def __init__(
        self,
        available: set[str] | None = None,
        *,
        repo_index: RepoCapabilityIndex | None = None,
    ) -> None:
        self.available = set(available or ())
        self._overlay: dict[str, frozenset[str]] = {}
        self._repo_index = repo_index or RepoCapabilityIndex()

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
        if raw in self.available:
            return True
        for provs in self._overlay.values():
            if raw in provs:
                return True
        return self._repo_index.has_capability(raw)

    def sources_providing(self, requirement: str) -> set[str]:
        return set(self._repo_index.owner_sources_for_capability(requirement) or ())

    def resolve(self, reqs) -> SolveResult:
        caps = set(self.available)
        for provs in self._overlay.values():
            caps |= set(provs)
        missing: set[str] = set()
        for r in reqs:
            raw = (r or "").strip()
            if not raw or raw in caps or self.has_provider(raw):
                continue
            leaves = requirement_check_capabilities(raw) or [raw]
            if leaves and all(
                leaf in caps or self.has_provider(leaf) for leaf in leaves
            ):
                continue
            missing.add(raw)
        return SolveResult(ok=not missing, missing_provides=missing)


def _tarball(releasename: str, specs: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for pkg, body in specs.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=f"repo-{releasename}/{releasename}/{pkg}/{pkg}.spec")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _fake_sparse(roots, *, releasename, owner, repo, distgit_branch, headers, **kwargs):
    specs = {
        "im": """Name: im
Version: 1
Release: 1
Summary: im
License: MIT
BuildRequires: pkgconfig(libcgraph)
Provides: im
%description
x
%prep
%build
%install
%files
""",
        "gv-devel": """Name: gv-devel
Version: 1
Release: 1
Summary: gv
License: MIT
Provides: pkgconfig(libcgraph)
%description
x
%prep
%build
%install
%files
""",
        "devil-src": """Name: devil-src
Version: 1
Release: 1
Summary: d
License: MIT
Provides: libIL.so.1()(64bit)
%description
x
%prep
%build
%install
%files
""",
    }
    tarball = _tarball(releasename, specs)
    all_names, members = _tarball_spec_member_index(tarball, releasename)
    # seed im + provider so expand can schedule without network fetch
    seed = sorted(set(roots) | {"gv-devel"})
    return _expand_chain_spec_closure(seed, all_names, tarball, releasename, members=members)


def _provides_index() -> DistgitProvidesIndex:
    return DistgitProvidesIndex(
        package_names=frozenset({"im", "gv-devel", "devil-src"}),
        capability_providers={
            "im": frozenset({"im"}),
            "pkgconfig(libcgraph)": frozenset({"gv-devel"}),
            "gv-devel": frozenset({"gv-devel"}),
            "libIL.so.1()(64bit)": frozenset({"devil-src"}),
            "devil-src": frozenset({"devil-src"}),
        },
    )


@pytest.mark.asyncio
async def test_repo_transitive_pulls_missing_source() -> None:
    """When a BR provider is missing from repos, expand pulls its distgit source."""
    clear_repo_capability_cache()
    # pkgconfig present in repo would skip gv-devel, but leave devil hole for
    # runtime. New planner only expands BuildRequires via solv misses.
    # Hermetic: no repo caps so im needs gv-devel via map, gv-devel has no BRs.
    repo = RepoCapabilityIndex()

    async def _fake_provides(*_a, **_k):
        return _provides_index()

    with (
        patch(
            "oreon_build.services.dep_plan.planner.expand_chain_spec_closure_sparse",
            side_effect=_fake_sparse,
        ),
        patch(
            "oreon_build.services.dep_plan.planner.get_repo_solver",
            return_value=FakeMultiArchSolver(repo_index=repo),
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
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["im"],
            architectures=["x86_64"],
            headers={},
        )

    built = {slot.name for layer in plan.layers for slot in layer}
    assert "im" in built
    assert "gv-devel" in built
