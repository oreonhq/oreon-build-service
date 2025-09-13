"""Login and token."""
from fastapi import APIRouter, HTTPException, status

from oreon_build.core.auth import create_access_token
from oreon_build.core.security import verify_password
from oreon_build.api.deps import DbSession
from oreon_build.api.schemas import LoginRequest, LoginResponse
from oreon_build.models import Account
from sqlalchemy import select
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest, db: DbSession):
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
