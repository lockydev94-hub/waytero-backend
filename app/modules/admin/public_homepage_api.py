# ============================================================
# WAYTERO — PUBLIC HOMEPAGE ROUTER (CMS-Driven)
# File: app/modules/admin/public_homepage_api.py
# Doc Ref:
#   Migration 0044_website_cms
#   BRD Part 6 §155 — admin-controlled homepage
#
#  GET /public/homepage
#    Returns the homepage bundle customer-web renders:
#      • header (singleton)
#      • footer (singleton)
#      • sections (visible + active in-window variants, in display_order)
#
#  GET /public/auth-modal-images
#    Returns the promotion images the admin uploaded for the website's
#    login/auth modal (left-side image slider). Reads the
#    AUTH_MODAL_IMAGES system configuration (JSON array of URLs).
#
#  GET /public/homepage-band-counts
#    Row counts for the six data-driven homepage bands, so the site can
#    tell which bands will paint before it lays out their backgrounds.
#
#    No auth — all are read by the public marketing site.
# ============================================================

import json
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.admin.services import (
    BlogService,
    CmsService,
    ServiceTypeService,
)
from app.modules.admin.schemas import PublicHomepageOut


router = APIRouter()


class PublicServiceTypeSeoOut(BaseModel):
    """SEO metadata for a platform service type (CAB | HOTEL | TOUR).

    Served on the public site so customer-web can render per-service
    listing-page metadata (title / description / keywords) straight from
    the DB, falling back to its own static defaults when these are null.
    Doc Ref: DB Schema Part 2 §8 — service_types (migration 0019).
    """

    type_code: str
    label: str
    description: Optional[str] = None
    icon_url: Optional[str] = None
    image_url: Optional[str] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None


@router.get(
    "/service-types",
    response_model=List[PublicServiceTypeSeoOut],
    tags=["Public Homepage"],
    summary="Active platform service types with their SEO metadata",
    description=(
        "Returns the active service types (CAB | HOTEL | TOUR) with the "
        "label, description, media and SEO fields admins maintain under "
        "Settings → Service Types. The customer-web uses this to render "
        "advanced, DB-driven SEO meta tags on its listing pages. "
        "No authentication required."
    ),
)
async def get_public_service_types(
    db: AsyncSession = Depends(get_db),
) -> List[PublicServiceTypeSeoOut]:
    rows = await ServiceTypeService.list_all(db, active_only=True)
    return [
        PublicServiceTypeSeoOut(
            type_code=st.type_code,
            label=st.label,
            description=st.description,
            icon_url=st.icon_url,
            image_url=st.image_url,
            seo_title=st.seo_title,
            seo_description=st.seo_description,
            seo_keywords=st.seo_keywords,
        )
        for st in rows
    ]


@router.get(
    "/homepage",
    response_model=PublicHomepageOut,
    tags=["Public Homepage"],
    summary="Resolve the homepage bundle (header + footer + active section variants)",
    description=(
        "Returns the homepage bundle that customer-web renders. Only sections "
        "with is_visible=TRUE and an in-window active variant are included. "
        "No authentication required."
    ),
)
async def get_public_homepage(db: AsyncSession = Depends(get_db)):
    return await CmsService.build_public_homepage(db)


class PublicPlatformProfileOut(BaseModel):
    """Platform branding media + public contact for the marketing site.

    Read from system_configurations (seeded by migration 0013 and updated
    by the admin via Settings → Platform Profile uploads). Media fields are
    empty strings when the admin hasn't uploaded the asset yet — customer-web
    then falls back to its bundled static files. support_phone is the contact
    number saved under Settings → Platform Details (SUPPORT_PHONE key);
    support_email/office_address/tagline/social_links come from the brand
    master keys seeded by migration 0072. office_hours and whatsapp_phone are
    the desk-hours keys (PLATFORM_OFFICE_HOURS / PLATFORM_WHATSAPP_PHONE) —
    whatsapp_phone is empty when the admin has not set one, in which case
    customer-web uses support_phone. customer-web falls back to its own brand
    master when a value is empty.
    """

    logo_url: str = ""
    favicon_url: str = ""
    og_image_url: str = ""
    support_phone: str = ""
    support_email: str = ""
    office_address: str = ""
    office_hours: str = ""
    whatsapp_phone: str = ""
    tagline: str = ""
    social_links: dict = {}


def _single_line_address(raw: str) -> str:
    """The OFFICE_ADDRESS key, else the primary (or first) entry of the
    PLATFORM_OFFICE_ADDRESSES array that Settings → Platform actually writes.

    The two keys were added a year apart and nothing ever reconciled them, so
    an admin who only filled the address cards would never appear on the
    contact page. Reading the array here is what closes that gap.
    """
    try:
        entries = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ""
    if not isinstance(entries, list) or not entries:
        return ""
    chosen = next(
        (e for e in entries if isinstance(e, dict) and e.get("is_primary")),
        next((e for e in entries if isinstance(e, dict)), None),
    )
    if not chosen:
        return ""
    return ", ".join(
        str(part).strip()
        for part in (
            chosen.get("line1"),
            chosen.get("line2"),
            chosen.get("city"),
            chosen.get("state"),
            chosen.get("pincode"),
        )
        if str(part or "").strip()
    )


@router.get(
    "/platform-profile",
    response_model=PublicPlatformProfileOut,
    tags=["Public Homepage"],
    summary="Platform branding media (logo, favicon, OG image) uploaded by admin",
    description=(
        "Returns the platform logo / favicon / Open-Graph image URLs the "
        "admin uploaded under Settings → Platform Profile. The customer-web "
        "uses favicon_url for the browser tab icon and og_image_url for "
        "social shares; empty strings mean the admin hasn't uploaded yet. "
        "Also returns contact/brand fields from system_configurations "
        "(SUPPORT_PHONE, SUPPORT_EMAIL, OFFICE_ADDRESS, PLATFORM_OFFICE_HOURS, "
        "PLATFORM_WHATSAPP_PHONE, PLATFORM_TAGLINE, SOCIAL_* keys) — "
        "customer-web falls back to its owner-approved brand master when a "
        "value is empty. No authentication required."
    ),
)
async def get_public_platform_profile(
    db: AsyncSession = Depends(get_db),
) -> PublicPlatformProfileOut:
    rows = (
        await db.execute(
            text(
                "SELECT config_key, config_value FROM system_configurations "
                "WHERE config_key IN ("
                "'PLATFORM_LOGO_URL', 'PLATFORM_FAVICON_URL', 'PLATFORM_OG_IMAGE_URL', "
                "'SUPPORT_PHONE', 'SUPPORT_EMAIL', 'OFFICE_ADDRESS', "
                "'PLATFORM_OFFICE_ADDRESSES', 'PLATFORM_OFFICE_HOURS', "
                "'PLATFORM_WHATSAPP_PHONE', "
                "'PLATFORM_TAGLINE', 'SOCIAL_YOUTUBE', 'SOCIAL_INSTAGRAM', 'SOCIAL_FACEBOOK'"
                ")"
            )
        )
    ).all()
    cfg = {key: (value or "") for key, value in rows}
    socials = {
        platform: url.strip()
        for platform, url in (
            ("youtube", cfg.get("SOCIAL_YOUTUBE", "")),
            ("instagram", cfg.get("SOCIAL_INSTAGRAM", "")),
            ("facebook", cfg.get("SOCIAL_FACEBOOK", "")),
        )
        if url.strip()
    }
    return PublicPlatformProfileOut(
        logo_url=cfg.get("PLATFORM_LOGO_URL", ""),
        favicon_url=cfg.get("PLATFORM_FAVICON_URL", ""),
        og_image_url=cfg.get("PLATFORM_OG_IMAGE_URL", ""),
        support_phone=cfg.get("SUPPORT_PHONE", "").strip(),
        support_email=cfg.get("SUPPORT_EMAIL", "").strip(),
        office_address=(
            cfg.get("OFFICE_ADDRESS", "").strip()
            or _single_line_address(cfg.get("PLATFORM_OFFICE_ADDRESSES", ""))
        ),
        office_hours=cfg.get("PLATFORM_OFFICE_HOURS", "").strip(),
        whatsapp_phone=cfg.get("PLATFORM_WHATSAPP_PHONE", "").strip(),
        tagline=cfg.get("PLATFORM_TAGLINE", "").strip(),
        social_links=socials,
    )


@router.get(
    "/site-seo",
    tags=["Public Homepage"],
    summary="Per-page head metadata authored in system configurations",
    description=(
        "Returns {page_key: {title, description}} for every SEO_PAGE_* "
        "key in system_configurations (seeded by migration 0072, editable "
        "by the admin). customer-web uses these for static-page <head> "
        "metadata and falls back to its brand master when a page has no "
        "DB row. No authentication required."
    ),
)
async def get_public_site_seo(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(
            text(
                "SELECT config_key, config_value FROM system_configurations "
                "WHERE config_key LIKE 'SEO_PAGE\\_%'"
            )
        )
    ).all()
    out: dict = {}
    for key, value in rows:
        try:
            data = json.loads(value or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        slug = key.removeprefix("SEO_PAGE_").lower()
        title = data.get("title")
        description = data.get("description")
        title = title.strip() if isinstance(title, str) else ""
        description = description.strip() if isinstance(description, str) else ""
        if title or description:
            out[slug] = {"title": title, "description": description}
    return out


class PublicAuthModalImagesOut(BaseModel):
    """Promotion images for the website login/auth modal (left-side slider)."""

    images: List[str] = []


@router.get(
    "/auth-modal-images",
    response_model=PublicAuthModalImagesOut,
    tags=["Public Homepage"],
    summary="Promotion images shown in the website login/auth modal",
    description=(
        "Returns the ordered list of image URLs the admin uploaded under "
        "CMS → Auth Modal. The customer-web renders them as a left-side "
        "slider next to the login form. Empty when nothing is configured."
    ),
)
async def get_public_auth_modal_images(
    db: AsyncSession = Depends(get_db),
) -> PublicAuthModalImagesOut:
    row = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations "
                "WHERE config_key = 'AUTH_MODAL_IMAGES'"
            )
        )
    ).first()

    images: List[str] = []
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, list):
                images = [str(u).strip() for u in parsed if str(u).strip()]
        except (ValueError, TypeError):
            images = []

    return PublicAuthModalImagesOut(images=images)


@router.get(
    "/homepage-band-counts",
    tags=["Public Homepage"],
    summary="How many rows each data-driven homepage band can actually paint",
    description=(
        "Returns {section_key: count} for the six homepage bands that fetch "
        "their content from the DB (FEATURED_HOTELS, POPULAR_DESTINATIONS, "
        "POPULAR_ROUTES, POPULAR_CITIES, TOUR_PACKAGES, BLOG). A band with "
        "count 0 renders nothing at all, so the website can only alternate "
        "its light/dark background correctly if it knows which bands will "
        "paint before it paints them. Counts mirror the filters of the "
        "public list endpoints each band calls. No authentication required."
    ),
)
async def get_public_homepage_band_counts(
    db: AsyncSession = Depends(get_db),
) -> dict:
    # One row of five scalars. Each subselect is the public list endpoint's
    # own predicate, noted inline — if a listing filter changes, this must
    # change with it or the homepage hides/shows a band the wrong way.
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT
                  -- /public/hotel/search  (public_hotel_api.py `where`)
                  (SELECT COUNT(*) FROM hotels h
                     WHERE h.status = 'ACTIVE' AND h.deleted_at IS NULL)
                     AS featured_hotels,

                  -- /public/tours/popular-destinations (tour/api.py): cities
                  -- grouped by their ACTIVE package count.
                  (SELECT COUNT(DISTINCT c.id) FROM cities c
                     JOIN states st        ON st.id = c.state_id
                     JOIN tour_packages tp ON tp.city_id = c.id
                          AND tp.status = 'ACTIVE'
                    WHERE c.is_active = TRUE) AS popular_destinations,

                  -- /public/routes (public_cab_route_api.py `where`)
                  (SELECT COUNT(*) FROM cab_routes r
                     WHERE r.is_active) AS popular_routes,

                  -- /public/cab/cities (public_cab_api.py): a city counts
                  -- when at least one active vehicle category can be priced
                  -- for it. The band additionally drops blank names.
                  (SELECT COUNT(*) FROM cities c
                    WHERE c.is_active = TRUE
                      AND BTRIM(COALESCE(c.name, '')) <> ''
                      AND EXISTS (
                            SELECT 1 FROM vehicle_categories vc
                             WHERE vc.is_active = TRUE
                               AND (EXISTS (SELECT 1 FROM vehicle_pricing_rules r
                                             WHERE r.city_id = c.id
                                               AND r.vehicle_category_id = vc.id)
                                 OR EXISTS (SELECT 1 FROM default_vehicle_pricing_rules d
                                            WHERE d.vehicle_category_id = vc.id))
                          )) AS popular_cities,

                  -- /public/tours/packages (tour/api.py statuses=['ACTIVE'])
                  (SELECT COUNT(*) FROM tour_packages
                     WHERE status = 'ACTIVE') AS tour_packages
                """
                )
            )
        )
        .mappings()
        .first()
    )

    # /public/blog (public_blog_api.py) — reused rather than re-derived so
    # the "published only" rule lives in one place.
    blog_total = await BlogService.count_posts(db, published_only=True)

    return {
        "FEATURED_HOTELS": int(row["featured_hotels"] or 0),
        "POPULAR_DESTINATIONS": int(row["popular_destinations"] or 0),
        "POPULAR_ROUTES": int(row["popular_routes"] or 0),
        "POPULAR_CITIES": int(row["popular_cities"] or 0),
        "TOUR_PACKAGES": int(row["tour_packages"] or 0),
        "BLOG": int(blog_total or 0),
    }
