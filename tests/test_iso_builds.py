# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import pytest

from oreon_build.services.iso_builds import iso_filename_for_arch, mock_config_for_arch, validate_iso_request


def test_iso_name_adds_arch_once() -> None:
    assert iso_filename_for_arch("Oreon-11-RP1-BB2610", "x86_64") == "Oreon-11-RP1-BB2610-x86_64.iso"
    assert iso_filename_for_arch("Oreon-11-RP1-BB2610-x86_64.iso", "x86_64") == "Oreon-11-RP1-BB2610-x86_64.iso"
    assert iso_filename_for_arch("Oreon-{arch}", "aarch64") == "Oreon-aarch64.iso"


def test_two_arches_need_two_kickstarts() -> None:
    with pytest.raises(ValueError, match="own kickstart"):
        validate_iso_request(
            release_folder="oreon-11-rp1",
            arches=["x86_64", "aarch64"],
            kickstarts={"x86_64": "11.ks"},
            iso_name="Oreon-11",
            project="Oreon",
            volid="Oreon-11",
            releasever="11",
        )


def test_mock_config_swaps_arch() -> None:
    assert mock_config_for_arch("oreon-11-x86_64", "aarch64") == "oreon-11-aarch64"
