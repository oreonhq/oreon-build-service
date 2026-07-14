from __future__ import annotations

from oreon_build.core.rpm_requires import (
    requirement_check_capabilities,
    rpm_requirement_capabilities,
    rpm_requirement_capabilities_from_value,
)


def test_pkgconfig_expands_inner_name() -> None:
    caps = rpm_requirement_capabilities("pkgconfig(libcgraph)")
    assert caps == ["pkgconfig(libcgraph)", "libcgraph"]


def test_virtual_caps_do_not_expand_inners() -> None:
    assert rpm_requirement_capabilities("python3dist(setuptools)") == ["python3dist(setuptools)"]
    assert rpm_requirement_capabilities("rpm_macro(py3_shebang_fix)") == [
        "rpm_macro(py3_shebang_fix)"
    ]


def test_file_path_capability() -> None:
    caps = rpm_requirement_capabilities("/usr/bin/node")
    assert caps == ["/usr/bin/node"]


def test_requirement_check_aliases_path_and_py_dist() -> None:
    path_caps = requirement_check_capabilities("/usr/bin/npm")
    assert "/usr/bin/npm" in path_caps
    assert "npm" in path_caps
    py_caps = requirement_check_capabilities("python3dist(setuptools)")
    assert "python3dist(setuptools)" in py_caps
    assert "python3-setuptools" in py_caps
    assert "python-setuptools" in py_caps


def test_sources_resolve_path_via_basename_provide() -> None:
    from oreon_build.core.rpm_requires import sources_for_requirement
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex

    empty = RepoCapabilityIndex()
    empty.finalize_lookups()
    distgit = {
        "/usr/bin/npm": {"nodejs22"},
        "npm": {"nodejs22", "nodejs24"},
        "python3dist(setuptools)": {"python-setuptools"},
        "python3-setuptools": {"python-setuptools"},
    }
    assert sources_for_requirement("/usr/bin/npm", empty, provider_maps=[distgit]) >= {"nodejs22"}
    assert "python-setuptools" in sources_for_requirement(
        "python3dist(setuptools)", empty, provider_maps=[distgit]
    )


def test_version_trimmed() -> None:
    caps = rpm_requirement_capabilities("glibc >= 2.38")
    assert caps == ["glibc"]


def test_rpmlib_skipped() -> None:
    assert rpm_requirement_capabilities("rpmlib(CompressedFileNames)") == []


def test_space_separated_brs() -> None:
    caps = rpm_requirement_capabilities_from_value("gcc make pkgconfig")
    assert caps == ["gcc", "make", "pkgconfig"]


def test_maven_coord_aliases() -> None:
    caps = requirement_check_capabilities("junit:junit")
    assert "junit:junit" in caps
    assert "mvn(junit:junit)" in caps


def test_versioned_stays_one_token() -> None:
    caps = rpm_requirement_capabilities_from_value("glibc >= 2.38")
    assert caps == ["glibc"]


def test_space_separated_brs_with_trailing_version() -> None:
    from oreon_build.core.rpm_requires import (
        _requirement_chunks,
        rpm_requirement_primary_capabilities_from_value,
    )

    chunks = _requirement_chunks("libSM-devel imsettings-devel >= 1.8.9")
    assert chunks == ["libSM-devel", "imsettings-devel >= 1.8.9"]
    assert rpm_requirement_primary_capabilities_from_value(
        "libSM-devel imsettings-devel >= 1.8.9"
    ) == ["libSM-devel", "imsettings-devel"]
    assert rpm_requirement_primary_capabilities_from_value(
        "desktop-file-utils gettext"
    ) == ["desktop-file-utils", "gettext"]


def test_is_distgit_source_name_rejects_sonames() -> None:
    from oreon_build.core.rpm_requires import is_distgit_source_name

    assert is_distgit_source_name("DevIL") is True
    assert is_distgit_source_name("libIL.so.1()(64bit)") is False
    assert is_distgit_source_name("/usr/bin/node") is False


def test_chain_plan_requirement_filters_rpm_primary_noise() -> None:
    from oreon_build.core.rpm_requires import (
        is_arch_qualified_rpm_dependency,
        is_chain_plan_requirement,
        is_low_level_runtime_requirement,
        rpm_arch_qualified_base,
    )

    assert is_chain_plan_requirement("timidity++-patches") is True
    assert is_chain_plan_requirement("allegro-devel") is True
    assert is_chain_plan_requirement("libxcrypt-devel(x86-64)") is False
    assert is_chain_plan_requirement("libc.so.6(GLIBC_2.14)(64bit)") is False
    assert is_chain_plan_requirement("ld-linux-x86-64.so.2()(64bit)") is False
    assert is_chain_plan_requirement("(llvm-libs(x86-64) or llvm22-libs(x86-64))") is False
    assert is_chain_plan_requirement("(sdubby > 1.0-3 if sdubby)") is False
    assert is_chain_plan_requirement("--modversion") is False
    assert is_chain_plan_requirement("-n") is False
    assert is_chain_plan_requirement("echo") is False
    assert is_chain_plan_requirement("bad") is False
    assert is_chain_plan_requirement("0/pppd/patchlevel.h") is False
    assert is_chain_plan_requirement("'s/^#define'") is False
    assert is_chain_plan_requirement("/usr/bin/pkg-config") is True
    assert is_chain_plan_requirement("python3-devel") is True
    assert is_arch_qualified_rpm_dependency("allegro(x86-64)") is True
    assert rpm_arch_qualified_base("allegro(x86-64)") == "allegro"
    assert is_low_level_runtime_requirement("libm.so.6(GLIBC_2.2.5)(64bit)") is True


def test_sources_for_requirement_from_spec_providers() -> None:
    from oreon_build.core.rpm_requires import sources_for_requirement
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex

    repo = RepoCapabilityIndex(
        binary_names={"libXtst-devel"},
        provides={"pkgconfig(xtst)", "libXtst-devel"},
        capability_providers={"pkgconfig(xtst)": {"libXtst-devel"}, "libXtst-devel": {"libXtst-devel"}},
        binary_source={"libXtst-devel": "libXtst"},
        source_names={"libXtst"},
    )
    repo.finalize_lookups()
    assert sources_for_requirement("pkgconfig(xtst)", repo) == set()

    empty = RepoCapabilityIndex()
    empty.finalize_lookups()
    spec_providers = {"allegro-devel": {"allegro"}, "allegro": {"allegro"}}
    assert sources_for_requirement("pkgconfig(xtst)", empty, provider_maps=[spec_providers]) == set()
    assert sources_for_requirement("allegro-devel", empty, provider_maps=[spec_providers]) == {"allegro"}


def test_sources_for_unmet_cap_keeps_already_built_mapped_provider() -> None:
    from oreon_build.core.rpm_requires import sources_for_requirement
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex

    repo = RepoCapabilityIndex(
        binary_names={"perl-CPAN-Meta-Requirements", "perl"},
        provides={"perl(CPAN::Meta::Requirements)", "perl-CPAN-Meta-Requirements"},
        capability_providers={
            "perl(CPAN::Meta::Requirements)": {"perl-CPAN-Meta-Requirements"},
            "perl-CPAN-Meta-Requirements": {"perl-CPAN-Meta-Requirements"},
        },
        binary_source={"perl-CPAN-Meta-Requirements": "perl"},
        source_names={"perl"},
    )
    repo.finalize_lookups()
    assert not repo.satisfies_requirement("perl(CPAN::Meta::Requirements::Range)")
    distgit = {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}}
    got = sources_for_requirement(
        "perl(CPAN::Meta::Requirements::Range)",
        repo,
        provider_maps=[distgit],
    )
    assert "perl-CPAN-Meta-Requirements" in got
    # sourcerpm remap to meta "perl" must not poison the rebuild target
    assert "perl" not in got


def test_force_rebuild_when_published_provider_is_uninstallable() -> None:
    from oreon_build.services.build_planner import _provider_rebuild_force_sources
    from oreon_build.services.distgit_order import _parse_spec_text
    from oreon_build.services.repo_capability_index import RepoCapabilityIndex

    repo = RepoCapabilityIndex(
        binary_names={"perl-Module-Build", "perl-CPAN-Meta", "perl-CPAN-Meta-Requirements"},
        provides={
            "perl(Module::Build)",
            "perl-Module-Build",
            "perl(CPAN::Meta)",
            "perl(CPAN::Meta::Merge)",
            "perl(CPAN::Meta::Requirements)",
            "perl-CPAN-Meta-Requirements",
        },
        capability_providers={
            "perl(Module::Build)": {"perl-Module-Build"},
            "perl(CPAN::Meta)": {"perl-CPAN-Meta"},
            "perl(CPAN::Meta::Merge)": {"perl-CPAN-Meta"},
            "perl(CPAN::Meta::Requirements)": {"perl-CPAN-Meta-Requirements"},
        },
        binary_requires={
            "perl-Module-Build": {"perl(CPAN::Meta)", "perl(CPAN::Meta::Merge)"},
            "perl-CPAN-Meta": {"perl(CPAN::Meta::Requirements)"},
            "perl-CPAN-Meta-Requirements": {"perl(CPAN::Meta::Requirements::Range)"},
        },
        binary_source={
            "perl-Module-Build": "perl-Module-Build",
            "perl-CPAN-Meta": "perl",
            "perl-CPAN-Meta-Requirements": "perl",
        },
        source_names={"perl-Module-Build", "perl", "perl-CPAN-Meta-Requirements"},
    )
    repo.finalize_lookups()
    assert repo.satisfies_requirement("perl(Module::Build)")
    assert "perl(CPAN::Meta::Requirements::Range)" in repo.missing_caps_on_provider_path(
        "perl(Module::Build)"
    )

    spec = (
        "Name: app\nVersion: 1\nRelease: 1\n"
        "BuildRequires: perl(Module::Build)\n"
        "Provides: app\n"
    )
    parsed = {"app": _parse_spec_text(spec, "app.spec")}
    distgit = {"perl(CPAN::Meta::Requirements::Range)": {"perl-CPAN-Meta-Requirements"}}
    force, _ok, _bad, _fetch = _provider_rebuild_force_sources(
        {"app"},
        parsed,
        {"app": spec},
        repo,
        distgit,
    )
    assert "perl-CPAN-Meta-Requirements" in force
    assert "perl" not in force


def test_indexed_provider_lookup_is_case_insensitive_and_fast() -> None:
    from oreon_build.core.rpm_requires import (
        _providers_from_maps,
        index_provider_map,
        merge_provider_maps,
    )

    huge = {f"cap-{i}": {f"src-{i}"} for i in range(5000)}
    huge["GtkMM24-Devel"] = {"gtkmm24"}
    huge["libIL.so"] = {"DevIL"}
    indexed = index_provider_map(huge)
    assert isinstance(indexed["__lower__"], dict)
    assert _providers_from_maps("gtkmm24-devel", [indexed], exact=False) == {"gtkmm24"}
    assert _providers_from_maps("libIL.so.1()(64bit)", [indexed], exact=False) == {"DevIL"}

    merged = merge_provider_maps(indexed, {"Extra": {"x"}})
    assert isinstance(merged["__lower__"], dict)
    assert "__lower__" not in merged["__lower__"]
    assert _providers_from_maps("extra", [merged], exact=False) == {"x"}
    # old seed path did setdefault("__lower__", set()) which corrupts the index
    corrupted = dict(indexed)
    corrupted["__lower__"] = {"poison"}
    assert not isinstance(corrupted["__lower__"], dict)
