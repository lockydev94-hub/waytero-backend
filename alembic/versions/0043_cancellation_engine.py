"""0043_cancellation_engine — admin-managed cancellation policy + request/approve flow

Revision ID: 0043_cancellation_engine
Revises: 0042_hotel_switch
Create Date: 2026-08-09

Doc Ref:
  BRD Part 3 §46 (Cab Cancellation Rules), §47 (Refund Rules)
  BRD Part 4 §82 (Hotel Cancellation Policy)
  BRD Part 7 §134 (BOOKING_CANCELLED notification)
  SRS Part 3 §99/§100 (Cancellation + Refund engine)
  SRS Part 5 §184/§185 (Hotel cancellation/refund engine)
  API Doc §19/§20/§22 (Cancel service / master / refund-preview)

Why:
  Cancellation today is admin-typed: the cancel endpoints accept
  cancellation_charge + refund_amount as plain numbers from the form, with
  no policy lookup. There is no partner-initiated cancel at all (partner
  can only "reject back to PENDING_ASSIGNMENT"), no customer cancel, no
  refund preview, and no notification on cancel.

  This migration lays the data-model foundation for the new admin-managed
  cancellation policy engine:

    1. Global cab ladder lives in system_configurations (already loaded by
       services; no new table needed — we add 5 seeded keys).
    2. cancellation_policy_versions — append-only audit of every admin
       edit to a global config key (used for "policy in effect at time of
       cancel" snapshots).
    3. booking_cancellation_requests — partner asks admin to cancel a
       cab/hotel reservation. Admin approves/rejects from the dashboard.
    4. reservation_cancellations — per-hotel cancellation row (sibling to
       booking_cancellations which is per-master-booking). Carries
       snapshot of the policy that was applied, the actor, and the source
       (CUSTOMER | PARTNER_REQUEST | ADMIN | SYSTEM).
    5. booking_cancellations gets a source + actor_role + policy_snapshot
       JSONB so we never lose the "what rule did we apply" trail.
    6. hotel_reservations.cancelled_by_user_id + cancelled_source
       columns; the timeline is already audited via BookingTimeline.
    7. cab_bookings.cancelled_by_user_id + cancelled_source — same idea.
    8. New permission strings: booking.cancel.{customer,partner,admin}
       plus booking.cancellation_policy.manage.

Notes:
  Every write is idempotent. No raw column data is destroyed in
  downgrade; new columns are dropped and tables removed.
"""

import sqlalchemy as sa
from alembic import op


revision = "0043_cancellation_engine"
down_revision = "0042_hotel_switch"
branch_labels = None
depends_on = None


CAB_LADDER_SEEDS = [
    ("CANCELLATION_FREE_HOURS_CAB", "2", "Free-cancel window (hours before pickup)"),
    ("CANCELLATION_TIER_1_HOURS_CAB", "12", "Tier-1 cutoff (hours before pickup) — full refund"),
    ("CANCELLATION_TIER_1_PERCENT_CAB", "75", "Tier-1 refund percent"),
    ("CANCELLATION_TIER_2_HOURS_CAB", "4", "Tier-2 cutoff (hours before pickup)"),
    ("CANCELLATION_TIER_2_PERCENT_CAB", "50", "Tier-2 refund percent"),
    ("CANCELLATION_SAME_DAY_PERCENT_CAB", "0", "Same-day / post-pickup refund percent"),
    ("CANCELLATION_AFTER_ASSIGNMENT_PERCENT_CAB", "50",
     "After-assignment refund percent (regardless of time)"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ════════════════════════════════════════════════════════════════
    # 1. Global cab cancellation ladder seeded into system_configurations
    #    We do not add a new column; the table already has the shape.
    #    These keys are read by the cancellation policy engine at runtime.
    # ════════════════════════════════════════════════════════════════
    for key, value, desc in CAB_LADDER_SEEDS:
        conn.execute(
            sa.text(
                """
                INSERT INTO system_configurations (config_key, config_value, description, updated_at)
                VALUES (:k, :v, :d, NOW())
                ON CONFLICT (config_key) DO NOTHING
                """
            ),
            {"k": key, "v": value, "d": desc},
        )

    # ════════════════════════════════════════════════════════════════
    # 2. cancellation_policy_versions — append-only audit of admin edits
    #    Every PUT /admin/settings/configurations/{key} writes a row here
    #    before the new value is committed. Lets us answer "what policy
    #    was in effect at the moment booking #12345 was cancelled".
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS cancellation_policy_versions (
                id              BIGSERIAL PRIMARY KEY,
                config_key      VARCHAR(100) NOT NULL,
                previous_value  VARCHAR(500),
                new_value       VARCHAR(500) NOT NULL,
                changed_by_user_id UUID REFERENCES users(id),
                change_reason   TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_policy_versions_key_time
                ON cancellation_policy_versions (config_key, created_at DESC)
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 3. booking_cancellation_requests — partner asks admin to cancel.
    #    Status: PENDING | APPROVED | REJECTED | WITHDRAWN | AUTO_CLOSED.
    #    On APPROVED the cancel handler runs and the request row is
    #    preserved (history).
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS booking_cancellation_requests (
                id                      BIGSERIAL PRIMARY KEY,
                booking_type            VARCHAR(20) NOT NULL,
                                                  -- CAB | HOTEL
                master_booking_id       BIGINT REFERENCES master_bookings(id),
                cab_booking_id          BIGINT REFERENCES cab_bookings(id),
                hotel_reservation_id    BIGINT REFERENCES hotel_reservations(id),

                requested_by_user_id    UUID NOT NULL REFERENCES users(id),
                requested_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                requested_reason        TEXT NOT NULL,
                requested_reason_code   VARCHAR(40),

                refund_preview_json     JSONB,

                status                  VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                reviewed_by_user_id     UUID REFERENCES users(id),
                reviewed_at             TIMESTAMPTZ,
                review_note             TEXT,

                CONSTRAINT chk_req_target CHECK (
                    (booking_type = 'CAB'  AND cab_booking_id IS NOT NULL
                                            AND hotel_reservation_id IS NULL) OR
                    (booking_type = 'HOTEL' AND hotel_reservation_id IS NOT NULL
                                            AND cab_booking_id IS NULL)
                )
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_cancel_req_status
                ON booking_cancellation_requests (status, requested_at DESC)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_cancel_req_cab
                ON booking_cancellation_requests (cab_booking_id)
                WHERE cab_booking_id IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_cancel_req_hotel
                ON booking_cancellation_requests (hotel_reservation_id)
                WHERE hotel_reservation_id IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_cancel_req_partner
                ON booking_cancellation_requests (requested_by_user_id, requested_at DESC)
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 4. reservation_cancellations — sibling of booking_cancellations
    #    for the hotel reservation level. Carries the snapshot of the
    #    policy applied so the audit trail is intact even if the hotel
    #    later edits its own policy or the global config moves.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS reservation_cancellations (
                id                       BIGSERIAL PRIMARY KEY,
                hotel_reservation_id     BIGINT NOT NULL UNIQUE
                                            REFERENCES hotel_reservations(id),
                cancelled_by_user_id     UUID REFERENCES users(id),
                cancelled_source         VARCHAR(20) NOT NULL,
                                            -- CUSTOMER | PARTNER_REQUEST | ADMIN | SYSTEM
                cancellation_reason      TEXT,
                cancellation_charge      NUMERIC(12,2) NOT NULL DEFAULT 0,
                refund_amount            NUMERIC(12,2) NOT NULL DEFAULT 0,
                policy_snapshot          JSONB,
                advance_refunded_total   NUMERIC(14,2) NOT NULL DEFAULT 0,
                cancelled_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 5. booking_cancellations — extend with source/role/snapshot.
    #    All additions are nullable / have defaults — safe on existing rows.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            ALTER TABLE booking_cancellations
                ADD COLUMN IF NOT EXISTS cancelled_source       VARCHAR(20) NOT NULL DEFAULT 'ADMIN',
                ADD COLUMN IF NOT EXISTS cancelled_by_role      VARCHAR(30),
                ADD COLUMN IF NOT EXISTS policy_snapshot        JSONB,
                ADD COLUMN IF NOT EXISTS advance_refunded_total NUMERIC(14,2) NOT NULL DEFAULT 0
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 6. hotel_reservations — actor + source on the row itself
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            ALTER TABLE hotel_reservations
                ADD COLUMN IF NOT EXISTS cancelled_by_user_id UUID REFERENCES users(id),
                ADD COLUMN IF NOT EXISTS cancelled_source     VARCHAR(20)
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 7. cab_bookings — same actor + source columns
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_bookings
                ADD COLUMN IF NOT EXISTS cancelled_by_user_id UUID REFERENCES users(id),
                ADD COLUMN IF NOT EXISTS cancelled_source     VARCHAR(20)
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 8. New permission codes. Idempotent via ON CONFLICT.
    #    The default role-permission mappings still need to be granted;
    #    that is done by the application layer (role-permission bootstrap)
    #    but we seed the rows so the matrix lookup can resolve.
    # ════════════════════════════════════════════════════════════════
    for code, desc in (
        ("booking.cancel.customer",
         "Cancel own booking (customer-side self-cancel)"),
        ("booking.cancel.partner_request",
         "Submit a cancellation request for an assigned booking (partner)"),
        ("booking.cancel.admin",
         "Approve, reject, or directly cancel any booking (admin)"),
        ("booking.cancellation_policy.manage",
         "Edit the global cancellation policy ladder (admin)"),
    ):
        conn.execute(
            sa.text(
                """
                INSERT INTO permissions (id, permission_code, permission_name, description)
                VALUES (gen_random_uuid(), :c, :c, :d)
                ON CONFLICT (permission_code) DO NOTHING
                """
            ),
            {"c": code, "d": desc},
        )

    # Default grants: ADMIN + SUPER_ADMIN get the admin + policy codes.
    # CUSTOMER gets customer self-cancel. PARTNER gets partner-request.
    # All other roles (DRIVER, CCO, VERIFICATION_OFFICER, FINANCE_MANAGER)
    # get nothing — they don't cancel.
    role_perm_map = {
        "SUPER_ADMIN": [
            "booking.cancel.admin",
            "booking.cancellation_policy.manage",
        ],
        "ADMIN": [
            "booking.cancel.admin",
            "booking.cancellation_policy.manage",
        ],
        "CUSTOMER": ["booking.cancel.customer"],
        "PARTNER": ["booking.cancel.partner_request"],
    }
    for role_code, perms in role_perm_map.items():
        for perm_code in perms:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                    SELECT gen_random_uuid(), r.id, p.id, NOW()
                    FROM roles r, permissions p
                    WHERE r.role_code = :rc AND p.permission_code = :pc
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"rc": role_code, "pc": perm_code},
            )


def downgrade() -> None:
    conn = op.get_bind()

    # 8. Revoke + remove the new permissions
    for code in (
        "booking.cancel.customer",
        "booking.cancel.partner_request",
        "booking.cancel.admin",
        "booking.cancellation_policy.manage",
    ):
        conn.execute(
            sa.text(
                """
                DELETE FROM role_permissions
                WHERE permission_id IN (
                    SELECT id FROM permissions WHERE permission_code = :c
                )
                """
            ),
            {"c": code},
        )
        conn.execute(
            sa.text("DELETE FROM permissions WHERE permission_code = :c"),
            {"c": code},
        )

    # 7. cab_bookings
    conn.execute(
        sa.text(
            """
            ALTER TABLE cab_bookings
                DROP COLUMN IF EXISTS cancelled_source,
                DROP COLUMN IF EXISTS cancelled_by_user_id
            """
        )
    )

    # 6. hotel_reservations
    conn.execute(
        sa.text(
            """
            ALTER TABLE hotel_reservations
                DROP COLUMN IF EXISTS cancelled_source,
                DROP COLUMN IF EXISTS cancelled_by_user_id
            """
        )
    )

    # 5. booking_cancellations
    conn.execute(
        sa.text(
            """
            ALTER TABLE booking_cancellations
                DROP COLUMN IF EXISTS advance_refunded_total,
                DROP COLUMN IF EXISTS policy_snapshot,
                DROP COLUMN IF EXISTS cancelled_by_role,
                DROP COLUMN IF EXISTS cancelled_source
            """
        )
    )

    # 4. reservation_cancellations
    conn.execute(sa.text("DROP TABLE IF EXISTS reservation_cancellations"))

    # 3. booking_cancellation_requests
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cancel_req_partner"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cancel_req_hotel"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cancel_req_cab"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cancel_req_status"))
    conn.execute(sa.text("DROP TABLE IF EXISTS booking_cancellation_requests"))

    # 2. cancellation_policy_versions
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_policy_versions_key_time"))
    conn.execute(sa.text("DROP TABLE IF EXISTS cancellation_policy_versions"))

    # 1. cab ladder seed keys (only delete if no other caller relies on them)
    for key, *_ in CAB_LADDER_SEEDS:
        conn.execute(
            sa.text("DELETE FROM system_configurations WHERE config_key = :k"),
            {"k": key},
        )
