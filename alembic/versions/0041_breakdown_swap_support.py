"""0041_breakdown_swap_support — vehicle breakdown → swap / handover lifecycle

Revision ID: 0041_breakdown_swap_support
Revises: 0040_realtime_notifications
Create Date: 2026-08-09

Doc Ref:
  BRD Part 3 §42 — Driver Assignment after partner acceptance
  BRD Part 6 §155 — Settlement edge cases
  Spec section: Cab Breakdown → Vehicle Swap (in-trip)

What this migration adds:

  On cab_bookings
  ────────────────
  is_breakdown_swap        BOOLEAN       — flag for UI / reports
  swap_count               INTEGER       — how many swaps happened on this booking
  last_swap_at             TIMESTAMPTZ   — timestamp of the most recent swap
  pre_swap_actual_km       NUMERIC(12,2) — KM covered at the moment breakdown was
                                            reported (audit / UI display only;
                                            does NOT feed pro-rata billing)
  original_partner_id      BIGINT FK     — snapshotted on first handover so the
                                            "lost-trip" partner can be identified
                                            after their assignment row closes
  breakdown_reason         VARCHAR(50)   — VEHICLE_BREAKDOWN | ACCIDENT |
                                            DRIVER_UNWELL | OTHER
  breakdown_latitude       NUMERIC(10,7) — last reported location
  breakdown_longitude      NUMERIC(10,7)
  breakdown_reported_at    TIMESTAMPTZ
  breakdown_reported_by    VARCHAR(20)   — DRIVER | PARTNER | ADMIN

  On cab_booking_assignments
  ──────────────────────────
  closed_at                TIMESTAMPTZ   — when this assignment ended
                                            (TRIP_COMPLETED | VEHICLE_BREAKDOWN |
                                             PARTNER_HANDOVER | ADMIN_REASSIGN).
                                            NULL = currently active assignment.
  close_reason             VARCHAR(50)
  km_at_assignment_start   NUMERIC(12,2) — odometer at open
  km_at_assignment_end     NUMERIC(12,2) — odometer at close

  Indexes
  ───────
  idx_cab_assign_active — partial index on cab_booking_id WHERE closed_at IS NULL
                          so "current assignment" lookups stay fast.

  assignment_type
  ───────────────
  The existing free-text column is extended by convention (no DB enum) to
  accept the new values SWAP_SAME_PARTNER, SWAP_HANDOVER, BREAKDOWN_REPLACEMENT.
  No CHECK constraint is added so the change is non-breaking for old rows.

All statements are idempotent so a partial run is recoverable.
"""

from alembic import op
import sqlalchemy as sa

revision = "0041_breakdown_swap_support"
down_revision = "0040_realtime_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── cab_bookings: breakdown / swap audit columns ─────────────────────────
    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS is_breakdown_swap        BOOLEAN      NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS swap_count               INTEGER      NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_swap_at             TIMESTAMPTZ  NULL,
            ADD COLUMN IF NOT EXISTS pre_swap_actual_km       NUMERIC(12,2) NULL,
            ADD COLUMN IF NOT EXISTS original_partner_id      BIGINT       NULL
                REFERENCES partners(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS breakdown_reason         VARCHAR(50)  NULL,
            ADD COLUMN IF NOT EXISTS breakdown_latitude       NUMERIC(10,7) NULL,
            ADD COLUMN IF NOT EXISTS breakdown_longitude      NUMERIC(10,7) NULL,
            ADD COLUMN IF NOT EXISTS breakdown_reported_at    TIMESTAMPTZ  NULL,
            ADD COLUMN IF NOT EXISTS breakdown_reported_by    VARCHAR(20)  NULL
    """
        )
    )

    # Index to find handovers quickly (for reports and reconciliation).
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_booking_breakdown_swap "
            "ON cab_bookings (is_breakdown_swap) WHERE is_breakdown_swap = TRUE"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_booking_original_partner "
            "ON cab_bookings (original_partner_id) WHERE original_partner_id IS NOT NULL"
        )
    )

    # ── cab_booking_assignments: closed_at / close_reason / km snapshots ────
    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_booking_assignments
            ADD COLUMN IF NOT EXISTS closed_at                TIMESTAMPTZ   NULL,
            ADD COLUMN IF NOT EXISTS close_reason             VARCHAR(50)   NULL,
            ADD COLUMN IF NOT EXISTS km_at_assignment_start   NUMERIC(12,2) NULL,
            ADD COLUMN IF NOT EXISTS km_at_assignment_end     NUMERIC(12,2) NULL
    """
        )
    )

    # Partial index — only the currently-active assignment per booking.
    # Speeds up "latest assignment wins" replacements throughout the codebase.
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_assign_active "
            "ON cab_booking_assignments (cab_booking_id) WHERE closed_at IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_assign_partner_active "
            "ON cab_booking_assignments (partner_id) WHERE closed_at IS NULL"
        )
    )

    # ── System configuration defaults ───────────────────────────────────────
    # Reuse existing rows where present; otherwise seed.
    conn.execute(
        sa.text(
            """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES (
            'BREAKDOWN_ACCEPTANCE_DEADLINE_MINUTES',
            '15',
            'Minutes a new partner has to accept a handover from a breakdown-affected trip',
            NOW()
        )
        ON CONFLICT (config_key) DO NOTHING
    """
        )
    )
    conn.execute(
        sa.text(
            """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES (
            'PARTNER_SELF_SERVE_SWAP_ENABLED',
            'true',
            'When true, partners may swap their own vehicle without admin confirmation',
            NOW()
        )
        ON CONFLICT (config_key) DO NOTHING
    """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_assign_partner_active"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_assign_active"))

    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_booking_assignments
            DROP COLUMN IF EXISTS km_at_assignment_end,
            DROP COLUMN IF EXISTS km_at_assignment_start,
            DROP COLUMN IF EXISTS close_reason,
            DROP COLUMN IF EXISTS closed_at
    """
        )
    )

    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_booking_original_partner"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_booking_breakdown_swap"))

    conn.execute(
        sa.text(
            """
        ALTER TABLE cab_bookings
            DROP COLUMN IF EXISTS breakdown_reported_by,
            DROP COLUMN IF EXISTS breakdown_reported_at,
            DROP COLUMN IF EXISTS breakdown_longitude,
            DROP COLUMN IF EXISTS breakdown_latitude,
            DROP COLUMN IF EXISTS breakdown_reason,
            DROP COLUMN IF EXISTS original_partner_id,
            DROP COLUMN IF EXISTS pre_swap_actual_km,
            DROP COLUMN IF EXISTS last_swap_at,
            DROP COLUMN IF EXISTS swap_count,
            DROP COLUMN IF EXISTS is_breakdown_swap
    """
        )
    )

    conn.execute(
        sa.text(
            "DELETE FROM system_configurations WHERE config_key IN "
            "('BREAKDOWN_ACCEPTANCE_DEADLINE_MINUTES', 'PARTNER_SELF_SERVE_SWAP_ENABLED')"
        )
    )
