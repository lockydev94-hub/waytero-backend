# ============================================================
# WAYTERO — DRIVER-APP BREAKDOWN API
# File: app/modules/driver/breakdown_api.py
# Prefix: /drivers  (mounted alongside the rest of the driver routes)
# Doc Ref:
#   BRD Part 3 §42 — Driver Assignment after partner acceptance
#   Spec: Cab Breakdown → Vehicle Swap (in-trip)
#
# Endpoints used by the Flutter Captain (driver) app:
#   POST /drivers/me/bookings/{cab_booking_id}/report-breakdown
#     — the driver's "Report Breakdown" button on the active-trip screen.
#     Captures GPS automatically. Same business rules as the admin/partner
#     endpoint — the driver must be the one on the active assignment.
#
# The driver app resolves its own driver_id from the JWT (via
# _resolve_driver_id) so the request body only carries the reason + GPS.
# ============================================================

from __future__ import annotations

from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ResourceNotFoundException
from app.modules.booking.services import breakdown as breakdown_service
from app.modules.driver.models import Driver
from app.shared.responses.base import success_response

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class DriverReportBreakdownRequest(BaseModel):
    """Body of the driver-app "Report Breakdown" button."""

    reason_code: str = Field(..., description="One of BREAKDOWN_REASON_CODES")
    latitude: Optional[float] = Field(
        None, description="Auto-captured GPS; null if permission denied"
    )
    longitude: Optional[float] = None
    notes: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _resolve_driver_id(db: AsyncSession, user_uuid: str) -> int:
    """Resolve the Driver row ID from the authenticated user's UUID.

    Drivers log in via the same auth flow as other roles but are linked
    to a Driver row through drivers.user_id. Raises 404 if the user
    has no driver profile.
    """
    driver = (
        await db.execute(select(Driver).where(Driver.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not driver:
        raise ResourceNotFoundException("Driver profile for user", user_uuid)
    return driver.id


# ════════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/bookings/{cab_booking_id}/report-breakdown",
    summary="Driver reports a vehicle breakdown on their active booking",
)
async def driver_report_breakdown(
    cab_booking_id: int,
    payload: DriverReportBreakdownRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Called from the driver app's active-trip screen. The driver must
    be the one on the *active* assignment for this cab — otherwise
    PermissionDeniedException is raised by the service.

    Side effects (same as the partner/admin endpoint, all in one
    transaction):
      • Cab → BREAKDOWN_REPORTED
      • Current vehicle → MAINTENANCE
      • Current driver availability → BREAK
      • pre_swap_actual_km snapshot + booking_timeline event
      • Admin cab-ops dashboard sees the booking on next refresh
    """
    driver_id = await _resolve_driver_id(db, current_user["sub"])

    snapshot = await breakdown_service.report_breakdown(
        db,
        cab_booking_id=cab_booking_id,
        reported_by=breakdown_service.REPORTER_DRIVER,
        reported_by_user_id=(
            UUID(current_user["sub"]) if current_user.get("sub") else None
        ),
        reporter_partner_id=None,
        reporter_driver_id=driver_id,
        reason_code=payload.reason_code,
        latitude=(
            Decimal(str(payload.latitude)) if payload.latitude is not None else None
        ),
        longitude=(
            Decimal(str(payload.longitude)) if payload.longitude is not None else None
        ),
        notes=payload.notes,
    )
    return success_response(
        "Breakdown reported. Operations has been notified — please wait for a replacement vehicle.",
        snapshot,
    )


__all__ = ["router"]
