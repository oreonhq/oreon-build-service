from oreon_build.core.spec_bcond import (
    active_brs,
    parse_spec_bconds,
    solve_bootstrap_defines,
)


def test_parse_bcond_with_and_conditional_br() -> None:
    spec = """
%bcond_with check
%if %{with_check}
BuildRequires: gcc
%endif
BuildRequires: make
"""
    info = parse_spec_bconds(spec)
    assert info.defaults.get("with_check") == 1
    assert "gcc" in info.conditional_brs.get("with_check", set())
    assert "make" in info.unconditional_brs


def test_solve_bootstrap_disables_gating_bcond() -> None:
    spec = """
%bcond_with check
%if %{with_check}
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
%bcond_with check
%if %{with_check}
BuildRequires: gcc
%endif
"""
    info = parse_spec_bconds(spec)
    profile, defines = solve_bootstrap_defines(info, {"gcc"})
    assert profile == "full"
    assert defines == {}
