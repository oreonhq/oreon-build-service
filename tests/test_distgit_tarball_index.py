# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import tarfile

from oreon_build.services.distgit_order import (
    _expand_chain_spec_closure,
    _tarball_spec_member_index,
)


def _make_mini_tarball(releasename: str, specs: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for pkg, body in specs.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=f"repo-{releasename}/{releasename}/{pkg}/{pkg}.spec")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_chain_closure_uses_single_tar_pass_for_reads() -> None:
    releasename = "oreon-11-rp1"
    specs = {
        "a": "Name: a\nVersion: 1\nRelease: 1%{?dist}\nBuildRequires: b\nProvides: a\n",
        "b": "Name: b\nVersion: 1\nRelease: 1%{?dist}\nProvides: b\n",
        "c": "Name: c\nVersion: 1\nRelease: 1%{?dist}\nProvides: c\n",
    }
    tarball = _make_mini_tarball(releasename, specs)
    all_names, members = _tarball_spec_member_index(tarball, releasename)
    assert all_names == {"a", "b", "c"}
    got, parsed, err = _expand_chain_spec_closure(
        ["a"], all_names, tarball, releasename, members=members
    )
    assert not err
    assert set(got.keys()) == {"a", "b"}
    assert set(parsed.keys()) == {"a", "b"}
