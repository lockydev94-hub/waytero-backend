# ============================================================
# WAYTERO — ADMIN BLOG API
# File: app/modules/admin/blog_api.py
# Doc Ref: Blog System §2 — Admin API
# Prefix: /admin/settings/blog  (registered in api/router.py)
#
# Full CRUD for blog posts:
#   GET    /admin/settings/blog          — list posts (paginated, filterable)
#   GET    /admin/settings/blog/tags     — list all tags in use
#   GET    /admin/settings/blog/{id}     — get single post
#   POST   /admin/settings/blog          — create post
#   PATCH  /admin/settings/blog/{id}     — update post
#   DELETE /admin/settings/blog/{id}     — delete post
#   POST   /admin/settings/blog/{id}/publish   — toggle publish
# ============================================================

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import (
    DuplicateResourceException,
    ResourceNotFoundException,
)
from app.modules.admin.schemas import (
    BlogPostOut,
    BlogPostListOut,
    BlogPostCreate,
    BlogPostUpdate,
)
from app.modules.admin.services import BlogService

router = APIRouter()


# ── List posts ────────────────────────────────────────────────────────────────


@router.get(
    "/blog",
    response_model=dict,
    tags=["Blog"],
    summary="List blog posts (paginated)",
)
async def list_blog_posts(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(None, description="Search title/excerpt"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    status: Optional[str] = Query(
        None, description="Filter by publish state: all | published | draft"
    ),
    published_only: bool = Query(False, description="Only published posts"),
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("blog.manage")),
):
    offset = (page - 1) * per_page
    posts = await BlogService.list_posts(
        db,
        published_only=published_only,
        status=status,
        search=search,
        tag=tag,
        limit=per_page,
        offset=offset,
    )
    total = await BlogService.count_posts(
        db, published_only=published_only, status=status, search=search, tag=tag
    )
    return {
        "success": True,
        "data": [BlogPostListOut.model_validate(p) for p in posts],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# ── List tags ─────────────────────────────────────────────────────────────────


@router.get(
    "/blog/tags",
    response_model=dict,
    tags=["Blog"],
    summary="List all tags in use across blog posts",
)
async def list_blog_tags(
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("blog.manage")),
):
    tags = await BlogService.list_tags(db)
    return {"success": True, "data": tags}


# ── Get single post ───────────────────────────────────────────────────────────


@router.get(
    "/blog/{post_id}",
    response_model=dict,
    tags=["Blog"],
    summary="Get a single blog post by ID",
)
async def get_blog_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("blog.manage")),
):
    post = await BlogService.get_by_id(db, post_id)
    if not post:
        raise ResourceNotFoundException("Blog post", post_id)
    return {"success": True, "data": BlogPostOut.model_validate(post)}


# ── Create post ───────────────────────────────────────────────────────────────


@router.post(
    "/blog",
    response_model=dict,
    tags=["Blog"],
    summary="Create a new blog post",
)
async def create_blog_post(
    payload: BlogPostCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission("blog.manage")),
):
    # Check slug uniqueness
    existing = await BlogService.get_by_slug(db, payload.slug)
    if existing:
        raise DuplicateResourceException("Blog post", "slug")

    post = await BlogService.create(db, payload, created_by=str(user["sub"]))
    return {"success": True, "data": BlogPostOut.model_validate(post)}


# ── Update post ───────────────────────────────────────────────────────────────


@router.patch(
    "/blog/{post_id}",
    response_model=dict,
    tags=["Blog"],
    summary="Update an existing blog post",
)
async def update_blog_post(
    post_id: int,
    payload: BlogPostUpdate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("blog.manage")),
):
    # If slug is being changed, check uniqueness
    if payload.slug is not None:
        existing = await BlogService.get_by_slug(db, payload.slug)
        if existing and existing.id != post_id:
            raise DuplicateResourceException("Blog post", "slug")

    post = await BlogService.update(db, post_id, payload)
    if not post:
        raise ResourceNotFoundException("Blog post", post_id)
    return {"success": True, "data": BlogPostOut.model_validate(post)}


# ── Delete post ───────────────────────────────────────────────────────────────


@router.delete(
    "/blog/{post_id}",
    response_model=dict,
    tags=["Blog"],
    summary="Delete a blog post",
)
async def delete_blog_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("blog.manage")),
):
    deleted = await BlogService.delete(db, post_id)
    if not deleted:
        raise ResourceNotFoundException("Blog post", post_id)
    return {"success": True, "message": "Blog post deleted"}


# ── Toggle publish ────────────────────────────────────────────────────────────


@router.post(
    "/blog/{post_id}/publish",
    response_model=dict,
    tags=["Blog"],
    summary="Toggle publish status of a blog post",
)
async def toggle_publish_blog_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("blog.manage")),
):
    post = await BlogService.get_by_id(db, post_id)
    if not post:
        raise ResourceNotFoundException("Blog post", post_id)

    post.is_published = not post.is_published
    if post.is_published and not post.published_at:
        post.published_at = datetime.now(timezone.utc)
    post.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(post)

    return {"success": True, "data": BlogPostOut.model_validate(post)}
