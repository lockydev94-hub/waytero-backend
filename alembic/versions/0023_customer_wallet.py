"""Customer wallet tables + auto-create wallets for existing partners/customers

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-02

Creates:
  - customer_wallets  (mirrors wallets but for customers)
  - customer_wallet_ledger
  - Back-fills wallets rows for existing partners missing a wallet
  - Back-fills customer_wallets rows for existing customers missing a wallet
"""

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022_pricing_formula_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Customer wallet table ──────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_wallets (
            id                BIGSERIAL PRIMARY KEY,
            customer_id       BIGINT NOT NULL UNIQUE REFERENCES customers(id),
            available_balance NUMERIC(14,2) NOT NULL DEFAULT 0,
            hold_balance      NUMERIC(14,2) NOT NULL DEFAULT 0,
            wallet_status     VARCHAR(50)   NOT NULL DEFAULT 'ACTIVE',
            created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_customer_wallet_cid ON customer_wallets(customer_id)
    """))

    # ── 2. Customer wallet ledger (immutable audit trail) ─────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_wallet_ledger (
            id                      BIGSERIAL PRIMARY KEY,
            customer_wallet_id      BIGINT NOT NULL REFERENCES customer_wallets(id),
            transaction_reference   VARCHAR(100),
            reference_type          VARCHAR(50),
            debit_amount            NUMERIC(14,2) NOT NULL DEFAULT 0,
            credit_amount           NUMERIC(14,2) NOT NULL DEFAULT 0,
            balance_after           NUMERIC(14,2),
            narration               TEXT,
            created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cust_ledger_wallet
            ON customer_wallet_ledger(customer_wallet_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cust_ledger_ref
            ON customer_wallet_ledger(transaction_reference)
    """))

    # ── 3. Back-fill: create wallets for existing partners missing one ─────────
    conn.execute(sa.text("""
        INSERT INTO wallets (partner_id, wallet_type, available_balance,
                             hold_balance, credit_limit, wallet_status, created_at)
        SELECT p.id, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW()
        FROM partners p
        WHERE NOT EXISTS (SELECT 1 FROM wallets w WHERE w.partner_id = p.id)
    """))

    # ── 4. Back-fill: create customer_wallets for existing customers ───────────
    conn.execute(sa.text("""
        INSERT INTO customer_wallets (customer_id, available_balance,
                                      hold_balance, wallet_status, created_at, updated_at)
        SELECT c.id, 0, 0, 'ACTIVE', NOW(), NOW()
        FROM customers c
        WHERE NOT EXISTS (SELECT 1 FROM customer_wallets cw WHERE cw.customer_id = c.id)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_wallet_ledger CASCADE"))
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_wallets CASCADE"))
