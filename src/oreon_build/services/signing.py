# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""RPM signing: GPG (rpm header) and optional pesign (PE / kernel modules inside payload)."""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from oreon_build.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _signing_passphrase_plain(settings: Settings) -> Optional[str]:
    sec = settings.signing_gpg_passphrase
    if sec is None:
        return None
    v = sec.get_secret_value().strip()
    return v or None


class PesignRepackError(RuntimeError):
    """Raised when pesign/repack was required (strict mode) but failed."""


@dataclass
class PesignRuntimeConfig:
    """Controller-issued pesign material on a worker (``pesign_nss_dir`` is a local extracted path)."""

    pesign_nss_dir: str
    pesign_rules_json: Optional[str] = None
    pesign_certificate_nickname: Optional[str] = None
    pesign_package_name_regex: str = r"^(kernel.*|shim.*|fwupd.*|grub2-efi.*)$"
    pesign_token: Optional[str] = None
    pesign_strict: bool = False
    pesign_aarch64_qemu_prefix: Optional[str] = None
    pesign_aarch64_guest_shell: Optional[str] = None


PesignConfig = Settings | PesignRuntimeConfig


def _norm_arch(a: str) -> str:
    x = (a or "").strip().lower()
    if x in ("amd64",):
        return "x86_64"
    if x in ("arm64",):
        return "aarch64"
    return x


_EM_AARCH64 = 183  # e_machine in ELFhdr (little-endian AArch64)


def _elf_is_aarch64(path: str | Path) -> bool:
    """True if path is an ELF64 executable with e_machine AArch64."""
    try:
        p = Path(path)
        with p.open("rb") as fh:
            hdr = fh.read(20)
    except OSError:
        return False
    if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
        return False
    if hdr[4] != 2:
        return False
    em = int.from_bytes(hdr[18:20], "little")
    return em == _EM_AARCH64


def _normalize_qemu_prefix_line(prefix: str) -> str:
    """Strip outer wrapping quotes some dotenv parsers leave on the whole value."""
    s = (prefix or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _qemu_bash_argv(prefix: str, script: str, guest_shell: Optional[str] = None) -> list[str]:
    """
    Build argv for qemu execution.
    Supports both:
      - "qemu-aarch64-static -L /usr/aarch64-linux-gnu"
      - "qemu-aarch64-static -L /usr/aarch64-linux-gnu /usr/bin/bash"
    """
    raw = _normalize_qemu_prefix_line(prefix)
    parts = [p.strip() for p in shlex.split(raw)]
    if not parts:
        raise ValueError("empty qemu prefix")
    tail_name = Path(parts[-1]).name.lower()
    # Only treat as explicit guest shell if it is a normal file named sh/bash (not a missing path
    # and not a directory whose basename happens to be "sh").
    if tail_name in ("bash", "sh"):
        explicit_shell = parts[-1]
        rp = Path(explicit_shell)
        if rp.is_dir():
            raise ValueError(
                f"last PESIGN_AARCH64_QEMU_PREFIX token is a directory, not a shell binary: {explicit_shell}"
            )
        if not rp.is_file():
            raise ValueError(
                f"guest shell is not present on this machine (create the AArch64 rootfs first): {explicit_shell}. "
                "Example Fedora host: "
                "dnf install --installroot=/var/lib/oreon/aarch64-root --releasever=$(rpm -E %fedora) "
                "--forcearch=aarch64 bash coreutils rpm-build perl rpmrebuild pesign nss-tools"
            )
        if not _elf_is_aarch64(rp):
            raise ValueError(
                f"explicit shell must be AArch64 ELF, not host binary: {explicit_shell}"
            )
        return parts + ["-lc", script]
    sysroot: str | None = None
    for i, tok in enumerate(parts):
        if tok == "-L" and i + 1 < len(parts):
            sysroot = parts[i + 1]
            break
    shell_candidate: str | None = None
    if sysroot:
        c1 = Path(sysroot) / "bin/bash"
        c2 = Path(sysroot) / "usr/bin/bash"
        c3 = Path(sysroot) / "bin/sh"
        c4 = Path(sysroot) / "usr/bin/sh"
        for c in (c1, c2, c3, c4):
            if c.is_file() and _elf_is_aarch64(c):
                shell_candidate = str(c)
                break
    if not shell_candidate and (guest_shell or "").strip():
        gs = Path(_normalize_qemu_prefix_line((guest_shell or "").strip()))
        if not gs.is_file():
            raise ValueError(f"PESIGN_AARCH64_GUEST_SHELL path not found: {gs}")
        if not _elf_is_aarch64(gs):
            raise ValueError(f"PESIGN_AARCH64_GUEST_SHELL must be AArch64 ELF: {gs}")
        shell_candidate = str(gs)
    if not shell_candidate:
        raise ValueError(
            "no AArch64 sh or bash under the qemu -L tree (Fedora /usr/aarch64-linux-gnu is often "
            "libraries only). Use a full AArch64 rootfs as -L (mock installroot or container root), "
            "append .../bin/sh to PESIGN_AARCH64_QEMU_PREFIX, or set PESIGN_AARCH64_GUEST_SHELL "
            "to an AArch64 ELF /bin/sh from that rootfs."
        )
    return parts + [shell_candidate, "-lc", script]


def _rpm_query(rpm_path: Path, query_format: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["rpm", "-qp", "--qf", query_format, str(rpm_path)],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except FileNotFoundError:
        logger.error("rpm command not found; cannot query %s", rpm_path.name)
        return None
    if result.returncode != 0:
        err = (result.stderr or "").strip() or (result.stdout or "").strip()
        logger.warning("rpm -qp failed for %s: %s", rpm_path.name, err or "(no output)")
        return None
    return (result.stdout or "").strip()


def _parse_pesign_rules(settings: PesignConfig) -> list[dict[str, str]]:
    raw = (settings.pesign_rules_json or "").strip()
    if not raw:
        return []
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid pesign_rules_json (not JSON): %s", exc)
        return []
    if not isinstance(data, list):
        logger.error("pesign_rules_json must be a JSON array")
        return []
    out: list[dict[str, str]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            logger.error("pesign_rules_json[%s] must be an object", i)
            continue
        name_rx = item.get("name_regex")
        nick = item.get("nickname")
        if not isinstance(name_rx, str) or not isinstance(nick, str):
            logger.error("pesign_rules_json[%s] needs string name_regex and nickname", i)
            continue
        rule: dict[str, str] = {"name_regex": name_rx, "nickname": nick}
        arx = item.get("arch_regex")
        if arx is not None:
            if not isinstance(arx, str):
                logger.error("pesign_rules_json[%s] arch_regex must be a string", i)
                continue
            rule["arch_regex"] = arx
        out.append(rule)
    return out


def resolve_pesign_nickname(package_name: str, arch: str, settings: PesignConfig) -> Optional[str]:
    """
    Pick the NSS signing certificate nickname for this RPM name and architecture.

    Order: ordered rules from pesign_rules_json (name_regex full-match, optional arch_regex
    full-match on RPM %%{ARCH}); then if pesign_certificate_nickname is set, use it when
    pesign_package_name_regex full-matches the name. Signing CA trust-only certs belong in the
    NSS DB but must not be used as -c signing identities.
    """
    if not settings.pesign_nss_dir:
        return None
    flags = re.IGNORECASE | re.DOTALL
    for rule in _parse_pesign_rules(settings):
        try:
            if not re.fullmatch(rule["name_regex"], package_name, flags=flags):
                continue
            arx = rule.get("arch_regex")
            if arx:
                if not re.fullmatch(arx, arch, flags=re.IGNORECASE):
                    continue
            return rule["nickname"]
        except re.error as exc:
            logger.error("Invalid regex in pesign rule: %s", exc)
            continue
    nick = settings.pesign_certificate_nickname
    if not nick:
        return None
    try:
        if re.fullmatch(settings.pesign_package_name_regex, package_name, flags=flags):
            return nick
    except re.error as exc:
        logger.error("Invalid pesign_package_name_regex: %s", exc)
    return None


def package_name_matches_pesign_pattern(
    package_name: str, settings: PesignConfig, arch: str = "x86_64"
) -> bool:
    """True if a signing nickname would be chosen for this name/arch (tests / diagnostics)."""
    return resolve_pesign_nickname(package_name, arch, settings) is not None


def should_apply_pesign_to_rpm(rpm_path: Path, settings: Optional[PesignConfig] = None) -> bool:
    """True if this binary RPM should be repacked with pesign on PE binaries and .ko modules."""
    s = settings or get_settings()
    if rpm_path.name.endswith(".src.rpm"):
        return False
    meta = _rpm_query(rpm_path, "%{NAME}\n%{ARCH}")
    if not meta:
        return False
    parts = meta.split("\n", 1)
    name = parts[0].strip()
    arch = (parts[1].strip() if len(parts) > 1 else "") or "noarch"
    return resolve_pesign_nickname(name, arch, s) is not None


# Executed by rpmrebuild with cwd = extracted RPM root (see rpmrebuild --change-files).
_PESIGN_TREE_SCRIPT = r"""#!/usr/bin/env bash
set -euo pipefail
NSS_DEFAULT_DB_TYPE="${NSS_DEFAULT_DB_TYPE:-sql}"
export NSS_DEFAULT_DB_TYPE
_pesign_one() {
  local f="$1"
  [[ -f "$f" && ! -L "$f" ]] || return 0
  local do_sign=0
  case "$f" in
    *.ko) do_sign=1 ;;
    *)
      if file -b -- "$f" 2>/dev/null | grep -qE 'PE32\+? executable|MS Windows'; then
        do_sign=1
      fi
      ;;
  esac
  [[ "$do_sign" -eq 1 ]] || return 0
  local tmp="${f}.pesign.$$"
  local -a args=( -s -n "${PESIGN_NSS_DIR}" -c "${PESIGN_CERT_NICK}" -i "$f" -o "$tmp" )
  if [[ -n "${PESIGN_TOKEN:-}" ]]; then
    args+=( -t "$PESIGN_TOKEN" )
  fi
  pesign "${args[@]}"
  mv -f "$tmp" "$f"
}
while IFS= read -r -d '' f; do
  _pesign_one "$f"
done < <(find . -type f -print0)
"""


def pesign_rpm_if_configured(rpm_path: Path, settings: Optional[PesignConfig] = None) -> bool:
    """
    If configured and a signing nickname is resolved for this RPM (rules and/or global fallback),
    rebuild the RPM with rpmrebuild so PE executables and *.ko modules are signed with pesign.

    Requires: pesign, rpmrebuild, rpm, file, bash, find on PATH.

    Returns True if pesign repack ran and succeeded. False if skipped (not configured / no match /
    tools missing when not strict). Raises PesignRepackError when strict and signing must apply
    but fails.
    """
    s = settings or get_settings()
    if rpm_path.name.endswith(".src.rpm"):
        return False
    meta = _rpm_query(rpm_path, "%{NAME}\n%{ARCH}")
    if not meta:
        return False
    parts = meta.split("\n", 1)
    pkg_name = parts[0].strip()
    pkg_arch = _norm_arch((parts[1].strip() if len(parts) > 1 else "") or "noarch")
    cert_nick = resolve_pesign_nickname(pkg_name, pkg_arch, s)
    if not cert_nick:
        return False

    host_arch = _norm_arch(platform.machine())
    cross_aarch64 = pkg_arch == "aarch64" and host_arch != "aarch64"

    for bin_name in ("pesign", "rpmrebuild", "file", "bash"):
        if not shutil.which(bin_name):
            msg = f"{bin_name} not found on PATH; cannot pesign RPM payload"
            logger.error(msg)
            if s.pesign_strict:
                raise PesignRepackError(msg) from None
            return False

    tmp_root = tempfile.mkdtemp(prefix="oreon-pesign-")
    script_path = Path(tmp_root) / "pesign-tree.sh"
    setarch_wrapper = Path(tmp_root) / "setarch"
    out_dir = Path(tmp_root) / "out"
    rb_tmp = Path(tmp_root) / "rpmrebuild"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        rb_tmp.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_PESIGN_TREE_SCRIPT, encoding="utf-8")
        script_path.chmod(0o700)

        env = os.environ.copy()
        env.setdefault("LC_ALL", "C.UTF-8")
        env["NSS_DEFAULT_DB_TYPE"] = "sql"
        env["PESIGN_NSS_DIR"] = s.pesign_nss_dir
        env["PESIGN_CERT_NICK"] = cert_nick
        if s.pesign_token:
            env["PESIGN_TOKEN"] = s.pesign_token
        env["RPMREBUILD_TMPDIR"] = str(rb_tmp)
        env["RPM_BUILD_ARCH"] = pkg_arch
        # rpmrebuild always injects `setarch <rpm-arch> ...`. On many x86 hosts,
        # util-linux setarch does not recognize "aarch64". For cross-aarch64 mode,
        # force qemu by providing PESIGN_AARCH64_QEMU_PREFIX.
        real_setarch = shutil.which("setarch") or "/usr/bin/setarch"
        setarch_wrapper.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'arch="${1:-}"',
                    "shift || true",
                    'if [[ "$arch" == "aarch64" ]]; then',
                    '  if [[ -z "${PESIGN_AARCH64_QEMU_PREFIX:-}" ]]; then',
                    '    echo "PESIGN_AARCH64_QEMU_PREFIX is required for aarch64 repack" >&2',
                    "    exit 97",
                    "  fi",
                    "  # rpmrebuild always runs `setarch aarch64 ...`. On x86 hosts that breaks, so the",
                    "  # outer flow wraps the whole rpmrebuild in qemu-aarch64. Once we are already",
                    "  # inside that guest userspace, child rpmbuild/rpm binaries are aarch64 ELFs.",
                    "  # Do not spawn another qemu layer or a host bash here; just run the command.",
                    '  bin="$(command -v "${1:-}" || true)"',
                    '  if [[ -n "$bin" ]] && file -Lb -- "$bin" 2>/dev/null | grep -qi "aarch64"; then',
                    "    exec \"$@\"",
                    "  fi",
                    '  exec "$@"',
                    "fi",
                    f'exec "{real_setarch}" "$arch" "$@"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        setarch_wrapper.chmod(0o755)
        env["PATH"] = f"{tmp_root}:{env.get('PATH', '')}"
        if cross_aarch64:
            qemu_prefix = (s.pesign_aarch64_qemu_prefix or "").strip()
            if not qemu_prefix:
                msg = (
                    f"Cross-arch aarch64 pesign repack requires PESIGN_AARCH64_QEMU_PREFIX "
                    f"(rpm={rpm_path.name}, host_arch={host_arch})"
                )
                logger.error(msg)
                raise PesignRepackError(msg) from None
            env["PESIGN_AARCH64_QEMU_PREFIX"] = qemu_prefix
            # rpmrebuild consumes this variable and appends it to rpmbuild/rpm commands.
            # Force build/host/target arches so rpmbuild does not reject aarch64 payload repack
            # on an x86_64 controller.
            extra_defines = " ".join(
                [
                    '--define "_target_cpu aarch64"',
                    '--define "_target aarch64-redhat-linux"',
                    '--define "_host_cpu aarch64"',
                    '--define "_host aarch64-redhat-linux"',
                    '--define "_build_cpu aarch64"',
                    '--define "_build aarch64-redhat-linux"',
                    '--define "_arch aarch64"',
                    '--define "_build_arch aarch64"',
                ]
            )
            env["RPMREBUILD_rpm_defines"] = f'{env.get("RPMREBUILD_rpm_defines", "").strip()} {extra_defines}'.strip()

        cmd = [
            "rpmrebuild",
            "--batch",
            "--notest-install",
            "--package",
            f"--change-files={script_path}",
            "--directory",
            str(out_dir),
            str(rpm_path),
        ]
        if cross_aarch64:
            # Run the entire rpmrebuild flow inside qemu-provided aarch64 userspace.
            # Supports prefixes with or without trailing shell binary.
            inner_cmd = " ".join(shlex.quote(x) for x in cmd)
            try:
                qemu_argv = _qemu_bash_argv(
                    env["PESIGN_AARCH64_QEMU_PREFIX"],
                    inner_cmd,
                    s.pesign_aarch64_guest_shell,
                )
            except ValueError as exc:
                msg = f"Invalid PESIGN_AARCH64_QEMU_PREFIX: {exc}"
                logger.error(msg)
                raise PesignRepackError(msg) from None
            result = subprocess.run(
                qemu_argv,
                capture_output=True,
                text=True,
                env=env,
            )
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            err = stderr or stdout
            msg = (
                f"rpmrebuild/pesign failed for {rpm_path.name} "
                f"(rpm_arch={pkg_arch}, host_arch={host_arch}): {err or '(no output)'}"
            )
            logger.error(msg)
            if stderr and stdout:
                logger.error(
                    "rpmrebuild detail stderr=%s stdout=%s",
                    stderr[-2000:],
                    stdout[-2000:],
                )
            if s.pesign_strict or cross_aarch64:
                raise PesignRepackError(msg) from None
            return False

        built = sorted(out_dir.glob("*.rpm"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not built:
            msg = f"rpmrebuild produced no .rpm in {out_dir} for {rpm_path.name}"
            logger.error(msg)
            if s.pesign_strict:
                raise PesignRepackError(msg) from None
            return False
        if len(built) > 1:
            logger.warning(
                "Multiple RPMs in rpmrebuild output dir; using newest: %s",
                built[0].name,
            )
        shutil.copyfile(built[0], rpm_path)
        logger.info(
            "pesign payload signing (rpmrebuild) completed for %s (cert=%s)",
            rpm_path.name,
            cert_nick,
        )
        return True
    except PesignRepackError:
        raise
    except OSError as exc:
        msg = f"pesign RPM repack I/O error for {rpm_path.name}: {exc}"
        logger.exception(msg)
        if s.pesign_strict:
            raise PesignRepackError(msg) from exc
        return False
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def sign_rpm(rpm_path: Path, key_id: Optional[str] = None) -> bool:
    settings = get_settings()
    kid = key_id or settings.signing_key_id
    if not kid:
        logger.warning("No signing key configured; skipping RPM sign")
        return False
    env = os.environ.copy()
    if settings.gpg_home:
        env["GNUPGHOME"] = settings.gpg_home
    env.setdefault("LC_ALL", "C.UTF-8")
    phrase = _signing_passphrase_plain(settings)
    pass_path: Optional[str] = None
    try:
        extra = "--batch --no-tty"
        if phrase:
            fd, pass_path = tempfile.mkstemp(prefix="oreon-gpg-pass-", text=True)
            with os.fdopen(fd, "w") as pf:
                pf.write(phrase)
            os.chmod(pass_path, 0o600)
            extra += f" --pinentry-mode loopback --passphrase-file {shlex.quote(pass_path)}"
        result = subprocess.run(
            [
                "rpm",
                "--define",
                f"_gpg_name {kid}",
                "--define",
                f"_gpg_sign_cmd_extra_args {extra}",
                "--addsign",
                str(rpm_path),
            ],
            capture_output=True,
            env=env,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip() or (result.stdout or "").strip()
            logger.error(
                "RPM signing failed (exit %d) for %s: %s",
                result.returncode,
                rpm_path.name,
                err or "(no output)",
            )
            return False
        return True
    except FileNotFoundError as e:
        logger.exception("rpm command not found: %s", e)
        return False
    finally:
        if pass_path:
            try:
                os.unlink(pass_path)
            except OSError:
                pass


def sign_repomd(repomd_path: Path, key_id: Optional[str] = None) -> bool:
    settings = get_settings()
    kid = key_id or settings.signing_key_id
    if not kid:
        logger.warning("No signing key configured; skipping repomd sign")
        return False
    env = os.environ.copy()
    if settings.gpg_home:
        env["GNUPGHOME"] = settings.gpg_home
    env.setdefault("LC_ALL", "C.UTF-8")
    phrase = _signing_passphrase_plain(settings)
    asc_path = repomd_path.parent / f"{repomd_path.name}.asc"
    try:
        asc_path.unlink(missing_ok=True)
    except OSError:
        pass
    cmd = ["gpg", "--batch", "--no-tty", "--yes", "--detach-sign", "--armor", "-u", kid, str(repomd_path)]
    stdin_input: Optional[str] = None
    if phrase:
        cmd[1:1] = ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
        stdin_input = phrase + "\n"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            text=True,
            input=stdin_input,
        )
        if result.returncode != 0:
            err = (result.stderr or "").strip() or (result.stdout or "").strip()
            logger.error(
                "repomd signing failed (exit %d): %s",
                result.returncode,
                err or "(no output)",
            )
            return False
        return True
    except FileNotFoundError as e:
        logger.exception("gpg command not found: %s", e)
        return False
