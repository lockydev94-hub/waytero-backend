"""create landing_pages — city / state × service-type marketing pages

Doc Ref: City + Service-Type Landing Pages §Phase 1 (plan: wise-moor-shrew)
Revision: 0069
DownRevision: 0068

Before this migration the website's city pages were TSX templates: every
word of copy was interpolated from the city name, so nothing was editable
from the admin, and cabs had no city dimension at all. The `cities` table is
a bare FK target (no slug, no image, no SEO columns), and the existing CMS
(`page_sections` / `section_variants`) is homepage-only with no route key.

`landing_pages` is one row per (page_type × scope) and follows the
`section_variants` precedent for typed content slots + JSONB, and the
`blog_posts` precedent for ORM-side `onupdate` timestamps (no DB trigger).

Slugs are provisioned here for every active city and state so the pages
exist as soon as the migration runs — but they ship UNPUBLISHED with NULL
copy. The service renders derived placeholder copy for NULL slots (and
falls back to curated/gradient imagery), so an unpublished-but-provisioned
row is harmless, while an authored row is never clobbered: every INSERT is
guarded by NOT EXISTS on (page_type, scope) and ON CONFLICT DO NOTHING.

Naming caveat worth knowing: a city and its state can produce the same slug
(e.g. Delhi city / Delhi state → `hotel-booking-in-delhi`). The city row is
inserted first and wins; the state row is skipped rather than published under
a mangled slug. Admin can create that page explicitly if it's wanted.
"""

from alembic import op
import sqlalchemy as sa

revision = "0069"
down_revision = "0068"


# Same normalisation the rest of the platform uses (migration 0068,
# app/shared/utilities/seo.py, Frontend/*/slugify): lower → non-alnum to
# dash → strip edge dashes. Kept inline because this is raw SQL over a
# derived column, not an ORM value.
def _slug_expr(col: str) -> str:
    return (
        f"btrim(regexp_replace(regexp_replace(lower(coalesce({col}, '')), "
        f"'[^a-z0-9]+', '-', 'g'), '^-+|-+$', '', 'g'))"
    )


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS landing_pages (
                id                  BIGSERIAL    PRIMARY KEY,
                slug                VARCHAR(120) NOT NULL,
                page_type           VARCHAR(30)  NOT NULL,
                city_id             BIGINT       REFERENCES cities(id) ON DELETE CASCADE,
                state_id            BIGINT       REFERENCES states(id) ON DELETE CASCADE,
                service_type        VARCHAR(20),
                display_name        VARCHAR(150) NOT NULL,

                -- Copy slots. NULL means "render the derived template", not
                -- "show nothing" — the service fills these from the scope
                -- name, service_types.label and live counts.
                eyebrow             VARCHAR(100),
                headline            VARCHAR(300),
                subheadline         VARCHAR(500),
                body_text           TEXT,

                -- Banner / social imagery
                banner_image_url    TEXT,
                banner_mobile_url   TEXT,
                banner_video_url    TEXT,
                banner_alt_text     VARCHAR(150),
                og_image_url        TEXT,
                overlay_opacity     NUMERIC(3, 2),

                -- Structured content
                gallery             JSONB NOT NULL DEFAULT '[]'::jsonb,
                highlights          JSONB NOT NULL DEFAULT '[]'::jsonb,
                stats               JSONB NOT NULL DEFAULT '[]'::jsonb,
                blocks              JSONB NOT NULL DEFAULT '[]'::jsonb,
                faqs                JSONB NOT NULL DEFAULT '[]'::jsonb,

                cta_text            VARCHAR(100),
                cta_link            VARCHAR(500),
                cta2_text           VARCHAR(100),
                cta2_link           VARCHAR(500),

                seo_title           VARCHAR(120),
                seo_description     VARCHAR(320),
                seo_keywords        VARCHAR(500),

                is_published        BOOLEAN NOT NULL DEFAULT FALSE,
                starts_at           TIMESTAMPTZ,
                ends_at             TIMESTAMPTZ,
                display_order       INTEGER NOT NULL DEFAULT 0,

                created_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
                updated_by_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

                CONSTRAINT chk_landing_pages_page_type CHECK (
                    page_type IN (
                        'CITY_HUB', 'CITY_CABS', 'CITY_HOTELS', 'CITY_TOURS',
                        'STATE_HOTELS', 'STATE_TOURS'
                    )
                ),
                -- Scope and page_type agree: no city row can claim a state
                -- page (and the reverse). A state-scope CAB page is not a
                -- legal combination at all — cab fares are priced per city,
                -- so there would be no honest number to show.
                CONSTRAINT chk_landing_pages_scope CHECK (
                    (page_type IN ('CITY_HUB', 'CITY_CABS', 'CITY_HOTELS', 'CITY_TOURS')
                        AND city_id IS NOT NULL AND state_id IS NULL)
                    OR
                    (page_type IN ('STATE_HOTELS', 'STATE_TOURS')
                        AND state_id IS NOT NULL AND city_id IS NULL)
                ),
                CONSTRAINT chk_landing_pages_service CHECK (
                    (page_type = 'CITY_HUB'    AND service_type IS NULL)
                    OR (page_type = 'CITY_CABS'    AND service_type = 'CAB')
                    OR (page_type = 'CITY_HOTELS'  AND service_type = 'HOTEL')
                    OR (page_type = 'CITY_TOURS'   AND service_type = 'TOUR')
                    OR (page_type = 'STATE_HOTELS' AND service_type = 'HOTEL')
                    OR (page_type = 'STATE_TOURS'  AND service_type = 'TOUR')
                ),
                CONSTRAINT chk_landing_pages_overlay CHECK (
                    overlay_opacity IS NULL
                    OR (overlay_opacity >= 0 AND overlay_opacity <= 1)
                )
            )
            """
        )
    )

    # The slug IS the whole URL segment on the website's [slug] engine, so it
    # is globally unique across page_types (see the Delhi caveat above).
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_landing_pages_slug "
            "ON landing_pages (slug)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_landing_pages_city_type "
            "ON landing_pages (page_type, city_id) WHERE city_id IS NOT NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_landing_pages_state_type "
            "ON landing_pages (page_type, state_id) WHERE state_id IS NOT NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_landing_pages_public "
            "ON landing_pages (is_published, page_type, display_order)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_landing_pages_city "
            "ON landing_pages (city_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_landing_pages_state "
            "ON landing_pages (state_id)"
        )
    )

    # ── Provision: 4 families per active city ───────────────────────────────
    conn.execute(
        sa.text(
            f"""
            INSERT INTO landing_pages
                (slug, page_type, city_id, service_type, display_name,
                 is_published, display_order, created_at, updated_at)
            SELECT
                CASE t.page_type
                    WHEN 'CITY_HUB'    THEN c.slug
                    WHEN 'CITY_CABS'   THEN 'cab-booking-in-'    || c.slug
                    WHEN 'CITY_HOTELS' THEN 'hotel-booking-in-'  || c.slug
                    WHEN 'CITY_TOURS'  THEN 'tour-packages-in-'  || c.slug
                END,
                t.page_type,
                c.id,
                t.service_type,
                c.name,
                FALSE,
                t.display_order,
                now(),
                now()
              FROM (
                SELECT id, name, {_slug_expr('name')} AS slug
                  FROM cities
                 WHERE coalesce(is_active, FALSE) IS TRUE
              ) c
              CROSS JOIN (VALUES
                    ('CITY_HUB',    NULL::varchar, 0),
                    ('CITY_CABS',   'CAB',         1),
                    ('CITY_HOTELS', 'HOTEL',       2),
                    ('CITY_TOURS',  'TOUR',        3)
              ) AS t(page_type, service_type, display_order)
             WHERE c.slug <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM landing_pages x
                    WHERE x.page_type = t.page_type AND x.city_id = c.id
               )
            ON CONFLICT DO NOTHING
            """
        )
    )

    # ── Provision: 2 families per active state ──────────────────────────────
    conn.execute(
        sa.text(
            f"""
            INSERT INTO landing_pages
                (slug, page_type, state_id, service_type, display_name,
                 is_published, display_order, created_at, updated_at)
            SELECT
                CASE t.page_type
                    WHEN 'STATE_HOTELS' THEN 'hotel-booking-in-' || s.slug
                    WHEN 'STATE_TOURS'  THEN 'tour-packages-in-' || s.slug
                END,
                t.page_type,
                s.id,
                t.service_type,
                s.name,
                FALSE,
                t.display_order,
                now(),
                now()
              FROM (
                SELECT id, name, {_slug_expr('name')} AS slug
                  FROM states
                 WHERE coalesce(is_active, FALSE) IS TRUE
              ) s
              CROSS JOIN (VALUES
                    ('STATE_HOTELS', 'HOTEL', 1),
                    ('STATE_TOURS',  'TOUR',  2)
              ) AS t(page_type, service_type, display_order)
             WHERE s.slug <> ''
               AND NOT EXISTS (
                   SELECT 1 FROM landing_pages x
                    WHERE x.page_type = t.page_type AND x.state_id = s.id
               )
            ON CONFLICT DO NOTHING
            """
        )
    )

    # ── Publish the showcase set (the city/state the owner named) ───────────
    conn.execute(
        sa.text(
            """
            UPDATE landing_pages
               SET is_published = TRUE,
                   updated_at   = now()
             WHERE slug IN (
                'bhubaneswar',
                'cab-booking-in-bhubaneswar',
                'hotel-booking-in-bhubaneswar',
                'tour-packages-in-bhubaneswar',
                'tour-packages-in-odisha',
                'hotel-booking-in-odisha'
             )
               AND is_published IS FALSE
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS landing_pages"))
