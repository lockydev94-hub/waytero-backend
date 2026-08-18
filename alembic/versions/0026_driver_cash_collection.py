"""Driver cash collection — partner collects cash held by their drivers

Revision ID: 0026
Revises: 0025

Creates:
  - driver_cash_collections       (one row per collection event)
  - driver_cash_collection_items  (the cab_bookings settled by that event)
  - cab_bookings.cash_amount_due  (rupees the driver actually took in hand)

Background:
  When a driver takes CASH from the customer, cab_bookings.cash_pending_at is
  set to 'DRIVER' (partner/booking_api.py collect-payment). Nothing in the
  system moved it off 'DRIVER' — the partner had no way to record that the
  driver handed the money over. These tables record that handover, and the
  collection flips cash_pending_at 'DRIVER' -> 'PARTNER'.

  'PARTNER' is already treated as settled by the admin trip-assistance
  attention bucket, so collected bookings correctly drop out of it.
"""

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Cash actually handed to the driver = fare − coupon − advance. That figure
    # is not derivable later: master_bookings.total_paid_amount is overwritten
    # with the full fare at payment time, so it must be stored when collected.
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS cash_amount_due NUMERIC(14,2)
    """))

    # Back-fill existing DRIVER-pending rows. Coupon/advance were both zero for
    # every one of them, so final_amount is the correct cash figure.
    conn.execute(sa.text("""
        UPDATE cab_bookings cb
        SET cash_amount_due = GREATEST(
            0,
            COALESCE(cb.final_amount, cb.estimated_amount, 0)
            - COALESCE((
                SELECT SUM(cu.discount_applied) FROM coupon_usages cu
                WHERE cu.master_booking_id = cb.master_booking_id
            ), 0)
        )
        WHERE cb.payment_mode = 'CASH' AND cb.cash_amount_due IS NULL
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS driver_cash_collections (
            id                BIGSERIAL PRIMARY KEY,
            collection_number VARCHAR(50)  NOT NULL UNIQUE,
            partner_id        BIGINT       NOT NULL REFERENCES partners(id),
            driver_id         BIGINT       NOT NULL REFERENCES drivers(id),
            bookings_count    INTEGER      NOT NULL DEFAULT 0,
            expected_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
            collected_amount  NUMERIC(14,2) NOT NULL DEFAULT 0,
            payment_method    VARCHAR(20)  NOT NULL DEFAULT 'CASH',
            status            VARCHAR(20)  NOT NULL DEFAULT 'COLLECTED',
            reference_note    TEXT,
            collected_by_user_id UUID,
            collected_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            created_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_dcc_partner_driver
            ON driver_cash_collections(partner_id, driver_id)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_dcc_collected_at
            ON driver_cash_collections(collected_at DESC)
    """))

    # One row per booking settled by a collection. UNIQUE on cab_booking_id is
    # the hard guarantee that the same cash can never be collected twice.
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS driver_cash_collection_items (
            id             BIGSERIAL PRIMARY KEY,
            collection_id  BIGINT NOT NULL REFERENCES driver_cash_collections(id) ON DELETE CASCADE,
            cab_booking_id BIGINT NOT NULL UNIQUE REFERENCES cab_bookings(id),
            booking_number VARCHAR(50)   NOT NULL,
            amount         NUMERIC(14,2) NOT NULL DEFAULT 0,
            created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_dcci_collection
            ON driver_cash_collection_items(collection_id)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS driver_cash_collection_items"))
    conn.execute(sa.text("DROP TABLE IF EXISTS driver_cash_collections"))
    conn.execute(sa.text("ALTER TABLE cab_bookings DROP COLUMN IF EXISTS cash_amount_due"))
