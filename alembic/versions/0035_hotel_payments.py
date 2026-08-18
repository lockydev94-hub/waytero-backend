"""Hotel advance payments, invoicing and payment custody

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-06

Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
         BRD Part 4 §57-92 — Hotel Booking Lifecycle
         DB Schema Part 5 §12 — hotel_reservations

Why a separate table instead of reusing advance_payments:
    advance_payments.cab_booking_id is NOT NULL and the partial unique index
    ux_advance_payments_active_booking permits exactly one ACTIVE row per
    booking. Hotel bookings take advances repeatedly across the stay, so that
    constraint is wrong here, and relaxing it would change the semantics the
    cab settlement path (_compute_position) already depends on.

Custody model mirrors the cab side: received_by is the signal that drives
settlement direction. ADMIN means the platform holds the money; PARTNER means
the hotel partner side does. Voiding flips status rather than deleting so the
audit trail survives.
"""

from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    # ── Advance payments taken against a hotel reservation ──────────────
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS hotel_advance_payments (
            id                     BIGSERIAL PRIMARY KEY,
            hotel_reservation_id   BIGINT NOT NULL
                                   REFERENCES hotel_reservations(id) ON DELETE CASCADE,
            master_booking_id      BIGINT NOT NULL
                                   REFERENCES master_bookings(id) ON DELETE CASCADE,
            receipt_number         VARCHAR(50) UNIQUE NOT NULL,
            amount                 NUMERIC(14,2) NOT NULL,
            payment_mode           VARCHAR(20)   NOT NULL,
            received_by            VARCHAR(20)   NOT NULL DEFAULT 'ADMIN',
            reference_number       VARCHAR(120),
            notes                  TEXT,
            status                 VARCHAR(20)   NOT NULL DEFAULT 'ACTIVE',
            refunded_amount        NUMERIC(14,2) NOT NULL DEFAULT 0.00,
            collected_by_user_id   UUID,
            collected_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            voided_by_user_id      UUID,
            void_reason            TEXT,
            voided_at              TIMESTAMPTZ,
            created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_hotel_advance_amount_positive CHECK (amount > 0),
            CONSTRAINT ck_hotel_advance_mode
                CHECK (payment_mode IN ('CASH','ONLINE','UPI','WALLET')),
            CONSTRAINT ck_hotel_advance_received_by
                CHECK (received_by IN ('ADMIN','PARTNER')),
            CONSTRAINT ck_hotel_advance_status
                CHECK (status IN ('ACTIVE','VOIDED'))
        )
    """))

    # Deliberately NO single-active unique index — a hotel stay may take
    # several advances (booking deposit, top-up during stay, etc).
    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_advance_reservation
            ON hotel_advance_payments (hotel_reservation_id, status)
    """))
    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_advance_collected
            ON hotel_advance_payments (status, collected_at)
    """))

    # ── Reservation-level billing / custody columns ─────────────────────
    # overtime_* capture a late check-out billed beyond the reserved period;
    # they are stored rather than recomputed so an invoice stays reproducible
    # even after config changes (BRD Rule 25).
    op.execute(sa.text("""
        ALTER TABLE hotel_reservations
            ADD COLUMN IF NOT EXISTS coupon_code              VARCHAR(50),
            ADD COLUMN IF NOT EXISTS coupon_discount          NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            ADD COLUMN IF NOT EXISTS invoice_number           VARCHAR(50),
            ADD COLUMN IF NOT EXISTS invoice_url              TEXT,
            ADD COLUMN IF NOT EXISTS invoice_generated_at     TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS payment_collected_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            ADD COLUMN IF NOT EXISTS payment_mode             VARCHAR(20),
            ADD COLUMN IF NOT EXISTS payment_collected_by     VARCHAR(20),
            ADD COLUMN IF NOT EXISTS payment_reference        VARCHAR(120),
            ADD COLUMN IF NOT EXISTS payment_collected_at     TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS overtime_hours           NUMERIC(6,2)  NOT NULL DEFAULT 0.00,
            ADD COLUMN IF NOT EXISTS overtime_charge          NUMERIC(12,2) NOT NULL DEFAULT 0.00
    """))

    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_hotel_reservations_invoice_number
            ON hotel_reservations (invoice_number)
            WHERE invoice_number IS NOT NULL
    """))

    # Back-fill existing rows so the NOT NULL defaults are explicit.
    op.execute(sa.text("""
        UPDATE hotel_reservations
           SET payment_collected_status = 'PENDING'
         WHERE payment_collected_status IS NULL
    """))

    # ── Runtime-configurable hotel billing rules ────────────────────────
    # Seeded here and read at runtime rather than hardcoded, per the
    # system_configurations convention used across the codebase.
    op.execute(sa.text("""
        INSERT INTO system_configurations (config_key, config_value, description)
        VALUES
            ('HOTEL_CHECKOUT_TIME', '11:00',
             'Standard hotel check-out time (HH:MM, 24h). Stays past this incur overtime.'),
            ('HOTEL_OVERTIME_GRACE_MINUTES', '60',
             'Minutes past check-out time before overtime billing starts.'),
            ('HOTEL_OVERTIME_MODE', 'SLAB',
             'How late check-out is billed: SLAB (half/full day) or HOURLY.'),
            ('HOTEL_OVERTIME_HOURLY_PERCENT', '10',
             'Percent of one night tariff charged per overtime hour when mode is HOURLY.'),
            ('HOTEL_OVERTIME_HALFDAY_PERCENT', '50',
             'Percent of one night tariff for a half-day late check-out (SLAB mode).'),
            ('HOTEL_OVERTIME_FULLDAY_PERCENT', '100',
             'Percent of one night tariff for a full-day late check-out (SLAB mode).'),
            ('HOTEL_OVERTIME_HALFDAY_UNTIL_HOURS', '6',
             'Hours past grace still billed as half day; beyond this bills a full day.')
        ON CONFLICT (config_key) DO NOTHING
    """))


def downgrade():
    op.execute(sa.text("""
        DELETE FROM system_configurations
         WHERE config_key IN (
            'HOTEL_CHECKOUT_TIME',
            'HOTEL_OVERTIME_GRACE_MINUTES',
            'HOTEL_OVERTIME_MODE',
            'HOTEL_OVERTIME_HOURLY_PERCENT',
            'HOTEL_OVERTIME_HALFDAY_PERCENT',
            'HOTEL_OVERTIME_FULLDAY_PERCENT',
            'HOTEL_OVERTIME_HALFDAY_UNTIL_HOURS'
         )
    """))
    op.execute(sa.text("DROP INDEX IF EXISTS ux_hotel_reservations_invoice_number"))
    op.execute(sa.text("""
        ALTER TABLE hotel_reservations
            DROP COLUMN IF EXISTS coupon_code,
            DROP COLUMN IF EXISTS coupon_discount,
            DROP COLUMN IF EXISTS invoice_number,
            DROP COLUMN IF EXISTS invoice_url,
            DROP COLUMN IF EXISTS invoice_generated_at,
            DROP COLUMN IF EXISTS payment_collected_status,
            DROP COLUMN IF EXISTS payment_mode,
            DROP COLUMN IF EXISTS payment_collected_by,
            DROP COLUMN IF EXISTS payment_reference,
            DROP COLUMN IF EXISTS payment_collected_at,
            DROP COLUMN IF EXISTS overtime_hours,
            DROP COLUMN IF EXISTS overtime_charge
    """))
    op.execute(sa.text("DROP TABLE IF EXISTS hotel_advance_payments"))
