#!/usr/bin/env python3
# ============================================================
# WAY TERO — PARTNER DEFAULT PASSWORD SEEDER (standalone)
# File: app/scripts/seed_partner_passwords.py
# Run via: python app/scripts/seed_partner_passwords.py
# Called by: app/core/lifespan.py on every backend restart
#
# Sets "Waytero@15" (Argon2id) for PARTNER users with no password_hash.
# Sets force_password_change = TRUE for those users.
# Idempotent — only touches users with NULL/empty password_hash.
# Doc Ref: Partner Portal — default password "Waytero@15" requirement
# ============================================================
import asyncio
import os
import sys
import logging

# Ensure the Backend/ directory is in path so app.* imports work
_BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
)
# Suppress SQLAlchemy engine noise — only app-level messages should appear
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
logger = logging.getLogger("waytero.seed_partner_passwords")
DEFAULT_PARTNER_PASSWORD = "Waytero@15"


async def run():
    from argon2 import PasswordHasher
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import text
    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    AsyncSessionLocal = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    ph = PasswordHasher(
        time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16
    )

    async with AsyncSessionLocal() as db:
        # Safety guard: check if force_password_change column exists yet (migration may not have run)
        col_check = (
            await db.execute(
                text(
                    """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'force_password_change'
        """
                )
            )
        ).scalar()
        if not col_check:
            logger.warning(
                "force_password_change column not found — migration 0025 not yet applied. Skipping seeder."
            )
            await engine.dispose()
            return

        rows = (
            await db.execute(
                text(
                    """
            SELECT id FROM users
            WHERE user_type = 'PARTNER'
              AND (password_hash IS NULL OR password_hash = '')
              AND deleted_at IS NULL
        """
                )
            )
        ).fetchall()

        if not rows:
            logger.info(
                "No partners need password seeding — all already have passwords"
            )
            await engine.dispose()
            return

        count = 0
        for row in rows:
            pwd_hash = ph.hash(DEFAULT_PARTNER_PASSWORD)
            await db.execute(
                text(
                    """
                UPDATE users
                SET password_hash = :pwd_hash,
                    force_password_change = TRUE,
                    updated_at = NOW()
                WHERE id = :uid
            """
                ),
                {"pwd_hash": pwd_hash, "uid": str(row[0])},
            )
            count += 1

        await db.commit()
        logger.info(f"Seeded default password for {count} partner(s)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
