"""0039_partner_acceptance_gate — partner acceptance gate for cab bookings

Revision ID: 0039_partner_acceptance_gate
Revises: 0038_drop_hotel_bookings_legacy
Create Date: 2026-08-08

Doc Ref:
  BRD Part 3 §42 — Driver Assignment after partner acceptance
  BRD Part 7 §155 — Operational backfill / configuration endpoints
  DB Schema Part 4 §7-8 — cab_bookings, cab_booking_assignments

What:
  Adds acceptance / rejection fields to cab_booking_assignments and
  cab_bookings so admin can place a booking in a new
  PENDING_PARTNER_ACCEPTANCE state. The partner must accept or reject
  within a configurable window (PARTNER_ACCEPTANCE_TIMEOUT_MINUTES,
  seeded below); if they do not respond, a Celery sweeper task
  (app.workers.timeout_sweeper) reverts the booking to
  PENDING_ASSIGNMENT so admin can reassign.

All ALTERs are idempotent (IF NOT EXISTS / IF EXISTS / ON CONFLICT),
so a partial run is recoverable and re-applying is safe.

The new booking_status string value (PENDING_PARTNER_ACCEPTANCE) does
NOT require a schema change — cab_bookings.booking_status is
already VARCHAR(50) and the codebase uses string comparison throughout.
"""

from alembic import op
import sqlalchemy as sa

revision = "0039_partner_acceptance_gate"
down_revision = "0038_drop_hotel_bookings_legacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── cab_booking_assignments: acceptance / rejection audit columns ────────
    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_booking_assignments
            ADD COLUMN IF NOT EXISTS accepted_at            TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS rejected_at            TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS rejection_reason_code  VARCHAR(50)  NULL,
            ADD COLUMN IF NOT EXISTS rejection_notes        TEXT         NULL,
            ADD COLUMN IF NOT EXISTS acceptance_deadline    TIMESTAMPTZ NULL
    """
        )
    )

    # ── cab_bookings: denormalised deadline + current pending partner ────────
    # pending_partner_id references the partner that is currently in the
    # acceptance window (set on assignment, cleared on accept/reject/timeout).
    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS acceptance_deadline    TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS partner_responded_at   TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS pending_partner_id     BIGINT      NULL
    """
        )
    )

    conn.execute(
        sa.text(
            """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_cab_bookings_pending_partner'
                  AND table_name = 'cab_bookings'
            ) THEN
                ALTER TABLE cab_bookings
                    ADD CONSTRAINT fk_cab_bookings_pending_partner
                    FOREIGN KEY (pending_partner_id) REFERENCES partners(id);
            END IF;
        END $$;
    """
        )
    )

    # ── Partial index: sweeper scans on (status, deadline) only ───────────────
    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_cab_booking_status_deadline
            ON cab_bookings (booking_status, acceptance_deadline)
            WHERE booking_status = 'PENDING_PARTNER_ACCEPTANCE'
    """
        )
    )

    # ── Seed the timeout config (10 minutes default) ─────────────────────────
    conn.execute(
        sa.text(
            """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES (
            'PARTNER_ACCEPTANCE_TIMEOUT_MINUTES',
            '10',
            'Minutes a partner has to accept a cab booking before auto-revert to PENDING_ASSIGNMENT',
            NOW()
        )
        ON CONFLICT (config_key) DO NOTHING
    """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
        DELETE FROM system_configurations
        WHERE config_key = 'PARTNER_ACCEPTANCE_TIMEOUT_MINUTES'
    """
        )
    )

    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_booking_status_deadline"))

    conn.execute(
        sa.text(
            """
        DO $$
        BEGIN
            ALTER TABLE cab_bookings
                DROP CONSTRAINT IF EXISTS fk_cab_bookings_pending_partner;
        END $$;
    """
        )
    )

    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_bookings
            DROP COLUMN IF EXISTS pending_partner_id,
            DROP COLUMN IF EXISTS partner_responded_at,
            DROP COLUMN IF EXISTS acceptance_deadline
    """
        )
    )

    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_booking_assignments
            DROP COLUMN IF EXISTS acceptance_deadline,
            DROP COLUMN IF EXISTS rejection_notes,
            DROP COLUMN IF EXISTS rejection_reason_code,
            DROP COLUMN IF EXISTS rejected_at,
            DROP COLUMN IF EXISTS accepted_at
    """
        )
    )
