"""Coupon funding and cab participation audit. Doc Ref: BRD Part 6 §129, §147."""

from alembic import op

revision = "0075"
down_revision = "0074"
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
    op.execute(
        """
        ALTER TABLE coupons
            ADD COLUMN IF NOT EXISTS coupon_type VARCHAR(20) NOT NULL DEFAULT 'PLATFORM',
            ADD COLUMN IF NOT EXISTS platform_share_percent SMALLINT
    """
    )
    _check(
        "coupons",
        "ck_coupons_coupon_type",
        "coupon_type IN ('PLATFORM','PARTNER_ONLY','PLATFORM_PARTNER')",
    )
    _check(
        "coupons",
        "ck_coupons_platform_share_pct",
        "(coupon_type = 'PLATFORM_PARTNER' AND platform_share_percent IS NOT NULL "
        "AND platform_share_percent BETWEEN 1 AND 99) OR "
        "(coupon_type <> 'PLATFORM_PARTNER' AND platform_share_percent IS NULL)",
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS coupon_partner_rules (
            id BIGSERIAL PRIMARY KEY,
            coupon_id BIGINT NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
            partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
            joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by VARCHAR(100),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            removed_at TIMESTAMPTZ,
            removed_by VARCHAR(100),
            CONSTRAINT uq_cpr_coupon_partner UNIQUE (coupon_id, partner_id)
        )
    """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cpr_partner ON coupon_partner_rules (partner_id)"
    )
    op.execute(
        """
        ALTER TABLE tour_bookings
            ADD COLUMN IF NOT EXISTS coupon_code VARCHAR(50),
            ADD COLUMN IF NOT EXISTS coupon_discount NUMERIC(12,2) NOT NULL DEFAULT 0
    """
    )
    op.execute(
        """
        ALTER TABLE coupon_usages
            ADD COLUMN IF NOT EXISTS coupon_type VARCHAR(20),
            ADD COLUMN IF NOT EXISTS platform_share NUMERIC(12,2),
            ADD COLUMN IF NOT EXISTS partner_share NUMERIC(12,2),
            ADD COLUMN IF NOT EXISTS cab_booking_id BIGINT REFERENCES cab_bookings(id),
            ADD COLUMN IF NOT EXISTS hotel_reservation_id BIGINT REFERENCES hotel_reservations(id),
            ADD COLUMN IF NOT EXISTS tour_booking_id BIGINT REFERENCES tour_bookings(id),
            ADD COLUMN IF NOT EXISTS eligible_partner_ids JSONB NOT NULL DEFAULT '[]'::jsonb
    """
    )
    op.execute(
        """
        UPDATE coupon_usages SET coupon_type = 'PLATFORM',
            platform_share = discount_applied, partner_share = 0
        WHERE coupon_type IS NULL AND platform_share IS NULL AND partner_share IS NULL
    """
    )
    op.execute(
        """
        ALTER TABLE coupon_usages ALTER COLUMN coupon_type SET NOT NULL,
            ALTER COLUMN platform_share SET NOT NULL, ALTER COLUMN partner_share SET NOT NULL
    """
    )
    # Ambiguous historical service links stay unallocated for explicit reconciliation.
    op.execute(
        """
        WITH candidates AS (
            SELECT cu.id, cb.id AS cab_id, NULL::BIGINT AS hotel_id
            FROM coupon_usages cu JOIN cab_bookings cb
              ON cb.master_booking_id = cu.master_booking_id
             AND cb.coupon_id = cu.coupon_id AND cb.coupon_discount = cu.discount_applied
            UNION ALL
            SELECT cu.id, NULL::BIGINT, hr.id FROM coupon_usages cu
            JOIN coupons c ON c.id = cu.coupon_id
            JOIN hotel_reservations hr ON hr.master_booking_id = cu.master_booking_id
             AND hr.coupon_code = c.coupon_code AND hr.coupon_discount = cu.discount_applied
        ), unambiguous AS (
            SELECT id, MAX(cab_id) AS cab_id, MAX(hotel_id) AS hotel_id
            FROM candidates GROUP BY id HAVING COUNT(*) = 1
        )
        UPDATE coupon_usages cu SET cab_booking_id = u.cab_id, hotel_reservation_id = u.hotel_id
        FROM unambiguous u WHERE cu.id = u.id AND cu.cab_booking_id IS NULL
            AND cu.hotel_reservation_id IS NULL AND cu.tour_booking_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM coupon_usages other
                WHERE other.master_booking_id = cu.master_booking_id
                  AND other.coupon_id = cu.coupon_id AND other.id <> cu.id)
    """
    )
    _check(
        "coupon_usages",
        "ck_cu_funding",
        "platform_share >= 0 AND partner_share >= 0 AND "
        "platform_share + partner_share = discount_applied AND "
        "((coupon_type = 'PLATFORM' AND partner_share = 0) OR "
        "(coupon_type = 'PARTNER_ONLY' AND platform_share = 0) OR "
        "coupon_type = 'PLATFORM_PARTNER')",
    )
    _check(
        "coupon_usages",
        "ck_cu_service_link",
        "num_nonnulls(cab_booking_id, hotel_reservation_id, tour_booking_id) <= 1",
    )
    _check(
        "coupon_usages", "ck_cu_cohort", "jsonb_typeof(eligible_partner_ids) = 'array'"
    )
    for column in ("cab_booking_id", "hotel_reservation_id", "tour_booking_id"):
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS uq_cu_{column} "
            f"ON coupon_usages ({column}) WHERE {column} IS NOT NULL"
        )
    op.execute(
        """
        ALTER TABLE pending_coupon_disbursements
            ADD COLUMN IF NOT EXISTS platform_share NUMERIC(14,2),
            ADD COLUMN IF NOT EXISTS partner_share NUMERIC(14,2),
            ADD COLUMN IF NOT EXISTS tour_booking_id BIGINT REFERENCES tour_bookings(id)
    """
    )
    op.execute(
        """
        UPDATE pending_coupon_disbursements SET platform_share = coupon_discount_amount,
            partner_share = 0 WHERE platform_share IS NULL AND partner_share IS NULL
    """
    )
    op.execute(
        """
        ALTER TABLE pending_coupon_disbursements
            ALTER COLUMN platform_share SET NOT NULL, ALTER COLUMN partner_share SET NOT NULL
    """
    )
    _check(
        "pending_coupon_disbursements",
        "ck_pcd_funding",
        "platform_share >= 0 AND partner_share >= 0 AND "
        "platform_share + partner_share = coupon_discount_amount",
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pcd_tour_booking "
        "ON pending_coupon_disbursements(tour_booking_id) WHERE tour_booking_id IS NOT NULL"
    )
    op.execute(
        """
        ALTER TABLE cab_booking_assignments
            ADD COLUMN IF NOT EXISTS available_cabs_at_offer INTEGER,
            ADD COLUMN IF NOT EXISTS available_cabs_at_decline INTEGER,
            ADD COLUMN IF NOT EXISTS availability_checked_at TIMESTAMPTZ
    """
    )
    _check(
        "cab_booking_assignments",
        "ck_cba_coupon_capacity",
        "(available_cabs_at_offer IS NULL OR available_cabs_at_offer >= 0) AND "
        "(available_cabs_at_decline IS NULL OR available_cabs_at_decline >= 0)",
    )
    _check("tour_bookings", "ck_tour_coupon_discount", "coupon_discount >= 0")


def downgrade() -> None:
    # Funding and participation history must survive a code rollback.
    pass
