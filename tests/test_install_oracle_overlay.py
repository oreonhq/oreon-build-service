# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace

from oreon_build.services.dep_plan.install_oracle import overlay_from_nodes


def _node(**kw) -> SimpleNamespace:
    base = dict(action="build", provides=set(), version="", release="", epoch=0, binaries=())
    base.update(kw)
    return SimpleNamespace(**base)


def test_overlay_picks_up_caps_from_provider_map() -> None:
    nodes = {
        "foo": _node(provides={"foo"}),
        "bar": _node(action="pending", provides={"bar"}),
        "blocked-pkg": _node(action="block", provides={"blocked-pkg"}),
    }
    provider_map = {
        "pkgconfig(foo)": {"foo"},
        "perl(Bar::Baz)": {"bar"},
        "pkgconfig(blocked-pkg)": {"blocked-pkg"},
    }
    overlay = overlay_from_nodes(nodes, provider_map)
    assert "foo" in overlay
    assert "pkgconfig(foo)" in overlay["foo"].provides
    assert "bar" in overlay
    assert "perl(Bar::Baz)" in overlay["bar"].provides
    assert "blocked-pkg" not in overlay


def test_overlay_reflects_provider_map_mutation_between_calls() -> None:
    nodes = {"foo": _node(provides={"foo"})}
    provider_map: dict = {"pkgconfig(foo)": {"foo"}}
    overlay1 = overlay_from_nodes(nodes, provider_map)
    assert "pkgconfig(foo)" in overlay1["foo"].provides

    provider_map["cmake(foo-1.0)"] = {"foo"}
    overlay2 = overlay_from_nodes(nodes, provider_map)
    assert "cmake(foo-1.0)" in overlay2["foo"].provides
    assert "pkgconfig(foo)" in overlay2["foo"].provides
