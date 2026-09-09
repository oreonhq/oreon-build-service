from __future__ import annotations

from oreon_build.services.virtual_resolve import (
    distgit_sources_for_virtual_cap,
    import_suggestion_for_cap,
)


def test_font_resolves_from_provider_data_only() -> None:
    # font tokens are squashed upstream names. without a declared font() provide
    # we refuse to invent a *-fonts package from a substring match.
    known = {"amiri-fonts", "liberation-fonts", "wqy-zenhei-fonts"}
    assert distgit_sources_for_virtual_cap(
        "amiri-quran-fonts", package_names=known, provider_map={}
    ) == set()
    assert distgit_sources_for_virtual_cap(
        "font(wenquanyizenhei)", package_names=known, provider_map={}
    ) == set()
    assert distgit_sources_for_virtual_cap(
        "font(wenquanyizenhei)",
        package_names=known,
        provider_map={"font(wenquanyizenhei)": {"wqy-zenhei-fonts"}},
    ) == {"wqy-zenhei-fonts"}
    assert distgit_sources_for_virtual_cap(
        "wqy-zenhei-fonts", package_names=known, provider_map={}
    ) == {"wqy-zenhei-fonts"}


def test_pkgconfig_versioned_from_provider_data() -> None:
    known = {"liborcus", "gperftools"}
    pmap = {"pkgconfig(liborcus-0.21)": {"liborcus"}}
    assert "liborcus" in distgit_sources_for_virtual_cap(
        "pkgconfig(liborcus-0.20)", package_names=known, provider_map=pmap
    )
    # no alias table. without a declared provide, pkgconfig(libtcmalloc) stays empty
    assert distgit_sources_for_virtual_cap(
        "pkgconfig(libtcmalloc)", package_names=known, provider_map={}
    ) == set()
    assert distgit_sources_for_virtual_cap(
        "pkgconfig(libtcmalloc)",
        package_names=known,
        provider_map={"pkgconfig(libtcmalloc)": {"gperftools"}},
    ) == {"gperftools"}


def test_python3dist_casefold() -> None:
    known = {"python-CommonMark"}
    pmap = {"python3dist(CommonMark)": {"python-CommonMark"}}
    assert distgit_sources_for_virtual_cap(
        "python3dist(commonmark)", package_names=known, provider_map=pmap
    ) == {"python-CommonMark"}


def test_mvn_ghc_tex_from_format_and_data() -> None:
    known = {"commons-lang3", "ghc-th-compat", "jna"}
    # artifact id is the rpm name for jpackage specs, no javax→jakarta inventing
    assert "commons-lang3" in distgit_sources_for_virtual_cap(
        "mvn(org.apache.commons:commons-lang3:4.0.2)", package_names=known
    )
    assert distgit_sources_for_virtual_cap(
        "mvn(javax.inject:javax.inject)", package_names=known, provider_map={}
    ) == set()
    assert "jna" in distgit_sources_for_virtual_cap(
        "mvn(net.java.dev.jna:jna)",
        package_names=known,
        provider_map={"mvn(net.java.dev.jna:jna)": {"jna"}},
    )
    assert "ghc-th-compat" in distgit_sources_for_virtual_cap(
        "ghc-devel(th-compat-0.1.7-G2CcjlEe1U5tMXwVbi6pm)", package_names=known
    )
    assert import_suggestion_for_cap("tex(a4wide.sty)") == "texlive-a4wide"


def test_mingw_dll_resolves_from_provider_data() -> None:
    # mingw DLL provides are registered by the distgit indexer from the specs.
    # no hardcoded dll→leaf map.
    known = {"mingw-gettext", "mingw-hunspell"}
    assert distgit_sources_for_virtual_cap(
        "mingw32(libintl-8.dll)", package_names=known, provider_map={}
    ) == set()
    assert distgit_sources_for_virtual_cap(
        "mingw32(libintl-8.dll)",
        package_names=known,
        provider_map={"mingw32(libintl-8.dll)": {"mingw-gettext"}},
    ) == {"mingw-gettext"}
    assert distgit_sources_for_virtual_cap(
        "mingw64(libhunspell-1.7-0.dll)",
        package_names=known,
        provider_map={"mingw64(libhunspell-1.7-0.dll)": {"mingw-hunspell"}},
    ) == {"mingw-hunspell"}


def test_subpkg_resolves_from_provider_data() -> None:
    # binary subpackages map through the provides index, not suffix stripping
    known = {"mono", "hal", "llvm15", "llvm18"}
    assert distgit_sources_for_virtual_cap(
        "mono(xbuild)", package_names=known, provider_map={}
    ) == set()
    assert distgit_sources_for_virtual_cap(
        "mono(xbuild)",
        package_names=known,
        provider_map={"mono(xbuild)": {"mono"}},
    ) == {"mono"}
    assert distgit_sources_for_virtual_cap(
        "hal-devel", package_names=known, provider_map={}
    ) == set()
    assert distgit_sources_for_virtual_cap(
        "hal-devel",
        package_names=known,
        provider_map={"hal-devel": {"hal"}},
    ) == {"hal"}
    assert import_suggestion_for_cap("hal-devel") == "hal"
    assert distgit_sources_for_virtual_cap(
        "llvm15-static = 15.0.7",
        package_names=known,
        provider_map={"llvm15-static": {"llvm15"}},
    ) == {"llvm15"}
    assert distgit_sources_for_virtual_cap(
        "llvm18-cmake-utils = 18.1.8",
        package_names=known,
        provider_map={"llvm18-cmake-utils": {"llvm18"}},
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
    assert distgit_sources_for_virtual_cap(
        "python3-matplotlib",
        package_names={"blosc", "python-matplotlib"},
        provider_map={},
    ) == {"python-matplotlib"}
    assert "python3" not in distgit_sources_for_virtual_cap(
        "python3dist(missingpkg)", package_names=known, provider_map={}
    )
    assert distgit_sources_for_virtual_cap(
        "python3-requests",
        package_names=known,
        provider_map={"python3-requests": {"python-requests"}},
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


def test_caps_resolve_from_provider_data() -> None:
    known = {
        "python-sphinx",
        "fcitx5",
        "mingw-hunspell",
        "wqy-zenhei-fonts",
        "python-dask",
        "python-versioneer",
        "python-snappy",
        "jupyterlab",
        "python-pytest-xdist",
        "ghc-crypton",
        "ghc-iproute",
    }
    provider_map = {
        "/usr/bin/sphinx-build": {"python-sphinx"},
        "pkgconfig(Fcitx5Core)": {"fcitx5"},
        "pkgconfig(Fcitx5Module)": {"fcitx5"},
        "mingw32(libhunspell-1.7-0.dll)": {"mingw-hunspell"},
        "mingw64(libhunspell-1.7-0.dll)": {"mingw-hunspell"},
        "font(wenquanyizenhei)": {"wqy-zenhei-fonts"},
        "python3-pytest-xdist+psutil": {"python-pytest-xdist"},
        "ghc-iproute-prof": {"ghc-iproute"},
    }
    cases = {
        "/usr/bin/sphinx-build": "python-sphinx",
        "pkgconfig(Fcitx5Core)": "fcitx5",
        "pkgconfig(Fcitx5Module)": "fcitx5",
        "mingw32(libhunspell-1.7-0.dll)": "mingw-hunspell",
        "mingw64(libhunspell-1.7-0.dll)": "mingw-hunspell",
        "font(wenquanyizenhei)": "wqy-zenhei-fonts",
        "python3dist(dask[array])": "python-dask",
        "python3dist(versioneer[toml])": "python-versioneer",
        "python3dist(python-snappy)": "python-snappy",
        "python3dist(jupyterlab)": "jupyterlab",
        "python3-pytest-xdist+psutil": "python-pytest-xdist",
        "ghc-prof(crypton-1.0.5-8aTdADO6LUoABPMdaJy4Z7)": "ghc-crypton",
        "ghc-iproute-prof": "ghc-iproute",
        "(python3dist(jupyterlab) < 4.6~~ with python3dist(jupyterlab) >= 4.5.3)": "jupyterlab",
    }
    for cap, src in cases.items():
        hits = distgit_sources_for_virtual_cap(
            cap, package_names=known, provider_map=provider_map
        )
        assert src in hits, f"{cap} -> {hits}, expected {src}"


def test_cmake_versioned_module_resolves_from_provider_data() -> None:
    # config module ships a versioned provide (cmake(bson-2.3.1)) while the
    # consumer asks for the bare cmake(bson). resolution is data-driven off the
    # provider map, no hardcoded package names.
    known = {"mongo-c-driver"}
    provider_map = {
        "cmake(bson-2.3.1)": {"mongo-c-driver"},
        "libbson": {"mongo-c-driver"},
    }
    for cap in ("cmake(bson)", "cmake(bson-1.0)", "cmake(bson-1.0) >= 1.11"):
        hits = distgit_sources_for_virtual_cap(
            cap, package_names=known, provider_map=provider_map
        )
        assert "mongo-c-driver" in hits, f"{cap} -> {hits}"


def test_cmake_module_maps_to_lowercase_distgit_source() -> None:
    known = {"kquickimageeditor", "opencv"}
    provider_map = {
        "cmake(KQuickImageEditor)": {"kquickimageeditor"},
        "cmake(OpenCV)": {"opencv"},
    }
    assert "kquickimageeditor" in distgit_sources_for_virtual_cap(
        "cmake(KQuickImageEditor)", package_names=known, provider_map=provider_map
    )
    assert "opencv" in distgit_sources_for_virtual_cap(
        "cmake(OpenCV)", package_names=known, provider_map=provider_map
    )
    assert "kquickimageeditor" in distgit_sources_for_virtual_cap(
        "cmake(KQuickImageEditor)", package_names=known, provider_map={}
    )


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
    # selective globs with real literal text in the basename are trusted.
    # directory-ownership patterns like %{_bindir}/[!l]* are rejected separately.
    provider_map = {"__glob__/usr/bin/obj*": {"binutils"}}
    hits = distgit_sources_for_virtual_cap(
        "/usr/bin/objcopy", package_names=set(), provider_map=provider_map
    )
    assert "binutils" in hits
    no_hits = distgit_sources_for_virtual_cap(
        "/usr/bin/as", package_names=set(), provider_map=provider_map
    )
    assert "binutils" not in no_hits


def test_directory_ownership_globs_are_ignored() -> None:
    # %{_bindir}/* and %{_bindir}/[a-wyz]* own a directory, not a file.
    # trusting them invents providers for every binary anyone asks for.
    provider_map = {
        "__glob__/usr/bin/*": {"everything"},
        "__glob__/usr/bin/[a-wyz]*": {"qdbm"},
        "__glob__/usr/bin/[!l]*": {"binutils"},
        "__glob__/usr/bin/qemu-*": {"qemu"},
    }
    hits = distgit_sources_for_virtual_cap(
        "/usr/bin/qemu-img", package_names=set(), provider_map=provider_map
    )
    assert hits == {"qemu"}
    assert distgit_sources_for_virtual_cap(
        "/usr/bin/objcopy", package_names=set(), provider_map=provider_map
    ) == set()


def test_devel_subpkg_resolves_via_declared_base() -> None:
    # wxGTK renamed itself and keeps wxGTK3 as a plain Provides, so wxGTK3-devel
    # is a split subpackage of wxGTK even though wxGTK3 is not a source.
    known = {"wxGTK", "openjpeg"}
    pmap = {"wxGTK3": {"wxGTK"}, "wxGTK-devel": {"wxGTK"}}
    assert distgit_sources_for_virtual_cap(
        "wxGTK3-devel", package_names=known, provider_map=pmap
    ) == {"wxGTK"}
    assert distgit_sources_for_virtual_cap(
        "libavcodec-free-devel = 8.1-5.or11",
        package_names={"ffmpeg"},
        provider_map={"libavcodec-free": {"ffmpeg"}},
    ) == {"ffmpeg"}
    # nothing declares the base, so nothing is invented
    assert distgit_sources_for_virtual_cap(
        "nosuchthing-devel", package_names=known, provider_map=pmap
    ) == set()


def test_devel_subpkg_skips_ambiguous_and_real_names() -> None:
    # clang is declared by several versioned compiler specs, picking one spawns junk
    pmap = {"clang": {"clang15", "clang17", "castxml"}, "yelp-tools": {"yelp-tools"}}
    assert distgit_sources_for_virtual_cap(
        "clang-devel", package_names={"clang15", "clang17"}, provider_map=pmap
    ) == set()
    # -tools/-doc are whole package names, never stripped
    assert distgit_sources_for_virtual_cap(
        "yelp-tools", package_names={"yelp", "yelp-tools"}, provider_map=pmap
    ) == {"yelp-tools"}


def test_ghc_devel_hash_uses_libhs_when_lib_not_imported() -> None:
    pmap = {"ghc-libhs(integer-conversion)": {"ghc-attoparsec-iso8601"}}
    assert distgit_sources_for_virtual_cap(
        "ghc-devel(integer-conversion-0.1.1-deadbeef)",
        package_names={"ghc-attoparsec-iso8601"},
        provider_map=pmap,
    ) == {"ghc-attoparsec-iso8601"}


def test_ghc_devel_prefers_dedicated_source() -> None:
    pmap = {"ghc-libhs(integer-conversion)": {"ghc-attoparsec-iso8601"}}
    assert distgit_sources_for_virtual_cap(
        "ghc-devel(integer-conversion-0.1.1-deadbeef)",
        package_names={"ghc-integer-conversion", "ghc-attoparsec-iso8601"},
        provider_map=pmap,
    ) == {"ghc-integer-conversion"}


def test_soname_cap_maps_from_provider_data() -> None:
    pmap = {
        "libavformat.so.62()(64bit)": {"ffmpeg"},
        "libavformat.so.62": {"ffmpeg"},
    }
    assert distgit_sources_for_virtual_cap(
        "libavformat.so.62()(64bit)",
        package_names={"ffmpeg"},
        provider_map=pmap,
    ) == {"ffmpeg"}


def test_pkgconfig_prefix_glob_from_files_map() -> None:
    from oreon_build.core.rpm_requires import index_provider_map

    pmap = index_provider_map({"pkgconfig(absl_*)": {"abseil-cpp"}})
    assert "abseil-cpp" in distgit_sources_for_virtual_cap(
        "pkgconfig(absl_test_instance_tracker)",
        package_names={"abseil-cpp", "ffmpeg"},
        provider_map=pmap,
    )


def test_bindir_glob_maps_sphinx_build() -> None:
    pmap = {"__glob__/usr/bin/sphinx-*": {"python-sphinx"}}
    assert "python-sphinx" in distgit_sources_for_virtual_cap(
        "/usr/bin/sphinx-build",
        package_names={"python-sphinx", "cmake"},
        provider_map=pmap,
    )
