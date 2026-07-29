from __future__ import annotations

from oreon_build.services.dynamic_brs import (
    merge_buildrequires,
    rpmspec_query_buildrequires,
    spec_has_generate_buildrequires,
)


def test_detect_generate_buildrequires() -> None:
    assert spec_has_generate_buildrequires("%generate_buildrequires\n%pyproject_buildrequires\n")
    assert not spec_has_generate_buildrequires("BuildRequires: make\n")


def test_rpmspec_query_static_brs() -> None:
    spec = """
Name: demo
Version: 1
Release: 1%{?dist}
Summary: demo
License: MIT

BuildRequires: make
BuildRequires: gcc

%description
x

%prep

%build

%install

%files
"""
    brs = rpmspec_query_buildrequires(spec)
    assert "make" in brs
    assert "gcc" in brs


def test_merge_buildrequires_dedupes() -> None:
    assert merge_buildrequires(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_evaluate_without_generate_is_complete() -> None:
    from oreon_build.services.dynamic_brs import evaluate_generate_buildrequires

    spec = """
Name: demo
Version: 1
Release: 1
Summary: demo
License: MIT
BuildRequires: make
%description
x
%prep
%build
%install
%files
"""
    result = evaluate_generate_buildrequires(spec)
    assert result.complete is True
    assert "make" in result.buildrequires


def test_cargo_spec_fails_closed_when_nosrc_missing() -> None:
    """No buildreqs.nosrc means complete=False (static BRs are not the second pass)."""
    from oreon_build.services.dynamic_brs import (
        clear_dynamic_br_cache,
        evaluate_generate_buildrequires,
        spec_uses_cargo_generate_buildrequires,
    )

    clear_dynamic_br_cache()
    spec = """
Name: rust-demo
Version: 1
Release: 1
Summary: demo
License: MIT
%global crate demo
Source0: https://example.invalid/demo.crate
BuildRequires: cargo-rpm-macros
%prep
%cargo_prep
%generate_buildrequires
%cargo_generate_buildrequires
%build
%cargo_build
%install
%cargo_install
%files
"""
    assert spec_uses_cargo_generate_buildrequires(spec)
    result = evaluate_generate_buildrequires(spec, allow_source_download=False)
    assert result.complete is False
    assert "cargo-rpm-macros" in result.buildrequires


def test_golang_generate_uses_static_when_nosrc_missing() -> None:
    """golist-style generate needs go-vendor-tools in mock chroot. static BRs plan that."""
    from oreon_build.services.dynamic_brs import (
        clear_dynamic_br_cache,
        evaluate_generate_buildrequires,
    )

    clear_dynamic_br_cache()
    spec = """
Name: golist
Version: 1
Release: 1
Summary: demo
License: MIT
BuildRequires: go-vendor-tools
BuildRequires: golang
%prep
%generate_buildrequires
%go_vendor_license_buildrequires -c go-vendor-tools.toml
%build
%install
%files
"""
    result = evaluate_generate_buildrequires(spec, allow_source_download=False)
    assert result.complete is False
    assert "go-vendor-tools" in result.buildrequires
    assert "golang" in result.buildrequires


def test_local_source_basenames_includes_patches() -> None:
    from oreon_build.services.dynamic_brs import local_source_basenames

    spec = """
Name: rust-askalono
Version: 1
Release: 1
Summary: x
License: MIT
Source: https://crates.io/api/v1/crates/askalono/0.5.0/download#/askalono-0.5.0.crate
Patch: askalono-fix-metadata-auto.diff
Patch1: extra-local.patch
%description
x
%prep
%build
%install
%files
"""
    names = local_source_basenames(spec)
    assert "askalono-fix-metadata-auto.diff" in names
    assert "extra-local.patch" in names
    assert "askalono-0.5.0.crate" not in names


def test_requires_from_nosrc_via_python_rpm(tmp_path) -> None:
    """Mocked nosrc path: python-rpm reader returns rich Requires when present."""
    from unittest.mock import MagicMock, patch

    from oreon_build.services import dynamic_brs as dyn

    nosrc = tmp_path / "pkg.buildreqs.nosrc.rpm"
    nosrc.write_bytes(b"not-a-real-rpm")

    rich = "(crate(quickcheck) >= 1.0.0 with crate(quickcheck) < 2.0.0~)"
    feat = "(crate(unicode-segmentation/default) >= 1.2.1 with crate(unicode-segmentation/default) < 2.0.0~)"

    class FakeDep:
        def __init__(self, dnevr: str) -> None:
            self._d = dnevr

        def DNEVR(self) -> str:
            return self._d

    fake_deps = [
        FakeDep("R cargo-rpm-macros"),
        FakeDep(f"R {rich}"),
        FakeDep(f"R {feat}"),
        FakeDep("R rpmlib(CompressedFileNames) <= 3.0.4-1"),
    ]

    fake_rpm = MagicMock()
    fake_rpm._RPMVSF_NOSIGNATURES = 0
    fake_rpm.ds.return_value = fake_deps
    fake_ts = MagicMock()
    fake_ts.hdrFromFdno.return_value = object()
    fake_rpm.TransactionSet.return_value = fake_ts

    with patch.dict("sys.modules", {"rpm": fake_rpm}):
        with patch("os.open", return_value=3), patch("os.close"):
            brs = dyn._requires_from_nosrc_rpm(nosrc)

    assert "cargo-rpm-macros" in brs
    assert rich in brs
    assert feat in brs
    assert not any(b.startswith("rpmlib(") for b in brs)
