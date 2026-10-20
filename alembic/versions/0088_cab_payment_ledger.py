"""0088_cab_payment_ledger — every rupee a customer pays leaves a row

Revision ID: 0088
Revises: 0087

Doc Ref: DB Schema Part 7 §3 (payments), §22 (payment rules — "every payment
belongs to one MTB", "multiple payments allowed per booking"); BRD Part 3 §45
(invoice at payment); Round 15 plan C4b.

Why:
  `payments` was created by the finance migration and has never had a single row
  written to it. Two admin reports already read it — the GST invoice join
  (`api_dashboard.py:4313`) and the customer payment history
  (`api_dashboard.py:4715`, which also joins `refunds` through it) — so both
  silently report "no payments" for customers who paid cash at the desk, and the
  finance module the owner's settlement workflow is supposed to sit on is empty.

  Worse, nothing records *how much the customer actually paid in* on a cab
  booking except `master_bookings.total_paid_amount`, which the collect-payment
  endpoints overwrite with a figure they compute themselves, and
  `master_bookings.payment_status`, which the admin desk and the partner portal
  hard-code to 'PAID' the moment a balance is recorded — before checking that
  the advance, the coupon and the fare add up. A booking the customer only paid
  half of reads 'PAID' to every report and to the customer's own invoice
  history.

  This migration turns the table into the ledger it was specified to be, for the
  cab money the platform actually takes today:

    * `cab_booking_id` / `advance_payment_id` — what each rupee settled. The
      receipt (`advance_payments`) stays the custody document with its own
      numbered slip and void columns; the ledger row is the money event, joined
      1:1 through `advance_payment_id`. Two tables, two jobs, one relationship
      enforced by a unique index instead of by discipline.
    * `refunded_amount` plus the status checks — money coming back is recorded
      against the payment it came from, so "what did the customer pay" is one
      subtraction away and cannot be argued with.
    * the CHECK constraints make a row that contradicts itself (REFUNDED with
      nothing refunded, PARTIAL_REFUNDED covering the whole amount, a void with
      no reason) impossible to write, exactly as `ck_cab_bookings_void_reason`
      does for invoices.
    * one live ADVANCE row per receipt and one live balance row per booking —
      the double-collect race the partner endpoint guards with a status check
      gets a database answer as well.

  Everything is backfilled from the rows already in the tables, because a ledger
  that starts on the day it was created is not a ledger: the collections the
  desk took last month belong in it just as much as today's. The backfill reads
  the same figures the invoice prints (fare, GST when it is a tax invoice, this
  cab's coupon, the advance net of refunds), so a re-run on a booking that has
  since moved produces nothing new rather than a second copy — the unique
  indexes are what make that statement true.

  `master_bookings.payment_status` and `total_paid_amount` are then recomputed
  from the ledger for masters whose only service line is a cab. Masters that
  also carry a hotel or a tour are left to `rollup_hotel_totals_into_master` /
  `rollup_tour_totals_into_master`, which own those columns today; two writers
  computing one column from different sums is how the numbers drifted apart in
  the first place.

Idempotent: ADD COLUMN IF NOT EXISTS, DO-block constraint guards, CREATE INDEX
IF NOT EXISTS, INSERT … WHERE NOT EXISTS, and a counter whose floor is raised
after every write rather than trusted.
"""

from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


_PAYMENT_COLUMNS = (
    ("cab_booking_id", "BIGINT"),
    ("advance_payment_id", "BIGINT"),
    ("received_by", "VARCHAR(20)"),
    ("collected_by_user_id", "UUID"),
    ("collected_by_role", "VARCHAR(30)"),
    ("refunded_amount", "NUMERIC(14,2) NOT NULL DEFAULT 0"),
    ("voided_at", "TIMESTAMPTZ"),
    ("void_reason", "VARCHAR(255)"),
)

# The number this ledger writes: WT-PAY-<YYYYMM>-<5 digits>, the cab invoice
# style with PAY in place of INV. The five digits come off the shared
# `invoice_sequences` counter (migration 0086), which is one counter per
# document kind, so 'PAYMENT' is a new row rather than a new table.
_BASE_SEQ = "(SELECT last_seq FROM invoice_sequences WHERE kind = 'PAYMENT')"

# What one live cab line costs the customer: the closed fare, GST on top only
# when the booking is a tax invoice, this cab's coupon off the bill. Before the
# trip closes there is no closed fare, so the quoted figure stands. This is
# `compute_balance_due`'s basis, written in SQL because a migration cannot call
# Python — the two are asserted against each other in
# tests/integration/test_cab_payment_ledger.py.
_PAYABLE_SQL = """
    CASE
        WHEN COALESCE(cb.final_amount, 0) > 0 THEN
            cb.final_amount
            + (CASE WHEN cb.is_tax_invoice THEN COALESCE(cb.gst_amount, 0)
                    ELSE 0 END)
            - COALESCE(cb.coupon_discount, 0)
        ELSE COALESCE(cb.estimated_amount, 0) - COALESCE(cb.coupon_discount, 0)
    END
"""


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


def _check(table: str, name: str, condition: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = '{name}' AND conrelid = '{table}'::regclass)
            THEN ALTER TABLE {table} ADD CONSTRAINT {name} CHECK {condition};
            END IF;
        END $$
    """
    )


def _raise_payment_counter_floor() -> None:
    """Make the counter at least as high as every number that now exists.

    Run after each backfill statement rather than trusted from the insert: on a
    re-run the counter is already ahead, and GREATEST leaves it alone.
    """
    op.execute(
        f"""
        UPDATE invoice_sequences
           SET last_seq = GREATEST(
                   last_seq,
                   (SELECT COUNT(*) FROM payments),
                   COALESCE((SELECT MAX(CAST(SPLIT_PART(payment_number, '-', 4)
                                             AS BIGINT))
                               FROM payments
                              WHERE payment_number LIKE 'WT-PAY-%'
                                AND SPLIT_PART(payment_number, '-', 4) ~ '^[0-9]+$'),
                            0)
               ),
               updated_at = NOW()
         WHERE kind = 'PAYMENT'
        """
    )


def upgrade() -> None:
    for column, column_type in _PAYMENT_COLUMNS:
        op.execute(
            f"ALTER TABLE payments ADD COLUMN IF NOT EXISTS {column} {column_type}"
        )

    _fk("payments", "fk_payments_cab_booking", "cab_booking_id", "cab_bookings",
        "ON DELETE SET NULL")
    _fk("payments", "fk_payments_advance_receipt", "advance_payment_id",
        "advance_payments", "ON DELETE SET NULL")

    # The vocabulary comes from DB Schema Part 7 §4 and §5 verbatim. A free-text
    # column is not an enum: the reports group on these values, so a typo has to
    # be refused at write time.
    _check(
        "payments",
        "ck_payments_type",
        """(payment_type IS NULL OR payment_type IN
             ('ADVANCE','FULL_PAYMENT','BALANCE_PAYMENT','WALLET_RECHARGE'))""",
    )
    _check(
        "payments",
        "ck_payments_status",
        """(payment_status IN
             ('PENDING','PROCESSING','SUCCESS','FAILED','REFUNDED','PARTIAL_REFUNDED'))""",
    )
    _check("payments", "ck_payments_amount_positive", "(amount > 0)")
    _check(
        "payments",
        "ck_payments_refunded_range",
        "((refunded_amount >= 0) AND (refunded_amount <= amount))",
    )
    # A status that contradicts the money is the failure this table exists to
    # prevent: REFUNDED must have moved the whole amount, PARTIAL_REFUNDED a part
    # of it, and nothing else may claim to have refunded anything.
    _check(
        "payments",
        "ck_payments_refund_status_consistent",
        """(
             (payment_status = 'REFUNDED'          AND refunded_amount =  amount)
          OR (payment_status = 'PARTIAL_REFUNDED' AND refunded_amount >  0
                                                AND refunded_amount <  amount)
          OR (payment_status NOT IN ('REFUNDED','PARTIAL_REFUNDED')
              AND refunded_amount = 0)
         )""",
    )
    _check(
        "payments",
        "ck_payments_void_reason",
        """(voided_at IS NULL
           OR (void_reason IS NOT NULL AND length(trim(void_reason)) > 0))""",
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_payments_cab_booking
            ON payments (cab_booking_id)
            WHERE cab_booking_id IS NOT NULL
        """
    )

    # One live ledger row per advance receipt. Re-recording an advance after a
    # void is legitimate (the desk voids a wrong slip and takes the right one),
    # so the index is partial on voided_at rather than on the receipt alone.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_live_advance
            ON payments (advance_payment_id)
            WHERE payment_type = 'ADVANCE'
              AND advance_payment_id IS NOT NULL
              AND voided_at IS NULL
        """
    )
    # One live balance collection per cab booking — the database answer to "was
    # this trip paid for twice", which a status check in the endpoint can only
    # mostly give.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_live_balance
            ON payments (cab_booking_id)
            WHERE payment_type IN ('BALANCE_PAYMENT','FULL_PAYMENT')
              AND cab_booking_id IS NOT NULL
              AND voided_at IS NULL
        """
    )

    # The counter kind, seeded from the highest payment number that exists — zero
    # rows today, and the same statement is what makes a re-run safe.
    op.execute(
        """
        INSERT INTO invoice_sequences (kind, last_seq)
        SELECT 'PAYMENT', 0
        WHERE NOT EXISTS (SELECT 1 FROM invoice_sequences WHERE kind = 'PAYMENT')
        """
    )
    _raise_payment_counter_floor()

    # ── backfill: the money the platform already took ───────────────────────
    #
    # Advances. Every receipt, ACTIVE or VOIDED, becomes the ledger row it should
    # have had. Status follows the money rather than the slip: an advance the
    # customer got back in full is REFUNDED, one got back in part is
    # PARTIAL_REFUNDED, and a voided-but-never-refunded receipt keeps SUCCESS (the
    # rupees did come in) while the derive excludes it through voided_at.
    op.execute(
        f"""
        INSERT INTO payments
            (uuid, payment_number, master_booking_id, customer_id,
             payment_type, payment_method, payment_status, amount,
             payment_datetime, cab_booking_id, advance_payment_id, received_by,
             collected_by_user_id, collected_by_role, refunded_amount,
             voided_at, void_reason, created_at)
        SELECT
             gen_random_uuid(),
             'WT-PAY-' || to_char(ap.collected_at, 'YYYYMM') || '-' ||
             lpad(({_BASE_SEQ} + rn)::text, 5, '0'),
             ap.master_booking_id,
             mb.customer_id,
             'ADVANCE',
             ap.payment_mode,
             CASE
                 WHEN ap.refunded_amount >= ap.amount AND ap.refunded_amount > 0
                     THEN 'REFUNDED'
                 WHEN ap.refunded_amount > 0 THEN 'PARTIAL_REFUNDED'
                 ELSE 'SUCCESS'
             END,
             ap.amount,
             ap.collected_at,
             ap.cab_booking_id,
             ap.id,
             ap.received_by,
             ap.collected_by_user_id,
             ap.collected_by_role,
             COALESCE(ap.refunded_amount, 0),
             ap.voided_at,
             ap.void_reason,
             ap.created_at
        FROM (
            SELECT ap.*,
                   row_number() OVER (ORDER BY ap.collected_at, ap.id) AS rn
              FROM advance_payments ap
             WHERE NOT EXISTS (
                 SELECT 1 FROM payments p
                  WHERE p.payment_type = 'ADVANCE'
                    AND p.advance_payment_id = ap.id
             )
        ) ap
        JOIN master_bookings mb ON mb.id = ap.master_booking_id
        """
    )
    _raise_payment_counter_floor()

    # The balance the desk or the driver collected on a closed cab trip, computed
    # on the same basis the invoice prints. A trip with no advance in front of it
    # is a FULL_PAYMENT, one with an advance is a BALANCE_PAYMENT — the
    # distinction the spec's §4 names and the reports group on.
    op.execute(
        f"""
        INSERT INTO payments
            (uuid, payment_number, master_booking_id, customer_id,
             payment_type, payment_method, payment_status, amount,
             payment_datetime, cab_booking_id, received_by, created_at)
        SELECT
             gen_random_uuid(),
             'WT-PAY-' || to_char(COALESCE(cb.trip_ended_at, cb.updated_at),
                                  'YYYYMM') || '-' ||
             lpad(({_BASE_SEQ} + rn)::text, 5, '0'),
             cb.master_booking_id,
             mb.customer_id,
             CASE WHEN COALESCE(adv.advance_net, 0) = 0
                  THEN 'FULL_PAYMENT' ELSE 'BALANCE_PAYMENT' END,
             cb.payment_mode,
             'SUCCESS',
             round({_PAYABLE_SQL}, 2),
             COALESCE(cb.trip_ended_at, cb.updated_at),
             cb.id,
             cb.payment_collected_by,
             COALESCE(cb.trip_ended_at, cb.updated_at)
        FROM (
            SELECT cb.*,
                   row_number() OVER (ORDER BY cb.id) AS rn
              FROM cab_bookings cb
             WHERE cb.payment_mode IS NOT NULL
               AND COALESCE(cb.final_amount, 0) > 0
               AND NOT EXISTS (
                   SELECT 1 FROM payments p
                    WHERE p.payment_type IN ('BALANCE_PAYMENT','FULL_PAYMENT')
                      AND p.cab_booking_id = cb.id
               )
        ) cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(ap.amount - COALESCE(ap.refunded_amount, 0)), 0)
                   AS advance_net
              FROM advance_payments ap
             WHERE ap.cab_booking_id = cb.id
               AND ap.status = 'ACTIVE'
        ) adv ON TRUE
        WHERE round({_PAYABLE_SQL}, 2) > 0
        """
    )
    _raise_payment_counter_floor()

    # Masters whose only service line is a cab now take their payment state from
    # the ledger: what is held against what the live cab lines cost. The booking
    # value (`total_amount`) is left to the writers that set it today — the
    # close, the amendment, the hotel and tour rollups — because a payment status
    # is a question about money received, not about what the trip was worth.
    # Mixed masters are left to the hotel and tour rollups entirely.
    op.execute(
        f"""
        WITH cab_only AS (
            SELECT mb.id AS mb_id
            FROM master_bookings mb
            WHERE EXISTS (SELECT 1 FROM booking_services bs
                          WHERE bs.master_booking_id = mb.id
                            AND bs.service_type = 'CAB')
              AND NOT EXISTS (SELECT 1 FROM booking_services bs
                              WHERE bs.master_booking_id = mb.id
                                AND bs.service_type IN ('HOTEL','TOUR'))
        ),
        owed AS (
            SELECT cb.master_booking_id AS mb_id,
                   round(SUM({_PAYABLE_SQL}), 2) AS payable
            FROM cab_bookings cb
            WHERE cb.master_booking_id IN (SELECT mb_id FROM cab_only)
              AND cb.booking_status <> 'CANCELLED'
            GROUP BY cb.master_booking_id
        ),
        held AS (
            SELECT p.master_booking_id AS mb_id,
                   SUM(p.amount - p.refunded_amount) AS paid
            FROM payments p
            WHERE p.master_booking_id IN (SELECT mb_id FROM cab_only)
              AND p.payment_status IN ('SUCCESS','PARTIAL_REFUNDED','REFUNDED')
              AND p.voided_at IS NULL
            GROUP BY p.master_booking_id
        )
        UPDATE master_bookings mb
           SET total_paid_amount = COALESCE(h.paid, 0),
               payment_status = CASE
                   WHEN COALESCE(h.paid, 0) <= 0 THEN 'PENDING'
                   WHEN COALESCE(h.paid, 0) < COALESCE(o.payable, 0) THEN 'PARTIAL'
                   ELSE 'PAID'
               END,
               updated_at = NOW()
          FROM cab_only c
          LEFT JOIN owed o ON o.mb_id = c.mb_id
          LEFT JOIN held h ON h.mb_id = c.mb_id
         WHERE mb.id = c.mb_id
        """
    )


def downgrade() -> None:
    # The ledger rows are money the platform took; removing the links is a schema
    # decision, not a step to re-run, so down drops only what this revision added
    # and leaves every recorded payment in place.
    op.execute("DROP INDEX IF EXISTS ux_payments_live_balance")
    op.execute("DROP INDEX IF EXISTS ux_payments_live_advance")
    op.execute("DROP INDEX IF EXISTS idx_payments_cab_booking")
    for name in (
        "ck_payments_void_reason",
        "ck_payments_refund_status_consistent",
        "ck_payments_refunded_range",
        "ck_payments_amount_positive",
        "ck_payments_status",
        "ck_payments_type",
        "fk_payments_advance_receipt",
        "fk_payments_cab_booking",
    ):
        op.execute(
            f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                                                      AND conrelid = 'payments'::regclass)
                THEN ALTER TABLE payments DROP CONSTRAINT {name};
                END IF;
            END $$
            """
        )
    for column, _type in _PAYMENT_COLUMNS:
        op.execute(f"ALTER TABLE payments DROP COLUMN IF EXISTS {column}")
