"""0099 — cab stops and the estimated drive time

Doc Ref: BRD Part 3 §35 (fare formula); SUPPLIER_CAB_PRICE_MASTER_V1 §4.6
         (customer website trip types); Doc Ref: trip_guard module.

Two additions so an advanced booking can be understood months later:

1. ``cab_bookings.stops`` — the ordered places between pickup and drop, as the
   customer or the desk entered them (name, resolved city, coordinates). The
   distance is measured across these legs, so storing the legs is the only way
   the fare can be re-added by hand. Defaults to ``[]``: a booking created
   before this migration is a two-point trip, which is what an empty list means.

2. ``cab_bookings.estimated_duration_minutes`` — the drive-time estimate shown
   on the results page, in minutes. Nullable on purpose: it is an estimate, and
   a row that was never given one should read as "unknown", not "zero minutes".

Idempotent, raw SQL — matches the 0097/0098 style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "ALTER TABLE cab_bookings "
            "ADD COLUMN IF NOT EXISTS stops JSONB NOT NULL DEFAULT '[]'::jsonb"
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE cab_bookings "
            "ADD COLUMN IF NOT EXISTS estimated_duration_minutes INTEGER"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE cab_bookings DROP COLUMN IF EXISTS stops"))
    conn.execute(
        sa.text(
            "ALTER TABLE cab_bookings "
            "DROP COLUMN IF EXISTS estimated_duration_minutes"
        )
    )
