# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from oreon_build.services.publisher import update_repodata_at_prefix


def test_repodata_update_purges_cloudflare_repodata_cache(tmp_path: Path) -> None:
    from oreon_build.services.publisher import _purge_repodata_cache

    request = MagicMock()
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"success": true}'

    with (
        patch.dict(
            "os.environ",
            {
                "CLOUDFLARE_ZONE_ID": "zone",
                "CLOUDFLARE_API_TOKEN": "token",
            },
            clear=False,
        ),
        patch("oreon_build.services.publisher.get_settings") as settings,
        patch("oreon_build.services.publisher.urllib.request.Request", return_value=request) as make_request,
        patch("oreon_build.services.publisher.urllib.request.urlopen", return_value=response),
    ):
        settings.return_value.cloudflare_zone_id = None
        settings.return_value.cloudflare_api_token = None
        settings.return_value.r2_public_url = "https://repo.example/"
        _purge_repodata_cache("oreon-11/dev/x86_64")

    make_request.assert_called()
    payloads = [call.kwargs["data"] for call in make_request.call_args_list]
    blob = b"".join(payloads)
    assert b"https://repo.example/oreon-11/dev/x86_64/repodata/repomd.xml" in blob
    assert b"repo.example/oreon-11/dev/x86_64/repodata/" in blob
    assert b"repodata/*" not in blob


def test_rpm_purge_batches_over_cloudflare_limit() -> None:
    from oreon_build.services.publisher import _purge_replaced_rpm_cache

    request = MagicMock()
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"success": true}'
    names = [f"pkg-{i}-1-1.x86_64.rpm" for i in range(31)]

    with (
        patch.dict(
            "os.environ",
            {"CLOUDFLARE_ZONE_ID": "zone", "CLOUDFLARE_API_TOKEN": "token"},
            clear=False,
        ),
        patch("oreon_build.services.publisher.get_settings") as settings,
        patch("oreon_build.services.publisher.urllib.request.Request", return_value=request) as make_request,
        patch("oreon_build.services.publisher.urllib.request.urlopen", return_value=response),
    ):
        settings.return_value.cloudflare_zone_id = None
        settings.return_value.cloudflare_api_token = None
        settings.return_value.r2_public_url = "https://repo.example/"
        _purge_replaced_rpm_cache("oreon-11/dev/x86_64", names)

    payloads = [json.loads(call.kwargs["data"]) for call in make_request.call_args_list]
    assert any("prefixes" in p for p in payloads)
    file_batches = [p["files"] for p in payloads if "files" in p]
    assert sum(len(b) for b in file_batches) == 31
    assert all(len(b) <= 30 for b in file_batches)
    assert not any("*" in u for b in file_batches for u in b)


def test_skip_createrepo_when_no_rpm_changes(tmp_path: Path) -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    container = tmp_path / "stage"
    repo_root = container / "repo"
    rpms_dir = repo_root / "RPMS"
    rpms_dir.mkdir(parents=True)
    (rpms_dir / "foo-1-1.x86_64.rpm").write_bytes(b"rpm")
    repodata = repo_root / "repodata"
    repodata.mkdir()
    (repodata / "repomd.xml").write_text("<?xml version='1.0'?><repomd/>")
    (repodata / "deadbeef-primary.xml.zst").write_bytes(b"x" * 4096)

    client = MagicMock()
    client.list_objects.return_value = [{"key": f"{prefix}/RPMS/foo-1-1.x86_64.rpm", "size": 3, "etag": "abc"}]

    with (
        patch("oreon_build.services.publisher._repo_temp_dir_parent", return_value=str(tmp_path)),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=container),
        patch("oreon_build.services.publisher._count_primary_packages", return_value=1),
        patch("oreon_build.services.publisher._sync_rpms_dir_from_r2", return_value=(1, 0)) as sync,
        patch("oreon_build.services.publisher._run_createrepo") as createrepo,
    ):
        ok = update_repodata_at_prefix(prefix, r2_client=client)

    assert ok is True
    sync.assert_called_once()
    createrepo.assert_not_called()


def test_staged_update_on_warm_mirror_runs_full_update_not_includepkg(tmp_path: Path) -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    container = tmp_path / "stage"
    repo_root = container / "repo"
    rpms_dir = repo_root / "RPMS"
    rpms_dir.mkdir(parents=True)
    keys = [{"key": f"{prefix}/RPMS/pkg-{i}.x86_64.rpm"} for i in range(120)]
    keys.append({"key": f"{prefix}/RPMS/new-2-1.x86_64.rpm"})
    for i in range(120):
        (rpms_dir / f"pkg-{i}.x86_64.rpm").write_bytes(b"old")
    (rpms_dir / "new-2-1.x86_64.rpm").write_bytes(b"new")
    repodata = repo_root / "repodata"
    repodata.mkdir()
    (repodata / "repomd.xml").write_text("<?xml version='1.0'?><repomd/>")
    primary = repodata / "deadbeef-primary.xml.zst"
    primary.write_bytes(b"x" * 4096)

    client = MagicMock()
    client.list_objects.return_value = keys
    client.get_object.return_value = b"new"

    with (
        patch("oreon_build.services.publisher._repo_temp_dir_parent", return_value=str(tmp_path)),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=container),
        patch("oreon_build.services.publisher._count_primary_packages", return_value=120),
        patch("oreon_build.services.publisher._sync_rpms_dir_from_r2") as sync,
        patch("oreon_build.services.publisher._run_createrepo") as createrepo,
        patch("oreon_build.services.publisher._validate_repodata_sane"),
        patch("oreon_build.services.publisher._upload_repodata_dir_ordered"),
        patch("oreon_build.services.publisher._cleanup_old_repodata"),
        patch("oreon_build.services.publisher.sign_repomd"),
    ):
        ok = update_repodata_at_prefix(
            prefix,
            r2_client=client,
            staged_binaries=["new-2-1.x86_64.rpm"],
        )

    assert ok is True
    sync.assert_not_called()
    createrepo.assert_called_once()
    assert createrepo.call_args.kwargs.get("force_full") is False


def test_warm_staged_publish_drops_local_rpms_pruned_from_r2(tmp_path: Path) -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    container = tmp_path / "stage"
    repo_root = container / "repo"
    rpms_dir = repo_root / "RPMS"
    rpms_dir.mkdir(parents=True)
    keys = [{"key": f"{prefix}/RPMS/pkg-{i}.x86_64.rpm"} for i in range(120)]
    keys.append({"key": f"{prefix}/RPMS/keep-2-1.x86_64.rpm"})
    for i in range(120):
        (rpms_dir / f"pkg-{i}.x86_64.rpm").write_bytes(b"old")
    (rpms_dir / "keep-2-1.x86_64.rpm").write_bytes(b"keep")
    # ghost nevra still on local stage after R2 prune
    (rpms_dir / "ghost-1-1.x86_64.rpm").write_bytes(b"ghost")
    repodata = repo_root / "repodata"
    repodata.mkdir()
    (repodata / "repomd.xml").write_text("<?xml version='1.0'?><repomd/>")
    (repodata / "deadbeef-primary.xml.zst").write_bytes(b"x" * 4096)

    client = MagicMock()
    client.list_objects.return_value = keys
    client.get_object.return_value = b"keep"

    with (
        patch("oreon_build.services.publisher._repo_temp_dir_parent", return_value=str(tmp_path)),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=container),
        patch("oreon_build.services.publisher._count_primary_packages", return_value=120),
        patch("oreon_build.services.publisher._sync_rpms_dir_from_r2") as sync,
        patch("oreon_build.services.publisher._run_createrepo") as createrepo,
        patch("oreon_build.services.publisher._validate_repodata_sane"),
        patch("oreon_build.services.publisher._upload_repodata_dir_ordered"),
        patch("oreon_build.services.publisher._cleanup_old_repodata"),
        patch("oreon_build.services.publisher.sign_repomd"),
    ):
        ok = update_repodata_at_prefix(
            prefix,
            r2_client=client,
            staged_binaries=["keep-2-1.x86_64.rpm"],
        )

    assert ok is True
    sync.assert_not_called()
    createrepo.assert_called_once()
    assert not (rpms_dir / "ghost-1-1.x86_64.rpm").exists()
    assert (rpms_dir / "keep-2-1.x86_64.rpm").exists()


def test_staged_identical_version_rebuild_always_runs_createrepo(tmp_path: Path) -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    container = tmp_path / "stage"
    repo_root = container / "repo"
    rpms_dir = repo_root / "RPMS"
    rpms_dir.mkdir(parents=True)
    keys = [{"key": f"{prefix}/RPMS/pkg-{i}.x86_64.rpm"} for i in range(120)]
    for i in range(120):
        (rpms_dir / f"pkg-{i}.x86_64.rpm").write_bytes(b"old")
    repodata = repo_root / "repodata"
    repodata.mkdir()
    (repodata / "repomd.xml").write_text("<?xml version='1.0'?><repomd/>")
    (repodata / "deadbeef-primary.xml.zst").write_bytes(b"x" * 4096)

    client = MagicMock()
    client.list_objects.return_value = keys
    client.get_object.return_value = b"rebuilt-bytes"

    with (
        patch("oreon_build.services.publisher._repo_temp_dir_parent", return_value=str(tmp_path)),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=container),
        patch("oreon_build.services.publisher._count_primary_packages", return_value=120),
        patch("oreon_build.services.publisher._sync_rpms_dir_from_r2") as sync,
        patch("oreon_build.services.publisher._clear_createrepo_cache") as clear_cache,
        patch("oreon_build.services.publisher._run_createrepo") as createrepo,
        patch("oreon_build.services.publisher._validate_repodata_sane"),
        patch("oreon_build.services.publisher._upload_repodata_dir_ordered"),
        patch("oreon_build.services.publisher._cleanup_old_repodata"),
        patch("oreon_build.services.publisher.sign_repomd"),
    ):
        ok = update_repodata_at_prefix(
            prefix,
            r2_client=client,
            staged_binaries=["pkg-0.x86_64.rpm"],
        )

    assert ok is True
    sync.assert_not_called()
    clear_cache.assert_not_called()
    createrepo.assert_called_once()
    assert createrepo.call_args.kwargs.get("force_full") is False
    client.get_object.assert_called()
    assert (rpms_dir / "pkg-0.x86_64.rpm").read_bytes() == b"rebuilt-bytes"


def test_staged_refresh_overwrites_same_size_stale_local(tmp_path: Path) -> None:
    """Same NEVRA promote can replace R2 bytes at identical size. never size-skip."""
    from oreon_build.services.publisher import _refresh_staged_rpms_from_r2

    rpms_dir = tmp_path / "RPMS"
    rpms_dir.mkdir()
    stale = b"A" * 64
    fresh = b"B" * 64
    (rpms_dir / "oreon-release-11-4.or11.noarch.rpm").write_bytes(stale)
    client = MagicMock()
    client.get_object.return_value = fresh
    _refresh_staged_rpms_from_r2(
        client,
        "oreon-11-rp1/testing/x86_64/RPMS/",
        rpms_dir,
        ["oreon-release-11-4.or11.noarch.rpm"],
        container=tmp_path,
    )
    assert (rpms_dir / "oreon-release-11-4.or11.noarch.rpm").read_bytes() == fresh
    client.get_object.assert_called_once()


def test_force_full_warm_mirror_still_syncs_from_r2(tmp_path: Path) -> None:
    prefix = "oreon-11-rp1/testing/x86_64"
    container = tmp_path / "stage"
    repo_root = container / "repo"
    rpms_dir = repo_root / "RPMS"
    rpms_dir.mkdir(parents=True)
    keys = [{"key": f"{prefix}/RPMS/pkg-{i}.x86_64.rpm", "size": 3} for i in range(120)]
    for i in range(120):
        (rpms_dir / f"pkg-{i}.x86_64.rpm").write_bytes(b"old")
    repodata = repo_root / "repodata"
    repodata.mkdir()
    (repodata / "repomd.xml").write_text("<?xml version='1.0'?><repomd/>")
    (repodata / "deadbeef-primary.xml.zst").write_bytes(b"x" * 4096)

    client = MagicMock()
    client.list_objects.return_value = keys

    with (
        patch("oreon_build.services.publisher._repo_temp_dir_parent", return_value=str(tmp_path)),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=container),
        patch("oreon_build.services.publisher._count_primary_packages", return_value=120),
        patch("oreon_build.services.publisher._sync_rpms_dir_from_r2", return_value=(120, 1)) as sync,
        patch("oreon_build.services.publisher._clear_createrepo_cache") as clear_cache,
        patch("oreon_build.services.publisher._run_createrepo") as createrepo,
        patch("oreon_build.services.publisher._validate_repodata_sane"),
        patch("oreon_build.services.publisher._upload_repodata_dir_ordered"),
        patch("oreon_build.services.publisher._cleanup_old_repodata"),
        patch("oreon_build.services.publisher.sign_repomd"),
    ):
        ok = update_repodata_at_prefix(prefix, r2_client=client, force_full=True)

    assert ok is True
    sync.assert_called_once()
    clear_cache.assert_called_once()
    createrepo.assert_called_once()
    assert createrepo.call_args.kwargs.get("force_full") is True


def test_staged_publish_with_truncated_local_repodata_pulls_r2_baseline_not_full_regen(
    tmp_path: Path,
) -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    container = tmp_path / "stage"
    repo_root = container / "repo"
    rpms_dir = repo_root / "RPMS"
    rpms_dir.mkdir(parents=True)
    keys = [{"key": f"{prefix}/RPMS/pkg-{i}.x86_64.rpm"} for i in range(120)]
    for i in range(120):
        (rpms_dir / f"pkg-{i}.x86_64.rpm").write_bytes(b"rpm")

    client = MagicMock()
    client.list_objects.return_value = keys
    client.get_object.return_value = b"staged"

    looks_valid = MagicMock(side_effect=[False, True])

    def _fake_sync_repodata(_client, _prefix, repodata_dir: Path) -> int:
        repodata_dir.mkdir(parents=True, exist_ok=True)
        (repodata_dir / "repomd.xml").write_text("<?xml version='1.0'?><repomd/>")
        return 1

    with (
        patch("oreon_build.services.publisher._repo_temp_dir_parent", return_value=str(tmp_path)),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=container),
        patch("oreon_build.services.publisher._count_primary_packages", return_value=120),
        patch("oreon_build.services.publisher._repodata_looks_valid", looks_valid),
        patch("oreon_build.services.publisher._sync_repodata_from_r2", side_effect=_fake_sync_repodata) as sync_repodata,
        patch("oreon_build.services.publisher._run_createrepo") as createrepo,
        patch("oreon_build.services.publisher._validate_repodata_sane"),
        patch("oreon_build.services.publisher._upload_repodata_dir_ordered"),
        patch("oreon_build.services.publisher._cleanup_old_repodata"),
        patch("oreon_build.services.publisher.sign_repomd"),
    ):
        ok = update_repodata_at_prefix(
            prefix,
            r2_client=client,
            staged_binaries=["pkg-0.x86_64.rpm"],
        )

    assert ok is True
    sync_repodata.assert_called_once()
    createrepo.assert_called_once()
    assert createrepo.call_args.kwargs.get("force_full") is False
