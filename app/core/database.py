# ============================================================
# WAY TERO — DATABASE CONFIGURATION
# File: app/core/database.py
# Doc Ref: Backend Architecture Part 2, Section 18
# Doc Ref: DB Architecture — PostgreSQL 17, SQLAlchemy 2.x
# Async support enabled via asyncpg.
# ============================================================

from collections.abc import AsyncGenerator

import asyncio
import atexit

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import MetaData
from app.core.config import settings

# --- Naming convention for constraints (important for Alembic migrations)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """
    WayTero Base Model.
    All SQLAlchemy models must inherit from this class.
    """

    metadata = metadata


# --- Async Engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_DEBUG,  # SQL logging in dev only
    pool_pre_ping=True,  # Reconnect on stale connections
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=3600,
)

# --- Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields an async DB session per request.
    Usage: db: AsyncSession = Depends(get_db)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_all_tables() -> None:
    """Create all tables. Called on application startup (dev only)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --- Loop-safe async runner for Celery workers ───────────────────────────
# The module-level async engine pools TCP connections bound to the event
# loop that first used them. Celery's default pool executes each task on a
# NEW event loop (asyncio.run), so a pooled asyncpg connection from a
# previous run raises "Future attached to a different loop" / "Event loop
# is closed" on the next task. run_worker_async() runs the coroutine on a
# dedicated loop and disposes the engine's pooled connections afterwards,
# so every task execution starts with loop-clean connections.
_worker_loop: asyncio.AbstractEventLoop | None = None


def _dispose_engine_sync() -> None:
    """Best-effort engine disposal at interpreter exit (worker shutdown)."""
    try:
        asyncio.run(engine.dispose())
    except Exception:  # pragma: no cover - shutdown best effort
        pass


def run_worker_async(coro) -> object:
    """Run an async coroutine from synchronous Celery task code safely."""
    global _worker_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        # Already inside a loop (should not happen for Celery tasks) —
        # just await the coroutine directly.
        return coro  # pragma: no cover - defensive
    if _worker_loop is None:
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
        atexit.register(_dispose_engine_sync)
    try:
        return _worker_loop.run_until_complete(coro)
    finally:
        # Drop pooled connections after every task run so the next run
        # (or a different loop) never reuses connections bound to a dead
        # loop. New connections are created lazily on demand.
        try:
            _worker_loop.run_until_complete(engine.dispose())
        except Exception:  # pragma: no cover - disposal best effort
            pass


async def drop_all_tables() -> None:
    """Drop all tables. Use only in test environments."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
