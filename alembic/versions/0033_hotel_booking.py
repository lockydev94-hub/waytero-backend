"""Add hotel_bookings table

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-06

Doc Ref: BRD Part 4 §57-92 — Hotel Booking Management
         DB Schema Part 4 — Booking Engine (hotel sub-booking)

Status flow:
  PENDING_PAYMENT → AWAITING_HOTEL_CONFIRMATION → CONFIRMED
  → CHECKED_IN → IN_HOUSE → CHECKED_OUT → COMPLETED → SETTLED
  (+ CANCELLED, NO_SHOW, REJECTED as terminal states)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0033"
down_revision = "0032_bank_verified"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
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

    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_booking_status ON hotel_bookings(booking_status)
    """))
    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_booking_mbid ON hotel_bookings(master_booking_id)
    """))
    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_hotel_booking_hotel ON hotel_bookings(hotel_id)
    """))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS hotel_bookings"))
