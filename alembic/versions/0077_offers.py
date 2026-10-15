"""Marketing offers backed by coupons. Doc Ref: BRD Part 6 §129 (offers CMS)."""

from alembic import op

revision = "0077"
down_revision = "0076"
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
        CREATE TABLE IF NOT EXISTS offers (
            id BIGSERIAL PRIMARY KEY,
            slug VARCHAR(160) NOT NULL UNIQUE,
            coupon_id BIGINT NOT NULL REFERENCES coupons(id) ON DELETE RESTRICT,
            service_type VARCHAR(20) NOT NULL,
            vehicle_category_id BIGINT REFERENCES vehicle_categories(id) ON DELETE RESTRICT,
            title VARCHAR(255) NOT NULL,
            short_description TEXT,
            terms_and_conditions TEXT NOT NULL DEFAULT '',
            image_url TEXT NOT NULL,
            badge_label VARCHAR(60),
            valid_from DATE NOT NULL,
            valid_to DATE NOT NULL,
            is_published BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order SMALLINT NOT NULL DEFAULT 0,
            created_by VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    _check(
        "offers",
        "ck_offers_service_type",
        "service_type IN ('CAB','HOTEL','TOUR')",
    )
    _check("offers", "ck_offers_window", "valid_from <= valid_to")
    _check(
        "offers",
        "ck_offers_cab_target",
        "(service_type = 'CAB' AND vehicle_category_id IS NOT NULL) OR "
        "(service_type <> 'CAB' AND vehicle_category_id IS NULL)",
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_offers_coupon ON offers (coupon_id)")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_offers_public ON offers (sort_order, valid_from)
        WHERE is_published
    """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS offer_hotel_links (
            id BIGSERIAL PRIMARY KEY,
            offer_id BIGINT NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            hotel_id BIGINT NOT NULL REFERENCES hotels(id) ON DELETE RESTRICT,
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            exit_requested_at TIMESTAMPTZ,
            exit_reason TEXT,
            exit_resolved_at TIMESTAMPTZ,
            exit_resolved_by VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_offer_hotel UNIQUE (offer_id, hotel_id)
        )
    """
    )
    _check(
        "offer_hotel_links",
        "ck_ohl_status",
        "status IN ('ACTIVE','EXIT_REQUESTED','REMOVED')",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ohl_hotel ON offer_hotel_links (hotel_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS offer_hotel_room_types (
            offer_id BIGINT NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            room_category_id BIGINT NOT NULL
                REFERENCES hotel_room_categories(id) ON DELETE CASCADE,
            PRIMARY KEY (offer_id, room_category_id)
        )
    """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS offer_tour_packages (
            id BIGSERIAL PRIMARY KEY,
            offer_id BIGINT NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            tour_package_id BIGINT NOT NULL REFERENCES tour_packages(id) ON DELETE RESTRICT,
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            exit_requested_at TIMESTAMPTZ,
            exit_reason TEXT,
            exit_resolved_at TIMESTAMPTZ,
            exit_resolved_by VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_offer_tour UNIQUE (offer_id, tour_package_id)
        )
    """
    )
    _check(
        "offer_tour_packages",
        "ck_otp_status",
        "status IN ('ACTIVE','EXIT_REQUESTED','REMOVED')",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_otp_tour ON offer_tour_packages (tour_package_id)"
    )
    for table in ("cab_bookings", "hotel_reservations", "tour_bookings"):
        op.execute(
            f"""ALTER TABLE {table} ADD COLUMN IF NOT EXISTS offer_id BIGINT
                REFERENCES offers(id) ON DELETE SET NULL"""
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_offer ON {table} (offer_id) "
            "WHERE offer_id IS NOT NULL"
        )


def downgrade() -> None:
    # Booking attribution history must survive a code rollback.
    pass
