# ============================================================
# WAY TERO — DATABASE CONFIGURATION
# File: app/core/database.py
# Doc Ref: Backend Architecture Part 2, Section 18
# Doc Ref: DB Architecture — PostgreSQL 17, SQLAlchemy 2.x
# Async support enabled via asyncpg.
# ============================================================

from collections.abc import AsyncGenerator

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


async def drop_all_tables() -> None:
    """Drop all tables. Use only in test environments."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
