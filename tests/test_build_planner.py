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


async def _noop_repo_expand(specs, parsed, repo, roots, **kwargs):
    return specs, parsed, []


async def _noop_br_expand(specs, parsed, repo, **kwargs):
    return specs, parsed, [], set()


async def _fake_sparse_closure(roots, *, releasename, owner, repo, distgit_branch, headers, **kwargs):
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
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=_noop_br_expand,
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
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
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
            root_packages=["a"],
            architectures=["x86_64"],
            headers={},
        )

    built = {slot.name for layer in plan.layers for slot in layer}
    assert built == {"a"}
    assert "b" not in built
    assert "c" not in built


@pytest.mark.asyncio
async def test_planner_blocks_when_default_bcond_br_missing_from_repo() -> None:
    clear_repo_capability_cache()
    specs = {
        "foo": """
Name: foo
Version: 1
Release: 1
%bcond_without check
%if %{with check}
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
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
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
            root_packages=["foo"],
            architectures=["x86_64"],
            headers={},
        )

    assert "missing-lib" in " ".join(plan.blocking_errors)
    assert "foo" not in {slot.name for layer in plan.layers for slot in layer if slot.action == "build"}


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
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=_noop_br_expand,
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


def test_planner_skips_repo_source_with_maven_brs() -> None:
    from oreon_build.services.build_planner import _plan_slots_for_package

    repo = RepoCapabilityIndex(
        source_names={"hamcrest"},
        binary_names={"hamcrest"},
        provides={"hamcrest", "mvn(org.hamcrest:hamcrest-core)", "mvn(junit:junit)"},
    )
    repo.finalize_lookups()
    spec = "Name: hamcrest\nVersion: 1\nRelease: 1\nBuildRequires: junit:junit\nProvides: hamcrest\n"
    slot = _plan_slots_for_package("hamcrest", spec, repo, force_build=False)
    assert slot.action == "skip"


def test_chain_spec_root_packages_dedupes() -> None:
    from oreon_build.services.build_planner import chain_spec_root_packages

    assert chain_spec_root_packages("a b : c a") == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_planner_blocks_unbuildable_transitive_dep() -> None:
    clear_repo_capability_cache()
    specs = {
        "im": "Name: im\nVersion: 1\nRelease: 1\nBuildRequires: pkgconfig(libcgraph), libIL.so.1()(64bit)\nProvides: im\n",
        "gv": "Name: gv\nVersion: 1\nRelease: 1\nProvides: pkgconfig(libcgraph)\n",
        "devil": "Name: devil\nVersion: 1\nRelease: 1\nBuildRequires: allegro-devel\nProvides: libIL.so.1()(64bit)\n",
    }
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
        binary_source={"gv-devil": "devil", "gv-devel": "gv"},
        source_names={"gv-devel"},
    )

    async def repo_expand(specs_in, parsed, repo_index, roots, **kwargs):
        if "devil" not in parsed:
            tarball = _tarball(kwargs["releasename"], {"devil": specs["devil"]})
            all_names, members = _tarball_spec_member_index(tarball, kwargs["releasename"])
            extra_specs, extra_parsed, err = _expand_chain_spec_closure(
                ["devil"],
                all_names,
                tarball,
                kwargs["releasename"],
                members=members,
            )
            return {**specs_in, **extra_specs}, {**parsed, **extra_parsed}, err
        return specs_in, parsed, []

    async def br_expand_missing_allegro(specs_in, parsed, repo_index, **kwargs):
        rel = kwargs.get("releasename", "oreon-11-rp1")
        return specs_in, parsed, [f"allegro: not in distgit at {rel}/allegro/ (needed by devil)"], {"allegro"}

    async def allegro_provides_index(**kwargs):
        from oreon_build.services.distgit_order import DistgitProvidesIndex

        return DistgitProvidesIndex(
            package_names=frozenset(["allegro"]),
            capability_providers={
                "allegro-devel": frozenset(["allegro"]),
                "allegro": frozenset(["allegro"]),
            },
        )

    with (
        patch(
            "oreon_build.services.distgit_order.get_distgit_provides_index_cached",
            side_effect=allegro_provides_index,
        ),
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=br_expand_missing_allegro,
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
    assert "devil" not in built
    assert plan.blocking_errors
    assert any("allegro" in e for e in plan.blocking_errors)
    assert not any("qt" in e for e in plan.blocking_errors)


@pytest.mark.asyncio
async def test_planner_blocks_when_runtime_requires_of_builddep_missing() -> None:
    clear_repo_capability_cache()
    specs = {
        "DevIL": "Name: DevIL\nVersion: 1\nRelease: 1\nBuildRequires: allegro-devel\nProvides: DevIL\n",
        "allegro": "Name: allegro\nVersion: 1\nRelease: 1\nRequires: timidity++-patches\nProvides: allegro allegro-devel\n",
    }
    repo = RepoCapabilityIndex(
        binary_names={"allegro", "allegro-devel"},
        provides={"allegro", "allegro-devel"},
        capability_providers={
            "allegro": {"allegro"},
            "allegro-devel": {"allegro-devel", "allegro"},
        },
        binary_requires={
            "allegro": {"timidity++-patches"},
            "allegro-devel": {"allegro(x86-64)", "liballeg.so.4.4()(64bit)"},
        },
        binary_source={"allegro": "allegro", "allegro-devel": "allegro"},
        source_names={"allegro"},
    )
    repo.finalize_lookups()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
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
            root_packages=["DevIL"],
            architectures=["x86_64"],
            headers={},
        )

    built = {slot.name for layer in plan.layers for slot in layer if slot.action == "build"}
    assert "DevIL" not in built
    assert plan.blocking_errors
    assert any("timidity++-patches" in e for e in plan.blocking_errors)
    assert not any("libxcrypt-devel" in e for e in plan.blocking_errors)
    assert not any("GLIBC" in e for e in plan.blocking_errors)


@pytest.mark.asyncio
async def test_planner_does_not_block_on_repo_satisfied_gcc() -> None:
    clear_repo_capability_cache()
    specs = {
        "DevIL": "Name: DevIL\nVersion: 1\nRelease: 1\nBuildRequires: gcc, allegro-devel\nProvides: DevIL\n",
        "gcc": "Name: gcc\nVersion: 1\nRelease: 1\nBuildRequires: libgomp-offload-nvptx\nProvides: gcc\n",
    }
    repo = RepoCapabilityIndex(
        binary_names={"gcc", "allegro", "allegro-devel"},
        provides={"gcc", "allegro-devel", "allegro"},
        source_names={"gcc", "allegro"},
        capability_providers={
            "gcc": {"gcc"},
            "allegro-devel": {"allegro"},
            "allegro": {"allegro"},
        },
        binary_requires={
            "allegro": {"timidity++-patches"},
            "allegro-devel": {"allegro(x86-64)"},
        },
        binary_source={"gcc": "gcc", "allegro": "allegro", "allegro-devel": "allegro"},
    )
    repo.finalize_lookups()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
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
            root_packages=["DevIL"],
            architectures=["x86_64"],
            headers={},
        )

    assert not any("gcc:" in e for e in plan.blocking_errors)
    assert any("timidity++-patches" in e for e in plan.blocking_errors)


@pytest.mark.asyncio
async def test_planner_queues_unbuilt_distgit_dep_on_path() -> None:
    clear_repo_capability_cache()
    specs = {
        "im": "Name: im\nVersion: 1\nRelease: 1\nBuildRequires: libIL-devel\nProvides: im\n",
        "gv": "Name: gv\nVersion: 1\nRelease: 1\nProvides: pkgconfig(libcgraph)\n",
        "devil": "Name: devil\nVersion: 1\nRelease: 1\nBuildRequires: allegro-devel\nProvides: libIL-devel libIL.so.1()(64bit)\n",
        "allegro": "Name: allegro\nVersion: 1\nRelease: 1\nProvides: allegro-devel\n",
    }
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
        binary_source={"gv-devil": "devil", "gv-devel": "gv"},
        source_names={"gv-devel"},
    )

    async def br_expand(specs_in, parsed, repo_index, **kwargs):
        out_specs = dict(specs_in)
        out_parsed = dict(parsed)
        err: list[str] = []
        if "devil" not in parsed:
            tarball = _tarball(kwargs["releasename"], {"devil": specs["devil"]})
            all_names, members = _tarball_spec_member_index(tarball, kwargs["releasename"])
            extra_specs, extra_parsed, e2 = _expand_chain_spec_closure(
                ["devil"],
                all_names,
                tarball,
                kwargs["releasename"],
                members=members,
            )
            out_specs.update(extra_specs)
            out_parsed.update(extra_parsed)
            err.extend(e2)
        if "allegro" not in parsed:
            tarball = _tarball(kwargs["releasename"], {"allegro": specs["allegro"]})
            all_names, members = _tarball_spec_member_index(tarball, kwargs["releasename"])
            extra_specs, extra_parsed, e2 = _expand_chain_spec_closure(
                ["allegro"],
                all_names,
                tarball,
                kwargs["releasename"],
                members=members,
            )
            out_specs.update(extra_specs)
            out_parsed.update(extra_parsed)
            err.extend(e2)
        return out_specs, out_parsed, err, set()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=br_expand,
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

    built = [slot.name for layer in plan.layers for slot in layer]
    assert not plan.blocking_errors
    assert "allegro" in built
    assert "devil" in built
    assert "im" in built
    assert built.index("allegro") < built.index("devil")


@pytest.mark.asyncio
async def test_planner_pulls_unsatisfied_build_dep_source() -> None:
    clear_repo_capability_cache()
    specs = {
        "app": "Name: app\nVersion: 1\nRelease: 1\nBuildRequires: libfoo-devel\nProvides: app\n",
        "libfoo": "Name: libfoo\nVersion: 1\nRelease: 1\nProvides: libfoo-devel\n",
    }
    empty_repo = RepoCapabilityIndex()

    async def br_expand(specs_in, parsed, repo_index, **kwargs):
        if "libfoo" not in parsed:
            tarball = _tarball(kwargs["releasename"], {"libfoo": specs["libfoo"]})
            all_names, members = _tarball_spec_member_index(tarball, kwargs["releasename"])
            extra_specs, extra_parsed, err = _expand_chain_spec_closure(
                ["libfoo"],
                all_names,
                tarball,
                kwargs["releasename"],
                members=members,
            )
            return {**specs_in, **extra_specs}, {**parsed, **extra_parsed}, err, set()
        return specs_in, parsed, [], set()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=br_expand,
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
    assert built == ["libfoo", "app"]


@pytest.mark.asyncio
async def test_planner_rebuilds_provider_for_versioned_br() -> None:
    clear_repo_capability_cache()
    specs = {
        "kwin": (
            "Name: kwin\nVersion: 6.7.2\nRelease: 1\n"
            "BuildRequires: wayland-protocols-devel >= 1.48\n"
            "Provides: kwin\n"
        ),
        "wayland-protocols": (
            "Name: wayland-protocols\nVersion: 1.48\nRelease: 1\n"
            "Provides: wayland-protocols-devel = 1.48\n"
            "Provides: wayland-protocols\n"
        ),
    }
    repo = RepoCapabilityIndex(
        source_names={"wayland-protocols"},
        binary_names={"wayland-protocols-devel"},
        provides={"wayland-protocols-devel"},
        capability_providers={"wayland-protocols-devel": {"wayland-protocols-devel"}},
        capability_evrs={"wayland-protocols-devel": [("0", "1.47", "1.or11")]},
        binary_source={"wayland-protocols-devel": "wayland-protocols"},
    )
    repo.finalize_lookups()

    async def br_expand(specs_in, parsed, _repo, **kwargs):
        from oreon_build.services.distgit_order import _parse_spec_text

        if "wayland-protocols" not in parsed and "wayland-protocols" in specs:
            parsed = dict(parsed)
            specs_out = dict(specs_in)
            specs_out["wayland-protocols"] = specs["wayland-protocols"]
            parsed["wayland-protocols"] = _parse_spec_text(
                specs["wayland-protocols"], "wayland-protocols.spec"
            )
            return specs_out, parsed, [], set()
        return specs_in, parsed, [], set()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=br_expand,
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
            root_packages=["kwin"],
            architectures=["x86_64"],
            headers={},
        )

    built = [slot.name for layer in plan.layers for slot in layer]
    assert "wayland-protocols" in built
    assert "kwin" in built
    assert built.index("wayland-protocols") < built.index("kwin")


@pytest.mark.asyncio
async def test_planner_orders_optional_perl_brs_before_test_simple() -> None:
    clear_repo_capability_cache()
    specs = {
        "perl-Test-Simple": (
            "Name: perl-Test-Simple\nVersion: 1\nRelease: 1\n"
            "%global perl_bootstrap 1\n"
            "%if %{defined perl_bootstrap}\n"
            "%bcond_without optional_test\n"
            "%else\n"
            "%bcond_with optional_test\n"
            "%endif\n"
            "%if %{with optional_test}\n"
            "BuildRequires: perl(JSON::MaybeXS)\n"
            "BuildRequires: perl(Test::Class)\n"
            "BuildRequires: perl(Test::Script)\n"
            "%endif\n"
            "Provides: perl(Test::Simple)\n"
        ),
        "perl-JSON-MaybeXS": (
            "Name: perl-JSON-MaybeXS\nVersion: 1\nRelease: 1\n"
            "Provides: perl(JSON::MaybeXS)\n"
            "Provides: perl-JSON-MaybeXS\n"
        ),
        "perl-Test-Class": (
            "Name: perl-Test-Class\nVersion: 1\nRelease: 1\n"
            "Provides: perl(Test::Class)\n"
            "Provides: perl-Test-Class\n"
        ),
        "perl-Test-Script": (
            "Name: perl-Test-Script\nVersion: 1\nRelease: 1\n"
            "Provides: perl(Test::Script)\n"
            "Provides: perl-Test-Script\n"
        ),
    }
    empty_repo = RepoCapabilityIndex()
    empty_repo.finalize_lookups()

    async def br_expand(specs_in, parsed, repo_index, **kwargs):
        from oreon_build.services.distgit_order import _parse_spec_text

        out_specs = dict(specs_in)
        out_parsed = dict(parsed)
        for name in ("perl-JSON-MaybeXS", "perl-Test-Class", "perl-Test-Script"):
            if name not in out_parsed and name in specs:
                out_specs[name] = specs[name]
                out_parsed[name] = _parse_spec_text(specs[name], f"{name}.spec")
        return out_specs, out_parsed, [], set()

    with (
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=br_expand,
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
            root_packages=["perl-Test-Simple"],
            architectures=["x86_64"],
            headers={},
        )

    built = [slot.name for layer in plan.layers for slot in layer]
    assert not plan.blocking_errors, plan.blocking_errors
    for dep in ("perl-JSON-MaybeXS", "perl-Test-Class", "perl-Test-Script"):
        assert dep in built
        assert built.index(dep) < built.index("perl-Test-Simple")


@pytest.mark.asyncio
async def test_planner_maps_path_br_to_nodejs_sources() -> None:
    clear_repo_capability_cache()
    specs = {
        "app": (
            "Name: app\nVersion: 1\nRelease: 1\n"
            "BuildRequires: /usr/bin/npm\n"
            "BuildRequires: /usr/bin/node\n"
            "Provides: app\n"
        ),
        "nodejs22": (
            "Name: nodejs22\nVersion: 1\nRelease: 1\n"
            "Provides: npm\n"
            "Provides: node\n"
            "%files\n"
            "%{_bindir}/npm\n"
            "%{_bindir}/node\n"
        ),
    }
    empty_repo = RepoCapabilityIndex()
    empty_repo.finalize_lookups()

    async def provides_idx(**kwargs):
        from oreon_build.services.distgit_order import DistgitProvidesIndex

        return DistgitProvidesIndex(
            package_names=frozenset(specs),
            capability_providers={
                "/usr/bin/npm": frozenset(["nodejs22"]),
                "/usr/bin/node": frozenset(["nodejs22"]),
                "npm": frozenset(["nodejs22"]),
                "node": frozenset(["nodejs22"]),
            },
        )

    async def br_expand(specs_in, parsed, repo_index, **kwargs):
        from oreon_build.services.distgit_order import _parse_spec_text

        if "nodejs22" not in parsed:
            out = dict(specs_in)
            outp = dict(parsed)
            out["nodejs22"] = specs["nodejs22"]
            outp["nodejs22"] = _parse_spec_text(specs["nodejs22"], "nodejs22.spec")
            return out, outp, [], set()
        return specs_in, parsed, [], set()

    with (
        patch(
            "oreon_build.services.distgit_order.get_distgit_provides_index_cached",
            side_effect=provides_idx,
        ),
        patch(
            "oreon_build.services.build_planner.get_distgit_provides_index_cached",
            side_effect=provides_idx,
        ),
        patch(
            "oreon_build.services.build_planner.expand_chain_spec_closure_sparse",
            side_effect=_make_sparse_closure(specs),
        ),
        patch(
            "oreon_build.services.build_planner.fetch_repo_transitive_extras_for_roots",
            side_effect=_noop_repo_expand,
        ),
        patch(
            "oreon_build.services.build_planner.expand_unsatisfied_build_dep_sources",
            side_effect=br_expand,
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
    assert not plan.blocking_errors, plan.blocking_errors
    assert "nodejs22" in built
    assert built.index("nodejs22") < built.index("app")
    assert not any("/usr/bin/" in e for e in plan.blocking_errors)
