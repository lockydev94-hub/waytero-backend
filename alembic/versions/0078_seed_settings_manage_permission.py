"""Seed the settings.manage permission used by the media library

Revision ID: 0078
Revises: 0077

``GET /admin/settings/media-library`` and
``DELETE /admin/settings/media-library/{public_id}`` are gated by
``require_permission("settings.manage")``, but that code was never inserted
into the ``permissions`` table — migration 0044 only seeded the two CMS
codes. Every role therefore received 403 "Access denied. Required
permissions: settings.manage", which left the media picker's Gallery tab
empty and its "Use this image" button permanently disabled.

Same shape as migration 0052: insert the permission, grant it to
SUPER_ADMIN and ADMIN, idempotent so it is safe on any database state.

Doc Ref: Migration 0044_website_cms (media library origin)
"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            INSERT INTO permissions (id, permission_code, permission_name, description)
            VALUES (gen_random_uuid(), 'settings.manage', 'settings.manage',
                    'Browse and delete Cloudinary assets in the media library')
            ON CONFLICT (permission_code) DO NOTHING
            """
        )
    )

    for role_code in ("SUPER_ADMIN", "ADMIN"):
        conn.execute(
            sa.text(
                """
                INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                SELECT gen_random_uuid(), r.id, p.id, NOW()
                FROM roles r, permissions p
                WHERE r.role_code = :rc AND p.permission_code = 'settings.manage'
                ON CONFLICT DO NOTHING
                """
            ),
            {"rc": role_code},
        )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE permission_code = 'settings.manage'
            )
            """
        )
    )
    conn.execute(
        sa.text("DELETE FROM permissions WHERE permission_code = 'settings.manage'")
    )
