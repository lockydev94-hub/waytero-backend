"""0080_cab_routes_cms_sections — register POPULAR_ROUTES / POPULAR_CITIES and give HERO a marquee

Revision ID: 0080
Revises: 0079

Doc Ref: migration 0079_cab_routes; BRD Part 6 §155 (homepage admin control);
shape copied from migration 0055_homepage_sections.

Why:
  The two new homepage bands need the same treatment every other section gets:
  a `page_sections` row so it appears in the CMS list, and one default active
  `section_variants` row carrying the header copy an admin can edit. Their
  content is a real table (cab_routes / cities), so no `content` JSONB is
  seeded for them — the section owns copy only.

  HERO gains `marquee_items` in its active variant's content. The merge is
  guarded by `NOT (content ? 'marquee_items')` so an admin who already edited
  the band is never overwritten on the next deploy.

  Seeded marquee items state only what the platform can back with data. The
  reference design's "50,000+ Customers / 1,000,000+ Hotels" counters are not
  true of this database and are deliberately absent — the admin authors those
  in the CMS if they ever become true.

Everything is idempotent (ON CONFLICT DO NOTHING / NOT EXISTS / key guard).
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None

NEW_SECTIONS = [
    (
        "POPULAR_ROUTES",
        "Popular Routes",
        26,
        "Inter-city cab corridors with fares from the live fare engine.",
        "routes",
    ),
    (
        "POPULAR_CITIES",
        "Popular Cities",
        27,
        "Cities with bookable supply on WayTero.",
        "cities",
    ),
]

DEFAULT_VARIANTS = [
    {
        "key": "POPULAR_ROUTES",
        "name": "Default Popular Routes",
        "tag": "Where people travel",
        "headline": "Popular Routes",
        "subheadline": "Inter-city cab fares from our live fare engine.",
        "cta_text": "View all routes",
        "cta_link": "/cabs/routes",
    },
    {
        "key": "POPULAR_CITIES",
        "name": "Default Popular Cities",
        "tag": "Where we operate",
        "headline": "Popular Cities",
        "subheadline": "Cities with cabs, hotels and tours you can book today.",
        "cta_text": None,
        "cta_link": None,
    },
]

_MARQUEE_ITEMS = [
    {"icon": "Layers", "label": "Wide Range of Options", "sublabel": "Cabs, hotels & tours"},
    {"icon": "ReceiptText", "label": "Transparent Pricing", "sublabel": "Fares from the live engine"},
    {"icon": "ShieldCheck", "label": "Secure Payments", "sublabel": "Encrypted checkout"},
    {"icon": "Store", "label": "Verified Partners", "sublabel": "Vetted supply network"},
]


def upgrade() -> None:
    conn = op.get_bind()

    for key, name, order, desc, icon in NEW_SECTIONS:
        conn.execute(
            sa.text(
                """
                INSERT INTO page_sections
                    (section_key, display_name, description, icon, display_order, is_visible, is_active)
                VALUES (:k, :n, :d, :ic, :o, TRUE, TRUE)
                ON CONFLICT (section_key) DO NOTHING
                """
            ),
            {"k": key, "n": name, "d": desc, "ic": icon, "o": order},
        )

    for v in DEFAULT_VARIANTS:
        conn.execute(
            sa.text(
                """
                INSERT INTO section_variants
                    (page_section_id, variant_name, variant_tag, display_order, is_active,
                     headline, subheadline, cta_text, cta_link, content)
                SELECT ps.id, :name, :tag, 0, TRUE, :headline, :subheadline,
                       :cta_text, :cta_link, NULL
                FROM page_sections ps
                WHERE ps.section_key = :key
                  AND NOT EXISTS (
                      SELECT 1 FROM section_variants sv WHERE sv.page_section_id = ps.id
                  )
                """
            ),
            {
                "key": v["key"],
                "name": v["name"],
                "tag": v["tag"],
                "headline": v["headline"],
                "subheadline": v["subheadline"],
                "cta_text": v["cta_text"],
                "cta_link": v["cta_link"],
            },
        )

    conn.execute(
        sa.text(
            """
            UPDATE section_variants sv
            SET content = COALESCE(sv.content, '{}'::jsonb)
                          || CAST(:items AS jsonb)
            FROM page_sections ps
            WHERE sv.page_section_id = ps.id
              AND ps.section_key = 'HERO'
              AND sv.is_active
              AND NOT (COALESCE(sv.content, '{}'::jsonb) ? 'marquee_items')
            """
        ),
        {"items": json.dumps({"marquee_items": _MARQUEE_ITEMS})},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE section_variants sv
            SET content = sv.content - 'marquee_items'
            FROM page_sections ps
            WHERE sv.page_section_id = ps.id
              AND ps.section_key = 'HERO'
              AND sv.content IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            DELETE FROM section_variants
            WHERE page_section_id IN (
                SELECT id FROM page_sections WHERE section_key IN ('POPULAR_ROUTES','POPULAR_CITIES'))
            """
        )
    )
    conn.execute(
        sa.text(
            "DELETE FROM page_sections WHERE section_key IN ('POPULAR_ROUTES','POPULAR_CITIES')"
        )
    )
