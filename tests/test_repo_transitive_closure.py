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


async def _fake_sparse(roots, *, releasename, owner, repo, distgit_branch, headers, **kwargs):
    specs = {
        "im": "Name: im\nVersion: 1\nRelease: 1\nBuildRequires: pkgconfig(libcgraph)\nProvides: im\n",
        "gv-devel": "Name: gv-devel\nVersion: 1\nRelease: 1\nProvides: pkgconfig(libcgraph)\n",
    }
    tarball = _tarball(releasename, specs)
    all_names, members = _tarball_spec_member_index(tarball, releasename)
    return _expand_chain_spec_closure(roots, all_names, tarball, releasename, members=members)


async def _noop_repo_expand(specs, parsed, repo, **kwargs):
    return specs, parsed, []


async def _noop_br_expand(specs, parsed, repo, **kwargs):
    return specs, parsed, [], set()


@pytest.mark.asyncio
async def test_repo_transitive_pulls_missing_source() -> None:
    clear_repo_capability_cache()
    repo = RepoCapabilityIndex(
        binary_names={"gv-devel", "gv-devil"},
        provides={"pkgconfig(libcgraph)", "gv-devel", "gv-devil"},
        capability_providers={
            "pkgconfig(libcgraph)": {"gv-devel"},
            "gv-devel": {"gv-devel"},
            "gv-devil": {"gv-devil"},
            "libIL.so.1()(64bit)": {"gv-devil"},
        },
        binary_requires={
            "gv-devel": {"gv-devil"},
            "gv-devil": {"libIL.so.1()(64bit)"},
        },
        binary_source={"gv-devil": "devil-src", "gv-devel": "gv"},
        source_names={"gv-devel"},
    )

    devil_specs = {
        "devil-src": "Name: devil-src\nVersion: 1\nRelease: 1\nProvides: libIL.so.1()(64bit)\n",
    }

    async def repo_expand(specs, parsed, repo_index, roots, **kwargs):
        if "devil-src" not in parsed:
            tarball = _tarball(kwargs["releasename"], devil_specs)
            all_names, members = _tarball_spec_member_index(tarball, kwargs["releasename"])
            extra_specs, extra_parsed, err = _expand_chain_spec_closure(
                ["devil-src"],
                all_names,
                tarball,
                kwargs["releasename"],
                members=members,
            )
            merged_specs = {**specs, **extra_specs}
            merged_parsed = {**parsed, **extra_parsed}
            return merged_specs, merged_parsed, err
        return specs, parsed, []

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_fake_sparse,
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=_noop_br_expand,
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
            root_packages=["im"],
            architectures=["x86_64"],
            headers={},
        )

    built = {slot.name for layer in plan.layers for slot in layer}
    assert "devil-src" in built
