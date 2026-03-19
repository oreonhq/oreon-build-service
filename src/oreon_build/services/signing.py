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
    try:
        subprocess.run(
            ["rpm", "--define", f"_gpg_name {kid}", "--addsign", str(rpm_path)],
            check=True,
            env=env,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.exception("RPM signing failed: %s", e)
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
        subprocess.run(
            ["gpg", "--detach-sign", "--armor", "-u", kid, str(repomd_path)],
            check=True,
            env=env,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.exception("repomd signing failed: %s", e)
        return False
