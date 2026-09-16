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
#    No auth — both are read by the public marketing site.
# ============================================================

import json
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.admin.services import CmsService, ServiceTypeService
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
    customer-web falls back to its own default when empty.
    """

    logo_url: str = ""
    favicon_url: str = ""
    og_image_url: str = ""
    support_phone: str = ""


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
        "No authentication required."
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
                "'SUPPORT_PHONE'"
                ")"
            )
        )
    ).all()
    cfg = {key: (value or "") for key, value in rows}
    return PublicPlatformProfileOut(
        logo_url=cfg.get("PLATFORM_LOGO_URL", ""),
        favicon_url=cfg.get("PLATFORM_FAVICON_URL", ""),
        og_image_url=cfg.get("PLATFORM_OG_IMAGE_URL", ""),
        support_phone=cfg.get("SUPPORT_PHONE", "").strip(),
    )


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
