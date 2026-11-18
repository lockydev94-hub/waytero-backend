"""0104 — tour trip execution by the assigned driver

Doc Ref: BRD Part 5 §115–§116 (tour start / in progress),
         06_Backend_Architecture/11_DRIVER_APP_BACKEND.md

Why:
  * Tour bookings had fleet assignment but no execution record. The assigned
    driver starts the tour in the driver app, records the start/end odometer,
    collects the remaining balance and closes the trip. Those facts need to
    live on the tour booking just like they do on cab_bookings.
  * Cash / UPI collected by the driver must remain visible as driver custody
    until the admin settles with the partner. cash_pending_at / cash_amount_due
    mirror the cab columns so the driver cash screen can include tours.

Idempotent, raw SQL — matches the hand-written migration style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            ALTER TABLE tour_bookings
                ADD COLUMN IF NOT EXISTS trip_start_km NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS trip_end_km NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS trip_started_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS trip_ended_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS actual_distance NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS cash_pending_at VARCHAR(20),
                ADD COLUMN IF NOT EXISTS cash_amount_due NUMERIC(14,2) NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS completed_by_driver_id BIGINT
                    REFERENCES drivers(id) ON DELETE SET NULL,
                ADD COLUMN IF NOT EXISTS close_notes TEXT
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_tour_bookings_driver_status
                ON tour_bookings (driver_id, booking_status)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_tour_bookings_driver_status"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE tour_bookings
                DROP COLUMN IF EXISTS close_notes,
                DROP COLUMN IF EXISTS completed_by_driver_id,
                DROP COLUMN IF EXISTS cash_amount_due,
                DROP COLUMN IF EXISTS cash_pending_at,
                DROP COLUMN IF EXISTS actual_distance,
                DROP COLUMN IF EXISTS trip_ended_at,
                DROP COLUMN IF EXISTS trip_started_at,
                DROP COLUMN IF EXISTS trip_end_km,
                DROP COLUMN IF EXISTS trip_start_km
            """
        )
    )
