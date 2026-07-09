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
from oreon_build.services.repo_capability_index import RepoCapabilityIndex, clear_repo_capability_cache


def _tarball(releasename: str, specs: dict[str, str]) -> bytes:
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
}


async def _fake_sparse_closure(roots, *, releasename, owner, repo, distgit_branch, headers):
    tarball = _tarball(releasename, _SPECS)
    all_names, members = _tarball_spec_member_index(tarball, releasename)
    return _expand_chain_spec_closure(
        roots,
        all_names,
        tarball,
        releasename,
        members=members,
    )


def _make_sparse_closure(specs: dict[str, str]):
    async def inner(roots, *, releasename, owner, repo, distgit_branch, headers):
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


@pytest.mark.asyncio
async def test_planner_expands_missing_closure() -> None:
    clear_repo_capability_cache()
    empty_repo = RepoCapabilityIndex()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_fake_sparse_closure,
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=empty_repo,
        ),
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

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_fake_sparse_closure,
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=repo,
        ),
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

    built = {slot.name for layer in plan.layers for slot in layer}
    assert built == {"a"}
    assert "b" in plan.skipped
    assert "c" in plan.skipped


@pytest.mark.asyncio
async def test_planner_bootstrap_profile_for_conditional_br() -> None:
    clear_repo_capability_cache()
    specs = {
        "foo": """
Name: foo
Version: 1
Release: 1
%bcond_with check
%if %{with_check}
BuildRequires: missing-lib
%endif
BuildRequires: glibc
Provides: foo
""",
    }
    repo = RepoCapabilityIndex(provides={"glibc"}, binary_names={"glibc"})

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=repo,
        ),
    ):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["foo"],
            architectures=["x86_64"],
            headers={},
        )

    assert plan.package_plans["foo"]["build_profile"] == "bootstrap"
    assert plan.package_plans["foo"]["rpmbuild_defines"].get("with_check") == "0"


@pytest.mark.asyncio
async def test_planner_orders_requires_closure() -> None:
    clear_repo_capability_cache()
    specs = {
        "app": "Name: app\nVersion: 1\nRelease: 1\nRequires: lib\nProvides: app\n",
        "lib": "Name: lib\nVersion: 1\nRelease: 1\nProvides: lib\n",
    }
    empty_repo = RepoCapabilityIndex()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.load_repo_capability_index",
            return_value=empty_repo,
        ),
    ):
        plan = await plan_automatic_chain(
            releasename="oreon-11-rp1",
            repo_branch="dev",
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["app"],
            architectures=["x86_64"],
            headers={},
        )

    built = [slot.name for layer in plan.layers for slot in layer]
    assert built == ["lib", "app"]


def test_chain_spec_root_packages_dedupes() -> None:
    from oreon_build.services.build_planner import chain_spec_root_packages

    assert chain_spec_root_packages("a b : c a") == ["a", "b", "c"]
