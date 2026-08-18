"""Backfill partner_bank_accounts verification_status APPROVED → VERIFIED

Revision ID: 0032
Revises: 0031
Create Date: 2024-08-06

Doc Ref: This fixes a value-mismatch bug. The hotel readiness check and repository
          query expected verification_status='VERIFIED', but the admin verify endpoint
          wrote 'APPROVED'. Standardized on 'VERIFIED' everywhere; this migration
          backfills existing approved rows so they count toward hotel readiness.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0032_bank_verified"
down_revision = "0031_hotel_module"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Backfill: any partner_bank_accounts row with verification_status='APPROVED'
    # becomes 'VERIFIED' so it satisfies hotel readiness checks.
    result = conn.execute(
        sa.text("""
            UPDATE partner_bank_accounts
            SET verification_status = 'VERIFIED'
            WHERE verification_status = 'APPROVED'
        """)
    )
    print(f"  Backfilled {result.rowcount} bank account(s) from APPROVED to VERIFIED")


def downgrade():
    # Reverting would turn VERIFIED back to APPROVED, but the endpoint now writes
    # VERIFIED going forward, so a true rollback is lossy. We'll do it anyway for
    # completeness, but note that any NEW verifications after this migration deployed
    # will also become APPROVED on downgrade.
    conn = op.get_bind()
    conn.execute(
        sa.text("""
            UPDATE partner_bank_accounts
            SET verification_status = 'APPROVED'
            WHERE verification_status = 'VERIFIED'
        """)
    )
