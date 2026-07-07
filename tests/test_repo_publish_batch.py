from oreon_build.services.repo_publish import (
    _PrefixPublishBatch,
    _group_publish_tasks_by_prefix,
)


def test_group_publish_tasks_merges_same_prefix():
    a = _PrefixPublishBatch(
        releasename="oreon-11-rp1",
        branch="dev",
        arch="x86_64",
        prefix="oreon-11-rp1/dev/x86_64",
        publish_task_ids=[1],
        attempt_ids=[10],
        payload_artifacts=["rpm-a"],
        build_attempt_r2_keys=["k-a"],
    )
    b = _PrefixPublishBatch(
        releasename="oreon-11-rp1",
        branch="dev",
        arch="x86_64",
        prefix="oreon-11-rp1/dev/x86_64",
        publish_task_ids=[2],
        attempt_ids=[11],
        payload_artifacts=["rpm-b"],
        build_attempt_r2_keys=["k-b"],
    )
    other = _PrefixPublishBatch(
        releasename="oreon-11-rp1",
        branch="dev",
        arch="aarch64",
        prefix="oreon-11-rp1/dev/aarch64",
        publish_task_ids=[3],
        attempt_ids=[12],
        payload_artifacts=["rpm-c"],
        build_attempt_r2_keys=["k-c"],
    )
    grouped = _group_publish_tasks_by_prefix([a, b, other])
    assert set(grouped.keys()) == {"oreon-11-rp1/dev/x86_64", "oreon-11-rp1/dev/aarch64"}
    merged = grouped["oreon-11-rp1/dev/x86_64"]
    assert merged.publish_task_ids == [1, 2]
    assert merged.attempt_ids == [10, 11]
    assert merged.payload_artifacts == ["rpm-a", "rpm-b"]
    assert merged.build_attempt_r2_keys == ["k-a", "k-b"]
