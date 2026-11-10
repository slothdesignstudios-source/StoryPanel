from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from storypanel.config import get_settings

_engine: AsyncEngine | None = None
_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _maker
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url, pool_size=10, max_overflow=20, pool_pre_ping=True
        )
        _maker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_maker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _maker is not None
    return _maker


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """One unit of work: commits on success, rolls back on any exception."""
    async with session_maker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _maker
    if _engine is not None:
        await _engine.dispose()
    _engine, _maker = None, None
