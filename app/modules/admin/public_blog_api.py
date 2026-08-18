# ============================================================
# WAYTERO — PUBLIC BLOG API
# File: app/modules/admin/public_blog_api.py
# Doc Ref: Blog System §3 — Public API
# Prefix: /public/blog  (registered in api/router.py)
#
# Public-facing blog endpoints (no auth required):
#   GET    /public/blog              — list published posts
#   GET    /public/blog/{slug}       — get single published post by slug
#   GET    /public/blog/tags         — list all tags in use
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ResourceNotFoundException
from app.modules.admin.schemas import BlogPostOut, BlogPostListOut
from app.modules.admin.services import BlogService

router = APIRouter()


@router.get(
    "/blog",
    response_model=dict,
    tags=["Public Blog"],
    summary="List published blog posts",
)
async def list_public_blog_posts(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(12, ge=1, le=50, description="Items per page"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    search: Optional[str] = Query(None, description="Search title/excerpt"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * per_page
    posts = await BlogService.list_posts(
        db,
        published_only=True,
        search=search,
        tag=tag,
        limit=per_page,
        offset=offset,
    )
    total = await BlogService.count_posts(
        db, published_only=True, search=search, tag=tag
    )
    return {
        "success": True,
        "data": [BlogPostListOut.model_validate(p) for p in posts],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get(
    "/blog/{slug}",
    response_model=dict,
    tags=["Public Blog"],
    summary="Get a single published blog post by slug",
)
async def get_public_blog_post(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    post = await BlogService.get_by_slug(db, slug)
    if not post or not post.is_published:
        raise ResourceNotFoundException("Blog post", slug)
    return {"success": True, "data": BlogPostOut.model_validate(post)}


@router.get(
    "/blog/tags",
    response_model=dict,
    tags=["Public Blog"],
    summary="List all tags in use across published posts",
)
async def list_blog_tags(
    db: AsyncSession = Depends(get_db),
):
    """Returns a deduplicated list of all tags used on published posts."""
    from sqlalchemy import text

    result = await db.execute(
        text(
            """
            SELECT DISTINCT jsonb_array_elements_text(tags) AS tag
            FROM blog_posts
            WHERE is_published = true
            ORDER BY tag
        """
        )
    )
    tags = [row[0] for row in result.all()]
    return {"success": True, "data": tags}
