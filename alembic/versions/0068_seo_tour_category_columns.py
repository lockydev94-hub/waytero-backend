"""add SEO columns to tour_packages and slug to category tables

Doc Ref: SEO Implementation §2 — per-entity metadata for tours & masters
Revision: 0068
DownRevision: 0067

blog_posts/hotels (0051/0056) already carry slug + seo_title/seo_description/
seo_keywords, and hotel_categories/service_types/vehicle_categories already
carry the seo_* columns (0031). Missing pieces this migration fixes:
tour_packages had NO seo_* columns, and none of the four category/master
tables had a slug — so SEO-friendly category landing pages were impossible.

Slugs are back-filled from label/name; collisions are de-duplicated by
keeping the lowest id (the rest stay NULL and get a slug when saved from
the admin, which now validates uniqueness).
"""

from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"


def _slug_expr(col: str) -> str:
    return (
        f"btrim(regexp_replace(regexp_replace(lower(coalesce({col}, '')), "
        f"'[^a-z0-9]+', '-', 'g'), '^-+|-+$', '', 'g'))"
    )


def upgrade() -> None:
    conn = op.get_bind()

    # ── tour_packages: SEO fields (slug exists + unique since 0067) ─────────
    conn.execute(
        sa.text(
            """
            ALTER TABLE tour_packages
                ADD COLUMN IF NOT EXISTS seo_title       VARCHAR(120),
                ADD COLUMN IF NOT EXISTS seo_description VARCHAR(320),
                ADD COLUMN IF NOT EXISTS seo_keywords    VARCHAR(500)
            """
        )
    )

    # ── category/master tables: slug ────────────────────────────────────────
    for table in ("hotel_categories", "service_types", "vehicle_categories"):
        conn.execute(
            sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS slug VARCHAR(120)")
        )

    # ── back-fill slugs (only where empty) ──────────────────────────────────
    conn.execute(
        sa.text(
            f"""
            UPDATE hotel_categories
               SET slug = NULLIF({_slug_expr('label')}, '')
             WHERE slug IS NULL
            """
        )
    )
    conn.execute(
        sa.text(
            f"""
            UPDATE vehicle_categories
               SET slug = NULLIF({_slug_expr('category_name')}, '')
             WHERE slug IS NULL
            """
        )
    )
    # service_types: label (e.g. "Cab") preferred, else lowercase type_code.
    conn.execute(
        sa.text(
            f"""
            UPDATE service_types
               SET slug = COALESCE(NULLIF({_slug_expr('label')}, ''),
                                   NULLIF(lower(type_code), ''))
             WHERE slug IS NULL
            """
        )
    )

    # ── de-duplicate slug collisions before adding unique indexes ───────────
    for table in ("hotel_categories", "service_types", "vehicle_categories"):
        conn.execute(
            sa.text(
                f"""
                UPDATE {table}
                   SET slug = NULL
                 WHERE slug IS NOT NULL
                   AND id NOT IN (
                       SELECT MIN(id) FROM {table}
                        WHERE slug IS NOT NULL
                        GROUP BY slug
                   )
                """
            )
        )

    # ── unique partial indexes (NULL slugs tolerated) ────────────────────────
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_hotel_categories_slug "
            "ON hotel_categories (slug) WHERE slug IS NOT NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_service_types_slug "
            "ON service_types (slug) WHERE slug IS NOT NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_vehicle_categories_slug "
            "ON vehicle_categories (slug) WHERE slug IS NOT NULL"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_hotel_categories_slug"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_service_types_slug"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uix_vehicle_categories_slug"))
    for table in ("hotel_categories", "service_types", "vehicle_categories"):
        conn.execute(
            sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS slug")
        )
    conn.execute(
        sa.text(
            """
            ALTER TABLE tour_packages
                DROP COLUMN IF EXISTS seo_title,
                DROP COLUMN IF EXISTS seo_description,
                DROP COLUMN IF EXISTS seo_keywords
            """
        )
    )
