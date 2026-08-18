"""Cab pricing engine v3 — toll, waiting charge, and night-charge type

Revision ID: 0050
Revises: 0049

Background:
    The fare engine (app/modules/booking/services/fare.py) today supports
    base fare + distance + driver allowance + a flat night charge. Realistic
    Indian cab trips add three more levers that admins configure per pricing
    rule:

      1. Toll — a flat amount added to the fare (Model A: estimated at booking
         time, replaced by actual at settlement).

      2. Waiting charge — free_waiting_minutes of grace, then billing by a
         configurable granularity (PER_MINUTE / PER_15_MINUTES /
         PER_30_MINUTES / PER_HOUR) at waiting_rate_per_hour.

      3. Night charge type — the existing night_charge stays as the "amount",
         but night_charge_type now says how to apply it:
             FIXED      → flat night_charge when the trip hits the night window
             PERCENTAGE → night_charge% of the pre-night subtotal
             PER_KM     → night_charge per billable km

    These columns are added to BOTH ``vehicle_pricing_rules`` (city-specific)
    and ``default_vehicle_pricing_rules`` (platform fallback). All defaults
    keep legacy behaviour: toll 0, no waiting, FIXED night charge. NULLs are
    forbidden so the fare engine never has to tolerate a None column value.

Idempotent SQL: every ADD COLUMN is ``IF NOT EXISTS`` so reruns don't break
the migration.

Doc Ref: BRD Part 3 §35 (fare engine), §36 (city-based pricing).
"""

from alembic import op
import sqlalchemy as sa

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. night_charge_type (FIXED / PERCENTAGE / PER_KM) ─────────────────
    # Default FIXED keeps current behaviour intact. The ENUM contract is
    # enforced at the application layer (fare.py) — the DB only stores the
    # text so the value can grow without a follow-up migration (same pattern
    # as driver_allowance_type in migration 0049).
    conn.execute(
        sa.text(
            """
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS night_charge_type VARCHAR(16)
                NOT NULL DEFAULT 'FIXED'
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS night_charge_type VARCHAR(16)
                NOT NULL DEFAULT 'FIXED'
    """
        )
    )

    # ── 2. toll (flat amount) ───────────────────────────────────────────────
    conn.execute(
        sa.text(
            """
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS toll NUMERIC(12, 2)
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS toll NUMERIC(12, 2)
                NOT NULL DEFAULT 0
    """
        )
    )

    # ── 3. waiting charge fields ────────────────────────────────────────────
    # free_waiting_minutes + actual_waiting_minutes in minutes; the rate is
    # per hour; granularity controls block billing. Defaults → no waiting
    # charge (0 free period only matters when actual > free).
    conn.execute(
        sa.text(
            """
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS free_waiting_minutes INTEGER
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS actual_waiting_minutes INTEGER
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS waiting_rate_per_hour NUMERIC(12, 2)
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS waiting_granularity VARCHAR(16)
                NOT NULL DEFAULT 'PER_15_MINUTES'
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS free_waiting_minutes INTEGER
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS actual_waiting_minutes INTEGER
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS waiting_rate_per_hour NUMERIC(12, 2)
                NOT NULL DEFAULT 0
    """
        )
    )
    conn.execute(
        sa.text(
            """
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS waiting_granularity VARCHAR(16)
                NOT NULL DEFAULT 'PER_15_MINUTES'
    """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Reversed order so a partial downgrade (crash mid-way) still leaves the
    # DB in a usable state.
    for col in (
        "waiting_granularity",
        "waiting_rate_per_hour",
        "actual_waiting_minutes",
        "free_waiting_minutes",
        "toll",
        "night_charge_type",
    ):
        conn.execute(
            sa.text(
                f"ALTER TABLE default_vehicle_pricing_rules DROP COLUMN IF EXISTS {col}"
            )
        )
        conn.execute(
            sa.text(f"ALTER TABLE vehicle_pricing_rules DROP COLUMN IF EXISTS {col}")
        )
