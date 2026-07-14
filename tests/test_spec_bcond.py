from oreon_build.core.spec_bcond import (
    active_brs,
    active_build_brs,
    default_build_defines,
    default_distro_macros,
    parse_spec_bconds,
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
