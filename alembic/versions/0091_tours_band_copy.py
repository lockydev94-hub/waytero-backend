"""0091_tours_band_copy — "Popular Tour Packages" copy for the tours band

Revision ID: 0091
Revises: 0090

Doc Ref:
  Migration 0055_homepage_sections — seeds the TOUR_PACKAGES section and its
  default variant this updates;
  BRD Part 6 §155 — homepage admin control.

Why:
  The redesigned tour-packages band (customer-web
  components/renderers/TourPackagesSection.tsx) moved to a left-aligned
  heading with flanking dashes, the "View All Packages" link on the right and
  four-up white cards. The mock's copy is "Popular Tour Packages" / "Explore
  top destinations with our best-selling tour packages.", and it carries no
  eyebrow — the old "Curated experiences" tag and "Tour Packages Loved by
  Travelers" headline belong to the previous centred layout.

  Seeded here rather than hardcoded so the admin can edit the words, and so
  the editor shows the copy the site actually renders.

  Idempotent: a plain UPDATE keyed on the section, safe to re-run. Only the
  default variant of TOUR_PACKAGES is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None

_OLD = {
    "variant_tag": "Curated experiences",
    "headline": "Tour Packages Loved by Travelers",
    "subheadline": "Handpicked itineraries with verified partners and transparent pricing.",
    "cta_text": None,
    "cta_link": None,
}

_NEW = {
    "variant_tag": None,
    "headline": "Popular Tour Packages",
    "subheadline": "Explore top destinations with our best-selling tour packages.",
    "cta_text": "View All Packages",
    "cta_link": "/tours",
}

_UPDATE = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag,
        headline = :headline,
        subheadline = :subheadline,
        cta_text = :cta_text,
        cta_link = :cta_link
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'TOUR_PACKAGES'
"""


def upgrade() -> None:
    op.get_bind().execute(sa.text(_UPDATE), _NEW)


def downgrade() -> None:
    op.get_bind().execute(sa.text(_UPDATE), _OLD)
