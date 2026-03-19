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

"""Discord webhook integration for audit events."""
from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

import httpx

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


async def _post(payload: dict, webhook_url: str | None = None) -> None:
    """Low-level helper to POST a payload to the Discord webhook, if configured."""
    settings = get_settings()
    url = webhook_url or getattr(settings, "discord_webhook_url", None)
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except Exception:
        logger.exception("Failed to send Discord webhook")


async def send_discord_message(text: str) -> None:
    """Send a plain-text message to the configured Discord webhook, if any."""
    if not text:
        return
    # Discord has a 2000-char hard limit on content; stay safely under it.
    await _post({"content": (text or "")[:1900]})


async def send_discord_embed(title: str, description: str, fields: list[tuple[str, str]]) -> None:
    """Send a single rich embed (no emojis) to the Discord webhook."""
    settings = get_settings()
    if not getattr(settings, "discord_webhook_url", None):
        return
    embed_fields = []
    for name, value in fields:
        if not value:
            continue
        embed_fields.append(
            {
                "name": name,
                "value": value[:900],  # safety
                "inline": False,
            }
        )
    embed = {
        "title": title[:256] if title else "Oreon Build Service",
        "description": (description or "")[:1000],
        "color": 0x2563EB,
        "fields": embed_fields,
    }
    await _post({"embeds": [embed]})


async def send_security_discord_embed(
    title: str, description: str, fields: list[tuple[str, str]]
) -> None:
    """Send a CVE/security embed to the dedicated security webhook (if configured)."""
    settings = get_settings()
    url = getattr(settings, "security_discord_webhook_url", None)
    if not url:
        return
    # If security and normal webhooks are misconfigured to be identical,
    # the embeds will appear in the same Discord destination.
    normal_url = getattr(settings, "discord_webhook_url", None)
    if normal_url and normal_url == url:
        logger.warning("SECURITY_DISCORD_WEBHOOK_URL equals DISCORD_WEBHOOK_URL; security embeds go to the normal webhook destination.")
    try:
        parsed = urlparse(url)
        fp = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        logger.info(
            "Posting security embed to webhook destination %s/%s (fp=%s)",
            parsed.netloc,
            (parsed.path[:20] + "..." if len(parsed.path) > 20 else parsed.path),
            fp,
        )
    except Exception:
        # Never break the request for logging failures.
        pass

    embed_fields = []
    for name, value in fields:
        if not value:
            continue
        embed_fields.append({"name": name, "value": value[:900], "inline": False})
    embed = {
        "title": title[:256] if title else "Oreon Security Advisory",
        "description": (description or "")[:1000],
        "color": 0xB91C1C,  # red-ish
        "fields": embed_fields,
    }
    await _post({"embeds": [embed]}, webhook_url=url)

