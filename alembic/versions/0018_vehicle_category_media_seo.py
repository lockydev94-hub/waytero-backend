"""Vehicle Category — Media & SEO fields

Revision ID: 0018_vehicle_category_media_seo
Revises: 0017_vehicle_verification
Create Date: 2026-07-30

Adds to vehicle_categories:
  image_url       — hero/listing image for the website (Cloudinary URL)
  icon_url        — small icon used in cards / filters (Cloudinary URL)
  seo_title       — <title> tag override for website category pages
  seo_description — <meta name="description"> content
  seo_keywords    — comma-separated keywords for <meta name="keywords">
  display_order   — controls sort order on website category listing

Seeding: No default rows. Safe on restart (ALTER … IF NOT EXISTS).
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_vehicle_category_media_seo"
down_revision = "0017_vehicle_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        ALTER TABLE vehicle_categories
            ADD COLUMN IF NOT EXISTS image_url       TEXT,
            ADD COLUMN IF NOT EXISTS icon_url        TEXT,
            ADD COLUMN IF NOT EXISTS seo_title       VARCHAR(120),
            ADD COLUMN IF NOT EXISTS seo_description VARCHAR(320),
            ADD COLUMN IF NOT EXISTS seo_keywords    VARCHAR(500),
            ADD COLUMN IF NOT EXISTS display_order   INTEGER NOT NULL DEFAULT 0
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        ALTER TABLE vehicle_categories
            DROP COLUMN IF EXISTS image_url,
            DROP COLUMN IF EXISTS icon_url,
            DROP COLUMN IF EXISTS seo_title,
            DROP COLUMN IF EXISTS seo_description,
            DROP COLUMN IF EXISTS seo_keywords,
            DROP COLUMN IF EXISTS display_order
    """))
