from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from oreon_build.config import get_settings

logger = logging.getLogger(__name__)


async def _post_layer_callback(layer_plan_id: int, status: str, error_message: str | None) -> None:
    settings = get_settings()
    url = (settings.ci_callback_url or "").strip()
    if not url:
        return
    payload: dict[str, Any] = {"layer_plan_id": layer_plan_id, "status": status}
    if error_message:
        payload["error_message"] = error_message
    headers: dict[str, str] = {}
    secret = (settings.ci_callback_secret or "").strip()
    if secret:
        headers["X-CI-Callback-Secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 300:
                logger.warning(
                    "CI layer callback non-2xx layer_plan_id=%s status=%s code=%s",
                    layer_plan_id, status, r.status_code,
                )
    except Exception:
        logger.exception("CI layer callback failed layer_plan_id=%s status=%s", layer_plan_id, status)


def notify_layer_plan_terminal(layer_plan_id: int, status: str, error_message: str | None = None) -> None:
    url = (get_settings().ci_callback_url or "").strip()
    if not url:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_post_layer_callback(layer_plan_id, status, error_message))
    except RuntimeError:
        logger.warning("CI layer callback skipped (no running loop) layer_plan_id=%s", layer_plan_id)
