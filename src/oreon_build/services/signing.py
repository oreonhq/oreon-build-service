"""RPM and repository metadata signing (GPG)."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


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
