from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from oreon_build.services.repo_publish import process_repo_publish_sync


def test_publish_stages_rpms_already_at_repo_path() -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    rpm_key = f"{prefix}/RPMS/graphviz-14.1.4-3.or11.x86_64.rpm"
    src_key = "oreon-11-rp1/dev/src/graphviz-14.1.4-3.or11.src.rpm"
    artifacts = [
        SimpleNamespace(
            filename="graphviz-14.1.4-3.or11.x86_64.rpm",
            r2_key=rpm_key,
        ),
        SimpleNamespace(
            filename="graphviz-14.1.4-3.or11.src.rpm",
            r2_key=src_key,
        ),
    ]

    client = MagicMock()
    client.get_object.return_value = b"rpm-bytes"

    with (
        patch("oreon_build.services.repo_publish.get_r2_client", return_value=client),
        patch("oreon_build.services.repo_publish.prefix_publish_lock"),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=MagicMock()),
        patch("oreon_build.services.publisher._ensure_stage_sidecar"),
        patch("oreon_build.services.publisher._persistent_repo_root") as repo_root,
        patch("oreon_build.api.worker_api._prune_superseded_repo_objects"),
        patch("oreon_build.services.repo_publish.update_repodata_at_prefix") as update_meta,
    ):
        rpms_dir = MagicMock()
        repo_root.return_value.__truediv__.return_value = rpms_dir
        process_repo_publish_sync(
            releasename="oreon-11-rp1",
            branch="dev",
            arch="x86_64",
            attempt_ids=[1],
            payload_artifacts=artifacts,
            build_attempt_r2_keys=[rpm_key, src_key],
        )

    client.put_object.assert_not_called()
    rpms_dir.mkdir.assert_called()
    update_meta.assert_called_once()
    assert update_meta.call_args.kwargs["staged_binaries"] == [
        "graphviz-14.1.4-3.or11.x86_64.rpm"
    ]


def test_publish_still_copies_legacy_builds_staging_keys() -> None:
    prefix = "oreon-11-rp1/dev/x86_64"
    staging_key = "oreon-11-rp1/builds/99/graphviz-14.1.4-3.or11.x86_64.rpm"
    dest_key = f"{prefix}/RPMS/graphviz-14.1.4-3.or11.x86_64.rpm"
    artifacts = [
        SimpleNamespace(
            filename="graphviz-14.1.4-3.or11.x86_64.rpm",
            r2_key=staging_key,
        ),
    ]

    client = MagicMock()
    client.get_object.return_value = b"rpm-bytes"

    with (
        patch("oreon_build.services.repo_publish.get_r2_client", return_value=client),
        patch("oreon_build.services.repo_publish.prefix_publish_lock"),
        patch("oreon_build.services.publisher._stage_container_for_prefix", return_value=MagicMock()),
        patch("oreon_build.services.publisher._ensure_stage_sidecar"),
        patch("oreon_build.services.publisher._persistent_repo_root") as repo_root,
        patch("oreon_build.services.publisher._clear_createrepo_cache"),
        patch("oreon_build.api.worker_api._prune_superseded_repo_objects"),
        patch("oreon_build.services.repo_publish.update_repodata_at_prefix") as update_meta,
    ):
        rpms_dir = MagicMock()
        repo_root.return_value.__truediv__.return_value = rpms_dir
        process_repo_publish_sync(
            releasename="oreon-11-rp1",
            branch="dev",
            arch="x86_64",
            attempt_ids=[99],
            payload_artifacts=artifacts,
            build_attempt_r2_keys=[staging_key],
        )

    client.put_object.assert_called_once_with(dest_key, b"rpm-bytes")
    update_meta.assert_called_once()
