"""Blog posts table — full CRUD for the WayTero blog system

Revision ID: 0051
Revises: 0050

Creates the ``blog_posts`` table with:
  - title, slug (unique), excerpt, content (rich text / HTML)
  - featured_image_url, author_name, author_avatar_url
  - tags (JSONB), is_published, published_at
  - seo_title, seo_description, seo_keywords
  - created_by (FK → users), created_at, updated_at

Also creates a GIN index on tags for tag-based queries and a
unique index on slug for fast lookups.

Doc Ref: Blog System §1 — Database Schema
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blog_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("slug", sa.String(300), nullable=False, unique=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("featured_image_url", sa.Text(), nullable=True),
        sa.Column("author_name", sa.String(150), nullable=True),
        sa.Column("author_avatar_url", sa.Text(), nullable=True),
        sa.Column("tags", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seo_title", sa.String(300), nullable=True),
        sa.Column("seo_description", sa.String(500), nullable=True),
        sa.Column("seo_keywords", sa.String(500), nullable=True),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # GIN index for tag-based queries
    op.create_index("idx_blog_posts_tags", "blog_posts", ["tags"], postgresql_using="gin")
    # Index for published + date ordering (the most common query)
    op.create_index(
        "idx_blog_posts_published_date",
        "blog_posts",
        ["is_published", sa.text("published_at DESC NULLS LAST")],
        postgresql_where=sa.text("is_published = true"),
    )
    # Index for slug lookups (already covered by unique constraint, but
    # an explicit index helps with FK-like joins)
    op.create_index("idx_blog_posts_slug", "blog_posts", ["slug"])


def downgrade() -> None:
    op.drop_index("idx_blog_posts_published_date", table_name="blog_posts")
    op.drop_index("idx_blog_posts_tags", table_name="blog_posts")
    op.drop_index("idx_blog_posts_slug", table_name="blog_posts")
    op.drop_table("blog_posts")