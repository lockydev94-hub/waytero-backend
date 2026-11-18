# ============================================================
# WAYTERO — CUSTOMER LIVE TRIP TRACKING
# File: app/modules/customer/tracking_api.py
# Prefix: /customers  (registered in api/router.py)
#
# Doc Ref:
#   07_Frontend_Architecture/04_FLUTTER_CUSTOMER_APP.md §24/§25
#   04_API_Documentation/13_DRIVER_APP_API.md (GPS contract)
#
# The customer sees the same driver-published GPS fact the partner sees. The
# only difference is the ownership check: the booking must belong to the
# calling customer instead of to the partner's assignment. No partner,
# commercial or settlement data is returned.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ResourceNotFoundException
from app.modules.booking.models import (
    CabBooking,
    CabBookingAssignment,
    DriverTripPoint,
    MasterBooking,
)
from app.modules.customer.cancellation_api import _resolve_customer
from app.modules.driver.models import Driver
from app.modules.vehicle.models import Vehicle
from app.shared.responses.base import success_response

router = APIRouter()

# A fix older than this is shown as stale rather than as the driver's current
# position. Matches the partner tracker so both sides tell the same story.
STALE_AFTER_SECONDS = 180


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _traffic_level(age_seconds: int | None, speed_kmh: float) -> str:
    if age_seconds is not None and age_seconds > STALE_AFTER_SECONDS:
        return "UNKNOWN"
    if speed_kmh < 10:
        return "HEAVY"
    if speed_kmh < 25:
        return "MODERATE"
    return "LIGHT"


@router.get(
    "/me/cab-bookings/{cab_booking_number}/live-location",
    tags=["Customer Bookings"],
    summary="Live driver location for one of my cab bookings.",
)
async def customer_live_location(
    cab_booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Return the latest GPS point the driver app uploaded for this cab.

    Unassigned / not-yet-started bookings are not errors: the app shows a
    "waiting for driver" state, so the payload carries `tracking_active`,
    `paused` and a human message instead of a 404.
    """
    cust = await _resolve_customer(db, current_user["sub"])
    cb = (
        await db.execute(
            select(CabBooking)
            .join(MasterBooking, MasterBooking.id == CabBooking.master_booking_id)
            .where(
                func.upper(CabBooking.booking_number)
                == cab_booking_number.strip().upper(),
                MasterBooking.customer_id == cust.id,
            )
        )
    ).scalar_one_or_none()
    if cb is None:
        raise ResourceNotFoundException("Cab booking", cab_booking_number)

    assignment = (
        await db.execute(
            select(CabBookingAssignment)
            .where(CabBookingAssignment.cab_booking_id == cb.id)
            .order_by(CabBookingAssignment.assigned_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    driver = None
    vehicle = None
    if assignment is not None:
        if assignment.driver_id is not None:
            driver = (
                await db.execute(
                    select(Driver).where(Driver.id == assignment.driver_id)
                )
            ).scalar_one_or_none()
        if assignment.vehicle_id is not None:
            vehicle = (
                await db.execute(
                    select(Vehicle).where(Vehicle.id == assignment.vehicle_id)
                )
            ).scalar_one_or_none()

    point = (
        await db.execute(
            select(DriverTripPoint)
            .where(DriverTripPoint.cab_booking_id == cb.id)
            .order_by(DriverTripPoint.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    tracking_active = cb.booking_status in {"STARTED", "IN_PROGRESS"}
    driver_payload = {
        "name": driver.full_name if driver else None,
        "mobile": driver.mobile if driver else None,
    }
    base = {
        "booking_number": cb.booking_number,
        "trip_status": cb.booking_status,
        "tracking_active": tracking_active,
        "paused": cb.trip_paused_at is not None,
        "paused_seconds": int(cb.trip_paused_seconds or 0),
        "gps_distance_km": float(cb.gps_distance_km or 0),
        "odometer_distance_km": (
            float(cb.odometer_distance_km)
            if cb.odometer_distance_km is not None
            else None
        ),
        "driver": driver_payload,
        "vehicle_reg": vehicle.registration_number if vehicle else None,
    }

    if point is None:
        return success_response(
            "No location yet",
            {
                **base,
                "location": None,
                "traffic_level": "UNKNOWN",
                "message": (
                    "Your driver has not shared a location for this trip yet."
                    if assignment is not None
                    else "A driver has not been assigned to this trip yet."
                ),
            },
        )

    recorded = _aware(point.recorded_at)
    age_seconds = (
        max(0, int((datetime.now(timezone.utc) - recorded).total_seconds()))
        if recorded
        else None
    )
    speed_kmh = float(point.speed_mps or 0) * 3.6

    return success_response(
        "Live location",
        {
            **base,
            "location": {
                "latitude": float(point.latitude),
                "longitude": float(point.longitude),
                "accuracy_m": (
                    float(point.accuracy_m) if point.accuracy_m is not None else None
                ),
                "speed_kmh": round(speed_kmh, 1),
                "recorded_at": _iso(point.recorded_at),
                "age_seconds": age_seconds,
                "is_stale": (age_seconds or 0) > STALE_AFTER_SECONDS,
            },
            "traffic_level": _traffic_level(age_seconds, speed_kmh),
            "message": None,
        },
    )
