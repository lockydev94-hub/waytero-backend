"""Platform profile seed — logo, favicon, OG image, office addresses, business/legal details

Revision ID: 0013_platform_profile
Revises: 0012_notification_tables
Create Date: 2026-07-28

Doc Ref:
  DB Schema Part 1 §12 — system_configurations (platform identity keys)
  Admin API §25 — System Configuration
  BRD §204  — GST Configuration

Seeds:
  PLATFORM_LOGO_URL          — Cloudinary URL of platform logo (uploaded via /admin/settings/upload-media)
  PLATFORM_FAVICON_URL       — Cloudinary URL of favicon
  PLATFORM_OG_IMAGE_URL      — Cloudinary URL of Open Graph image
  PLATFORM_OFFICE_ADDRESSES  — JSON array of office address objects
  BUSINESS_LEGAL_NAME        — Full registered legal name of the company
  BUSINESS_GST_NUMBER        — GSTIN (Goods & Services Tax Identification Number)
  BUSINESS_PAN_NUMBER        — PAN of the company
  BUSINESS_REGISTRATION_NUMBER — CIN / company registration number
  BUSINESS_INCORPORATION_DATE  — Date of incorporation (YYYY-MM-DD string)
  BUSINESS_REGISTERED_ADDRESS  — Official registered address (string)

These keys integrate with the existing system_configurations table created in 0003.
All ON CONFLICT DO NOTHING so re-running is safe when backend restarts.

Note on PLATFORM_OFFICE_ADDRESSES:
  Stored as a JSON array. Each item has the shape:
    { "label": "Head Office", "line1": "...", "line2": "...",
      "city": "...", "state": "...", "pincode": "...",
      "phone": "...", "is_primary": true }
  (The schema comment is kept here in the Python docstring, not inside
   the SQL string, to avoid SQLAlchemy mistaking ':true' for a bind param.)
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_platform_profile"
down_revision = "0012_notification_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # NOTE: SQL comments have been intentionally omitted from this sa.text() block.
    # SQLAlchemy's text() parser treats any ":word" token as a bind parameter,
    # even inside -- comments. The schema for PLATFORM_OFFICE_ADDRESSES is
    # documented in this file's module docstring above.
    conn.execute(sa.text("""
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('PLATFORM_LOGO_URL',            '',   'Cloudinary URL of the platform logo (PNG/SVG, 400x120 recommended)',                                           NOW()),
            ('PLATFORM_FAVICON_URL',         '',   'Cloudinary URL of the platform favicon (ICO/PNG, 32x32)',                                                      NOW()),
            ('PLATFORM_OG_IMAGE_URL',        '',   'Cloudinary URL of the Open Graph / social share image (1200x630)',                                             NOW()),
            ('PLATFORM_OFFICE_ADDRESSES',    '[]', 'JSON array of office address objects (label, line1, line2, city, state, pincode, phone, is_primary)',           NOW()),
            ('BUSINESS_LEGAL_NAME',          '',   'Full registered legal name of the company',                                                                    NOW()),
            ('BUSINESS_GST_NUMBER',          '',   'GSTIN — 15-digit Goods and Services Tax Identification Number',                                                NOW()),
            ('BUSINESS_PAN_NUMBER',          '',   'PAN of the company (10 alphanumeric characters)',                                                              NOW()),
            ('BUSINESS_REGISTRATION_NUMBER', '',   'CIN / Company Identification Number from MCA',                                                                 NOW()),
            ('BUSINESS_INCORPORATION_DATE',  '',   'Date of incorporation in YYYY-MM-DD format',                                                                   NOW()),
            ('BUSINESS_REGISTERED_ADDRESS',  '',   'Official registered address of the company (as on MCA records)',                                               NOW())
        ON CONFLICT (config_key) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        DELETE FROM system_configurations WHERE config_key IN (
            'PLATFORM_LOGO_URL',
            'PLATFORM_FAVICON_URL',
            'PLATFORM_OG_IMAGE_URL',
            'PLATFORM_OFFICE_ADDRESSES',
            'BUSINESS_LEGAL_NAME',
            'BUSINESS_GST_NUMBER',
            'BUSINESS_PAN_NUMBER',
            'BUSINESS_REGISTRATION_NUMBER',
            'BUSINESS_INCORPORATION_DATE',
            'BUSINESS_REGISTERED_ADDRESS'
        )
    """))
