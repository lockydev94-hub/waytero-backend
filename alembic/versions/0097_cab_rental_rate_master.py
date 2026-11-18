"""0097 — supplier cab price master as the platform's default rental rate card

Revision ID: 0097
Revises: 0096

Doc Ref: BRD Part 3 §35 (fare engine), §36 (city-based pricing);
Docs/01_Business_Requirements/SUPPLIER_CAB_PRICE_MASTER_V1.md (the sheet this seeds).

Why a new table instead of more rows in ``default_vehicle_pricing_rules``:

  The supplier sheet sells Local rental as **packages** — 5h/50km, 8h/80km,
  10h/100km, 12h/120km — and Outstation/Daily as 12h/250km and 12h/100km per day,
  each with its own extra-hour and extra-kilometre rate. The per-km rule sheet
  (``base_fare`` + included km + ``per_km_rate``) holds one price per
  (category, trip type) and has no hours column at all, so a Sedan's four local
  packages cannot live in it without inventing a "duration" that the fare engine
  would then silently ignore.

  ``cab_rental_rates`` therefore stores one row per quotable package, tagged with
  its rental type, included hours, included kilometres, price, extra-hour/km
  rates, driver allowance and midnight-return charge. That is the sheet, column
  for column, so a price can be traced from the supplier document to a row.

  ``vehicle_class`` is the supplier's own vehicle identity rather than
  ``vehicle_categories.id``: the sheet distinguishes 17 vehicles while the
  category taxonomy has 7 generic classes, and merging them would lose prices
  (Force Urbania 13+D is ₹8,000/10h, Force Tempo Traveller 13+D is ₹5,000/10h —
  both "13 seater"). ``vehicle_classes`` is deliberately plain text so the
  customer-facing category taxonomy is not reshaped by a supplier cost sheet.

Scope of the seed:

  * ``city_id IS NULL`` — platform defaults, used by every city with no sheet.
  * ``city_id = Bhubaneswar`` — the same numbers as the city's own sheet, so
    Bhubaneswar is explicit and can be revised independently later.

  The existing per-km ``default_vehicle_pricing_rules`` are left untouched: they
  still price any trip that does not select a package, and the package card is
  additive. Nothing here changes a live quote by itself.

  Totals: 16 rate classes (17 supplier vehicles; Ertiga/Rumion and
  Bolero/Tavera are quoted identically and share ``MUV``), 36 local packages,
  16 outstation packages, 16 daily packages = 68 rows per scope, 136 seeded.

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, and the seed uses
ON CONFLICT … DO UPDATE so re-running corrects prices instead of duplicating.
"""

from alembic import op
import sqlalchemy as sa

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


# ── The sheet ────────────────────────────────────────────────────────────────
# (code, label, models, seats,
#  local packages [(hours, km, price), …], (local extra_hour, extra_km),
#  outstation (price, extra_hour, extra_km, driver_allowance, midnight),
#  daily price)
_CLASSES = (
    (
        "SEDAN",
        "Sedan",
        "Swift Dzire / Hyundai Aura / Honda Amaze",
        4,
        ((5, 50, 1800), (8, 80, 2200), (10, 100, 2500), (12, 120, 3000)),
        (120, 12),
        (4000, 150, 12, 150, 300),
        3500,
    ),
    (
        "MUV",
        "MUV / 6–7 Seater",
        "Maruti Ertiga / Toyota Rumion; Mahindra Bolero / Chevrolet Tavera",
        7,
        ((8, 80, 2800), (10, 100, 3200), (12, 120, 3500)),
        (150, 15),
        (5000, 150, 15, 150, 300),
        4000,
    ),
    (
        "PREMIUM_MUV",
        "Premium MUV",
        "Kia Carens",
        7,
        ((8, 80, 3000), (10, 100, 3500), (12, 120, 3800)),
        (150, 15),
        (5000, 150, 15, 150, 300),
        4500,
    ),
    (
        "SUV",
        "Premium SUV",
        "Toyota Innova Crysta",
        6,
        ((8, 80, 3200), (10, 100, 3500), (12, 120, 4000)),
        (150, 15),
        (5000, 150, 15, 150, 300),
        5000,
    ),
    (
        "URBANIA_13D",
        "Force Urbania 13+D Seater",
        "Force Urbania 13+D",
        13,
        ((10, 100, 8000), (12, 120, 10000)),
        (300, 40),
        (12000, 300, 40, 200, 500),
        10000,
    ),
    (
        "URBANIA_16D",
        "Force Urbania 16+D Seater",
        "Force Urbania 16+D",
        16,
        ((10, 100, 10000), (12, 120, 12000)),
        (300, 45),
        (15000, 300, 45, 200, 500),
        12000,
    ),
    (
        "TRAVELLER_13D",
        "Force Tempo Traveller 13+D Seater",
        "Force Tempo Traveller 13+D",
        13,
        ((10, 100, 5000), (12, 120, 6000)),
        (250, 30),
        (7000, 250, 30, 200, 400),
        7000,
    ),
    (
        "TRAVELLER_17D",
        "Force Tempo Traveller 17+D Seater",
        "Force Tempo Traveller 17+D",
        17,
        ((10, 100, 5500), (12, 120, 6500)),
        (250, 30),
        (8000, 250, 30, 200, 400),
        8000,
    ),
    (
        "TRAVELLER_20D",
        "Force Tempo Traveller 20+D Seater",
        "Force Tempo Traveller 20+D",
        20,
        ((10, 100, 6000), (12, 120, 7000)),
        (250, 35),
        (9000, 250, 35, 200, 500),
        9000,
    ),
    (
        "TRAVELLER_25D",
        "Force Tempo Traveller 25+D Seater",
        "Force Tempo Traveller 25+D",
        25,
        ((10, 100, 7000), (12, 120, 8000)),
        (300, 35),
        (10000, 300, 35, 200, 500),
        10000,
    ),
    (
        "SML_BUS_13D",
        "SML Bus 13+D Seater",
        "SML Bus 13+D",
        13,
        ((10, 100, 8000), (12, 120, 10000)),
        (300, 40),
        (12000, 300, 40, 200, 500),
        10000,
    ),
    (
        "SML_BUS_19D",
        "SML Bus 19+D Seater",
        "SML Bus 19+D",
        19,
        ((10, 100, 10000), (12, 120, 12000)),
        (300, 45),
        (15000, 300, 45, 200, 500),
        12000,
    ),
    (
        "SML_BUS_28D",
        "SML Bus 28+D Seater",
        "SML Bus 28+D",
        28,
        ((10, 100, 11000), (12, 120, 12000)),
        (400, 45),
        (16000, 400, 45, 200, 500),
        13000,
    ),
    (
        "SML_BUS_38D",
        "SML Bus 38+D Seater",
        "SML Bus 38+D",
        38,
        ((10, 100, 12000), (12, 120, 15000)),
        (500, 50),
        (18000, 500, 50, 200, 500),
        15000,
    ),
    (
        "AC_BUS_40D",
        "AC Bus 2/2 Pushback 40+D Seater",
        "AC Bus 2/2 Pushback 40+D",
        40,
        ((10, 100, 12000), (12, 120, 15000)),
        (500, 50),
        (18000, 500, 50, 500, 500),
        15000,
    ),
    (
        "AC_BUS_50D",
        "AC Bus 2/3 Non-Pushback 50/55+D Seater",
        "AC Bus 2/3 Non-Pushback 50/55+D",
        55,
        ((12, 120, 15000),),
        (500, 60),
        (20000, 500, 60, 500, 500),
        18000,
    ),
)

_OUTSTATION_HOURS = 12
_OUTSTATION_KM = 250
_DAILY_HOURS = 12
_DAILY_KM = 100

_INSERT = sa.text(
    """
    INSERT INTO cab_rental_rates (
        city_id, vehicle_class, vehicle_label, vehicle_models, seating_capacity,
        rental_type, included_hours, included_km, price,
        extra_hour_rate, extra_km_rate, driver_allowance,
        toll_included, parking_included, night_halt_included,
        midnight_return_charge, currency, source
    ) VALUES (
        :city_id, :vehicle_class, :label, :models, :seats,
        :rental_type, :included_hours, :included_km, :price,
        :extra_hour_rate, :extra_km_rate, :driver_allowance,
        :toll_included, :parking_included, :night_halt_included,
        :midnight_return_charge, 'INR', 'SUPPLIER'
    )
    ON CONFLICT ON CONSTRAINT uq_cab_rental_rates DO UPDATE SET
        vehicle_label        = EXCLUDED.vehicle_label,
        vehicle_models       = EXCLUDED.vehicle_models,
        seating_capacity     = EXCLUDED.seating_capacity,
        price                = EXCLUDED.price,
        extra_hour_rate      = EXCLUDED.extra_hour_rate,
        extra_km_rate        = EXCLUDED.extra_km_rate,
        driver_allowance     = EXCLUDED.driver_allowance,
        toll_included        = EXCLUDED.toll_included,
        parking_included     = EXCLUDED.parking_included,
        night_halt_included  = EXCLUDED.night_halt_included,
        midnight_return_charge = EXCLUDED.midnight_return_charge,
        updated_at           = NOW()
    """
)


def _rows_for(scope_city_id):
    """Every package row for one scope (None = platform default)."""
    rows = []
    for (
        code,
        label,
        models,
        seats,
        local,
        (local_xh, local_xkm),
        out,
        daily,
    ) in _CLASSES:
        base = {
            "city_id": scope_city_id,
            "vehicle_class": code,
            "label": label,
            "models": models,
            "seats": seats,
        }
        for hours, km, price in local:
            rows.append(
                {
                    **base,
                    "rental_type": "LOCAL",
                    "included_hours": hours,
                    "included_km": km,
                    "price": price,
                    "extra_hour_rate": local_xh,
                    "extra_km_rate": local_xkm,
                    "driver_allowance": 0,
                    "toll_included": False,
                    "parking_included": False,
                    "night_halt_included": False,
                    "midnight_return_charge": None,
                }
            )
        out_price, out_xh, out_xkm, out_da, out_midnight = out
        rows.append(
            {
                **base,
                "rental_type": "OUTSTATION",
                "included_hours": _OUTSTATION_HOURS,
                "included_km": _OUTSTATION_KM,
                "price": out_price,
                "extra_hour_rate": out_xh,
                "extra_km_rate": out_xkm,
                "driver_allowance": out_da,
                "toll_included": False,
                "parking_included": False,
                "night_halt_included": False,
                "midnight_return_charge": out_midnight,
            }
        )
        # The daily package includes toll, parking, driver allowance and night
        # halt; overtime beyond 12h/100km per day is charged at the outstation
        # extra rates (documented in the master, §1).
        rows.append(
            {
                **base,
                "rental_type": "DAILY",
                "included_hours": _DAILY_HOURS,
                "included_km": _DAILY_KM,
                "price": daily,
                "extra_hour_rate": out_xh,
                "extra_km_rate": out_xkm,
                "driver_allowance": 0,
                "toll_included": True,
                "parking_included": True,
                "night_halt_included": True,
                "midnight_return_charge": None,
            }
        )
    return rows


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
        CREATE TABLE IF NOT EXISTS cab_rental_rates (
            id                     BIGSERIAL PRIMARY KEY,
            -- NULL = platform default; set = that city's own sheet.
            city_id                BIGINT REFERENCES cities(id) ON DELETE CASCADE,
            vehicle_class          VARCHAR(40)  NOT NULL,
            vehicle_label          VARCHAR(120) NOT NULL,
            vehicle_models         VARCHAR(255),
            seating_capacity       INTEGER,
            rental_type            VARCHAR(20)  NOT NULL,
            included_hours         INTEGER      NOT NULL,
            included_km            INTEGER      NOT NULL,
            price                  NUMERIC(12,2) NOT NULL,
            extra_hour_rate        NUMERIC(12,2),
            extra_km_rate          NUMERIC(12,2),
            driver_allowance       NUMERIC(12,2),
            toll_included          BOOLEAN      NOT NULL DEFAULT FALSE,
            parking_included       BOOLEAN      NOT NULL DEFAULT FALSE,
            night_halt_included    BOOLEAN      NOT NULL DEFAULT FALSE,
            midnight_return_charge NUMERIC(12,2),
            currency               VARCHAR(3)   NOT NULL DEFAULT 'INR',
            source                 VARCHAR(40)  NOT NULL DEFAULT 'SUPPLIER',
            effective_from         DATE,
            is_active              BOOLEAN      NOT NULL DEFAULT TRUE,
            updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_cab_rental_rates_type
                CHECK (rental_type IN ('LOCAL', 'OUTSTATION', 'DAILY')),
            CONSTRAINT ck_cab_rental_rates_price
                CHECK (price >= 0 AND included_hours > 0 AND included_km > 0)
        )
    """
        )
    )

    # NULLS NOT DISTINCT so the platform-default sheet (city_id NULL) is unique
    # too — a plain UNIQUE would let every re-run insert a second default row.
    conn.execute(
        sa.text(
            """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_cab_rental_rates'
                  AND conrelid = 'cab_rental_rates'::regclass
            ) THEN
                ALTER TABLE cab_rental_rates
                    ADD CONSTRAINT uq_cab_rental_rates
                    UNIQUE NULLS NOT DISTINCT
                    (city_id, vehicle_class, rental_type, included_hours, included_km);
            END IF;
        END $$
    """
        )
    )

    conn.execute(
        sa.text(
            """
        CREATE INDEX IF NOT EXISTS idx_cab_rental_rates_lookup
        ON cab_rental_rates (vehicle_class, rental_type)
        WHERE is_active = TRUE
    """
        )
    )

    # ── Seed: platform defaults, then Bhubaneswar's own sheet ───────────────
    bbsr_id = conn.execute(
        sa.text(
            "SELECT id FROM cities WHERE LOWER(name) = 'bhubaneswar' "
            "ORDER BY id LIMIT 1"
        )
    ).scalar()

    # bbsr_id can be None on a database where the city has not been created
    # yet; the platform-default sheet still seeds, and the city sheet is
    # seeded by the next migration run once the city exists.
    scopes = [None] if bbsr_id is None else [None, bbsr_id]
    for city_id in scopes:
        for row in _rows_for(city_id):
            conn.execute(_INSERT, row)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_rental_rates_lookup"))
    conn.execute(sa.text("DROP TABLE IF EXISTS cab_rental_rates"))
