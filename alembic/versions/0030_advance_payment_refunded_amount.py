"""Track refunded amount per advance payment

Revision ID: 0030_advance_refunded_amount
Revises: 0029_gst_challan_records
Create Date: 2026-08-04

Doc Ref:
  BRD Part 6 §151-153 — refund management
  Payment API §15 — initiate refund
  admin/payment_api.py — POST /admin/payments/refund

Why:
  A partial refund previously voided the whole advance, destroying the
  un-refunded balance. Partial refunds now leave the advance ACTIVE, so the
  cumulative refunded amount must be tracked per row to block over-refunding.

  Back-fill: advances voided with a 'REFUND:' reason were full refunds, so
  their refunded_amount equals the advance amount.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_advance_refunded_amount"
down_revision = "0029_gst_challan_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        ALTER TABLE advance_payments
        ADD COLUMN IF NOT EXISTS refunded_amount NUMERIC(14, 2) NOT NULL DEFAULT 0
    """))

    conn.execute(sa.text("""
        UPDATE advance_payments
        SET refunded_amount = amount
        WHERE status = 'VOIDED'
          AND void_reason LIKE 'REFUND:%'
          AND refunded_amount = 0
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE advance_payments DROP COLUMN IF EXISTS refunded_amount
    """))
