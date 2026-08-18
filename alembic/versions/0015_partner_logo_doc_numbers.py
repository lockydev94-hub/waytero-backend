"""Add logo_url, doc_number to partners/partner_documents; admin bank account endpoint support

Revision ID: 0015_partner_logo_doc_numbers
Revises: 0014_staff_profiles
Create Date: 2026-07-29

Doc Ref: DB Schema Part 2 — Partner §4 (partners), §9 (partner_documents), §12 (partner_bank_accounts)
"""

from alembic import op
import sqlalchemy as sa

revision = "0015_partner_logo_doc_numbers"
down_revision = "0014_staff_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── partners: add logo_url ─────────────────────────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE partners
            ADD COLUMN IF NOT EXISTS logo_url TEXT
    """))

    # ── partner_documents: add document_number for Aadhaar/PAN/GST numbers ────
    conn.execute(sa.text("""
        ALTER TABLE partner_documents
            ADD COLUMN IF NOT EXISTS document_number VARCHAR(100)
    """))

    # ── partner_bank_accounts: add account_type (SAVINGS | CURRENT) ───────────
    conn.execute(sa.text("""
        ALTER TABLE partner_bank_accounts
            ADD COLUMN IF NOT EXISTS account_type VARCHAR(20) DEFAULT 'SAVINGS'
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE partners DROP COLUMN IF EXISTS logo_url"))
    conn.execute(sa.text("ALTER TABLE partner_documents DROP COLUMN IF EXISTS document_number"))
    conn.execute(sa.text("ALTER TABLE partner_bank_accounts DROP COLUMN IF EXISTS account_type"))
