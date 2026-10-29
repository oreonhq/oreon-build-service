# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from fastapi import APIRouter, HTTPException, Request, status

from oreon_build.core.auth import create_access_token
from oreon_build.core.security import verify_password
from oreon_build.api.deps import DbSession
from oreon_build.api.schemas import LoginRequest, LoginResponse
from oreon_build.models import Account
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import time

router = APIRouter(prefix="/auth", tags=["auth"])

_login_hits: dict[str, list[float]] = {}
_LOGIN_WINDOW = 300.0
_LOGIN_MAX = 12


def login_rate_limited(ip: str, now: float | None = None) -> bool:
    stamp = time.monotonic() if now is None else now
    recent = [t for t in _login_hits.get(ip, []) if stamp - t < _LOGIN_WINDOW]
    if len(recent) >= _LOGIN_MAX:
        _login_hits[ip] = recent
        return True
    recent.append(stamp)
    _login_hits[ip] = recent
    return False


@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest, request: Request, db: DbSession):
    ip = request.client.host if request.client else "unknown"
    if login_rate_limited(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")
    result = await db.execute(
        select(Account).where(Account.username == data.username).options(selectinload(Account.role))
    )
    account = result.scalar_one_or_none()
    if not account or not account.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not verify_password(data.password, account.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_access_token(account.username, account.role.name)
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        username=account.username,
        role=account.role.name.value,
    )
