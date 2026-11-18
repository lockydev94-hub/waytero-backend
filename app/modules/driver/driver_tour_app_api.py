# ============================================================
# WAYTERO — DRIVER APP TOUR EXECUTION API
# File: app/modules/driver/driver_tour_app_api.py
# Prefix: /drivers/me/tours
# Doc Ref: 04_API_Documentation/13_DRIVER_APP_API.md
#          BRD Part 5 §115–§116 (tour start / in progress / completion)
#
# Tour packages are sold at a fixed price, so the driver never calculates a
# fare. The driver owns execution: start with the hand-over odometer, close
# with the drop-off odometer, collect the balance that the platform advance
# did not cover, and hand the cash/UPI custody to the partner for settlement.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.booking.models import BookingTimeline, MasterBooking
from app.modules.driver.models import Driver, DriverAvailability
from app.modules.tour.models import (
    TourAdvancePayment,
    TourBooking,
    TourItinerary,
    TourPackage,
)
from app.modules.vehicle.models import Vehicle
from app.infrastructure.email import send_customer_event_email
from app.shared.responses.base import success_response

router = APIRouter()

DRIVER_TOUR_ACTIVE = "IN_PROGRESS"
DRIVER_TOUR_ASSIGNED = "CONFIRMED"
DRIVER_TOUR_TERMINAL = ("COMPLETED", "SETTLED", "CANCELLED")


async def _email_customer_for_tour(
    db: AsyncSession,
    *,
    master_booking_id: int,
    tour_booking_id: int,
    event_type: str,
    context: dict,
) -> None:
    """Best-effort customer email for a driver-run tour action.

    No address, email disabled or SMTP down all mean "no email"; the tour
    action itself must never fail because of it.
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
            related_type="TOUR_BOOKING",
            related_id=tour_booking_id,
        )
    except Exception:  # pragma: no cover — never break the tour action
        pass


class StartTourRequest(BaseModel):
    start_km: float = Field(..., gt=0, description="Odometer at trip hand-over")
    notes: Optional[str] = Field(None, max_length=500)


class CloseTourRequest(BaseModel):
    end_km: float = Field(..., ge=0, description="Odometer at drop-off")
    payment_mode: str = Field("CASH", description="CASH | UPI")
    payment_collected: bool = True
    reference_number: Optional[str] = Field(None, max_length=120)
    notes: Optional[str] = Field(None, max_length=500)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _f(value) -> Optional[float]:
    return float(value) if value is not None else None


def _money(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


async def _driver_tour(
    db: AsyncSession,
    driver: Driver,
    booking_number: str,
) -> tuple[TourBooking, TourPackage, MasterBooking]:
    """Resolve a tour by number through the driver's live assignment.

    The JWT only proves who the driver is. Ownership is the tour's driver_id
    plus the package belonging to the driver's partner, never a client id.
    """
    row = (
        await db.execute(
            select(TourBooking, TourPackage, MasterBooking)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
            .where(
                TourBooking.booking_number == booking_number,
                TourBooking.driver_id == driver.id,
                TourPackage.partner_id == driver.partner_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ResourceNotFoundException("Tour booking")
    return row


async def _lock_tour(db: AsyncSession, booking: TourBooking) -> TourBooking:
    """Serialize driver start/close so a double tap cannot double-collect."""
    return (
        await db.execute(
            select(TourBooking).where(TourBooking.id == booking.id).with_for_update()
        )
    ).scalar_one()


async def _vehicle_block(db: AsyncSession, vehicle_id: Optional[int]) -> Optional[dict]:
    if not vehicle_id:
        return None
    vehicle = (
        await db.execute(select(Vehicle).where(Vehicle.id == vehicle_id))
    ).scalar_one_or_none()
    if vehicle is None:
        return None
    return {
        "id": vehicle.id,
        "registration_number": vehicle.registration_number,
        "make": vehicle.vehicle_brand,
        "model": vehicle.vehicle_model,
        "seats": vehicle.seating_capacity,
    }


async def _customer_block(db: AsyncSession, master: MasterBooking) -> dict:
    from app.modules.auth.models.user import User
    from app.modules.customer.models import Customer

    customer = (
        await db.execute(select(Customer).where(Customer.id == master.customer_id))
    ).scalar_one_or_none()
    if customer is None:
        return {"name": "Customer", "mobile": None}
    mobile = (
        await db.execute(select(User.mobile_number).where(User.id == customer.user_id))
    ).scalar_one_or_none()
    return {
        "name": " ".join(filter(None, [customer.first_name, customer.last_name]))
        or "Customer",
        "mobile": mobile,
    }


async def _itinerary(db: AsyncSession, package_id: int) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(TourItinerary)
                .where(TourItinerary.package_id == package_id)
                .order_by(TourItinerary.day_number)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "day": row.day_number,
            "title": row.title,
            "description": row.description,
            "activities": row.activities or [],
        }
        for row in rows
    ]


async def tour_public(
    db: AsyncSession,
    booking: TourBooking,
    package: TourPackage,
    master: MasterBooking,
    *,
    include_detail: bool = False,
) -> dict:
    paid = Decimal(str(booking.advance_total or 0))
    payable = booking.payable_amount
    balance_due = max(payable - paid, Decimal("0.00"))
    status = (booking.booking_status or "").upper()
    payload = {
        "booking_number": booking.booking_number,
        "master_booking_number": master.booking_number,
        "service": "TOUR",
        "status": status,
        "payment_status": booking.payment_status,
        "package_name": package.package_name,
        "package_code": package.package_code,
        "destination": package.destination,
        "duration_days": package.duration_days,
        "duration_nights": package.duration_nights,
        "travel_start_date": (
            booking.travel_start_date.isoformat() if booking.travel_start_date else None
        ),
        "travel_end_date": (
            booking.travel_end_date.isoformat() if booking.travel_end_date else None
        ),
        "persons_count": booking.persons_count,
        "pickup_location": booking.pickup_location,
        "pickup_datetime": _iso(booking.pickup_datetime),
        "total_amount": _money(payable),
        "advance_total": _money(paid),
        "balance_due": _money(balance_due),
        "special_requests": booking.special_requests,
        "hotel_details": booking.hotel_details,
        "other_details": booking.other_details,
        "trip_start_km": _f(booking.trip_start_km),
        "trip_end_km": _f(booking.trip_end_km),
        "trip_started_at": _iso(booking.trip_started_at),
        "trip_ended_at": _iso(booking.trip_ended_at),
        "odometer_distance_km": _f(booking.actual_distance),
        "cash_to_collect": (
            _money(booking.cash_amount_due)
            if booking.cash_pending_at == "DRIVER"
            else 0.0
        ),
        "cash_pending_at": booking.cash_pending_at,
        "invoice_number": booking.invoice_number,
        "can_start": (
            status == DRIVER_TOUR_ASSIGNED
            and booking.driver_id is not None
            and booking.vehicle_id is not None
        ),
        "can_close": status == DRIVER_TOUR_ACTIVE,
        "is_live": status == DRIVER_TOUR_ACTIVE,
        "vehicle": await _vehicle_block(db, booking.vehicle_id),
    }
    if include_detail:
        payload["customer"] = await _customer_block(db, master)
        payload["itinerary"] = await _itinerary(db, package.id)
        payload["advances"] = await _advances(db, booking.id)
    return payload


async def _advances(db: AsyncSession, tour_booking_id: int) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(TourAdvancePayment)
                .where(
                    TourAdvancePayment.tour_booking_id == tour_booking_id,
                    TourAdvancePayment.status == "ACTIVE",
                )
                .order_by(TourAdvancePayment.collected_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "receipt_number": row.receipt_number,
            "amount": _money(row.amount),
            "payment_mode": row.payment_mode,
            "received_by": row.received_by,
            "collected_at": _iso(row.collected_at),
        }
        for row in rows
    ]


async def tour_cash_total(db: AsyncSession, driver_id: int) -> Decimal:
    value = (
        await db.execute(
            select(func.coalesce(func.sum(TourBooking.cash_amount_due), 0)).where(
                TourBooking.driver_id == driver_id,
                TourBooking.cash_pending_at == "DRIVER",
            )
        )
    ).scalar() or 0
    return Decimal(str(value))


async def tour_dashboard_rows(
    db: AsyncSession, driver: Driver
) -> tuple[Optional[dict], list[dict]]:
    """Active tour plus the assigned-but-not-started tours for /drivers/me."""
    active = (
        await db.execute(
            select(TourBooking, TourPackage, MasterBooking)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
            .where(
                TourBooking.driver_id == driver.id,
                TourBooking.booking_status == DRIVER_TOUR_ACTIVE,
            )
            .order_by(TourBooking.trip_started_at.desc())
            .limit(1)
        )
    ).one_or_none()
    active_row = await tour_public(db, *active) if active else None

    offers = (
        await db.execute(
            select(TourBooking, TourPackage, MasterBooking)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
            .where(
                TourBooking.driver_id == driver.id,
                TourBooking.booking_status == DRIVER_TOUR_ASSIGNED,
            )
            .order_by(TourBooking.travel_start_date)
            .limit(10)
        )
    ).all()
    offer_rows = [await tour_public(db, *row) for row in offers]
    return active_row, offer_rows


async def _notify_partner(
    db: AsyncSession,
    booking: TourBooking,
    package: TourPackage,
    action: str,
) -> None:
    try:
        from app.modules.notification.services.booking_notifications import (
            notify_booking_updated,
        )

        await notify_booking_updated(
            db,
            service_type="TOUR",
            master_booking_id=booking.master_booking_id,
            service_id=booking.id,
            booking_number=booking.booking_number,
            service_status=booking.booking_status,
            action=action,
            partner_ids=[package.partner_id],
        )
    except Exception:  # pragma: no cover - realtime is best effort
        pass


# ════════════════════════════════════════════════════════════════
# ENDPOINTS
# ════════════════════════════════════════════════════════════════


@router.get("/me/tours", summary="[Driver App] My tour jobs")
async def list_my_tours(
    scope: str = Query("active", pattern="^(active|assigned|history|all)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    stmt = (
        select(TourBooking, TourPackage, MasterBooking)
        .join(TourPackage, TourPackage.id == TourBooking.package_id)
        .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
        .where(
            TourBooking.driver_id == driver.id,
            TourPackage.partner_id == driver.partner_id,
        )
    )
    if scope == "active":
        stmt = stmt.where(TourBooking.booking_status == DRIVER_TOUR_ACTIVE)
    elif scope == "assigned":
        stmt = stmt.where(TourBooking.booking_status == DRIVER_TOUR_ASSIGNED)
    elif scope == "history":
        stmt = stmt.where(TourBooking.booking_status.in_(DRIVER_TOUR_TERMINAL))
    else:
        stmt = stmt.where(TourBooking.booking_status != "CANCELLED")
    stmt = stmt.order_by(
        TourBooking.travel_start_date.desc(), TourBooking.created_at.desc()
    )
    rows = (await db.execute(stmt.offset((page - 1) * limit).limit(limit))).all()
    items = [await tour_public(db, *row) for row in rows]
    return success_response(
        "My tour jobs", {"items": items, "page": page, "page_size": limit}
    )


@router.get("/me/tours/{booking_number}", summary="[Driver App] Tour detail")
async def get_my_tour(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    booking, package, master = await _driver_tour(db, driver, booking_number)
    return success_response(
        "Tour booking",
        await tour_public(db, booking, package, master, include_detail=True),
    )


@router.post("/me/tours/{booking_number}/start", summary="[Driver App] Start tour")
async def start_tour(
    booking_number: str,
    payload: StartTourRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    from app.modules.booking.services import breakdown as breakdown_service

    driver = await _resolve_driver(db, current_user["sub"])
    booking, package, master = await _driver_tour(db, driver, booking_number)
    booking = await _lock_tour(db, booking)

    if booking.booking_status != DRIVER_TOUR_ASSIGNED:
        raise BusinessException(
            f"Cannot start a tour while it is '{booking.booking_status}'.",
            code="TOUR_NOT_READY",
        )
    if not booking.vehicle_id or not booking.driver_id:
        raise BusinessException(
            "Your partner must assign a vehicle and driver before the tour can start.",
            code="TOUR_FLEET_MISSING",
        )
    if driver.status and driver.status.upper() not in ("APPROVED", "ACTIVE"):
        raise PermissionDeniedException(
            "Your driver profile is not active. Ask your partner to approve it.",
            code="DRIVER_NOT_ACTIVE",
        )
    other = (
        await db.execute(
            select(TourBooking.booking_number).where(
                TourBooking.driver_id == driver.id,
                TourBooking.booking_status == DRIVER_TOUR_ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if other:
        raise BusinessException(
            f"Tour {other} is already running. Close it before starting another.",
            code="TOUR_ALREADY_RUNNING",
        )

    now = _now()
    booking.trip_start_km = Decimal(str(payload.start_km))
    booking.trip_started_at = now
    booking.actual_distance = Decimal("0.00")
    booking.completed_by_driver_id = driver.id
    booking.booking_status = DRIVER_TOUR_ACTIVE
    master.booking_status = DRIVER_TOUR_ACTIVE
    if payload.notes:
        booking.close_notes = payload.notes.strip()[:500]

    await breakdown_service.mark_resources_on_trip(
        db,
        vehicle_id=booking.vehicle_id,
        driver_id=driver.id,
        on_trip=True,
    )

    db.add(
        BookingTimeline(
            master_booking_id=booking.master_booking_id,
            event_type="TOUR_STARTED_BY_DRIVER",
            event_description=(
                f"Driver {driver.full_name} started {booking.booking_number} "
                f"from {package.package_name}. Start odometer: "
                f"{payload.start_km:.1f} km."
            ),
            event_timestamp=now,
        )
    )
    await db.flush()
    await _notify_partner(db, booking, package, "DRIVER_START_TOUR")
    await _email_customer_for_tour(
        db,
        master_booking_id=booking.master_booking_id,
        tour_booking_id=booking.id,
        event_type="trip_started",
        context={
            "service": "Tour",
            "booking_number": booking.booking_number,
            "message": (
                f"Your {package.package_name} tour has started. "
                f"{driver.full_name} is your driver for the journey."
            ),
            "details": [
                ("Package", package.package_name),
                ("Destination", package.destination),
                ("Driver", driver.full_name),
            ],
        },
    )
    return success_response(
        "Tour started. Home, Tours and Cash are locked until you close it.",
        await tour_public(db, booking, package, master, include_detail=True),
    )


def _balance_due(booking: TourBooking) -> Decimal:
    return max(
        booking.payable_amount - Decimal(str(booking.advance_total or 0)),
        Decimal("0.00"),
    )


@router.get(
    "/me/tours/{booking_number}/close-preview",
    summary="[Driver App] Preview the tour balance before closing",
)
async def tour_close_preview(
    booking_number: str,
    end_km: float = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    driver = await _resolve_driver(db, current_user["sub"])
    booking, package, _ = await _driver_tour(db, driver, booking_number)
    if booking.booking_status != DRIVER_TOUR_ACTIVE:
        raise BusinessException(
            f"Cannot close a tour while it is '{booking.booking_status}'.",
            code="TOUR_NOT_RUNNING",
        )
    start_km = _f(booking.trip_start_km)
    if start_km is None:
        raise BusinessException(
            "Tour start odometer is missing.", code="TOUR_START_MISSING"
        )
    if end_km < start_km:
        raise ValidationException(
            f"End odometer ({end_km:.1f}) cannot be less than the start "
            f"({start_km:.1f})."
        )
    paid = Decimal(str(booking.advance_total or 0))
    payable = booking.payable_amount
    balance = _balance_due(booking)
    return success_response(
        "Tour closing preview",
        {
            "booking_number": booking.booking_number,
            "package_name": package.package_name,
            "destination": package.destination,
            "travel_start_date": (
                booking.travel_start_date.isoformat()
                if booking.travel_start_date
                else None
            ),
            "travel_end_date": (
                booking.travel_end_date.isoformat() if booking.travel_end_date else None
            ),
            "persons_count": booking.persons_count,
            "total_amount": _money(payable),
            "advance_paid": _money(paid),
            "balance_due": _money(balance),
            "trip_start_km": start_km,
            "trip_end_km": end_km,
            "odometer_distance_km": round(end_km - start_km, 2),
            "payment_status": booking.payment_status,
        },
    )


@router.post("/me/tours/{booking_number}/close", summary="[Driver App] Close tour")
async def close_tour(
    booking_number: str,
    payload: CloseTourRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    from app.modules.booking.services import breakdown as breakdown_service
    from app.modules.tour.services import (
        collect_advance,
        rollup_tour_totals_into_master,
    )

    driver = await _resolve_driver(db, current_user["sub"])
    booking, package, master = await _driver_tour(db, driver, booking_number)
    booking = await _lock_tour(db, booking)

    if booking.booking_status != DRIVER_TOUR_ACTIVE:
        raise BusinessException(
            f"Cannot close a tour while it is '{booking.booking_status}'.",
            code="TOUR_NOT_RUNNING",
        )
    start_km = _f(booking.trip_start_km)
    if start_km is None:
        raise BusinessException(
            "Tour start odometer is missing.", code="TOUR_START_MISSING"
        )
    if payload.end_km < start_km:
        raise ValidationException(
            f"End odometer ({payload.end_km:.1f}) cannot be less than the start "
            f"({start_km:.1f})."
        )

    mode = payload.payment_mode.strip().upper()
    if mode not in ("CASH", "UPI"):
        raise ValidationException(
            "payment_mode must be CASH or UPI — the platform advance is "
            "already recorded separately."
        )

    balance = _balance_due(booking)
    if balance > 0 and not payload.payment_collected:
        raise ValidationException(
            "Confirm the balance was collected before closing the tour."
        )
    # Advance already recorded before this close. `collect_advance` below adds
    # the balance, so capture it first for the receipt.
    advance_before_close = Decimal(str(booking.advance_total or 0))

    now = _now()
    odometer_km = Decimal(str(payload.end_km)) - Decimal(str(start_km))
    odometer_km = max(Decimal("0.00"), odometer_km)
    receipt_number = None
    if balance > 0:
        collected = await collect_advance(
            db,
            tour_booking_id=booking.id,
            amount=balance,
            payment_mode=mode,
            received_by="DRIVER",
            user_id=str(current_user["sub"]),
            source_role="DRIVER",
            reference_number=payload.reference_number,
            notes=(
                f"Balance collected by driver {driver.full_name} at tour close."
                + (f" {payload.notes.strip()}" if payload.notes else "")
            ),
        )
        receipt_number = collected["receipt_number"]

    booking.trip_end_km = Decimal(str(payload.end_km))
    booking.trip_ended_at = now
    booking.actual_distance = odometer_km.quantize(Decimal("0.01"))
    booking.booking_status = "COMPLETED"
    booking.completed_by_driver_id = driver.id
    # Only physical cash becomes driver cash custody. A UPI balance is
    # traceable and does not sit in the driver's hand.
    if balance > 0 and mode == "CASH":
        booking.cash_pending_at = "DRIVER"
        booking.cash_amount_due = balance
    else:
        booking.cash_pending_at = "NONE"
        booking.cash_amount_due = Decimal("0.00")
    if payload.notes:
        booking.close_notes = payload.notes.strip()[:500]
    master.booking_status = "COMPLETED"
    await rollup_tour_totals_into_master(db, booking.master_booking_id)

    await breakdown_service.mark_resources_on_trip(
        db,
        vehicle_id=booking.vehicle_id,
        driver_id=driver.id,
        on_trip=False,
    )
    availability = (
        await db.execute(
            select(DriverAvailability).where(DriverAvailability.driver_id == driver.id)
        )
    ).scalar_one_or_none()
    if availability:
        availability.availability_status = "ONLINE"

    db.add(
        BookingTimeline(
            master_booking_id=booking.master_booking_id,
            event_type="TRIP_CLOSED_BY_DRIVER",
            event_description=(
                f"Driver {driver.full_name} closed {booking.booking_number}. "
                f"Odometer: {odometer_km:.1f} km ({start_km:.1f} → "
                f"{payload.end_km:.1f}). Package total ₹{float(booking.payable_amount):,.2f}; "
                f"advance ₹{float(booking.advance_total or 0):,.2f}; "
                f"balance collected ₹{float(balance):,.2f} via {mode}."
            ),
            event_timestamp=now,
        )
    )
    await db.flush()
    await _notify_partner(db, booking, package, "DRIVER_CLOSE_TOUR")
    await _email_customer_for_tour(
        db,
        master_booking_id=booking.master_booking_id,
        tour_booking_id=booking.id,
        event_type="trip_completed",
        context={
            "service": "Tour",
            "booking_number": booking.booking_number,
            "amount": _money(booking.payable_amount),
            "amount_label": "Tour total",
            "message": (
                "Your tour is complete. Your invoice is available in the app "
                "once the tour operator generates it."
            ),
            "details": [
                ("Package", package.package_name),
                ("Destination", package.destination),
            ],
        },
    )

    warnings = []
    if balance > 0 and mode == "CASH":
        warnings.append(
            f"Cash of ₹{float(balance):,.2f} is in your custody until "
            "your partner collects it. Hand it over against the cash record."
        )
    if odometer_km <= 0:
        warnings.append(
            "The start and end odometer readings are the same. Your partner "
            "may ask you to confirm the readings."
        )

    return success_response(
        "Tour completed." + (f" {warnings[0]}" if warnings else ""),
        {
            "booking_number": booking.booking_number,
            "status": booking.booking_status,
            "package_name": package.package_name,
            "destination": package.destination,
            "total_amount": _money(booking.payable_amount),
            "advance_total": _money(booking.advance_total or 0),
            "advance_applied": _money(
                min(advance_before_close, booking.payable_amount)
            ),
            "balance_collected": _money(balance),
            "cash_to_collect": _money(booking.cash_amount_due),
            "cash_pending_at": booking.cash_pending_at,
            "payment_mode": mode,
            "receipt_number": receipt_number,
            "payment_status": booking.payment_status,
            "trip_start_km": start_km,
            "trip_end_km": payload.end_km,
            "odometer_distance_km": _money(odometer_km),
            "trip_started_at": _iso(booking.trip_started_at),
            "trip_ended_at": _iso(booking.trip_ended_at),
            "warnings": warnings,
        },
    )
