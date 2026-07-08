from oreon_build.core.machine_arch import normalize_machine_arch
from oreon_build.services.build_chain import done_slots_from_job_rows


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