"""0098 — package booking links and customer-specific rate overrides

Doc Ref: Docs/01_Business_Requirements/SUPPLIER_CAB_PRICE_MASTER_V1.md §1, §3
         BRD Part 3 §35 (fare engine), §36 (city-based pricing)

Two additions on top of 0097's rate card:

1. ``cab_rental_rates.vehicle_category_id`` — the bookable category a rate class
   belongs to. The rate card sells 16 supplier vehicle classes; ``cab_bookings``
   and partner assignment speak in the 7 ``vehicle_categories``. Without this
   link a package booking could name a class nobody's fleet is matched against.
   Backfilled here, so the column can stay nullable for a class an admin adds
   later and has not mapped yet.

2. ``cab_custom_quotes`` — an approved price structure for one customer. One row
   overrides the money lines of one (city, class, rental type) rate-card quote:
   the package price and/or the extra-hour and extra-km rates. Null means "use
   the rate card line"; a value means "this customer agreed this instead". The
   standard quote is never mutated — the booking keeps both sides, which is what
   lets the desk show the difference rather than a number with no context.

   A customer's city-specific row beats their all-city row. Only one active row
   may exist per (customer, city, class, rental type); the partial unique index
   enforces that with ``COALESCE(city_id, 0)`` so the all-city row is unique too.

Idempotent, raw SQL — matches the 0097 style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

# Rate class -> bookable category. The card has no Urbania/Traveller split by
# seats at category level and no bus category at all, so the mapping is to the
# closest fleet category a partner can actually accept the job in. Seats on the
# rate class remain the booking-time truth for the vehicle.
_CATEGORY_BY_CLASS = {
    "SEDAN": "SEDAN",
    "MUV": "MUV",
    "PREMIUM_MUV": "MUV",
    "SUV": "SUV",
    "TRAVELLER_13D": "TEMPO_TRAVELLER",
    "TRAVELLER_17D": "TEMPO_TRAVELLER",
    "TRAVELLER_20D": "TEMPO_TRAVELLER",
    "TRAVELLER_25D": "TEMPO_TRAVELLER",
    "URBANIA_13D": "TEMPO_TRAVELLER",
    "URBANIA_16D": "TEMPO_TRAVELLER",
    "SML_BUS_13D": "MINI_BUS",
    "SML_BUS_19D": "MINI_BUS",
    "SML_BUS_28D": "MINI_BUS",
    "SML_BUS_38D": "MINI_BUS",
    "AC_BUS_40D": "MINI_BUS",
    "AC_BUS_50D": "MINI_BUS",
}


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_rental_rates
                ADD COLUMN IF NOT EXISTS vehicle_category_id INTEGER
                    REFERENCES vehicle_categories(id)
            """
        )
    )

    # Backfill only unmapped rows, so a deliberate re-mapping by an admin is not
    # undone by re-running the migration.
    for vehicle_class, category_name in _CATEGORY_BY_CLASS.items():
        conn.execute(
            sa.text(
                """
                UPDATE cab_rental_rates r
                   SET vehicle_category_id = c.id
                  FROM vehicle_categories c
                 WHERE r.vehicle_class = :vehicle_class
                   AND r.vehicle_category_id IS NULL
                   AND LOWER(c.category_name) = LOWER(:category_name)
                """
            ),
            {"vehicle_class": vehicle_class, "category_name": category_name},
        )

    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_cab_rental_rates_category
                ON cab_rental_rates (vehicle_category_id)
            """
        )
    )

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS cab_custom_quotes (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL
                    REFERENCES customers(id) ON DELETE CASCADE,
                city_id INTEGER REFERENCES cities(id),
                vehicle_class VARCHAR(40) NOT NULL,
                rental_type VARCHAR(20) NOT NULL,
                -- Which package the price above is for. A negotiated rate is
                -- about "your 8h/80km Sedan", not "any Sedan package", and
                -- keying on it stops a 12h booking from inheriting the 5h
                -- price an admin agreed for the same customer.
                included_hours INTEGER NOT NULL,
                package_price NUMERIC(12, 2),
                extra_hour_rate NUMERIC(10, 2),
                extra_km_rate NUMERIC(10, 2),
                valid_from DATE,
                valid_to DATE,
                reason TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by UUID REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT ck_cab_custom_quotes_type
                    CHECK (rental_type IN ('LOCAL', 'OUTSTATION', 'DAILY')),
                CONSTRAINT ck_cab_custom_quotes_amounts
                    CHECK (
                        (package_price IS NULL OR package_price >= 0)
                        AND (extra_hour_rate IS NULL OR extra_hour_rate >= 0)
                        AND (extra_km_rate IS NULL OR extra_km_rate >= 0)
                    ),
                CONSTRAINT ck_cab_custom_quotes_window
                    CHECK (
                        valid_from IS NULL OR valid_to IS NULL
                        OR valid_from <= valid_to
                    ),
                -- An override that overrides nothing is an empty promise to the
                -- customer; require at least one money line.
                CONSTRAINT ck_cab_custom_quotes_not_empty
                    CHECK (
                        package_price IS NOT NULL
                        OR extra_hour_rate IS NOT NULL
                        OR extra_km_rate IS NOT NULL
                    )
            )
            """
        )
    )

    conn.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_cab_custom_quotes_active
                ON cab_custom_quotes (
                    customer_id, COALESCE(city_id, 0), vehicle_class,
                    rental_type, included_hours
                )
                WHERE is_active
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_cab_custom_quotes_customer
                ON cab_custom_quotes (customer_id, is_active)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS cab_custom_quotes"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_cab_rental_rates_category"))
    conn.execute(
        sa.text(
            "ALTER TABLE cab_rental_rates DROP COLUMN IF EXISTS vehicle_category_id"
        )
    )
