from __future__ import annotations

import io
import tarfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from oreon_build.core.rpm_requires import requirement_check_capabilities
from oreon_build.services.build_planner import plan_automatic_chain
from oreon_build.services.dep_plan.order import topo_layers
from oreon_build.services.distgit_order import (
    DistgitProvidesIndex,
    _expand_chain_spec_closure,
    _tarball_spec_member_index,
)
from oreon_build.services.repo_capability_index import RepoCapabilityIndex
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


def _make_mini_tarball(releasename: str, specs: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for pkg, body in specs.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=f"repo-{releasename}/{releasename}/{pkg}/{pkg}.spec")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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
    "d": """Name: d
Version: 1
Release: 1
Summary: d
License: MIT
Provides: d
%description
x
%prep
%build
%install
%files
""",
}


def _make_sparse_closure(specs: dict[str, str]):
    async def inner(roots, *, releasename, owner, repo, distgit_branch, headers, **kwargs):
        tarball = _make_mini_tarball(releasename, specs)
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
def _plan_patches(specs: dict[str, str], repo: RepoCapabilityIndex):
    async def _fake_provides(*_a, **_k):
        return _provides_index(specs)

    with (
        patch(
            "oreon_build.services.dep_plan.planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
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
        yield


@pytest.mark.asyncio
async def test_ci_chain_expands_missing_deps() -> None:
    with _plan_patches(_SPECS, RepoCapabilityIndex()):
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
    assert built == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_ci_chain_orders_changed_packages_by_dependency() -> None:
    with _plan_patches(_SPECS, RepoCapabilityIndex()):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["a", "b"],
            architectures=["x86_64"],
            headers={},
        )
    flat = [slot.name for layer in plan.layers for slot in layer]
    assert set(flat) == {"a", "b", "c"}
    assert flat.index("c") < flat.index("b") < flat.index("a")


@pytest.mark.asyncio
async def test_ci_chain_independent_packages_same_layer() -> None:
    with _plan_patches(_SPECS, RepoCapabilityIndex()):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["c", "d"],
            architectures=["x86_64"],
            headers={},
        )
    assert len(plan.layers) == 1
    assert {slot.name for slot in plan.layers[0]} == {"c", "d"}


def test_topo_cycle_break_prefers_providers_over_consumers() -> None:
    names = {"biber", "perl-A", "perl-B", "perl-C", "texlive-foo"}
    deps = {
        "biber": {"perl-A", "perl-B", "perl-C"},
        "perl-A": {"perl-B"},
        "perl-B": {"perl-C"},
        "perl-C": {"perl-A"},
        "texlive-foo": set(),
    }
    layers, cyc = topo_layers(names, deps)
    assert cyc is True
    flat = [n for layer in layers for n in layer]
    assert flat.index("texlive-foo") < flat.index("biber")
    assert flat.index("perl-A") < flat.index("biber")
    assert flat.index("perl-B") < flat.index("biber")
    assert flat.index("perl-C") < flat.index("biber")
