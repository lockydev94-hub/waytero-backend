"""add tour support to booking_cancellation_requests

Doc Ref: BRD Part 5 §119 — Tour cancellation policy (partner request path)
Revision: 0061
DownRevision: 0060

Partners can already request admin to cancel assigned CAB / HOTEL bookings
(booking_type CAB | HOTEL). This migration adds the TOUR leg:
  * a tour_booking_id column on booking_cancellation_requests,
  * a relaxed chk_req_target constraint so exactly one target per row
    (cab XOR hotel XOR tour) holds,
  * a partial index for the admin request-inbox lookup.
"""

from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            ALTER TABLE booking_cancellation_requests
                ADD COLUMN IF NOT EXISTS tour_booking_id BIGINT
                    REFERENCES tour_bookings(id) ON DELETE CASCADE
            """
        )
    )

    # Relax the old CAB-only / HOTEL-only CHECK to a three-way XOR. The old
    # constraint is dropped by name — the column-add above is idempotent, so
    # this migration may run against a fresh DB where the constraint exists.
    conn.execute(
        sa.text(
            "ALTER TABLE booking_cancellation_requests "
            "DROP CONSTRAINT IF EXISTS chk_req_target"
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE booking_cancellation_requests
                ADD CONSTRAINT chk_req_target CHECK (
                    (booking_type = 'CAB'  AND cab_booking_id IS NOT NULL
                                            AND hotel_reservation_id IS NULL
                                            AND tour_booking_id IS NULL) OR
                    (booking_type = 'HOTEL' AND hotel_reservation_id IS NOT NULL
                                            AND cab_booking_id IS NULL
                                            AND tour_booking_id IS NULL) OR
                    (booking_type = 'TOUR' AND tour_booking_id IS NOT NULL
                                            AND cab_booking_id IS NULL
                                            AND hotel_reservation_id IS NULL)
                )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_cancel_req_tour
                ON booking_cancellation_requests (tour_booking_id)
                WHERE tour_booking_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cancel_req_tour"))
    conn.execute(
        sa.text(
            "ALTER TABLE booking_cancellation_requests "
            "DROP CONSTRAINT IF EXISTS chk_req_target"
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE booking_cancellation_requests
                ADD CONSTRAINT chk_req_target CHECK (
                    (booking_type = 'CAB'  AND cab_booking_id IS NOT NULL
                                            AND hotel_reservation_id IS NULL) OR
                    (booking_type = 'HOTEL' AND hotel_reservation_id IS NOT NULL
                                            AND cab_booking_id IS NULL)
                )
            """
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE booking_cancellation_requests "
            "DROP COLUMN IF EXISTS tour_booking_id"
        )
    )
