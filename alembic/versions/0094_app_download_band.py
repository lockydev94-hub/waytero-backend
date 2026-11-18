"""0094_app_download_band — enable and re-copy the app-promotion band

Revision ID: 0094
Revises: 0093

Doc Ref:
  Migration 0055_homepage_sections — seeds APP_DOWNLOAD (display_order 92) with
  is_active = false; BRD Part 6 §155 — homepage admin control.

Why:
  The band was seeded switched off and never turned on, so the homepage has
  never rendered it. It is now part of the page and redesigned to the 2026-11
  mock: a single rounded panel with the two-phone artwork on the left, the
  claim and store badges in the middle, and the four app benefits plus the
  scan-to-download code on the right.

  Seeded here so the copy is CMS-editable and the band reads the same on every
  environment, rather than living in the renderer's defaults only.

  The two store badges are wired to Settings → Footer → app store links (the
  same `app_store_links` the footer uses). Those are still empty, so the badges
  paint but do not navigate until the owner sets them.

  Idempotent: one UPDATE on page_sections plus one on section_variants, both
  keyed on section_key, with the JSONB content merged (`||`).
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_BENEFITS = [
    {"icon": "CheckCircle2", "label": "Quick Booking"},
    {"icon": "MapPin", "label": "Real-time Tracking"},
    {"icon": "Gift", "label": "Exclusive Offers"},
    {"icon": "CalendarCheck", "label": "Manage Bookings"},
]

_SHOW = """
    UPDATE page_sections
    SET is_active = true
    WHERE section_key = 'APP_DOWNLOAD'
"""

_HIDE = """
    UPDATE page_sections
    SET is_active = false
    WHERE section_key = 'APP_DOWNLOAD'
"""

_UPDATE_COPY = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag,
        headline = :headline,
        subheadline = :subheadline,
        content = COALESCE(sv.content, '{}'::jsonb) || CAST(:content AS JSONB)
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'APP_DOWNLOAD'
"""

_DROP_ITEMS = """
    UPDATE section_variants sv
    SET content = COALESCE(sv.content, '{}'::jsonb) - 'items'
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'APP_DOWNLOAD'
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(_SHOW))
    conn.execute(
        sa.text(_UPDATE_COPY),
        {
            "variant_tag": "Get the WayTero App",
            "headline": "Book, Track & Travel on the Go",
            "subheadline": "A smarter way to book cabs and tour packages anytime, anywhere.",
            "content": json.dumps({"items": _BENEFITS}),
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(_UPDATE_COPY),
        {
            "variant_tag": "On the go",
            "headline": "Travel Smarter with the WayTero App",
            "subheadline": "Book, track and manage trips on the go — available on iOS and Android.",
            "content": "{}",
        },
    )
    conn.execute(sa.text(_DROP_ITEMS))
    conn.execute(sa.text(_HIDE))
