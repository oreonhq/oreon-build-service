from __future__ import annotations

import json
import pytest

from oreon_build.config import Settings
from oreon_build.services.signing import (
    _PESIGN_TREE_SCRIPT,
    package_name_matches_pesign_pattern,
    PesignRepackError,
    pesign_rpm_if_configured,
    resolve_pesign_nickname,
)


def test_pesign_tree_never_treats_kernel_modules_as_pe_images() -> None:
    assert "*.ko)" not in _PESIGN_TREE_SCRIPT
    assert "scripts/sign-file" in _PESIGN_TREE_SCRIPT


def test_pesign_tree_does_not_skip_pre_signed_pe_images() -> None:
    assert 'cd "$RPM_BUILD_ROOT"' in _PESIGN_TREE_SCRIPT
    assert "pesign -S" not in _PESIGN_TREE_SCRIPT
    assert 'pesign "${args[@]}"' in _PESIGN_TREE_SCRIPT
    assert "vmlinuz-*" in _PESIGN_TREE_SCRIPT


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
    assert package_name_matches_pesign_pattern("kernel", s) is False
    assert package_name_matches_pesign_pattern("kernel-headers", s) is False
    assert package_name_matches_pesign_pattern("kernel-cross-headers", s) is False
    assert package_name_matches_pesign_pattern("kernel-devel", s) is False
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

def test_pesign_expected_for_filename(monkeypatch) -> None:
    from oreon_build.api import worker_api

    s = Settings(
        pesign_rules_json=OREON_LIKE_RULES,
        pesign_nss_dir="/etc/pki/pesign",
    )
    monkeypatch.setattr(worker_api, "get_settings", lambda: s)
    assert worker_api._pesign_expected_for_filename("kernel-core-6.15.9-100.oreon.x86_64.rpm") is True
    assert worker_api._pesign_expected_for_filename("kernel-cross-headers-7.2.3-200.or11.aarch64.rpm") is False
    assert worker_api._pesign_expected_for_filename("kernel-6.15.9-100.oreon.src.rpm") is False
    assert worker_api._pesign_expected_for_filename("grub2-efi-x64-2.12-5.oreon.x86_64.rpm") is True
    assert worker_api._pesign_expected_for_filename("bash-5.2.32-1.oreon.x86_64.rpm") is False
    assert worker_api._pesign_expected_for_filename("not-an-rpm.txt") is False


def test_cross_arch_pesign_uses_host_tools_without_qemu_root(monkeypatch, tmp_path) -> None:
    from oreon_build.services import signing

    rpm = tmp_path / "kernel-core.rpm"
    rpm.write_bytes(b"rpm")
    settings = Settings(
        pesign_certificate_nickname="signer",
        pesign_nss_dir="/etc/pki/pesign",
        pesign_package_name_regex=r"^kernel.*$",
        pesign_strict=False,
        pesign_aarch64_qemu_prefix=None,
    )
    monkeypatch.setattr(signing, "_rpm_query", lambda *a: "kernel-core\naarch64")
    monkeypatch.setattr(signing.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(signing.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OREON_RPM_TMPDIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        signing.subprocess,
        "run",
        lambda cmd, **kwargs: calls.append(cmd) or type("Result", (), {"returncode": 1, "stderr": "test", "stdout": ""})(),
    )
    with pytest.raises(PesignRepackError):
        pesign_rpm_if_configured(rpm, settings)
    assert calls and calls[0][0] == "rpmrebuild"
    additional = next(arg for arg in calls[0] if arg.startswith("--additional="))
    assert "--target aarch64" in additional


@pytest.mark.parametrize("arch", ["aarch64", "x86_64"])
@pytest.mark.parametrize("name", [
    "kernel-debug-debuginfo", "kernel-debuginfo", "kernel-debuginfo-common-aarch64",
    "kernel-debugsource", "kernel-modules", "kernel-modules-core",
    "kernel-debug-modules-extra", "kernel-tools", "kernel-tools-libs", "kernel-debug",
])
def test_kernel_without_efi_payload_skips_repack(monkeypatch, tmp_path, name, arch):
    from oreon_build.services import signing

    settings = Settings(pesign_rules_json=OREON_LIKE_RULES, pesign_nss_dir="/etc/pki/pesign")
    rpm = tmp_path / f"{name}.rpm"
    rpm.write_bytes(b"rpm")
    monkeypatch.setattr(signing, "_rpm_query", lambda *args: f"{name}\n{arch}")
    monkeypatch.setattr(signing.tempfile, "mkdtemp", lambda **kwargs: pytest.fail("unnecessary payload repack"))
    assert resolve_pesign_nickname(name, arch, settings) is None
    assert pesign_rpm_if_configured(rpm, settings) is False


@pytest.mark.parametrize("name", ["kernel-core", "kernel-debug-core", "kernel-uki-virt", "kernel-debug-uki-dtbloader"])
def test_kernel_efi_payload_still_requires_signing(name):
    settings = Settings(pesign_rules_json=OREON_LIKE_RULES, pesign_nss_dir="/etc/pki/pesign")
    assert resolve_pesign_nickname(name, "aarch64", settings) == "oreonsecureboot501"
