"""Driver trip proof photos and verified Razorpay UPI QR collection.

Revision ID: 0106
Revises: 0105
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_bookings
                ADD COLUMN IF NOT EXISTS trip_start_photo_url TEXT,
                ADD COLUMN IF NOT EXISTS trip_end_photo_url TEXT
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS payment_qr_codes (
                id BIGSERIAL PRIMARY KEY,
                qr_reference VARCHAR(120) NOT NULL UNIQUE,
                master_booking_id BIGINT NOT NULL,
                cab_booking_id BIGINT,
                booking_number VARCHAR(40) NOT NULL,
                amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
                currency VARCHAR(3) NOT NULL DEFAULT 'INR',
                image_url TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                gateway_payment_id VARCHAR(120),
                paid_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                paid_at TIMESTAMPTZ
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_payment_qr_codes_booking
                ON payment_qr_codes (booking_number, created_at DESC)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS payment_qr_codes"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_bookings
                DROP COLUMN IF EXISTS trip_start_photo_url,
                DROP COLUMN IF EXISTS trip_end_photo_url
            """
        )
    )
