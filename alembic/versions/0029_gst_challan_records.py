"""GST Challan Records table + TDS deduction on settlement support

Revision ID: 0029_gst_challan_records
Revises: 0028
Create Date: 2026-08-04

Doc Ref:
  BRD §155 — GST Integration
  BRD §156 — TDS on partner payments (194C)
  14_Reporting_Business_Intelligence/04_FINANCIAL_REPORTING.md

Changes:
  1. gst_challan_records — persists monthly GST challan metadata (admin-side)
     platform files GST-3B/GSTR-1 monthly; this table tracks each challan.

  2. partner_gst_challans — company partner's own GST challan filing record
     Per BRD: COMPANY partners file their own GST; admin generates the
     challans from this portal. INDIVIDUAL partners → platform/admin files.

  3. tds_deduction_records — records TDS deducted at settlement time for
     COMPANY partners (Section 194C, 1% on payout).

  4. system_configurations seed:
     TDS_ENABLED — 'true' by default (TDS on company partner payouts)
     TDS_RATE    — '1' (1% u/s 194C for transporter contractors)
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_gst_challan_records"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Platform GST Challan Records (admin monthly GST filing) ─────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS gst_challan_records (
            id               BIGSERIAL PRIMARY KEY,
            month            VARCHAR(7) NOT NULL,        -- 'YYYY-MM'
            year             INTEGER    NOT NULL,
            month_number     SMALLINT   NOT NULL,        -- 1-12
            total_taxable    NUMERIC(14,2) NOT NULL DEFAULT 0,
            gst_amount       NUMERIC(14,2) NOT NULL DEFAULT 0,
            total_invoices   INTEGER    NOT NULL DEFAULT 0,
            cgst_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,  -- GST/2
            sgst_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,  -- GST/2
            challan_number   VARCHAR(100) NOT NULL,
            challan_date     DATE         NOT NULL DEFAULT CURRENT_DATE,
            challan_status   VARCHAR(20)  NOT NULL DEFAULT 'PENDING',  -- PENDING|FILED
            filed_date       DATE,
            notes            TEXT,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (month)
        )
    """))

    # ── 2. Partner GST Challans (COMPANY partner's own monthly GST) ────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_gst_challans (
            id               BIGSERIAL PRIMARY KEY,
            partner_id       BIGINT     NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            month            VARCHAR(7) NOT NULL,    -- 'YYYY-MM'
            year             INTEGER    NOT NULL,
            month_number     SMALLINT   NOT NULL,
            total_bookings   INTEGER    NOT NULL DEFAULT 0,
            taxable_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
            gst_amount       NUMERIC(14,2) NOT NULL DEFAULT 0,
            cgst_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,
            sgst_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,
            gst_number       VARCHAR(20),
            challan_number   VARCHAR(100),
            challan_date     DATE,
            challan_status   VARCHAR(20)  NOT NULL DEFAULT 'PENDING',  -- PENDING|GENERATED|FILED
            filed_date       DATE,
            notes            TEXT,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            UNIQUE (partner_id, month)
        )
    """))

    # ── 3. TDS Deduction Records (B2B partners, 194C, 1%) ──────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS tds_deduction_records (
            id                 BIGSERIAL PRIMARY KEY,
            partner_id         BIGINT     NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            cab_booking_id     BIGINT     NOT NULL REFERENCES cab_bookings(id) ON DELETE CASCADE,
            settlement_id      BIGINT,   -- future: link to settlement record
            month              VARCHAR(7) NOT NULL,   -- 'YYYY-MM' of the booking
            gross_payout       NUMERIC(14,2) NOT NULL DEFAULT 0,
            tds_rate           NUMERIC(5,2)  NOT NULL DEFAULT 1.00,
            tds_amount         NUMERIC(14,2) NOT NULL DEFAULT 0,
            net_payout         NUMERIC(14,2) NOT NULL DEFAULT 0,
            pan_number         VARCHAR(20),
            deducted_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            financial_year     VARCHAR(10)   NOT NULL,  -- e.g. '2025-26'
            quarter            VARCHAR(5)    NOT NULL,  -- 'Q1'|'Q2'|'Q3'|'Q4'
            form_26q_filed     BOOLEAN       NOT NULL DEFAULT false,
            created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            UNIQUE (cab_booking_id)
        )
    """))

    # ── 4. System config seed: TDS settings ────────────────────────────────
    conn.execute(sa.text("""
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('TDS_ENABLED',
             'true',
             'Enable TDS deduction on company (B2B) partner payouts at settlement. 1% under Section 194C.',
             NOW()),
            ('TDS_RATE',
             '1',
             'TDS rate % deducted from company partner payouts at settlement time (Section 194C = 1%).',
             NOW())
        ON CONFLICT (config_key) DO NOTHING
    """))

    print("0029_gst_challan_records: upgrade complete")


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS tds_deduction_records"))
    conn.execute(sa.text("DROP TABLE IF EXISTS partner_gst_challans"))
    conn.execute(sa.text("DROP TABLE IF EXISTS gst_challan_records"))
    conn.execute(sa.text(
        "DELETE FROM system_configurations WHERE config_key IN ('TDS_ENABLED', 'TDS_RATE')"
    ))
