from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.services.build_chain import done_slots_from_job_rows, newest_done_slots_from_lineage


def test_done_slots_include_success_and_skipped_only() -> None:
    rows = [
        (1, "x86_64", "success"),
        (2, "aarch64", "success"),
        (3, "x86_64", "skipped"),
        (4, "x86_64", "failed"),
        (5, "aarch64", "cancelled"),
        (6, "x86_64", "running"),
    ]
    slots = done_slots_from_job_rows(rows)
    assert slots == {
        (1, normalize_machine_arch("x86_64")),
        (2, normalize_machine_arch("aarch64")),
        (3, normalize_machine_arch("x86_64")),
    }


def test_lineage_keeps_ancestor_success_when_later_chain_has_no_job() -> None:
    lineage = [747, 746, 743]
    rows = [
        (746, 10, "x86_64", "success"),
        (743, 10, "x86_64", "success"),
        (747, 11, "x86_64", "cancelled"),
        (746, 11, "x86_64", "cancelled"),
        (747, 12, "aarch64", "failed"),
        (743, 12, "aarch64", "success"),
    ]
    slots = newest_done_slots_from_lineage(lineage, rows)
    assert (10, normalize_machine_arch("x86_64")) in slots
    assert (11, normalize_machine_arch("x86_64")) not in slots
    assert (12, normalize_machine_arch("aarch64")) not in slots


def test_lineage_cancelled_resume_does_not_undo_ancestor_success() -> None:
    lineage = [748, 747, 746]
    rows = [
        (748, 10, "x86_64", "cancelled"),
        (746, 10, "x86_64", "success"),
        (748, 11, "aarch64", "failed"),
        (746, 11, "aarch64", "success"),
    ]
    slots = newest_done_slots_from_lineage(lineage, rows)
    assert (10, normalize_machine_arch("x86_64")) in slots
    assert (11, normalize_machine_arch("aarch64")) not in slots