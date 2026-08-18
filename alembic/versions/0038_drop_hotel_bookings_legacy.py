"""0038_drop_hotel_bookings_legacy — remove the superseded hotel_bookings table

Revision ID: 0038_drop_hotel_bookings_legacy
Revises: 0037_platform_audit_logs
Create Date: 2026-08-08

Doc Ref:
  BRD Part 4 §57-92 — Hotel Booking Management
  DB Schema Part 5 §26 — Hotel Module (hotel_reservations replaces hotel_bookings)
  01_Backend/cab_hotel_audit.md — Finding #1

Why:
  hotel_bookings was the first hotel booking table (created in 0033). It was
  superseded by hotel_reservations (migrations 0031 / 0034 / 0035) which has
  the BookingService indirection (booking_service_id FK), pricing snapshots
  (rate_snapshot, commission_config_snapshot, cancellation_policy_snapshot)
  and the full reservation lifecycle columns. Only hotel_reservations is wired
  into the admin and partner APIs (`_build_hotel_out`, the /admin/bookings
  list filter on BookingService.service_type, partner/hotel_booking_api.py).

  In dev, hotel_bookings has 0 rows and no application code writes to it.
  The relationship `MasterBooking.hotel_bookings` still exists in the
  booking model, but no router reads through it. Dropping the table therefore
  has no functional impact.

  CASCADE drops the foreign keys from master_bookings and hotels if any rows
  still reference this table in another environment.

Idempotent: DROP TABLE IF EXISTS, so a partial run is recoverable.

Downgrade reconstructs the original table verbatim from migration 0033's
CREATE TABLE block. Indexes are recreated as well. Data is not recovered —
the upgrade has destroyed it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_drop_hotel_bookings_legacy"
down_revision = "0037_platform_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Drop the legacy hotel_bookings table ──────────────────────────────
    # CASCADE so the FKs from master_bookings and hotels (and any orphaned
    # FK reference we haven't audited) drop with it. Idempotent: safe to run
    # against an environment where the table is already absent.
    conn.execute(sa.text("DROP TABLE IF EXISTS hotel_bookings CASCADE"))


def downgrade() -> None:
    conn = op.get_bind()

    # ── Restore hotel_bookings verbatim from migration 0033 ───────────────
    # Data is gone — the upgrade destroyed it. This downgrade only restores
    # the schema so an environment that needs to roll back can still create
    # the table without a hand-written DDL.
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS hotel_bookings (
            id                         BIGSERIAL PRIMARY KEY,
            uuid                       UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
            master_booking_id          BIGINT NOT NULL REFERENCES master_bookings(id),
            booking_number             VARCHAR(100) NOT NULL UNIQUE,

            hotel_id                   BIGINT REFERENCES hotels(id),
            hotel_name                 VARCHAR(255),
            hotel_address              TEXT,
            room_category_id           BIGINT,
            room_category_name         VARCHAR(255),
            room_type                  VARCHAR(100),
            meal_plan                  VARCHAR(50),

            check_in_date              DATE,
            check_out_date             DATE,
            num_nights                 INTEGER,
            num_rooms                  INTEGER NOT NULL DEFAULT 1,
            num_guests                 INTEGER NOT NULL DEFAULT 1,

            base_amount                NUMERIC(12,2),
            taxes_amount               NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            additional_charges         NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            final_amount               NUMERIC(12,2),

            actual_check_in_at         TIMESTAMPTZ,
            actual_check_out_at        TIMESTAMPTZ,
            check_in_id_proof          VARCHAR(100),
            check_in_id_number         VARCHAR(100),
            check_out_notes            TEXT,

            cancellation_reason        TEXT,
            cancellation_charge        NUMERIC(12,2) NOT NULL DEFAULT 0.00,
            refund_amount              NUMERIC(12,2) NOT NULL DEFAULT 0.00,

            booking_status             VARCHAR(50) NOT NULL DEFAULT 'PENDING_PAYMENT',

            hotel_confirmation_number  VARCHAR(100),
            voucher_url                TEXT,

            created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_booking_status ON hotel_bookings(booking_status)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_booking_mbid ON hotel_bookings(master_booking_id)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_booking_hotel ON hotel_bookings(hotel_id)
    """))