# ============================================================
# WAYTERO — PARTNER DRIVER TRACKING / DISTANCE REVIEWS
# File: app/modules/partner/driver_tracking_api.py
# Prefix: /partners
# Doc Ref: 04_API_Documentation/13_DRIVER_APP_API.md
#          06_Backend_Architecture/11_DRIVER_APP_BACKEND.md
#
# Live tracking is only exposed to the partner that owns the assignment. It is
# deliberately separate from the partner booking router because these are
# operational driver facts, not booking commercial data.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.booking.models import (
    CabBooking,
    CabBookingAssignment,
    DriverTripPoint,
    TripDistanceReview,
)
from app.modules.driver.models import Driver
from app.modules.partner.booking_api import _resolve_partner_id
from app.modules.vehicle.models import Vehicle
from app.shared.responses.base import success_response

router = APIRouter()


class ResolveReviewRequest(BaseModel):
    status: str = Field("ACCEPTED", description="ACCEPTED | REVIEWED")
    note: Optional[str] = Field(None, max_length=1000)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get(
    "/me/bookings/{booking_number}/live-location",
    summary="[Partner] Live driver location and traffic estimate",
)
async def live_location(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    cb = (
        await db.execute(
            select(CabBooking).where(
                func.upper(CabBooking.booking_number) == booking_number.strip().upper()
            )
        )
    ).scalar_one_or_none()
    if cb is None:
        raise ResourceNotFoundException("Booking", booking_number)

    assignment = (
        await db.execute(
            select(CabBookingAssignment)
            .where(
                CabBookingAssignment.cab_booking_id == cb.id,
                CabBookingAssignment.partner_id == partner_id,
            )
            .order_by(CabBookingAssignment.assigned_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise ResourceNotFoundException("Assignment for this partner")

    point = (
        await db.execute(
            select(DriverTripPoint)
            .where(DriverTripPoint.cab_booking_id == cb.id)
            .order_by(DriverTripPoint.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    driver = (
        await db.execute(select(Driver).where(Driver.id == assignment.driver_id))
    ).scalar_one_or_none()
    vehicle = (
        await db.execute(select(Vehicle).where(Vehicle.id == assignment.vehicle_id))
    ).scalar_one_or_none()
    if point is None:
        return success_response(
            "No location yet",
            {
                "booking_number": cb.booking_number,
                "driver": {
                    "id": driver.id if driver else None,
                    "name": driver.full_name if driver else None,
                    "mobile": driver.mobile if driver else None,
                },
                "vehicle_reg": vehicle.registration_number if vehicle else None,
                "location": None,
                "tracking_active": cb.booking_status == "STARTED",
                "message": "The driver has not uploaded a location for this trip.",
            },
        )

    recorded = _aware(point.recorded_at)
    age_seconds = (
        max(0, int((datetime.now(timezone.utc) - recorded).total_seconds()))
        if recorded
        else None
    )
    speed_kmh = float(point.speed_mps or 0) * 3.6
    if age_seconds is not None and age_seconds > 180:
        traffic = "UNKNOWN"
    elif speed_kmh < 10:
        traffic = "HEAVY"
    elif speed_kmh < 25:
        traffic = "MODERATE"
    else:
        traffic = "LIGHT"

    return success_response(
        "Live location",
        {
            "booking_number": cb.booking_number,
            "driver": {
                "id": driver.id if driver else None,
                "name": driver.full_name if driver else None,
                "mobile": driver.mobile if driver else None,
            },
            "vehicle_reg": vehicle.registration_number if vehicle else None,
            "location": {
                "latitude": float(point.latitude),
                "longitude": float(point.longitude),
                "accuracy_m": (
                    float(point.accuracy_m) if point.accuracy_m is not None else None
                ),
                "speed_kmh": round(speed_kmh, 1),
                "recorded_at": _iso(point.recorded_at),
                "age_seconds": age_seconds,
                "is_stale": (age_seconds or 0) > 180,
            },
            "traffic_level": traffic,
            "tracking_active": cb.booking_status == "STARTED",
            "paused": cb.trip_paused_at is not None,
            "paused_seconds": int(cb.trip_paused_seconds or 0),
            "gps_distance_km": float(cb.gps_distance_km or 0),
            "odometer_distance_km": (
                float(cb.odometer_distance_km or 0)
                if cb.odometer_distance_km is not None
                else None
            ),
            "trip_status": cb.booking_status,
        },
    )


@router.get(
    "/me/distance-reviews",
    summary="[Partner] Odometer-vs-map distance reviews",
)
async def list_distance_reviews(
    status: str = Query(
        "PENDING_REVIEW", pattern="^(PENDING_REVIEW|ACCEPTED|REVIEWED|ALL)$"
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    stmt = (
        select(TripDistanceReview, CabBooking, Driver)
        .join(CabBooking, CabBooking.id == TripDistanceReview.cab_booking_id)
        .join(Driver, Driver.id == TripDistanceReview.driver_id)
        .where(Driver.partner_id == partner_id)
    )
    if status != "ALL":
        stmt = stmt.where(TripDistanceReview.status == status)
    rows = (
        await db.execute(
            stmt.order_by(TripDistanceReview.created_at.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": review.id,
            "booking_number": cb.booking_number,
            "driver_id": driver.id,
            "driver_name": driver.full_name,
            "driver_code": driver.driver_code,
            "odometer_km": float(review.odometer_km),
            "gps_km": float(review.gps_km),
            "difference_km": float(review.difference_km),
            "difference_pct": float(review.difference_pct),
            "status": review.status,
            "review_note": review.review_note,
            "created_at": _iso(review.created_at),
            "trip_ended_at": _iso(cb.trip_ended_at),
        }
        for review, cb, driver in rows
    ]
    return success_response(
        "Distance reviews retrieved", {"items": items, "page": page}
    )


@router.post(
    "/me/distance-reviews/{review_id}/resolve",
    summary="[Partner] Resolve an odometer-vs-map review",
)
async def resolve_distance_review(
    review_id: int,
    payload: ResolveReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    row = (
        await db.execute(
            select(TripDistanceReview, CabBooking, Driver)
            .join(CabBooking, CabBooking.id == TripDistanceReview.cab_booking_id)
            .join(Driver, Driver.id == TripDistanceReview.driver_id)
            .where(
                TripDistanceReview.id == review_id,
                Driver.partner_id == partner_id,
            )
        )
    ).first()
    if row is None:
        raise ResourceNotFoundException("Distance review", review_id)
    review, cb, _driver = row
    status = payload.status.strip().upper()
    if status not in ("ACCEPTED", "REVIEWED"):
        raise ValidationException("status must be ACCEPTED or REVIEWED")
    review.status = status
    review.review_note = payload.note
    review.reviewed_by_user_id = UUID(str(current_user["sub"]))
    review.reviewed_at = datetime.now(timezone.utc)
    cb.distance_review_status = status
    return success_response(
        "Distance review updated", {"id": review.id, "status": review.status}
    )
