"""0083_guest_booking_feedback — let a logged-out traveller review a booking

Revision ID: 0083
Revises: 0082

Doc Ref: migration 0079 (CHECK recipe); booking_reviews (booking models:500).

Why:
  The public feedback page accepts a booking reference + registered mobile
  from someone with no account session. `booking_reviews` already keys a
  review to (master_booking_id, customer_id) with rating 1-5, so it is the
  right table — a fourth capture table would have had to re-join back to it
  anyway. It gains the fields the guest flow needs and a moderation status.

  `customer_id` stays NOT NULL and is filled from the resolved booking's own
  customer: every booking belongs to an account, and the traveller who typed
  the feedback is recorded separately in `guest_*`.

  Status default — the subtle part. Every row that exists at migration time
  was written by the authenticated flow, which had no moderation concept, so
  `status` is added with DEFAULT 'APPROVED' and then the default is what new
  guest rows will get. Concretely: add with DEFAULT 'PENDING', then flip the
  pre-existing (all source='CUSTOMER') rows to 'APPROVED'. Nothing a guest
  submits later is accidentally treated as already reviewed.

  The duplicate rule ("one feedback per booking, a rejection frees the slot")
  is the partial unique index at the bottom. It is scoped to
  source = 'GUEST_WEB' on purpose: a legacy booking that happens to hold two
  customer reviews must not make this migration fail, and the owner's rule is
  about the guest form.

  Approved guest feedback is admin-only for now — nothing reads this table
  into the public homepage TESTIMONIALS band, which is CMS JSONB content.

Idempotent: ADD COLUMN IF NOT EXISTS, the DO-block constraint guard, CREATE
INDEX IF NOT EXISTS, and the backfill UPDATE is guarded so a re-run matches
nothing.
"""

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def _check(table: str, name: str, expression: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = '{name}' AND conrelid = '{table}'::regclass)
            THEN ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression});
            END IF;
        END $$
    """
    )


def upgrade() -> None:
    for ddl in (
        "guest_name VARCHAR(150)",
        "guest_mobile VARCHAR(20)",
        "guest_email VARCHAR(255)",
        # Exactly what the visitor typed at step 1, kept for the admin to
        # compare against the resolved booking.
        "booking_ref_submitted VARCHAR(100)",
        "rejection_reason TEXT",
        "moderated_by UUID REFERENCES users(id) ON DELETE SET NULL",
        "moderated_at TIMESTAMPTZ",
    ):
        op.execute(f"ALTER TABLE booking_reviews ADD COLUMN IF NOT EXISTS {ddl}")

    op.execute(
        """
        ALTER TABLE booking_reviews
            ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'CUSTOMER'
        """
    )
    op.execute(
        """
        ALTER TABLE booking_reviews
            ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
        """
    )
    # Rows predating the guest flow were never queued for moderation, so they
    # are approved as far as the platform is concerned.
    op.execute(
        """
        UPDATE booking_reviews
           SET status = 'APPROVED'
         WHERE source = 'CUSTOMER' AND status = 'PENDING'
        """
    )
    _check(
        "booking_reviews",
        "ck_booking_reviews_status",
        "status IN ('PENDING','APPROVED','REJECTED')",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_booking_reviews_moderation"
        " ON booking_reviews (status, created_at DESC)"
    )
    # One live guest feedback per booking. REJECTED is deliberately outside the
    # index so a traveller can try again after the admin rejects the first one.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_booking_reviews_live_per_booking
            ON booking_reviews (master_booking_id)
            WHERE source = 'GUEST_WEB' AND status IN ('PENDING','APPROVED')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uniq_booking_reviews_live_per_booking")
    op.execute("DROP INDEX IF EXISTS idx_booking_reviews_moderation")
    op.execute(
        "ALTER TABLE booking_reviews DROP CONSTRAINT IF EXISTS ck_booking_reviews_status"
    )
    for name in (
        "status",
        "source",
        "moderated_at",
        "moderated_by",
        "rejection_reason",
        "booking_ref_submitted",
        "guest_email",
        "guest_mobile",
        "guest_name",
    ):
        op.execute(f"ALTER TABLE booking_reviews DROP COLUMN IF EXISTS {name}")
    # Guest rows survive as ordinary reviews; who wrote them and whether they
    # were moderated is lost with the columns.
