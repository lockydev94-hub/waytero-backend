"""0036_hotel_settlement — settlement plumbing shared by cab and hotel

Two schema gaps surface the moment hotel settlement reuses the cab settlement
arithmetic (BRD Part 6 §147):

  1. pending_coupon_disbursements was written to by the cab settlement code
     (settlement_api.settle_booking) but no migration ever created it. A cab
     settlement with a coupon would have raised UndefinedTable at runtime. We
     create it here, once, for real — and widen it so a hotel reservation can
     own a disbursement row too (cab_booking_id XOR hotel_reservation_id).

  2. tds_deduction_records was created cab-only in 0029: cab_booking_id NOT
     NULL with UNIQUE(cab_booking_id). Company-partner TDS on a hotel payout
     has nowhere to land and no ON CONFLICT arbiter. We make cab_booking_id
     nullable, add hotel_reservation_id, and give it its own unique index so
     the hotel settle path can upsert exactly as the cab one does.

Everything is idempotent (IF NOT EXISTS / IF EXISTS, guarded DO blocks) so a DB
that already had pending_coupon_disbursements hand-created is reconciled rather
than broken.

Doc Ref: BRD Part 6 §147 (settlement), §129/§137-143 (commission, TDS)
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_hotel_settlement"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. pending_coupon_disbursements ──────────────────────────────────────
    # Created fresh where absent. cab_booking_id and hotel_reservation_id are
    # both nullable — a row belongs to exactly one side.
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS pending_coupon_disbursements (
            id                      BIGSERIAL PRIMARY KEY,
            cab_booking_id          BIGINT REFERENCES cab_bookings(id) ON DELETE CASCADE,
            hotel_reservation_id    BIGINT REFERENCES hotel_reservations(id) ON DELETE CASCADE,
            master_booking_id       BIGINT NOT NULL,
            partner_id              BIGINT NOT NULL,
            customer_id             BIGINT,
            coupon_discount_amount  NUMERIC(14,2) NOT NULL DEFAULT 0,
            booking_number          VARCHAR(100),
            invoice_number          VARCHAR(100),
            service_type            VARCHAR(10) NOT NULL DEFAULT 'CAB',  -- CAB | HOTEL
            status                  VARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- PENDING | DISBURSED
            disbursed_at            TIMESTAMPTZ,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    # Reconcile a table that predates this migration (hand-created cab-only):
    # widen cab_booking_id to nullable and backfill the new columns.
    conn.execute(sa.text("""
        ALTER TABLE pending_coupon_disbursements
            ADD COLUMN IF NOT EXISTS hotel_reservation_id BIGINT
                REFERENCES hotel_reservations(id) ON DELETE CASCADE
    """))
    conn.execute(sa.text("""
        ALTER TABLE pending_coupon_disbursements
            ADD COLUMN IF NOT EXISTS service_type VARCHAR(10) NOT NULL DEFAULT 'CAB'
    """))
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'pending_coupon_disbursements'
                  AND column_name = 'cab_booking_id'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE pending_coupon_disbursements
                    ALTER COLUMN cab_booking_id DROP NOT NULL;
            END IF;
        END $$;
    """))

    # One PENDING/DISBURSED row per booking, per side. Partial unique indexes so
    # the NULL side never collides.
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pcd_cab_booking
            ON pending_coupon_disbursements (cab_booking_id)
            WHERE cab_booking_id IS NOT NULL
    """))
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pcd_hotel_reservation
            ON pending_coupon_disbursements (hotel_reservation_id)
            WHERE hotel_reservation_id IS NOT NULL
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_pcd_status
            ON pending_coupon_disbursements (status)
    """))

    # ── 2. tds_deduction_records — allow hotel payouts ───────────────────────
    conn.execute(sa.text("""
        ALTER TABLE tds_deduction_records
            ADD COLUMN IF NOT EXISTS hotel_reservation_id BIGINT
                REFERENCES hotel_reservations(id) ON DELETE CASCADE
    """))
    conn.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tds_deduction_records'
                  AND column_name = 'cab_booking_id'
                  AND is_nullable = 'NO'
            ) THEN
                ALTER TABLE tds_deduction_records
                    ALTER COLUMN cab_booking_id DROP NOT NULL;
            END IF;
        END $$;
    """))
    conn.execute(sa.text("""
        ALTER TABLE tds_deduction_records
            ADD COLUMN IF NOT EXISTS service_type VARCHAR(10) NOT NULL DEFAULT 'CAB'
    """))
    conn.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_tds_hotel_reservation
            ON tds_deduction_records (hotel_reservation_id)
            WHERE hotel_reservation_id IS NOT NULL
    """))


def downgrade() -> None:
    conn = op.get_bind()
    # Non-destructive downgrade: keep pending_coupon_disbursements (it should
    # have existed all along) and only peel back the hotel additions.
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_tds_hotel_reservation"))
    conn.execute(sa.text("ALTER TABLE tds_deduction_records DROP COLUMN IF EXISTS hotel_reservation_id"))
    conn.execute(sa.text("ALTER TABLE tds_deduction_records DROP COLUMN IF EXISTS service_type"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uq_pcd_hotel_reservation"))
    conn.execute(sa.text("ALTER TABLE pending_coupon_disbursements DROP COLUMN IF EXISTS hotel_reservation_id"))
    conn.execute(sa.text("ALTER TABLE pending_coupon_disbursements DROP COLUMN IF EXISTS service_type"))
