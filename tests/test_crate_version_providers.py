from oreon_build.services.dep_plan.providers import (
    _rust_compat_series_from_package,
    _source_may_satisfy_crate_versions,
    providers_for_requirement,
)
from oreon_build.services.dep_plan.install_oracle import overlay_from_nodes
from oreon_build.services.dep_plan.types import PackageNode
from oreon_build.services.repo_solver import OverlayPackage


def test_compat_series_from_package_name():
    assert _rust_compat_series_from_package("rust-rustix0.37") == "0.37"
    assert _rust_compat_series_from_package("rust-rustix0.38") == "0.38"
    assert _rust_compat_series_from_package("rust-rustix") is None
    # major-only compat needs Version: to strip; without it crate name keeps the digit
    assert _rust_compat_series_from_package("rust-nom7") is None


def test_source_may_satisfy_rustix_037_bounds():
    rich = (
        "(crate(rustix/default) >= 0.37.0 with crate(rustix/default) < 0.38.0~)"
    )
    assert _source_may_satisfy_crate_versions("rust-rustix0.37", rich)
    assert not _source_may_satisfy_crate_versions("rust-rustix0.38", rich)
    # current unsuffixed package kept; Version: decides later
    assert _source_may_satisfy_crate_versions("rust-rustix", rich)


def test_compat_series_is_range_not_point():
    # rust-structmeta0.1 ships 0.1.6; series token must not fail >= 0.1.4
    rich = (
        "(crate(structmeta/default) >= 0.1.4 with crate(structmeta/default) < 0.2.0~)"
    )
    assert _source_may_satisfy_crate_versions("rust-structmeta0.1", rich)
    assert not _source_may_satisfy_crate_versions("rust-structmeta0.2", rich)


def test_current_0x_package_not_rejected_by_name():
    rich = (
        "(crate(env_logger/default) >= 0.11.0 with crate(env_logger/default) < 0.12.0~)"
    )
    assert _source_may_satisfy_crate_versions("rust-env_logger", rich)
    assert not _source_may_satisfy_crate_versions("rust-env_logger0.10", rich)


def test_providers_for_requirement_prefers_compat_series():
    pmap = {
        "crate(rustix)": {"rust-rustix", "rust-rustix0.37", "rust-rustix0.38"},
        "crate(rustix/default)": {
            "rust-rustix",
            "rust-rustix0.37",
            "rust-rustix0.38",
        },
    }
    rich = (
        "(crate(rustix/default) >= 0.37.0 with crate(rustix/default) < 0.38.0~)"
    )
    got = providers_for_requirement(
        rich,
        provider_map=pmap,
        package_names=set(pmap["crate(rustix)"]),
    )
    assert "rust-rustix0.37" in got
    assert "rust-rustix0.38" not in got
    assert "rust-rustix" in got


def test_providers_structmeta_compat_series():
    pmap = {
        "crate(structmeta)": {"rust-structmeta0.1"},
        "crate(structmeta/default)": {"rust-structmeta0.1"},
    }
    rich = (
        "(crate(structmeta/default) >= 0.1.4 with crate(structmeta/default) < 0.2.0~)"
    )
    got = providers_for_requirement(
        rich,
        provider_map=pmap,
        package_names={"rust-structmeta0.1"},
    )
    assert got == {"rust-structmeta0.1"}


def test_overlay_stamps_version_and_crate_caps():
    nodes = {
        "rust-rustix0.37": PackageNode(
            name="rust-rustix0.37",
            version="0.37.28",
            release="1.or11",
            provides={"rust-rustix0.37", "rust-rustix0.37+default-devel"},
        )
    }
    ov = overlay_from_nodes(nodes, {"crate(rustix)": {"rust-rustix0.37"}})
    pkg = ov["rust-rustix0.37"]
    assert isinstance(pkg, OverlayPackage)
    assert pkg.version == "0.37.28"
    assert "crate(rustix)" in pkg.provides
    assert "crate(rustix/default)" in pkg.provides


def test_overlay_skips_blocked_nodes():
    nodes = {
        "golist": PackageNode(name="golist", action="block", reason="missing go-vendor-tools"),
        "sbctl": PackageNode(name="sbctl", action="pending"),
    }
    ov = overlay_from_nodes(nodes, {"golist": {"golist"}, "sbctl": {"sbctl"}})
    assert "golist" not in ov
    assert "sbctl" in ov


def test_providers_for_versioned_perl_requirement_uses_parent_walk():
    # versioned perl(...) should still find the distgit source package
    got = providers_for_requirement(
        "perl(MIME::Entity) >= 5.508",
        provider_map={},
        package_names={"perl-MIME-Entity"},
    )
    assert got == {"perl-MIME-Entity"}


def test_providers_for_versioned_perl_requirement_uses_metacpan_dist():
    # MIME::Entity is in the MIME-tools distribution on CPAN -> perl-MIME-tools
    got = providers_for_requirement(
        "perl(MIME::Entity) >= 3.0",
        provider_map={},
        package_names={"perl-MIME-tools"},
    )
    assert got == {"perl-MIME-tools"}
