"""Add default_vehicle_pricing_rules table and seed defaults

Revision ID: 0011_default_vehicle_pricing
Revises: 0010_app_versions_table
Create Date: 2026-07-28

Doc Ref:
  - DB Schema Part 3 §18 — vehicle_pricing_rules (city-specific)
  - BRD Part 3 §35 — CAB FARE ENGINE: "Pricing must never be hardcoded."
  - BRD Part 3 §36 — CITY BASED PRICING

Purpose:
  When admin has NOT yet configured a city-specific pricing rule for a
  given (vehicle_category, trip_type) combination, the booking engine
  falls back to these platform-level defaults.  Admin can update any
  default at any time from the Settings → Vehicle Pricing Rules tab.
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_default_vehicle_pricing"
down_revision = "0010_app_versions_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Table ─────────────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS default_vehicle_pricing_rules (
            id                  BIGSERIAL PRIMARY KEY,

            -- No city_id — these are GLOBAL defaults
            vehicle_category_id BIGINT NOT NULL
                REFERENCES vehicle_categories(id),

            trip_type           VARCHAR(50)   NOT NULL,

            base_fare           NUMERIC(12,2) NOT NULL DEFAULT 0,
            minimum_km          INTEGER       NOT NULL DEFAULT 0,
            per_km_rate         NUMERIC(12,2) NOT NULL DEFAULT 0,
            driver_allowance    NUMERIC(12,2) NOT NULL DEFAULT 0,
            night_charge        NUMERIC(12,2) NOT NULL DEFAULT 0,

            updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

            UNIQUE (vehicle_category_id, trip_type)
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_def_pricing_cat_trip
        ON default_vehicle_pricing_rules(vehicle_category_id, trip_type)
    """))

    # ── Seed sensible India-market defaults ───────────────────────────────────
    # trip_types: LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP
    # Values sourced from BRD Part 3 §36 examples and typical market rates.
    # Admin SHOULD override these for each city via the city-specific rules.
    conn.execute(sa.text("""
        INSERT INTO default_vehicle_pricing_rules
            (vehicle_category_id, trip_type, base_fare, minimum_km, per_km_rate, driver_allowance, night_charge)
        SELECT
            vc.id,
            t.trip_type,
            t.base_fare,
            t.minimum_km,
            t.per_km_rate,
            t.driver_allowance,
            t.night_charge
        FROM vehicle_categories vc
        CROSS JOIN (VALUES
            -- LOCAL trips
            ('LOCAL',      150.00, 5,  12.00,   0.00, 50.00),
            -- AIRPORT transfers
            ('AIRPORT',    200.00, 5,  14.00,   0.00, 75.00),
            -- OUTSTATION (generic — used when ONE_WAY or ROUND_TRIP not found)
            ('OUTSTATION', 300.00, 0,  14.00, 300.00, 100.00),
            -- ONE_WAY outstation
            ('ONE_WAY',    300.00, 0,  14.00, 300.00, 100.00),
            -- ROUND_TRIP outstation
            ('ROUND_TRIP', 300.00, 0,  12.00, 250.00, 100.00)
        ) AS t(trip_type, base_fare, minimum_km, per_km_rate, driver_allowance, night_charge)
        ON CONFLICT (vehicle_category_id, trip_type) DO UPDATE SET
            base_fare        = EXCLUDED.base_fare,
            minimum_km       = EXCLUDED.minimum_km,
            per_km_rate      = EXCLUDED.per_km_rate,
            driver_allowance = EXCLUDED.driver_allowance,
            night_charge     = EXCLUDED.night_charge,
            updated_at       = NOW()
    """))

    # Apply category-specific multipliers to make defaults realistic:
    #   HATCHBACK  → 1.0x  (baseline above)
    #   SEDAN      → 1.1x
    #   SUV        → 1.4x
    #   MUV        → 1.35x
    #   TEMPO_TRAVELLER → 2.0x
    #   MINI_BUS   → 2.8x
    #   LUXURY     → 2.5x
    conn.execute(sa.text("""
        UPDATE default_vehicle_pricing_rules dvp
        SET
            base_fare        = ROUND(dvp.base_fare        * m.mult, 2),
            per_km_rate      = ROUND(dvp.per_km_rate      * m.mult, 2),
            driver_allowance = ROUND(dvp.driver_allowance * m.mult, 2),
            night_charge     = ROUND(dvp.night_charge     * m.mult, 2)
        FROM (
            SELECT vc.id, vc.category_name,
                   CASE vc.category_name
                       WHEN 'HATCHBACK'       THEN 1.00
                       WHEN 'SEDAN'           THEN 1.10
                       WHEN 'SUV'             THEN 1.40
                       WHEN 'MUV'             THEN 1.35
                       WHEN 'TEMPO_TRAVELLER' THEN 2.00
                       WHEN 'MINI_BUS'        THEN 2.80
                       WHEN 'LUXURY'          THEN 2.50
                       ELSE 1.00
                   END AS mult
            FROM vehicle_categories vc
        ) m
        WHERE dvp.vehicle_category_id = m.id
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_def_pricing_cat_trip"))
    conn.execute(sa.text("DROP TABLE IF EXISTS default_vehicle_pricing_rules"))
