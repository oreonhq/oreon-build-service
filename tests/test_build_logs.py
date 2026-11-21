# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.services.build_logs import (
    is_result_log,
    iter_log_files,
    safe_log_name,
    sort_log_names,
)
from oreon_build.services.r2 import log_file_r2_key, log_files_prefix


def test_safe_log_name_rejects_escape():
    assert safe_log_name("../build.log") is None
    assert safe_log_name("/etc/passwd") is None
    assert safe_log_name("srpm/../../root.log") is None
    assert safe_log_name("rebuild/build.log") == "rebuild/build.log"
    assert safe_log_name("noarch_rpmdiff.json") == "noarch_rpmdiff.json"


def test_is_result_log_skips_rpms(tmp_path):
    assert is_result_log(tmp_path / "build.log")
    assert is_result_log(tmp_path / "dnf5.log")
    assert is_result_log(tmp_path / "noarch_rpmdiff.json")
    assert is_result_log(tmp_path / "available_pkgs")
    assert not is_result_log(tmp_path / "bash.rpm")
    assert not is_result_log(tmp_path / "bash.src.rpm")


def test_iter_log_files_keeps_relative_names(tmp_path):
    (tmp_path / "build.log").write_text("full", encoding="utf-8")
    (tmp_path / "pkg.rpm").write_bytes(b"rpm")
    nested = tmp_path / "x86_64"
    nested.mkdir()
    (nested / "root.log").write_text("root", encoding="utf-8")
    outside = tmp_path.parent / "secret.log"
    outside.write_text("nope", encoding="utf-8")
    try:
        (tmp_path / "linked.log").symlink_to(outside)
    except OSError:
        pass
    names = [name for name, _path in iter_log_files(tmp_path)]
    assert "build.log" in names
    assert "x86_64/root.log" in names
    assert "pkg.rpm" not in names
    assert "linked.log" not in names
    assert all(safe_log_name(name) == name for name in names)


def test_sort_puts_mock_output_before_build_log():
    ordered = sort_log_names([
        "rebuild/build.log",
        "srpm/root.log",
        "rebuild/mock_output.log",
        "resolve/state.log",
        "rebuild/hw_info.log",
    ])
    assert ordered == [
        "srpm/root.log",
        "resolve/state.log",
        "rebuild/mock_output.log",
        "rebuild/build.log",
        "rebuild/hw_info.log",
    ]


def test_log_file_key_sits_beside_the_combined_log():
    assert log_files_prefix("oreon-11-rp1", 12) == "oreon-11-rp1/logs/12/"
    assert log_file_r2_key("oreon-11-rp1", 12, "rebuild/build.log") == (
        "oreon-11-rp1/logs/12/rebuild/build.log"
    )
