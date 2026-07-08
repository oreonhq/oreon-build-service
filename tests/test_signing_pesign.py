from __future__ import annotations

import json

from oreon_build.config import Settings
from oreon_build.services.signing import (
    package_name_matches_pesign_pattern,
    resolve_pesign_nickname,
)

OREON_LIKE_RULES = json.dumps(
    [
        {"name_regex": r"^grub2-efi.*", "nickname": "oreon-grub2-signer"},
        {
            "name_regex": r"^kernel.*",
            "arch_regex": r"^(s390x|s390)$",
            "nickname": "oreonsecureboot302",
        },
        {"name_regex": r"^kernel.*", "nickname": "oreonsecureboot501"},
    ]
)

def test_pesign_pattern_requires_nickname_when_no_rules() -> None:
    s = Settings(
        pesign_certificate_nickname=None,
        pesign_nss_dir="/etc/pki/pesign",
        pesign_package_name_regex=r"^(kernel.*)$",
        pesign_rules_json="[]",
    )
    assert package_name_matches_pesign_pattern("kernel-core", s) is False

def test_pesign_pattern_kernel_matches_global() -> None:
    s = Settings(
        pesign_certificate_nickname="signer",
        pesign_nss_dir="/etc/pki/pesign",
        pesign_package_name_regex=r"^(kernel.*|shim.*)$",
    )
    assert package_name_matches_pesign_pattern("kernel-core", s) is True
    assert package_name_matches_pesign_pattern("shim-x64", s) is True
    assert package_name_matches_pesign_pattern("bash", s) is False

def test_pesign_pattern_requires_nss_dir() -> None:
    s = Settings(
        pesign_certificate_nickname="signer",
        pesign_nss_dir="",
        pesign_package_name_regex=r"^(kernel.*)$",
    )
    assert package_name_matches_pesign_pattern("kernel-core", s) is False

def test_rules_grub_kernel_s390() -> None:
    s = Settings(
        pesign_rules_json=OREON_LIKE_RULES,
        pesign_nss_dir="/etc/pki/pesign",
    )
    assert resolve_pesign_nickname("grub2-efi-x64", "x86_64", s) == "oreon-grub2-signer"
    assert resolve_pesign_nickname("kernel-core", "s390x", s) == "oreonsecureboot302"
    assert resolve_pesign_nickname("kernel-core", "x86_64", s) == "oreonsecureboot501"
    assert resolve_pesign_nickname("kernel-core", "aarch64", s) == "oreonsecureboot501"

def test_rules_then_global_fallback() -> None:
    s = Settings(
        pesign_rules_json=json.dumps(
            [{"name_regex": r"^grub2-efi.*", "nickname": "oreon-grub2-signer"}]
        ),
        pesign_certificate_nickname="oreonsecureboot501",
        pesign_package_name_regex=r"^(shim.*)$",
        pesign_nss_dir="/etc/pki/pesign",
    )
    assert resolve_pesign_nickname("grub2-efi-x64", "x86_64", s) == "oreon-grub2-signer"
    assert resolve_pesign_nickname("shim-x64", "x86_64", s) == "oreonsecureboot501"
    assert resolve_pesign_nickname("bash", "x86_64", s) is None
