"""Admin-managed inter-city cab routes priced by the live fare engine.

Doc Ref: BRD Part 3 §35-§36 (fare engine, city pricing); migration 0079.

A route stores only its endpoints and the distance between them — never a
price. Vehicle fares are computed per request from `pricing_city_id`'s rules,
so a route quote can never drift from what the booking flow charges.
"""

from alembic import op

revision = "0079"
down_revision = "0078"
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
        CREATE TABLE IF NOT EXISTS cab_routes (
            id BIGSERIAL PRIMARY KEY,
            slug VARCHAR(160) NOT NULL UNIQUE,
            origin_label VARCHAR(200) NOT NULL,
            destination_label VARCHAR(200) NOT NULL,
            origin_city_id BIGINT REFERENCES cities(id) ON DELETE SET NULL,
            destination_city_id BIGINT REFERENCES cities(id) ON DELETE SET NULL,
            pricing_city_id BIGINT NOT NULL REFERENCES cities(id) ON DELETE RESTRICT,
            distance_km NUMERIC(12,2) NOT NULL,
            distance_source VARCHAR(10) NOT NULL DEFAULT 'MANUAL',
            description TEXT NOT NULL DEFAULT '',
            terms_and_conditions TEXT NOT NULL DEFAULT '',
            image_url TEXT,
            is_active BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order SMALLINT NOT NULL DEFAULT 0,
            created_by VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    )
    _check("cab_routes", "ck_cab_routes_distance", "distance_km > 0 AND distance_km <= 5000")
    _check(
        "cab_routes",
        "ck_cab_routes_distance_source",
        "distance_source IN ('GOOGLE','HAVERSINE','MANUAL')",
    )
    _check(
        "cab_routes",
        "ck_cab_routes_endpoints_differ",
        "lower(trim(origin_label)) <> lower(trim(destination_label))",
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cab_routes_public
            ON cab_routes (sort_order, id) WHERE is_active
    """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_cab_routes_pricing_city ON cab_routes (pricing_city_id)"
    )
    # Two admins must not publish the same corridor twice under different slugs.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_cab_routes_endpoints
            ON cab_routes (lower(origin_label), lower(destination_label))
    """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cab_route_vehicle_categories (
            route_id BIGINT NOT NULL REFERENCES cab_routes(id) ON DELETE CASCADE,
            vehicle_category_id BIGINT NOT NULL
                REFERENCES vehicle_categories(id) ON DELETE CASCADE,
            PRIMARY KEY (route_id, vehicle_category_id)
        )
    """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crvs_vehicle
            ON cab_route_vehicle_categories (vehicle_category_id)
    """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cab_route_vehicle_categories")
    op.execute("DROP TABLE IF EXISTS cab_routes")
