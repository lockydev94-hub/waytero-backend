"""0085_branches_and_user_branch — branch as a first-class thing, staff attached to one

Revision ID: 0085
Revises: 0084

Doc Ref: DB Architecture Part 3 §5 (users); migration 0003 (cities seed),
         0053 (website leads), 0082 (capture surfaces), 0084 (MPIN/work hours).

Why:
  The owner runs WayTero branch by branch — Bhubaneswar is the head office,
  Cuttack is a sub-branch — and the Cuttack manager should see Cuttack's
  bookings, chats, leads and care logs, nothing else. Today there is no branch
  concept anywhere in the schema: `staff_profiles.city` is free text
  (0014:89) and `users` carries no location, so every staff account reads the
  whole platform.

  A branch is *one city* (owner decision), and `city_id` is already the location
  key on master_bookings / hotels / tour_packages / vehicles / partners. So the
  branch is reached through the city that is already there instead of adding a
  `branch_id` to every transactional table — one meaning for "which city is this
  row in", and no second key to keep in sync. `branches.city_id` is UNIQUE to
  enforce that 1:1 mapping at the DB level.

  `users.branch_id` is nullable with NULL reserved for SUPER_ADMIN, which is
  what makes "super admin sees everything" expressible without a magic id.
  The cross-column CHECK encodes the invariant the app depends on — a staff
  account always belongs to exactly one branch, a super admin never does.
  Postgres allows it because both columns are on the same row; no FK is needed
  between them. It is added AFTER the backfill below, so existing staff rows
  pass from the first run.

  The queue tables a branch works every day (enquiries, callback requests,
  quotations, contact messages, partner applications, chat conversations,
  customer care logs) have no city at all, so they cannot be scoped. One
  nullable `city_id` each: nullable because a website enquiry genuinely has no
  city until someone reads it, and the plan is that unassigned rows belong to
  the head office, not to every branch.

  `customer_care_logs` has no migration today — it is created lazily at runtime
  by `_ensure_care_log_table()` (admin/customer_care_api.py). That means the
  table's shape lives in an API module and a fresh database only gets it after
  someone opens the Customer Care screen. This migration takes ownership
  (CREATE TABLE IF NOT EXISTS with the column), and the runtime DDL gains the
  same column so existing dev databases cannot drift from the migration.

  "Exactly one super admin" and "exactly one head office" are deliberately NOT
  unique indexes. No super admin is seeded (0014:17), so a database may already
  hold two or three test accounts, and a partial unique index would abort
  `alembic upgrade head` for everyone instead of letting the owner clean up.
  Both rules are enforced in the service layer, and
  `GET /admin/branch-scope/audit` reports the counts so drift is visible.

Everything is idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS,
ADD COLUMN IF NOT EXISTS, the DO-block constraint guard, ON CONFLICT DO NOTHING
on the seeds, and NULL-guarded UPDATEs for the backfills.
"""

from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

# The four staff types that must belong to a branch. CUSTOMER / PARTNER /
# DRIVER are deliberately outside the branch concept — they are scoped by their
# own account (partner isolation), not by an office.
STAFF_TYPES = "'ADMIN','CCO','VERIFICATION_OFFICER','FINANCE_MANAGER'"


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


def queue_indexes() -> tuple[tuple[str, str, str], ...]:
    """(table, index name, index tail columns) for the branch-scoped queues.

    Shared by upgrade and downgrade so the names cannot drift apart. The tail
    is the column each queue actually filters on — contact_messages has no
    status (0053:73-83), it is a read/unread inbox with no updated_at either.
    """
    return (
        ("enquiries", "idx_enquiries_city_queue", "status, created_at DESC"),
        ("callback_requests", "idx_callback_requests_city_queue", "status, created_at DESC"),
        ("quotations", "idx_quotations_city_queue", "status, created_at DESC"),
        ("contact_messages", "idx_contact_messages_city_queue", "is_read, created_at DESC"),
        ("partner_applications", "idx_partner_applications_city_queue", "status, created_at DESC"),
        # Chat ids are UUIDs and the admin list orders by activity, not arrival.
        ("chat_conversations", "idx_chat_conversations_city_queue", "status, updated_at DESC"),
        ("customer_care_logs", "idx_ccl_city_queue", "status, created_at DESC"),
    )


def upgrade() -> None:
    # ── branches ────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS branches (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            code VARCHAR(50) NOT NULL UNIQUE,
            city_id BIGINT NOT NULL REFERENCES cities(id),
            address VARCHAR(255),
            is_headquarters BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    # One branch per city (owner decision). This index is also the ON CONFLICT
    # target for the city-keyed bootstrap below, so it must exist first.
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_branches_city ON branches (city_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_branches_active ON branches (is_active)")

    # ── users.branch_id ─────────────────────────────────────────────────
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS branch_id BIGINT")
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = 'fk_users_branch' AND conrelid = 'users'::regclass)
            THEN ALTER TABLE users ADD CONSTRAINT fk_users_branch
                 FOREIGN KEY (branch_id) REFERENCES branches(id) ON DELETE RESTRICT;
            END IF;
        END $$
    """
    )
    # RESTRICT, not CASCADE: deleting a branch that still has staff must be
    # refused by the API (B7), never silently orphan a login.
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_branch ON users (branch_id)")

    # ── city_id on the queues a branch works daily ──────────────────────
    # customer_care_logs has no migration today: _ensure_care_log_table()
    # (admin/customer_care_api.py) makes it the first time someone opens the
    # Customer Care screen, so a fresh database has no such table yet and the
    # ALTERs below would fail. Created here first — same columns as the runtime
    # DDL, minus the inline city FK, which the loop adds under one name whether
    # this statement or the runtime one made the table.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_care_logs (
            id BIGSERIAL PRIMARY KEY,
            log_number VARCHAR(60) UNIQUE NOT NULL,
            customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
            city_id BIGINT,
            issue_type VARCHAR(60) NOT NULL DEFAULT 'INQUIRY',
            subject VARCHAR(255) NOT NULL,
            description TEXT NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'OPEN',
            priority VARCHAR(30) NOT NULL DEFAULT 'MEDIUM',
            booking_id BIGINT REFERENCES master_bookings(id) ON DELETE SET NULL,
            resolution_notes TEXT,
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            resolved_by UUID REFERENCES users(id) ON DELETE SET NULL,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )

    # Each index leads with city_id: "this branch's open rows, newest first" is
    # how every one of these queues is read.
    for table, _, _ in queue_indexes():
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS city_id BIGINT")
        op.execute(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = 'fk_{table}_city'
                                 AND conrelid = '{table}'::regclass)
                THEN ALTER TABLE {table} ADD CONSTRAINT fk_{table}_city
                     FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE SET NULL;
                END IF;
            END $$
        """
        )
    for table, index_name, tail in queue_indexes():
        op.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {index_name}
                ON {table} (city_id, {tail})
        """
        )

    # ── bootstrap the two branches the owner described ──────────────────
    # Name and code come from the seeded city row (0003), and the head-office
    # address from the OFFICE_ADDRESS brand config (0072) — nothing invented.
    # ON CONFLICT DO NOTHING: re-running, or the owner having registered the
    # city from /branches first, both leave the existing row untouched.
    op.execute(
        """
        INSERT INTO branches (name, code, city_id, address, is_headquarters, is_active)
        SELECT c.name, c.city_code, c.id,
               (SELECT config_value FROM system_configurations
                 WHERE config_key = 'OFFICE_ADDRESS'),
               TRUE, TRUE
        FROM cities c
        WHERE c.city_code = 'BBS'
        ON CONFLICT (city_id) DO NOTHING
    """
    )
    op.execute(
        """
        INSERT INTO branches (name, code, city_id, address, is_headquarters, is_active)
        SELECT c.name, c.city_code, c.id, NULL, FALSE, TRUE
        FROM cities c
        WHERE c.city_code = 'CTK'
        ON CONFLICT (city_id) DO NOTHING
    """
    )

    # ── backfill: existing staff belong to the head office ──────────────
    # Every staff account in the database today works out of Bhubaneswar, and
    # the owner re-assigns anyone who does not from /users. Runs before the
    # CHECK so the constraint is added against rows that already satisfy it.
    op.execute(
        f"""
        UPDATE users
           SET branch_id = (SELECT id FROM branches WHERE is_headquarters ORDER BY id LIMIT 1)
         WHERE branch_id IS NULL
           AND user_type IN ({STAFF_TYPES})
    """
    )
    _check(
        "users",
        "ck_users_branch_membership",
        f"""(user_type = 'SUPER_ADMIN' AND branch_id IS NULL)
            OR (user_type IN ({STAFF_TYPES}) AND branch_id IS NOT NULL)
            OR (user_type IN ('CUSTOMER','PARTNER','DRIVER'))""",
    )

    # ── backfill: derive partner_applications.city_id from its free-text city
    # The application form collects a typed city name, so match it against
    # master data. No match stays NULL and reads as "head office" — a real
    # value, not a guess at a city the applicant mis-typed.
    op.execute(
        """
        UPDATE partner_applications AS pa
           SET city_id = c.id
          FROM cities c
         WHERE pa.city_id IS NULL
           AND pa.city IS NOT NULL
           AND btrim(lower(pa.city)) = btrim(lower(c.name))
    """
    )


def downgrade() -> None:
    # Dropping the column also drops its FK constraint and any index that
    # includes it, so the named index/FK work here is belt-and-braces for a
    # partially-applied upgrade.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_branch_membership")
    for table, index_name, _ in queue_indexes():
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_city")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS city_id")
    op.execute("DROP INDEX IF EXISTS idx_users_branch")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_branch")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS branch_id")
    op.execute("DROP INDEX IF EXISTS uq_branches_city")
    op.execute("DROP TABLE IF EXISTS branches")
