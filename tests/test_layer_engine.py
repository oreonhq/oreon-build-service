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
