"""0058_tour_settle_closes_master — align tour settlement master status.

Cab and hotel settlement close the master booking (booking_status = 'CLOSED')
while the service row stays 'SETTLED'. Tour settlement wrongly left the master
at 'SETTLED'. This migration backfills already-settled tour master bookings so
the admin bookings list shows 'Closed' like cab/hotel, and re-flags the ledger
as the fix point for future settles (the service itself now writes 'CLOSED').

Doc Ref: BRD Part 3 §45 (settlement & close), Admin API §11 (bookings list).
"""

from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Backfill: any master booking still flagged 'SETTLED' that has a tour
    # booking whose lifecycle status is 'SETTLED' — and no non-terminal
    # cab/hotel/tour service remaining — gets closed, matching cab/hotel
    # behaviour. We match on the real lifecycle tables (tour_bookings,
    # cab_bookings, hotel_reservations), NOT on booking_services, whose
    # service_status is a stale creation-time snapshot. Idempotent by
    # construction.
    conn.execute(
        sa.text(
            """
            UPDATE master_bookings mb
            SET booking_status = 'CLOSED',
                updated_at = NOW()
            WHERE mb.booking_status = 'SETTLED'
              AND EXISTS (
                  SELECT 1 FROM tour_bookings tb
                  WHERE tb.master_booking_id = mb.id
                    AND tb.booking_status = 'SETTLED'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM cab_bookings cb
                  WHERE cb.master_booking_id = mb.id
                    AND COALESCE(cb.booking_status, '') NOT IN
                        ('SETTLED', 'CANCELLED')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM hotel_reservations hr
                  WHERE hr.master_booking_id = mb.id
                    AND COALESCE(hr.reservation_status, '') NOT IN
                        ('SETTLED', 'CANCELLED', 'REJECTED', 'NO_SHOW')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM tour_bookings tb2
                  WHERE tb2.master_booking_id = mb.id
                    AND COALESCE(tb2.booking_status, '') NOT IN
                        ('SETTLED', 'CANCELLED')
              )
            """
        )
    )


def downgrade() -> None:
    # No safe reverse — we don't know which masters were SETTLED before this
    # migration. Leave the data as-is.
    pass
