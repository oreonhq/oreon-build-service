# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pytest

from oreon_build.services.rpmbuild_order import (
    bootstrap_names_by_layer,
    explicit_bootstrap_by_layer,
    layer_bootstrap_names,
    parse_manual_chain,
)


def test_parse_manual_chain_koji_format() -> None:
    assert parse_manual_chain("pkg-a pkg-b : pkg-c : pkg-d pkg-e") == [
        ["pkg-a", "pkg-b"],
        ["pkg-c"],
        ["pkg-d", "pkg-e"],
    ]


def test_parse_manual_chain_accepts_newlines_and_commas_within_group() -> None:
    assert parse_manual_chain("pkg-a, pkg-b\n: pkg-c") == [["pkg-a", "pkg-b"], ["pkg-c"]]


@pytest.mark.parametrize("chain", [": pkg", "pkg :", "pkg :: next", "pkg /bad"])
def test_parse_manual_chain_rejects_invalid_input(chain: str) -> None:
    with pytest.raises(ValueError):
        parse_manual_chain(chain)


def test_parse_manual_chain_allows_later_bootstrap_repeat() -> None:
    layers = parse_manual_chain("maven-surefire : maven-compiler : maven-surefire")
    assert layers == [["maven-surefire"], ["maven-compiler"], ["maven-surefire"]]
    assert bootstrap_names_by_layer(layers) == {0: {"maven-surefire"}}


def test_parse_manual_chain_rejects_duplicate_in_same_group() -> None:
    with pytest.raises(ValueError, match="same group"):
        parse_manual_chain("pkg-a pkg-a")


def test_parse_manual_chain_boot_suffix_then_full() -> None:
    chain = "aom : libheif#boot : librsvg : chafa"
    layers = parse_manual_chain(chain)
    assert layers == [["aom"], ["libheif"], ["libheif"], ["librsvg"], ["chafa"]]
    assert explicit_bootstrap_by_layer(chain) == {"1": ["libheif"]}
    assert bootstrap_names_by_layer(layers) == {1: {"libheif"}}
    assert layer_bootstrap_names(layers, 1, {"1": ["libheif"]}) == {"libheif"}
    assert layer_bootstrap_names(layers, 2, {"1": ["libheif"]}) == set()


def test_boot_suffix_in_a_shared_group_then_full() -> None:
    layers = parse_manual_chain("foo libheif#boot")
    assert layers == [["foo", "libheif"], ["libheif"]]
    assert bootstrap_names_by_layer(layers) == {0: {"libheif"}}


def test_boot_suffix_same_group_as_real_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="same group"):
        parse_manual_chain("libheif#boot libheif")


def test_boot_suffix_and_later_repeat_is_rejected() -> None:
    with pytest.raises(ValueError, match="already includes the full build"):
        parse_manual_chain("libheif#boot : librsvg : libheif")


@pytest.mark.parametrize("chain", ["#boot", "libheif#boots", "libheif#boot#boot"])
def test_parse_manual_chain_rejects_bad_boot_suffix(chain: str) -> None:
    with pytest.raises(ValueError):
        parse_manual_chain(chain)
