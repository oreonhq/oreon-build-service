from __future__ import annotations

from oreon_build.services.virtual_resolve import (
    distgit_sources_for_virtual_cap,
    import_suggestion_for_cap,
)


def test_font_binaries_map_to_parent_font_src() -> None:
    known = {"amiri-fonts", "liberation-fonts", "liberation-narrow-fonts"}
    assert distgit_sources_for_virtual_cap(
        "amiri-quran-fonts", package_names=known
    ) == {"amiri-fonts"}
    assert distgit_sources_for_virtual_cap(
        "liberation-mono-fonts", package_names=known
    ) == {"liberation-fonts"}


def test_pkgconfig_versioned_and_tcmalloc() -> None:
    known = {"liborcus", "gperftools"}
    pmap = {"pkgconfig(liborcus-0.21)": {"liborcus"}}
    assert "liborcus" in distgit_sources_for_virtual_cap(
        "pkgconfig(liborcus-0.20)", package_names=known, provider_map=pmap
    )
    assert "gperftools" in distgit_sources_for_virtual_cap(
        "pkgconfig(libtcmalloc)", package_names=known, provider_map={}
    )


def test_python3dist_casefold() -> None:
    known = {"python-CommonMark"}
    pmap = {"python3dist(CommonMark)": {"python-CommonMark"}}
    assert distgit_sources_for_virtual_cap(
        "python3dist(commonmark)", package_names=known, provider_map=pmap
    ) == {"python-CommonMark"}


def test_mvn_mingw_ghc_tex() -> None:
    known = {
        "jakarta-inject",
        "maven-surefire",
        "mingw-gettext",
        "mingw-gmp",
        "ghc-th-compat",
    }
    assert "jakarta-inject" in distgit_sources_for_virtual_cap(
        "mvn(javax.inject:javax.inject)", package_names=known
    )
    assert "maven-surefire" in distgit_sources_for_virtual_cap(
        "mvn(org.apache.maven.plugins:maven-surefire-plugin)", package_names=known
    )
    assert "mingw-gettext" in distgit_sources_for_virtual_cap(
        "mingw32(libintl-8.dll)", package_names=known
    )
    assert "ghc-th-compat" in distgit_sources_for_virtual_cap(
        "ghc-devel(th-compat-0.1.7-G2CcjlEe1U5tMXwVbi6pm)", package_names=known
    )
    assert import_suggestion_for_cap("tex(a4wide.sty)") == "texlive-a4wide"


def test_mono_xbuild_and_devel_subpkg() -> None:
    known = {"mono", "hal"}
    assert distgit_sources_for_virtual_cap(
        "mono(xbuild)", package_names=known, provider_map={}
    ) == {"mono"}
    assert distgit_sources_for_virtual_cap(
        "hal-devel", package_names=known, provider_map={}
    ) == {"hal"}
    assert import_suggestion_for_cap("hal-devel") == "hal"


def test_mvn_gav_version_not_suggested_as_package() -> None:
    # group:artifact:version must use artifact, never the version token
    assert import_suggestion_for_cap("mvn(org.foo:commons-lang3:4.0.2)") == "commons-lang3"
    assert import_suggestion_for_cap("mvn(org.foo:bar:4.0.2)") == "bar"
    assert import_suggestion_for_cap("mvn(org.foo:4.0.2)") is None
    known = {"commons-lang3"}
    assert "commons-lang3" in distgit_sources_for_virtual_cap(
        "mvn(org.apache.commons:commons-lang3:4.0.2)",
        package_names=known,
        provider_map={},
    )


def test_tex_leaf_maps_via_collection_subpackage() -> None:
    pmap = {
        "texlive-a4wide": {"texlive-collection-latexextra"},
        "texlive-tabulary": {"texlive-collection-latexextra"},
    }
    known = {"texlive-collection-latexextra"}
    assert distgit_sources_for_virtual_cap(
        "tex(a4wide.sty)", package_names=known, provider_map=pmap
    ) == {"texlive-collection-latexextra"}
    assert distgit_sources_for_virtual_cap(
        "tex(tabulary.sty)", package_names=known, provider_map=pmap
    ) == {"texlive-collection-latexextra"}
