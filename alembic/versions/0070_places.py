"""create places + PLACE_CABS landing pages — airport / railway-station pages

Doc Ref: Place-Level Landing Pages (plan: rosy-vent-shrew)
Revision: 0070
DownRevision: 0069

`landing_pages` (0069) can only scope to a city or a state. Place-level
queries — "bhubaneswar airport cab booking", "bhubaneswar railway station
taxi" — have no row to land on, because the platform has no places table at
all: hotels only keep a free-text `landmark`, cab bookings a free-text
`pickup_location`. This migration adds `places` (an admin-maintained registry
of airports and railway stations per city) and a seventh landing-page type,
`PLACE_CABS`, scoped to a place.

PLACE_CABS keeps `city_id` NOT NULL as well: the fare matrix is priced per
city (`cab_fare_matrix(db, city_id)`), so the place page shows the city's
airport-trip fares under the place's headline — the only honest number
available.

Following the 0069 precedent, provisioned pages ship UNPUBLISHED with NULL
copy; every INSERT is guarded by NOT EXISTS and ON CONFLICT DO NOTHING.
The CHECK constraints are dropped and recreated (DROP IF EXISTS + ADD keeps
this re-runnable).
"""

from alembic import op
import sqlalchemy as sa

revision = "0070"
down_revision = "0069"


# Same normalisation as migration 0069 / shared/utilities/seo.py.
def _slug_expr(col: str) -> str:
    return (
        f"btrim(regexp_replace(regexp_replace(lower(coalesce({col}, '')), "
        f"'[^a-z0-9]+', '-', 'g'), '^-+|-+$', '', 'g'))"
    )


def upgrade() -> None:
    conn = op.get_bind()

    # ── places ───────────────────────────────────────────────────────────────
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS places (
                id              BIGSERIAL    PRIMARY KEY,
                city_id         BIGINT       NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
                kind            VARCHAR(30)  NOT NULL,
                name            VARCHAR(200) NOT NULL,
                code            VARCHAR(10),
                address         TEXT,
                latitude        NUMERIC(10, 7),
                longitude       NUMERIC(10, 7),
                is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
                seo_title       VARCHAR(120),
                seo_description VARCHAR(320),
                seo_keywords    VARCHAR(500),
                created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

                CONSTRAINT chk_places_kind CHECK (
                    kind IN ('AIRPORT', 'RAILWAY_STATION')
                )
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_places_city_name "
            "ON places (city_id, lower(name))"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_places_code "
            "ON places (code, kind) WHERE code IS NOT NULL"
        )
    )
    conn.execute(
        sa.text("CREATE INDEX IF NOT EXISTS idx_places_city ON places (city_id)")
    )

    # ── landing_pages: place_id + PLACE_CABS ────────────────────────────────
    conn.execute(
        sa.text(
            "ALTER TABLE landing_pages "
            "ADD COLUMN IF NOT EXISTS place_id BIGINT "
            "REFERENCES places(id) ON DELETE CASCADE"
        )
    )
    # 0069's per-city uniqueness counted every city_id row; a city can now
    # host MANY place pages (airport + station), so the index only applies to
    # city/state-scope pages from here on.
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_landing_pages_city_type"))
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_landing_pages_city_type "
            "ON landing_pages (page_type, city_id) "
            "WHERE city_id IS NOT NULL AND place_id IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_landing_pages_place_type "
            "ON landing_pages (page_type, place_id) WHERE place_id IS NOT NULL"
        )
    )
    conn.execute(
        sa.text("CREATE INDEX IF NOT EXISTS idx_landing_pages_place ON landing_pages (place_id)")
    )

    conn.execute(sa.text("ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS chk_landing_pages_page_type"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE landing_pages ADD CONSTRAINT chk_landing_pages_page_type CHECK (
                page_type IN (
                    'CITY_HUB', 'CITY_CABS', 'CITY_HOTELS', 'CITY_TOURS',
                    'STATE_HOTELS', 'STATE_TOURS', 'PLACE_CABS'
                )
            )
            """
        )
    )

    conn.execute(sa.text("ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS chk_landing_pages_scope"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE landing_pages ADD CONSTRAINT chk_landing_pages_scope CHECK (
                (page_type IN ('CITY_HUB', 'CITY_CABS', 'CITY_HOTELS', 'CITY_TOURS')
                    AND city_id IS NOT NULL AND state_id IS NULL AND place_id IS NULL)
                OR
                (page_type IN ('STATE_HOTELS', 'STATE_TOURS')
                    AND state_id IS NOT NULL AND city_id IS NULL AND place_id IS NULL)
                OR
                -- A place page is city-scoped too: its fares, hotels and
                -- tours all resolve through the owning city.
                (page_type = 'PLACE_CABS'
                    AND place_id IS NOT NULL AND city_id IS NOT NULL AND state_id IS NULL)
            )
            """
        )
    )

    conn.execute(sa.text("ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS chk_landing_pages_service"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE landing_pages ADD CONSTRAINT chk_landing_pages_service CHECK (
                (page_type = 'CITY_HUB'     AND service_type IS NULL)
                OR (page_type = 'CITY_CABS'    AND service_type = 'CAB')
                OR (page_type = 'CITY_HOTELS'  AND service_type = 'HOTEL')
                OR (page_type = 'CITY_TOURS'   AND service_type = 'TOUR')
                OR (page_type = 'STATE_HOTELS' AND service_type = 'HOTEL')
                OR (page_type = 'STATE_TOURS'  AND service_type = 'TOUR')
                OR (page_type = 'PLACE_CABS'   AND service_type = 'CAB')
            )
            """
        )
    )

    # ── Seed: Bhubaneswar's airport + railway station ───────────────────────
    conn.execute(
        sa.text(
            """
            INSERT INTO places (city_id, kind, name, code, latitude, longitude)
            SELECT c.id, t.kind, t.name, t.code, t.lat, t.lng
              FROM cities c
              CROSS JOIN (VALUES
                    ('AIRPORT', 'Biju Patnaik International Airport', 'BBI',
                     20.2444000::numeric, 85.8174000::numeric),
                    ('RAILWAY_STATION', 'Bhubaneswar Railway Station', 'BBS',
                     20.2306000::numeric, 85.8275000::numeric)
              ) AS t(kind, name, code, lat, lng)
             WHERE lower(c.name) = 'bhubaneswar'
               AND NOT EXISTS (
                   SELECT 1 FROM places x
                    WHERE x.city_id = c.id AND lower(x.name) = lower(t.name)
               )
            ON CONFLICT DO NOTHING
            """
        )
    )

    # ── Provision PLACE_CABS pages for every active place (unpublished) ────
    # Keyword-first slug: "{city}-{place}-cab", with the city prefix dropped
    # from the place part when the name already starts with the city
    # ("Bhubaneswar Railway Station" → bhubaneswar-railway-station-cab, not
    # bhubaneswar-bhubaneswar-railway-station-cab).
    conn.execute(
        sa.text(
            f"""
            INSERT INTO landing_pages
                (slug, page_type, city_id, place_id, service_type, display_name,
                 is_published, display_order, created_at, updated_at)
            SELECT
                base.city_slug || '-' || base.place_slug || '-cab',
                'PLACE_CABS',
                base.city_id,
                base.place_id,
                'CAB',
                base.place_name,
                FALSE,
                1,
                now(),
                now()
              FROM (
                SELECT
                    c.id         AS city_id,
                    p.id         AS place_id,
                    p.name       AS place_name,
                    {_slug_expr('c.name')} AS city_slug,
                    CASE
                        WHEN {_slug_expr('p.name')} LIKE {_slug_expr('c.name')} || '-%'
                            THEN btrim(substring(
                                    {_slug_expr('p.name')}
                                    from length({_slug_expr('c.name')}) + 1
                                 ), '-')
                        ELSE {_slug_expr('p.name')}
                    END AS place_slug
                  FROM places p
                  JOIN cities c ON c.id = p.city_id
                 WHERE coalesce(p.is_active, FALSE) IS TRUE
                   AND coalesce(c.is_active, FALSE) IS TRUE
              ) base
             WHERE base.place_slug IS NOT NULL
               AND base.place_slug <> ''
               AND base.city_slug <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM landing_pages x
                    WHERE x.page_type = 'PLACE_CABS' AND x.place_id = base.place_id
               )
            ON CONFLICT DO NOTHING
            """
        )
    )

    # Publish the two showcase place pages, matching 0069's showcase set.
    conn.execute(
        sa.text(
            """
            UPDATE landing_pages
               SET is_published = TRUE,
                   updated_at   = now()
             WHERE slug IN ('bhubaneswar-biju-patnaik-international-airport-cab',
                            'bhubaneswar-railway-station-cab')
               AND is_published IS FALSE
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_landing_pages_place"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_landing_pages_place_type"))
    conn.execute(sa.text("ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS chk_landing_pages_page_type"))
    conn.execute(sa.text("ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS chk_landing_pages_scope"))
    conn.execute(sa.text("ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS chk_landing_pages_service"))
    # Rows must go before the 0069-era CHECKs come back — they reject
    # PLACE_CABS outright.
    conn.execute(sa.text("DELETE FROM landing_pages WHERE page_type = 'PLACE_CABS'"))
    conn.execute(
        sa.text(
            """
            ALTER TABLE landing_pages ADD CONSTRAINT chk_landing_pages_page_type CHECK (
                page_type IN (
                    'CITY_HUB', 'CITY_CABS', 'CITY_HOTELS', 'CITY_TOURS',
                    'STATE_HOTELS', 'STATE_TOURS'
                )
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE landing_pages ADD CONSTRAINT chk_landing_pages_scope CHECK (
                (page_type IN ('CITY_HUB', 'CITY_CABS', 'CITY_HOTELS', 'CITY_TOURS')
                    AND city_id IS NOT NULL AND state_id IS NULL)
                OR
                (page_type IN ('STATE_HOTELS', 'STATE_TOURS')
                    AND state_id IS NOT NULL AND city_id IS NULL)
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE landing_pages ADD CONSTRAINT chk_landing_pages_service CHECK (
                (page_type = 'CITY_HUB'    AND service_type IS NULL)
                OR (page_type = 'CITY_CABS'    AND service_type = 'CAB')
                OR (page_type = 'CITY_HOTELS'  AND service_type = 'HOTEL')
                OR (page_type = 'CITY_TOURS'   AND service_type = 'TOUR')
                OR (page_type = 'STATE_HOTELS' AND service_type = 'HOTEL')
                OR (page_type = 'STATE_TOURS'  AND service_type = 'TOUR')
            )
            """
        )
    )
    conn.execute(
        sa.text(
            "ALTER TABLE landing_pages DROP CONSTRAINT IF EXISTS "
            "landing_pages_place_id_fkey"
        )
    )
    conn.execute(sa.text("ALTER TABLE landing_pages DROP COLUMN IF EXISTS place_id"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_landing_pages_city_type"))
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_landing_pages_city_type "
            "ON landing_pages (page_type, city_id) WHERE city_id IS NOT NULL"
        )
    )
    conn.execute(sa.text("DROP TABLE IF EXISTS places"))
