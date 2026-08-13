from __future__ import annotations

import json
from pathlib import Path

import pytest

from oreon_build.config import Settings
from oreon_build.services.signing import (
    PesignRepackError,
    export_nss_cert_der,
    package_name_matches_pesign_pattern,
    package_wants_secureboot_uki_cer,
    resolve_pesign_nickname,
    secureboot_uki_cer_filename,
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


def test_secureboot_uki_cer_filename() -> None:
    assert secureboot_uki_cer_filename("x86_64") == "secureboot-uki-x86_64.cer"
    assert secureboot_uki_cer_filename("amd64") == "secureboot-uki-x86_64.cer"
    assert secureboot_uki_cer_filename("aarch64") == "secureboot-uki-aarch64.cer"
    assert secureboot_uki_cer_filename("arm64") == "secureboot-uki-aarch64.cer"


def test_package_wants_secureboot_uki_cer() -> None:
    assert package_wants_secureboot_uki_cer("kernel") is True
    assert package_wants_secureboot_uki_cer("kernel-core") is True
    assert package_wants_secureboot_uki_cer("kernel-uki-x86_64") is True
    assert package_wants_secureboot_uki_cer("shim-x64") is False
    assert package_wants_secureboot_uki_cer("grub2-efi-x64") is False


def test_export_nss_cert_der_writes_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 0
        stdout = b"\x30" + b"\x00" * 40
        stderr = b""

    def _run(cmd, capture_output=False, env=None):
        assert cmd[0] == "certutil"
        assert "-r" in cmd
        return _R()

    monkeypatch.setattr("oreon_build.services.signing.shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr("oreon_build.services.signing.subprocess.run", _run)
    dest = tmp_path / "out.cer"
    export_nss_cert_der("/etc/pki/pesign", "oreonsecureboot501", dest)
    assert dest.read_bytes().startswith(b"\x30")
    assert dest.stat().st_size >= 32


def test_export_nss_cert_der_fails_on_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 0
        stdout = b"nope"
        stderr = b""

    monkeypatch.setattr("oreon_build.services.signing.shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr("oreon_build.services.signing.subprocess.run", lambda *a, **k: _R())
    with pytest.raises(PesignRepackError, match="empty/short"):
        export_nss_cert_der("/etc/pki/pesign", "oreonsecureboot501", tmp_path / "x.cer")
