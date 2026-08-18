"""Advance payments — money collected from the customer before trip end

Revision ID: 0027
Revises: 0026

Creates:
  - advance_payments  (one ACTIVE row per cab booking)

Background:
  Until now "advance" was not a stored concept. Every advance_paid in the
  application was an alias for master_bookings.total_paid_amount — a bare
  number with no payment mode, no receiver, no receipt and no uniqueness.
  Worse, that field is overwritten with the full fare when final payment is
  collected, so the advance figure was destroyed at the moment it mattered.
  Migration 0026 added cab_bookings.cash_amount_due purely to work around
  this (see its docstring).

  An advance needs metadata the old field cannot carry: who physically
  received the money. That answer drives the settlement direction — if the
  platform took the advance but the driver took the balance, the partner's
  net position is neither "debit commission" nor "credit payout". So it
  gets its own table.

  received_by is ADMIN | PARTNER | DRIVER and is the sole custody signal:
  ADMIN means the platform holds it, PARTNER/DRIVER means the partner side
  does. payment_mode is constrained by receiver at the service layer
  (ADMIN may take ONLINE; partner and driver may not) so that custody is
  never ambiguous.

  The partial unique index is the hard guarantee that a booking can never
  carry two live advances — the same approach 0026 took with the UNIQUE on
  driver_cash_collection_items.cab_booking_id. Voiding sets status='VOIDED'
  rather than deleting, which releases the index while keeping the audit
  trail intact.
"""

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS advance_payments (
            id                   BIGSERIAL PRIMARY KEY,
            cab_booking_id       BIGINT       NOT NULL REFERENCES cab_bookings(id) ON DELETE CASCADE,
            master_booking_id    BIGINT       NOT NULL REFERENCES master_bookings(id) ON DELETE CASCADE,
            booking_number       VARCHAR(50)  NOT NULL,
            receipt_number       VARCHAR(50)  NOT NULL UNIQUE,
            amount               NUMERIC(14,2) NOT NULL CHECK (amount > 0),
            payment_mode         VARCHAR(20)  NOT NULL,
            received_by          VARCHAR(20)  NOT NULL,
            partner_id           BIGINT       REFERENCES partners(id),
            driver_id            BIGINT       REFERENCES drivers(id),
            reference_note       TEXT,
            status               VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
            collected_by_user_id UUID         NOT NULL,
            collected_by_role    VARCHAR(20)  NOT NULL,
            collected_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            voided_by_user_id    UUID,
            void_reason          TEXT,
            voided_at            TIMESTAMP WITH TIME ZONE,
            created_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # The one-advance-per-booking guarantee. Partial so that voided rows stay
    # in the table for audit without blocking a corrected entry.
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_advance_payments_active_booking
            ON advance_payments (cab_booking_id) WHERE status = 'ACTIVE'
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_advance_payments_status_collected
            ON advance_payments (status, collected_at DESC)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_advance_payments_partner
            ON advance_payments (partner_id)
    """))

    # No back-fill: admin booking creation has always written
    # total_paid_amount = 0, so there is no historical advance to migrate.


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS advance_payments"))
