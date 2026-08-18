"""Add app_versions table

Revision ID: 0010_app_versions_table
Revises: 0009_platform_config_seed
Create Date: 2026-07-28

Doc Ref: DB Schema Part 1, Section 13 — app_versions table
Admin API §25 — App Version Management
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_app_versions_table"
down_revision = "0009_platform_config_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── Create app_versions table (DB Schema Part 1 §13) ─────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS app_versions (
            id              BIGSERIAL PRIMARY KEY,
            platform        VARCHAR(50),
            version         VARCHAR(50),
            is_force_update BOOLEAN NOT NULL DEFAULT FALSE,
            release_notes   TEXT,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    # ── Index for fast platform lookups ──────────────────────────────────────
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_app_versions_platform
        ON app_versions (platform, created_at DESC)
    """))

    # ── Seed initial placeholder rows for ANDROID and IOS ────────────────────
    conn.execute(sa.text("""
        INSERT INTO app_versions (platform, version, is_force_update, release_notes, created_at)
        VALUES
            ('ANDROID', '1.0.0', false, 'Initial release', NOW()),
            ('IOS',     '1.0.0', false, 'Initial release', NOW())
        ON CONFLICT DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("DROP INDEX IF EXISTS ix_app_versions_platform"))
    conn.execute(sa.text("DROP TABLE IF EXISTS app_versions"))
