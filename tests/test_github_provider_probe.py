from __future__ import annotations

import pytest

from oreon_build.services.distgit_order import (
    DistgitProvidesIndex,
    _GITHUB_SEARCH_CACHE,
    _PROVIDES_INDEX_CACHE,
    _github_search_distgit_providers,
    _lightweight_provides_from_spec_text,
    _provides_match_capability,
    _search_queries_for_capability,
    _warm_provides_index_lookup,
)


def test_lightweight_package_devel_and_pc() -> None:
    spec = """
Name: libxml++
%global api_ver 2.6
%package devel
%package doc
%files devel
%{_libdir}/pkgconfig/%{name}-%{api_ver}.pc
"""
    provided = _lightweight_provides_from_spec_text("libxml++", spec)
    assert "libxml++-devel" in provided
    assert "libxml++-doc" in provided
    assert "pkgconfig(libxml++-2.6)" in provided
    assert "pkgconfig(*)" not in provided


def test_perl_package_name_maps_to_virtual_provide() -> None:
    from oreon_build.services.distgit_order import (
        _perl_provide_from_package_name,
        distgit_providers_dict,
    )

    assert _perl_provide_from_package_name("perl-Any-URI-Escape") == "perl(Any::URI::Escape)"
    assert _perl_provide_from_package_name("perl-URI") == "perl(URI)"
    assert _perl_provide_from_package_name("perl-interpreter") is None
    assert _perl_provide_from_package_name("perl-URI-tests") is None

    spec = "Name: perl-Any-URI-Escape\nVersion: 0.01\n%files\n%{perl_vendorlib}/Any*\n"
    provided = _lightweight_provides_from_spec_text("perl-Any-URI-Escape", spec)
    assert "perl(Any::URI::Escape)" in provided
    assert _provides_match_capability("perl(Any::URI::Escape)", provided)

    idx = DistgitProvidesIndex(
        package_names=frozenset({"perl-Any-URI-Escape"}),
        capability_providers={},
    )
    assert "perl-Any-URI-Escape" in distgit_providers_dict(idx).get("perl(Any::URI::Escape)", set())


def test_rust_package_name_maps_to_crate_provide() -> None:
    from oreon_build.services.distgit_order import (
        _crate_provides_from_package_name,
        _rust_crate_name_from_package,
        distgit_providers_dict,
    )

    assert _crate_provides_from_package_name("rust-clap") == {"crate(clap)"}
    assert _crate_provides_from_package_name("rust-serde_json") == {"crate(serde_json)"}
    assert _crate_provides_from_package_name("rust-askalono") == {"crate(askalono)"}
    assert _crate_provides_from_package_name("rust-packaging") == set()
    assert _crate_provides_from_package_name("rust-clap+default-devel") == set()

    assert _rust_crate_name_from_package("rust-itertools0.13", "0.13.0") == "itertools"
    assert _rust_crate_name_from_package("rust-nom7", "7.1.3") == "nom"
    assert _rust_crate_name_from_package("rust-rstest0.11", "0.11.0") == "rstest"
    assert _rust_crate_name_from_package("rust-rand_pcg0.2", "0.2.1") == "rand_pcg"
    assert _rust_crate_name_from_package("rust-sha1", "0.10.1") == "sha1"
    assert _crate_provides_from_package_name("rust-itertools0.13", version="0.13.0") == {
        "crate(itertools)"
    }
    assert _crate_provides_from_package_name("rust-nom7", version="7.1.3") == {"crate(nom)"}
    # no Version: still strip 0.x compat dirs
    assert _crate_provides_from_package_name("rust-itertools0.13") == {"crate(itertools)"}

    idx = DistgitProvidesIndex(
        package_names=frozenset(
            {"rust-clap", "rust-serde_json", "rust-rayon", "rust-ignore", "rust-askalono"}
        ),
        capability_providers={},
    )
    mapped = distgit_providers_dict(idx)
    assert mapped.get("crate(clap)") == {"rust-clap"}
    assert mapped.get("crate(serde_json)") == {"rust-serde_json"}
    assert mapped.get("crate(askalono)") == {"rust-askalono"}


def test_lightweight_rust_compat_pkg_uses_global_crate() -> None:
    spec = """
%global crate itertools
Name: rust-itertools0.13
Version: 0.13.0
Release: 1
%package devel
%description
x
%files
"""
    provided = _lightweight_provides_from_spec_text("rust-itertools0.13", spec)
    assert "crate(itertools)" in provided
    assert "crate(itertools0.13)" not in provided

    nom = """
%global crate nom
Name: rust-nom7
Version: 7.1.3
Release: 1
%description
x
%files
"""
    provided2 = _lightweight_provides_from_spec_text("rust-nom7", nom)
    assert "crate(nom)" in provided2
    assert "crate(nom7)" not in provided2


def test_lightweight_bcond_compat_pkg_name() -> None:
    # llvm15-style: %bcond_without compat_build → Name becomes llvm15 not llvm
    spec = """
%bcond_without compat_build
%global maj_ver 15
%if %{with compat_build}
%global pkg_name llvm%{maj_ver}
%else
%global pkg_name llvm
%endif
Name:           %{pkg_name}
Version:        15.0.7
%package static
%package cmake-utils
%description
x
%files
"""
    provided = _lightweight_provides_from_spec_text("llvm15", spec)
    assert "llvm15" in provided
    assert "llvm15-static" in provided
    assert "llvm15-cmake-utils" in provided
    assert "llvm-static" not in provided


def test_lightweight_soname_from_files() -> None:
    spec = """
Name: DevIL
%files
%{_libdir}/libIL.so.*
%{_libdir}/libILU.so.*
"""
    provided = _lightweight_provides_from_spec_text("DevIL", spec)
    assert _provides_match_capability("libIL.so.1()(64bit)", provided)
    assert _provides_match_capability("libILU.so.1()(64bit)", provided)


def test_files_section_keeps_if_and_license() -> None:
    spec = """
Name: qt5-qtbase
%package devel
%package private-devel
%files devel
%license LICENSE.FDL
%if "%{_qt5_bindir}" != "%{_bindir}"
%{_qt5_bindir}/qmake-qt5
%endif
%{_qt5_libdir}/pkgconfig/Qt5Core.pc
%{_qt5_libdir}/pkgconfig/Qt5Gui.pc
%files private-devel
%{_qt5_libdir}/pkgconfig/Qt5OpenGLExtensions.pc
"""
    provided = _lightweight_provides_from_spec_text("qt5-qtbase", spec)
    assert "pkgconfig(Qt5Core)" in provided
    assert "pkgconfig(Qt5Gui)" in provided
    assert "pkgconfig(Qt5OpenGLExtensions)" in provided
    assert "qt5-qtbase-private-devel" in provided


def test_pkgconfig_glob_match_from_files() -> None:
    provided = {"pkgconfig(Qt5Core)", "pkgconfig(Qt5Gui)", "qt5-qtbase"}
    assert _provides_match_capability("pkgconfig(Qt5Core)", provided)
    assert not _provides_match_capability("pkgconfig(Qt5Core)", {"pkgconfig(*)", "OpenIPMI"})
    assert _provides_match_capability(
        "pkgconfig(Qt5*)",
        {"pkgconfig(Qt5Core)", "pkgconfig(Qt5Gui)", "qt5-qtbase"},
    )
    assert _provides_match_capability(
        "pkgconfig(absl_test_instance_tracker)",
        {"pkgconfig(absl_*)"},
    )


def test_lightweight_bindir_glob_keeps_pattern() -> None:
    spec = """
Name: python-sphinx
%files -n python3-sphinx
%{_bindir}/sphinx-*
"""
    provided = _lightweight_provides_from_spec_text("python-sphinx", spec)
    assert "__glob__/usr/bin/sphinx-*" in provided
    assert "/usr/bin/sphinx" in provided


def test_lightweight_pkgconfig_prefix_glob() -> None:
    spec = """
Name: abseil-cpp
%files devel
%{_libdir}/pkgconfig/absl_*.pc
"""
    provided = _lightweight_provides_from_spec_text("abseil-cpp", spec)
    assert "pkgconfig(absl_*)" in provided
    assert "pkgconfig(absl_)" not in provided
    spec_bare = """
Name: libcaca
%files devel
%{_libdir}/pkgconfig/*.pc
"""
    bare = _lightweight_provides_from_spec_text("libcaca", spec_bare)
    assert "pkgconfig(*)" not in bare
    assert not any(c.startswith("pkgconfig(") for c in bare)


def test_lightweight_python_pkgversion_package() -> None:
    spec = """
Name: python-qt5
%package -n python%{python3_pkgversion}-qt5-devel
"""
    provided = _lightweight_provides_from_spec_text("python-qt5", spec)
    assert "python3-qt5-devel" in provided


def test_lightweight_bindir_and_python_dist_invent() -> None:
    node_spec = """
Name: nodejs22
Provides: npm
%files
%{_bindir}/node
%{_bindir}/npm
"""
    provided = _lightweight_provides_from_spec_text("nodejs22", node_spec)
    assert "/usr/bin/npm" in provided
    assert "/usr/bin/node" in provided
    assert "npm" in provided
    assert "node" in provided

    py_spec = """
Name: python-setuptools
%package -n python3-setuptools
"""
    py_prov = _lightweight_provides_from_spec_text("python-setuptools", py_spec)
    assert "python3dist(setuptools)" in py_prov
    idx = DistgitProvidesIndex(
        package_names=frozenset(["python-setuptools", "nodejs22"]),
        capability_providers={
            "/usr/bin/npm": frozenset(["nodejs22"]),
            "npm": frozenset(["nodejs22"]),
            "python3-setuptools": frozenset(["python-setuptools"]),
        },
    )
    mapped = idx.indexed_providers()
    assert "nodejs22" in mapped.get("/usr/bin/npm", set()) | mapped.get("__lower__", {}).get(
        "/usr/bin/npm".lower(), set()
    )
    from oreon_build.core.rpm_requires import _providers_from_maps

    assert _providers_from_maps("python3dist(setuptools)", [mapped], exact=True) == {
        "python-setuptools"
    }
    assert _providers_from_maps("/usr/bin/npm", [mapped], exact=True) == {"nodejs22"}


def test_search_queries_are_literal_provides() -> None:
    qs = _search_queries_for_capability("oreonhq", "rpm-specfiles", "oreon-11-rp1", "pkgconfig(Qt5Core)")
    assert any("Provides: pkgconfig(Qt5Core)" in q for q in qs)
    assert any("Qt5Core.pc" in q for q in qs)
    assert not any("qt5-qtbase" in q for q in qs)


def test_warm_index_lookup_uses_provides_map() -> None:
    _PROVIDES_INDEX_CACHE.clear()
    index = DistgitProvidesIndex(
        package_names=frozenset(["libxml++", "qt5-qtbase", "DevIL"]),
        capability_providers={
            "libxml++-devel": frozenset(["libxml++"]),
            "pkgconfig(Qt5Core)": frozenset(["qt5-qtbase"]),
            "pkgconfig(*)": frozenset(["OpenIPMI"]),
            "libIL.so": frozenset(["DevIL"]),
        },
    )
    import time

    _PROVIDES_INDEX_CACHE[("o", "r", "b", "oreon-11-rp1")] = (time.monotonic(), index)
    assert _warm_provides_index_lookup("libxml++-devel") == {"libxml++"}
    assert _warm_provides_index_lookup("pkgconfig(Qt5Core)") == {"qt5-qtbase"}
    assert _warm_provides_index_lookup("libIL.so.1()(64bit)") == {"DevIL"}
    _PROVIDES_INDEX_CACHE.clear()


@pytest.mark.asyncio
async def test_github_resolve_uses_warm_index_not_name_guessing() -> None:
    _GITHUB_SEARCH_CACHE.clear()
    _PROVIDES_INDEX_CACHE.clear()
    import time

    index = DistgitProvidesIndex(
        package_names=frozenset(["libxml++"]),
        capability_providers={"libxml++-devel": frozenset(["libxml++"])},
    )
    _PROVIDES_INDEX_CACHE[("oreonhq", "rpm-specfiles", "dev/pre-beta-1", "oreon-11-rp1")] = (
        time.monotonic(),
        index,
    )

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            raise AssertionError(f"should not hit network when index has the cap: {url}")

    srcs = await _github_search_distgit_providers(
        FakeClient(),
        owner="oreonhq",
        repo="rpm-specfiles",
        releasename="oreon-11-rp1",
        capability="libxml++-devel",
        headers={},
        distgit_branch="dev/pre-beta-1",
    )
    assert srcs == {"libxml++"}
    _PROVIDES_INDEX_CACHE.clear()


@pytest.mark.asyncio
async def test_github_warm_index_miss_skips_network() -> None:
    _GITHUB_SEARCH_CACHE.clear()
    _PROVIDES_INDEX_CACHE.clear()
    import time

    index = DistgitProvidesIndex(
        package_names=frozenset(["libxml++"]),
        capability_providers={"libxml++-devel": frozenset(["libxml++"])},
    )
    _PROVIDES_INDEX_CACHE[("oreonhq", "rpm-specfiles", "dev/pre-beta-1", "oreon-11-rp1")] = (
        time.monotonic(),
        index,
    )

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            raise AssertionError(f"warm index miss must not search github: {url}")

    srcs = await _github_search_distgit_providers(
        FakeClient(),
        owner="oreonhq",
        repo="rpm-specfiles",
        releasename="oreon-11-rp1",
        capability="gtkmm24-devel",
        headers={},
        distgit_branch="dev/pre-beta-1",
    )
    assert srcs == set()
    _PROVIDES_INDEX_CACHE.clear()
    _GITHUB_SEARCH_CACHE.clear()


@pytest.mark.asyncio
async def test_github_search_verifies_hit_spec() -> None:
    _GITHUB_SEARCH_CACHE.clear()
    _PROVIDES_INDEX_CACHE.clear()

    class FakeResp:
        def __init__(self, status_code: int, payload=None, text: str = ""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            if "search/code" in url:
                return FakeResp(
                    200,
                    {
                        "items": [
                            {"path": "oreon-11-rp1/libxml++/libxml++.spec"},
                            {"path": "oreon-11-rp1/libffado/libffado.spec"},
                        ]
                    },
                )
            if "libxml++/libxml++.spec" in url:
                return FakeResp(200, text="Name: libxml++\n%package devel\n%files devel\n")
            if "libffado/libffado.spec" in url:
                return FakeResp(
                    200,
                    text="Name: libffado\nBuildRequires: libxml++-devel\n",
                )
            return FakeResp(404, {"message": "Not Found"})

    srcs = await _github_search_distgit_providers(
        FakeClient(),
        owner="oreonhq",
        repo="rpm-specfiles",
        releasename="oreon-11-rp1",
        capability="libxml++-devel",
        headers={},
        distgit_branch="dev/pre-beta-1",
    )
    assert srcs == {"libxml++"}


def test_lightweight_perl_module_from_file_path() -> None:
    spec = """
Name: perl-MIME-Entity
%files
%{_datadir}/perl5/MIME/Entity.pm
"""
    provided = _lightweight_provides_from_spec_text("perl-MIME-Entity", spec)
    assert "perl(MIME::Entity)" in provided


def test_lightweight_bindir_glob_stored_for_later_matching() -> None:
    spec = """
Name: binutils
%files
%{_bindir}/[!l]*
"""
    provided = _lightweight_provides_from_spec_text("binutils", spec)
    assert "__glob__/usr/bin/[!l]*" in provided


def test_lightweight_ghc_lib_subpackage_vendored_lib() -> None:
    spec = """
Name: ghc-vector-algorithms
%global bitvec bitvec-1.1.5.0
%global subpkgs %{bitvec}
%package -n ghc-vector-algorithms-devel
%package -n ghc-vector-algorithms-doc
%package -n ghc-vector-algorithms-prof
%prep
%build
%ghc_lib_subpackage -l BSD-3-Clause %{bitvec}
%files
"""
    provided = _lightweight_provides_from_spec_text("ghc-vector-algorithms", spec)
    assert "ghc-libhs(bitvec)" in provided


def test_lightweight_cmake_module_from_file_path() -> None:
    spec = """
Name: libbson
%files
%{_libdir}/cmake/bson-1.0/bson-targets.cmake
"""
    provided = _lightweight_provides_from_spec_text("libbson", spec)
    assert "cmake(bson-1.0)" in provided


def test_lightweight_ecm_config_cmake_provide():
    spec = """
Name: extra-cmake-modules
%files
%{_datadir}/ECM/cmake/ECMConfig.cmake
%{_datadir}/ECM/cmake/ECMConfigVersion.cmake
"""
    provided = _lightweight_provides_from_spec_text("extra-cmake-modules", spec)
    assert "cmake(ECM)" in provided
    assert "cmake(ECMConfig.cmake)" not in provided


def test_lightweight_perl_nested_module_from_file_path() -> None:
    spec = """
Name: perl-Log-Report-Minimal
%files
%{_datadir}/perl5/Log/Report/Minimal/Domain.pm
"""
    provided = _lightweight_provides_from_spec_text("perl-Log-Report-Minimal", spec)
    assert "perl(Log::Report::Minimal::Domain)" in provided


def test_lightweight_nested_version_macro_cmake_and_mingw_dll() -> None:
    mongo = """
%global up_version 2.3.1
Name: mongo-c-driver
Version: %{up_version}%{?up_prever:~%{up_prever}}
%files -n libbson-devel
%{_libdir}/cmake/bson-%{version}
"""
    provided = _lightweight_provides_from_spec_text("mongo-c-driver", mongo)
    assert "cmake(bson-2.3.1)" in provided
    assert "cmake(bson-)" not in provided

    mingw = """
Name: mingw-hunspell
%files -n mingw32-hunspell
%{mingw32_bindir}/libhunspell-1.7-0.dll
%files -n mingw64-hunspell
%{mingw64_bindir}/libhunspell-1.7-0.dll
"""
    provided = _lightweight_provides_from_spec_text("mingw-hunspell", mingw)
    assert "mingw32(libhunspell-1.7-0.dll)" in provided
    assert "mingw64(libhunspell-1.7-0.dll)" in provided


def test_lightweight_mingw_sysroot_does_not_emit_host_cmake_or_pkgconfig() -> None:
    mingw = """
Name: mingw-qt6-qtbase
Version: 6.7.4
%files -n mingw64-qt6-qtbase
%{mingw64_libdir}/cmake/Qt6Core/Qt6CoreConfig.cmake
%{mingw64_libdir}/pkgconfig/Qt6Core.pc
%{mingw64_bindir}/libQt6Core.dll
"""
    provided = _lightweight_provides_from_spec_text("mingw-qt6-qtbase", mingw)
    assert "cmake(Qt6Core)" not in provided
    assert "pkgconfig(Qt6Core)" not in provided
    assert "mingw64(libQt6Core.dll)" in provided


def test_perl_metacpan_dist_maps_unprefixed_dpkg() -> None:
    from oreon_build.services.perl_resolve import distgit_sources_for_perl_module

    assert distgit_sources_for_perl_module("Dpkg::Version", {"dpkg"}) == {"dpkg"}
    assert distgit_sources_for_perl_module(
        "CPAN::Meta::Requirements::Range",
        {"perl-CPAN", "perl-CPAN-Meta", "perl-CPAN-Meta-Requirements"},
    ) == {"perl-CPAN-Meta-Requirements"}


def test_lightweight_goipath_global_maps_to_golang_provide() -> None:
    spec = """
%global goipath github.com/briandowns/spinner
Name: golang-github-briandowns-spinner
Version: 1.23.2
%gometa -f
%files
"""
    provided = _lightweight_provides_from_spec_text("golang-github-briandowns-spinner", spec)
    assert "golang(github.com/briandowns/spinner)" in provided


def test_lightweight_versioned_soname_from_files_macro_expand() -> None:
    spec = """
Name: boost
%files python3
%{_libdir}/libboost_python314.so.1.83.0
"""
    provided = _lightweight_provides_from_spec_text("boost", spec)
    assert "libboost_python314.so.1.83.0" in provided


def test_lightweight_bare_star_soname_uses_package_name() -> None:
    spec = """
Name: libcerf
%global sover 3
%files
%{_libdir}/*.so.%{sover}*
"""
    provided = _lightweight_provides_from_spec_text("libcerf", spec)
    assert "libcerf.so.3" in provided


def test_local_source_negative_cache_skips_repeat_fetch() -> None:
    """A permanently-404 patch must not be re-hit on every fixed-point wave."""
    import time as time_mod

    from oreon_build.services import distgit_order as do

    url = "https://raw.githubusercontent.com/x/y/z/rel/pkg/never-exists.patch"
    do._LOCAL_SOURCE_FAIL_CACHE.pop(url, None)
    try:
        assert not do._local_source_fail_cached(url)
        do._local_source_mark_failed(url)
        assert do._local_source_fail_cached(url)
        # expired entries dont block forever
        do._LOCAL_SOURCE_FAIL_CACHE[url] = time_mod.monotonic() - do._LOCAL_SOURCE_FAIL_TTL_SEC - 1
        assert not do._local_source_fail_cached(url)
    finally:
        do._LOCAL_SOURCE_FAIL_CACHE.pop(url, None)
