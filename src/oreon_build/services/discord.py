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

# Discord can be slow or blocked from some hosts, short timeouts caused noisy tracebacks.
_DISCORD_TIMEOUT = httpx.Timeout(30.0, connect=20.0)


async def _post(payload: dict, webhook_url: str | None = None) -> None:
    """Low-level helper to POST a payload to the Discord webhook, if configured."""
    settings = get_settings()
    url = webhook_url or getattr(settings, "discord_webhook_url", None)
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=_DISCORD_TIMEOUT) as client:
            await client.post(url, json=payload)
    except httpx.RequestError as e:
        logger.warning("Discord webhook request failed (%s)", e)
    except httpx.HTTPStatusError as e:
        logger.warning("Discord webhook HTTP error %s", e.response.status_code)
    except Exception:
        logger.exception("Discord webhook unexpected error")


async def send_discord_message(text: str) -> None:
    """Send a plain-text message to the configured Discord webhook, if any."""
    if not text:
        return
    # Discord has a 2000-char hard limit on content; stay safely under it.
    await _post({"content": (text or "")[:1900]})


# Discord embed side-bar colors by event category.
# Values are 24-bit integers (0xRRGGBB).
DISCORD_COLOR_SUCCESS  = 0x16A34A  # green
DISCORD_COLOR_FAILED   = 0xDC2626  # red
DISCORD_COLOR_CANCELLED = 0x6B7280  # gray
DISCORD_COLOR_WARNING  = 0xD97706  # amber / stale / timeout
DISCORD_COLOR_INFO     = 0x2563EB  # blue (default, trigger / create / update)
DISCORD_COLOR_PROMOTE  = 0x7C3AED  # purple (promotions)
DISCORD_COLOR_RETRY    = 0xF59E0B  # yellow-orange


def _color_for_action(action: str, details: str | None = None) -> int:
    """Return the Discord embed color int for a given audit action + detail string."""
    details_lower = (details or "").lower()

    if action == "build.result":
        if "status=success" in details_lower or "status=repaired_success" in details_lower:
            return DISCORD_COLOR_SUCCESS
        if "status=failed" in details_lower or "status=ignored" in details_lower:
            return DISCORD_COLOR_FAILED
        if "status=cancelled" in details_lower:
            return DISCORD_COLOR_CANCELLED
        return DISCORD_COLOR_INFO

    if action in ("build.stale_timeout",):
        return DISCORD_COLOR_WARNING

    if action in ("build.cancel",):
        return DISCORD_COLOR_CANCELLED

    if action in ("build.retry",):
        return DISCORD_COLOR_RETRY

    if action in ("promotion.to_stable",):
        return DISCORD_COLOR_SUCCESS

    if action in ("promotion.to_testing",):
        return DISCORD_COLOR_PROMOTE

    if action in ("promotion.create",):
        return DISCORD_COLOR_PROMOTE

    if action in ("package.delete", "account.delete", "release.delete", "mock_env.delete", "schedule.delete"):
        return DISCORD_COLOR_FAILED

    # build.trigger, build.github_auto_trigger, build.github_catchup_trigger,
    # package.create/update, account.create/update, release.create/update, etc.
    return DISCORD_COLOR_INFO


async def send_discord_embed(
    title: str,
    description: str,
    fields: list[tuple[str, str]],
    color: int | None = None,
) -> None:
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
        "color": color if color is not None else DISCORD_COLOR_INFO,
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

