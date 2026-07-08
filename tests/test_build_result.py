from oreon_build.core.build_result import (
    is_architecture_excluded_error,
    normalize_terminal_build_status,
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
