from oreon_build.services.rpmbuild_settle import _known_sources_from_primary, _resolve_primary_key


def test_resolve_primary_key_from_repomd() -> None:
    repomd = b"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <location href="repodata/primary.xml.gz"/>
  </data>
</repomd>
"""
    key = _resolve_primary_key("oreon/dev/x86_64", repomd)
    assert key == "oreon/dev/x86_64/repodata/primary.xml.gz"


def test_known_sources_from_primary_xml() -> None:
    primary = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" packages="1">
  <package type="rpm">
    <name>foo</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.0" rel="1.or1"/>
    <format xmlns:rpm="http://linux.duke.edu/metadata/rpm">
      <rpm:sourcerpm>foo-1.0-1.or1.src.rpm</rpm:sourcerpm>
    </format>
  </package>
</metadata>
"""
    out = _known_sources_from_primary(primary)
    assert "foo" in out
