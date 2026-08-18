# ============================================================
# WAYTERO — BLOG SYSTEM TESTS
# File: tests/unit/test_blog.py
# Doc Ref: Blog System §1–§4
#
# Covers the DB-independent parts of the blog system:
#   • Pydantic schema validation (slug pattern, length limits)
#   • BlogService filter-building (published / draft / tag / search)
# ============================================================

import pytest
from pydantic import ValidationError

from app.modules.admin.schemas import BlogPostCreate, BlogPostUpdate
from app.modules.admin.services import BlogService


# ── Schema validation ─────────────────────────────────────────────────────────


def test_blog_post_create_accepts_valid_slug() -> None:
    payload = BlogPostCreate(
        title="Best hill stations in India",
        slug="best-hill-stations-in-india",
        tags=["travel", "hill-stations"],
    )

    assert payload.slug == "best-hill-stations-in-india"
    assert payload.tags == ["travel", "hill-stations"]
    assert payload.is_published is False


def test_blog_post_create_rejects_invalid_slug() -> None:
    for bad in ("Best Hill Stations", "my_slug", "slug!", "UPPER", "trailing-"):
        with pytest.raises(ValidationError):
            BlogPostCreate(title="T", slug=bad)


def test_blog_post_create_rejects_overlong_title() -> None:
    with pytest.raises(ValidationError):
        BlogPostCreate(title="a" * 301, slug="a" * 301)


def test_blog_post_update_allows_partial_payload() -> None:
    payload = BlogPostUpdate(slug="only-slug-updated")

    assert payload.slug == "only-slug-updated"
    assert payload.title is None
    assert payload.is_published is None


# ── Service filter building (no DB) ───────────────────────────────────────────


def _sql(query) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def test_list_posts_published_only_builds_where_clause() -> None:
    q = BlogService._apply_filters(
        __import__("sqlalchemy").select(__import__("sqlalchemy").literal(1)),
        published_only=True,
    )
    sql = _sql(q)

    assert "is_published" in sql
    assert "true" in sql.lower()


def test_list_posts_draft_filter_targets_unpublished() -> None:
    q = BlogService._apply_filters(
        __import__("sqlalchemy").select(__import__("sqlalchemy").literal(1)),
        status="draft",
    )
    sql = _sql(q)

    # draft → is_published = false
    assert "is_published" in sql
    assert "false" in sql.lower()


def test_list_posts_tag_filter_builds_contains() -> None:
    q = BlogService._apply_filters(
        __import__("sqlalchemy").select(__import__("sqlalchemy").literal(1)),
        tag="hill-stations",
    )
    # JSONB values can't be rendered with literal_binds — use the default
    # compile so the contains() operator survives as "@>".
    sql = str(q.compile())

    assert "tags" in sql
    assert "@>" in sql


def test_list_posts_search_targets_title_and_excerpt() -> None:
    q = BlogService._apply_filters(
        __import__("sqlalchemy").select(__import__("sqlalchemy").literal(1)),
        search="monsoon",
    )
    sql = _sql(q).lower()

    assert "title" in sql
    assert "excerpt" in sql
    assert "monsoon" in sql
