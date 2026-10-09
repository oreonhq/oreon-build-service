from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from oreon_build.api.deps import CurrentAdmin, CurrentUser, DbSession
from oreon_build.api.schemas import AccountCreate, AccountResponse, AccountUpdate, PasswordChange
from oreon_build.core.audit import get_client_ip, get_user_agent, log_audit
from oreon_build.core.security import hash_password, verify_password
from oreon_build.models import Account, Role, RoleName
from oreon_build.models import (
    AuditLog, BuildJob, LayerPlan, Promotion, SideTag, SideTagMember,
    CveMatch, SecurityAdvisory, ReleaseMonitorScan,
)

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts(db: DbSession, _: CurrentAdmin):
    result = await db.execute(
        select(Account).options(selectinload(Account.role)).order_by(Account.username)
    )
    accounts = result.scalars().all()
    return [
        AccountResponse(
            id=a.id,
            username=a.username,
            role=a.role.name.value,
            is_active=a.is_active,
            created_at=a.created_at,
        )
        for a in accounts
    ]


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    data: AccountCreate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(select(Account).where(Account.username == data.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")
    result = await db.execute(select(Role).where(Role.name == RoleName(data.role)))
    role = result.scalar_one()
    account = Account(
        username=data.username,
        password_hash=hash_password(data.password),
        role_id=role.id,
        is_active=True,
    )
    db.add(account)
    await db.flush()
    await log_audit(
        db,
        "account.create",
        account_id=admin.id,
        resource_type="account",
        resource_id=str(account.id),
        details=data.username,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return AccountResponse(
        id=account.id,
        username=account.username,
        role=role.name.value,
        is_active=account.is_active,
        created_at=account.created_at,
    )


@router.post("/me/password")
async def change_own_password(
    data: PasswordChange,
    request: Request,
    db: DbSession,
    user: CurrentUser,
):
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is wrong")
    if data.current_password == data.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different")
    user.password_hash = hash_password(data.new_password)
    await log_audit(
        db,
        "account.password_change",
        account_id=user.id,
        resource_type="account",
        resource_id=str(user.id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.flush()
    return {"status": "ok"}


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(account_id: int, db: DbSession, _: CurrentAdmin):
    result = await db.execute(
        select(Account).where(Account.id == account_id).options(selectinload(Account.role))
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return AccountResponse(
        id=account.id,
        username=account.username,
        role=account.role.name.value,
        is_active=account.is_active,
        created_at=account.created_at,
    )


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: int,
    data: AccountUpdate,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(
        select(Account).where(Account.id == account_id).options(selectinload(Account.role))
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if data.password is not None:
        account.password_hash = hash_password(data.password)
    if data.role is not None:
        r = await db.execute(select(Role).where(Role.name == RoleName(data.role)))
        account.role_id = r.scalar_one().id
    if data.is_active is not None:
        account.is_active = data.is_active
    await log_audit(
        db,
        "account.update",
        account_id=admin.id,
        resource_type="account",
        resource_id=str(account_id),
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    return AccountResponse(
        id=account.id,
        username=account.username,
        role=account.role.name.value,
        is_active=account.is_active,
        created_at=account.created_at,
    )


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    request: Request,
    db: DbSession,
    admin: CurrentAdmin,
):
    result = await db.execute(
        select(Account).where(Account.id == account_id).options(selectinload(Account.role))
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if account.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete self")
    # Preserve build/security history while removing references to the login.
    # Memberships are permissions and must disappear with the account.
    await db.execute(delete(SideTagMember).where(SideTagMember.account_id == account_id))
    for column in (
        AuditLog.account_id, BuildJob.triggered_by_id, LayerPlan.triggered_by_id,
        Promotion.promoted_by_id, SideTag.owner_account_id,
        CveMatch.handled_by_account_id, SecurityAdvisory.handled_by_account_id,
        SecurityAdvisory.created_by_account_id, ReleaseMonitorScan.created_by_account_id,
    ):
        await db.execute(
            update(column.class_).where(column == account_id).values({column: None})
        )
    await db.delete(account)
    await log_audit(
        db,
        "account.delete",
        account_id=admin.id,
        resource_type="account",
        resource_id=str(account_id),
        details=account.username,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
