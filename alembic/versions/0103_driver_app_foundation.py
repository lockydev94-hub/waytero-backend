"""0103 — WayTero Partner Driver app foundation

Doc Ref: 06_Backend_Architecture/11_DRIVER_APP_BACKEND.md
         04_API_Documentation/13_DRIVER_APP_API.md

Why:
  * Drivers are rows in `drivers`, not users. The driver app cannot obtain a
    JWT without a DRIVER `users` row, so every active driver is backfilled one
    and linked through `drivers.user_id`. The backfill is deliberately
    conservative: a mobile already owned by another user type is left alone
    rather than hijacked.
  * The partner's acceptance (`cab_booking_assignments.accepted_at`) is not the
    driver's acceptance. The new offer columns record the driver-side decision
    and its 60-second window separately.
  * One driver may hold only one live assignment. A partial unique index makes
    that a database fact, not a service-level convention.
  * Pause/resume needs a pause anchor and accumulated seconds; map distance is
    accumulated server-side from batched GPS points and deliberately excludes
    paused movement.
  * Odometer-vs-map mismatches are preserved for partner review instead of
    blocking trip completion.

Idempotent, raw SQL — matches the existing hand-written migration style.
"""

from alembic import op
import sqlalchemy as sa

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Driver identities ────────────────────────────────────────────────
    conn.execute(
        sa.text(
            """
            INSERT INTO users (
                id, user_code, first_name, last_name, mobile_number, email,
                user_type, status, is_mobile_verified, is_email_verified,
                is_active, failed_login_attempts, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                LEFT('DRV-' || d.driver_code, 30),
                COALESCE(NULLIF(SPLIT_PART(d.full_name, ' ', 1), ''), 'Driver'),
                NULLIF(TRIM(SUBSTRING(d.full_name FROM POSITION(' ' IN d.full_name))), ''),
                d.mobile,
                CASE
                    WHEN d.email IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM users eu
                         WHERE LOWER(eu.email) = LOWER(d.email)
                     )
                    THEN LOWER(d.email)
                    ELSE NULL
                END,
                'DRIVER'::user_type_enum,
                'ACTIVE'::user_status_enum,
                TRUE,
                FALSE,
                TRUE,
                0,
                NOW(),
                NOW()
            FROM drivers d
            WHERE d.user_id IS NULL
              AND d.deleted_at IS NULL
              AND d.mobile IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM users u WHERE u.mobile_number = d.mobile
              )
            ON CONFLICT DO NOTHING
            """
        )
    )

    conn.execute(
        sa.text(
            """
            UPDATE drivers d
               SET user_id = u.id,
                   updated_at = NOW()
              FROM users u
             WHERE d.user_id IS NULL
               AND u.mobile_number = d.mobile
               AND u.user_type = 'DRIVER'
               AND d.deleted_at IS NULL
            """
        )
    )

    # ── Offer lifecycle on the assignment ────────────────────────────────
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_booking_assignments
                ADD COLUMN IF NOT EXISTS offer_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                ADD COLUMN IF NOT EXISTS offered_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS offer_expires_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS driver_accepted_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS driver_declined_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS driver_decline_reason VARCHAR(50),
                ADD COLUMN IF NOT EXISTS driver_decline_notes TEXT
            """
        )
    )

    # Existing rows were assigned before driver acceptance existed. Treat them
    # as already accepted so deploying this does not strand live trips.
    conn.execute(
        sa.text(
            """
            UPDATE cab_booking_assignments
               SET offer_status = 'ACCEPTED',
                   offered_at = COALESCE(offered_at, assigned_at),
                   driver_accepted_at = COALESCE(driver_accepted_at, assigned_at)
             WHERE driver_id IS NOT NULL
               AND closed_at IS NULL
               AND driver_accepted_at IS NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE cab_booking_assignments
               SET offer_status = 'CLOSED'
             WHERE closed_at IS NOT NULL
               AND offer_status = 'PENDING'
            """
        )
    )

    # One live assignment per driver. Any historical duplicate open rows are
    # closed first so the index can be created on an existing production DB.
    conn.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY driver_id
                           ORDER BY assigned_at DESC NULLS LAST, id DESC
                       ) AS rn
                  FROM cab_booking_assignments
                 WHERE closed_at IS NULL
                   AND driver_id IS NOT NULL
            )
            UPDATE cab_booking_assignments a
               SET closed_at = NOW(),
                   close_reason = 'DRIVER_DUPLICATE_CLEANUP'
              FROM ranked r
             WHERE a.id = r.id AND r.rn > 1
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_driver_one_live_assignment
                ON cab_booking_assignments (driver_id)
                WHERE closed_at IS NULL
                  AND driver_id IS NOT NULL
                  AND offer_status IN ('PENDING', 'ACCEPTED')
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_assignment_driver_offer
                ON cab_booking_assignments (driver_id, offer_status, offer_expires_at)
            """
        )
    )

    # ── Trip execution fields on the cab booking ─────────────────────────
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_bookings
                ADD COLUMN IF NOT EXISTS trip_paused_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS trip_paused_seconds INTEGER NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS gps_distance_km NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS odometer_distance_km NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS distance_mismatch_km NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS distance_mismatch_pct NUMERIC(7,2),
                ADD COLUMN IF NOT EXISTS distance_review_status VARCHAR(30),
                ADD COLUMN IF NOT EXISTS trip_last_lat NUMERIC(10,7),
                ADD COLUMN IF NOT EXISTS trip_last_lng NUMERIC(10,7),
                ADD COLUMN IF NOT EXISTS trip_last_gps_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS cash_handover_notified_at TIMESTAMPTZ
            """
        )
    )

    # ── Batched GPS evidence ─────────────────────────────────────────────
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS driver_trip_points (
                id BIGSERIAL PRIMARY KEY,
                cab_booking_id BIGINT NOT NULL REFERENCES cab_bookings(id) ON DELETE CASCADE,
                assignment_id BIGINT REFERENCES cab_booking_assignments(id) ON DELETE SET NULL,
                driver_id BIGINT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
                latitude NUMERIC(10,7) NOT NULL,
                longitude NUMERIC(10,7) NOT NULL,
                accuracy_m NUMERIC(8,2),
                speed_mps NUMERIC(8,2),
                is_paused BOOLEAN NOT NULL DEFAULT FALSE,
                recorded_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_trip_points_booking_time
                ON driver_trip_points (cab_booking_id, recorded_at)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_trip_points_driver_time
                ON driver_trip_points (driver_id, recorded_at DESC)
            """
        )
    )

    # ── Odometer-vs-map review ───────────────────────────────────────────
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS trip_distance_reviews (
                id BIGSERIAL PRIMARY KEY,
                cab_booking_id BIGINT NOT NULL UNIQUE
                    REFERENCES cab_bookings(id) ON DELETE CASCADE,
                assignment_id BIGINT REFERENCES cab_booking_assignments(id) ON DELETE SET NULL,
                driver_id BIGINT NOT NULL REFERENCES drivers(id),
                odometer_km NUMERIC(12,2) NOT NULL,
                gps_km NUMERIC(12,2) NOT NULL,
                difference_km NUMERIC(12,2) NOT NULL,
                difference_pct NUMERIC(7,2) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'PENDING_REVIEW',
                review_note TEXT,
                reviewed_by_user_id UUID REFERENCES users(id),
                reviewed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_distance_reviews_status
                ON trip_distance_reviews (status, created_at DESC)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS trip_distance_reviews"))
    conn.execute(sa.text("DROP TABLE IF EXISTS driver_trip_points"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_bookings
                DROP COLUMN IF EXISTS trip_paused_at,
                DROP COLUMN IF EXISTS trip_paused_seconds,
                DROP COLUMN IF EXISTS gps_distance_km,
                DROP COLUMN IF EXISTS odometer_distance_km,
                DROP COLUMN IF EXISTS distance_mismatch_km,
                DROP COLUMN IF EXISTS distance_mismatch_pct,
                DROP COLUMN IF EXISTS distance_review_status,
                DROP COLUMN IF EXISTS trip_last_lat,
                DROP COLUMN IF EXISTS trip_last_lng,
                DROP COLUMN IF EXISTS trip_last_gps_at,
                DROP COLUMN IF EXISTS cash_handover_notified_at
            """
        )
    )
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_assignment_driver_offer"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ux_driver_one_live_assignment"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_booking_assignments
                DROP COLUMN IF EXISTS offer_status,
                DROP COLUMN IF EXISTS offered_at,
                DROP COLUMN IF EXISTS offer_expires_at,
                DROP COLUMN IF EXISTS driver_accepted_at,
                DROP COLUMN IF EXISTS driver_declined_at,
                DROP COLUMN IF EXISTS driver_decline_reason,
                DROP COLUMN IF EXISTS driver_decline_notes
            """
        )
    )
    # Deliberately do not delete backfilled users: they may already own
    # sessions and audit rows.
