"""0057_tour_booking_manage — tour booking management (advance, vehicle/driver,
trip edits, additional charges, invoicing). Mirrors the cab/hotel manage flow:
Doc Ref: BRD Part 3 §45 (advance & settlement custody), BRD Part 5 §6 (tour
package management).
"""

from alembic import op
import sqlalchemy as sa

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Manage columns on tour_bookings ────────────────────────────────────
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS pickup_location TEXT"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS pickup_datetime TIMESTAMP WITH TIME ZONE"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS vehicle_id BIGINT REFERENCES vehicles(id) ON DELETE SET NULL"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS driver_id BIGINT REFERENCES drivers(id) ON DELETE SET NULL"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS hotel_details TEXT"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS other_details TEXT"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS itinerary_snapshot JSONB"))
    conn.execute(
        sa.text(
            "ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS additional_amount "
            "NUMERIC(14, 2) NOT NULL DEFAULT 0"
        )
    )
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS additional_charge_note TEXT"))
    conn.execute(
        sa.text(
            "ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS advance_total "
            "NUMERIC(14, 2) NOT NULL DEFAULT 0"
        )
    )
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS advance_received_by VARCHAR(20)"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS invoice_number VARCHAR(50)"))
    conn.execute(sa.text("ALTER TABLE tour_bookings ADD COLUMN IF NOT EXISTS invoiced_at TIMESTAMP WITH TIME ZONE"))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_tour_bookings_vehicle ON tour_bookings(vehicle_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_tour_bookings_driver ON tour_bookings(driver_id)"))

    # ── Tour advance payments (mirrors hotel_advance_payments) ─────────────
    # A tour takes advances repeatedly (deposit at booking, top-ups, balance
    # on completion) — deliberately no single-active index, like hotels.
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS tour_advance_payments (
                id                  BIGSERIAL PRIMARY KEY,
                tour_booking_id     BIGINT NOT NULL REFERENCES tour_bookings(id) ON DELETE CASCADE,
                master_booking_id   BIGINT NOT NULL REFERENCES master_bookings(id) ON DELETE CASCADE,
                receipt_number      VARCHAR(50) NOT NULL UNIQUE,
                amount              NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
                payment_mode        VARCHAR(20) NOT NULL,           -- CASH | ONLINE | UPI | WALLET
                received_by         VARCHAR(20) NOT NULL DEFAULT 'ADMIN',  -- ADMIN | PARTNER | DRIVER
                reference_number    VARCHAR(120),
                notes               TEXT,
                status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE | VOIDED
                refunded_amount     NUMERIC(14, 2) NOT NULL DEFAULT 0,
                collected_by_user_id UUID,
                collected_by_role   VARCHAR(20) NOT NULL DEFAULT 'ADMIN',  -- ADMIN | PARTNER
                collected_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                voided_by_user_id   UUID,
                void_reason         TEXT,
                voided_at           TIMESTAMP WITH TIME ZONE,
                created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_tour_advance_booking ON tour_advance_payments(tour_booking_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_tour_advance_status ON tour_advance_payments(status, collected_at)"))

    # ── Additional charges (trip modifications — extra days, extra stops) ──
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS tour_booking_charges (
                id               BIGSERIAL PRIMARY KEY,
                tour_booking_id  BIGINT NOT NULL REFERENCES tour_bookings(id) ON DELETE CASCADE,
                label            VARCHAR(255) NOT NULL,
                amount           NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
                reason           TEXT,
                added_by_user_id UUID,
                added_by_role    VARCHAR(20) NOT NULL DEFAULT 'ADMIN',  -- ADMIN | PARTNER
                created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_tour_charges_booking ON tour_booking_charges(tour_booking_id)"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS tour_booking_charges"))
    conn.execute(sa.text("DROP TABLE IF EXISTS tour_advance_payments"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_tour_bookings_vehicle"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_tour_bookings_driver"))
    for col in (
        "pickup_location", "pickup_datetime", "vehicle_id", "driver_id",
        "hotel_details", "other_details", "itinerary_snapshot",
        "additional_amount", "additional_charge_note", "advance_total",
        "advance_received_by", "invoice_number", "invoiced_at",
    ):
        conn.execute(sa.text(f"ALTER TABLE tour_bookings DROP COLUMN IF EXISTS {col}"))
