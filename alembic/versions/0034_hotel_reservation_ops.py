"""Add operational columns to hotel_reservations for admin lifecycle management

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-06

Doc Ref: BRD Part 4 §57-92 — Hotel Booking Lifecycle
         DB Schema Part 5 §12 — hotel_reservations

Admin actions (confirm, check-in, check-out, cancel, no-show) need to store
their results on the hotel_reservations row. These columns mirror what
hotel_bookings has, added here idempotently so existing rows are unaffected.
"""

from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        ALTER TABLE hotel_reservations
            ADD COLUMN IF NOT EXISTS hotel_confirmation_number VARCHAR(100),
            ADD COLUMN IF NOT EXISTS cancellation_reason        TEXT,
            ADD COLUMN IF NOT EXISTS cancellation_charge        NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            ADD COLUMN IF NOT EXISTS refund_amount              NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            ADD COLUMN IF NOT EXISTS check_in_id_proof          VARCHAR(100),
            ADD COLUMN IF NOT EXISTS check_in_id_number         VARCHAR(100),
            ADD COLUMN IF NOT EXISTS actual_check_in_at         TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS actual_check_out_at        TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS check_out_notes            TEXT
    """))


def downgrade():
    op.execute(sa.text("""
        ALTER TABLE hotel_reservations
            DROP COLUMN IF EXISTS hotel_confirmation_number,
            DROP COLUMN IF EXISTS cancellation_reason,
            DROP COLUMN IF EXISTS cancellation_charge,
            DROP COLUMN IF EXISTS refund_amount,
            DROP COLUMN IF EXISTS check_in_id_proof,
            DROP COLUMN IF EXISTS check_in_id_number,
            DROP COLUMN IF EXISTS actual_check_in_at,
            DROP COLUMN IF EXISTS actual_check_out_at,
            DROP COLUMN IF EXISTS check_out_notes
    """))
