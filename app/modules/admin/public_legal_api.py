# ============================================================
# WAYTERO — PUBLIC LEGAL + CANCELLATION POLICY API
# File: app/modules/admin/public_legal_api.py
# Prefix: /public  (registered in api/router.py)
#
# No auth — consumed by customer-web's SSR pages.
#
#   GET /public/legal/{slug}
#       Serves the seeded legal page (privacy / terms / refund /
#       cookies / booking-instructions) as the raw JSON stored in
#       system_configurations under LEGAL_PAGE_{SLUG_UPPER}.
#
#   GET /public/cancellation-policy
#       Returns the LIVE cab + tour ladders from system_configurations
#       so the Refund Policy page always shows the numbers the
#       cancellation engine actually enforces (even after an admin
#       edits a ladder). Hotels are per-hotel and noted as such.
# ============================================================

from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

router = APIRouter()

LEGAL_SLUGS = {
    "privacy",
    "terms",
    "refund",
    "cookies",
    "booking-instructions",
}


@router.get(
    "/legal/{slug}",
    tags=["Public Legal"],
    summary="Return a legal page (privacy / terms / refund / cookies / booking-instructions).",
)
async def get_legal_page(
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    key = f"LEGAL_PAGE_{slug.strip().lower().replace('-', '_').upper()}"
    row = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": key},
        )
    ).first()
    if not row or not row[0]:
        raise HTTPException(404, "Legal page not found")
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        raise HTTPException(500, "Stored legal page is not valid JSON")


@router.get(
    "/legal",
    tags=["Public Legal"],
    summary="List the available legal page slugs.",
)
async def list_legal_pages(
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    rows = (
        (
            await db.execute(
                text(
                    "SELECT config_key FROM system_configurations WHERE config_key LIKE 'LEGAL_PAGE_%'"
                )
            )
        )
        .scalars()
        .all()
    )
    slugs = sorted(
        key.replace("LEGAL_PAGE_", "").lower().replace("_", "-") for key in rows
    )
    return {"slugs": slugs}


@router.get(
    "/cancellation-policy",
    tags=["Public Legal"],
    summary="Return the live cab + tour cancellation ladders (no auth).",
)
async def public_cancellation_policy(
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT config_key, config_value FROM system_configurations
                    WHERE config_key IN (
                        'CANCELLATION_FREE_HOURS_CAB',
                        'CANCELLATION_TIER_1_HOURS_CAB',
                        'CANCELLATION_TIER_1_PERCENT_CAB',
                        'CANCELLATION_TIER_2_HOURS_CAB',
                        'CANCELLATION_TIER_2_PERCENT_CAB',
                        'CANCELLATION_SAME_DAY_PERCENT_CAB',
                        'CANCELLATION_AFTER_ASSIGNMENT_PERCENT_CAB',
                        'TOUR_CANCELLATION_FREE_DAYS',
                        'TOUR_CANCELLATION_TIER_1_DAYS',
                        'TOUR_CANCELLATION_TIER_1_PERCENT',
                        'TOUR_CANCELLATION_TIER_2_DAYS',
                        'TOUR_CANCELLATION_TIER_2_PERCENT',
                        'TOUR_CANCELLATION_TIER_3_PERCENT',
                        'TOUR_CANCELLATION_LAST_MINUTE_PERCENT'
                    )
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    cfg: Dict[str, str] = {r["config_key"]: r["config_value"] for r in rows}

    def _num(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "cab": {
            "free_hours": _num("CANCELLATION_FREE_HOURS_CAB", 2),
            "tier_1_hours": _num("CANCELLATION_TIER_1_HOURS_CAB", 12),
            "tier_1_percent": _num("CANCELLATION_TIER_1_PERCENT_CAB", 75),
            "tier_2_hours": _num("CANCELLATION_TIER_2_HOURS_CAB", 4),
            "tier_2_percent": _num("CANCELLATION_TIER_2_PERCENT_CAB", 50),
            "same_day_percent": _num("CANCELLATION_SAME_DAY_PERCENT_CAB", 0),
            "after_assignment_percent": _num(
                "CANCELLATION_AFTER_ASSIGNMENT_PERCENT_CAB", 50
            ),
            "currency": "INR",
        },
        "tour": {
            "free_days": _num("TOUR_CANCELLATION_FREE_DAYS", 30),
            "tier_1_days": _num("TOUR_CANCELLATION_TIER_1_DAYS", 15),
            "tier_1_percent": _num("TOUR_CANCELLATION_TIER_1_PERCENT", 100),
            "tier_2_days": _num("TOUR_CANCELLATION_TIER_2_DAYS", 7),
            "tier_2_percent": _num("TOUR_CANCELLATION_TIER_2_PERCENT", 75),
            "tier_3_percent": _num("TOUR_CANCELLATION_TIER_3_PERCENT", 50),
            "last_minute_percent": _num("TOUR_CANCELLATION_LAST_MINUTE_PERCENT", 0),
            "currency": "INR",
        },
        "hotel": {
            "note": (
                "Each hotel sets its own cancellation ladder (typically free up to "
                "72 hours, 75% up to 48 hours, 50% up to 24 hours, none same-day). "
                "The policy is shown on the hotel page before booking."
            ),
        },
    }
