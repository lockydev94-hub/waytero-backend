"""0047 — make master_booking_id UNIQUE on booking_cancellations

Revision ID: 0047
Revises: 0046_city_coordinates
Create Date: 2026-08-11

Doc Ref:
  BRD Part 3 §46 (Cab Cancellation Rules)
  SRS Part 3 §99  (one cancellation row per master booking)

Why:
  The SQLAlchemy model (`app/modules/booking/models/__init__.py`
  `BookingCancellation`) declares `master_booking_id` as `unique=True`,
  but migration 0007_phase3_booking created the table without that
  constraint, and no subsequent migration added it. The cancellation
  orchestrator relies on UPSERT semantics (`ON CONFLICT (master_booking_id)
  DO UPDATE`) to keep at-most-one cancellation row per master booking,
  which Postgres rejects without a unique constraint.

This migration:
  • adds the missing UNIQUE constraint on
    booking_cancellations.master_booking_id
  • is idempotent — wrapped in a DO block that checks pg_constraint first,
    so re-running is a no-op
  • downgrades cleanly with DROP CONSTRAINT IF EXISTS

Backfill:
  If duplicate rows somehow exist (they shouldn't, but defensive), the
  DELETE step keeps the oldest row per master_booking_id. Without that,
  the ALTER TABLE would fail with "could not create unique index".

Revision ID is the short numeric form ("0047") because alembic_version
is VARCHAR(32) and the long filename form ("0047_booking_cancellations_unique",
36 chars) overflows it. 0043 / 0045 etc. follow the same convention.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046_city_coordinates"


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Defensive de-dup. If anything has slipped through (e.g. a partial
    #    admin-cancel + a customer-cancel racing, neither of which the
    #    current code paths allow, but be safe), collapse to the oldest row.
    conn.execute(
        sa.text(
            """
            DELETE FROM booking_cancellations bc
             WHERE EXISTS (
                 SELECT 1 FROM booking_cancellations older
                  WHERE older.master_booking_id = bc.master_booking_id
                    AND older.id < bc.id
             )
            """
        )
    )

    # 2. Idempotently add the UNIQUE constraint. The DO block makes it safe
    #    to re-run on a DB that already has the constraint.
    conn.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conrelid = 'booking_cancellations'::regclass
                       AND contype = 'u'
                       AND conname = 'uq_booking_cancellations_master_booking_id'
                ) THEN
                    ALTER TABLE booking_cancellations
                      ADD CONSTRAINT uq_booking_cancellations_master_booking_id
                      UNIQUE (master_booking_id);
                END IF;
            END
            $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE booking_cancellations DROP CONSTRAINT IF EXISTS uq_booking_cancellations_master_booking_id"
        )
    )