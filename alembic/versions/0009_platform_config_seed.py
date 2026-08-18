"""Platform config seed — create api_integrations table, seed platform details + 3rd-party integrations

Revision ID: 0009_platform_config_seed
Revises: 0008_phase3_finance
Create Date: 2026-07-28

Doc Ref:
  DB Schema Part 1 §12 — system_configurations (platform, timezone, currency keys)
  DB Schema Part 1 §14 — api_integrations table + seed rows (CLOUDINARY, FIREBASE, GOOGLE_MAPS)
  Admin API §25 — System Configuration
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_platform_config_seed"
down_revision = "0008_phase3_finance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Create api_integrations table (DB Schema Part 1 §14) ─────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS api_integrations (
            id           BIGSERIAL PRIMARY KEY,
            service_name VARCHAR(100),
            service_type VARCHAR(100),
            configuration JSONB,
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    # ── Platform details / timezone / currency into system_configurations ─────
    conn.execute(sa.text("""
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('PLATFORM_NAME',            'WayTero',             'Display name of the platform',              NOW()),
            ('PLATFORM_TAGLINE',         'Your Way, Our Route', 'Platform tagline shown in apps',            NOW()),
            ('PLATFORM_TIMEZONE',        'Asia/Kolkata',        'Default timezone for the platform (IANA)',  NOW()),
            ('PLATFORM_CURRENCY',        'INR',                 'ISO 4217 currency code used platform-wide', NOW()),
            ('PLATFORM_CURRENCY_SYMBOL', '₹',                  'Currency symbol shown in UI',               NOW()),
            ('PLATFORM_COUNTRY',         'IN',                  'ISO 3166-1 alpha-2 country code',           NOW()),
            ('PLATFORM_LOCALE',          'en-IN',               'Default locale for formatting',             NOW()),
            ('SUPPORT_EMAIL',            '',                    'Platform support email address',            NOW()),
            ('SUPPORT_PHONE',            '',                    'Platform support phone number',             NOW())
        ON CONFLICT (config_key) DO NOTHING
    """))

    # ── Seed Cloudinary, Firebase, Google Maps placeholder rows ───────────────
    conn.execute(sa.text("""
        INSERT INTO api_integrations (service_name, service_type, configuration, is_active, created_at)
        VALUES
            ('Cloudinary',  'CLOUDINARY',   '{"cloud_name":"","api_key":"","api_secret":"","upload_preset":""}',                                                                        false, NOW()),
            ('Firebase',    'FIREBASE',     '{"project_id":"","private_key_id":"","private_key":"","client_email":"","web_api_key":"","storage_bucket":""}',  false, NOW()),
            ('Google Maps', 'GOOGLE_MAPS',  '{"api_key":"","map_id":""}',                                                                                                               false, NOW())
    """))


def downgrade() -> None:
    conn = op.get_bind()

    # Remove seeded api_integrations rows
    conn.execute(sa.text(
        "DELETE FROM api_integrations WHERE service_type IN ('CLOUDINARY','FIREBASE','GOOGLE_MAPS')"
    ))

    # Drop table only if it was created by this migration (check row count first)
    result = conn.execute(sa.text("SELECT COUNT(*) FROM api_integrations"))
    if result.scalar() == 0:
        conn.execute(sa.text("DROP TABLE IF EXISTS api_integrations"))

    # Remove seeded system_configuration keys
    conn.execute(sa.text("""
        DELETE FROM system_configurations WHERE config_key IN (
            'PLATFORM_NAME','PLATFORM_TAGLINE','PLATFORM_TIMEZONE',
            'PLATFORM_CURRENCY','PLATFORM_CURRENCY_SYMBOL','PLATFORM_COUNTRY',
            'PLATFORM_LOCALE','SUPPORT_EMAIL','SUPPORT_PHONE'
        )
    """))
