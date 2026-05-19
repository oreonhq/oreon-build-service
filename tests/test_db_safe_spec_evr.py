"""package_versions.version is varchar(64); macro-heavy spec EVRs must not overflow it."""
from __future__ import annotations

from oreon_build.services.distgit_order import _parse_spec_text, db_safe_spec_evr


def test_db_safe_spec_evr_passes_short_clean_evr() -> None:
    assert db_safe_spec_evr("0:1.2.3-4.fc43") == "0:1.2.3-4.fc43"


def test_db_safe_spec_evr_hashes_unexpanded_macros() -> None:
    raw = (
        "0:3.0-%autorelease -s 20250708git"
        "%(c=8a97ebc88daa3495d6f57ec10bb515224400186f; echo ${c:0:7})"
    )
    safe = db_safe_spec_evr(raw)
    assert len(safe) <= 64
    assert "%" not in safe
    assert safe.startswith("0:unexpanded-")
    assert safe == db_safe_spec_evr(raw)


def test_evr_canonical_from_macro_heavy_spec() -> None:
    spec = """Name: javapackages-bootstrap
Version: 3.0-%autorelease
Release: -s 20250708git%(c=8a97ebc; echo ${c:0:7})
Epoch: 0
Provides: javapackages-bootstrap
"""
    parsed = _parse_spec_text(spec)
    evr = parsed.evr_canonical()
    assert len(evr) <= 64
    assert "%" not in evr
