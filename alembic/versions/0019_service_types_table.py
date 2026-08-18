"""Service Types — dynamic DB table with media & SEO

Revision ID: 0019_service_types_table
Revises: 0018_vehicle_category_media_seo
Create Date: 2026-07-30

Creates service_types table:
  type_code       — CAB | HOTEL | TOUR (unique, immutable key)
  label           — Display label (e.g. "Cab Booking")
  description     — Short description for admin & website
  icon_url        — Cloudinary icon URL
  image_url       — Cloudinary hero image URL
  seo_title       — <title> override for website service pages
  seo_description — <meta description>
  seo_keywords    — comma-separated keywords
  display_order   — sort order on website
  is_active       — visibility toggle
  created_at      — row creation timestamp

Seeds:
  CAB, HOTEL, TOUR with sensible defaults.
  ON CONFLICT DO NOTHING — safe on restart.
"""

import sqlalchemy as sa
from alembic import op
from datetime import datetime, timezone

revision = "0019_service_types_table"
down_revision = "0018_vehicle_category_media_seo"
branch_labels = None
depends_on = None

_NOW = datetime.now(timezone.utc).isoformat()


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS service_types (
            id              BIGSERIAL PRIMARY KEY,
            type_code       VARCHAR(50)  NOT NULL UNIQUE,
            label           VARCHAR(150) NOT NULL,
            description     TEXT,
            icon_url        TEXT,
            image_url       TEXT,
            seo_title       VARCHAR(120),
            seo_description VARCHAR(320),
            seo_keywords    VARCHAR(500),
            display_order   INTEGER NOT NULL DEFAULT 0,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_service_types_code
            ON service_types (type_code)
    """))

    # Seed default rows — idempotent
    conn.execute(sa.text("""
        INSERT INTO service_types
            (type_code, label, description, display_order, is_active, created_at)
        VALUES
            ('CAB',   'Cab Booking',   'Cab and vehicle ride bookings powered by vehicle categories', 0, TRUE, NOW()),
            ('HOTEL', 'Hotel Booking', 'Hotel and accommodation bookings for travellers',             1, TRUE, NOW()),
            ('TOUR',  'Tour Package',  'Tour packages and guided trip bookings',                       2, TRUE, NOW())
        ON CONFLICT (type_code) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS service_types CASCADE"))
