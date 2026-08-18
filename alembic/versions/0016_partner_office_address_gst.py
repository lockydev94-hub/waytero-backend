"""Add office_address fields to partners + ensure gst_details completeness

Revision ID: 0016_partner_office_address_gst
Revises: 0015_partner_logo_doc_numbers
Create Date: 2026-07-29

Doc Ref:
  DB Schema Part 2 §4  — partners table (office_address fields)
  DB Schema Part 2 §11 — partner_gst_details (legal_name, trade_name required for COMPANY)
  BRD Part 2 §20 — Partner Profile: Business Information includes Address
  BRD Part 2 §18 — Company Partner: must provide GST Certificate

Changes:
  partners:
    + office_address_line_1  VARCHAR(255)   # street / building
    + office_address_line_2  VARCHAR(255)   # area / landmark (optional)
    + office_city_id         BIGINT FK→cities.id  # must match partners.city_id
    + office_state_id        BIGINT FK→states.id
    + office_postal_code     VARCHAR(20)

  partner_gst_details:
    + pan_number  VARCHAR(20)   # PAN is always required (Individual + Company)

Note: office_city_id must match partners.city_id — enforced at application layer.
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_partner_office_address_gst"
down_revision = "0015_partner_logo_doc_numbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── partners: office address fields ───────────────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE partners
            ADD COLUMN IF NOT EXISTS office_address_line_1 VARCHAR(255),
            ADD COLUMN IF NOT EXISTS office_address_line_2 VARCHAR(255),
            ADD COLUMN IF NOT EXISTS office_city_id        BIGINT
                REFERENCES cities(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS office_state_id       BIGINT
                REFERENCES states(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS office_postal_code    VARCHAR(20)
    """))

    # ── partner_gst_details: pan_number for both individual & company ─────────
    conn.execute(sa.text("""
        ALTER TABLE partner_gst_details
            ADD COLUMN IF NOT EXISTS pan_number VARCHAR(20)
    """))

    # ── index for office city lookups ─────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_partner_office_city
            ON partners (office_city_id)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_partner_office_city"))
    conn.execute(sa.text("ALTER TABLE partners DROP COLUMN IF EXISTS office_address_line_1"))
    conn.execute(sa.text("ALTER TABLE partners DROP COLUMN IF EXISTS office_address_line_2"))
    conn.execute(sa.text("ALTER TABLE partners DROP COLUMN IF EXISTS office_city_id"))
    conn.execute(sa.text("ALTER TABLE partners DROP COLUMN IF EXISTS office_state_id"))
    conn.execute(sa.text("ALTER TABLE partners DROP COLUMN IF EXISTS office_postal_code"))
    conn.execute(sa.text("ALTER TABLE partner_gst_details DROP COLUMN IF EXISTS pan_number"))
