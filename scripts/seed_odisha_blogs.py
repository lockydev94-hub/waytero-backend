#!/usr/bin/env python3
"""
WayTero — Blog Post Seeder (admin-equivalent bulk insert).

Creates 10 PUBLISHED blog posts exactly the way the admin blog editor
(POST /api/v1/admin/settings/blog) would have saved them:

  blog_posts — title/slug/content/tags/SEO, is_published=True,
               published_at stamped, created_by = the admin user

Prerequisites checked before any write (fails loudly if unmet):
  * at least one admin user exists (created_by attribution)
  * no slug collision with existing posts

Idempotent: posts with the same slug are SKIPPED, not duplicated.
Re-running is always safe.

Featured images are intentionally NOT set — the admin uploads and manages
featured images from the admin dashboard afterwards (BlogEditorPage →
MediaUploader).

Usage (from the backend project root on the server):
    python -m scripts.seed_odisha_blogs            # normal run
    python -m scripts.seed_odisha_blogs --dry-run  # validate only
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path setup (same pattern as seed_cms_defaults.py) ────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Register the users table in Base.metadata — blog_posts.created_by has an FK
# to users.id and the flush fails without this model imported.
import app.modules.auth.models.user  # noqa: F401
from app.core.config import settings
from app.modules.admin.models import BlogPost
from app.modules.auth.models.user import User

from scripts.odisha_blog_data import AUTHOR_NAME, BLOG_POSTS

DRY_RUN = "--dry-run" in sys.argv


async def main() -> None:
    print("=" * 64)
    print("WayTero — Blog Post Seeder")
    print("=" * 64)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # ── 1. Locate an admin user for created_by attribution ───────────
        admin = await db.scalar(
            select(User).where(
                User.user_type.in_(["ADMIN", "SUPER_ADMIN"])
            ).order_by(User.created_at).limit(1)
        )
        if admin is None:
            print("FATAL: no ADMIN/SUPER_ADMIN user found; cannot attribute posts.")
            sys.exit(1)
        print(f"Admin   : {admin.email}")

        # ── 2. Idempotency + collision checks ────────────────────────────
        existing_slugs = {
            r for r in (await db.scalars(select(BlogPost.slug))).all()
        }
        inserts = [p for p in BLOG_POSTS if p["slug"] not in existing_slugs]
        skips = len(BLOG_POSTS) - len(inserts)
        print(f"To insert: {len(inserts)}  |  already present (skip): {skips}")
        if DRY_RUN:
            for p in inserts:
                print(f"  would insert {p['slug']} — {p['title']}")
            print("DRY RUN — no changes written.")
            await engine.dispose()
            return

        # ── 3. Insert posts, staggering published_at for a natural feed ──
        now = datetime.now(timezone.utc)
        for idx, post_data in enumerate(inserts):
            # Newest first on the public listing; space them a week apart
            # so the archive looks organic rather than machine-stamped.
            published_at = now - timedelta(days=7 * (len(inserts) - 1 - idx))
            post = BlogPost(
                title=post_data["title"],
                slug=post_data["slug"],
                excerpt=post_data["excerpt"],
                content=post_data["content"],
                # featured_image_url intentionally left NULL — managed
                # by the admin from the dashboard media uploader.
                featured_image_url=None,
                author_name=AUTHOR_NAME,
                author_avatar_url=None,
                tags=post_data["tags"],
                is_published=True,
                published_at=published_at,
                seo_title=post_data["seo_title"],
                seo_description=post_data["seo_description"],
                seo_keywords=post_data["seo_keywords"],
                created_by=admin.id,
            )
            db.add(post)
            await db.flush()
            print(
                f"  inserted id={post.id} {post.slug} "
                f"(published {published_at.date().isoformat()})"
            )

        await db.commit()

    await engine.dispose()
    print("Done. Posts are PUBLISHED and live on the public blog.")


if __name__ == "__main__":
    asyncio.run(main())
