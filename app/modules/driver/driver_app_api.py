# ============================================================
# WAYTERO — PARTNER DRIVER APP API
# File: app/modules/driver/driver_app_api.py
# Prefix: /drivers  (registered separately from the partner-gated router)
# Doc Ref: 04_API_Documentation/13_DRIVER_APP_API.md
#          06_Backend_Architecture/11_DRIVER_APP_BACKEND.md
#
# The driver is a partner's employee, not a marketplace participant. Every
# endpoint resolves the Driver row from the JWT user, then checks that the
# booking's live assignment names that driver. Nothing here accepts a partner
# or booking id from the client as proof of ownership.
# ============================================================

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.booking.models import (
    CabBooking,
    CabBookingAssignment,
    DriverTripPoint,
    MasterBooking,
    TripDistanceReview,
)
from app.modules.admin.models import ApiIntegration
from app.modules.customer.models import Customer
from app.modules.driver.constants import DRIVER_PAUSE_AUTO_RESUME_SECONDS
from app.modules.driver.models import (
    Driver,
    DriverAvailability,
    DriverPerformanceSummary,
)
from app.modules.master.models import City
from app.modules.partner.models import Partner
from app.modules.vehicle.models import Vehicle
from app.infrastructure.email import send_customer_event_email
from app.shared.responses.base import success_response

router = APIRouter()

# A trip that came back from Google/odometer with this much disagreement is
# still completed, but the partner gets a review item. Both must trip so a
# 900 m difference on a 4 km trip (22%) is not treated like a 900 m difference
# on a 300 km trip (0.3%).
MISMATCH_MIN_KM = Decimal("3.00")
MISMATCH_MIN_PCT = Decimal("10.00")
OFFER_SECONDS = 60
MAX_ACCURACY_M = 150.0
MAX_JUMP_KM = 5.0


async def _email_customer_for_trip(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_id: int,
    event_type: str,
    context: dict,
) -> None:
    """Tell the customer about a driver action.

    Best-effort: the email engine itself checks EMAIL_ENABLED, SMTP
    credentials and whether the customer actually has an address. A trip
    action must never fail because email is off.
    """
    try:
        row = (
            await db.execute(
                text("SELECT customer_id FROM master_bookings WHERE id = :mb"),
                {"mb": master_booking_id},
            )
        ).first()
        if row is None or row[0] is None:
            return
        await send_customer_event_email(
            db,
            customer_id=int(row[0]),
            event_type=event_type,
            context=context,
            related_type="CAB_BOOKING",
            related_id=cab_booking_id,
        )
    except Exception:  # pragma: no cover — never break the driver action
        pass


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class AvailabilityRequest(BaseModel):
    status: str = Field(..., description="ONLINE | OFFLINE | BREAK")


class DeclineOfferRequest(BaseModel):
    reason_code: str = Field(
        ..., description="TOO_FAR | VEHICLE_ISSUE | PERSONAL | OTHER"
    )
    notes: Optional[str] = Field(None, max_length=500)


class StartTripRequest(BaseModel):
    start_km: float = Field(..., ge=0)
    start_photo_url: str = Field(..., min_length=8, max_length=1000)
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class CloseTripRequest(BaseModel):
    end_km: float = Field(..., ge=0)
    payment_mode: str = Field("CASH", description="CASH | ONLINE | WALLET")
    cash_collected: bool = True
    payment_reference: Optional[str] = Field(None, max_length=120)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    notes: Optional[str] = Field(None, max_length=500)


class PaymentQrRequest(BaseModel):
    end_km: float = Field(..., ge=0)


class LocationPoint(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy_m: Optional[float] = Field(None, ge=0)
    speed_mps: Optional[float] = Field(None, ge=0)
    recorded_at: Optional[datetime] = None


class LocationBatch(BaseModel):
    points: list[LocationPoint] = Field(..., min_length=1, max_length=200)


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _f(value) -> Optional[float]:
    return float(value) if value is not None else None


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. The server owns map distance; a client-supplied
    counter can be edited and cannot be reconstructed after the fact."""
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


async def _resolve_driver(db: AsyncSession, user_id: str) -> Driver:
    driver = (
        await db.execute(
            select(Driver).where(
                Driver.user_id == UUID(str(user_id)),
                Driver.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if driver is None:
        raise ResourceNotFoundException("Driver profile")
    return driver


def _ensure_eligible(driver: Driver) -> None:
    if (driver.status or "").upper() not in ("APPROVED", "ACTIVE"):
        raise PermissionDeniedException(
            message=(
                "Your driver profile is not active yet. Ask your partner to "
                "complete approval before accepting trips."
            ),
            code="DRIVER_NOT_ACTIVE",
        )


async def _availability(db: AsyncSession, driver_id: int) -> DriverAvailability:
    row = (
        await db.execute(
            select(DriverAvailability).where(DriverAvailability.driver_id == driver_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = DriverAvailability(driver_id=driver_id, availability_status="OFFLINE")
        db.add(row)
        await db.flush()
    return row


async def _expire_stale_offer(
    db: AsyncSession, cb: CabBooking, assignment: CabBookingAssignment
) -> bool:
    """Lazily expire an unanswered offer. Returns True when it expired.

    There is no always-on scheduler guarantee, so every driver read/action
    treats an expired PENDING offer as closed. The database index then frees
    the driver for the next assignment immediately.
    """
    if (
        assignment.offer_status != "PENDING"
        or assignment.offer_expires_at is None
        or _aware(assignment.offer_expires_at) >= _now()
    ):
        return False

    assignment.offer_status = "EXPIRED"
    assignment.closed_at = _now()
    assignment.close_reason = "OFFER_EXPIRED"
    if cb.booking_status == "DRIVER_ASSIGNED":
        cb.booking_status = "ASSIGNED"
        cb.needs_attention = "DRIVER_OFFER_EXPIRED"
    await db.flush()
    return True


async def _driver_cab(
    db: AsyncSession,
    driver: Driver,
    booking_number: str,
    *,
    require_live: bool = False,
) -> tuple[MasterBooking, CabBooking, CabBookingAssignment]:
    cb = (
        await db.execute(
            select(CabBooking)
            .options(selectinload(CabBooking.assignments))
            .where(
                func.upper(CabBooking.booking_number) == booking_number.strip().upper()
            )
        )
    ).scalar_one_or_none()
    if cb is None:
        raise ResourceNotFoundException("Booking", booking_number)

    mine = [a for a in (cb.assignments or []) if a.driver_id == driver.id]
    if not mine:
        raise PermissionDeniedException(
            message="This booking is not assigned to you.",
            code="BOOKING_NOT_YOURS",
        )
    assignment = max(mine, key=lambda a: (a.assigned_at or _now(), a.id))
    await _expire_stale_offer(db, cb, assignment)

    if require_live:
        if (
            assignment.closed_at is not None
            or assignment.offer_status != "ACCEPTED"
            or assignment.driver_accepted_at is None
        ):
            raise PermissionDeniedException(
                message="Accept this booking before starting the trip.",
                code="DRIVER_OFFER_NOT_ACCEPTED",
            )
        if cb.booking_status not in ("DRIVER_ASSIGNED", "STARTED"):
            raise BusinessException(
                message=(
                    f"This booking is '{cb.booking_status}' and cannot be "
                    "started from the driver app."
                ),
                code="BOOKING_NOT_STARTABLE",
            )

    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == cb.master_booking_id)
        )
    ).scalar_one_or_none()
    if mb is None:
        raise ResourceNotFoundException("Master booking")
    return mb, cb, assignment


def _booking_public(
    cb: CabBooking, mb: MasterBooking, assignment: CabBookingAssignment
) -> dict:
    """The only shape the driver app uses for lists and completed history.

    Deliberately excludes customer name/phone/email and the master booking
    total. A driver needs the trip, not the customer's account.
    """
    accepted = assignment.driver_accepted_at is not None
    now = _now()
    paused_at = _aware(cb.trip_paused_at)
    pickup_at = _aware(cb.pickup_datetime)
    pickup_overdue = (
        pickup_at is not None
        and pickup_at < now
        and cb.booking_status == "DRIVER_ASSIGNED"
    )
    return {
        "booking_number": cb.booking_number,
        "master_booking_number": mb.booking_number,
        "status": cb.booking_status,
        "offer_status": assignment.offer_status,
        "trip_type": cb.trip_type,
        "pickup_location": cb.pickup_location,
        "pickup_latitude": _f(cb.pickup_latitude),
        "pickup_longitude": _f(cb.pickup_longitude),
        "drop_location": cb.drop_location,
        "drop_latitude": _f(cb.drop_latitude),
        "drop_longitude": _f(cb.drop_longitude),
        "stops": cb.stops or [],
        "pickup_datetime": _iso(cb.pickup_datetime),
        # The app warns on screen when the scheduled pickup has passed. The
        # sweeper sends the matching push once per driver/booking.
        "pickup_overdue": pickup_overdue,
        "pickup_overdue_seconds": (
            int((now - pickup_at).total_seconds())
            if pickup_overdue and pickup_at is not None
            else 0
        ),
        "estimated_distance_km": _f(cb.estimated_distance),
        "estimated_amount": (
            _money(cb.estimated_amount) if cb.estimated_amount else None
        ),
        "final_amount": _money(cb.final_amount) if cb.final_amount else None,
        "actual_distance_km": _f(cb.actual_distance),
        "gps_distance_km": _f(cb.gps_distance_km),
        "odometer_distance_km": _f(cb.odometer_distance_km),
        "cash_to_collect": (
            _money(cb.cash_amount_due)
            if cb.cash_pending_at == "DRIVER" and cb.cash_amount_due is not None
            else 0.0
        ),
        "cash_pending_at": cb.cash_pending_at,
        "payment_mode": cb.payment_mode,
        "trip_start_km": _f(cb.trip_start_km),
        "trip_end_km": _f(cb.trip_end_km),
        "trip_started_at": _iso(cb.trip_started_at),
        "trip_ended_at": _iso(cb.trip_ended_at),
        "trip_paused_at": _iso(cb.trip_paused_at),
        "trip_paused_seconds": int(cb.trip_paused_seconds or 0),
        "trip_auto_resume_at": (
            _iso(paused_at + timedelta(seconds=DRIVER_PAUSE_AUTO_RESUME_SECONDS))
            if paused_at is not None
            else None
        ),
        "distance_review_status": cb.distance_review_status,
        "offer_expires_at": _iso(assignment.offer_expires_at),
        "accepted_at": _iso(assignment.driver_accepted_at),
        "can_accept": (
            assignment.offer_status == "PENDING"
            and assignment.closed_at is None
            and accepted is False
        ),
        "can_start": (
            assignment.offer_status == "ACCEPTED"
            and assignment.driver_accepted_at is not None
            and assignment.closed_at is None
            and cb.booking_status == "DRIVER_ASSIGNED"
        ),
        "can_pause": cb.booking_status == "STARTED" and cb.trip_paused_at is None,
        "can_resume": cb.booking_status == "STARTED" and cb.trip_paused_at is not None,
        "can_close": cb.booking_status == "STARTED",
    }


async def _customer_block(db: AsyncSession, mb: MasterBooking) -> dict:
    customer = (
        await db.execute(select(Customer).where(Customer.id == mb.customer_id))
    ).scalar_one_or_none()
    if customer is None:
        return {"name": "Customer", "mobile": None}
    from app.modules.auth.models.user import User

    mobile = (
        await db.execute(select(User.mobile_number).where(User.id == customer.user_id))
    ).scalar_one_or_none()
    return {
        "name": " ".join(filter(None, [customer.first_name, customer.last_name]))
        or "Customer",
        "mobile": mobile,
    }


def _is_terminal(status: str) -> bool:
    return (status or "").upper() in (
        "COMPLETED",
        "COMPLETED_PAYMENT_PENDING",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
        "REFUNDED",
    )


async def _notify_partner_updated(
    db: AsyncSession,
    *,
    mb: MasterBooking,
    cb: CabBooking,
    action: str,
    partner_id: int,
) -> None:
    try:
        from app.modules.notification.services.booking_notifications import (
            notify_booking_updated,
        )

        await notify_booking_updated(
            db,
            service_type="CAB",
            master_booking_id=mb.id,
            service_id=cb.id,
            booking_number=cb.booking_number,
            service_status=cb.booking_status,
            action=action,
            partner_ids=[partner_id],
        )
    except Exception:  # pragma: no cover - realtime is best effort
        pass


# ════════════════════════════════════════════════════════════════
# PROFILE / DASHBOARD
# ════════════════════════════════════════════════════════════════


@router.get("/me", summary="[Driver App] Driver profile and dashboard")
@router.get("/me/dashboard", include_in_schema=False)
async def driver_me(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    availability = await _availability(db, driver.id)
    partner_name = (
        await db.execute(
            select(Partner.business_name).where(Partner.id == driver.partner_id)
        )
    ).scalar_one_or_none()

    offers = (
        await db.execute(
            select(CabBookingAssignment, CabBooking)
            .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBookingAssignment.closed_at.is_(None),
                CabBookingAssignment.offer_status == "PENDING",
                CabBooking.booking_status == "DRIVER_ASSIGNED",
            )
            .order_by(CabBookingAssignment.assigned_at.desc())
        )
    ).all()
    offer_rows = []
    for assignment, cb in offers:
        if await _expire_stale_offer(db, cb, assignment):
            continue
        mb = (
            await db.execute(
                select(MasterBooking).where(MasterBooking.id == cb.master_booking_id)
            )
        ).scalar_one_or_none()
        if mb:
            offer_rows.append(_booking_public(cb, mb, assignment))

    # Current job: an accepted assignment is the driver's active job before
    # start as well as after it. Leaving DRIVER_ASSIGNED out made a job vanish
    # from Home once the driver tapped Accept, until the trip was started.
    active = (
        await db.execute(
            select(CabBookingAssignment, CabBooking)
            .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBookingAssignment.closed_at.is_(None),
                CabBookingAssignment.offer_status == "ACCEPTED",
                CabBooking.booking_status.in_(("DRIVER_ASSIGNED", "STARTED")),
            )
            .order_by(CabBookingAssignment.assigned_at.desc())
            .limit(1)
        )
    ).first()
    active_row = None
    if active:
        assignment, cb = active
        mb = (
            await db.execute(
                select(MasterBooking).where(MasterBooking.id == cb.master_booking_id)
            )
        ).scalar_one_or_none()
        if mb:
            active_row = _booking_public(cb, mb, assignment)

    today = _now().date()
    trips_today = (
        await db.execute(
            select(func.count(CabBooking.id))
            .join(
                CabBookingAssignment,
                CabBookingAssignment.cab_booking_id == CabBooking.id,
            )
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBooking.booking_status == "COMPLETED",
                func.date(CabBooking.trip_ended_at) == today,
            )
        )
    ).scalar() or 0
    km_today = (
        await db.execute(
            select(func.coalesce(func.sum(CabBooking.actual_distance), 0))
            .join(
                CabBookingAssignment,
                CabBookingAssignment.cab_booking_id == CabBooking.id,
            )
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBooking.booking_status == "COMPLETED",
                func.date(CabBooking.trip_ended_at) == today,
            )
        )
    ).scalar() or 0
    cash_in_hand = (
        await db.execute(
            select(func.coalesce(func.sum(CabBooking.cash_amount_due), 0))
            .join(
                CabBookingAssignment,
                CabBookingAssignment.cab_booking_id == CabBooking.id,
            )
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBooking.cash_pending_at == "DRIVER",
            )
        )
    ).scalar() or 0
    completed_total = (
        await db.execute(
            select(func.count(CabBooking.id))
            .join(
                CabBookingAssignment,
                CabBookingAssignment.cab_booking_id == CabBooking.id,
            )
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBooking.booking_status == "COMPLETED",
            )
        )
    ).scalar() or 0
    perf = (
        await db.execute(
            select(DriverPerformanceSummary).where(
                DriverPerformanceSummary.driver_id == driver.id
            )
        )
    ).scalar_one_or_none()

    from app.modules.driver.driver_tour_app_api import (
        tour_cash_total,
        tour_dashboard_rows,
    )
    from app.modules.tour.models import TourBooking

    tour_cash = await tour_cash_total(db, driver.id)
    cash_in_hand = Decimal(str(cash_in_hand or 0)) + tour_cash

    tour_trips_today = (
        await db.execute(
            select(func.count(TourBooking.id)).where(
                TourBooking.driver_id == driver.id,
                TourBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
                func.date(TourBooking.trip_ended_at) == today,
            )
        )
    ).scalar() or 0
    tour_km_today = (
        await db.execute(
            select(func.coalesce(func.sum(TourBooking.actual_distance), 0)).where(
                TourBooking.driver_id == driver.id,
                TourBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
                func.date(TourBooking.trip_ended_at) == today,
            )
        )
    ).scalar() or 0
    tour_completed_total = (
        await db.execute(
            select(func.count(TourBooking.id)).where(
                TourBooking.driver_id == driver.id,
                TourBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
            )
        )
    ).scalar() or 0
    trips_today = int(trips_today or 0) + int(tour_trips_today)
    km_today = Decimal(str(km_today or 0)) + Decimal(str(tour_km_today))
    completed_total = int(completed_total or 0) + int(tour_completed_total)
    active_tour_row, tour_offer_rows = await tour_dashboard_rows(db, driver)

    vehicle = (
        await db.execute(
            select(Vehicle)
            .join(CabBookingAssignment, CabBookingAssignment.vehicle_id == Vehicle.id)
            .where(
                CabBookingAssignment.driver_id == driver.id,
                CabBookingAssignment.closed_at.is_(None),
            )
            .order_by(CabBookingAssignment.assigned_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if vehicle is None:
        # Between trips the driver still drives the vehicle their partner last
        # put them in. Showing "No vehicle assigned" right after a completed
        # trip is misleading, so fall back to the most recent assignment.
        vehicle = (
            await db.execute(
                select(Vehicle)
                .join(
                    CabBookingAssignment,
                    CabBookingAssignment.vehicle_id == Vehicle.id,
                )
                .where(CabBookingAssignment.driver_id == driver.id)
                .order_by(CabBookingAssignment.assigned_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return success_response(
        "Driver profile",
        {
            "driver": {
                "id": driver.id,
                "driver_code": driver.driver_code,
                "full_name": driver.full_name,
                "mobile": driver.mobile,
                "email": driver.email,
                "status": driver.status,
                "partner_id": driver.partner_id,
                "partner_name": partner_name,
                "license_number": driver.license_number,
                "license_expiry_date": (
                    driver.license_expiry_date.isoformat()
                    if driver.license_expiry_date
                    else None
                ),
            },
            "availability": availability.availability_status,
            "vehicle": (
                {
                    "id": vehicle.id,
                    "registration_number": vehicle.registration_number,
                    "make": vehicle.vehicle_brand,
                    "model": vehicle.vehicle_model,
                    "status": vehicle.status,
                }
                if vehicle
                else None
            ),
            "stats": {
                "trips_today": int(trips_today),
                "km_today": _money(km_today),
                "cash_in_hand": _money(cash_in_hand),
                "completed_total": int(completed_total),
                "acceptance_rate": _f(perf.acceptance_rate) if perf else None,
                "average_rating": _f(perf.average_rating) if perf else None,
            },
            "offers": offer_rows,
            "active_booking": active_row,
            "tour_offers": tour_offer_rows,
            "active_tour": active_tour_row,
        },
    )


@router.patch("/me/availability", summary="[Driver App] Go online / offline / break")
async def update_driver_availability(
    payload: AvailabilityRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    _ensure_eligible(driver)
    status = payload.status.strip().upper()
    if status not in ("ONLINE", "OFFLINE", "BREAK"):
        raise ValidationException("Status must be ONLINE, OFFLINE or BREAK.")
    availability = await _availability(db, driver.id)
    if availability.availability_status == "ON_TRIP" and status != "ON_TRIP":
        raise BusinessException(
            "Finish or hand over the active trip before changing availability.",
            code="DRIVER_ON_TRIP",
        )
    availability.availability_status = status
    availability.last_online_at = (
        _now() if status == "ONLINE" else availability.last_online_at
    )
    return success_response("Availability updated", {"status": status})


# ════════════════════════════════════════════════════════════════
# BOOKINGS / OFFERS
# ════════════════════════════════════════════════════════════════


@router.get("/me/bookings", summary="[Driver App] My bookings")
async def list_my_bookings(
    scope: str = Query("active", pattern="^(active|history|offers)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    page_size: Optional[int] = Query(None, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    limit = page_size or limit
    driver = await _resolve_driver(db, current_user["sub"])
    stmt = (
        select(CabBookingAssignment, CabBooking, MasterBooking)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .join(MasterBooking, MasterBooking.id == CabBooking.master_booking_id)
        .where(CabBookingAssignment.driver_id == driver.id)
    )
    if scope == "offers":
        stmt = stmt.where(
            CabBookingAssignment.offer_status == "PENDING",
            CabBookingAssignment.closed_at.is_(None),
            CabBooking.booking_status == "DRIVER_ASSIGNED",
        )
    elif scope == "active":
        stmt = stmt.where(
            CabBookingAssignment.closed_at.is_(None),
            CabBookingAssignment.offer_status == "ACCEPTED",
            CabBooking.booking_status.in_(
                ("DRIVER_ASSIGNED", "STARTED", "COMPLETED_PAYMENT_PENDING")
            ),
        )
    else:
        stmt = stmt.where(
            or_(
                CabBooking.booking_status.in_(
                    (
                        "COMPLETED",
                        "COMPLETED_PAYMENT_PENDING",
                        "SETTLEMENT_PENDING",
                        "SETTLED",
                        "CANCELLED",
                        "REFUNDED",
                    )
                ),
                CabBookingAssignment.offer_status.in_(("DECLINED", "EXPIRED")),
            )
        )

    rows = (
        await db.execute(
            stmt.order_by(CabBookingAssignment.assigned_at.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
    ).all()
    items = []
    for assignment, cb, mb in rows:
        if await _expire_stale_offer(db, cb, assignment):
            continue
        items.append(_booking_public(cb, mb, assignment))
    return success_response("Bookings retrieved", {"items": items, "page": page})


@router.get("/me/bookings/{booking_number}", summary="[Driver App] Booking detail")
async def get_my_booking(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    mb, cb, assignment = await _driver_cab(db, driver, booking_number)
    payload = _booking_public(cb, mb, assignment)

    # Customer contact is released only after this driver has accepted and only
    # while the trip is live. Once terminal, the detail is a receipt, not a
    # customer record.
    if (
        assignment.driver_accepted_at is not None
        and assignment.closed_at is None
        and not _is_terminal(cb.booking_status)
    ):
        payload["customer"] = await _customer_block(db, mb)
    else:
        payload["customer"] = None

    city = (
        await db.execute(select(City).where(City.id == mb.city_id))
    ).scalar_one_or_none()
    payload["city_name"] = city.name if city else None
    payload["notes"] = cb.remarks if hasattr(cb, "remarks") else None
    payload["vehicle"] = None
    if assignment.vehicle_id:
        vehicle = (
            await db.execute(select(Vehicle).where(Vehicle.id == assignment.vehicle_id))
        ).scalar_one_or_none()
        if vehicle:
            payload["vehicle"] = {
                "id": vehicle.id,
                "registration_number": vehicle.registration_number,
                "make": vehicle.vehicle_brand,
                "model": vehicle.vehicle_model,
                "color": None,
            }
    return success_response("Booking detail", payload)


@router.post(
    "/me/bookings/{booking_number}/accept", summary="[Driver App] Accept offer"
)
async def accept_offer(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    _ensure_eligible(driver)
    mb, cb, assignment = await _driver_cab(db, driver, booking_number)

    if assignment.offer_status == "ACCEPTED" and assignment.driver_accepted_at:
        return success_response(
            "Offer already accepted", _booking_public(cb, mb, assignment)
        )
    if assignment.offer_status != "PENDING" or assignment.closed_at is not None:
        raise BusinessException(
            "This offer is no longer available. Refresh your bookings.",
            code="OFFER_NOT_OPEN",
        )
    if await _expire_stale_offer(db, cb, assignment):
        raise BusinessException(
            "This offer has expired. Ask your partner to assign it again.",
            code="OFFER_EXPIRED",
        )

    # One live assignment per driver. The partial unique index is the final
    # guard; this read gives the app a useful message before the constraint.
    other = (
        await db.execute(
            select(CabBookingAssignment.id).where(
                CabBookingAssignment.driver_id == driver.id,
                CabBookingAssignment.id != assignment.id,
                CabBookingAssignment.closed_at.is_(None),
                CabBookingAssignment.offer_status.in_(("PENDING", "ACCEPTED")),
            )
        )
    ).first()
    if other is not None:
        raise BusinessException(
            "You already have a live booking. Complete or release it before "
            "accepting another.",
            code="DRIVER_SINGLE_WINDOW",
        )

    now = _now()
    try:
        assignment.offer_status = "ACCEPTED"
        assignment.driver_accepted_at = now
        assignment.driver_declined_at = None
        assignment.driver_decline_reason = None
        assignment.acceptance_deadline = None
        cb.booking_status = "DRIVER_ASSIGNED"
        availability = await _availability(db, driver.id)
        availability.availability_status = "ONLINE"
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise BusinessException(
            "Another booking was accepted just before this one. Only one live "
            "booking is allowed per driver.",
            code="DRIVER_SINGLE_WINDOW",
        ) from exc

    from app.modules.booking.models import BookingTimeline

    db.add(
        BookingTimeline(
            master_booking_id=mb.id,
            event_type="DRIVER_ACCEPTED_OFFER",
            event_description=(
                f"Driver {driver.full_name} ({driver.driver_code}) accepted "
                f"cab {cb.booking_number}."
            ),
            event_timestamp=now,
        )
    )
    partner_user_notified = False
    try:
        from app.modules.notification.services.booking_notifications import (
            partner_driver_responded,
        )

        await partner_driver_responded(
            db,
            partner_id=driver.partner_id,
            master_booking_id=mb.id,
            cab_booking_number=cb.booking_number,
            decision="ACCEPTED",
            driver_name=driver.full_name,
        )
        partner_user_notified = True
    except Exception:  # pragma: no cover
        pass
    await _notify_partner_updated(
        db, mb=mb, cb=cb, action="DRIVER_ACCEPTED", partner_id=driver.partner_id
    )
    vehicle = None
    if assignment.vehicle_id is not None:
        vehicle = (
            await db.execute(select(Vehicle).where(Vehicle.id == assignment.vehicle_id))
        ).scalar_one_or_none()
    await _email_customer_for_trip(
        db,
        master_booking_id=mb.id,
        cab_booking_id=cb.id,
        event_type="driver_assigned",
        context={
            "service": "Cab",
            "booking_number": cb.booking_number,
            "message": (
                f"{driver.full_name} has accepted your booking and will be "
                "your driver. You can follow the trip in the app."
            ),
            "details": [
                ("Driver", driver.full_name),
                (
                    "Vehicle",
                    vehicle.registration_number if vehicle else None,
                ),
                ("Pickup", cb.pickup_location),
            ],
        },
    )
    return success_response(
        "Booking accepted. Start the trip when you reach the pickup point.",
        {
            **_booking_public(cb, mb, assignment),
            "partner_notified": partner_user_notified,
        },
    )


@router.post(
    "/me/bookings/{booking_number}/decline", summary="[Driver App] Decline offer"
)
async def decline_offer(
    booking_number: str,
    payload: DeclineOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    mb, cb, assignment = await _driver_cab(db, driver, booking_number)
    if assignment.offer_status != "PENDING" or assignment.closed_at is not None:
        raise BusinessException("This offer is no longer open.", code="OFFER_NOT_OPEN")
    if payload.reason_code not in ("TOO_FAR", "VEHICLE_ISSUE", "PERSONAL", "OTHER"):
        raise ValidationException("Choose a valid decline reason.")

    now = _now()
    assignment.offer_status = "DECLINED"
    assignment.driver_declined_at = now
    assignment.driver_decline_reason = payload.reason_code
    assignment.driver_decline_notes = payload.notes
    assignment.closed_at = now
    assignment.close_reason = "DRIVER_DECLINED"
    if cb.booking_status == "DRIVER_ASSIGNED":
        cb.booking_status = "ASSIGNED"
        cb.needs_attention = "DRIVER_DECLINED"
    await db.flush()

    from app.modules.booking.models import BookingTimeline

    db.add(
        BookingTimeline(
            master_booking_id=mb.id,
            event_type="DRIVER_DECLINED_OFFER",
            event_description=(
                f"Driver {driver.full_name} declined cab {cb.booking_number}: "
                f"{payload.reason_code}."
            ),
            event_timestamp=now,
        )
    )
    try:
        from app.modules.notification.services.booking_notifications import (
            partner_driver_responded,
        )

        await partner_driver_responded(
            db,
            partner_id=driver.partner_id,
            master_booking_id=mb.id,
            cab_booking_number=cb.booking_number,
            decision="DECLINED",
            driver_name=driver.full_name,
            reason_code=payload.reason_code,
        )
    except Exception:  # pragma: no cover
        pass
    await _notify_partner_updated(
        db, mb=mb, cb=cb, action="DRIVER_DECLINED", partner_id=driver.partner_id
    )
    return success_response(
        "Offer declined. Your partner can assign another driver.",
        {"booking_number": cb.booking_number},
    )


# ════════════════════════════════════════════════════════════════
# TRIP PROOF MEDIA
# ════════════════════════════════════════════════════════════════


def _cloudinary_sign(params: dict, api_secret: str) -> str:
    excluded = {"file", "api_key", "resource_type", "cloud_name"}
    sorted_str = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k not in excluded
    )
    return hashlib.sha1((sorted_str + api_secret).encode("utf-8")).hexdigest()


@router.post("/me/media", summary="[Driver App] Upload trip proof photo")
async def upload_trip_media(
    file: UploadFile = File(...),
    kind: str = Form("odometer"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Upload an odometer photo to the driver's own Cloudinary folder.

    The driver app must capture this before starting a trip, and the URL is
    stored on the cab booking so the partner can review the proof later.
    """
    driver = await _resolve_driver(db, current_user["sub"])
    integration = (
        await db.execute(
            select(ApiIntegration).where(
                ApiIntegration.service_type == "CLOUDINARY",
                ApiIntegration.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not integration or not integration.configuration:
        raise BusinessException(
            "Photo upload is not configured. Ask your partner to enable the "
            "Cloudinary integration.",
            code="MEDIA_NOT_CONFIGURED",
        )
    cfg = integration.configuration
    cloud_name = str(cfg.get("cloud_name") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    api_secret = str(cfg.get("api_secret") or "").strip()
    if not cloud_name or not api_key or not api_secret:
        raise BusinessException(
            "Photo upload is not fully configured.", code="MEDIA_NOT_CONFIGURED"
        )

    content = await file.read()
    if not content:
        raise ValidationException("The photo is empty.")
    if len(content) > 10 * 1024 * 1024:
        raise ValidationException("The photo must be smaller than 10 MB.")

    safe_kind = "start" if kind.lower() != "end" else "end"
    stamp = int(time.time())
    upload_params = {
        "timestamp": stamp,
        "folder": f"waytero/drivers/{driver.id}/trip-proof",
        "public_id": f"{safe_kind}_{stamp}",
    }
    upload_params["signature"] = _cloudinary_sign(upload_params, api_secret)
    upload_params["api_key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
                data=upload_params,
                files={
                    "file": (
                        file.filename or "odometer.jpg",
                        content,
                        file.content_type or "image/jpeg",
                    )
                },
            )
    except httpx.HTTPError as exc:
        raise BusinessException(
            "The photo could not be uploaded. Check your connection and retry.",
            code="MEDIA_UPLOAD_FAILED",
        ) from exc
    if response.status_code not in (200, 201):
        raise BusinessException(
            "The photo upload was refused. Try again or contact support.",
            code="MEDIA_UPLOAD_FAILED",
        )
    url = str(response.json().get("secure_url") or "").strip()
    if not url:
        raise BusinessException(
            "The photo upload did not return a URL.", code="MEDIA_UPLOAD_FAILED"
        )
    return success_response("Photo uploaded.", {"url": url, "kind": safe_kind})


# ════════════════════════════════════════════════════════════════
# TRIP EXECUTION
# ════════════════════════════════════════════════════════════════


@router.post("/me/bookings/{booking_number}/start", summary="[Driver App] Start trip")
async def start_trip(
    booking_number: str,
    payload: StartTripRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    _ensure_eligible(driver)
    mb, cb, assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    if cb.booking_status != "DRIVER_ASSIGNED":
        raise BusinessException(
            f"Trip cannot start while the booking is '{cb.booking_status}'.",
            code="BOOKING_NOT_STARTABLE",
        )
    if payload.start_km < 0:
        raise ValidationException("Start odometer cannot be negative.")

    now = _now()
    cb.trip_start_km = Decimal(str(payload.start_km))
    cb.trip_start_photo_url = payload.start_photo_url.strip()
    cb.trip_started_at = now
    cb.booking_status = "STARTED"
    cb.trip_paused_at = None
    cb.trip_paused_seconds = 0
    cb.gps_distance_km = Decimal("0.00")
    cb.odometer_distance_km = Decimal("0.00")
    cb.distance_mismatch_km = None
    cb.distance_mismatch_pct = None
    cb.distance_review_status = None
    cb.trip_last_lat = (
        Decimal(str(payload.latitude)) if payload.latitude is not None else None
    )
    cb.trip_last_lng = (
        Decimal(str(payload.longitude)) if payload.longitude is not None else None
    )
    cb.trip_last_gps_at = now if payload.latitude is not None else None
    assignment.km_at_assignment_start = cb.trip_start_km

    availability = await _availability(db, driver.id)
    availability.availability_status = "ON_TRIP"

    from app.modules.booking.services import breakdown as breakdown_service
    from app.modules.booking.models import BookingTimeline

    await breakdown_service.mark_resources_on_trip(
        db,
        vehicle_id=assignment.vehicle_id,
        driver_id=driver.id,
        on_trip=True,
    )
    if mb.booking_status not in ("COMPLETED", "CLOSED", "CANCELLED"):
        mb.booking_status = "IN_PROGRESS"
    db.add(
        BookingTimeline(
            master_booking_id=mb.id,
            event_type="TRIP_STARTED_BY_DRIVER",
            event_description=(
                f"Driver {driver.full_name} started {cb.booking_number}. "
                f"Start odometer: {payload.start_km:.1f} km. "
                "Odometer photo captured."
            ),
            event_timestamp=now,
        )
    )
    await _notify_partner_updated(
        db, mb=mb, cb=cb, action="DRIVER_START_TRIP", partner_id=driver.partner_id
    )
    await _email_customer_for_trip(
        db,
        master_booking_id=mb.id,
        cab_booking_id=cb.id,
        event_type="trip_started",
        context={
            "service": "Cab",
            "booking_number": cb.booking_number,
            "message": (
                "Your cab trip has started. Open the app to follow the "
                "driver's live location."
            ),
            "details": [
                ("Driver", driver.full_name),
                ("Pickup", cb.pickup_location),
            ],
        },
    )
    return success_response(
        "Trip started. Location tracking is now active.",
        {
            "booking_number": cb.booking_number,
            "status": cb.booking_status,
            "trip_start_km": payload.start_km,
            "trip_start_photo_url": cb.trip_start_photo_url,
            "trip_started_at": _iso(now),
            "tracking": True,
        },
    )


@router.post("/me/bookings/{booking_number}/pause", summary="[Driver App] Pause trip")
async def pause_trip(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    mb, cb, assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    if cb.booking_status != "STARTED":
        raise BusinessException(
            "Only a running trip can be paused.", code="TRIP_NOT_RUNNING"
        )
    if cb.trip_paused_at is not None:
        return success_response(
            "Trip is already paused", {"paused_at": _iso(cb.trip_paused_at)}
        )

    now = _now()
    cb.trip_paused_at = now
    availability = await _availability(db, driver.id)
    availability.availability_status = "BREAK"

    from app.modules.booking.models import BookingTimeline

    db.add(
        BookingTimeline(
            master_booking_id=mb.id,
            event_type="TRIP_PAUSED_BY_DRIVER",
            event_description=f"Driver paused {cb.booking_number}. Paused movement is excluded from map distance.",
            event_timestamp=now,
        )
    )
    await _notify_partner_updated(
        db, mb=mb, cb=cb, action="DRIVER_PAUSE_TRIP", partner_id=driver.partner_id
    )
    return success_response("Trip paused", {"paused_at": _iso(now)})


@router.post("/me/bookings/{booking_number}/resume", summary="[Driver App] Resume trip")
async def resume_trip(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    mb, cb, assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    if cb.booking_status != "STARTED":
        raise BusinessException(
            "Only a running trip can be resumed.", code="TRIP_NOT_RUNNING"
        )
    if cb.trip_paused_at is None:
        return success_response(
            "Trip is already running", {"paused_seconds": cb.trip_paused_seconds}
        )

    now = _now()
    paused_from = _aware(cb.trip_paused_at) or now
    elapsed = max(0, int((now - paused_from).total_seconds()))
    cb.trip_paused_seconds = int(cb.trip_paused_seconds or 0) + elapsed
    cb.trip_paused_at = None
    # Do not bridge the pause gap: the next accepted point starts a new segment.
    cb.trip_last_lat = None
    cb.trip_last_lng = None
    cb.trip_last_gps_at = None
    availability = await _availability(db, driver.id)
    availability.availability_status = "ON_TRIP"

    from app.modules.booking.models import BookingTimeline

    db.add(
        BookingTimeline(
            master_booking_id=mb.id,
            event_type="TRIP_RESUMED_BY_DRIVER",
            event_description=f"Driver resumed {cb.booking_number} after {elapsed}s pause.",
            event_timestamp=now,
        )
    )
    await _notify_partner_updated(
        db, mb=mb, cb=cb, action="DRIVER_RESUME_TRIP", partner_id=driver.partner_id
    )
    return success_response(
        "Trip resumed",
        {"resumed_at": _iso(now), "paused_seconds": cb.trip_paused_seconds},
    )


@router.post(
    "/me/bookings/{booking_number}/location", summary="[Driver App] Upload GPS batch"
)
async def upload_locations(
    booking_number: str,
    payload: LocationBatch,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    mb, cb, assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    if cb.booking_status != "STARTED":
        raise BusinessException(
            "Location is accepted only while the trip is running.",
            code="TRIP_NOT_RUNNING",
        )

    paused = cb.trip_paused_at is not None
    distance = Decimal(str(cb.gps_distance_km or 0))
    last_lat = _f(cb.trip_last_lat)
    last_lng = _f(cb.trip_last_lng)
    last_at = _aware(cb.trip_last_gps_at)
    accepted = 0
    rejected = 0

    for point in sorted(
        payload.points,
        key=lambda p: _aware(p.recorded_at) or _now(),
    ):
        recorded_at = _aware(point.recorded_at) or _now()
        if point.accuracy_m is not None and point.accuracy_m > MAX_ACCURACY_M:
            rejected += 1
            continue
        if recorded_at > _now() + timedelta(minutes=2):
            recorded_at = _now()
        if last_at is not None and recorded_at <= last_at:
            rejected += 1
            continue

        step = 0.0
        if not paused and last_lat is not None and last_lng is not None:
            step = _haversine_km(last_lat, last_lng, point.latitude, point.longitude)
            # A single bad fix must not add an airport run to the map distance.
            if step > MAX_JUMP_KM:
                rejected += 1
                step = 0.0
                # Resynchronise on this point rather than repeatedly comparing
                # every later point against the bad one.
                last_lat, last_lng = point.latitude, point.longitude
                last_at = recorded_at
                cb.trip_last_lat = Decimal(str(point.latitude))
                cb.trip_last_lng = Decimal(str(point.longitude))
                cb.trip_last_gps_at = recorded_at
                continue
            # GPS noise under ~8 m should not accumulate into billable km.
            if step < 0.008:
                step = 0.0

        if not paused:
            distance += Decimal(str(step))
            last_lat, last_lng = point.latitude, point.longitude
            last_at = recorded_at
            cb.trip_last_lat = Decimal(str(point.latitude))
            cb.trip_last_lng = Decimal(str(point.longitude))
            cb.trip_last_gps_at = recorded_at

        db.add(
            DriverTripPoint(
                cab_booking_id=cb.id,
                assignment_id=assignment.id,
                driver_id=driver.id,
                latitude=Decimal(str(point.latitude)),
                longitude=Decimal(str(point.longitude)),
                accuracy_m=(
                    Decimal(str(point.accuracy_m))
                    if point.accuracy_m is not None
                    else None
                ),
                speed_mps=(
                    Decimal(str(point.speed_mps))
                    if point.speed_mps is not None
                    else None
                ),
                is_paused=paused,
                recorded_at=recorded_at,
            )
        )
        accepted += 1

    cb.gps_distance_km = distance.quantize(Decimal("0.01"))
    await db.flush()
    return success_response(
        "Location batch stored",
        {
            "accepted": accepted,
            "rejected": rejected,
            "paused": paused,
            "gps_distance_km": _money(cb.gps_distance_km),
            "last_location": {
                "latitude": _f(cb.trip_last_lat),
                "longitude": _f(cb.trip_last_lng),
                "recorded_at": _iso(cb.trip_last_gps_at),
            },
        },
    )


@router.get(
    "/me/bookings/{booking_number}/close-preview",
    summary="[Driver App] Preview the fare before closing",
)
async def close_preview(
    booking_number: str,
    end_km: float = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    from app.modules.admin.services import (
        CommissionService,
        DefaultPricingService,
    )
    from app.modules.booking import services as advance_service
    from app.modules.booking.services.fare import calculate_fare, trip_days_from
    from app.modules.booking.services.settlement_math import (
        compute_close_economics,
        money,
    )
    from app.modules.admin.models import SystemConfiguration

    driver = await _resolve_driver(db, current_user["sub"])
    _mb, cb, _assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    if cb.booking_status != "STARTED":
        raise BusinessException(
            "A fare preview is only available while the trip is running.",
            code="TRIP_NOT_RUNNING",
        )
    start = _f(cb.trip_start_km)
    if start is None:
        raise BusinessException(
            "Trip start odometer is missing.", code="TRIP_START_MISSING"
        )
    if end_km < start:
        raise ValidationException("End odometer cannot be less than the start reading.")

    odometer_km = Decimal(str(end_km)) - Decimal(str(start))
    gps_km = Decimal(str(cb.gps_distance_km or 0))
    calculated = None
    rule = None
    if cb.vehicle_category_id and _mb.city_id and cb.trip_type:
        rule = await DefaultPricingService.get_effective_rule(
            db, _mb.city_id, cb.vehicle_category_id, cb.trip_type
        )
        if rule:
            calculated = calculate_fare(
                rule,
                odometer_km,
                start_dt=cb.trip_started_at,
                end_dt=_now(),
                trip_type=cb.trip_type,
                trip_days=trip_days_from(cb.pickup_datetime, cb.return_datetime),
            )
    if calculated is None or calculated <= 0:
        calculated = Decimal(str(cb.final_amount or cb.estimated_amount or 0))
    if calculated <= 0:
        raise BusinessException(
            "No pricing rule could produce a fare preview.", code="FARE_RULE_MISSING"
        )

    partner = (
        await db.execute(select(Partner).where(Partner.id == driver.partner_id))
    ).scalar_one_or_none()
    commission_rule = await CommissionService.resolve(
        db,
        city_id=partner.city_id if partner else None,
        service_type="CAB",
        for_date=_now().date(),
        partner_id=driver.partner_id,
    )
    gst_rows = (
        (
            await db.execute(
                select(SystemConfiguration).where(
                    SystemConfiguration.config_key.in_(["GST_ENABLED", "GST_RATE"])
                )
            )
        )
        .scalars()
        .all()
    )
    gst_cfg = {r.config_key: (r.config_value or "").strip() for r in gst_rows}
    gst_enabled = gst_cfg.get("GST_ENABLED", "false").lower() == "true"
    econ = compute_close_economics(
        final_amount=money(calculated),
        commission=commission_rule,
        gst_enabled=gst_enabled,
        gst_rate_percent=gst_cfg.get("GST_RATE", "5"),
    )
    advance_paid = Decimal(
        str(await advance_service.get_advance_amount(db, cb.id) or 0)
    )
    coupon_discount = money(cb.coupon_discount)
    balance_due = advance_service.compute_balance_due(
        final_amount=econ.final_amount,
        coupon_discount=coupon_discount,
        advance_paid=advance_paid,
        gst_amount=econ.gst_amount,
        is_tax_invoice=gst_enabled,
    )
    return success_response(
        "Fare preview",
        {
            "booking_number": cb.booking_number,
            "odometer_distance_km": _money(odometer_km),
            "gps_distance_km": _money(gps_km),
            "difference_km": _money(abs(odometer_km - gps_km)),
            "estimated_final_amount": _money(econ.final_amount),
            "cash_to_collect": _money(balance_due),
            "advance_paid": _money(advance_paid),
            "coupon_discount": _money(coupon_discount),
            "gst_amount": _money(econ.gst_amount),
            "gst_enabled": gst_enabled,
            "partner_payout": _money(econ.partner_payout),
            "platform_commission": _money(econ.platform_commission),
            "pricing_source": rule.get("source") if rule else "BOOKED_ESTIMATE",
        },
    )


@router.post(
    "/me/bookings/{booking_number}/payment-qr",
    summary="[Driver App] Create UPI QR for trip balance",
)
async def create_payment_qr(
    booking_number: str,
    payload: PaymentQrRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Create a fixed-amount, single-use Razorpay UPI QR for the balance.

    The fare comes from the server-side close preview, never from the client, so
    the QR amount is the same amount the close endpoint will insist was paid.
    """
    from app.modules.payment.services import PaymentGatewayError, create_booking_qr

    quote = await close_preview(
        booking_number, payload.end_km, db=db, current_user=current_user
    )
    data = quote["data"]
    amount = Decimal(str(data.get("cash_to_collect") or 0))
    if amount <= 0:
        raise BusinessException(
            "The advance already covers this trip; there is no balance to collect.",
            code="NO_BALANCE_DUE",
        )

    driver = await _resolve_driver(db, current_user["sub"])
    _mb, cb, _assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    try:
        qr = await create_booking_qr(
            db,
            master_booking_id=cb.master_booking_id,
            cab_booking_id=cb.id,
            booking_number=cb.booking_number,
            amount=amount,
            description=f"WayTero trip balance {cb.booking_number}",
        )
    except PaymentGatewayError as exc:
        raise BusinessException(
            f"The payment QR could not be created: {exc}",
            code="PAYMENT_GATEWAY_ERROR",
        ) from exc
    qr["booking_number"] = cb.booking_number
    qr["preview"] = data
    return success_response("Scan this QR to pay the trip balance.", qr)


@router.get(
    "/me/bookings/{booking_number}/payment-qr/{qr_reference}",
    summary="[Driver App] Check UPI QR payment status",
)
async def payment_qr_status(
    booking_number: str,
    qr_reference: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Ask Razorpay whether the customer's QR payment has landed."""
    from app.modules.payment.services import PaymentGatewayError, fetch_booking_qr

    driver = await _resolve_driver(db, current_user["sub"])
    _mb, cb, _assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    try:
        status = await fetch_booking_qr(db, qr_reference)
    except PaymentGatewayError as exc:
        raise BusinessException(
            f"The payment status could not be checked: {exc}",
            code="PAYMENT_GATEWAY_ERROR",
        ) from exc
    if int(status["master_booking_id"]) != int(cb.master_booking_id) or (
        status.get("cab_booking_id") is not None
        and int(status["cab_booking_id"]) != int(cb.id)
    ):
        raise ResourceNotFoundException("Payment QR")
    status["booking_number"] = cb.booking_number
    return success_response("Payment status", status)


@router.post("/me/bookings/{booking_number}/close", summary="[Driver App] Close trip")
async def close_trip(
    booking_number: str,
    payload: CloseTripRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.services import (
        CommissionService,
        DefaultPricingService,
    )
    from app.modules.booking import services as advance_service
    from app.modules.booking.models import BookingTimeline
    from app.modules.booking.services import payments as payments_service
    from app.modules.booking.services import breakdown as breakdown_service
    from app.modules.booking.services.fare import calculate_fare, trip_days_from
    from app.modules.booking.services.settlement_math import (
        commission_snapshot,
        compute_close_economics,
        money,
    )

    driver = await _resolve_driver(db, current_user["sub"])
    mb, cb, assignment = await _driver_cab(
        db, driver, booking_number, require_live=True
    )
    if cb.booking_status != "STARTED":
        raise BusinessException(
            f"Cannot close trip while the booking is '{cb.booking_status}'.",
            code="TRIP_NOT_RUNNING",
        )
    start_km = _f(cb.trip_start_km)
    if start_km is None:
        raise BusinessException(
            "Trip start odometer is missing.", code="TRIP_START_MISSING"
        )
    if payload.end_km < start_km:
        raise ValidationException(
            f"End odometer ({payload.end_km:.1f}) cannot be less than start "
            f"({start_km:.1f})."
        )

    now = _now()
    if cb.trip_paused_at is not None:
        paused_from = _aware(cb.trip_paused_at) or now
        cb.trip_paused_seconds = int(cb.trip_paused_seconds or 0) + max(
            0, int((now - paused_from).total_seconds())
        )
        cb.trip_paused_at = None

    odometer_km = Decimal(str(payload.end_km)) - Decimal(str(start_km))
    odometer_km = max(Decimal("0.00"), odometer_km)
    gps_km = Decimal(str(cb.gps_distance_km or 0))
    mismatch_km = abs(odometer_km - gps_km) if gps_km > 0 else None
    mismatch_pct = (
        (mismatch_km / odometer_km * 100)
        if mismatch_km is not None and odometer_km > 0
        else None
    )
    needs_review = (
        mismatch_km is not None
        and mismatch_km >= MISMATCH_MIN_KM
        and (mismatch_pct or 0) >= MISMATCH_MIN_PCT
    )
    cb.odometer_distance_km = odometer_km.quantize(Decimal("0.01"))
    cb.distance_mismatch_km = (
        mismatch_km.quantize(Decimal("0.01")) if mismatch_km is not None else None
    )
    cb.distance_mismatch_pct = (
        mismatch_pct.quantize(Decimal("0.01")) if mismatch_pct is not None else None
    )
    cb.distance_review_status = (
        "PENDING_REVIEW"
        if needs_review
        else ("MATCHED" if gps_km > 0 else "NO_GPS_DATA")
    )

    # Server-authoritative fare: the driver confirms the odometer, not the price.
    calculated_fare = None
    fare_warning = None
    fare_rule = None
    if cb.vehicle_category_id and mb.city_id and cb.trip_type:
        effective = await DefaultPricingService.get_effective_rule(
            db, mb.city_id, cb.vehicle_category_id, cb.trip_type
        )
        if effective:
            fare_rule = effective
            calculated_fare = calculate_fare(
                effective,
                odometer_km,
                start_dt=cb.trip_started_at,
                end_dt=now,
                trip_type=cb.trip_type,
                trip_days=trip_days_from(cb.pickup_datetime, cb.return_datetime),
            )
            if calculated_fare <= 0:
                calculated_fare = None

    if calculated_fare is not None:
        final_dec = money(calculated_fare)
    else:
        final_dec = money(cb.final_amount or cb.estimated_amount or 0)
        if final_dec <= 0:
            raise BusinessException(
                "The fare could not be calculated for this trip. Ask your "
                "partner to add the pricing rule, then close the trip.",
                code="FARE_RULE_MISSING",
            )
        fare_warning = (
            "No live pricing rule matched this trip; the booked estimate was "
            "used as the fare. Your partner can review it."
        )

    partner = (
        await db.execute(select(Partner).where(Partner.id == driver.partner_id))
    ).scalar_one_or_none()
    commission_rule = await CommissionService.resolve(
        db,
        city_id=partner.city_id if partner else None,
        service_type="CAB",
        for_date=now.date(),
        partner_id=driver.partner_id,
    )
    gst_rows = (
        (
            await db.execute(
                select(SystemConfiguration).where(
                    SystemConfiguration.config_key.in_(["GST_ENABLED", "GST_RATE"])
                )
            )
        )
        .scalars()
        .all()
    )
    gst_cfg = {r.config_key: (r.config_value or "").strip() for r in gst_rows}
    gst_enabled = gst_cfg.get("GST_ENABLED", "false").lower() == "true"
    econ = compute_close_economics(
        final_amount=final_dec,
        commission=commission_rule,
        gst_enabled=gst_enabled,
        gst_rate_percent=gst_cfg.get("GST_RATE", "5"),
    )

    advance_paid = Decimal(
        str(await advance_service.get_advance_amount(db, cb.id) or 0)
    )
    coupon_discount = money(cb.coupon_discount)
    balance_due = advance_service.compute_balance_due(
        final_amount=econ.final_amount,
        coupon_discount=coupon_discount,
        advance_paid=advance_paid,
        gst_amount=econ.gst_amount,
        is_tax_invoice=gst_enabled,
    )

    mode = payload.payment_mode.strip().upper()
    if mode not in ("CASH", "ONLINE"):
        raise ValidationException("payment_mode must be CASH or ONLINE.")

    # ONLINE is not a label the driver may choose on trust. The money must have
    # landed on the Razorpay QR this trip generated, and it must cover exactly
    # the server-calculated balance, before the booking is allowed to close.
    payment_reference = (payload.payment_reference or "").strip()
    if mode == "ONLINE":
        if not payment_reference:
            raise ValidationException(
                "Generate and complete the UPI QR payment before closing online."
            )
        from app.modules.payment.services import PaymentGatewayError, fetch_booking_qr

        try:
            qr_status = await fetch_booking_qr(db, payment_reference)
        except PaymentGatewayError as exc:
            raise BusinessException(
                f"The online payment could not be verified: {exc}",
                code="PAYMENT_GATEWAY_ERROR",
            ) from exc
        if int(qr_status["master_booking_id"]) != int(mb.id) or (
            qr_status.get("cab_booking_id") is not None
            and int(qr_status["cab_booking_id"]) != int(cb.id)
        ):
            raise BusinessException(
                "This payment QR does not belong to this trip.",
                code="PAYMENT_QR_MISMATCH",
            )
        if Decimal(str(qr_status["paid_amount"])) < Decimal(str(balance_due)):
            raise BusinessException(
                "The online payment has not been received yet. Wait for the "
                "customer to pay, then check again.",
                code="ONLINE_PAYMENT_PENDING",
            )
    else:
        from app.modules.payment.services import cancel_open_booking_links

        # Cancel first: a link that was just paid must be discovered as PAID by
        # the cancellation check, not hidden behind an early cash close.
        await cancel_open_booking_links(db, cb.id)
        already_paid = (
            await db.execute(
                text(
                    "SELECT 1 FROM payment_qr_codes "
                    "WHERE cab_booking_id = :cab AND status = 'PAID' LIMIT 1"
                ),
                {"cab": cb.id},
            )
        ).scalar_one_or_none()
        if already_paid is not None:
            raise BusinessException(
                "An online payment has already been received for this trip; "
                "close it as ONLINE instead of cash.",
                code="ONLINE_PAYMENT_ALREADY_RECEIVED",
            )

    cb.trip_end_km = Decimal(str(payload.end_km))
    cb.trip_ended_at = now
    cb.actual_distance = odometer_km
    cb.final_amount = econ.final_amount
    cb.platform_commission = econ.platform_commission
    cb.partner_payout = econ.partner_payout
    cb.gst_rate = econ.gst_rate_pct
    cb.gst_amount = econ.gst_amount
    cb.is_tax_invoice = gst_enabled
    cb.payment_mode = mode
    cb.booking_status = "COMPLETED"
    if mode == "CASH" and payload.cash_collected and balance_due > 0:
        cb.cash_pending_at = "DRIVER"
        cb.cash_amount_due = balance_due
        cb.payment_collected_by = "DRIVER"
    else:
        cb.cash_pending_at = "NONE"
        cb.cash_amount_due = Decimal("0.00")
        cb.payment_collected_by = "RAZORPAY" if mode == "ONLINE" else None
    cb.commission_config_snapshot = commission_snapshot(
        econ,
        fare_close_check=(
            None
            if fare_rule is None
            else {
                "rule_id": fare_rule.get("rule_id"),
                "rule_table": fare_rule.get("rule_table"),
                "city_id": fare_rule.get("city_id"),
                "pricing_source": fare_rule.get("source"),
                "calculated_fare": str(calculated_fare) if calculated_fare else None,
                "accepted_final_amount": str(econ.final_amount),
            }
        ),
    )
    mb.total_amount = econ.final_amount
    mb.booking_status = "COMPLETED"

    closed_id = await breakdown_service.close_assignment_on_trip_complete(
        db, cab_booking_id=cb.id, end_km=cb.trip_end_km
    )
    if closed_id is not None:
        await breakdown_service.mark_resources_on_trip(
            db,
            vehicle_id=assignment.vehicle_id,
            driver_id=driver.id,
            on_trip=False,
        )
    availability = await _availability(db, driver.id)
    availability.availability_status = "ONLINE"

    if needs_review:
        existing = (
            await db.execute(
                select(TripDistanceReview).where(
                    TripDistanceReview.cab_booking_id == cb.id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                TripDistanceReview(
                    cab_booking_id=cb.id,
                    assignment_id=assignment.id,
                    driver_id=driver.id,
                    odometer_km=odometer_km,
                    gps_km=gps_km,
                    difference_km=mismatch_km or Decimal("0"),
                    difference_pct=mismatch_pct or Decimal("0"),
                    status="PENDING_REVIEW",
                )
            )
        await db.flush()
        await payments_service.derive_master_payment_status(db, mb.id)
        await db.refresh(mb)

    db.add(
        BookingTimeline(
            master_booking_id=mb.id,
            event_type="TRIP_CLOSED_BY_DRIVER",
            event_description=(
                f"Driver {driver.full_name} closed {cb.booking_number}. "
                f"Odometer: {odometer_km:.1f} km | Map: {gps_km:.1f} km | "
                f"Final: ₹{econ.final_amount:.2f} | Mode: {mode} | "
                f"Distance review: {cb.distance_review_status}."
            ),
            event_timestamp=now,
        )
    )
    await db.flush()
    if mode == "ONLINE" and balance_due > 0:
        await payments_service.record_final_payment(
            db,
            master_booking_id=mb.id,
            cab_booking_id=cb.id,
            amount=balance_due,
            payment_mode="ONLINE",
            received_by="RAZORPAY",
            collected_by_role="CUSTOMER",
            reference_note=f"Razorpay UPI QR {payment_reference}",
        )
    await payments_service.derive_master_payment_status(db, mb.id)
    await _notify_partner_updated(
        db, mb=mb, cb=cb, action="DRIVER_CLOSE_TRIP", partner_id=driver.partner_id
    )
    await _email_customer_for_trip(
        db,
        master_booking_id=mb.id,
        cab_booking_id=cb.id,
        event_type="trip_completed",
        context={
            "service": "Cab",
            "booking_number": cb.booking_number,
            "amount": _money(econ.final_amount),
            "amount_label": "Trip total",
            "message": (
                "Your trip is complete. Your invoice is available in the app "
                "once payment is recorded."
            ),
            "details": [
                ("Driver", driver.full_name),
                ("Payment mode", mode),
            ],
        },
    )

    warnings = []
    if needs_review:
        warnings.append(
            f"Map distance ({gps_km:.1f} km) differs from the odometer "
            f"({odometer_km:.1f} km) by {mismatch_km:.1f} km. Your partner "
            "will review it; the trip is complete."
        )
    elif gps_km <= 0:
        warnings.append(
            "No map distance was recorded. The trip is complete, but your "
            "partner may ask about it."
        )
    if fare_warning:
        warnings.append(fare_warning)

    return success_response(
        "Trip completed." + (" " + " ".join(warnings) if warnings else ""),
        {
            "booking_number": cb.booking_number,
            "status": "COMPLETED",
            "trip_end_km": payload.end_km,
            "odometer_distance_km": _money(odometer_km),
            "gps_distance_km": _money(gps_km),
            "distance_mismatch_km": (
                _money(mismatch_km) if mismatch_km is not None else None
            ),
            "distance_mismatch_pct": _f(mismatch_pct),
            "distance_review_status": cb.distance_review_status,
            "final_amount": _money(econ.final_amount),
            "cash_to_collect": (
                _money(balance_due) if cb.cash_pending_at == "DRIVER" else 0.0
            ),
            "cash_pending_at": cb.cash_pending_at,
            "payment_mode": mode,
            "paused_seconds": int(cb.trip_paused_seconds or 0),
            "warnings": warnings,
            "partner_payout": _money(econ.partner_payout),
            "platform_commission": _money(econ.platform_commission),
        },
    )


# ════════════════════════════════════════════════════════════════
# CASH
# ════════════════════════════════════════════════════════════════


@router.get("/me/cash", summary="[Driver App] Cash currently in my hand")
async def my_cash(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    rows = (
        (
            await db.execute(
                select(CabBooking)
                .join(
                    CabBookingAssignment,
                    CabBookingAssignment.cab_booking_id == CabBooking.id,
                )
                .where(
                    CabBookingAssignment.driver_id == driver.id,
                    CabBooking.cash_pending_at == "DRIVER",
                )
                .order_by(CabBooking.trip_ended_at.desc())
            )
        )
        .scalars()
        .all()
    )
    from app.modules.tour.models import TourBooking, TourPackage

    tour_rows = (
        await db.execute(
            select(TourBooking, TourPackage)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .where(
                TourBooking.driver_id == driver.id,
                TourBooking.cash_pending_at == "DRIVER",
            )
            .order_by(TourBooking.trip_ended_at.desc())
        )
    ).all()
    cab_items = [
        {
            "service": "CAB",
            "booking_number": cb.booking_number,
            "trip_ended_at": _iso(cb.trip_ended_at),
            "pickup_location": cb.pickup_location,
            "drop_location": cb.drop_location,
            "cash_amount": _money(cb.cash_amount_due),
            "final_amount": _money(cb.final_amount),
        }
        for cb in rows
    ]
    tour_items = [
        {
            "service": "TOUR",
            "booking_number": tour.booking_number,
            "trip_ended_at": _iso(tour.trip_ended_at),
            "pickup_location": tour.pickup_location,
            "drop_location": package.destination,
            "cash_amount": _money(tour.cash_amount_due),
            "final_amount": _money(tour.payable_amount),
        }
        for tour, package in tour_rows
    ]
    items = sorted(
        cab_items + tour_items,
        key=lambda item: item.get("trip_ended_at") or "",
        reverse=True,
    )
    total = sum((r.cash_amount_due or 0) for r in rows) + sum(
        (tour.cash_amount_due or 0) for tour, _ in tour_rows
    )
    return success_response(
        "Cash in hand",
        {"total": _money(total), "items": items},
    )
