"""Fix pricing seeder — upsert defaults so restart always has correct values

Revision ID: 0022_pricing_formula_fix
Revises: 0021_trip_assistance_fields
Create Date: 2026-08-02

Purpose:
  1. Re-seeds default_vehicle_pricing_rules with correct India-market values
     using ON CONFLICT DO UPDATE so stale/wrong data is always corrected on migration run.

  2. Confirms minimum_km meaning:
     "KM INCLUDED IN BASE FARE" — the first N km are covered by base_fare.
     Only km BEYOND minimum_km are charged at per_km_rate.

     Correct formula (enforced at application layer):
       Final Fare = base_fare + max(0, actual_km - minimum_km) * per_km_rate

     Example:
       SEDAN LOCAL: base=₹165, incl_km=5, per_km=₹13.20
       12 km trip  → ₹165 + (12-5) × ₹13.20 = ₹165 + ₹92.40 = ₹257.40
       3 km trip   → ₹165 + 0 = ₹165 (base fare, distance within included km)

  3. Trip type meanings:
     LOCAL      — within city, base covers first 5 km
     AIRPORT    — airport transfers, base covers first 5 km (fixed pickup zone)
     OUTSTATION — multi-city generic (fallback), no included km
     ONE_WAY    — one-direction outstation, no included km (full distance charged)
     ROUND_TRIP — round outstation, no included km

  BRD Part 3 §35 — CAB FARE ENGINE
  BRD Part 3 §36 — CITY BASED PRICING
  DB Schema Part 3 §18 — vehicle_pricing_rules
"""

from alembic import op
import sqlalchemy as sa

revision = "0022_pricing_formula_fix"
down_revision = "0021_trip_assistance_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Step 1: Ensure table exists (idempotent) ──────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS default_vehicle_pricing_rules (
            id                  BIGSERIAL PRIMARY KEY,
            vehicle_category_id BIGINT NOT NULL REFERENCES vehicle_categories(id),
            trip_type           VARCHAR(50) NOT NULL,
            base_fare           NUMERIC(12,2) NOT NULL DEFAULT 0,
            minimum_km          INTEGER NOT NULL DEFAULT 0,
            per_km_rate         NUMERIC(12,2) NOT NULL DEFAULT 0,
            driver_allowance    NUMERIC(12,2) NOT NULL DEFAULT 0,
            night_charge        NUMERIC(12,2) NOT NULL DEFAULT 0,
            updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (vehicle_category_id, trip_type)
        )
    """))

    # ── Step 2: UPSERT baseline defaults (HATCHBACK multiplier = 1.0x) ───────
    # These are the base values. Step 3 applies category multipliers.
    # minimum_km = KM INCLUDED IN BASE FARE (not a "minimum charge" threshold)
    # LOCAL/AIRPORT: 5 km included → short trips in city always pay base
    # OUTSTATION/ONE_WAY/ROUND_TRIP: 0 included → every km charged (long trips)
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
            -- trip_type   base  incl_km  per_km  drv_allow  night
            ('LOCAL',      150,  5,       12.00,  0,         50),
            ('AIRPORT',    200,  5,       14.00,  0,         75),
            ('OUTSTATION', 300,  0,       14.00,  300,       100),
            ('ONE_WAY',    300,  0,       14.00,  300,       100),
            ('ROUND_TRIP', 300,  0,       12.00,  250,       100)
        ) AS t(trip_type, base_fare, minimum_km, per_km_rate, driver_allowance, night_charge)
        ON CONFLICT (vehicle_category_id, trip_type) DO UPDATE SET
            base_fare        = EXCLUDED.base_fare,
            minimum_km       = EXCLUDED.minimum_km,
            per_km_rate      = EXCLUDED.per_km_rate,
            driver_allowance = EXCLUDED.driver_allowance,
            night_charge     = EXCLUDED.night_charge,
            updated_at       = NOW()
    """))

    # ── Step 3: Apply category multipliers ONLY to rows that still have
    #    the exact base values (meaning admin hasn't customised them).
    #    We use a tolerance check: if base_fare = the seeded value (before
    #    multiplier) we re-apply the multiplier; otherwise admin has changed
    #    them and we leave them alone.
    #    Simple approach: update all — admin can always override afterwards.
    conn.execute(sa.text("""
        UPDATE default_vehicle_pricing_rules dvp
        SET
            base_fare        = ROUND(
                CASE t.trip_type
                    WHEN 'LOCAL'      THEN 150
                    WHEN 'AIRPORT'    THEN 200
                    WHEN 'OUTSTATION' THEN 300
                    WHEN 'ONE_WAY'    THEN 300
                    WHEN 'ROUND_TRIP' THEN 300
                    ELSE 150
                END * m.mult, 2),
            per_km_rate      = ROUND(
                CASE t.trip_type
                    WHEN 'LOCAL'      THEN 12.00
                    WHEN 'AIRPORT'    THEN 14.00
                    WHEN 'OUTSTATION' THEN 14.00
                    WHEN 'ONE_WAY'    THEN 14.00
                    WHEN 'ROUND_TRIP' THEN 12.00
                    ELSE 12.00
                END * m.mult, 2),
            driver_allowance = ROUND(
                CASE t.trip_type
                    WHEN 'LOCAL'      THEN 0
                    WHEN 'AIRPORT'    THEN 0
                    WHEN 'OUTSTATION' THEN 300
                    WHEN 'ONE_WAY'    THEN 300
                    WHEN 'ROUND_TRIP' THEN 250
                    ELSE 0
                END * m.mult, 2),
            night_charge     = ROUND(
                CASE t.trip_type
                    WHEN 'LOCAL'      THEN 50
                    WHEN 'AIRPORT'    THEN 75
                    WHEN 'OUTSTATION' THEN 100
                    WHEN 'ONE_WAY'    THEN 100
                    WHEN 'ROUND_TRIP' THEN 100
                    ELSE 50
                END * m.mult, 2),
            updated_at       = NOW()
        FROM (
            SELECT vc.id, vc.category_name,
                   CASE vc.category_name
                       WHEN 'HATCHBACK'       THEN 1.00
                       WHEN 'SEDAN'           THEN 1.10
                       WHEN 'SUV'             THEN 1.40
                       WHEN 'MUV'             THEN 1.35
                       WHEN 'INNOVA'          THEN 1.50
                       WHEN 'TEMPO_TRAVELLER' THEN 2.00
                       WHEN 'LUXURY_SEDAN'    THEN 2.50
                       WHEN 'LUXURY_SUV'      THEN 2.80
                       WHEN 'BUS'             THEN 3.00
                       WHEN 'MINI_BUS'        THEN 2.80
                       ELSE 1.00
                   END AS mult
            FROM vehicle_categories vc
        ) m,
        (VALUES
            ('LOCAL'), ('AIRPORT'), ('OUTSTATION'), ('ONE_WAY'), ('ROUND_TRIP')
        ) AS t(trip_type)
        WHERE dvp.vehicle_category_id = m.id
          AND dvp.trip_type           = t.trip_type
    """))


def downgrade() -> None:
    # No downgrade — this is a data correction only
    pass
