from __future__ import annotations

import io
import tarfile
from unittest.mock import patch

import pytest

from oreon_build.services.build_planner import plan_automatic_chain
from oreon_build.services.distgit_order import (
    _expand_chain_spec_closure,
    _tarball_spec_member_index,
)
from oreon_build.services.repo_capability_index import RepoCapabilityIndex


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
    "a": "Name: a\nVersion: 1\nRelease: 1\nBuildRequires: b\nProvides: a\n",
    "b": "Name: b\nVersion: 1\nRelease: 1\nBuildRequires: c\nProvides: b\n",
    "c": "Name: c\nVersion: 1\nRelease: 1\nProvides: c\n",
    "d": "Name: d\nVersion: 1\nRelease: 1\nProvides: d\n",
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


async def _noop_repo_expand(specs, parsed, repo, roots, **kwargs):
    return specs, parsed, []


async def _noop_br_expand(specs, parsed, repo, **kwargs):
    return specs, parsed, [], set()


@pytest.mark.asyncio
async def test_ci_chain_expands_missing_deps() -> None:
    releasename = "oreon-11-rp1"
    empty_repo = RepoCapabilityIndex()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(_SPECS),
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=empty_repo,
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=_noop_br_expand,
        ),
    ):
        plan = await plan_automatic_chain(
            releasename=releasename,
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
    releasename = "oreon-11-rp1"
    empty_repo = RepoCapabilityIndex()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(_SPECS),
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=empty_repo,
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=_noop_br_expand,
        ),
    ):
        plan = await plan_automatic_chain(
            releasename=releasename,
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
    releasename = "oreon-11-rp1"
    empty_repo = RepoCapabilityIndex()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(_SPECS),
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=empty_repo,
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=_noop_br_expand,
        ),
    ):
        plan = await plan_automatic_chain(
            releasename=releasename,
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["c", "d"],
            architectures=["x86_64"],
            headers={},
        )

    assert len(plan.layers) == 1
    assert {slot.name for slot in plan.layers[0]} == {"c", "d"}
