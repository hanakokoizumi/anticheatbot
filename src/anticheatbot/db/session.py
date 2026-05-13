from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("engine not configured")
    return _session_factory


def configure_engine(database_url: str) -> AsyncEngine:
    global _engine, _session_factory
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    _engine = create_async_engine(database_url, echo=False, connect_args=connect_args)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("engine not configured")
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("engine not configured")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    from anticheatbot.db.models import Base

    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
