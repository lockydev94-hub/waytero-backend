"""Seed the blog.manage permission for the admin blog system

Revision ID: 0052
Revises: 0051

Migration 0051 created the ``blog_posts`` table and the admin blog
endpoints were written against the ``blog.manage`` permission, but that
permission was never inserted into the ``permissions`` table (only the
CMS permissions ``cms.section.manage`` / ``cms.header_footer.manage``
were seeded by migration 0044). As a result every admin blog endpoint
that is gated by ``require_permission("blog.manage")`` returned 403 for
all roles.

This migration seeds the permission and grants it to SUPER_ADMIN and
ADMIN, mirroring the pattern used in migration 0044. It is written
idempotently (ON CONFLICT DO NOTHING) so it is safe to run on any
database state.

Doc Ref: Blog System §2 — Admin API
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Insert the permission code (idempotent).
    conn.execute(
        sa.text(
            """
            INSERT INTO permissions (id, permission_code, permission_name, description)
            VALUES (gen_random_uuid(), 'blog.manage', 'blog.manage',
                    'Create / edit / publish / delete blog posts')
            ON CONFLICT (permission_code) DO NOTHING
            """
        )
    )

    # 2. Grant to SUPER_ADMIN + ADMIN (idempotent).
    for role_code in ("SUPER_ADMIN", "ADMIN"):
        conn.execute(
            sa.text(
                """
                INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                SELECT gen_random_uuid(), r.id, p.id, NOW()
                FROM roles r, permissions p
                WHERE r.role_code = :rc AND p.permission_code = 'blog.manage'
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
                SELECT id FROM permissions WHERE permission_code = 'blog.manage'
            )
            """
        )
    )
    conn.execute(
        sa.text("DELETE FROM permissions WHERE permission_code = 'blog.manage'")
    )
