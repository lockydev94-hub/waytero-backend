"""0096_partner_cta_copy — homepage closing band becomes partner recruitment

Revision ID: 0096
Revises: 0095

Doc Ref:
  Migration 0055_homepage_sections — seeds the CTA band and its default
  variant, which this updates; BRD Part 6 §155 — homepage admin control.

Why:
  The homepage now closes on "Become a WayTero Partner" instead of the generic
  "Ready for your next journey?" panel: the new PartnerCtaSection renders the
  partner artwork and a Register call to action. The band's copy is CMS copy,
  so it is seeded here rather than baked into the renderer.

  Only the homepage variant changes — CtaSection (still used by about,
  careers, cabs, hotels, tours and the rest) reads its own page's variant and
  is untouched. variant_tag is cleared because the design has no eyebrow.

  Idempotent: a single UPDATE keyed on section_key.
"""

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

_UPDATE = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag,
        headline = :headline,
        subheadline = :subheadline,
        cta_text = :cta_text,
        cta_link = :cta_link
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'CTA'
      AND ps.display_order = 80
"""


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(_UPDATE),
        {
            "variant_tag": None,
            "headline": "Become a WayTero Partner",
            "subheadline": (
                "Grow your business with India's growing travel platform. "
                "Join as a driver, fleet owner or tour operator."
            ),
            "cta_text": "Register as Partner",
            "cta_link": "/partner",
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(_UPDATE),
        {
            "variant_tag": "Ready when you are",
            "headline": "Ready for your next journey?",
            "subheadline": "Book cabs, hotels and tours with upfront, GST-inclusive pricing.",
            "cta_text": "Start Booking",
            "cta_link": "/cabs",
        },
    )
