# ============================================================
# WAYTERO — PUBLIC FIREBASE WEB CONFIG
# File: app/modules/admin/public_firebase_api.py
# Doc Ref:
#   Security Hardening — S5: api-integrations credential leak closure
#
# Mounted at /public/firebase-config.
#
# The old flow made the partner portal read the raw FIREBASE row from
# /admin/settings/api-integrations, which also leaked the *server* credential
# (private_key) to anyone who could reach that unauthenticated endpoint.
#
# This endpoint returns ONLY the public browser-side FCM web config fields
# (apiKey, authDomain, projectId, storageBucket, messagingSenderId, appId).
# Those fields are designed to ship to browsers by Google's own model; the
# private server credential is never exposed here.
# ============================================================

from fastapi import APIRouter, Depends
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.admin.models import ApiIntegration

router = APIRouter()

# Fields that are safe to ship to a browser (Google's public web config).
PUBLIC_FIREBASE_FIELDS = (
    "api_key",
    "web_api_key",
    "auth_domain",
    "project_id",
    "storage_bucket",
    "messaging_sender_id",
    "app_id",
    "web_app_id",
    "vapid_key",
)


@router.get(
    "/firebase-config",
    tags=["Public Config"],
    summary="Public Firebase web config for FCM push (no server credentials)",
)
async def get_public_firebase_config(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        sa_select(ApiIntegration).where(
            ApiIntegration.service_type == "FIREBASE",
            ApiIntegration.is_active == True,  # noqa: E712
        )
    )
    integration = result.scalar_one_or_none()
    if not integration or not integration.configuration:
        return {"configured": False, "config": None}

    full = integration.configuration
    config = {
        "api_key": full.get("web_api_key") or full.get("api_key"),
        "auth_domain": full.get("auth_domain"),
        "project_id": full.get("project_id"),
        "storage_bucket": full.get("storage_bucket"),
        "messaging_sender_id": full.get("messaging_sender_id"),
        "app_id": full.get("web_app_id") or full.get("app_id"),
    }
    return {"configured": True, "config": {k: v for k, v in config.items() if v}}
