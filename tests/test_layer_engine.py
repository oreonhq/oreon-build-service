from __future__ import annotations

from oreon_build.models import BuildStatus
from oreon_build.services.layer_engine import (
    _bootstrap_drops_for_graph,
    _canonical_layer_name,
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
    assert parse_missing_provides_from_text("No matching package to install: python3-test") == set()


def test_build_status_values_are_terminally_distinct():
    assert BuildStatus.BLOCKED != BuildStatus.QUEUED


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


def test_python3dist_maps_to_python_source():
    sources = _layer_sources_for_requirement(
        "python3dist(matplotlib)",
        {},
        {"blosc", "python-matplotlib"},
    )
    assert "python-matplotlib" in sources
