from __future__ import annotations

import pytest

from oreon_build.services.rpmbuild_order import parse_manual_chain


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


def test_parse_manual_chain_rejects_duplicate_package() -> None:
    with pytest.raises(ValueError, match="more than once"):
        parse_manual_chain("pkg-a : pkg-a")
