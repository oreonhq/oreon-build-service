from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

import httpx

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)

_DISCORD_TIMEOUT = httpx.Timeout(30.0, connect=20.0)

async def _post(payload: dict, webhook_url: str | None = None) -> None:
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
    if not text:
        return
    await _post({"content": (text or "")[:1900]})

DISCORD_COLOR_SUCCESS  = 0x16A34A
DISCORD_COLOR_FAILED   = 0xDC2626
DISCORD_COLOR_CANCELLED = 0x6B7280
DISCORD_COLOR_WARNING  = 0xD97706
DISCORD_COLOR_INFO     = 0x2563EB
DISCORD_COLOR_PROMOTE  = 0x7C3AED
DISCORD_COLOR_RETRY    = 0xF59E0B

def _color_for_action(action: str, details: str | None = None) -> int:
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

    return DISCORD_COLOR_INFO

async def send_discord_embed(
    title: str,
    description: str,
    fields: list[tuple[str, str]],
    color: int | None = None,
) -> None:
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
                "value": value[:900],
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
    title: str,
    description: str,
    fields: list[tuple[str, str]],
    *,
    color: int | None = None,
) -> None:
    settings = get_settings()
    url = getattr(settings, "security_discord_webhook_url", None)
    if not url:
        return
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
        pass

    embed_fields = []
    for name, value in fields:
        if not value:
            continue
        embed_fields.append({"name": name, "value": value[:900], "inline": False})
    embed_color = color if color is not None else 0xB91C1C
    embed = {
        "title": title[:256] if title else "Oreon Security Advisory",
        "description": (description or "")[:1000],
        "color": embed_color,
        "fields": embed_fields,
    }
    await _post({"embeds": [embed]}, webhook_url=url)

