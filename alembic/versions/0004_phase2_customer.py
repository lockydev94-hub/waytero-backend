"""Phase 2 - Customer tables: customers, customer_addresses

Revision ID: 0004_phase2_customer
Revises: 0003_phase2_master
Create Date: 2026-06-02

Doc Ref: DB Schema Part 2 — Customer (Sections 2, 3)
Phase: 2 — Customer Module
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_phase2_customer"
down_revision = "0003_phase2_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ============================================================
    # CUSTOMERS TABLE — Doc Ref: Section 2
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customers (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            customer_code VARCHAR(50) NOT NULL UNIQUE,
            first_name VARCHAR(150),
            last_name VARCHAR(150),
            gender VARCHAR(20),
            date_of_birth DATE,
            city_id BIGINT REFERENCES cities(id),
            referral_code VARCHAR(50),
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMP WITH TIME ZONE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_customers_city ON customers(city_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_customers_active ON customers(is_active)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_customers_referral ON customers(referral_code)"))

    # ============================================================
    # CUSTOMER ADDRESSES — Doc Ref: Section 3
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS customer_addresses (
            id BIGSERIAL PRIMARY KEY,
            customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            address_type VARCHAR(50),
            address_line_1 VARCHAR(255),
            address_line_2 VARCHAR(255),
            city_id BIGINT REFERENCES cities(id),
            state_id BIGINT REFERENCES states(id),
            postal_code VARCHAR(20),
            latitude NUMERIC(10,7),
            longitude NUMERIC(10,7),
            is_default BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_customer_addr_customer ON customer_addresses(customer_id)"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS customer_addresses CASCADE"))
    conn.execute(sa.text("DROP TABLE IF EXISTS customers CASCADE"))
