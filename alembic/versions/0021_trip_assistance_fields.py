"""Trip Assistance — admin-initiated trip start/end fields on cab_bookings

Revision ID: 0021_trip_assistance_fields
Revises: 0020_coupon_system
Create Date: 2026-07-31

Adds to cab_bookings:
  trip_start_km         — odometer reading at trip start (admin-assisted)
  trip_started_at       — datetime when admin started trip
  trip_end_km           — odometer at trip end (admin-assisted close)
  trip_ended_at         — datetime when admin closed trip
  actual_distance       — computed km (trip_end_km - trip_start_km)
  payment_mode          — CASH | ONLINE | WALLET (how customer paid driver)
  payment_collected_by  — DRIVER | PARTNER | PLATFORM
  cash_pending_at       — DRIVER | PARTNER | NONE (where cash is sitting)
  platform_commission   — commission amount deducted by platform
  partner_payout        — net amount to partner after commission
  invoice_url           — Cloudinary URL of generated invoice PDF
  invoice_number        — unique invoice number

Doc Ref: BRD Part 3 §43-45, BRD Part 6 §147, DB Schema Part 4 §7
"""

from alembic import op
import sqlalchemy as sa

revision = "0021_trip_assistance_fields"
down_revision = "0020_coupon_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Add trip assistance columns to cab_bookings
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS trip_start_km         NUMERIC(12,2),
            ADD COLUMN IF NOT EXISTS trip_started_at        TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS trip_end_km            NUMERIC(12,2),
            ADD COLUMN IF NOT EXISTS trip_ended_at          TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS actual_distance        NUMERIC(12,2),
            ADD COLUMN IF NOT EXISTS payment_mode           VARCHAR(20),
            ADD COLUMN IF NOT EXISTS payment_collected_by   VARCHAR(20),
            ADD COLUMN IF NOT EXISTS cash_pending_at        VARCHAR(20)  DEFAULT 'NONE',
            ADD COLUMN IF NOT EXISTS platform_commission    NUMERIC(14,2),
            ADD COLUMN IF NOT EXISTS partner_payout         NUMERIC(14,2),
            ADD COLUMN IF NOT EXISTS invoice_url            TEXT,
            ADD COLUMN IF NOT EXISTS invoice_number         VARCHAR(100)
    """))

    # Index on invoice_number for lookup
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cab_invoice_number
        ON cab_bookings(invoice_number)
        WHERE invoice_number IS NOT NULL
    """))

    print("0021_trip_assistance_fields: upgrade complete")


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            DROP COLUMN IF EXISTS trip_start_km,
            DROP COLUMN IF EXISTS trip_started_at,
            DROP COLUMN IF EXISTS trip_end_km,
            DROP COLUMN IF EXISTS trip_ended_at,
            DROP COLUMN IF EXISTS actual_distance,
            DROP COLUMN IF EXISTS payment_mode,
            DROP COLUMN IF EXISTS payment_collected_by,
            DROP COLUMN IF EXISTS cash_pending_at,
            DROP COLUMN IF EXISTS platform_commission,
            DROP COLUMN IF EXISTS partner_payout,
            DROP COLUMN IF EXISTS invoice_url,
            DROP COLUMN IF EXISTS invoice_number
    """))
