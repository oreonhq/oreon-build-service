from oreon_build.core.spec_bcond import (
    active_brs,
    active_build_brs,
    default_build_defines,
    default_distro_macros,
    parse_spec_bconds,
    rpm_bcond_define_args,
    solve_bootstrap_defines,
)


def test_parse_bcond_with_defaults_off() -> None:
    spec = """
%bcond_with check
%if %{with check}
BuildRequires: gcc
%endif
BuildRequires: make
"""
    info = parse_spec_bconds(spec)
    assert info.defaults.get("with_check") == 0
    assert "gcc" not in info.unconditional_brs
    assert "make" in info.unconditional_brs


def test_parse_modern_bcond_default_on() -> None:
    spec = """
%bcond check 1
%bcond manpages 1
%bcond scancode %{defined fedora}
%if %{with check}
BuildRequires: askalono-cli
BuildRequires: trivy
%endif
%if %{with manpages}
BuildRequires: scdoc
%endif
%if %{with scancode}
BuildRequires: scancode-toolkit
%endif
BuildRequires: python3-devel
"""
    info = parse_spec_bconds(spec, distro_macros=default_distro_macros("oreon-11-rp1"))
    assert info.defaults.get("with_check") == 1
    assert info.defaults.get("with_manpages") == 1
    assert info.defaults.get("with_scancode") == 0
    brs = active_build_brs(info, default_build_defines(info))
    assert "askalono-cli" in brs
    assert "trivy" in brs
    assert "scdoc" in brs
    assert "python3-devel" in brs
    assert "scancode-toolkit" not in brs

    fedora = parse_spec_bconds(spec, distro_macros=default_distro_macros("fedora-44"))
    assert fedora.defaults.get("with_scancode") == 1
    fbrs = active_build_brs(fedora, default_build_defines(fedora))
    assert "scancode-toolkit" in fbrs


def test_undefined_rhel_keeps_oreon_bcond_on() -> None:
    spec = """
%bcond bench %[%{undefined rhel} || 0%{?rhel} < 11]
%if %{with bench}
BuildRequires: python3-matplotlib
%endif
BuildRequires: cmake
"""
    oreon = parse_spec_bconds(spec, distro_macros=default_distro_macros("oreon-11-rp1"))
    assert oreon.defaults.get("with_bench") == 1
    obrs = active_build_brs(oreon, default_build_defines(oreon))
    assert "python3-matplotlib" in obrs
    assert "cmake" in obrs

    el11 = parse_spec_bconds(spec, distro_macros={"rhel": "11"})
    assert el11.defaults.get("with_bench") == 0
    ebrs = active_build_brs(el11, default_build_defines(el11))
    assert "python3-matplotlib" not in ebrs
    assert "cmake" in ebrs
    spec = """
%bcond scancode 1
%bcond scancode_tests %[ %{with scancode} && 1 ]
%if %{with scancode_tests}
BuildRequires: pytest
%endif
"""
    info = parse_spec_bconds(spec)
    assert info.defaults.get("with_scancode") == 1
    assert info.defaults.get("with_scancode_tests") == 1
    assert "pytest" in active_build_brs(info, default_build_defines(info))


def test_parse_bcond_without_defaults_on() -> None:
    spec = """
%bcond_without check
%if %{with check}
BuildRequires: gcc
%endif
BuildRequires: make
"""
    info = parse_spec_bconds(spec)
    assert info.defaults.get("with_check") == 1
    assert "gcc" in info.conditional_brs.get("with_check", set())
    assert "make" in info.unconditional_brs


def test_nested_conditional_version_macro() -> None:
    from oreon_build.core.spec_bcond import _expand_br_macros

    assert (
        _expand_br_macros(
            "%{up_version}%{?up_prever:~%{up_prever}}",
            {"up_version": "2.3.1"},
        )
        == "2.3.1"
    )
    assert (
        _expand_br_macros(
            "%{up_version}%{?up_prever:~%{up_prever}}",
            {"up_version": "2.3.1", "up_prever": "rc1"},
        )
        == "2.3.1~rc1"
    )


def test_global_dotted_version_not_bool_eval() -> None:
    spec = """
%bcond_without compat_build
%global maj_ver 15
%global api_ver 2.6
%if %{with compat_build}
%global pkg_name llvm%{maj_ver}
%else
%global pkg_name llvm
%endif
"""
    info = parse_spec_bconds(spec)
    assert info.macros.get("api_ver") == "2.6"
    assert info.macros.get("pkg_name") == "llvm15"


def test_default_build_defines_uses_spec_defaults() -> None:
    spec = """
%bcond_without ecore
%if %{with ecore}
BuildRequires: ecore-devel
%endif
"""
    info = parse_spec_bconds(spec)
    assert default_build_defines(info) == {"with_ecore": "1"}


def test_solve_bootstrap_disables_gating_bcond() -> None:
    spec = """
%bcond_without check
%if %{with check}
BuildRequires: missing-pkg
%endif
BuildRequires: make
"""
    info = parse_spec_bconds(spec)
    profile, defines = solve_bootstrap_defines(info, {"make"})
    assert profile == "bootstrap"
    assert defines.get("with_check") == "0"
    assert active_brs(info, defines) <= {"make"}


def test_solve_full_when_repo_has_everything() -> None:
    spec = """
%bcond_without check
%if %{with check}
BuildRequires: gcc
%endif
"""
    info = parse_spec_bconds(spec)
    profile, defines = solve_bootstrap_defines(info, {"gcc"})
    assert profile == "full"
    assert defines == {}


def test_libffado_style_brs_keep_virtual_caps_primary_only() -> None:
    spec = """
BuildRequires: libxml++-devel
BuildRequires: python3-qt5-devel
BuildRequires: python3dist(setuptools)
BuildRequires: rpm_macro(py3_shebang_fix)
"""
    info = parse_spec_bconds(spec, distro_macros=default_distro_macros("oreon-11-rp1"))
    brs = active_build_brs(info, {})
    assert "libxml++-devel" in brs
    assert "python3-qt5-devel" in brs
    assert "python3dist(setuptools)" in brs
    assert "rpm_macro(py3_shebang_fix)" in brs
    assert "setuptools" not in brs
    assert "py3_shebang_fix" not in brs


def test_rpm_bang_not_and_ifnarch_keeps_ffado_br() -> None:
    from oreon_build.core.spec_bcond import _eval_condition, _expand_expr

    macros = default_distro_macros("oreon-11-rp1")
    macros["bootstrap"] = "0"
    expr = "!0%{?bootstrap} && !0%{?flatpak} && 0%{?rhel} < 9"
    assert "not" in _expand_expr(expr, macros, {})
    assert _eval_condition(expr, macros, {}) is True

    spec = """
%global bootstrap 0
BuildRequires: alsa-lib-devel
%ifnarch s390 s390x
%if !0%{?bootstrap} && !0%{?flatpak} && 0%{?rhel} < 9
BuildRequires: libffado-devel
%endif
%endif
BuildRequires: python3
"""
    info = parse_spec_bconds(spec, distro_macros=macros, target_arches={"x86_64", "aarch64"})
    assert "libffado-devel" in info.unconditional_brs
    assert "libffado-devel" in active_build_brs(info, {k: str(v) for k, v in info.defaults.items()})

    s390 = parse_spec_bconds(spec, distro_macros=macros, target_arches={"s390x"})
    assert "libffado-devel" not in s390.unconditional_brs


def test_defined_macro_enables_oreon_bcond_without_optional() -> None:
    spec = """
%if %{defined perl_bootstrap}
%bcond_without optional_test
%else
%bcond_with optional_test
%endif
%if %{with optional_test}
BuildRequires: perl(JSON::MaybeXS)
BuildRequires: perl(Test::Class)
BuildRequires: perl(Test::Script)
%endif
BuildRequires: perl(ExtUtils::MakeMaker)
"""
    macros = default_distro_macros("oreon-11-rp1")
    macros["perl_bootstrap"] = "1"
    info = parse_spec_bconds(spec, distro_macros=macros)
    assert info.defaults.get("with_optional_test") == 1
    brs = active_build_brs(info, default_build_defines(info))
    assert "perl(JSON::MaybeXS)" in brs
    assert "perl(Test::Class)" in brs
    assert "perl(Test::Script)" in brs


def test_py3_dist_macro_expands_in_buildrequires() -> None:
    spec = """
BuildRequires: %{py3_dist setuptools jinja2}
BuildRequires: make
"""
    info = parse_spec_bconds(spec, distro_macros=default_distro_macros("oreon-11-rp1"))
    brs = active_build_brs(info, {})
    assert "python3dist(setuptools)" in brs
    assert "python3dist(jinja2)" in brs
    assert "setuptools" not in brs
    assert "jinja2}" not in brs
    assert "make" in brs


def test_negated_optional_macro_enables_extra_test_brs() -> None:
    # rpm 0%{!?perl_bootstrap:1} == 01 when perl_bootstrap unset
    spec = """
%if ! (0%{?rhel})
%bcond_without perl_B_Keywords_enables_extra_test
%else
%bcond_with perl_B_Keywords_enables_extra_test
%endif
BuildRequires: perl(Test::More)
%if 0%{!?perl_bootstrap:1} && %{with perl_B_Keywords_enables_extra_test}
BuildRequires: perl(Perl::MinimumVersion) >= 1.20
BuildRequires: perl(Test::Kwalitee)
BuildRequires: perl(Test::MinimumVersion) >= 0.008
BuildRequires: perl(Text::CSV_XS)
%endif
"""
    macros = default_distro_macros("oreon-11-rp1")
    info = parse_spec_bconds(spec, distro_macros=macros)
    assert info.defaults.get("with_perl_B_Keywords_enables_extra_test") == 1
    brs = active_build_brs(info, default_build_defines(info))
    assert "perl(Test::More)" in brs
    assert "perl(Perl::MinimumVersion) >= 1.20" in brs
    assert "perl(Test::Kwalitee)" in brs
    assert "perl(Test::MinimumVersion) >= 0.008" in brs
    assert "perl(Text::CSV_XS)" in brs

    macros2 = dict(macros)
    macros2["perl_bootstrap"] = "1"
    info2 = parse_spec_bconds(spec, distro_macros=macros2)
    brs2 = active_build_brs(info2, default_build_defines(info2))
    assert "perl(Test::More)" in brs2
    assert "perl(Test::Kwalitee)" not in brs2
    assert "perl(Text::CSV_XS)" not in brs2


def test_graphviz_style_globals_disable_optional_brs() -> None:
    spec = """
%bcond_with guile
%bcond_with python2
%global DEVIL  1
%global QTAPPS 0
%global GTS    0
%if 0%{?oreon} >= 11
%global DEVIL  0
%endif
%if %{with guile}
%global GUILE 1
%else
%global GUILE 0
%endif
Name: graphviz
%if %{GUILE}
BuildRequires: guile30-devel
%endif
%if %{with python2}
BuildRequires: python2-devel
%endif
%if %{DEVIL}
BuildRequires: DevIL-devel
%endif
%if %{QTAPPS}
BuildRequires: qt-devel
%endif
%if %{GTS}
BuildRequires: gts-devel
%endif
BuildRequires: make
"""
    info = parse_spec_bconds(spec, distro_macros=default_distro_macros("oreon-11-rp1"))
    assert info.macros.get("DEVIL") == "0"
    assert info.macros.get("GUILE") == "0"
    assert "DevIL-devel" not in info.unconditional_brs
    assert "guile30-devel" not in info.unconditional_brs
    assert "python2-devel" not in info.unconditional_brs
    assert "qt-devel" not in info.unconditional_brs
    assert "gts-devel" not in info.unconditional_brs
    assert "make" in info.unconditional_brs
    profile, _ = solve_bootstrap_defines(info, {"make"})
    assert profile == "full"
    assert "DevIL-devel" not in active_build_brs(info, {k: str(v) for k, v in info.defaults.items()})


def test_rpm_bcond_define_args_uses_with_without_macros() -> None:
    flat = " ".join(
        rpm_bcond_define_args(
            {
                "with_perl_Test_Simple_enables_optional_test": "0",
                "oreon": "0",
                "with_check": "1",
            }
        )
    )
    assert (
        "_without_perl_Test_Simple_enables_optional_test --without-perl_Test_Simple_enables_optional_test"
        in flat
    )
    assert "_with_check --with-check" in flat
    assert "oreon 0" in flat
    assert "with_check 0" not in flat
    assert "with_check 1" not in flat
    assert "with_perl_Test_Simple_enables_optional_test 0" not in flat


def test_rpmspec_with_check_zero_does_not_enable_check_brs() -> None:
    """Regression: --define 'with_check 0' wrongly enabled %{with check}."""
    from oreon_build.services.dynamic_brs import rpmspec_query_buildrequires

    spec = """
%bcond check 0
Name: demo
Version: 1
Release: 1
Summary: x
License: MIT
BuildRequires: foo
%if %{with check}
BuildRequires: bar-check
%endif
%description
x
%prep
%build
%install
%files
"""
    brs = rpmspec_query_buildrequires(spec, defines={"with_check": "0"})
    assert "foo" in brs
    assert "bar-check" not in brs


def test_simple_qmark_macro_gate_hal() -> None:
    spec = """
%define hal 1
%define udisks2 1
BuildRequires: qt-devel
%if 0%{?hal:1}
BuildRequires: hal-devel
%endif
%if 0%{?udisks2:1}
BuildRequires: udisks2-devel
%endif
"""
    info = parse_spec_bconds(spec)
    assert "hal-devel" not in info.unconditional_brs
    assert "hal-devel" in info.conditional_brs.get("hal", set())
    assert "hal-devel" in active_build_brs(info, {})
    assert "hal-devel" not in active_build_brs(info, {"hal": "0"})
    assert "udisks2-devel" in active_build_brs(info, {})


def test_version_macro_expands_in_buildrequires() -> None:
    spec = """
%global framework baloo
Name: kf6-%{framework}
Version: 6.28.0
Release: 1
BuildRequires: extra-cmake-modules >= %{version}
BuildRequires: cmake(KF6Config)
"""
    info = parse_spec_bconds(spec)
    assert info.macros.get("version") == "6.28.0"
    assert info.macros.get("name") == "kf6-baloo"
    assert "extra-cmake-modules >= 6.28.0" in info.unconditional_brs
    assert "extra-cmake-modules >= %{version}" not in info.unconditional_brs
