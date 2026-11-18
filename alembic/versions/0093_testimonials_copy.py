"""0093_testimonials_copy — "What Our Travelers Say" band copy

Revision ID: 0093
Revises: 0092

Doc Ref:
  Migration 0055_homepage_sections — seeds TESTIMONIALS plus its default
  variant, which this updates; BRD Part 6 §155 — homepage admin control.

Why:
  The testimonials band was redesigned to the 2026-11 mock: the heading, the
  aggregate stars and the prev/next controls now sit in a left column rather
  than a centred header over the cards. That composition has no eyebrow, so
  variant_tag is cleared, and the heading/subtitle are the mock's wording.

  The review cards themselves are untouched — they are real seeded reviews,
  not mock copy. Idempotent: plain UPDATE keyed on section_key.
"""

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

_UPDATE = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag,
        headline = :headline,
        subheadline = :subheadline
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'TESTIMONIALS'
"""


def _set(conn, variant_tag, headline, subheadline):
    conn.execute(
        sa.text(_UPDATE),
        {"variant_tag": variant_tag, "headline": headline, "subheadline": subheadline},
    )


def upgrade() -> None:
    _set(
        op.get_bind(),
        None,
        "What Our Travelers Say",
        "Real experiences from happy customers.",
    )


def downgrade() -> None:
    _set(op.get_bind(), "Loved by travelers", "What Our Customers Say", None)
