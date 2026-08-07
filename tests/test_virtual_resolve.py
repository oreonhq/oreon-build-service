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
    known = {"mono", "hal", "llvm15", "llvm18"}
    assert distgit_sources_for_virtual_cap(
        "mono(xbuild)", package_names=known, provider_map={}
    ) == {"mono"}
    assert distgit_sources_for_virtual_cap(
        "hal-devel", package_names=known, provider_map={}
    ) == {"hal"}
    assert import_suggestion_for_cap("hal-devel") == "hal"
    assert distgit_sources_for_virtual_cap(
        "llvm15-static = 15.0.7", package_names=known, provider_map={}
    ) == {"llvm15"}
    assert distgit_sources_for_virtual_cap(
        "llvm18-cmake-utils = 18.1.8", package_names=known, provider_map={}
    ) == {"llvm18"}


def test_crate_maps_to_rust_package_not_compiler() -> None:
    known = {"rust", "rust-askalono", "rust-rayon", "rust-rustix0.37"}
    rich = "(crate(askalono/default) >= 0.5.0 with crate(askalono/default) < 0.6.0~)"
    got = distgit_sources_for_virtual_cap(rich, package_names=known, provider_map={})
    assert got == {"rust-askalono"}
    assert "rust" not in got
    assert distgit_sources_for_virtual_cap(
        "crate(rayon/default)", package_names=known, provider_map={}
    ) == {"rust-rayon"}
    assert distgit_sources_for_virtual_cap(
        "crate(rustix)", package_names=known, provider_map={}
    ) == {"rust-rustix0.37"}
    # missing crate must not invent the compiler package
    assert distgit_sources_for_virtual_cap(
        "crate(difference)", package_names={"rust"}, provider_map={}
    ) == set()
    assert import_suggestion_for_cap(rich) == "rust-askalono"


def test_python_and_golang_do_not_collapse_to_runtime() -> None:
    known = {
        "python3",
        "python",
        "python-requests",
        "golang",
        "go",
        "golang-github-spf13-cobra",
    }
    assert distgit_sources_for_virtual_cap(
        "python3dist(requests)", package_names=known, provider_map={}
    ) == {"python-requests"}
    assert "python3" not in distgit_sources_for_virtual_cap(
        "python3dist(missingpkg)", package_names=known, provider_map={}
    )
    assert distgit_sources_for_virtual_cap(
        "python3-requests", package_names=known, provider_map={}
    ) == {"python-requests"}
    assert distgit_sources_for_virtual_cap(
        "golang-github-spf13-cobra", package_names=known, provider_map={}
    ) == {"golang-github-spf13-cobra"}
    assert import_suggestion_for_cap("python3dist(requests)") == "python-requests"
    assert (
        import_suggestion_for_cap("golang(github.com/spf13/cobra)")
        == "golang-github-spf13-cobra"
    )


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


def test_toast_false_imports_map_to_existing_sources() -> None:
    known = {
        "python-sphinx",
        "dpkg",
        "mongo-c-driver",
        "fcitx5",
        "mingw-hunspell",
        "wqy-zenhei-fonts",
        "python-wxpython4",
        "python-dask",
        "python-versioneer",
        "python-snappy",
        "python-pywt",
        "jupyterlab",
        "python-pytest-xdist",
        "ghc-crypton",
        "ghc-iproute",
    }
    cases = {
        "/usr/bin/sphinx-build": "python-sphinx",
        "pkgconfig(Fcitx5Core)": "fcitx5",
        "pkgconfig(Fcitx5Module)": "fcitx5",
        "mingw32(libhunspell-1.7-0.dll)": "mingw-hunspell",
        "mingw64(libhunspell-1.7-0.dll)": "mingw-hunspell",
        "font(wenquanyizenhei)": "wqy-zenhei-fonts",
        "python3dist(wxpython)": "python-wxpython4",
        "python3dist(dask[array])": "python-dask",
        "python3dist(versioneer[toml])": "python-versioneer",
        "python3dist(python-snappy)": "python-snappy",
        "python3dist(pywavelets)": "python-pywt",
        "python3dist(jupyterlab)": "jupyterlab",
        "python3-pytest-xdist+psutil": "python-pytest-xdist",
        "cmake(bson-1.0)": "mongo-c-driver",
        "cmake(bson-1.0) >= 1.11": "mongo-c-driver",
        "ghc-prof(crypton-1.0.5-8aTdADO6LUoABPMdaJy4Z7)": "ghc-crypton",
        "ghc-iproute-prof": "ghc-iproute",
        "(python3dist(jupyterlab) < 4.6~~ with python3dist(jupyterlab) >= 4.5.3)": "jupyterlab",
    }
    for cap, src in cases.items():
        hits = distgit_sources_for_virtual_cap(cap, package_names=known, provider_map={})
        assert src in hits, f"{cap} -> {hits}, expected {src}"


def test_mozjs_and_ghc_libhs_map_to_existing_sources() -> None:
    known = {
        "mozjs140",
        "ghc-base-orphans",
        "ghc-prettyprinter",
        "ghc-vector-algorithms",
        "ghc-bitvec",
    }
    cases = {
        "pkgconfig(mozjs-140)": "mozjs140",
        "libHSbase-orphans-0.9.4-7BfHuMWFviR1vB5XQuLgjl-ghc9.10.3.so()(64bit)": "ghc-base-orphans",
        "libHSprettyprinter-1.7.1-K35zp2YrYoHG4xnuHsN7jV-ghc9.10.3.so()(64bit)": "ghc-prettyprinter",
    }
    for cap, src in cases.items():
        hits = distgit_sources_for_virtual_cap(cap, package_names=known, provider_map={})
        assert src in hits, f"{cap} -> {hits}, expected {src}"

    # bitvec is vendored inside ghc-vector-algorithms, not its own package -
    # this must resolve to the vendoring package via the ghc-libhs() marker
    # registered from its %ghc_lib_subpackage invocation, not suggest an import.
    hits = distgit_sources_for_virtual_cap(
        "libHSbitvec-1.1.5.0-AhS0HXjz7497U87uwYveTR-ghc9.10.3.so()(64bit)",
        package_names=known,
        provider_map={"ghc-libhs(bitvec)": {"ghc-vector-algorithms"}},
    )
    assert "ghc-vector-algorithms" in hits


def test_golang_submodule_path_resolves_via_goipath_prefix() -> None:
    from oreon_build.services.virtual_resolve import golang_path_to_sources

    pmap = {
        "golang(github.com/aws/aws-sdk-go-v2)": {"golang-github-aws-sdk-2"},
        "__goprefix__github.com/aws/aws-sdk-go-v2": {"golang-github-aws-sdk-2"},
        "golang(github.com/aws/aws-lambda-go)": {"golang-github-aws-lambda"},
        "__goprefix__github.com/aws/aws-lambda-go": {"golang-github-aws-lambda"},
    }
    assert golang_path_to_sources(
        "golang(github.com/aws/aws-sdk-go-v2/service/cloudwatch/types)", pmap
    ) == {"golang-github-aws-sdk-2"}
    assert golang_path_to_sources("golang(github.com/aws/aws-lambda-go)", pmap) == {
        "golang-github-aws-lambda"
    }
    assert golang_path_to_sources("golang(github.com/unrelated/pkg)", pmap) == set()


def test_guile_pkgconfig_maps_to_concatenated_version_source() -> None:
    known = {"guile30", "guile30-devel"}
    hits = distgit_sources_for_virtual_cap(
        "pkgconfig(guile-3.0)", package_names=known, provider_map={}
    )
    assert "guile30" in hits


def test_bindir_glob_provide_matches_real_glob_semantics() -> None:
    # binutils ships %{_bindir}/[!l]* instead of listing objcopy literally.
    # the requirement must be matched against the real glob, not a name table.
    provider_map = {"__glob__/usr/bin/[!l]*": {"binutils"}}
    hits = distgit_sources_for_virtual_cap(
        "/usr/bin/objcopy", package_names=set(), provider_map=provider_map
    )
    assert "binutils" in hits
    # sanity: a name starting with the excluded letter must not match
    no_hits = distgit_sources_for_virtual_cap(
        "/usr/bin/ld.gold", package_names=set(), provider_map=provider_map
    )
    assert "binutils" not in no_hits
