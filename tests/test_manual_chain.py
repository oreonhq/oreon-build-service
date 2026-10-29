# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import pytest

from oreon_build.services.rpmbuild_order import bootstrap_names_by_layer, parse_manual_chain


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
