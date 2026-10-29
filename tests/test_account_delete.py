# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from oreon_build.api.routers import accounts
from oreon_build.models import Account, Role, RoleName, AuditLog


@pytest.mark.asyncio
@pytest.mark.parametrize('role', [RoleName.ADMIN, RoleName.MAINTAINER])
async def test_delete_account_preserves_referenced_history(monkeypatch, role):
    engine = create_engine('sqlite://')
    event.listen(engine, 'connect', lambda conn, _: conn.execute('PRAGMA foreign_keys=ON'))
    Account.metadata.create_all(engine)
    with Session(engine) as session:
        admin_role = Role(name=RoleName.ADMIN)
        target_role = admin_role if role == RoleName.ADMIN else Role(name=role)
        admin = Account(username='admin', password_hash='unused', role=admin_role)
        target = Account(username='target', password_hash='unused', role=target_role)
        session.add_all([admin, target])
        session.flush()
        target_id = target.id
        # Audit history is the minimal real record; other updates must still compile.
        session.add(AuditLog(account_id=target_id, action='test'))
        session.commit()

        class DB:
            async def execute(self, stmt):
                return session.execute(stmt)

            async def delete(self, obj):
                session.delete(obj)

        monkeypatch.setattr(accounts, 'log_audit', AsyncMock())
        await accounts.delete_account(target_id, Request({'type': 'http', 'headers': []}), DB(), admin)
        session.commit()
        assert session.get(Account, target_id) is None
        assert session.scalar(select(AuditLog)).account_id is None
        assert session.get(Account, admin.id) is not None
        with pytest.raises(HTTPException, match='Cannot delete self'):
            await accounts.delete_account(admin.id, Request({'type': 'http', 'headers': []}), DB(), admin)
    engine.dispose()
