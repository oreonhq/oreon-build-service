# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import zstandard as zstd

from oreon_build.services.repo_capability_index import (
    RepoCapabilityIndex,
    _decompress_if_needed,
    _ingest_primary_xml,
    _merge_arch_indexes,
)


def test_decompress_primary_zst() -> None:
    xml = b'<?xml version="1.0"?><metadata packages="1"></metadata>'
    raw = zstd.ZstdCompressor().compress(xml)
    out = _decompress_if_needed(raw, "repodata/abc-primary.xml.zst")
    assert out == xml


def test_transitive_unsatisfied_walks_binary_requires() -> None:
    idx = RepoCapabilityIndex(
        binary_names={"gv-devel", "gv-devil"},
        provides={"pkgconfig(libcgraph)", "gv-devel", "gv-devil"},
        capability_providers={
            "pkgconfig(libcgraph)": {"gv-devel"},
            "gv-devel": {"gv-devel"},
            "gv-devil": {"gv-devil"},
            "libIL.so.1()(64bit)": {"gv-devil"},
        },
        binary_requires={
            "gv-devel": {"gv-devil"},
            "gv-devil": {"libIL.so.1()(64bit)"},
        },
        binary_source={"gv-devil": "devil-src", "gv-devel": "gv"},
        source_names={"gv-devel"},
    )
    missing = idx.transitive_unsatisfied("pkgconfig(libcgraph)")
    assert "libIL.so.1()(64bit)" in missing
    assert idx.distgit_sources_for_capability("libIL.so.1()(64bit)") == {"devil-src"}


def test_broken_runtime_pulls_provider_not_consumer() -> None:
    from oreon_build.services.distgit_order import _collect_repo_transitive_candidates, _parse_spec_text

    repo = RepoCapabilityIndex(
        binary_names={"graphviz-devel", "graphviz-devil"},
        provides={"pkgconfig(libcgraph)", "graphviz-devel", "graphviz-devil"},
        capability_providers={
            "pkgconfig(libcgraph)": {"graphviz-devel"},
            "graphviz-devel": {"graphviz-devel"},
            "graphviz-devil": {"graphviz-devil"},
        },
        binary_requires={
            "graphviz-devel": {"graphviz-devil"},
            "graphviz-devil": {"libIL.so.1()(64bit)"},
        },
        binary_source={"graphviz-devel": "graphviz", "graphviz-devil": "graphviz"},
        source_names={"graphviz"},
    )
    repo.finalize_lookups()
    parsed = {
        "im": _parse_spec_text(
            "Name: im\nVersion: 1\nRelease: 1\nBuildRequires: pkgconfig(libcgraph)\nProvides: im\n",
            "im.spec",
        ),
    }
    # leaf-only. install holes on published BRs are force/dnf's job, not distro pathwalk.
    cands = _collect_repo_transitive_candidates(
        parsed,
        repo,
        scope_packages={"im"},
        distgit_providers={"libIL.so.1()(64bit)": {"DevIL"}},
    )
    assert "DevIL" not in cands
    assert "graphviz" not in cands
    assert repo.missing_caps_on_provider_path("pkgconfig(libcgraph)") == {"libIL.so.1()(64bit)"}


def test_satisfied_br_still_detects_missing_runtime_provider() -> None:
    from oreon_build.services.distgit_order import _collect_repo_transitive_candidates, _parse_spec_text

    repo = RepoCapabilityIndex(
        binary_names={"graphviz-devel", "graphviz-devil"},
        provides={"pkgconfig(libcgraph)", "graphviz-devel", "graphviz-devil"},
        capability_providers={
            "pkgconfig(libcgraph)": {"graphviz-devel"},
            "graphviz-devel": {"graphviz-devel"},
            "graphviz-devil": {"graphviz-devil"},
        },
        binary_requires={
            "graphviz-devel": {"graphviz-devil"},
            "graphviz-devil": {"libIL.so.1()(64bit)"},
        },
        binary_source={"graphviz-devel": "graphviz", "graphviz-devil": "graphviz"},
        source_names={"graphviz"},
    )
    repo.finalize_lookups()
    parsed = {
        "ImageMagick": _parse_spec_text(
            "Name: ImageMagick\nVersion: 1\nRelease: 1\nBuildRequires: pkgconfig(libcgraph)\nProvides: ImageMagick\n",
            "ImageMagick.spec",
        ),
    }
    cands = _collect_repo_transitive_candidates(
        parsed,
        repo,
        scope_packages={"ImageMagick"},
        distgit_providers={"libIL.so.1()(64bit)": {"DevIL"}},
    )
    assert "DevIL" not in cands
    assert "libIL.so.1()(64bit)" in repo.missing_caps_on_provider_path("pkgconfig(libcgraph)")


def test_path_scoped_broken_runtime_ignores_unrelated_consumers() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"pango", "python3.11-tkinter", "graphviz-devel", "graphviz-devil"},
        provides={"pkgconfig(pango)", "pkgconfig(libcgraph)", "pango", "graphviz-devel"},
        capability_providers={
            "pkgconfig(pango)": {"pango"},
            "pkgconfig(libcgraph)": {"graphviz-devel"},
            "pango": {"pango"},
            "graphviz-devel": {"graphviz-devel"},
            "graphviz-devil": {"graphviz-devil"},
        },
        binary_requires={
            "pango": {"libtcl8.6.so()(64bit)"},
            "python3.11-tkinter": {"libtcl8.6.so()(64bit)"},
            "graphviz-devel": {"graphviz-devil"},
            "graphviz-devil": {"libIL.so.1()(64bit)"},
        },
        binary_source={
            "pango": "pango",
            "python3.11-tkinter": "python3.11",
            "graphviz-devel": "graphviz",
            "graphviz-devil": "graphviz",
        },
        source_names={"pango", "python3.11", "graphviz"},
    )
    repo.finalize_lookups()
    assert "python3.11" not in repo.broken_runtime_sources_on_path("pkgconfig(pango)")
    assert "pango" in repo.broken_runtime_sources_on_path("pkgconfig(pango)")
    assert "graphviz" in repo.broken_runtime_sources_on_path("pkgconfig(libcgraph)")
    assert "python3.11" not in repo.broken_runtime_sources_on_path("pkgconfig(libcgraph)")


def test_file_path_requires_use_filelists_provides() -> None:
    from oreon_build.services.distgit_order import _collect_repo_transitive_candidates, _parse_spec_text

    repo = RepoCapabilityIndex(
        binary_names={"pango", "python3"},
        provides={"pkgconfig(pango)", "pango", "/usr/bin/python3"},
        capability_providers={
            "pkgconfig(pango)": {"pango"},
            "pango": {"pango"},
            "/usr/bin/python3": {"python3"},
        },
        binary_requires={
            "pango": {"/usr/bin/python3"},
        },
        binary_source={"pango": "pango", "python3": "python3"},
        source_names={"pango", "python3"},
    )
    repo.finalize_lookups()
    assert repo.satisfies_requirement("/usr/bin/python3")
    assert repo.broken_runtime_sources_on_path("pkgconfig(pango)") == set()
    parsed = {
        "app": _parse_spec_text(
            "Name: app\nVersion: 1\nRelease: 1\nBuildRequires: pkgconfig(pango)\nProvides: app\n",
            "app.spec",
        ),
    }
    cands = _collect_repo_transitive_candidates(parsed, repo, scope_packages={"app"})
    assert "python3" not in cands
    assert "pango" not in cands


def test_missing_file_path_marks_provider_source_broken() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"pango"},
        provides={"pkgconfig(pango)", "pango"},
        capability_providers={
            "pkgconfig(pango)": {"pango"},
            "pango": {"pango"},
        },
        binary_requires={
            "pango": {"/usr/bin/missing-tool"},
        },
        binary_source={"pango": "pango"},
        source_names={"pango"},
    )
    repo.finalize_lookups()
    assert not repo.satisfies_requirement("/usr/bin/missing-tool")
    assert "pango" in repo.broken_runtime_sources_on_path("pkgconfig(pango)")


def test_rpmlib_is_always_satisfied() -> None:
    repo = RepoCapabilityIndex()
    repo.finalize_lookups()
    assert repo.satisfies_requirement("rpmlib(CompressedFileNames) <= 3.0.4-1")


def test_virtual_caps_need_real_provides_not_source_name_aliases() -> None:
    """python3dist/crate installability is the virtual Provide, not a guessed src name."""
    repo = RepoCapabilityIndex(
        source_names={"python-requests", "python3", "rust-askalono", "rust"},
        binary_names={"python3", "rust"},
        provides={"python3", "rust"},
    )
    repo.finalize_lookups()
    assert not repo.satisfies_requirement("python3dist(requests)")
    assert not repo.satisfies_requirement("crate(askalono)")
    assert not repo.satisfies_requirement(
        "(crate(askalono/default) >= 0.5.0 with crate(askalono/default) < 0.6.0~)"
    )

    ok = RepoCapabilityIndex(
        provides={
            "python3dist(requests)",
            "crate(askalono)",
            "crate(askalono/default)",
        },
        binary_names={"python3-requests", "rust-askalono"},
        source_names={"python-requests", "rust-askalono"},
    )
    ok.finalize_lookups()
    assert ok.satisfies_requirement("python3dist(requests)")
    assert ok.satisfies_requirement("crate(askalono)")


def test_ingest_filelists_adds_path_provides() -> None:
    from oreon_build.services.repo_capability_index import _ingest_filelists_xml

    xml = b"""<?xml version="1.0"?>
<filelists packages="1">
  <package pkgid="abc" name="python3" arch="x86_64">
    <version epoch="0" ver="3.14" rel="1"/>
    <file>/usr/bin/python3</file>
    <file type="dir">/usr/lib</file>
  </package>
</filelists>"""
    idx = RepoCapabilityIndex()
    _ingest_filelists_xml(idx, xml)
    assert "/usr/bin/python3" in idx.provides
    assert "python3" in idx.capability_providers["/usr/bin/python3"]


def test_load_arch_metadata_skips_filelists_by_default(monkeypatch) -> None:
    from oreon_build.services import repo_capability_index as rci

    calls: list[str] = []

    class _R2:
        def get_object(self, key):
            if key.endswith("repomd.xml"):
                return b"<repomd/>"
            return None

    monkeypatch.setattr(rci, "get_r2_client", lambda: _R2())
    monkeypatch.setattr(rci, "_resolve_repodata_key", lambda *a, **k: "primary.xml.gz")
    monkeypatch.setattr(
        rci,
        "_fetch_repodata_blob",
        lambda *a, **k: calls.append("fetch") or b"<metadata/>",
    )
    monkeypatch.setattr(rci, "_decompress_if_needed", lambda raw, key: raw)
    monkeypatch.setattr(
        rci,
        "_ingest_primary_xml",
        lambda idx, xml: idx.provides.add("pkgconfig(foo)"),
    )

    def _boom(*a, **k):
        raise AssertionError("filelists must not be ingested by default")

    monkeypatch.setattr(rci, "_ingest_filelists_xml", _boom)
    monkeypatch.setattr(rci, "_INCLUDE_FILELISTS", False)
    idx = rci._load_arch_metadata("oreon-11-rp1", "testing", "x86_64")
    assert "pkgconfig(foo)" in idx.provides


def test_collect_ignores_satisfied_caps() -> None:
    from oreon_build.services.distgit_order import _collect_repo_transitive_candidates, _parse_spec_text

    repo = RepoCapabilityIndex(
        source_names={"gcc", "make", "glibc"},
        binary_names={"gcc", "make"},
        provides={"gcc", "make", "glibc"},
    )
    repo.finalize_lookups()
    parsed = {
        "im": _parse_spec_text(
            "Name: im\nVersion: 1\nRelease: 1\n"
            "BuildRequires: gcc\nBuildRequires: make\nBuildRequires: totally-fake-pkg\n"
            "Provides: im\n",
            "im.spec",
        ),
    }
    cands = _collect_repo_transitive_candidates(parsed, repo)
    assert "gcc" not in cands
    assert "make" not in cands
    assert "totally-fake-pkg" not in cands


def test_ingest_primary_xml_populates_names() -> None:
    xml = b"""<?xml version="1.0"?>
<metadata>
  <package type="rpm">
    <name>macros-pkg</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="11" rel="3.or11"/>
    <format>
      <rpm:sourcerpm xmlns:rpm="http://linux.duke.edu/metadata/rpm">macros-pkg-11-3.or11.src.rpm</rpm:sourcerpm>
      <rpm:provides xmlns:rpm="http://linux.duke.edu/metadata/rpm">
        <rpm:entry name="macros-pkg"/>
      </rpm:provides>
    </format>
  </package>
</metadata>"""
    idx = RepoCapabilityIndex()
    _ingest_primary_xml(idx, xml)
    assert "macros-pkg" in idx.binary_names
    assert "macros-pkg" in idx.source_names


def test_multi_arch_satisfies_source_any_arch_for_exclusive() -> None:
    x86 = RepoCapabilityIndex(
        binary_names={"sgx-enclave-prebuilt-common"},
        provides={"sgx-enclave-prebuilt-common"},
        capability_providers={"sgx-enclave-prebuilt-common": {"sgx-enclave-prebuilt-common"}},
        binary_source={"sgx-enclave-prebuilt-common": "linux-sgx-enclaves-prebuilt"},
        source_names={"linux-sgx-enclaves-prebuilt", "kernel"},
    )
    aarch = RepoCapabilityIndex(
        binary_names={"kernel"},
        provides={"kernel"},
        capability_providers={"kernel": {"kernel"}},
        binary_source={"kernel": "kernel"},
        source_names={"kernel"},
    )
    merged = _merge_arch_indexes([x86, aarch])
    assert merged.satisfies_requirement("sgx-enclave-prebuilt-common") is False
    assert merged.satisfies_source("linux-sgx-enclaves-prebuilt") is True
    assert merged.satisfies_source("kernel") is True
    assert "linux-sgx-enclaves-prebuilt" in merged.source_names_any
    assert "linux-sgx-enclaves-prebuilt" not in merged.source_names


def test_multi_arch_merge_requires_capability_on_every_arch() -> None:
    x86 = RepoCapabilityIndex(
        binary_names={"common", "x86-only"},
        provides={"common", "x86-only", "dbus-c++-devel"},
        capability_providers={"dbus-c++-devel": {"dbus-c++-devel"}},
        binary_source={"dbus-c++-devel": "dbus-c++"},
        source_names={"common-src", "dbus-c++"},
    )
    aarch = RepoCapabilityIndex(
        binary_names={"common", "aarch-only", "dbus-c++-devel"},
        provides={"common", "aarch-only", "dbus-c++-devel"},
        capability_providers={"dbus-c++-devel": {"dbus-c++-devel"}},
        binary_source={"dbus-c++-devel": "dbus-c++"},
        source_names={"common-src", "dbus-c++"},
    )
    merged = _merge_arch_indexes([x86, aarch])
    assert merged.satisfies_requirement("common") is True
    assert merged.satisfies_requirement("x86-only") is False
    assert merged.satisfies_requirement("aarch-only") is False
    # present on aarch only in this fixture's binary_names intersection sense:
    # x86 listed it in provides but not binary_names consistently --- force the real case
    x862 = RepoCapabilityIndex(
        binary_names={"common"},
        provides={"common"},
        capability_providers={},
        binary_source={},
        source_names={"common-src"},
    )
    aarch2 = RepoCapabilityIndex(
        binary_names={"common", "dbus-c++-devel"},
        provides={"common", "dbus-c++-devel"},
        capability_providers={"dbus-c++-devel": {"dbus-c++-devel"}},
        binary_source={"dbus-c++-devel": "dbus-c++"},
        source_names={"common-src", "dbus-c++"},
    )
    merged2 = _merge_arch_indexes([x862, aarch2])
    assert merged2.satisfies_requirement("dbus-c++-devel") is False
    assert merged2.distgit_sources_for_capability("dbus-c++-devel") == {"dbus-c++"}


def test_arch_partial_subpackage_maps_to_built_source() -> None:
    # libquadmath-devel only on x86, gcc source already published on both
    x86 = RepoCapabilityIndex(
        binary_names={"gcc", "libquadmath-devel"},
        provides={"gcc", "libquadmath-devel"},
        capability_providers={
            "gcc": {"gcc"},
            "libquadmath-devel": {"libquadmath-devel"},
        },
        binary_source={"gcc": "gcc", "libquadmath-devel": "gcc"},
        binary_requires={"libquadmath-devel": {"libquadmath", "glibc-devel"}},
        source_names={"gcc"},
        capability_evrs={"gcc": [("0", "16", "1")], "libquadmath-devel": [("0", "16", "1")]},
    )
    aarch = RepoCapabilityIndex(
        binary_names={"gcc"},
        provides={"gcc"},
        capability_providers={"gcc": {"gcc"}},
        binary_source={"gcc": "gcc"},
        source_names={"gcc"},
        capability_evrs={"gcc": [("0", "16", "1")]},
    )
    merged = _merge_arch_indexes([x86, aarch])
    assert merged.satisfies_requirement("libquadmath-devel") is False
    assert merged.has_capability_any_arch("libquadmath-devel") is True
    assert merged.distgit_sources_for_capability("libquadmath-devel") == {"gcc"}
    from oreon_build.core.rpm_requires import sources_for_requirement

    assert sources_for_requirement("libquadmath-devel", merged) == {"gcc"}
    miss = merged.transitive_unsatisfied("libquadmath-devel")
    assert "libquadmath-devel" in miss
    assert "libquadmath" in miss or "glibc-devel" in miss


def test_rich_or_dep_uses_arch_unqualified_leaves() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"llvm-libs"},
        provides={"llvm-libs"},
        source_names={"llvm"},
    )
    repo.finalize_lookups()
    assert repo.satisfies_requirement("(llvm-libs(x86-64) or llvm22-libs(x86-64))") is True
    assert repo.satisfies_requirement("(llvm-libs(aarch-64) or llvm22-libs(aarch-64))") is True
    assert repo.rich_dependency_missing_leaves("(llvm-libs(x86-64) or llvm22-libs(x86-64))") == []


def test_rich_if_dep_vacuous_when_condition_absent() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"mesa-dri-drivers"},
        provides={"mesa-dri-drivers"},
        source_names={"mesa"},
    )
    repo.finalize_lookups()
    assert repo.satisfies_requirement("(sdubby > 1.0-3 if sdubby)") is True
    assert repo.rich_dependency_missing_leaves("(sdubby > 1.0-3 if sdubby)") == []
    assert (
        repo.satisfies_requirement(
            "(mesa-dri-drivers(x86-64) = 26.0.2-4.or11 if mesa-dri-drivers(x86-64))"
        )
        is True
    )


def test_rich_or_dep_reports_actionable_leaves_when_missing() -> None:
    repo = RepoCapabilityIndex()
    repo.finalize_lookups()
    miss = repo.rich_dependency_missing_leaves("(foo-libs(x86-64) or bar-libs(x86-64))")
    assert miss == ["foo-libs", "bar-libs"]
    assert repo.satisfies_requirement("(foo-libs(x86-64) or bar-libs(x86-64))") is False


def test_rich_leaf_parser_drops_version_fragments() -> None:
    from oreon_build.services.repo_capability_index import _rich_dep_leaf_names

    assert _rich_dep_leaf_names("(sdubby > 1.0-3 if sdubby)") == ["sdubby", "sdubby"]
    assert "26.0.2-4.or11" not in _rich_dep_leaf_names(
        "(mesa-dri-drivers(x86-64) = 26.0.2-4.or11 if mesa-dri-drivers(x86-64))"
    )
    assert _rich_dep_leaf_names(
        "(mesa-dri-drivers(x86-64) = 26.0.2-4.or11 if mesa-dri-drivers(x86-64))"
    ) == ["mesa-dri-drivers(x86-64)", "mesa-dri-drivers(x86-64)"]


def test_iter_plan_capabilities_expands_unsatisfied_rich_not_whole_string() -> None:
    from oreon_build.core.spec_bcond import parse_spec_bconds
    from oreon_build.services.distgit_order import _parse_spec_text, iter_plan_capabilities

    repo = RepoCapabilityIndex(
        binary_names={"llvm-libs", "mesa-dri-drivers"},
        provides={"llvm-libs", "mesa-dri-drivers"},
    )
    repo.finalize_lookups()
    parsed = _parse_spec_text(
        "Name: plasma-desktop\nVersion: 1\nRelease: 1\n"
        "Requires: (llvm-libs(x86-64) or llvm22-libs(x86-64))\n"
        "Requires: (sdubby > 1.0-3 if sdubby)\n"
        "Requires: (mesa-dri-drivers(x86-64) = 1 if mesa-dri-drivers(x86-64))\n"
        "BuildRequires: scim-devel\n"
        "Provides: plasma-desktop\n",
        "plasma-desktop.spec",
    )
    bcond = parse_spec_bconds(
        "Name: plasma-desktop\nVersion: 1\nRelease: 1\n"
        "Requires: (llvm-libs(x86-64) or llvm22-libs(x86-64))\n"
        "Requires: (sdubby > 1.0-3 if sdubby)\n"
        "Requires: (mesa-dri-drivers(x86-64) = 1 if mesa-dri-drivers(x86-64))\n"
        "BuildRequires: scim-devel\n"
        "Provides: plasma-desktop\n",
    )
    caps = iter_plan_capabilities(parsed, bcond, repo)
    assert "scim-devel" in caps
    assert not any(c.startswith("(") for c in caps)
    assert "sdubby" not in caps
    assert "llvm-libs" not in caps


def test_satisfies_requirement_is_case_sensitive_like_dnf() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"packagekit-qt6-devel"},
        provides={"packagekit-qt6-devel", "pkgconfig(packagekitqt6)"},
        capability_providers={"packagekit-qt6-devel": {"packagekit-qt6-devel"}},
        binary_source={"packagekit-qt6-devel": "packagekit-qt"},
        source_names={"packagekit-qt"},
    )
    repo.finalize_lookups()
    assert repo.satisfies_requirement("packagekit-qt6-devel") is True
    assert repo.satisfies_requirement("PackageKit-Qt6-devel") is False
    assert repo.has_capability_ci("PackageKit-Qt6-devel") is True


def test_case_insensitive_capability_present() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"packagekit-qt6-devel"},
        provides={"packagekit-qt6-devel"},
        capability_providers={"packagekit-qt6-devel": {"packagekit-qt6-devel"}},
        binary_source={"packagekit-qt6-devel": "packagekit-qt"},
        source_names={"packagekit-qt"},
    )
    repo.finalize_lookups()
    assert repo.has_capability_ci("PackageKit-Qt6-devel") is True
    assert repo.satisfies_requirement("packagekit-qt6-devel") is True


def test_repo_channels_from_mock_urls() -> None:
    from oreon_build.services.repo_capability_index import (
        default_capability_extra_branches,
        repo_channels_from_mock_urls,
    )

    urls = [
        "https://repo-us.oreonhq.com/oreon-11-rp1/testing/$basearch",
        "https://repo-us.oreonhq.com/oreon-11-rp1/stable/x86_64",
        "https://example.com/other-rel/testing/$basearch",
    ]
    assert repo_channels_from_mock_urls("oreon-11-rp1", urls) == ["testing", "stable"]
    assert repo_channels_from_mock_urls("missing", urls) == []
    assert default_capability_extra_branches("dev") == ["testing", "stable"]
    assert default_capability_extra_branches("testing") == ["dev", "stable"]


def test_union_indexes_merges_channel_provides() -> None:
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex, _union_indexes

    dest = RepoCapabilityIndex(
        binary_names={"foo"},
        provides={"foo"},
        capability_providers={"foo": {"foo"}},
        source_names={"foo"},
    )
    testing = RepoCapabilityIndex(
        binary_names={"python3-devel"},
        provides={"python3-devel", "pkgconfig(libsystemd)"},
        capability_providers={
            "python3-devel": {"python3-devel"},
            "pkgconfig(libsystemd)": {"systemd-devel"},
        },
        binary_source={"python3-devel": "python3.14", "systemd-devel": "systemd"},
        source_names={"python3.14", "systemd"},
    )
    merged = _union_indexes([dest, testing])
    merged.finalize_lookups()
    assert merged.has_capability("foo")
    assert merged.has_capability("python3-devel")
    assert merged.has_capability("pkgconfig(libsystemd)")
    assert merged.satisfies_requirement("python3-devel")


def test_satisfies_requirement_checks_provide_evr() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"wayland-protocols-devel"},
        provides={"wayland-protocols-devel"},
        capability_providers={"wayland-protocols-devel": {"wayland-protocols-devel"}},
        capability_evrs={"wayland-protocols-devel": [("0", "1.47", "1.or11")]},
        binary_source={"wayland-protocols-devel": "wayland-protocols"},
        source_names={"wayland-protocols"},
    )
    repo.finalize_lookups()
    assert repo.satisfies_requirement("wayland-protocols-devel")
    assert repo.satisfies_requirement("wayland-protocols-devel >= 1.47")
    assert not repo.satisfies_requirement("wayland-protocols-devel >= 1.48")
    assert not repo.satisfies_requirement("wayland-protocols-devel > 1.47")


def test_rich_crate_with_version_bounds_not_satisfied_by_old_evr() -> None:
    """0.10 must not satisfy (crate(env_logger) >= 0.11 with < 0.12)."""
    repo = RepoCapabilityIndex(
        binary_names={"rust-env_logger0.10"},
        provides={"crate(env_logger)", "crate(env_logger/default)", "crate(env_logger/regex)"},
        capability_providers={
            "crate(env_logger)": {"rust-env_logger0.10"},
            "crate(env_logger/default)": {"rust-env_logger0.10"},
            "crate(env_logger/regex)": {"rust-env_logger0.10"},
        },
        capability_evrs={
            "crate(env_logger)": [("0", "0.10.2", "")],
            "crate(env_logger/default)": [("0", "0.10.2", "")],
            "crate(env_logger/regex)": [("0", "0.10.2", "")],
        },
        binary_source={"rust-env_logger0.10": "rust-env_logger0.10"},
        source_names={"rust-env_logger0.10"},
    )
    repo.finalize_lookups()
    assert repo.has_capability("crate(env_logger)")
    assert repo.satisfies_requirement("crate(env_logger)")
    assert not repo.satisfies_requirement("crate(env_logger) >= 0.11.0")
    assert not repo.satisfies_requirement(
        "(crate(env_logger) >= 0.11.0 with crate(env_logger) < 0.12.0~)"
    )
    assert not repo.satisfies_requirement(
        "(crate(env_logger/default) >= 0.11.0 with crate(env_logger/default) < 0.12.0~)"
    )


def test_source_is_repo_current_presence_and_evr() -> None:
    repo = RepoCapabilityIndex(
        binary_names={"rust-clap"},
        provides={"rust-clap", "crate(clap)"},
        capability_providers={"crate(clap)": {"rust-clap"}, "rust-clap": {"rust-clap"}},
        capability_evrs={
            "rust-clap": [("0", "4.5.0", "1.or11")],
            "crate(clap)": [("0", "4.5.0", "")],
        },
        binary_source={"rust-clap": "rust-clap"},
        source_names={"rust-clap"},
    )
    repo.finalize_lookups()
    assert repo.source_present_in_repo("rust-clap")
    assert repo.source_is_repo_current("rust-clap")
    assert not repo.source_is_repo_current("rust-clap", require_evr=True)
    assert repo.source_is_repo_current("rust-clap", 0, "4.5.0", "1.or11", require_evr=True)
    assert repo.source_is_repo_current("rust-clap", 0, "4.4.0", "1.or11", require_evr=True)
    assert not repo.source_is_repo_current("rust-clap", 0, "4.6.0", "1.or11", require_evr=True)
    assert not repo.source_is_repo_current("rust-missing", 0, "1.0.0", "1")
