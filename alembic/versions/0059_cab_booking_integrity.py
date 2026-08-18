"""0059_cab_booking_integrity — money CHECKs, missing indexes & dangling FKs.

Fixes three classes of schema gaps surfaced by the cab-booking audit:

1. Money columns on the booking/settlement path had no guard against
   negative amounts (the usual signature of double-charge / refund math
   bugs). A CHECK (col >= 0) is added to every money column that can
   legitimately be zero but must never go negative. (Wallet balances are
   deliberately excluded — a partner wallet may go negative when they owe
   the platform after an advance-recovery.)

2. FK columns used in joins/lookups were unindexed. Hot-path indexes are
   added for cab_bookings, cab_booking_assignments, cancellation requests,
   advance payments, coupon usages and pending disbursements.

3. user-reference columns on the advance-payment tables had no FK
   constraint at all ("dangling FKs"). They are now constrained to
   users.id. All 6 columns were verified to have 0 orphan rows before
   adding. settlement_items.booking_id is intentionally left un-FK'd — it
   is polymorphic (booking_type distinguishes CAB/HOTEL/TOUR), so a single
   FK would be wrong.

Also flips cab_bookings.booking_status to NOT NULL (backfilled; the table
currently has no NULL rows) so status guards in service code can rely on a
non-null value.

Idempotent: index creation uses IF NOT EXISTS; CHECK and FK additions are
guarded by pg_constraint probes so re-running the migration is safe.

Doc Ref: BRD Part 6 §147-155 (cab lifecycle), Admin API §11 (bookings).
"""

from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def _add_check(conn, table: str, column: str) -> None:
    """Add CHECK (column >= 0) if it is not already present."""
    name = f"{table}_{column}_nonneg_check"
    conn.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT {name}
                    CHECK ({column} >= 0);
                END IF;
            END $$;
            """
        )
    )


def _add_fk(conn, table: str, column: str, ref: str) -> None:
    """Add FK column -> users.id if it is not already present."""
    name = f"fk_{table}_{column}"
    conn.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT {name}
                    FOREIGN KEY ({column}) REFERENCES {ref};
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. cab_bookings.booking_status NOT NULL (backfill then enforce) ──
    conn.execute(
        sa.text(
            "UPDATE cab_bookings SET booking_status = 'PENDING_ASSIGNMENT' "
            "WHERE booking_status IS NULL"
        )
    )
    conn.execute(
        sa.text("ALTER TABLE cab_bookings ALTER COLUMN booking_status SET NOT NULL")
    )

    # ── 2. Money CHECK constraints ───────────────────────────────────────────
    for col in (
        "estimated_amount",
        "final_amount",
        "platform_commission",
        "partner_payout",
        "gst_amount",
        "cash_amount_due",
        "coupon_discount",
    ):
        _add_check(conn, "cab_bookings", col)

    for col in ("total_amount", "total_paid_amount", "total_refund_amount"):
        _add_check(conn, "master_bookings", col)

    _add_check(
        conn, "advance_payments", "refunded_amount"
    )  # amount: pre-existing CHECK

    for col in (
        "gross_amount",
        "commission_amount",
        "gst_amount",
        "net_payable_amount",
    ):
        _add_check(conn, "settlements", col)

    # ── 3. Missing hot-path indexes ──────────────────────────────────────────
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_booking_vehicle_category "
            "ON cab_bookings(vehicle_category_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_booking_pending_partner "
            "ON cab_bookings(pending_partner_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_booking_cancelled_by "
            "ON cab_bookings(cancelled_by_user_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cab_assignment_vehicle "
            "ON cab_booking_assignments(vehicle_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cancel_req_master "
            "ON booking_cancellation_requests(master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_cancel_req_reviewed_by "
            "ON booking_cancellation_requests(reviewed_by_user_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_advance_payments_master "
            "ON advance_payments(master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_advance_payments_driver "
            "ON advance_payments(driver_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_advance_payments_collected_by "
            "ON advance_payments(collected_by_user_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_advance_payments_voided_by "
            "ON advance_payments(voided_by_user_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_coupon_usages_master "
            "ON coupon_usages(master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_pending_coupon_master "
            "ON pending_coupon_disbursements(master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_pending_coupon_customer "
            "ON pending_coupon_disbursements(customer_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_hotel_advance_master "
            "ON hotel_advance_payments(master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_tour_advance_master "
            "ON tour_advance_payments(master_booking_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_settlement_item_booking "
            "ON settlement_items(booking_id)"
        )
    )

    # ── 4. Dangling FKs on advance-payment user references ───────────────────
    for tbl in (
        "advance_payments",
        "hotel_advance_payments",
        "tour_advance_payments",
    ):
        _add_fk(conn, tbl, "collected_by_user_id", "users(id)")
        _add_fk(conn, tbl, "voided_by_user_id", "users(id)")


def downgrade() -> None:
    conn = op.get_bind()

    for tbl in (
        "advance_payments",
        "hotel_advance_payments",
        "tour_advance_payments",
    ):
        conn.execute(
            sa.text(
                f"ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS "
                f"fk_{tbl}_collected_by_user_id"
            )
        )
        conn.execute(
            sa.text(
                f"ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS "
                f"fk_{tbl}_voided_by_user_id"
            )
        )

    for index in (
        "idx_cab_booking_vehicle_category",
        "idx_cab_booking_pending_partner",
        "idx_cab_booking_cancelled_by",
        "idx_cab_assignment_vehicle",
        "idx_cancel_req_master",
        "idx_cancel_req_reviewed_by",
        "idx_advance_payments_master",
        "idx_advance_payments_driver",
        "idx_advance_payments_collected_by",
        "idx_advance_payments_voided_by",
        "idx_coupon_usages_master",
        "idx_pending_coupon_master",
        "idx_pending_coupon_customer",
        "idx_hotel_advance_master",
        "idx_tour_advance_master",
        "idx_settlement_item_booking",
    ):
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {index}"))

    conn.execute(
        sa.text("ALTER TABLE cab_bookings ALTER COLUMN booking_status DROP NOT NULL")
    )

    for table, cols in (
        (
            "cab_bookings",
            (
                "estimated_amount",
                "final_amount",
                "platform_commission",
                "partner_payout",
                "gst_amount",
                "cash_amount_due",
                "coupon_discount",
            ),
        ),
        (
            "master_bookings",
            ("total_amount", "total_paid_amount", "total_refund_amount"),
        ),
        ("advance_payments", ("refunded_amount",)),
        (
            "settlements",
            ("gross_amount", "commission_amount", "gst_amount", "net_payable_amount"),
        ),
    ):
        for col in cols:
            conn.execute(
                sa.text(
                    f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
                    f"{table}_{col}_nonneg_check"
                )
            )
