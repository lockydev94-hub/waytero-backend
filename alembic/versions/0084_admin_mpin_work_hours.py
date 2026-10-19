"""0084_admin_mpin_work_hours — MPIN lock columns, daily work-hours table, admin session config

Revision ID: 0084
Revises: 0083

Doc Ref: Admin Portal §39 Security (session handling); DB Architecture Part 3 §5 (users).

Why:
  The owner does not want the dashboard to throw an admin out after ten idle
  minutes — an admin mid-booking loses the form they were filling. The session
  is to survive and the screen merely LOCKS behind a per-user MPIN, with a
  forced logout once a day at a super-admin-configurable hour. That needs three
  things the schema does not have.

  1. An MPIN to unlock with. It lives on `users` rather than a 1:1 table because
     `password_hash`, `failed_login_attempts` and `locked_until` are already
     there and the row is already loaded on every /auth/me — a side table would
     add a JOIN to the hottest admin path for four columns.

     MPIN gets its OWN counter and deadline instead of reusing
     `failed_login_attempts` / `locked_until`: `User.is_locked` reads
     `locked_until`, so a few MPIN typos on the unlock screen would lock the
     password login too and the admin could not get back in at all.

  2. A record of who was in the dashboard and for how long, for the super
     admin's leave/attendance view. Neither existing source can produce it:
     `audit_logs.user_id` is BIGINT while `users.id` is UUID, so the auth layer
     inserts login rows with user_id NULL (unattributable), and
     `user_sessions.logout_at` has been declared since 0001 but is never written
     anywhere — every session row looks like it is still running. `sessions`
     also has one row per device, not one row per day, so "hours on Tuesday"
     would mean stitching overlapping ranges together in SQL every time.

     Hence a day row: one per (user, date), accumulating only ACTIVE seconds
     from a client heartbeat. `first_login_at` / `last_logout_at` are the
     bookends an attendance report reads; `last_seen_at` bounds a day whose
     admin closed the laptop without logging out. `active_seconds` is capped by
     CHECK at 86400 so a broken client cannot write a 30-hour day.

  3. Config keys for the policy (default idle minutes, daily logout hour, MPIN
     strength/lockout) so the super admin can change them from /settings
     without a deploy. Seeded, not created on demand — `ADMIN_IDLE_DEFAULT_MINUTES`
     has to exist before the first admin opens their profile and sees "inherits
     the platform default of 5".

     `idle_lock_minutes` on the user row is nullable on purpose: NULL means
     "inherit the platform default", so raising the default later still reaches
     everyone who never customized, and only the admins who deliberately picked
     a shorter time keep their choice.

Note: there is no separate "max clamp" config key — the DB CHECK
(1..240) plus the Pydantic validator on the profile endpoint are the clamp.

Everything is idempotent: ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS,
CREATE INDEX IF NOT EXISTS, the DO-block constraint guard, and
ON CONFLICT DO NOTHING on the seeds.
"""

from alembic import op

revision = "0084"
down_revision = "0083"
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
    # ── users: MPIN unlock + per-user idle time ─────────────────────────
    for ddl in (
        "mpin_hash TEXT",
        "mpin_locked_until TIMESTAMPTZ",
        "idle_lock_minutes INT",
    ):
        op.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {ddl}")
    op.execute(
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS mpin_failed_attempts INT NOT NULL DEFAULT 0
        """
    )
    # A custom idle time is an admin making the screen lock SOONER, never a way
    # to dodge the lock by asking for a week. 240 keeps a legitimate
    # "whole morning of reading" option available.
    _check("users", "ck_users_idle_lock_minutes",
           "idle_lock_minutes IS NULL OR (idle_lock_minutes >= 1 AND idle_lock_minutes <= 240)")
    _check("users", "ck_users_mpin_attempts", "mpin_failed_attempts >= 0")

    # ── staff_work_daily: one accumulated row per person per day ────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_work_daily (
            id BIGSERIAL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            work_date DATE NOT NULL,
            first_login_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_logout_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active_seconds INT NOT NULL DEFAULT 0,
            session_count INT NOT NULL DEFAULT 1,
            heartbeat_count INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    _check("staff_work_daily", "ck_swd_active_seconds",
           "active_seconds >= 0 AND active_seconds <= 86400")
    # The heartbeat upsert target — also the report's "no duplicate day rows"
    # guarantee under the multi-tab lease.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_work_daily_user_date
            ON staff_work_daily (user_id, work_date)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_work_daily_date
            ON staff_work_daily (work_date DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_work_daily_user
            ON staff_work_daily (user_id, work_date DESC)
        """
    )

    # ── policy config seeds ─────────────────────────────────────────────
    op.execute(
        """
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('ADMIN_IDLE_DEFAULT_MINUTES', '5',  'Idle minutes before the admin dashboard locks for staff with no personal setting', NOW()),
            ('ADMIN_DAILY_LOGOUT_HOUR',    '23', 'Hour of day (PLATFORM_TIMEZONE) when every admin session is force-logged-out',     NOW()),
            ('ADMIN_MPIN_MIN_LENGTH',      '6',  'Minimum digits in an admin MPIN',                                                  NOW()),
            ('ADMIN_MPIN_MAX_ATTEMPTS',    '5',  'Wrong MPIN attempts before the unlock screen is locked',                           NOW()),
            ('ADMIN_MPIN_LOCKOUT_MINUTES', '15', 'Minutes an MPIN lockout lasts',                                                    NOW())
        ON CONFLICT (config_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_staff_work_daily_user")
    op.execute("DROP INDEX IF EXISTS idx_staff_work_daily_date")
    op.execute("DROP INDEX IF EXISTS uq_staff_work_daily_user_date")
    op.execute("DROP TABLE IF EXISTS staff_work_daily")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_mpin_attempts")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_idle_lock_minutes")
    for name in ("idle_lock_minutes", "mpin_locked_until", "mpin_failed_attempts", "mpin_hash"):
        op.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {name}")
    op.execute(
        """
        DELETE FROM system_configurations
         WHERE config_key IN ('ADMIN_IDLE_DEFAULT_MINUTES','ADMIN_DAILY_LOGOUT_HOUR',
                              'ADMIN_MPIN_MIN_LENGTH','ADMIN_MPIN_MAX_ATTEMPTS',
                              'ADMIN_MPIN_LOCKOUT_MINUTES')
        """
    )
