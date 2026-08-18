"""
WayTero — Wallet Seeder (idempotent)
=====================================
Run on every backend restart via lifespan.py to ensure ALL existing
partners and customers have a wallet row (balance = 0).

Uses ON CONFLICT DO NOTHING — completely safe to run repeatedly.

Also callable directly:
    cd Backend/
    python scripts/seed_wallets.py
"""
import asyncio
import os
import sys

# Ensure UTF-8 output even on Windows CP1252 terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Add Backend/ root to sys.path so app.* imports work ──────────────────────
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

# ── Load .env BEFORE importing app config so DATABASE_URL is set ─────────────
_env_file = os.path.join(_BACKEND_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

from sqlalchemy import text
from app.core.database import AsyncSessionLocal


async def seed_wallets() -> None:
    async with AsyncSessionLocal() as db:

        # ── 1. Partner wallets (table wallets) ────────────────────────────────
        result_p = await db.execute(text("""
            INSERT INTO wallets
                (partner_id, wallet_type, available_balance,
                 hold_balance, credit_limit, wallet_status, created_at)
            SELECT p.id, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW()
            FROM   partners p
            WHERE  NOT EXISTS (
                SELECT 1 FROM wallets w WHERE w.partner_id = p.id
            )
            ON CONFLICT (partner_id) DO NOTHING
        """))
        partner_count = result_p.rowcount

        # ── 2. Customer wallets ───────────────────────────────────────────────
        # Guard: table may not exist yet if migration 0023 hasn't run yet.
        try:
            result_c = await db.execute(text("""
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance,
                     wallet_status, created_at, updated_at)
                SELECT c.id, 0, 0, 'ACTIVE', NOW(), NOW()
                FROM   customers c
                WHERE  NOT EXISTS (
                    SELECT 1 FROM customer_wallets cw WHERE cw.customer_id = c.id
                )
                ON CONFLICT (customer_id) DO NOTHING
            """))
            customer_count = result_c.rowcount
        except Exception as exc:
            # Table not yet created — Alembic migration will create it.
            customer_count = 0
            print(f"[seed_wallets] customer_wallets not ready yet: {exc}")

        await db.commit()

    print(
        f"[seed_wallets] [OK] Partners: {partner_count} new wallet(s) created | "
        f"Customers: {customer_count} new wallet(s) created"
    )


if __name__ == "__main__":
    asyncio.run(seed_wallets())
