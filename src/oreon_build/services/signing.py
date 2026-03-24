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
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from oreon_build.config import Settings, get_settings

logger = logging.getLogger(__name__)


class PesignRepackError(RuntimeError):
    """Raised when pesign/repack was required (strict mode) but failed."""


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


def _parse_pesign_rules(settings: Settings) -> list[dict[str, str]]:
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


def resolve_pesign_nickname(package_name: str, arch: str, settings: Settings) -> Optional[str]:
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
    package_name: str, settings: Settings, arch: str = "x86_64"
) -> bool:
    """True if a signing nickname would be chosen for this name/arch (tests / diagnostics)."""
    return resolve_pesign_nickname(package_name, arch, settings) is not None


def should_apply_pesign_to_rpm(rpm_path: Path, settings: Optional[Settings] = None) -> bool:
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


def pesign_rpm_if_configured(rpm_path: Path, settings: Optional[Settings] = None) -> bool:
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
    pkg_arch = (parts[1].strip() if len(parts) > 1 else "") or "noarch"
    cert_nick = resolve_pesign_nickname(pkg_name, pkg_arch, s)
    if not cert_nick:
        return False

    for bin_name in ("pesign", "rpmrebuild", "file", "bash"):
        if not shutil.which(bin_name):
            msg = f"{bin_name} not found on PATH; cannot pesign RPM payload"
            logger.error(msg)
            if s.pesign_strict:
                raise PesignRepackError(msg) from None
            return False

    tmp_root = tempfile.mkdtemp(prefix="oreon-pesign-")
    script_path = Path(tmp_root) / "pesign-tree.sh"
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
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            err = (result.stderr or "").strip() or (result.stdout or "").strip()
            msg = f"rpmrebuild/pesign failed for {rpm_path.name}: {err or '(no output)'}"
            logger.error(msg)
            if s.pesign_strict:
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
    try:
        result = subprocess.run(
            ["rpm", "--define", f"_gpg_name {kid}", "--addsign", str(rpm_path)],
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


def sign_repomd(repomd_path: Path, key_id: Optional[str] = None) -> bool:
    settings = get_settings()
    kid = key_id or settings.signing_key_id
    if not kid:
        logger.warning("No signing key configured; skipping repomd sign")
        return False
    env = os.environ.copy()
    if settings.gpg_home:
        env["GNUPGHOME"] = settings.gpg_home
    try:
        result = subprocess.run(
            ["gpg", "--detach-sign", "--armor", "-u", kid, str(repomd_path)],
            capture_output=True,
            env=env,
            text=True,
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
