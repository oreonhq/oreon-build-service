from __future__ import annotations

from oreon_build.models import BuildStatus
from oreon_build.services.layer_engine import (
    _bootstrap_drops_for_graph,
    _canonical_layer_name,
    _incomplete_layer_sources,
    _layer_sources_for_requirement,
    _requirement_matches_sources,
    _strongly_connected_components,
    parse_missing_provides_from_text,
)


def test_generic_cycle_produces_bootstrap_drops():
    graph = {"alpha": {"beta"}, "beta": {"alpha"}, "gamma": {"alpha"}}
    assert _bootstrap_drops_for_graph(graph) == {
        "alpha": {"beta"},
        "beta": {"alpha"},
    }


def test_self_cycle_is_bootstrapped():
    assert _bootstrap_drops_for_graph({"tool": {"tool"}}) == {"tool": {"tool"}}


def test_non_cycle_edge_is_not_dropped():
    assert _bootstrap_drops_for_graph({"consumer": {"provider"}, "provider": set()}) == {}


def test_cargo_specs_skip_layer_bootstrap_drops():
    from oreon_build.services.layer_engine import _spec_skips_layer_bootstrap

    assert _spec_skips_layer_bootstrap("%cargo_generate_buildrequires -f debug\n")
    assert _spec_skips_layer_bootstrap("%cargo_prep\n")
    assert not _spec_skips_layer_bootstrap("BuildRequires: gcc\n")


def test_scc_handles_large_graph_without_recursion():
    size = 3000
    graph = {f"pkg-{index}": {f"pkg-{(index + 1) % size}"} for index in range(size)}
    components = _strongly_connected_components(graph)
    assert components == [set(graph)]


def test_requirement_provider_matching_handles_rich_capability():
    provider_map = {"pkgconfig(libfoo)": {"libfoo"}}
    requirement = "(pkgconfig(libfoo) >= 1 with pkgconfig(libfoo) < 2)"
    assert _layer_sources_for_requirement(requirement, provider_map, {"consumer", "libfoo"}) == {"libfoo"}
    assert _requirement_matches_sources(requirement, {"libfoo"}, provider_map, {"consumer", "libfoo"})


def test_requirement_provider_matching_handles_virtual_capability():
    provider_map = {"cmake(FooConfig)": {"foo"}}
    assert _layer_sources_for_requirement("cmake(FooConfig)", provider_map, {"foo", "bar"}) == {"foo"}


def test_requirement_provider_matching_does_not_match_unrelated_source():
    assert not _requirement_matches_sources("pkgconfig(libfoo)", {"bar"}, {"pkgconfig(libfoo)": {"foo"}}, {"bar", "foo"})


def test_name_normalization_is_stable():
    assert _canonical_layer_name("  Foo.spec ") == "foo"


def test_missing_provides_extracts_rich_tokens():
    text = "nothing provides requested (pkgconfig(foo) >= 1 with pkgconfig(foo) < 2)"
    values = parse_missing_provides_from_text(text)
    assert "pkgconfig(foo)" in values
    assert "with" not in values


def test_missing_provides_ignores_unrelated_dnf_messages():
    assert parse_missing_provides_from_text("Transaction failed: disk full") == set()


def test_build_status_values_are_terminally_distinct():
    assert BuildStatus.BLOCKED != BuildStatus.QUEUED
    assert BuildStatus.CANCELED == BuildStatus.CANCELLED
    assert BuildStatus.CANCELED.value == "cancelled"


def test_manifest_requirements_honor_with_check():
    from types import SimpleNamespace

    from oreon_build.services.layer_engine import _manifest_build_requirements

    spec = """
Name: demo
Version: 1
Release: 1
Summary: x
License: MIT
%bcond check 1
BuildRequires: gcc
%if %{with check}
BuildRequires: python3dist(tox-current-env)
%endif
%description
x
%prep
%build
%install
%files
"""
    parsed = SimpleNamespace(rpm_build_requires=None, build_requires=["gcc"], build_requires_raw=["gcc"])
    full = _manifest_build_requirements(spec, parsed, architecture="x86_64")
    boot = _manifest_build_requirements(
        spec, parsed, architecture="x86_64", defines={"with_check": "0"}
    )
    assert any("gcc" in item for item in full)
    assert any("tox-current-env" in item for item in full)
    assert any("gcc" in item for item in boot)
    assert not any("tox-current-env" in item for item in boot)


def test_bootstrap_keeps_generated_crate_buildrequires():
    from types import SimpleNamespace

    from oreon_build.services.layer_engine import _manifest_build_requirements

    spec = """
Name: rust-rand0.8
Version: 0.8.5
Release: 1
Summary: x
License: MIT
BuildRequires: rust
BuildRequires: cargo-rpm-macros
%description
x
"""
    parsed = SimpleNamespace(
        rpm_build_requires=["rust", "cargo-rpm-macros", "crate(rand_chacha) >= 0.3 with crate(rand_chacha) < 0.4~"],
        build_requires=["rust", "cargo-rpm-macros"],
        build_requires_raw=["rust", "cargo-rpm-macros"],
    )
    boot = _manifest_build_requirements(
        spec, parsed, architecture="x86_64", defines={"with_check": "0"}
    )
    assert any("crate(rand_chacha)" in item for item in boot)


def test_python3_binary_maps_to_python_source():
    sources = _layer_sources_for_requirement(
        "python3-matplotlib",
        {},
        {"blosc", "python-matplotlib"},
    )
    assert "python-matplotlib" in sources


def test_ghc_subpackage_devel_maps_to_ghc():
    sources = _layer_sources_for_requirement(
        "ghc-extra-devel",
        {},
        {"ghc", "shake"},
    )
    assert "ghc" in sources


def test_ghc_library_devel_maps_to_library_source_not_compiler_alts():
    sources = _layer_sources_for_requirement(
        "ghc-extra-devel",
        {},
        {"ghc", "ghc-extra", "ghc9.8", "shake"},
    )
    assert "ghc-extra" in sources
    assert "ghc9.8" not in sources


def test_ghc_core_devel_collapses_compiler_alts_to_ghc():
    sources = _layer_sources_for_requirement(
        "ghc-base-devel",
        {"ghc-base-devel": {"ghc", "ghc9.8", "ghc9.10"}},
        {"ghc", "ghc9.8", "ghc9.10", "shake"},
    )
    assert sources == {"ghc"}


def test_crate_maps_to_compat_rust_source():
    sources = _layer_sources_for_requirement(
        "crate(rand_chacha)",
        {},
        {"rust-rand0.8", "rust-rand_chacha0.3"},
    )
    assert "rust-rand_chacha0.3" in sources
    assert "rust-rand0.8" not in sources


def test_crate_does_not_attach_longer_crate_name():
    sources = _layer_sources_for_requirement(
        "crate(toml)",
        {},
        {"rust-toml", "rust-toml_edit", "rust-toml_edit0.19"},
    )
    assert "rust-toml" in sources
    assert "rust-toml_edit" not in sources
    assert "rust-toml_edit0.19" not in sources


def test_cmake_kf6_module_maps_to_kf6_source():
    from oreon_build.services.virtual_resolve import distgit_sources_for_virtual_cap

    known = {"kf6-kcoreaddons", "kf6-kio", "qt6-qtwebengine"}
    assert "kf6-kcoreaddons" in distgit_sources_for_virtual_cap(
        "cmake(KF6CoreAddons) >= 6.28.0", package_names=known, provider_map={}
    )
    assert "kf6-kio" in distgit_sources_for_virtual_cap(
        "cmake(KF6KIO)", package_names=known, provider_map={}
    )
    assert "qt6-qtwebengine" in distgit_sources_for_virtual_cap(
        "cmake(Qt6WebEngine)", package_names=known, provider_map={}
    )


def test_cmake_ecm_maps_to_extra_cmake_modules():
    names = {"extra-cmake-modules", "kf6-karchive"}
    for cap in ("cmake(ECM)", "cmake(ECM) >= 6.28.0", "extra-cmake-modules >= %{version}"):
        sources = _layer_sources_for_requirement(cap, {}, names)
        assert "extra-cmake-modules" in sources
        assert "kf6-karchive" not in sources
    statuses = {("extra-cmake-modules", "x86_64"): {BuildStatus.BLOCKED}}
    assert "extra-cmake-modules" in _incomplete_layer_sources(
        ("cmake(ECM)",), {}, names, statuses, "x86_64"
    )


def test_unexpanded_version_br_keeps_package_name():
    from oreon_build.core.rpm_requires import requirement_usable_for_solver

    assert requirement_usable_for_solver("extra-cmake-modules >= %{version}") == "extra-cmake-modules"
    assert requirement_usable_for_solver("cmake(ECM) >= %{version}") == "cmake(ECM)"
    assert requirement_usable_for_solver("%{foo}") is None
    assert requirement_usable_for_solver("zlib >= 1.2") == "zlib >= 1.2"


def test_version_macro_expand_keeps_evr():
    from oreon_build.core.rpm_requires import expand_version_macros, requirement_usable_for_solver

    expanded = expand_version_macros("extra-cmake-modules >= %{version}", "6.28.0")
    assert expanded == "extra-cmake-modules >= 6.28.0"
    assert requirement_usable_for_solver(expanded) == "extra-cmake-modules >= 6.28.0"
    assert expand_version_macros("cmake(ECM) >= %{version}", "6.28.0") == "cmake(ECM) >= 6.28.0"


def test_python3dist_maps_to_python_source():
    sources = _layer_sources_for_requirement(
        "python3dist(matplotlib)",
        {},
        {"blosc", "python-matplotlib"},
    )
    assert "python-matplotlib" in sources


def test_incomplete_sources_drop_finished_providers():
    names = {"kde", "blosc"}
    provider_map = {"blosc": {"blosc"}}
    statuses = {("blosc", "x86_64"): {BuildStatus.SUCCESS}}
    assert _incomplete_layer_sources(("blosc",), provider_map, names, statuses, "x86_64") == {"blosc"}
    assert _incomplete_layer_sources(
        ("blosc",), provider_map, names, statuses, "x86_64", {("blosc", "x86_64"): True}
    ) == set()
    statuses = {("blosc", "x86_64"): {BuildStatus.FAILED}}
    assert _incomplete_layer_sources(("blosc",), provider_map, names, statuses, "x86_64") == {"blosc"}


def test_bootstrap_drops_hide_cycle_caps():
    from oreon_build.services.layer_engine import _missing_caps_after_bootstrap_drops

    names = {"rust-foo", "rust-bar"}
    provider_map = {"crate(foo)": {"rust-foo"}, "crate(bar)": {"rust-bar"}}
    kept = _missing_caps_after_bootstrap_drops(
        ["crate(bar)", "pkgconfig(zlib)"],
        {"rust-bar"},
        provider_map,
        names,
    )
    assert kept == ["pkgconfig(zlib)"]


def test_missing_provides_extracts_needed_by():
    text = "Error: nothing provides crate(foo) >= 1.2.3 needed by rust-bar.src"
    values = parse_missing_provides_from_text(text)
    assert "crate(foo)" in values


def test_caps_provided_by_dropped_sources():
    from oreon_build.services.layer_engine import _caps_provided_by_sources

    caps = _caps_provided_by_sources(
        {"rust-foo"},
        {"crate(foo)": {"rust-foo"}, "crate(bar)": {"rust-bar"}},
    )
    assert "crate(foo)" in caps
    assert "crate(bar)" not in caps


def test_incomplete_sources_wait_for_repo_publish():
    names = {"kde", "blosc"}
    provider_map = {"blosc": {"blosc"}}
    statuses = {("blosc", "x86_64"): {BuildStatus.SUCCESS}}
    ready = {("blosc", "x86_64"): False}
    assert _incomplete_layer_sources(
        ("blosc",), provider_map, names, statuses, "x86_64", ready
    ) == {"blosc"}
    ready = {("blosc", "x86_64"): True}
    assert _incomplete_layer_sources(
        ("blosc",), provider_map, names, statuses, "x86_64", ready
    ) == set()


def test_spec_prepare_waits_for_unpublished_success():
    from oreon_build.services.layer_engine import _layer_job_needs_spec_prepare

    done = {("cmake", "x86_64"): {BuildStatus.SUCCESS}}
    ready = {("cmake", "x86_64"): False}
    assert _layer_job_needs_spec_prepare(["cmake"], "x86_64", done, ready) is False
    ready = {("cmake", "x86_64"): True}
    assert _layer_job_needs_spec_prepare(["cmake"], "x86_64", done, ready) is True


def test_spec_prepare_skipped_while_in_layer_blockers_open():
    from oreon_build.services.layer_engine import _layer_job_needs_spec_prepare

    statuses = {("cmake", "x86_64"): {BuildStatus.BLOCKED}}
    assert _layer_job_needs_spec_prepare(["cmake"], "x86_64", statuses) is False


def test_spec_prepare_runs_when_blockers_are_done_or_outside_layer():
    from oreon_build.services.layer_engine import _layer_job_needs_spec_prepare

    done = {("cmake", "x86_64"): {BuildStatus.SUCCESS}}
    assert _layer_job_needs_spec_prepare(["cmake"], "x86_64", done, {("cmake", "x86_64"): True}) is True
    assert _layer_job_needs_spec_prepare(["outside"], "x86_64", done) is True
    assert _layer_job_needs_spec_prepare([], "x86_64", done) is True


def test_open_layer_blockers_are_spec_mapped_only():
    from oreon_build.services.layer_engine import _open_layer_blockers

    statuses = {
        ("glibc", "x86_64"): {BuildStatus.BLOCKED},
        ("extra-cmake-modules", "x86_64"): {BuildStatus.BLOCKED},
    }
    out = _open_layer_blockers(
        {"extra-cmake-modules"}, statuses, "x86_64", "kwin"
    )
    assert out == {"extra-cmake-modules"}
    out = _open_layer_blockers(set(), statuses, "x86_64", "kwin")
    assert out == set()


def test_unpublished_layer_success_still_blocks():
    from oreon_build.services.layer_engine import _open_layer_blockers

    statuses = {("libfoo", "x86_64"): {BuildStatus.SUCCESS}}
    ready = {("libfoo", "x86_64"): False}
    out = _open_layer_blockers({"libfoo"}, statuses, "x86_64", "consumer", ready)
    assert out == {"libfoo"}
    ready = {("libfoo", "x86_64"): True}
    out = _open_layer_blockers({"libfoo"}, statuses, "x86_64", "consumer", ready)
    assert out == set()


def test_unbuilt_layer_job_blocks_even_if_repo_has_a_hole():
    from oreon_build.services.layer_engine import _open_layer_blockers

    statuses = {("libfoo", "x86_64"): {BuildStatus.BLOCKED}}
    out = _open_layer_blockers({"libfoo"}, statuses, "x86_64", "consumer")
    assert out == {"libfoo"}


def test_sat_job_requirements_drop_chroot_tools_keep_real_brs():
    from oreon_build.services.layer_engine import _sat_job_requirements

    out = _sat_job_requirements(
        (
            "gcc",
            "gcc >= 15",
            "make",
            "cmake(KF6Config) >= 6.28.0",
            "python3-devel",
            "rpmlib(CompressedFileNames) <= 3.0.4-1",
        )
    )
    assert "gcc" not in out
    assert "make" not in out
    assert "gcc >= 15" in out
    assert "cmake(KF6Config) >= 6.28.0" in out
    assert "python3-devel" in out


def test_missing_caps_only_block_on_in_layer_sources():
    from oreon_build.services.layer_engine import _layer_blockers_for_caps

    names = {"kf6-baloo", "qt6-qtwebengine"}
    statuses = {
        ("kf6-baloo", "x86_64"): {BuildStatus.BLOCKED},
        ("qt6-qtwebengine", "x86_64"): {BuildStatus.BLOCKED},
    }
    out = _layer_blockers_for_caps(
        {"gcc-toolset-13", "cmake(KF6Config) >= 6.28.0"},
        {},
        names,
        statuses,
        "x86_64",
        "plasma-desktop",
    )
    assert out == set()
    out = _layer_blockers_for_caps(
        {"qt6-qtwebengine"},
        {},
        names,
        statuses,
        "x86_64",
        "kio-extras",
    )
    assert out == {"qt6-qtwebengine"}


def test_heal_sources_pull_real_distgit_not_chroot_tools():
    from oreon_build.services.layer_engine import _heal_sources_for_requirement

    distgit = {"kf6-kconfig", "kf6-baloo", "gcc", "extra-cmake-modules", "python-tox"}
    out = _heal_sources_for_requirement(
        "cmake(KF6Config) >= 6.28.0",
        {},
        distgit,
        "kf6-baloo",
    )
    assert out == {"kf6-kconfig"}
    assert _heal_sources_for_requirement("gcc >= 15", {}, distgit, "plasma-desktop") == set()
    tox = _heal_sources_for_requirement(
        "python3dist(tox)",
        {},
        distgit,
        "python-cssselect",
    )
    assert "python-tox" in tox


def test_heal_crate_feature_maps_to_rust_source():
    from oreon_build.services.layer_engine import _heal_sources_for_requirement

    distgit = {"rust-signal-hook", "rust-signal-hook-registry"}
    out = _heal_sources_for_requirement(
        "crate(signal-hook-registry/default)",
        {},
        distgit,
        "rust-signal-hook",
    )
    assert "rust-signal-hook-registry" in out


def test_sat_extra_branches_follow_mock_urls_not_all_channels():
    from oreon_build.services.layer_engine import _sat_extra_branches

    urls = ["https://repo-us.oreonhq.com/test1/stable/x86_64/"]
    assert _sat_extra_branches("side-plasma", "test1", urls) == ["stable"]
    assert sorted(_sat_extra_branches("dev", "test1", None)) == ["stable", "testing"]


def test_gcc_br_does_not_map_outside_layer():
    sources = _layer_sources_for_requirement(
        "gcc >= 15",
        {},
        {"plasma-desktop", "kwin", "extra-cmake-modules"},
    )
    assert "gcc" not in sources
    assert "glibc" not in sources
    sources = _layer_sources_for_requirement("gcc >= 15", {}, {"gcc", "plasma-desktop"})
    assert "gcc" in sources
