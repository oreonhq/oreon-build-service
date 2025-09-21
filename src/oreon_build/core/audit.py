"""Audit logging for actions."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oreon_build.models import Account, AuditLog, BuildJob
from oreon_build.services.discord import send_discord_embed


async def log_audit(
    db: AsyncSession,
    action: str,
    account_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    entry = AuditLog(
        account_id=account_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(entry)
    await db.flush()
    # Also emit a Discord webhook, if configured. Keep this text-only: no emojis.
    # Resolve username if possible.
    username = None
    if account_id is not None:
        result = await db.execute(select(Account).where(Account.id == account_id))
        acc = result.scalar_one_or_none()
        if acc:
            username = acc.username
    who = username or (str(account_id) if account_id is not None else "system")
    resource = f"{resource_type or '-'}#{resource_id or '-'}"
    title = action.replace(".", " / ") if action else "Oreon Build Service"
    description = details or ""

    fields = [("User", who), ("Target", resource)]

    # For build jobs, include package / release / arch for quick context in Discord.
    if resource_type == "build_job" and resource_id and str(resource_id).isdigit():
        job_id = int(str(resource_id))
        jb_result = await db.execute(
            select(BuildJob)
            .where(BuildJob.id == job_id)
            .options(
                selectinload(BuildJob.package),
                selectinload(BuildJob.release),
                selectinload(BuildJob.build_target),
            )
        )
        jb = jb_result.scalar_one_or_none()
        if jb:
            if jb.package:
                fields.append(("Package", f"{jb.package.name} (id={jb.package_id})"))
            if jb.release:
                fields.append(("Release", f"{jb.release.releasename} (id={jb.release_id})"))
            if jb.build_target and jb.build_target.architecture:
                fields.append(("Architecture", jb.build_target.architecture))

    await send_discord_embed(title=title, description=description, fields=fields)


def get_client_ip(request: Any) -> Optional[str]:
    if hasattr(request, "client") and request.client:
        return request.client.host
    forwarded = getattr(request, "headers", None) and request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return None


def get_user_agent(request: Any) -> Optional[str]:
    if getattr(request, "headers", None):
        return request.headers.get("User-Agent")
    return None
