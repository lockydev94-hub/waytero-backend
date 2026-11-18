"""0090_ride_types_section — add the "Choose Your Ride Type" homepage band

Revision ID: 0090
Revises: 0089

Doc Ref:
  Migration 0055_homepage_sections — the page_sections / section_variants seed
  this follows;
  BRD Part 6 §155 — homepage admin control.

Why:
  The homepage design puts a row of the five cab trip types (Local Cab,
  Airport Transfer, One Way, Outstation, Round Trip) directly under the
  services band. The band is a new CMS section — `RIDE_TYPES` — so the copy
  and the cards stay editable in the admin like every other band, and the
  renderer (customer-web/src/components/renderers/RideTypesSection.tsx) can
  disappear if the admin empties it or switches the cab vertical off.

  Placement: SERVICES sits at display_order 20 and OFFERS at 22, so 21 lands
  the band exactly between them.

  Idempotent: the page_sections insert is ON CONFLICT DO NOTHING, and the
  variant is only seeded when the section has none — an admin-authored
  variant is never clobbered on a re-run.

  `content.items` holds only what the renderer reads: icon (a name its ICONS
  lookup maps), title, desc. The card link is optional and defaults to
  /cabs, so it is not seeded — five identical "/cabs" values would be noise
  the admin has to scroll past.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

SECTION = {
    "key": "RIDE_TYPES",
    "name": "Ride Types",
    "description": "Cab trip types (local, airport, one-way, outstation, round trip).",
    "icon": "car",
    "order": 21,
}

CONTENT = {
    "items": [
        {"icon": "CarFront", "title": "Local Cab", "desc": "Travel within your city"},
        {
            "icon": "Plane",
            "title": "Airport Transfer",
            "desc": "Hassle-free airport pickup & drop",
        },
        {
            "icon": "MoveRight",
            "title": "One Way",
            "desc": "Simple and affordable rides",
        },
        {
            "icon": "CircleUserRound",
            "title": "Outstation",
            "desc": "Travel to nearby cities with ease",
        },
        {
            "icon": "RefreshCw",
            "title": "Round Trip",
            "desc": "Go, explore and return comfortably",
        },
    ]
}

_INSERT_SECTION = """
    INSERT INTO page_sections
        (section_key, display_name, description, icon, display_order, is_visible, is_active)
    VALUES (:k, :n, :d, :ic, :o, TRUE, TRUE)
    ON CONFLICT (section_key) DO NOTHING
"""

_INSERT_VARIANT = """
    INSERT INTO section_variants
        (page_section_id, variant_name, variant_tag, display_order, is_active,
         headline, subheadline, cta_text, cta_link, content)
    SELECT ps.id, :name, NULL, 0, TRUE, :headline, :subheadline, NULL, NULL,
           CAST(:content AS JSONB)
    FROM page_sections ps
    WHERE ps.section_key = :key
      AND NOT EXISTS (
          SELECT 1 FROM section_variants sv WHERE sv.page_section_id = ps.id
      )
"""


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(_INSERT_SECTION),
        {
            "k": SECTION["key"],
            "n": SECTION["name"],
            "d": SECTION["description"],
            "ic": SECTION["icon"],
            "o": SECTION["order"],
        },
    )

    conn.execute(
        sa.text(_INSERT_VARIANT),
        {
            "key": SECTION["key"],
            "name": "Default Ride Types",
            "headline": "Choose Your Ride Type",
            "subheadline": "Flexible cab options for every travel need.",
            "content": json.dumps(CONTENT),
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            DELETE FROM section_variants
            WHERE page_section_id IN (
                SELECT id FROM page_sections WHERE section_key = 'RIDE_TYPES'
            )
            """
        )
    )
    conn.execute(sa.text("DELETE FROM page_sections WHERE section_key = 'RIDE_TYPES'"))
