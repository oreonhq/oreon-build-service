from pathlib import Path

from oreon_build.cli.orbscli import bump_spec_to_version, find_pkg_specs, parse_package_blob


def test_parse_package_blob_three_formats():
    assert parse_package_blob("pkg1\npkg2\npkg3") == ["pkg1", "pkg2", "pkg3"]
    assert parse_package_blob("pkg1, pkg2, pkg3") == ["pkg1", "pkg2", "pkg3"]
    assert parse_package_blob("pkg1 pkg2 pkg3") == ["pkg1", "pkg2", "pkg3"]


def test_bump_spec_rewrites_version_release_and_source_not_changelog():
    spec = "\n".join(
        [
            "Name: foo",
            "Version: 1.2.3",
            "Release: 5%{?dist}",
            "Source0: https://example.com/foo-1.2.3.tar.gz",
            "%changelog",
            "* Sat Aug 15 2026 Someone - 1.2.3-5",
            "- old",
            "",
        ]
    )
    out, old, new = bump_spec_to_version(spec, "1.4.0")
    assert old == "1.2.3"
    assert new == "1.4.0"
    assert "Version: 1.4.0" in out
    assert "Release: 1%{?dist}" in out
    assert "foo-1.4.0.tar.gz" in out
    assert "* Sat Aug 15 2026 Someone - 1.2.3-5" in out


def test_bump_spec_define_version():
    spec = "%global version 0.8.0\nVersion: %{version}\nRelease: 2%{?dist}\n"
    out, old, new = bump_spec_to_version(spec, "0.9.1")
    assert old == "0.8.0"
    assert new == "0.9.1"
    assert "%global version 0.9.1" in out
    assert "Version: %{version}" in out
    assert "Release: 1%{?dist}" in out


def test_find_pkg_specs_release_subdir(tmp_path: Path):
    p = tmp_path / "oreon-11-rp1" / "foo"
    p.mkdir(parents=True)
    spec = p / "foo.spec"
    spec.write_text("Version: 1\n", encoding="utf-8")
    assert find_pkg_specs(tmp_path, "foo") == [spec]
    assert find_pkg_specs(tmp_path / "oreon-11-rp1", "foo") == [spec]
