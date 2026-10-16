"""0082_capture_surfaces — tables for the three public capture forms

Revision ID: 0082
Revises: 0081

Doc Ref: migration 0079 (table + CHECK recipe); Website Lead Capture §2.

Why:
  The homepage gains three public, no-login capture surfaces — an enquiry
  modal, a callback card, and (in 0083) guest feedback — and the owner needs
  each one to land in an admin queue a human can work. None of the existing
  tables fit:

    * `contact_messages` is the /contact inbox. Its `subject` and `message`
      are NOT NULL and it has no status column, so there is nowhere to record
      "called them", "quoted them", "booked them". Reusing it would make
      website enquiries and support tickets indistinguishable in one list.
    * There is no callback-queue table at all.
    * There is no quotation entity anywhere in the schema; the owner asked for
      "make quotation for this customer", so one is introduced here as a plain
      DB row — no PDF, no email (EMAIL_ENABLED is off, so an email-only design
      would be dead code).

  `handled_by` / `created_by` are the admin who worked the row, so the queue
  shows ownership rather than an anonymous status flip.

  Money is NUMERIC(12,2) — Decimal end-to-end, per the platform rule; no
  float touches an amount column.

  The duplicate guards live in the application (see public_capture_api) rather
  than as unique indexes here, matching how partner_applications already
  refuses a second live row for the same mobile: a NEW-status check is a
  queue rule the admin can change, not a data-integrity invariant.

Everything is idempotent: CREATE TABLE IF NOT EXISTS, the DO-block constraint
guard, CREATE INDEX IF NOT EXISTS.
"""

from alembic import op

revision = "0082"
down_revision = "0081"
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
    # ── enquiries ───────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS enquiries (
            id BIGSERIAL PRIMARY KEY,
            first_name VARCHAR(100) NOT NULL,
            last_name VARCHAR(100),
            email VARCHAR(255) NOT NULL,
            mobile VARCHAR(10) NOT NULL,
            service_type VARCHAR(10) NOT NULL DEFAULT 'OTHER',
            message TEXT NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'NEW',
            handle_note TEXT,
            handled_by UUID REFERENCES users(id) ON DELETE SET NULL,
            handled_at TIMESTAMPTZ,
            source VARCHAR(30) NOT NULL DEFAULT 'WEB_RAIL',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    _check(
        "enquiries",
        "ck_enquiries_service_type",
        "service_type IN ('CAB','HOTEL','TOUR','OTHER')",
    )
    _check(
        "enquiries",
        "ck_enquiries_status",
        "status IN ('NEW','CONTACTED','QUOTED','BOOKED','CLOSED')",
    )
    _check("enquiries", "ck_enquiries_mobile_digits", "mobile ~ '^[6-9][0-9]{9}$'")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_enquiries_queue
            ON enquiries (status, created_at DESC)
    """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_enquiries_mobile ON enquiries (mobile)")

    # ── callback_requests ───────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS callback_requests (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(150),
            mobile VARCHAR(10) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'NEW',
            handle_note TEXT,
            handled_by UUID REFERENCES users(id) ON DELETE SET NULL,
            called_at TIMESTAMPTZ,
            source VARCHAR(30) NOT NULL DEFAULT 'WEB_CARD',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    _check(
        "callback_requests",
        "ck_callback_requests_status",
        "status IN ('NEW','CALLED','NOT_PICKED','DONE')",
    )
    _check(
        "callback_requests",
        "ck_callback_requests_mobile_digits",
        "mobile ~ '^[6-9][0-9]{9}$'",
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_callback_requests_queue
            ON callback_requests (status, created_at DESC)
    """
    )
    # The duplicate check ("this mobile already has an open request") and the
    # Customer Care lookup both probe by mobile.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_callback_requests_mobile"
        " ON callback_requests (mobile)"
    )

    # ── quotations ──────────────────────────────────────────────────────
    # A quotation is offered before a booking exists, so all three links are
    # optional: enquiry (what they asked for), customer (once registered),
    # master_booking (once booked). ON DELETE SET NULL keeps the quote legible
    # after the row it pointed at is removed.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quotations (
            id BIGSERIAL PRIMARY KEY,
            enquiry_id BIGINT REFERENCES enquiries(id) ON DELETE SET NULL,
            customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
            master_booking_id BIGINT REFERENCES master_bookings(id) ON DELETE SET NULL,
            service_type VARCHAR(10) NOT NULL DEFAULT 'OTHER',
            summary TEXT NOT NULL,
            amount NUMERIC(12,2) NOT NULL,
            currency CHAR(3) NOT NULL DEFAULT 'INR',
            valid_till DATE,
            terms TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    _check(
        "quotations",
        "ck_quotations_service_type",
        "service_type IN ('CAB','HOTEL','TOUR','OTHER')",
    )
    _check(
        "quotations",
        "ck_quotations_status",
        "status IN ('DRAFT','SENT','ACCEPTED','DECLINED','EXPIRED')",
    )
    # Upper bound guards against a fat-fingered amount; 10,000,000 is well
    # past any single WayTero quotation today.
    _check("quotations", "ck_quotations_amount", "amount > 0 AND amount <= 10000000")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotations_enquiry"
        " ON quotations (enquiry_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotations_status ON quotations (status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quotations")
    op.execute("DROP TABLE IF EXISTS callback_requests")
    op.execute("DROP TABLE IF EXISTS enquiries")
