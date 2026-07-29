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
