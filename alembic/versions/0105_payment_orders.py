"""customer Razorpay payment orders

Revision ID: 0105
Revises: 0104
Create Date: 2026-11-17

A gateway order is not a payment. It is the attempt: what was priced, which
booking it belongs to, and whether Razorpay ever captured it. Keeping it in its
own table leaves the receipt tables (advance_payments / hotel_advance_payments /
tour_advance_payments) as the record of money actually held, which is the same
contract the admin and partner panels already read.

The order reference is Razorpay's own `order_...` id, unique so a duplicate
verify or a webhook replay can be answered from the row rather than charging
twice.
"""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS payment_orders (
                id BIGSERIAL PRIMARY KEY,
                order_reference VARCHAR(120) NOT NULL UNIQUE,
                master_booking_id BIGINT NOT NULL
                    REFERENCES master_bookings(id) ON DELETE CASCADE,
                customer_id BIGINT NOT NULL
                    REFERENCES customers(id) ON DELETE CASCADE,
                booking_number VARCHAR(100) NOT NULL,
                amount NUMERIC(14,2) NOT NULL,
                currency VARCHAR(10) NOT NULL DEFAULT 'INR',
                payment_type VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'CREATED',
                gateway_payment_id VARCHAR(120),
                receipt_number VARCHAR(80),
                failure_reason TEXT,
                gateway_response JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT ck_payment_orders_type
                    CHECK (payment_type IN ('FULL', 'ADVANCE')),
                CONSTRAINT ck_payment_orders_status
                    CHECK (status IN ('CREATED', 'PAID', 'FAILED')),
                CONSTRAINT ck_payment_orders_amount
                    CHECK (amount > 0)
            )
            """
        )
    )

    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_booking "
            "ON payment_orders (master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_customer "
            "ON payment_orders (customer_id, created_at DESC)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_status "
            "ON payment_orders (status)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS payment_orders"))
