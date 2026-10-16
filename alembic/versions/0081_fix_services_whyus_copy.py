"""0081_fix_services_whyus_copy — put the real headline back on two homepage bands

Revision ID: 0081
Revises: 0080

Doc Ref: migration 0055_homepage_sections (the seed copy this restores);
BRD Part 6 §155 (homepage admin control).

Why:
  Two live variants carry copy that was typed into the CMS over the 0055 seed:
  SERVICES reads "Get Offer in Cab Book" for both headline and sub-heading, and
  WHY_US reads "way chooes Us". Both also show the literal word "DEFAULT" as
  their eyebrow — the section editor prefilled `variant_tag` with that
  placeholder and it is the line the renderer prints above the heading, so the
  placeholder became site copy. (The prefill itself is fixed in the admin
  portal; nothing on the site should ever render "DEFAULT".)

  These are the only two rows carrying it, confirmed against this database, so
  the repair is scoped to them.

  Guarded column by column on the exact junk, never on a whole row:
    • variant_tag is restored only where it is literally 'DEFAULT';
    • headline / subheadline are restored only where they still equal the junk
      the screenshots showed.
  So a re-run matches nothing (idempotent), an admin who has since written real
  copy is left alone, and a partially-fixed band still gets the half that is
  broken.

downgrade() does not put "way chooes Us" back on the site. It restores only the
eyebrow, and only to the placeholder it came from, which is the sole part of the
junk this migration can prove was there before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None

# section_key -> (what the eyebrow says now, the seed copy to restore)
FIXES = [
    {
        "key": "SERVICES",
        "junk": "Get Offer in Cab Book",
        "tag": "What we offer",
        "headline": "Everything for Your Journey",
        "subheadline": (
            "One platform for every travel need — from your first mile "
            "to your last check-out."
        ),
    },
    {
        "key": "WHY_US",
        "junk": "way chooes Us",
        "tag": "Why choose us",
        "headline": "Built for Indian Travelers",
        "subheadline": (
            "We understand the nuances of travel across India — "
            "and built WayTero to solve them."
        ),
    },
]

_UPGRADE = """
    UPDATE section_variants sv
    SET variant_tag = CASE WHEN sv.variant_tag = 'DEFAULT'
                           THEN :tag ELSE sv.variant_tag END,
        headline    = CASE WHEN sv.headline = :junk
                           THEN :headline ELSE sv.headline END,
        subheadline = CASE WHEN sv.subheadline = :junk
                           THEN :subheadline ELSE sv.subheadline END,
        updated_at  = NOW()
    FROM page_sections ps
    WHERE sv.page_section_id = ps.id
      AND ps.section_key = :key
      AND (
            sv.variant_tag = 'DEFAULT'
         OR sv.headline = :junk
         OR sv.subheadline = :junk
      )
"""

_DOWNGRADE = """
    UPDATE section_variants sv
    SET variant_tag = 'DEFAULT',
        updated_at  = NOW()
    FROM page_sections ps
    WHERE sv.page_section_id = ps.id
      AND ps.section_key = :key
      AND sv.variant_tag = :tag
"""


def upgrade() -> None:
    conn = op.get_bind()
    for f in FIXES:
        conn.execute(sa.text(_UPGRADE), f)


def downgrade() -> None:
    conn = op.get_bind()
    for f in FIXES:
        conn.execute(sa.text(_DOWNGRADE), {"key": f["key"], "tag": f["tag"]})
