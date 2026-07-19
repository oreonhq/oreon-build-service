from oreon_build.core.build_result import (
    find_architecture_excluded_line,
    is_architecture_excluded_error,
    normalize_terminal_build_status,
    prepare_build_error_message,
)


def test_is_architecture_excluded_error() -> None:
    assert is_architecture_excluded_error("error: Architecture is not included: aarch64")
    assert is_architecture_excluded_error("ERROR: architecture is not included: x86_64")
    assert not is_architecture_excluded_error("mock --rebuild failed")
    assert not is_architecture_excluded_error(None)


def test_normalize_terminal_build_status_arch_skip() -> None:
    assert (
        normalize_terminal_build_status(
            "failed",
            "error: Architecture is not included: aarch64",
        )
        == "skipped"
    )
    assert normalize_terminal_build_status("failed", "some other error") == "failed"
    assert normalize_terminal_build_status("success", None) == "success"


def test_find_architecture_excluded_line_in_long_log() -> None:
    log = "prep\n" * 200 + "error: Architecture is not included: aarch64\nmore\n" * 3
    hit = find_architecture_excluded_line(log)
    assert hit is not None
    assert "architecture is not included: aarch64" in hit.lower()
    assert normalize_terminal_build_status("failed", hit) == "skipped"


def test_prepare_build_error_message_prefers_arch_line_over_head_truncation() -> None:
    head = "Binary RPM build (mock --rebuild)\n" + ("noise\n" * 400)
    tail = "error: Architecture is not included: aarch64\n"
    full = head + tail
    assert len(full) > 2000
    assert "Architecture is not included" not in full[:2000]
    msg = prepare_build_error_message(full)
    assert msg.lower().startswith("error: architecture is not included: aarch64")
    assert normalize_terminal_build_status("failed", msg) == "skipped"


def test_prepare_build_error_message_keeps_tail_for_other_errors() -> None:
    body = ("line\n" * 500) + "FINAL_ERROR_MARKER\n"
    msg = prepare_build_error_message(body, limit=200)
    assert "FINAL_ERROR_MARKER" in msg
    assert len(msg) <= 200
