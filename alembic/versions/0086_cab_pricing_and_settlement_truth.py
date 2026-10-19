"""0086_cab_pricing_and_settlement_truth — fare lines on the cab row, route tolls,
period settlement statement lines, and a lockable invoice counter

Revision ID: 0086
Revises: 0085

Doc Ref: Round 15 plan C1; BRD Part 3 §35 (fare formula), §45 (invoice at payment);
DB Schema Part 3 §18 (pricing rules), Part 7 §15 (settlements).

Why:
  The cab chain could not answer three questions the owner asks every day, and
  each one needed a column that does not exist yet.

  * "What is the customer actually paying, and why?" — `cab_bookings` carried one
    number (`estimated_amount`, then `final_amount`) and no line items. The fare
    engine already computes them (`booking/services/fare.py:calculate_fare_breakdown`
    returns base, distance charge, driver allowance, night, toll, waiting), but
    every caller threw the lines away and kept the total. So the invoice, the
    settlement line and the phone-desk screen each re-derived the price from
    whatever rule happened to be current, and could disagree. Persisting the
    lines makes the stored row the answer.

  * "Where did the kilometres come from?" — `estimated_distance` was whatever the
    browser sent (`public_cab_api.py` priced from `body.estimated_distance_km`
    verbatim) or whatever the agent typed (`CabBookingCreateByAdmin`).
    `distance_source` and `billing_distance_km` record the server's own
    resolution, and `billing_distance_km` is deliberately separate from
    `estimated_distance`: for ONE_WAY / ROUND_TRIP / OUTSTATION the billable
    figure is `MAX(actual, minimum_km)` (`fare.py:_BILLABLE_MIN_TRIP_TYPES`), so
    "23 km driven" and "30 km billed" are both true and both needed.

  * "What commission was in force when this settled?" — `hotel_reservations`
    already snapshots both its rate and its commission config; `cab_bookings`
    stored only the computed `platform_commission` amount. The owner's rule is
    that nobody adjusts a commission figure, which requires the rule to be on
    the row so a later edit to `commission_rules` cannot reprice history.

  Nothing here re-prices an existing booking. All new columns are nullable and
  unbackfilled: a pre-0086 row with `distance_source IS NULL` means "priced
  before the server took responsibility for the distance", which is a truthful
  statement, whereas backfilling `GOOGLE` would be a fabrication and backfilling
  zero would silently reprice real money.

  `cab_routes.toll_charge` — the owner's "some routes have toll, it is not
  included in the booking, add it as an extra amount". `cab_routes` already
  carries `pricing_city_id` and `distance_km`, so a matched route can answer
  distance and toll together. Nullable rather than DEFAULT 0: a route whose toll
  nobody has confirmed is not a route whose toll is zero.

  `settlement_items` gains the frozen per-line figures and a pointer at the
  wallet ledger row that already moved the money. `booking_id` + `booking_type`
  already say which booking a line is, so no `cab_booking_id` is added — that
  would be a second way to say the same thing.

  `invoice_sequences` replaces four string-arithmetic invoice generators
  (`trip_assistance_api.py` has two of them, disagreeing: MAX+1 versus COUNT+1;
  `partner/booking_api.py` a third; `hotel/services/billing.py:next_invoice_number`
  a fourth that reads `ORDER BY id DESC LIMIT 1` with no lock). A counter row
  taken with FOR UPDATE is the only shape in Postgres where two concurrent
  payments cannot produce the same invoice number.

  `settlements` needs no re-shaping — it is already a period statement
  (`start_date`, `end_date`, gross/commission/gst/net_payable, `settlement_status`)
  and has 0 rows, so nothing depends on its current emptiness. What it lacked was
  a guard against generating the same partner-period twice, which the unique
  index adds: a duplicate document becomes an IntegrityError the service turns
  into a 422, not a second set of books.

Everything is idempotent: ADD COLUMN IF NOT EXISTS, CREATE TABLE / CREATE INDEX
IF NOT EXISTS, the DO-block constraint guard, INSERT … WHERE NOT EXISTS.
"""

from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


# (column, type) for the cab_bookings fare lines. Kept as one tuple so upgrade
# and downgrade cannot drift on the list.
_CAB_FARE_COLUMNS = (
    ("pricing_city_id", "BIGINT"),
    ("distance_source", "VARCHAR(20)"),
    ("billing_distance_km", "NUMERIC(10,2)"),
    ("base_fare_amount", "NUMERIC(12,2)"),
    ("distance_charge_amount", "NUMERIC(12,2)"),
    ("driver_allowance_amount", "NUMERIC(12,2)"),
    ("night_charge_amount", "NUMERIC(12,2)"),
    ("toll_amount", "NUMERIC(12,2)"),
    ("waiting_charge_amount", "NUMERIC(12,2)"),
    ("rate_snapshot", "JSONB"),
    ("commission_config_snapshot", "JSONB"),
    ("invoice_voided_at", "TIMESTAMPTZ"),
    ("invoice_void_reason", "VARCHAR(255)"),
    ("needs_attention", "VARCHAR(40)"),
)

# (column, type) for settlement_items: the frozen figures of one line, and the
# wallet ledger row that proves the money already moved for it.
_SETTLEMENT_ITEM_COLUMNS = (
    ("billing_distance_km", "NUMERIC(10,2)"),
    ("coupon_discount", "NUMERIC(12,2)"),
    ("coupon_platform_share", "NUMERIC(12,2)"),
    ("coupon_partner_share", "NUMERIC(12,2)"),
    ("commission_snapshot", "JSONB"),
    ("wallet_ledger_id", "BIGINT"),
)

# Config the auto-assign chain reads, seeded so the settings screen shows the
# keys instead of the operator guessing their spelling. Values are the current
# behaviour's equivalent: the chain stays off until the owner turns it on, so
# this migration changes nothing a partner can feel.
_AUTO_ASSIGN_CONFIG = (
    ("auto_assign_enabled", "false",
     "Offer cabs to partners automatically instead of waiting for a human pick."),
    ("auto_assign_round_size", "3",
     "How many partners are offered the same cab in one round."),
    ("auto_assign_max_rounds", "3",
     "Rounds to try before handing the cab back to the desk."),
    ("auto_assign_timeout_seconds", "180",
     "Seconds a partner has to accept before the offer expires."),
)


def _fk(table: str, name: str, column: str, ref_table: str, actions: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = '{name}' AND conrelid = '{table}'::regclass)
            THEN ALTER TABLE {table} ADD CONSTRAINT {name}
                 FOREIGN KEY ({column}) REFERENCES {ref_table}(id) {actions};
            END IF;
        END $$
    """
    )


def upgrade() -> None:
    # ── cab_bookings: the fare the customer was actually quoted ──────────
    for column, column_type in _CAB_FARE_COLUMNS:
        op.execute(f"ALTER TABLE cab_bookings ADD COLUMN IF NOT EXISTS {column} {column_type}")

    # The city whose rule sheet priced this trip. Kept separate from
    # master_bookings.city_id because the pricing city is a fact about the quote
    # (it can be the pickup city while the booking is filed elsewhere) and
    # because cab_routes already names its pricing city the same way.
    _fk("cab_bookings", "fk_cab_bookings_pricing_city", "pricing_city_id",
        "cities", "ON DELETE SET NULL")

    # A voided invoice is a document that must never be reissued, so the reason
    # travels with the timestamp — a void with no explanation is unfalsifiable.
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = 'ck_cab_bookings_void_reason'
                             AND conrelid = 'cab_bookings'::regclass)
            THEN ALTER TABLE cab_bookings ADD CONSTRAINT ck_cab_bookings_void_reason
                 CHECK (invoice_voided_at IS NULL
                        OR (invoice_void_reason IS NOT NULL
                            AND length(trim(invoice_void_reason)) > 0));
            END IF;
        END $$
        """
    )

    # The desk queue filter: only rows the chain gave up on are interesting.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cab_bookings_needs_attention
            ON cab_bookings (needs_attention)
            WHERE needs_attention IS NOT NULL
        """
    )

    # ── cab_routes: toll, and the city pair that resolves a distance ─────
    op.execute("ALTER TABLE cab_routes ADD COLUMN IF NOT EXISTS toll_charge NUMERIC(12,2)")
    op.execute("ALTER TABLE cab_routes ADD COLUMN IF NOT EXISTS toll_notes VARCHAR(255)")
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = 'ck_cab_routes_toll_nonneg'
                             AND conrelid = 'cab_routes'::regclass)
            THEN ALTER TABLE cab_routes ADD CONSTRAINT ck_cab_routes_toll_nonneg
                 CHECK (toll_charge IS NULL OR toll_charge >= 0);
            END IF;
        END $$
        """
    )
    # Distance resolution looks a route up by its city pair. The table is 16
    # rows today; the index is here because the lookup becomes a per-quote
    # query, not because the scan is slow.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cab_routes_city_pair
            ON cab_routes (origin_city_id, destination_city_id)
            WHERE is_active = TRUE
        """
    )

    # ── settlements: refuse the same partner-period twice ────────────────
    # settlement_type is nullable, and in Postgres a UNIQUE index lets any
    # number of NULL through — so the index is on the coalesced value, which is
    # what actually stops a duplicate statement.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_settlements_partner_period
            ON settlements (
                partner_id,
                COALESCE(settlement_type, 'CAB'),
                start_date,
                end_date
            )
        """
    )

    # ── settlement_items: the frozen line ───────────────────────────────
    for column, column_type in _SETTLEMENT_ITEM_COLUMNS:
        op.execute(
            f"ALTER TABLE settlement_items ADD COLUMN IF NOT EXISTS {column} {column_type}"
        )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_settlement_items_ledger
            ON settlement_items (wallet_ledger_id)
            WHERE wallet_ledger_id IS NOT NULL
        """
    )

    # ── invoice_sequences: one counter per document kind ────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS invoice_sequences (
            kind     VARCHAR(20) PRIMARY KEY,
            last_seq BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    for kind in ("CAB", "HOTEL"):
        op.execute(
            f"""
            INSERT INTO invoice_sequences (kind, last_seq)
            SELECT '{kind}', 0
            WHERE NOT EXISTS (SELECT 1 FROM invoice_sequences WHERE kind = '{kind}')
            """
        )
    # Seeded from the highest number already in the table, so the first
    # counter-issued invoice cannot collide with an existing one. Reading the
    # old MAX is legitimate here in a way it is not at request time: it runs
    # once, inside the migration's own transaction.
    #
    # The two kinds are seeded differently because their formats differ. A cab
    # number is `WT-INV-<YYYYMM>-<5 digits>` — the month is its own hyphenated
    # part, so the sequence is the 4th part and runs globally. A hotel number is
    # `WT-HINV-<year>` immediately followed by `<6 digits>` with no separator,
    # so the sequence starts at character 13 and the year is not part of it.
    op.execute(
        """
        UPDATE invoice_sequences s
           SET last_seq = COALESCE(
                   (SELECT MAX(CAST(SPLIT_PART(c.invoice_number, '-', 4) AS BIGINT))
                      FROM cab_bookings c
                     WHERE c.invoice_number LIKE 'WT-INV-%'),
                   0)
         WHERE s.kind = 'CAB' AND s.last_seq = 0
        """
    )
    op.execute(
        """
        UPDATE invoice_sequences s
           SET last_seq = COALESCE(
                   (SELECT MAX(CAST(SUBSTRING(h.invoice_number FROM 13) AS BIGINT))
                      FROM hotel_reservations h
                     WHERE LENGTH(h.invoice_number) >= 13
                       AND h.invoice_number LIKE 'WT-HINV-%'),
                   0)
         WHERE s.kind = 'HOTEL' AND s.last_seq = 0
        """
    )

    # ── auto-assign config ──────────────────────────────────────────────
    for key, value, description in _AUTO_ASSIGN_CONFIG:
        op.execute(
            f"""
            INSERT INTO system_configurations (config_key, config_value, description, updated_at)
            SELECT '{key}', '{value}', '{description}', NOW()
            WHERE NOT EXISTS (SELECT 1 FROM system_configurations
                              WHERE config_key = '{key}')
            """
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS invoice_sequences")

    for column, _type in _SETTLEMENT_ITEM_COLUMNS:
        op.execute(f"ALTER TABLE settlement_items DROP COLUMN IF EXISTS {column}")
    op.execute("DROP INDEX IF EXISTS idx_settlement_items_ledger")

    op.execute("DROP INDEX IF EXISTS uq_settlements_partner_period")

    op.execute("ALTER TABLE cab_routes DROP CONSTRAINT IF EXISTS ck_cab_routes_toll_nonneg")
    op.execute("DROP INDEX IF EXISTS idx_cab_routes_city_pair")
    op.execute("ALTER TABLE cab_routes DROP COLUMN IF EXISTS toll_notes")
    op.execute("ALTER TABLE cab_routes DROP COLUMN IF EXISTS toll_charge")

    for key, _value, _description in _AUTO_ASSIGN_CONFIG:
        op.execute(
            f"DELETE FROM system_configurations WHERE config_key = '{key}'"
        )

    op.execute("ALTER TABLE cab_bookings DROP CONSTRAINT IF EXISTS ck_cab_bookings_void_reason")
    op.execute("DROP INDEX IF EXISTS idx_cab_bookings_needs_attention")
    op.execute("ALTER TABLE cab_bookings DROP CONSTRAINT IF EXISTS fk_cab_bookings_pricing_city")
    for column, _type in _CAB_FARE_COLUMNS:
        op.execute(f"ALTER TABLE cab_bookings DROP COLUMN IF EXISTS {column}")
