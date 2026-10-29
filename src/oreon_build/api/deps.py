# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from oreon_build.core.auth import get_current_user_optional, require_account, require_admin
from oreon_build.db import get_db
from oreon_build.models import Account

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUserOptional = Annotated[Account | None, Depends(get_current_user_optional)]
CurrentUser = Annotated[Account, Depends(require_account)]
CurrentAdmin = Annotated[Account, Depends(require_admin)]
