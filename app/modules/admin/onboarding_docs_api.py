# ============================================================
# WAYTERO — ADMIN ONBOARDING DOCS API
# File: app/modules/admin/onboarding_docs_api.py
# Prefix: /admin/onboarding-docs  (registered in api/router.py)
#
# Purpose:
#   Serve the partner onboarding documents as downloadable PDFs:
#     - Guides: partner registration, driver onboarding, vehicle onboarding
#     - Forms:  partner registration, vehicle registration, driver registration
#
#   Documents are generated on the fly by onboarding_pdf_service.py with the
#   same platform header (logo, name, GSTIN, address, support contact) as the
#   invoice PDFs, pulled from system_configurations at render time.
#
# Doc Ref:
#   Docs/22_Partner_Onboarding_Guides/
#   BRD Part 2 §17-22 — Partner KYC & onboarding
#   BRD Part 3 §133-138 — Vehicle onboarding
# ============================================================

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.admin.models import SystemConfiguration
from app.modules.admin.onboarding_pdf_service import (
    _BRANDING_KEYS,
    FORM_KEYS,
    GUIDE_KEYS,
    generate_onboarding_pdf,
)

router = APIRouter()

_BRANDING_KEYS_LIST = list(_BRANDING_KEYS)


async def _load_branding_cfg(db: AsyncSession) -> dict[str, str]:
    config_rows = (
        (
            await db.execute(
                select(SystemConfiguration).where(
                    SystemConfiguration.config_key.in_(_BRANDING_KEYS_LIST)
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.config_key: (row.config_value or "") for row in config_rows}


@router.get("/guide/{doc_type}")
async def download_onboarding_guide(
    doc_type: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Download an onboarding guide PDF (partner / driver / vehicle)."""
    key = f"guide_{doc_type}"
    if key not in GUIDE_KEYS:
        raise HTTPException(
            404,
            f"Unknown guide '{doc_type}'. Available: "
            + ", ".join(k.removeprefix("guide_") for k in GUIDE_KEYS),
        )
    cfg = await _load_branding_cfg(db)
    try:
        doc = generate_onboarding_pdf(key, cfg)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@router.get("/form/{form_type}")
async def download_onboarding_form(
    form_type: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Download a hand-fillable registration form PDF (partner / vehicle / driver)."""
    key = f"form_{form_type}"
    if key not in FORM_KEYS:
        raise HTTPException(
            404,
            f"Unknown form '{form_type}'. Available: "
            + ", ".join(k.removeprefix("form_") for k in FORM_KEYS),
        )
    cfg = await _load_branding_cfg(db)
    try:
        doc = generate_onboarding_pdf(key, cfg)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )
