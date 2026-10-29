# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.worker.main import _mock_rpmbuild_define_args


def test_with_zero_uses_rpm_without_macro() -> None:
    args = _mock_rpmbuild_define_args(
        {
            "with_perl_Test_Simple_enables_optional_test": "0",
            "oreon": "0",
        }
    )
    flat = " ".join(args)
    assert "--without=perl_Test_Simple_enables_optional_test" in args
    assert "_without_perl_Test_Simple_enables_optional_test --without-perl_Test_Simple_enables_optional_test" in flat
    assert "oreon 0" in flat
    assert "with_perl_Test_Simple_enables_optional_test 0" not in flat


def test_with_one_uses_rpm_with_macro() -> None:
    args = _mock_rpmbuild_define_args({"with_check": "1"})
    flat = " ".join(args)
    assert "--with=check" in args
    assert "_with_check --with-check" in flat
    assert "with_check 1" not in flat


def test_bootstrap_with_check_zero_passes_mock_without() -> None:
    args = _mock_rpmbuild_define_args({"with_check": "0"})
    assert "--without=check" in args
    assert args[args.index("--define") + 1] == "_without_check --without-check"
