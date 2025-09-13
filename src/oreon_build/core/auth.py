"""JWT and dependency injection for auth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oreon_build.config import get_settings
from oreon_build.db import get_db
from oreon_build.models import Account, RoleName

settings = get_settings()


def create_access_token(username: str, role: RoleName) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": username, "role": role.value, "exp": expire}
    return jwt.encode(
        payload,
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )


async def get_current_user_optional(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Optional[Account]:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        username = payload.get("sub")
        if not username:
            return None
    except JWTError:
        return None
    result = await db.execute(
        select(Account).where(Account.username == username).options(selectinload(Account.role))
    )
    account = result.scalar_one_or_none()
    if not account or not account.is_active:
        return None
    return account


async def require_account(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Account:
    account = await get_current_user_optional(request, db)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return account


async def require_admin(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Account:
    account = await require_account(request, db)
    if not account.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return account
