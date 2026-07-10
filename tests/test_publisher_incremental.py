from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from oreon_build.services.publisher import update_repodata_at_prefix


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
    clear_cache.assert_called_once()
    createrepo.assert_called_once()
    assert createrepo.call_args.kwargs.get("force_full") is False
    client.get_object.assert_called()


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
