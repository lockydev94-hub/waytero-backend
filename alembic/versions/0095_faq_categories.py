"""0095_faq_categories — file the homepage FAQs under tabs

Revision ID: 0095
Revises: 0094

Doc Ref:
  Migration 0055_homepage_sections — seeds the FAQ band and its questions,
  which this updates; BRD Part 6 §155 — homepage admin control.

Why:
  The FAQ band was redesigned to the 2026-11 mock: a category rail on the
  left filters the accordion on the right, so every question needs a
  `category`. The questions and answers are unchanged — only grouped, and
  only where a category is missing, so an admin's own tagging survives a
  re-run.

  The band also drops its eyebrow (the mock has the heading with dashes
  either side and no tag above it), so variant_tag is cleared.

  The array is rewritten as JSONB rather than patched in place because the
  categories live on the items, not the variant.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

# Question → tab. Anything unlisted lands in "General".
_CATEGORY = {
    "How do I book a cab with WayTero?": "Cab Booking",
    "How do I track my cab in real time?": "Cab Booking",
    "What payment methods are accepted?": "Payments & Refunds",
    "Can I get a refund if I cancel my booking?": "Payments & Refunds",
    "Is my data safe with WayTero?": "Safety & Support",
    "Are the hotels verified?": "Hotels & Stays",
}

_SELECT = """
    SELECT sv.id, sv.content
    FROM section_variants sv
    JOIN page_sections ps ON ps.id = sv.page_section_id
    WHERE ps.section_key = 'FAQ'
"""

_UPDATE_CONTENT = (
    "UPDATE section_variants SET content = CAST(:content AS JSONB) WHERE id = :id"
)

_SET_TAG = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'FAQ'
"""


def _rewrite(conn, add: bool) -> None:
    for row_id, content in conn.execute(sa.text(_SELECT)).all():
        content = dict(content or {})
        faqs = content.get("faqs")
        if not isinstance(faqs, list):
            continue
        for faq in faqs:
            if not isinstance(faq, dict):
                continue
            if add:
                if not faq.get("category"):
                    faq["category"] = _CATEGORY.get(faq.get("question", ""), "General")
            else:
                faq.pop("category", None)
        content["faqs"] = faqs
        conn.execute(
            sa.text(_UPDATE_CONTENT), {"id": row_id, "content": json.dumps(content)}
        )


def upgrade() -> None:
    conn = op.get_bind()
    _rewrite(conn, add=True)
    conn.execute(sa.text(_SET_TAG), {"variant_tag": None})


def downgrade() -> None:
    conn = op.get_bind()
    _rewrite(conn, add=False)
    conn.execute(sa.text(_SET_TAG), {"variant_tag": "Help centre"})
