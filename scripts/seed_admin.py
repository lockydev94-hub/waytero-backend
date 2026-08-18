# ============================================================
# WAYTERO — SEED SUPER ADMIN
# File: scripts/seed_admin.py
# Run: python -m scripts.seed_admin  (from Backend/ dir)
# Creates a SUPER_ADMIN user with a known dev password.
# Safe to re-run — skips if admin already exists.
# ============================================================

import asyncio
import sys
import os

# Ensure app package is importable when run from Backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.security import hash_password

# ── Dev credentials (printed on run, shown in login UI in dev mode) ──
ADMIN_MOBILE   = "9000000001"
ADMIN_PASSWORD = "Admin@WayTero1"   # meets: 12+ chars, upper, lower, digit, special
ADMIN_FIRST    = "Super"
ADMIN_LAST     = "Admin"
ADMIN_EMAIL    = "admin@waytero.dev"


async def seed():
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # Check if admin already exists
        result = await db.execute(
            text("SELECT id FROM users WHERE mobile_number = :mobile AND user_type = 'SUPER_ADMIN'"),
            {"mobile": ADMIN_MOBILE},
        )
        existing = result.fetchone()
        if existing:
            print(f"[seed_admin] SUPER_ADMIN already exists (mobile: {ADMIN_MOBILE}). Skipping.")
            await engine.dispose()
            return

        # Fetch SUPER_ADMIN role id
        role_result = await db.execute(
            text("SELECT id FROM roles WHERE role_code = 'SUPER_ADMIN'")
        )
        role_row = role_result.fetchone()
        if not role_row:
            print("[seed_admin] ERROR: SUPER_ADMIN role not found. Run migrations first (alembic upgrade head).")
            await engine.dispose()
            sys.exit(1)
        role_id = role_row[0]

        user_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        password_hash = hash_password(ADMIN_PASSWORD)

        # Insert user
        await db.execute(
            text("""
                INSERT INTO users (
                    id, user_code, first_name, last_name, mobile_number,
                    email, password_hash, user_type, status,
                    is_mobile_verified, is_email_verified, is_active,
                    created_at, updated_at
                ) VALUES (
                    :id, :user_code, :first_name, :last_name, :mobile_number,
                    :email, :password_hash, 'SUPER_ADMIN', 'ACTIVE',
                    TRUE, TRUE, TRUE,
                    :created_at, :updated_at
                )
            """),
            {
                "id": user_id,
                "user_code": "ADMIN-0001",
                "first_name": ADMIN_FIRST,
                "last_name": ADMIN_LAST,
                "mobile_number": ADMIN_MOBILE,
                "email": ADMIN_EMAIL,
                "password_hash": password_hash,
                "created_at": now,
                "updated_at": now,
            },
        )

        # Assign SUPER_ADMIN role
        await db.execute(
            text("""
                INSERT INTO user_roles (id, user_id, role_id, assigned_at)
                VALUES (:id, :user_id, :role_id, :assigned_at)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "role_id": role_id,
                "assigned_at": now,
            },
        )

        await db.commit()

    await engine.dispose()

    print("=" * 56)
    print("  WayTero - SUPER ADMIN SEEDED SUCCESSFULLY")
    print("=" * 56)
    print(f"  Mobile   : {ADMIN_MOBILE}")
    print(f"  Password : {ADMIN_PASSWORD}")
    print(f"  Email    : {ADMIN_EMAIL}")
    print(f"  Role     : SUPER_ADMIN")
    print("=" * 56)
    print("  Login: POST /api/v1/auth/admin/login")
    print("  Step 1: submit mobile + password")
    print("  Step 2: OTP will appear in backend console logs")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(seed())
