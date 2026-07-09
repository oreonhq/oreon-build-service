from __future__ import annotations

import io
import tarfile
from unittest.mock import AsyncMock, patch

import pytest

from oreon_build.services.distgit_order import compute_distgit_build_order_for_chain


def _make_mini_tarball(releasename: str, specs: dict[str, str]) -> bytes:
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
    "d": "Name: d\nVersion: 1\nRelease: 1\nProvides: d\n",
}


@pytest.mark.asyncio
async def test_ci_chain_does_not_expand_closure() -> None:
    """Only the changed packages should be built — not their full transitive deps."""
    releasename = "oreon-11-rp1"
    tarball = _make_mini_tarball(releasename, _SPECS)

    with patch(
        "oreon_build.services.distgit_order._download_distgit_tarball",
        new_callable=AsyncMock,
        return_value=(tarball, None),
    ):
        order = await compute_distgit_build_order_for_chain(
            releasename=releasename,
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["a"],
        )

    assert set(order.package_names) == {"a"}


@pytest.mark.asyncio
async def test_ci_chain_orders_changed_packages_by_dependency() -> None:
    """Changed packages should be ordered: deps that also changed first, then dependents."""
    releasename = "oreon-11-rp1"
    tarball = _make_mini_tarball(releasename, _SPECS)

    with patch(
        "oreon_build.services.distgit_order._download_distgit_tarball",
        new_callable=AsyncMock,
        return_value=(tarball, None),
    ):
        order = await compute_distgit_build_order_for_chain(
            releasename=releasename,
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["a", "b"],
        )

    assert set(order.package_names) == {"a", "b"}
    flat = [pkg for layer in order.layers for pkg in layer]
    assert set(flat) == {"a", "b"}
    assert flat.index("b") < flat.index("a")


@pytest.mark.asyncio
async def test_ci_chain_independent_packages_same_layer() -> None:
    """Two changed packages with no dependency between them land in the same layer."""
    releasename = "oreon-11-rp1"
    tarball = _make_mini_tarball(releasename, _SPECS)

    with patch(
        "oreon_build.services.distgit_order._download_distgit_tarball",
        new_callable=AsyncMock,
        return_value=(tarball, None),
    ):
        order = await compute_distgit_build_order_for_chain(
            releasename=releasename,
            distgit_url="https://github.com/oreonhq/rpm-specfiles.git",
            distgit_branch="main",
            root_packages=["c", "d"],
        )

    assert set(order.package_names) == {"c", "d"}
    assert len(order.layers) == 1
    assert set(order.layers[0]) == {"c", "d"}
