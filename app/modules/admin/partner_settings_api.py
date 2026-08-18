# ============================================================
# WAYTERO — PARTNER SETTINGS API ROUTER (ASYNC)
# File: app/modules/admin/partner_settings_api.py
# Doc Ref:
#   Security Hardening — S1: partner-portal dependencies on /admin/settings
#
# Mounted at /partners/settings (see app/api/router.py).
# These endpoints give the partner portal its legitimately-needed settings
# data WITHOUT exposing the admin settings surface:
#   GET  /partners/settings/master/cities   — city picker for pricing rules
#   POST /partners/settings/upload-media    — Cloudinary upload scoped to the
#                                             authenticated partner's folder
#
# The Cloudinary upload here deliberately ignores any caller-supplied folder
# override: the folder is derived from the authenticated partner_id in the JWT,
# so a partner can never write into another partner's or the platform's folder.
# ============================================================

import hashlib
import time
from typing import List

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.modules.admin.models import ApiIntegration
from app.modules.admin.schemas import CityOut
from app.modules.admin.services import CityService

router = APIRouter()


@router.get(
    "/master/cities",
    response_model=List[CityOut],
    tags=["Partner Settings – Master Data"],
)
async def list_cities_for_partner(
    active_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """List cities for the partner portal pricing/city pickers."""
    return await CityService.list_all(db, active_only=active_only)


# ── Cloudinary upload (partner-scoped) ──────────────────────────────────────
# Mirrors the admin /admin/settings/upload-media logic but forces the folder
# to waytero/partners/{partner_id}/{documents|photos} — never caller-supplied.

ASSET_SPECS: dict = {
    "logo": {"width": 400, "height": 120, "crop": "fill"},
    "favicon": {"width": 32, "height": 32, "crop": "fill"},
    "og_image": {"width": 1200, "height": 630, "crop": "fill"},
    "general": {},
}


def _cloudinary_sign(params: dict, api_secret: str) -> str:
    """Cloudinary v2 signature = SHA1(sorted key=value pairs + api_secret)."""
    excluded = {"file", "api_key", "resource_type", "cloud_name"}
    sorted_str = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k not in excluded
    )
    return hashlib.sha1((sorted_str + api_secret).encode("utf-8")).hexdigest()


@router.post(
    "/upload-media",
    tags=["Partner Settings – Media Upload"],
    summary="Upload a partner document/photo to Cloudinary (partner-scoped folder)",
)
async def upload_partner_media(
    file: UploadFile = File(...),
    asset_type: str = Form("general", description="document | photo | general"),
    current_user: dict = Depends(require_roles("PARTNER")),
    db: AsyncSession = Depends(get_db),
):
    partner_id = current_user.get("partner_id")
    if not partner_id:
        raise HTTPException(
            status_code=403,
            detail="Partner context required for media upload",
        )

    result = await db.execute(
        sa_select(ApiIntegration).where(
            ApiIntegration.service_type == "CLOUDINARY",
            ApiIntegration.is_active == True,  # noqa: E712
        )
    )
    integration = result.scalar_one_or_none()
    if not integration or not integration.configuration:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cloudinary integration is not configured or not active. "
                "Contact the WayTero administrator."
            ),
        )

    cfg: dict = integration.configuration
    cloud_name = cfg.get("cloud_name", "").strip()
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()
    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(
            status_code=400,
            detail="Cloudinary integration is incomplete. Contact the WayTero administrator.",
        )

    folder = f"waytero/partners/{partner_id}/{asset_type}s"
    ts = int(time.time())
    upload_params: dict = {
        "timestamp": ts,
        "folder": folder,
        "public_id": f"{asset_type}_{ts}_{partner_id}",
    }

    upload_params["signature"] = _cloudinary_sign(upload_params, api_secret)
    upload_params["api_key"] = api_key

    content = await file.read()
    if len(content) > 20 * 1024 * 1024:  # 20 MB cap
        raise HTTPException(status_code=413, detail="File too large — maximum 20 MB.")

    ct = (file.content_type or "").lower()
    filename_lower = (file.filename or "").lower()
    is_raw = ct == "application/pdf" or filename_lower.endswith(".pdf")
    resource_type = "raw" if is_raw else "image"

    upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                upload_url,
                data=upload_params,
                files={
                    "file": (
                        file.filename or "upload",
                        content,
                        file.content_type or "image/png",
                    )
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cloudinary request failed: {exc}")

    if resp.status_code not in (200, 201):
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=502, detail=f"Cloudinary error: {detail}")

    data = resp.json()
    return {
        "secure_url": data.get("secure_url"),
        "public_id": data.get("public_id"),
        "width": data.get("width"),
        "height": data.get("height"),
        "format": data.get("format"),
        "asset_type": asset_type,
        "resource_type": resource_type,
    }
