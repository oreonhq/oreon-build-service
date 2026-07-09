from __future__ import annotations

import zstandard as zstd

from oreon_build.services.repo_capability_index import _decompress_if_needed, _ingest_primary_xml, RepoCapabilityIndex


def test_decompress_primary_zst() -> None:
    xml = b'<?xml version="1.0"?><metadata packages="1"></metadata>'
    raw = zstd.ZstdCompressor().compress(xml)
    out = _decompress_if_needed(raw, "repodata/abc-primary.xml.zst")
    assert out == xml


def test_ingest_primary_xml_populates_names() -> None:
    xml = b"""<?xml version="1.0"?>
<metadata>
  <package type="rpm">
    <name>ocaml-srpm-macros</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="11" rel="3.or11"/>
    <format>
      <rpm:sourcerpm xmlns:rpm="http://linux.duke.edu/metadata/rpm">ocaml-srpm-macros-11-3.or11.src.rpm</rpm:sourcerpm>
      <rpm:provides xmlns:rpm="http://linux.duke.edu/metadata/rpm">
        <rpm:entry name="ocaml-srpm-macros"/>
      </rpm:provides>
    </format>
  </package>
</metadata>"""
    idx = RepoCapabilityIndex()
    _ingest_primary_xml(idx, xml)
    assert "ocaml-srpm-macros" in idx.binary_names
    assert "ocaml-srpm-macros" in idx.source_names
