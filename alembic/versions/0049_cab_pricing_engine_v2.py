"""Cab pricing engine v2 — per-trip / per-day / per-km driver allowance + billable minimum

Revision ID: 0049
Revises: 0048

Background:
    The cab booking system originally had a single flat driver_allowance per
    pricing rule and used ``max(0, distance - minimum_km) * per_km_rate`` as
    the distance charge. That fails two real-world cases:

    1. Outstation one-way under minimum: Bhubaneswar → Puri is 65 km but the
       minimum billing distance is 100 km. The customer should be charged
       100 km, not 0 km. Real-world cab engines use
       ``MAX(actual_km, minimum_km) * per_km_rate`` for OUTSTATION / ONE_WAY /
       ROUND_TRIP.

    2. Multi-day round trips: the driver physically stays overnight in the
       destination city on a 2-day Bhubaneswar → Puri → Bhubaneswar trip. The
       customer should pay 2 × per-day driver allowance, not 1 × flat. The
       single flat field cannot represent this.

    This migration adds the schema needed to fix both:

      - ``driver_allowance_type`` (PER_TRIP / PER_DAY / PER_KM / NONE) on
        BOTH ``vehicle_pricing_rules`` (city-specific) and
        ``default_vehicle_pricing_rules`` (platform fallback). Backfilled to
        ``PER_TRIP`` so existing rows behave exactly as before — no
        regression until an admin opts in via the pricing rule form.

      - ``return_datetime`` on ``cab_bookings`` — required when a rule uses
        PER_DAY so the server can compute trip_days from
        ``pickup_datetime → return_datetime``. NULL allowed for single-day
        trips (driver_allowance_type=PER_TRIP / PER_KM / NONE don't need it).

Idempotent SQL: every ADD COLUMN is ``IF NOT EXISTS`` so reruns don't break
the migration. The backfill is also safe to rerun.

Doc Ref: BRD Part 3 §35 (fare engine), §36 (city-based pricing), §45 (settlement).
"""
from alembic import op
import sqlalchemy as sa

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


# Trip types where the minimum_km is a "billable minimum" (always charge
# at least that many km) rather than "first N km included in base fare".
# Mirrored in fare.py — keep both in sync.
_OUTSTATION_TRIP_TYPES = ("OUTSTATION", "ONE_WAY", "ROUND_TRIP")


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Pricing rule: driver_allowance_type ──────────────────────────────
    # Default PER_TRIP keeps current behaviour intact. The ENUM contract is
    # enforced at the application layer (fare.py) — the DB only stores the
    # text so the value can grow without a follow-up migration.
    conn.execute(sa.text("""
        ALTER TABLE vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS driver_allowance_type VARCHAR(16)
                NOT NULL DEFAULT 'PER_TRIP'
    """))
    conn.execute(sa.text("""
        ALTER TABLE default_vehicle_pricing_rules
            ADD COLUMN IF NOT EXISTS driver_allowance_type VARCHAR(16)
                NOT NULL DEFAULT 'PER_TRIP'
    """))

    # CHECK constraint via app+tests; DB-side CHECK is omitted intentionally
    # because rerun semantics with NOT VALID are awkward and the column has
    # a DEFAULT so old rows never end up NULL.

    # ── 2. Cab booking: return_datetime ──────────────────────────────────────
    # Captured for ROUND_TRIP so the fare engine can compute trip_days when
    # driver_allowance_type = PER_DAY. NULL for single-day bookings.
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS return_datetime TIMESTAMPTZ
    """))

    # Index for "find all round trips in date range" admin queries — small
    # but useful for ops dashboards.
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cab_return_datetime
            ON cab_bookings(return_datetime)
            WHERE return_datetime IS NOT NULL
    """))

    # ── 3. Backfill: existing rows already have a default of PER_TRIP via
    # the column DEFAULT, so no UPDATE is needed. The driver_allowance_type
    # column also accepts the value 'PER_TRIP' as the historical behaviour.

    # ── 4. Persist the constant for the fare engine ────────────────────────
    # We don't store the trip_type set in the DB; it's the same enum
    # everywhere. The check_constraint_trip_types migration is implicit
    # (the codepath validates trip_type ∈ valid_trip_types before insert).


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_return_datetime"))
    conn.execute(sa.text("ALTER TABLE cab_bookings DROP COLUMN IF EXISTS return_datetime"))
    conn.execute(sa.text("ALTER TABLE vehicle_pricing_rules DROP COLUMN IF EXISTS driver_allowance_type"))
    conn.execute(sa.text("ALTER TABLE default_vehicle_pricing_rules DROP COLUMN IF EXISTS driver_allowance_type"))
