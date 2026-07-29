from oreon_build.worker.main import _mock_rpmbuild_define_args


def test_with_zero_uses_rpm_without_macro() -> None:
    args = _mock_rpmbuild_define_args(
        {
            "with_perl_Test_Simple_enables_optional_test": "0",
            "oreon": "0",
        }
    )
    flat = " ".join(args)
    assert "_without_perl_Test_Simple_enables_optional_test --without-perl_Test_Simple_enables_optional_test" in flat
    assert "oreon 0" in flat
    assert "with_perl_Test_Simple_enables_optional_test 0" not in flat


def test_with_one_uses_rpm_with_macro() -> None:
    args = _mock_rpmbuild_define_args({"with_check": "1"})
    flat = " ".join(args)
    assert "_with_check --with-check" in flat
    assert "with_check 1" not in flat
