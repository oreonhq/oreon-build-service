from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oreon_build.config import get_settings
from oreon_build.db.base import Base

_settings = get_settings()
_server_settings = {
    "application_name": _settings.database_application_name,
}
if _settings.database_idle_in_transaction_timeout_seconds > 0:
    _server_settings["idle_in_transaction_session_timeout"] = str(
        _settings.database_idle_in_transaction_timeout_seconds * 1000
    )
_engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_use_lifo=True,
    pool_recycle=1800,
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_max_overflow,
    pool_timeout=_settings.database_pool_timeout,
    connect_args={"server_settings": _server_settings},
)
async_session_maker = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncSession:
    """One-off session for startup scripts (no dependency injection)."""
    return async_session_maker()
