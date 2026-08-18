"""GST / Tax support on cab_bookings + system_configurations

Revision ID: 0024_gst_tax_on_bookings
Revises: 0023_customer_wallet
Create Date: 2026-08-02

Doc Ref:
  BRD Part 6 §155 — GST Integration (configurable)
  SRS Part 7 §282-285 — GST Engine
  SRS Part 2 §46-47 — Partner types (INDIVIDUAL / COMPANY)
  DB Schema Part 4 §7 — cab_bookings

Changes:
  system_configurations — seed new GST control keys:
    GST_ENABLED         — 'true' | 'false'  toggle
    GST_RATE            — customer-facing GST % on cab fare (default 5)
    COMMISSION_GST_RATE — GST % on platform commission from partners (default 18)

  cab_bookings — add tax columns:
    gst_rate            NUMERIC(5,2)   — GST % applied at booking/invoice time
    gst_amount          NUMERIC(14,2)  — GST rupees on fare (fare × gst_rate / 100)
    is_tax_invoice      BOOLEAN        — true = tax invoice; false = non-tax / receipt

  All EXISTING cab_bookings rows:
    gst_rate       = 0
    gst_amount     = 0
    is_tax_invoice = false
  (Back-dated as non-tax bookings per requirement)

Note:
  GST_PERCENTAGE (seeded in 0003) is the LEGACY key used for platform commission GST.
  We keep it and also add COMMISSION_GST_RATE pointing to the same default (18)
  so that the Settings page can surface them separately.
  The close-trip + collect-payment flows read GST_ENABLED + GST_RATE at runtime.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_gst_tax_on_bookings"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. New system_configuration keys ─────────────────────────────────────
    conn.execute(sa.text("""
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('GST_ENABLED',
             'false',
             'Enable GST on cab fares. When true, GST_RATE % is added to the fare and shown on invoice.',
             NOW()),
            ('GST_RATE',
             '5',
             'GST percentage charged on cab fare to customer (Indian GST for transport = 5%)',
             NOW()),
            ('COMMISSION_GST_RATE',
             '18',
             'GST % on platform commission charged to partners (default 18%). Separate from customer-facing fare GST.',
             NOW())
        ON CONFLICT (config_key) DO NOTHING
    """))

    # ── 2. Add tax columns to cab_bookings ────────────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS gst_rate       NUMERIC(5,2)  DEFAULT 0,
            ADD COLUMN IF NOT EXISTS gst_amount     NUMERIC(14,2) DEFAULT 0,
            ADD COLUMN IF NOT EXISTS is_tax_invoice BOOLEAN       DEFAULT false
    """))

    # ── 3. Back-fill ALL existing rows as non-tax bookings ────────────────────
    conn.execute(sa.text("""
        UPDATE cab_bookings
        SET
            gst_rate       = 0,
            gst_amount     = 0,
            is_tax_invoice = false
        WHERE gst_rate IS NULL OR is_tax_invoice IS NULL
    """))

    print("0024_gst_tax_on_bookings: upgrade complete — all existing bookings marked non-tax")


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            DROP COLUMN IF EXISTS gst_rate,
            DROP COLUMN IF EXISTS gst_amount,
            DROP COLUMN IF EXISTS is_tax_invoice
    """))
    conn.execute(sa.text("""
        DELETE FROM system_configurations
        WHERE config_key IN ('GST_ENABLED', 'GST_RATE', 'COMMISSION_GST_RATE')
    """))
