"""Cab booking coupon columns — persist coupon_code and discount on the booking

Revision ID: 0048
Revises: 0047

Background:
    Customer-web's /public/cab/create-booking was previewing the coupon
    discount in the BookingReviewModal but never telling the server about it.
    Partner close-trip already reads `coupon_usages` and the cab-side code
    pulls `coupon_discount` from the join — but `cab_bookings` itself never
    had a coupon_code/coupon_id to look up. So a customer who applied
    `WELCOME10` at quote time would see the right "after coupon" total in
    the modal, but the booking row would silently fall back to the full fare
    at close-trip.

    This migration adds the columns so the customer-web path can persist the
    coupon at booking creation time AND the admin/partner lifecycles can
    independently validate / audit the coupon the customer entered.

    Existing rows are back-filled with NULLs — we don't attempt to infer
    coupons from history. Old bookings will simply show no coupon line on
    the invoice.

    Doc Ref: BRD Part 3 §35 (pricing), audit trail for §45 (discount/coupon).
"""

from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. The discount actually applied to this booking at quote time ──────
    # Stored separately from coupon_usages so the cab_bookings row is
    # self-describing — useful when the customer view needs to render the
    # discount without joining coupon_usages (which is a write-only audit).
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS coupon_discount NUMERIC(12,2) DEFAULT 0
    """))

    # ── 2. The coupon code the customer entered (uppercase, trim-preserved) ──
    # Pure denormalisation for audit + display. FK would be nicer but the
    # coupon might be deleted (deactivated) after the booking, so we keep the
    # string snapshot.
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS coupon_code VARCHAR(50)
    """))

    # ── 3. The coupon id at the moment of booking (nullable — no coupon) ────
    # We keep the FK as a soft reference (ON DELETE SET NULL) so a coupon
    # can be hard-deleted without orphaning the booking row, while the
    # coupon_code snapshot above still shows in the customer view.
    conn.execute(sa.text("""
        ALTER TABLE cab_bookings
            ADD COLUMN IF NOT EXISTS coupon_id BIGINT
                REFERENCES coupons(id) ON DELETE SET NULL
    """))

    # Index for the rare "find bookings that used this coupon" admin query.
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cab_coupon_id
            ON cab_bookings(coupon_id)
    """))

    # Back-fill: existing rows had no coupon persisted (this migration is
    # the first time the column exists). 0 = "no coupon applied" — matches
    # the partner-side formula which computes `final_amount = fare − coupon`.
    conn.execute(sa.text("""
        UPDATE cab_bookings
        SET coupon_discount = COALESCE(coupon_discount, 0)
        WHERE coupon_discount IS NULL
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cab_coupon_id"))
    conn.execute(sa.text("ALTER TABLE cab_bookings DROP COLUMN IF EXISTS coupon_id"))
    conn.execute(sa.text("ALTER TABLE cab_bookings DROP COLUMN IF EXISTS coupon_code"))
    conn.execute(sa.text("ALTER TABLE cab_bookings DROP COLUMN IF EXISTS coupon_discount"))
