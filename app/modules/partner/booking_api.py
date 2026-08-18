# ============================================================
# WAYTERO — PARTNER BOOKING API
# File: app/modules/partner/booking_api.py
# Prefix: /partners  (registered in api/router.py)
# Endpoints:
#   GET  /partners/me/bookings                   — list partner's assigned cab bookings
#   GET  /partners/me/bookings/{booking_number}  — booking detail + assign resources
#   POST /partners/me/bookings/{booking_number}/assign-driver — assign driver+vehicle
# Doc Ref:
#   Booking API §17 — Assign Driver (Partner action)
#   BRD Part 3 §42  — Driver Assignment after partner acceptance
#   DB Schema Part 4 §7-8 — cab_bookings, cab_booking_assignments
# ============================================================

import math
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.booking import services as advance_service
from app.modules.booking.services import breakdown as breakdown_service
from app.modules.booking.models import (
    MasterBooking,
    CabBooking,
    CabBookingAssignment,
    BookingTimeline,
)
from app.modules.partner.models import Partner
from app.modules.customer.models import Customer
from app.modules.driver.models import Driver, DriverAvailability
from app.modules.vehicle.models import Vehicle, VehicleCategory
from app.modules.master.models import City
from app.modules.auth.models.user import User
from app.shared.responses.base import success_response

router = APIRouter()


# Terminal booking states where the partner's assignment may legitimately be
# closed (close-trip stamps closed_at with reason TRIP_COMPLETED). In these
# states the original partner must still be able to read and run downstream
# steps (collect-payment, view pricing) even with no open assignment row.
_PARTNER_TERMINAL_STATUSES = frozenset(
    {"COMPLETED", "SETTLEMENT_PENDING", "CANCELLED", "REFUNDED"}
)

# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _resolve_partner_id(db: AsyncSession, user_uuid: str) -> int:
    """
    Resolve the Partner row ID from the authenticated user's UUID.
    Raises 404 if user has no partner profile.
    """
    from uuid import UUID

    p = (
        await db.execute(select(Partner).where(Partner.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Partner profile not found for this user.")
    return p.id


async def _notify_booking_updated(
    db: AsyncSession,
    *,
    service_type: str,
    master_booking_id: int,
    service_id: int,
    booking_number: str,
    service_status: str,
    action: str,
    payment_status=None,
    partner_ids=None,
):
    """Best-effort realtime fan-out so open booking pages on both portals
    refresh when a cab/tour status changes. Never blocks the caller."""
    try:
        from app.modules.notification.services.booking_notifications import (
            notify_booking_updated,
        )

        await notify_booking_updated(
            db,
            service_type=service_type,
            master_booking_id=master_booking_id,
            service_id=service_id,
            booking_number=booking_number,
            service_status=service_status,
            action=action,
            payment_status=payment_status,
            partner_ids=partner_ids or [],
        )
    except Exception as _exc:  # pragma: no cover - best-effort
        import logging as _log

        _log.getLogger("waytero.partner").warning(
            "partner.booking_ws_notify_failed bn=%s err=%s", booking_number, _exc
        )


async def _log_timeline(
    db: AsyncSession, master_booking_id: int, event_type: str, description: str
):
    entry = BookingTimeline(
        master_booking_id=master_booking_id,
        event_type=event_type,
        event_description=description,
        event_timestamp=datetime.now(timezone.utc),
    )
    db.add(entry)


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class BookingListItem(BaseModel):
    booking_number: str
    master_booking_number: str
    master_booking_id: int
    booking_status: str
    trip_type: Optional[str]
    pickup_location: Optional[str]
    drop_location: Optional[str]
    pickup_datetime: Optional[str]
    journey_start_date: Optional[str]
    amount: float
    partner_payout: float
    customer_name: str
    customer_mobile: Optional[str]
    vehicle_category: Optional[str]
    driver_id: Optional[int]
    driver_name: Optional[str]
    driver_mobile: Optional[str]
    vehicle_reg: Optional[str]
    assigned_at: Optional[str]

    class Config:
        from_attributes = True


class BookingListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: List[BookingListItem]


class TimelineEvent(BaseModel):
    event_type: str
    event_description: Optional[str]
    event_timestamp: str


class AvailableDriver(BaseModel):
    id: int
    driver_code: str
    full_name: str
    mobile: str
    status: str
    availability_status: str


class AvailableVehicle(BaseModel):
    id: int
    registration_number: str
    make: Optional[str]
    model: Optional[str]
    color: Optional[str]
    status: str
    category_name: Optional[str]
    availability_status: str


class AssignmentInfo(BaseModel):
    assignment_id: int
    assigned_at: Optional[str]
    assignment_type: Optional[str]
    driver_id: Optional[int]
    driver_name: Optional[str]
    driver_mobile: Optional[str]
    driver_code: Optional[str]
    vehicle_id: Optional[int]
    vehicle_reg: Optional[str]
    vehicle_make: Optional[str]
    vehicle_model: Optional[str]
    vehicle_color: Optional[str]
    # Partner acceptance gate (migration 0039)
    accepted_at: Optional[str] = None
    rejected_at: Optional[str] = None
    rejection_reason_code: Optional[str] = None
    rejection_notes: Optional[str] = None
    acceptance_deadline: Optional[str] = None


class AdvanceInfo(BaseModel):
    """The live advance on this booking. Doc Ref: BRD Part 3 §45."""

    id: int
    receipt_number: str
    amount: float
    payment_mode: str
    received_by: str
    reference_note: Optional[str] = None
    status: str
    collected_by_role: str
    collected_at: Optional[str] = None


class BookingDetail(BaseModel):
    booking_number: str
    master_booking_number: str
    master_booking_id: int
    booking_status: str
    master_status: str
    trip_type: Optional[str]
    vehicle_category: Optional[str]
    pickup_location: Optional[str]
    drop_location: Optional[str]
    pickup_datetime: Optional[str]
    estimated_distance: float
    estimated_amount: float
    final_amount: float
    partner_payout: float
    city_name: Optional[str]
    journey_start_date: Optional[str]
    journey_end_date: Optional[str]
    payment_status: str
    total_amount: float
    total_paid_amount: float
    remarks: Optional[str]
    customer_name: str
    customer_mobile: Optional[str]
    customer_email: Optional[str]
    # Trip execution fields (for driver assistance panel)
    trip_start_km: Optional[float]
    trip_end_km: Optional[float]
    trip_started_at: Optional[str]
    trip_ended_at: Optional[str]
    actual_distance: Optional[float]
    payment_mode: Optional[str]
    payment_collected_by: Optional[str]
    cash_pending_at: Optional[str]
    coupon_discount: float
    advance_paid: float
    advance: Optional[AdvanceInfo] = None
    invoice_number: Optional[str]
    assignment: AssignmentInfo
    timeline: List[TimelineEvent]
    available_drivers: List[AvailableDriver]
    available_vehicles: List[AvailableVehicle]
    # Partner acceptance gate (migration 0039) — cab-level fields so the
    # booking detail page can render the Accept/Reject banner + countdown.
    acceptance_deadline: Optional[str] = None
    acceptance_status: Optional[str] = (
        None  # "PENDING" | "ACCEPTED" | "REJECTED" | "TIMEOUT" | None
    )
    pending_partner_id: Optional[int] = None


class AssignDriverRequest(BaseModel):
    driver_id: int
    vehicle_id: int


class RejectBookingRequest(BaseModel):
    """Partner rejection payload. `reason_code` must be one of the codes
    listed by GET /me/reject-reasons. `notes` is required when reason is
    OTHER, optional otherwise.
    """

    reason_code: str
    notes: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# BOOKING COUNTS — GET /partners/me/bookings/counts
# Returns actionable booking counts for sidebar badge + topbar.
# Actionable = ASSIGNED (needs driver) + DRIVER_ASSIGNED + STARTED
# ════════════════════════════════════════════════════════════════


@router.get("/me/bookings/counts", tags=["Partner Bookings"])
async def get_partner_booking_counts(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns booking counts grouped by status for the sidebar badge.
    - actionable: PENDING_PARTNER_ACCEPTANCE + ASSIGNED + DRIVER_ASSIGNED + STARTED
    - pending_acceptance: PENDING_PARTNER_ACCEPTANCE only (needs partner accept/reject)
    - pending_driver: ASSIGNED only (needs driver assignment)
    - in_progress: DRIVER_ASSIGNED + STARTED
    - total_active: all non-terminal bookings

    Note: the partner only sees cabs whose *latest* assignment is theirs,
    so a previously-assigned-and-reassigned-away cab does not inflate
    these counts.
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    # Subquery: cab_booking_ids whose latest assignment belongs to this partner.
    # Mirrors the filter used in list_partner_bookings so counts and lists
    # always agree.
    latest_assignment_subq = (
        select(
            CabBookingAssignment.cab_booking_id,
            func.max(CabBookingAssignment.assigned_at).label("max_assigned_at"),
        )
        .group_by(CabBookingAssignment.cab_booking_id)
        .subquery()
    )
    latest_self_q = (
        select(CabBookingAssignment.cab_booking_id)
        .join(
            latest_assignment_subq,
            (
                CabBookingAssignment.cab_booking_id
                == latest_assignment_subq.c.cab_booking_id
            )
            & (
                CabBookingAssignment.assigned_at
                == latest_assignment_subq.c.max_assigned_at
            ),
        )
        .where(CabBookingAssignment.partner_id == partner_id)
    )
    owned_ids = (await db.execute(latest_self_q)).scalars().all()

    if not owned_ids:
        return {
            "actionable": 0,
            "pending_acceptance": 0,
            "pending_driver": 0,
            "in_progress": 0,
            "total_active": 0,
        }

    rows = (
        (
            await db.execute(
                select(
                    CabBooking.booking_status, func.count(CabBooking.id).label("cnt")
                )
                .where(CabBooking.id.in_(owned_ids))
                .group_by(CabBooking.booking_status)
            )
        )
        .mappings()
        .all()
    )

    counts: dict[str, int] = {r["booking_status"]: int(r["cnt"]) for r in rows}

    pending_acceptance = counts.get("PENDING_PARTNER_ACCEPTANCE", 0)
    pending_driver = counts.get("ASSIGNED", 0)
    in_progress = counts.get("DRIVER_ASSIGNED", 0) + counts.get("STARTED", 0)
    actionable = pending_acceptance + pending_driver + in_progress
    terminal = {"COMPLETED", "SETTLED", "SETTLEMENT_PENDING", "CANCELLED"}
    total_active = sum(v for k, v in counts.items() if k not in terminal)

    return {
        "actionable": actionable,
        "pending_acceptance": pending_acceptance,
        "pending_driver": pending_driver,
        "in_progress": in_progress,
        "total_active": total_active,
    }


# ════════════════════════════════════════════════════════════════
# LIST PARTNER BOOKINGS — GET /partners/me/bookings
# Returns all cab bookings assigned to this partner's ID,
# most recent first, with optional status/trip_type filter.
# Doc Ref: Booking API §24, BRD Part 3 §42
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/bookings", response_model=BookingListResponse, tags=["Partner Bookings"]
)
async def list_partner_bookings(
    status: Optional[str] = Query(None, description="Cab booking status filter"),
    trip_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return all cab bookings where this partner is the assigned partner
    on the *latest* assignment row. Cab reassigned *away* from this
    partner are not returned.
    Enriched with customer info, vehicle category, and latest driver assignment.
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    # Find cab_booking_ids where the LATEST assignment row is this partner.
    # A booking reassigned away from this partner should not appear.
    latest_assignment_subq = (
        select(
            CabBookingAssignment.cab_booking_id,
            func.max(CabBookingAssignment.assigned_at).label("max_assigned_at"),
        )
        .group_by(CabBookingAssignment.cab_booking_id)
        .subquery()
    )
    assign_q = (
        select(CabBookingAssignment.cab_booking_id)
        .join(
            latest_assignment_subq,
            (
                CabBookingAssignment.cab_booking_id
                == latest_assignment_subq.c.cab_booking_id
            )
            & (
                CabBookingAssignment.assigned_at
                == latest_assignment_subq.c.max_assigned_at
            ),
        )
        .where(CabBookingAssignment.partner_id == partner_id)
    )
    assigned_cab_ids = (await db.execute(assign_q)).scalars().all()

    if not assigned_cab_ids:
        return BookingListResponse(total=0, page=page, limit=limit, pages=1, items=[])

    # Query cab bookings
    q = select(CabBooking).where(CabBooking.id.in_(assigned_cab_ids))
    if status:
        q = q.where(CabBooking.booking_status == status.upper())
    if trip_type:
        q = q.where(CabBooking.trip_type == trip_type.upper())

    total = (
        await db.execute(select(func.count()).select_from(q.subquery()))
    ).scalar() or 0
    offset = (page - 1) * limit
    rows = (
        (
            await db.execute(
                q.options(selectinload(CabBooking.assignments))
                .order_by(desc(CabBooking.created_at))
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Build enriched list items
    items = []
    for cb in rows:
        mb = (
            await db.execute(
                select(MasterBooking).where(MasterBooking.id == cb.master_booking_id)
            )
        ).scalar_one_or_none()

        # Customer info
        cust = cust_user = None
        if mb:
            cust = (
                await db.execute(select(Customer).where(Customer.id == mb.customer_id))
            ).scalar_one_or_none()
            if cust:
                cust_user = (
                    await db.execute(select(User).where(User.id == cust.user_id))
                ).scalar_one_or_none()

        # Vehicle category
        cat_name = None
        if cb.vehicle_category_id:
            cat = (
                await db.execute(
                    select(VehicleCategory).where(
                        VehicleCategory.id == cb.vehicle_category_id
                    )
                )
            ).scalar_one_or_none()
            cat_name = cat.category_name if cat else None

        # Latest assignment for this partner. We keep closed (historical) rows
        # visible so the detail page can show the full history; the active row
        # is just whichever is most recent.
        latest = None
        partner_assigns = [
            a for a in (cb.assignments or []) if a.partner_id == partner_id
        ]
        if partner_assigns:
            latest = sorted(partner_assigns, key=lambda a: a.assigned_at, reverse=True)[
                0
            ]

        driver_id = driver_name = driver_mobile = vehicle_reg = None
        assigned_at = None
        if latest:
            assigned_at = latest.assigned_at.isoformat() if latest.assigned_at else None
            if latest.driver_id:
                driver_id = latest.driver_id
                drv = (
                    await db.execute(
                        select(Driver).where(Driver.id == latest.driver_id)
                    )
                ).scalar_one_or_none()
                if drv:
                    driver_name = drv.full_name
                    driver_mobile = drv.mobile
            if latest.vehicle_id:
                veh = (
                    await db.execute(
                        select(Vehicle).where(Vehicle.id == latest.vehicle_id)
                    )
                ).scalar_one_or_none()
                vehicle_reg = veh.registration_number if veh else None

        items.append(
            BookingListItem(
                booking_number=cb.booking_number,
                master_booking_number=mb.booking_number if mb else "",
                master_booking_id=mb.id if mb else 0,
                booking_status=cb.booking_status,
                trip_type=cb.trip_type,
                pickup_location=cb.pickup_location,
                drop_location=cb.drop_location,
                pickup_datetime=(
                    cb.pickup_datetime.isoformat() if cb.pickup_datetime else None
                ),
                journey_start_date=(
                    mb.journey_start_date.isoformat()
                    if mb and mb.journey_start_date
                    else None
                ),
                amount=float(cb.estimated_amount or 0),
                partner_payout=float(cb.partner_payout or 0),
                customer_name=cust.full_name if cust else "—",
                customer_mobile=cust_user.mobile_number if cust_user else None,
                vehicle_category=cat_name,
                driver_id=driver_id,
                driver_name=driver_name,
                driver_mobile=driver_mobile,
                vehicle_reg=vehicle_reg,
                assigned_at=assigned_at,
            )
        )

    return BookingListResponse(
        total=total,
        page=page,
        limit=limit,
        pages=math.ceil(total / limit) if total else 1,
        items=items,
    )


# ════════════════════════════════════════════════════════════════
# BOOKING DETAIL — GET /partners/me/bookings/{booking_number}
# Returns full cab booking detail + available drivers & vehicles
# Doc Ref: Booking API §17, BRD Part 3 §42
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/bookings/{booking_number}",
    response_model=BookingDetail,
    tags=["Partner Bookings"],
)
async def get_partner_booking_detail(
    booking_number: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
):
    """
    Full detail for a cab booking assigned to this partner.
    Includes timeline, latest assignment, and available driver/vehicle lists.
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    # Fetch the cab booking
    cb = (
        await db.execute(
            select(CabBooking)
            .options(
                selectinload(CabBooking.assignments),
            )
            .where(CabBooking.booking_number == booking_number)
        )
    ).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, "Cab booking not found.")

    # Confirm partner owns this booking (any historical row counts; the detail
    # page surfaces the full handover timeline).
    partner_assigns = [a for a in (cb.assignments or []) if a.partner_id == partner_id]
    if not partner_assigns:
        raise HTTPException(403, "You are not assigned to this booking.")

    # Master booking
    mb = (
        await db.execute(
            select(MasterBooking)
            .options(selectinload(MasterBooking.timelines))
            .where(MasterBooking.id == cb.master_booking_id)
        )
    ).scalar_one_or_none()

    # Customer
    cust = cust_user = None
    if mb:
        cust = (
            await db.execute(select(Customer).where(Customer.id == mb.customer_id))
        ).scalar_one_or_none()
        if cust:
            cust_user = (
                await db.execute(select(User).where(User.id == cust.user_id))
            ).scalar_one_or_none()

    # City
    city_name = None
    if mb and mb.city_id:
        city = (
            await db.execute(select(City).where(City.id == mb.city_id))
        ).scalar_one_or_none()
        city_name = city.name if city else None

    # Vehicle category
    cat_name = None
    if cb.vehicle_category_id:
        cat = (
            await db.execute(
                select(VehicleCategory).where(
                    VehicleCategory.id == cb.vehicle_category_id
                )
            )
        ).scalar_one_or_none()
        cat_name = cat.category_name if cat else None

    # Latest assignment for this partner — prefer the active one (closed_at IS
    # NULL) so a handover doesn't leave the partner portal surfacing closed rows,
    # but fall back to the most recent assignment overall once the trip is closed
    # (close-trip sets closed_at, which would otherwise make this list empty).
    open_assigns = [a for a in partner_assigns if a.closed_at is None]
    latest_assign = sorted(
        open_assigns if open_assigns else partner_assigns,
        key=lambda a: a.assigned_at,
        reverse=True,
    )[0]

    driver_name = driver_mobile = driver_code = None
    vehicle_reg = vehicle_make = vehicle_model = vehicle_color = None

    if latest_assign.driver_id:
        drv = (
            await db.execute(select(Driver).where(Driver.id == latest_assign.driver_id))
        ).scalar_one_or_none()
        if drv:
            driver_name = drv.full_name
            driver_mobile = drv.mobile
            driver_code = drv.driver_code

    if latest_assign.vehicle_id:
        veh = (
            await db.execute(
                select(Vehicle).where(Vehicle.id == latest_assign.vehicle_id)
            )
        ).scalar_one_or_none()
        if veh:
            vehicle_reg = veh.registration_number
            vehicle_make = veh.vehicle_brand
            vehicle_model = veh.vehicle_model
            vehicle_color = getattr(veh, "color", None)

    # Timeline from master booking
    timeline_events = []
    if mb and mb.timelines:
        for t in sorted(mb.timelines, key=lambda x: x.event_timestamp, reverse=True):
            timeline_events.append(
                TimelineEvent(
                    event_type=t.event_type or "",
                    event_description=t.event_description,
                    event_timestamp=t.event_timestamp.isoformat(),
                )
            )

    # Available drivers for this partner (APPROVED/ACTIVE, not already on active trip)
    active_driver_ids_q = (
        select(CabBookingAssignment.driver_id)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .where(
            CabBooking.booking_status.in_(["ASSIGNED", "DRIVER_ASSIGNED", "STARTED"]),
            CabBookingAssignment.driver_id.is_not(None),
        )
    )
    active_driver_ids = set((await db.execute(active_driver_ids_q)).scalars().all())

    drivers_q = (
        select(Driver)
        .where(
            and_(
                Driver.partner_id == partner_id,
                Driver.status.in_(["APPROVED", "ACTIVE"]),
            )
        )
        .order_by(Driver.full_name)
    )
    drivers = (await db.execute(drivers_q)).scalars().all()

    available_drivers = []
    for d in drivers:
        avail = (
            await db.execute(
                select(DriverAvailability).where(DriverAvailability.driver_id == d.id)
            )
        ).scalar_one_or_none()
        avail_status = avail.availability_status if avail else "OFFLINE"
        available_drivers.append(
            AvailableDriver(
                id=d.id,
                driver_code=d.driver_code,
                full_name=d.full_name,
                mobile=d.mobile,
                status=d.status,
                availability_status=(
                    "BUSY" if d.id in active_driver_ids else avail_status
                ),
            )
        )

    # Available vehicles for this partner (APPROVED/ACTIVE, matching category if set)
    active_vehicle_ids_q = (
        select(CabBookingAssignment.vehicle_id)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .where(
            CabBooking.booking_status.in_(["ASSIGNED", "DRIVER_ASSIGNED", "STARTED"]),
            CabBookingAssignment.vehicle_id.is_not(None),
        )
    )
    active_vehicle_ids = set((await db.execute(active_vehicle_ids_q)).scalars().all())

    veh_q = select(Vehicle).where(
        and_(
            Vehicle.partner_id == partner_id, Vehicle.status.in_(["APPROVED", "ACTIVE"])
        )
    )
    if cb.vehicle_category_id:
        veh_q = veh_q.where(Vehicle.vehicle_category_id == cb.vehicle_category_id)
    vehicles = (
        (await db.execute(veh_q.order_by(Vehicle.registration_number))).scalars().all()
    )

    available_vehicles = []
    for v in vehicles:
        vc_name = None
        if v.vehicle_category_id:
            vc = (
                await db.execute(
                    select(VehicleCategory).where(
                        VehicleCategory.id == v.vehicle_category_id
                    )
                )
            ).scalar_one_or_none()
            vc_name = vc.category_name if vc else None
        available_vehicles.append(
            AvailableVehicle(
                id=v.id,
                registration_number=v.registration_number,
                make=v.vehicle_brand,
                model=v.vehicle_model,
                color=getattr(v, "color", None),
                status="BUSY" if v.id in active_vehicle_ids else v.status,
                category_name=vc_name,
                availability_status=(
                    "BUSY" if v.id in active_vehicle_ids else "AVAILABLE"
                ),
            )
        )

    # Coupon discount (from coupon_usages table, consistent with trip-assist endpoints)
    from sqlalchemy import func as _func_d
    from app.modules.admin.coupon_models import CouponUsage as _CouponUsage_d

    coupon_q = await db.execute(
        select(_func_d.sum(_CouponUsage_d.discount_applied)).where(
            _CouponUsage_d.master_booking_id == cb.master_booking_id
        )
    )
    coupon_discount_val = float(coupon_q.scalar() or 0)
    # Advance comes from advance_payments, not total_paid_amount — that field is
    # a running "paid so far" total and is overwritten at final payment.
    advance_row = await advance_service.get_active_advance(db, cb.id)
    advance_paid_val = float(advance_row["amount"]) if advance_row else 0.0
    advance_summary_val = advance_service.advance_summary(advance_row)

    # Prevent browser from caching booking detail — payment/invoice state must always be fresh
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

    return BookingDetail(
        booking_number=cb.booking_number,
        master_booking_number=mb.booking_number if mb else "",
        master_booking_id=mb.id if mb else 0,
        booking_status=cb.booking_status,
        master_status=mb.booking_status if mb else "",
        trip_type=cb.trip_type,
        vehicle_category=cat_name,
        pickup_location=cb.pickup_location,
        drop_location=cb.drop_location,
        pickup_datetime=cb.pickup_datetime.isoformat() if cb.pickup_datetime else None,
        estimated_distance=float(cb.estimated_distance or 0),
        estimated_amount=float(cb.estimated_amount or 0),
        final_amount=float(cb.final_amount or 0),
        partner_payout=float(cb.partner_payout or 0),
        city_name=city_name,
        journey_start_date=(
            mb.journey_start_date.isoformat() if mb and mb.journey_start_date else None
        ),
        journey_end_date=(
            mb.journey_end_date.isoformat() if mb and mb.journey_end_date else None
        ),
        payment_status=mb.payment_status if mb else "",
        total_amount=float(mb.total_amount or 0) if mb else 0,
        total_paid_amount=float(mb.total_paid_amount or 0) if mb else 0,
        remarks=mb.remarks if mb else None,
        customer_name=cust.full_name if cust else "—",
        customer_mobile=cust_user.mobile_number if cust_user else None,
        customer_email=cust_user.email if cust_user else None,
        # Trip execution fields
        trip_start_km=float(cb.trip_start_km) if cb.trip_start_km is not None else None,
        trip_end_km=float(cb.trip_end_km) if cb.trip_end_km is not None else None,
        trip_started_at=cb.trip_started_at.isoformat() if cb.trip_started_at else None,
        trip_ended_at=cb.trip_ended_at.isoformat() if cb.trip_ended_at else None,
        actual_distance=(
            float(cb.actual_distance) if cb.actual_distance is not None else None
        ),
        payment_mode=cb.payment_mode,
        payment_collected_by=cb.payment_collected_by,
        cash_pending_at=cb.cash_pending_at,
        coupon_discount=coupon_discount_val,
        advance_paid=advance_paid_val,
        advance=AdvanceInfo(**advance_summary_val) if advance_summary_val else None,
        invoice_number=cb.invoice_number,
        assignment=AssignmentInfo(
            assignment_id=latest_assign.id,
            assigned_at=(
                latest_assign.assigned_at.isoformat()
                if latest_assign.assigned_at
                else None
            ),
            assignment_type=latest_assign.assignment_type,
            driver_id=latest_assign.driver_id,
            driver_name=driver_name,
            driver_mobile=driver_mobile,
            driver_code=driver_code,
            vehicle_id=latest_assign.vehicle_id,
            vehicle_reg=vehicle_reg,
            vehicle_make=vehicle_make,
            vehicle_model=vehicle_model,
            vehicle_color=vehicle_color,
            # Partner acceptance gate (migration 0039)
            accepted_at=(
                latest_assign.accepted_at.isoformat()
                if latest_assign.accepted_at
                else None
            ),
            rejected_at=(
                latest_assign.rejected_at.isoformat()
                if latest_assign.rejected_at
                else None
            ),
            rejection_reason_code=latest_assign.rejection_reason_code,
            rejection_notes=latest_assign.rejection_notes,
            acceptance_deadline=(
                latest_assign.acceptance_deadline.isoformat()
                if latest_assign.acceptance_deadline
                else None
            ),
        ),
        # Cab-level acceptance fields — duplicated from the cab row so the
        # partner detail page can render countdown/status without joining.
        acceptance_deadline=(
            cb.acceptance_deadline.isoformat() if cb.acceptance_deadline else None
        ),
        acceptance_status=(
            "PENDING"
            if cb.booking_status == "PENDING_PARTNER_ACCEPTANCE"
            else (
                "ACCEPTED"
                if latest_assign.accepted_at is not None
                else (
                    "REJECTED"
                    if latest_assign.rejected_at is not None
                    else (
                        "TIMEOUT"
                        if latest_assign.rejection_reason_code == "TIMEOUT"
                        else None
                    )
                )
            )
        ),
        pending_partner_id=cb.pending_partner_id,
        timeline=timeline_events,
        available_drivers=available_drivers,
        available_vehicles=available_vehicles,
    )


# ════════════════════════════════════════════════════════════════
# PARTNER ACCEPTANCE / REJECTION
# Doc Ref: BRD Part 3 §42 — Driver Assignment after partner acceptance
#
# After admin assigns a cab booking the partner gets a 10-minute
# window to accept or reject. Accept flips the cab to ASSIGNED (the
# status the rest of the partner workflow already understands).
# Reject returns the cab to PENDING_ASSIGNMENT so admin can pick a
# different partner.
#
# Both endpoints are scoped to the latest assignment for this partner;
# if the admin has reassigned the cab away, the partner's accept/reject
# attempt is rejected with 410 Gone.
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/bookings/{booking_number}/accept",
    tags=["Partner Bookings"],
)
async def partner_accept_booking(
    booking_number: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Partner accepts an admin-assigned cab booking.

    Flips cab.booking_status from PENDING_PARTNER_ACCEPTANCE to ASSIGNED,
    stamps accepted_at on the latest assignment row, and clears the
    acceptance deadline / pending partner pointer on the cab.

    Returns 410 Gone if the acceptance window has already expired (the
    sweeper would have moved it back by now, but guard explicitly).
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    _, cb, partner_assigns = await _get_partner_cab_booking(
        db, booking_number, partner_id
    )

    # Status guard
    if cb.booking_status != "PENDING_PARTNER_ACCEPTANCE":
        raise HTTPException(
            400,
            f"Cannot accept: booking is '{cb.booking_status}'. "
            "Acceptance is only valid when status is PENDING_PARTNER_ACCEPTANCE.",
        )

    # Deadline guard
    now = datetime.now(timezone.utc)
    if cb.acceptance_deadline is None or cb.acceptance_deadline <= now:
        raise HTTPException(
            410,
            f"Acceptance window has expired (deadline {cb.acceptance_deadline}). "
            "Please refresh — admin will reassign.",
        )

    # The latest assignment is the one in the acceptance window — must be
    # the still-open one (closed_at IS NULL).
    latest = sorted(
        [a for a in partner_assigns if a.closed_at is None],
        key=lambda a: a.assigned_at,
        reverse=True,
    )[0]
    if latest.accepted_at is not None or latest.rejected_at is not None:
        # Already responded to in a previous attempt — idempotent re-call is
        # safe and returns the existing state.
        return {
            "success": True,
            "message": "Booking already responded to",
            "cab_status": cb.booking_status,
            "accepted_at": (
                latest.accepted_at.isoformat() if latest.accepted_at else None
            ),
        }

    latest.accepted_at = now
    cb.booking_status = "ASSIGNED"
    cb.partner_responded_at = now
    cb.acceptance_deadline = None
    cb.pending_partner_id = None

    await _log_timeline(
        db,
        cb.master_booking_id,
        "PARTNER_ACCEPTED",
        f"Partner accepted cab {cb.booking_number}. Awaiting driver assignment.",
    )
    # get_db auto-commits on success.

    # ── Realtime: notify any other open partner tabs (Doc Ref: BRD Part 7 §155) ──
    try:
        from app.modules.notification.realtime import manager as _rt
        from uuid import UUID as _UUID

        await _rt.send_to_user(
            _UUID(current_user["sub"]),
            {
                "event": "BOOKING_PARTNER_RESPONDED",
                "data": {
                    "cab_booking_number": cb.booking_number,
                    "master_booking_id": cb.master_booking_id,
                    "decision": "ACCEPTED",
                    "cab_status": "ASSIGNED",
                },
            },
        )
        # Admin-side refresh nudge — admin tabs subscribed to the
        # booking channel pick this up and refetch.
        await _rt.broadcast(
            {
                "event": "BOOKING_ADMIN_REFRESH",
                "data": {
                    "cab_booking_number": cb.booking_number,
                    "master_booking_id": cb.master_booking_id,
                    "new_status": "ASSIGNED",
                    "reason": "PARTNER_ACCEPTED",
                },
            }
        )
    except Exception as _ws_exc:  # pragma: no cover
        import logging as _log

        _log.getLogger("waytero.partner").warning(
            "partner_accept.ws_notify_failed err=%s", _ws_exc
        )

    await _notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=cb.master_booking_id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="ASSIGNED",
        action="PARTNER_ACCEPTED",
        partner_ids=[partner_id],
    )

    return {
        "success": True,
        "message": "Booking accepted",
        "cab_status": "ASSIGNED",
        "accepted_at": now.isoformat(),
    }


@router.post(
    "/me/bookings/{booking_number}/reject",
    tags=["Partner Bookings"],
)
async def partner_reject_booking(
    booking_number: str,
    payload: RejectBookingRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Partner rejects an admin-assigned cab booking.

    Required: reason_code (one of REJECT_REASON_CODES).
    Optional: notes — REQUIRED when reason_code is "OTHER".

    Returns the cab to PENDING_ASSIGNMENT so admin can reassign.
    """
    from app.modules.partner.constants import REJECT_REASON_CODES

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, partner_assigns = await _get_partner_cab_booking(
        db, booking_number, partner_id
    )

    if cb.booking_status != "PENDING_PARTNER_ACCEPTANCE":
        raise HTTPException(
            400,
            f"Cannot reject: booking is '{cb.booking_status}'. "
            "Rejection is only valid when status is PENDING_PARTNER_ACCEPTANCE.",
        )

    if payload.reason_code not in REJECT_REASON_CODES:
        raise HTTPException(
            400,
            f"Unknown reject reason '{payload.reason_code}'. "
            f"Valid: {', '.join(sorted(REJECT_REASON_CODES))}",
        )

    notes = (payload.notes or "").strip()
    if payload.reason_code == "OTHER" and not notes:
        raise HTTPException(400, "notes is required when reason_code is OTHER.")

    latest = sorted(
        [a for a in partner_assigns if a.closed_at is None],
        key=lambda a: a.assigned_at,
        reverse=True,
    )[0]
    now = datetime.now(timezone.utc)
    latest.rejected_at = now
    latest.rejection_reason_code = payload.reason_code
    latest.rejection_notes = notes or None

    cb.booking_status = "PENDING_ASSIGNMENT"
    cb.partner_responded_at = now
    cb.acceptance_deadline = None
    cb.pending_partner_id = None

    label = payload.reason_code
    notes_part = f" | notes: {notes}" if notes else ""
    await _log_timeline(
        db,
        mb.id,
        "PARTNER_REJECTED",
        f"Partner rejected cab {cb.booking_number}. Reason: {label}{notes_part}. "
        f"Returned to PENDING_ASSIGNMENT for admin reassignment.",
    )
    # get_db auto-commits on success.

    # ── Realtime: notify any other open partner tabs (Doc Ref: BRD Part 7 §155) ──
    try:
        from app.modules.notification.realtime import manager as _rt
        from uuid import UUID as _UUID

        await _rt.send_to_user(
            _UUID(current_user["sub"]),
            {
                "event": "BOOKING_PARTNER_RESPONDED",
                "data": {
                    "cab_booking_number": cb.booking_number,
                    "master_booking_id": mb.id,
                    "decision": "REJECTED",
                    "reason_code": payload.reason_code,
                    "cab_status": "PENDING_ASSIGNMENT",
                },
            },
        )
        await _rt.broadcast(
            {
                "event": "BOOKING_ADMIN_REFRESH",
                "data": {
                    "cab_booking_number": cb.booking_number,
                    "master_booking_id": mb.id,
                    "new_status": "PENDING_ASSIGNMENT",
                    "reason": "PARTNER_REJECTED",
                    "rejection_reason_code": payload.reason_code,
                },
            }
        )
    except Exception as _ws_exc:  # pragma: no cover
        import logging as _log

        _log.getLogger("waytero.partner").warning(
            "partner_reject.ws_notify_failed err=%s", _ws_exc
        )

    await _notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=mb.id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="PENDING_ASSIGNMENT",
        action="PARTNER_REJECTED",
        partner_ids=[partner_id],
    )

    return {
        "success": True,
        "message": "Booking rejected; returned to PENDING_ASSIGNMENT",
        "cab_status": "PENDING_ASSIGNMENT",
        "rejected_at": now.isoformat(),
        "reason_code": payload.reason_code,
    }


@router.get(
    "/me/reject-reasons",
    tags=["Partner Bookings"],
)
async def list_reject_reasons():
    """
    Return the partner-facing reject reason catalogue. Static — no DB.
    Mirrors the codes validated by POST /me/bookings/{bn}/reject.
    """
    from app.modules.partner.constants import REJECT_REASONS

    return {"items": REJECT_REASONS}


# ════════════════════════════════════════════════════════════════
# ASSIGN DRIVER — POST /partners/me/bookings/{booking_number}/assign-driver
# Partner assigns their own driver + vehicle to the cab booking.
# Doc Ref: Booking API §17, BRD Part 3 §42
# Guard: cab must be ASSIGNED (partner already set), not STARTED/COMPLETED
# Guard: driver and vehicle must belong to this partner and be APPROVED/ACTIVE
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/bookings/{booking_number}/assign-driver",
    tags=["Partner Bookings"],
)
async def partner_assign_driver(
    booking_number: str,
    payload: AssignDriverRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Partner assigns their own driver + vehicle to a cab booking.
    - cab must be status ASSIGNED (partner already selected by admin)
    - driver must be APPROVED/ACTIVE and belong to this partner
    - vehicle must be APPROVED/ACTIVE and belong to this partner
    - vehicle_category_id of vehicle must match cab booking (soft check, warn only)
    Updates cab booking status to DRIVER_ASSIGNED.
    Logs timeline event.
    Doc Ref: BRD Part 3 §42, Booking API §17
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    # Fetch cab booking
    cb = (
        await db.execute(
            select(CabBooking)
            .options(selectinload(CabBooking.assignments))
            .where(CabBooking.booking_number == booking_number)
        )
    ).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, "Cab booking not found.")

    # Ownership check — must be the partner on the ACTIVE assignment. After
    # a handover, the original partner's row is closed and they can no longer
    # mutate the booking.
    partner_assigns = [
        a
        for a in (cb.assignments or [])
        if a.partner_id == partner_id and a.closed_at is None
    ]
    if not partner_assigns:
        raise HTTPException(
            410,
            "This booking is no longer with you — it was handed over to "
            "another partner after a vehicle breakdown.",
        )

    # Status guard — can only assign driver when status is ASSIGNED
    if cb.booking_status not in ("ASSIGNED", "DRIVER_ASSIGNED"):
        raise HTTPException(
            400,
            f"Cannot assign driver: booking is '{cb.booking_status}'. "
            "Booking must be in ASSIGNED or DRIVER_ASSIGNED status.",
        )

    # Driver guard
    driver = (
        await db.execute(select(Driver).where(Driver.id == payload.driver_id))
    ).scalar_one_or_none()
    if not driver:
        raise HTTPException(404, "Driver not found.")
    if driver.partner_id != partner_id:
        raise HTTPException(400, "Driver does not belong to your fleet.")
    if driver.status not in ("APPROVED", "ACTIVE"):
        raise HTTPException(
            400,
            f"Driver is not eligible (status: {driver.status}). Must be APPROVED or ACTIVE.",
        )

    # Vehicle guard
    vehicle = (
        await db.execute(select(Vehicle).where(Vehicle.id == payload.vehicle_id))
    ).scalar_one_or_none()
    if not vehicle:
        raise HTTPException(404, "Vehicle not found.")
    if vehicle.partner_id != partner_id:
        raise HTTPException(400, "Vehicle does not belong to your fleet.")
    if vehicle.status not in ("APPROVED", "ACTIVE"):
        raise HTTPException(
            400,
            f"Vehicle is not eligible (status: {vehicle.status}). Must be APPROVED or ACTIVE.",
        )

    # Update the active assignment (closed_at IS NULL) so a prior handover
    # row is never mutated.
    latest_assign = sorted(
        [a for a in partner_assigns if a.closed_at is None],
        key=lambda a: a.assigned_at,
        reverse=True,
    )[0]
    latest_assign.driver_id = payload.driver_id
    latest_assign.vehicle_id = payload.vehicle_id

    # Advance cab booking status
    cb.booking_status = "DRIVER_ASSIGNED"

    # Log to master booking timeline
    await _log_timeline(
        db,
        cb.master_booking_id,
        "DRIVER_ASSIGNED",
        f"Partner assigned driver '{driver.full_name}' and vehicle '{vehicle.registration_number}' to cab {cb.booking_number}.",
    )
    # get_db auto-commits on success.

    await _notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=cb.master_booking_id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="DRIVER_ASSIGNED",
        action="ASSIGN_DRIVER",
        partner_ids=[partner_id],
    )

    return {
        "success": True,
        "message": "Driver and vehicle assigned successfully.",
        "cab_status": "DRIVER_ASSIGNED",
        "driver_name": driver.full_name,
        "vehicle_reg": vehicle.registration_number,
    }


# ════════════════════════════════════════════════════════════════
# PARTNER TRIP ASSISTANCE
# Partners can help drivers who cannot operate the driver app.
# Allowed actions (mirrors admin trip-assist but SCOPED TO PARTNER):
#   POST /me/bookings/{bn}/start-trip      — Start trip on driver's behalf
#   POST /me/bookings/{bn}/close-trip      — Close trip on driver's behalf
#   GET  /me/bookings/{bn}/pricing         — Pricing preview for close-trip
#   POST /me/bookings/{bn}/collect-payment — Collect CASH or WALLET payment
#   GET  /me/bookings/{bn}/download-invoice — Download PDF invoice
#   GET  /me/bookings/{bn}/customer-wallet  — Customer wallet balance
#
# NOT allowed (admin-only):
#   - settle booking
#   - change commission
#   - any action outside this partner's own assignment
#
# Doc Ref: BRD Part 3 §43-45, BRD Part 6 §129, §147
# ════════════════════════════════════════════════════════════════


class PartnerTripStartRequest(BaseModel):
    start_km: float
    start_datetime: datetime


class PartnerTripCloseRequest(BaseModel):
    end_km: float
    end_datetime: datetime
    final_amount: float


class PartnerCollectPaymentRequest(BaseModel):
    payment_mode: str  # CASH | WALLET
    payment_collected_by: str  # DRIVER | PARTNER


async def _get_partner_cab_booking(
    db: AsyncSession,
    booking_number: str,
    partner_id: int,
) -> tuple:
    """
    Fetch cab_booking + master_booking scoped to this partner.

    Raises:
      404 — booking / master booking not found
      403 — partner has no assignment row for this cab at all
      410 — cab is currently PENDING_PARTNER_ACCEPTANCE but the
            pending_partner_id has been reassigned away from this
            partner (admin reassigned, or sweeper timed out)
    """
    cb = (
        await db.execute(
            select(CabBooking)
            .options(selectinload(CabBooking.assignments))
            .where(CabBooking.booking_number == booking_number.strip().upper())
        )
    ).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, f"Booking '{booking_number}' not found.")

    partner_assigns = [a for a in cb.assignments if a.partner_id == partner_id]
    if not partner_assigns:
        raise HTTPException(403, "You are not authorized to manage this booking.")

    # Active-assignment guard — after a handover (migration 0041) the
    # original partner's row is closed; they must not be allowed to mutate
    # the booking that's now with a different partner. Skip this for
    # terminal states: close-trip also closes the assignment (reason
    # TRIP_COMPLETED), so a finished booking must not be locked out of
    # downstream steps like collect-payment.
    if cb.booking_status not in _PARTNER_TERMINAL_STATUSES:
        active_partner_assigns = [a for a in partner_assigns if a.closed_at is None]
        if not active_partner_assigns:
            raise HTTPException(
                410,
                "This booking is no longer with you — it was handed over to "
                "another partner after a vehicle breakdown. You may view it "
                "for audit but cannot start, close or modify it.",
            )

    # Guard against stale assignments from a prior admin reassignment:
    # if the cab is currently in PENDING_PARTNER_ACCEPTANCE, only the
    # partner in pending_partner_id is allowed to accept / reject. A
    # previously-assigned-but-no-longer-pending partner gets 410 Gone so
    # the FE can refetch and stop showing the action buttons.
    if (
        cb.booking_status == "PENDING_PARTNER_ACCEPTANCE"
        and cb.pending_partner_id is not None
        and cb.pending_partner_id != partner_id
    ):
        raise HTTPException(
            410,
            "This cab has been reassigned to another partner. "
            "Please refresh — your accept/reject no longer applies.",
        )

    mb = (
        await db.execute(
            select(MasterBooking)
            .options(selectinload(MasterBooking.timelines))
            .where(MasterBooking.id == cb.master_booking_id)
        )
    ).scalar_one_or_none()
    if not mb:
        raise HTTPException(404, "Master booking not found.")

    return mb, cb, partner_assigns


# ── START TRIP ────────────────────────────────────────────────────────────────


@router.post(
    "/me/bookings/{booking_number}/start-trip",
    summary="Partner starts trip on driver's behalf",
    tags=["Partner Bookings"],
)
async def partner_start_trip(
    booking_number: str,
    payload: PartnerTripStartRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    if cb.booking_status != "DRIVER_ASSIGNED":
        raise HTTPException(
            400,
            f"Cannot start trip: booking is '{cb.booking_status}'. "
            "Trip can only be started when status is DRIVER_ASSIGNED.",
        )
    if payload.start_km < 0:
        raise HTTPException(400, "Start KM cannot be negative.")

    cb.trip_start_km = Decimal(str(payload.start_km))
    cb.trip_started_at = payload.start_datetime
    cb.booking_status = "STARTED"

    # Migration 0041: snapshot the odometer onto the active assignment so the
    # audit trail records which km-reading each driver started from, even
    # after a mid-trip vehicle swap. Also flip the active vehicle/driver to
    # ON_TRIP — those statuses were defined in the schema but never wired up.
    active_assign = next(
        (a for a in (cb.assignments or []) if a.closed_at is None), None
    )
    if active_assign is not None:
        active_assign.km_at_assignment_start = Decimal(str(payload.start_km))
    await breakdown_service.mark_resources_on_trip(
        db,
        vehicle_id=active_assign.vehicle_id if active_assign else None,
        driver_id=active_assign.driver_id if active_assign else None,
        on_trip=True,
    )

    if mb.booking_status not in ("COMPLETED", "CLOSED", "CANCELLED"):
        mb.booking_status = "IN_PROGRESS"

    await _log_timeline(
        db,
        mb.id,
        "TRIP_STARTED_BY_PARTNER",
        f"Partner started trip for {cb.booking_number} on driver's behalf. "
        f"Start KM: {payload.start_km}, Started at: {payload.start_datetime.isoformat()}",
    )
    # get_db auto-commits on success.

    await _notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=mb.id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="STARTED",
        action="START_TRIP",
        partner_ids=[partner_id],
    )

    return {
        "success": True,
        "message": "Trip started successfully.",
        "cab_status": "STARTED",
        "trip_start_km": payload.start_km,
        "trip_started_at": payload.start_datetime.isoformat(),
    }


# ── PRICING PREVIEW ───────────────────────────────────────────────────────────


@router.get(
    "/me/bookings/{booking_number}/pricing",
    summary="Get pricing rule + calculated amount preview",
    tags=["Partner Bookings"],
)
async def partner_get_pricing(
    booking_number: str,
    end_km: float = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    city = (
        await db.execute(select(City).where(City.id == mb.city_id))
    ).scalar_one_or_none()

    from app.modules.admin.services import DefaultPricingService

    rule_data = None
    if cb.vehicle_category_id and mb.city_id and cb.trip_type:
        effective = await DefaultPricingService.get_effective_rule(
            db, mb.city_id, cb.vehicle_category_id, cb.trip_type
        )
        if effective:
            rule_data = {
                "base_fare": float(effective["base_fare"] or 0),
                "per_km_rate": float(effective["per_km_rate"] or 0),
                "minimum_km": int(effective["minimum_km"] or 0),
                "driver_allowance": float(effective["driver_allowance"] or 0),
                "night_charge": float(effective["night_charge"] or 0),
                "driver_allowance_type": effective.get(
                    "driver_allowance_type", "PER_TRIP"
                ),
                "night_charge_type": effective.get("night_charge_type", "FIXED"),
                "toll": float(effective.get("toll") or 0),
                "source": effective.get("source", "unknown"),
            }

    calculated_amount = None
    actual_distance = None
    if end_km is not None and cb.trip_start_km is not None:
        actual_distance = max(0.0, end_km - float(cb.trip_start_km))
        if rule_data:
            from app.modules.booking.services.fare import calculate_fare, trip_days_from

            # Pass both ends so night_charge fires when the trip falls in
            # the night window. Use the API parameter end_km and the
            # recorded trip_start_km as the start. trip_days from
            # pickup→return_datetime lets PER_DAY rules compute correctly
            # for a round trip.
            from datetime import datetime as _dt, timezone as _tz

            now_ist = _dt.now(_tz.utc)
            calculated_amount = float(
                calculate_fare(
                    rule_data,
                    actual_distance,
                    start_dt=now_ist,
                    end_dt=now_ist,
                    trip_type=cb.trip_type,
                    trip_days=trip_days_from(cb.pickup_datetime, cb.return_datetime),
                )
            )

    return {
        "booking_number": booking_number,
        "city_name": city.name if city else None,
        "trip_type": cb.trip_type,
        "trip_start_km": float(cb.trip_start_km) if cb.trip_start_km else None,
        "estimated_distance": (
            float(cb.estimated_distance) if cb.estimated_distance else None
        ),
        "estimated_amount": float(cb.estimated_amount) if cb.estimated_amount else None,
        "pricing_rule": rule_data,
        "actual_distance": actual_distance,
        "calculated_amount": calculated_amount,
    }


# ── CLOSE TRIP ────────────────────────────────────────────────────────────────


@router.post(
    "/me/bookings/{booking_number}/close-trip",
    summary="Partner closes trip on driver's behalf",
    tags=["Partner Bookings"],
)
async def partner_close_trip(
    booking_number: str,
    payload: PartnerTripCloseRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    from app.modules.admin.services import DefaultPricingService
    from app.modules.admin.models import CommissionRule, SystemConfiguration

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, partner_assigns = await _get_partner_cab_booking(
        db, booking_number, partner_id
    )

    if cb.booking_status != "STARTED":
        raise HTTPException(
            400,
            f"Cannot close trip: booking is '{cb.booking_status}'. "
            "Trip must be in STARTED status to close.",
        )

    start_km = float(cb.trip_start_km) if cb.trip_start_km is not None else None
    if start_km is not None and payload.end_km < start_km:
        raise HTTPException(
            400, f"End KM ({payload.end_km}) cannot be less than start KM ({start_km})."
        )

    if cb.trip_started_at:
        from datetime import timezone as _tz

        end_dt = payload.end_datetime
        start_dt = cb.trip_started_at
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=_tz.utc)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=_tz.utc)
        if end_dt < start_dt:
            raise HTTPException(400, "End datetime cannot be before trip start time.")

    if payload.final_amount <= 0:
        raise HTTPException(400, "Final amount must be greater than 0.")

    # Fare sanity check (±50% of calculated fare — warn only, not hard-block for partner)
    # Doc Ref: BRD Part 3 §35 — uses the shared fare formula in
    # app.modules.booking.services.fare so admin/partner cannot drift.
    fare_warning = None
    calculated_fare = None
    if cb.vehicle_category_id and mb.city_id and cb.trip_type:
        effective = await DefaultPricingService.get_effective_rule(
            db, mb.city_id, cb.vehicle_category_id, cb.trip_type
        )
        if effective:
            from app.modules.booking.services.fare import calculate_fare, trip_days_from

            actual_dist = (
                (payload.end_km - start_km) if start_km is not None else payload.end_km
            )
            calculated_fare = float(
                calculate_fare(
                    effective,
                    actual_dist,
                    start_dt=cb.trip_started_at,
                    end_dt=payload.end_datetime,
                    trip_type=cb.trip_type,
                    trip_days=trip_days_from(cb.pickup_datetime, cb.return_datetime),
                )
            )
            if calculated_fare > 0:
                deviation_pct = (
                    abs(payload.final_amount - calculated_fare) / calculated_fare * 100
                )
                if deviation_pct > 50:
                    raise HTTPException(
                        400,
                        f"Final amount ₹{payload.final_amount:.2f} deviates {deviation_pct:.1f}% "
                        f"from calculated fare ₹{calculated_fare:.2f} (allowed ±50%).",
                    )
                elif deviation_pct > 20:
                    fare_warning = (
                        f"Note: Final amount ₹{payload.final_amount:.2f} deviates "
                        f"{deviation_pct:.1f}% from calculated fare ₹{calculated_fare:.2f}."
                    )

    actual_distance = (
        Decimal(str(payload.end_km)) - cb.trip_start_km
        if cb.trip_start_km is not None
        else Decimal(str(payload.end_km))
    )
    if actual_distance < 0:
        actual_distance = Decimal("0.00")

    # Commission lookup (same as admin)
    commission_percent = 10.0
    commission_source = "default_10pct"
    p = (
        await db.execute(select(Partner).where(Partner.id == partner_id))
    ).scalar_one_or_none()
    if p and p.city_id:
        city_rule = (
            await db.execute(
                select(CommissionRule)
                .where(
                    CommissionRule.service_type == "CAB",
                    CommissionRule.commission_type == "PERCENTAGE",
                    CommissionRule.city_id == p.city_id,
                    CommissionRule.is_active,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if city_rule:
            commission_percent = float(city_rule.commission_value or 10)
            commission_source = f"city_rule:{city_rule.id}"
        else:
            gen_rule = (
                await db.execute(
                    select(CommissionRule)
                    .where(
                        CommissionRule.service_type == "CAB",
                        CommissionRule.commission_type == "PERCENTAGE",
                        CommissionRule.is_active,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if gen_rule:
                commission_percent = float(gen_rule.commission_value or 10)
                commission_source = f"general_rule:{gen_rule.id}"

    final_dec = Decimal(str(payload.final_amount))
    platform_commission = Decimal(
        str(round(payload.final_amount * commission_percent / 100, 2))
    )
    partner_payout = final_dec - platform_commission

    # GST from system config
    gst_cfg_rows = (
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
    gst_cfg = {r.config_key: (r.config_value or "").strip() for r in gst_cfg_rows}
    gst_enabled = gst_cfg.get("GST_ENABLED", "false").lower() == "true"
    gst_rate_pct = 0.0
    gst_amount_dec = Decimal("0.00")
    if gst_enabled:
        try:
            gst_rate_pct = float(gst_cfg.get("GST_RATE", "5"))
        except ValueError:
            gst_rate_pct = 5.0
        gst_amount_dec = Decimal(str(round(float(final_dec) * gst_rate_pct / 100, 2)))

    cb.trip_end_km = Decimal(str(payload.end_km))
    cb.trip_ended_at = payload.end_datetime
    cb.final_amount = final_dec
    cb.actual_distance = actual_distance
    cb.platform_commission = platform_commission
    cb.partner_payout = partner_payout
    cb.booking_status = "COMPLETED"
    # Migration 0041: close the active assignment so subsequent handover-aware
    # queries don't pick up a stale open row. Also release the vehicle/driver
    # back to ACTIVE / OFFLINE so the resources become assignable again.
    closed_id = await breakdown_service.close_assignment_on_trip_complete(
        db, cab_booking_id=cb.id, end_km=cb.trip_end_km
    )
    closed_assign = (
        next((a for a in (cb.assignments or []) if a.id == closed_id), None)
        if closed_id is not None
        else None
    )
    if closed_assign is not None:
        await breakdown_service.mark_resources_on_trip(
            db,
            vehicle_id=closed_assign.vehicle_id,
            driver_id=closed_assign.driver_id,
            on_trip=False,
        )
    cb.gst_rate = Decimal(str(gst_rate_pct))
    cb.gst_amount = gst_amount_dec
    cb.is_tax_invoice = gst_enabled

    mb.total_amount = final_dec
    mb.booking_status = "COMPLETED"

    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func

    coupon_q = await db.execute(
        select(_func.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    advance_paid = await advance_service.get_advance_amount(db, cb.id)
    # Gross-based, so this matches the customer-facing invoice PDF exactly.
    balance_due = advance_service.compute_balance_due(
        final_amount=payload.final_amount,
        coupon_discount=coupon_discount,
        advance_paid=advance_paid,
        gst_amount=float(gst_amount_dec),
        is_tax_invoice=gst_enabled,
    )

    await _log_timeline(
        db,
        mb.id,
        "TRIP_CLOSED_BY_PARTNER",
        f"Partner closed trip for {cb.booking_number} on driver's behalf. "
        f"End KM: {payload.end_km} | Distance: {float(actual_distance):.1f} km | "
        f"Final: ₹{payload.final_amount:.2f} | Coupon: −₹{coupon_discount:.2f} | "
        f"Advance: −₹{advance_paid:.2f} | Balance due: ₹{balance_due:.2f} | "
        f"Commission ({commission_percent}%, {commission_source}): ₹{float(platform_commission):.2f} | "
        f"Partner payout: ₹{float(partner_payout):.2f}"
        + (f" | {fare_warning}" if fare_warning else ""),
    )
    # get_db auto-commits on success.

    await _notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=mb.id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="COMPLETED",
        action="CLOSE_TRIP",
        payment_status=mb.payment_status,
        partner_ids=[partner_id],
    )

    # ── Email: trip completed to the customer ──
    try:
        from app.infrastructure.email import send_event_email

        cust_row = (
            await db.execute(
                select(User.email, Customer.first_name, Customer.last_name)
                .join(Customer, Customer.user_id == User.id)
                .where(Customer.id == mb.customer_id)
            )
        ).first()
        if cust_row and cust_row.email:
            name = (
                " ".join(filter(None, [cust_row.first_name, cust_row.last_name]))
                or "there"
            )
            await send_event_email(
                db,
                event_type="booking_completed",
                to_email=cust_row.email,
                to_name=name,
                context={
                    "name": name,
                    "service": "Cab",
                    "booking_number": cb.booking_number,
                    "message": (
                        "Your trip is complete — thank you for riding with WayTero. "
                        "Your tax invoice is available in your bookings."
                    ),
                    "details": [
                        ("Pickup", cb.pickup_location or ""),
                        ("Drop", cb.drop_location or ""),
                    ],
                    "amount": float(final_dec),
                    "amount_label": "Trip fare",
                },
                related_type="CAB_BOOKING",
                related_id=cb.id,
            )
    except Exception:  # pragma: no cover — email must never break bookings
        pass

    return {
        "success": True,
        "message": "Trip closed successfully."
        + (f" Warning: {fare_warning}" if fare_warning else ""),
        "cab_status": "COMPLETED",
        "trip_end_km": payload.end_km,
        "actual_distance": float(actual_distance),
        "final_amount": float(final_dec),
        "calculated_fare": calculated_fare,
        "fare_warning": fare_warning,
        "coupon_discount": coupon_discount,
        "advance_paid": advance_paid,
        "balance_due": balance_due,
        "platform_commission": float(platform_commission),
        "commission_percent": commission_percent,
        "partner_payout": float(partner_payout),
    }


# ── CUSTOMER WALLET BALANCE ───────────────────────────────────────────────────


@router.get(
    "/me/bookings/{booking_number}/customer-wallet",
    summary="Get customer wallet balance for a booking",
    tags=["Partner Bookings"],
)
async def partner_get_customer_wallet(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    from sqlalchemy import text as _text2

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    wallet_row = (
        (
            await db.execute(
                _text2(
                    "SELECT id, available_balance, hold_balance, wallet_status FROM customer_wallets WHERE customer_id = :cid"
                ),
                {"cid": mb.customer_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not wallet_row:
        return {
            "customer_id": mb.customer_id,
            "wallet_found": False,
            "available_balance": 0.0,
            "hold_balance": 0.0,
            "wallet_status": None,
        }

    return {
        "customer_id": mb.customer_id,
        "wallet_found": True,
        "available_balance": float(wallet_row["available_balance"] or 0),
        "hold_balance": float(wallet_row["hold_balance"] or 0),
        "wallet_status": wallet_row["wallet_status"],
    }


# ── COLLECT PAYMENT ───────────────────────────────────────────────────────────


@router.post(
    "/me/bookings/{booking_number}/collect-payment",
    summary="Partner collects payment after trip completion",
    tags=["Partner Bookings"],
)
async def partner_collect_payment(
    booking_number: str,
    payload: PartnerCollectPaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    from sqlalchemy import text as _text3, func as _func2
    from app.modules.admin.coupon_models import CouponUsage

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    if cb.booking_status != "COMPLETED":
        raise HTTPException(
            400,
            f"Cannot collect payment: booking is '{cb.booking_status}'. Must be COMPLETED.",
        )

    # Lock the cab row FOR UPDATE so two concurrent requests cannot both pass
    # the duplicate-payment guard below (TOCTOU race). The second request
    # blocks here until the first commits, then re-reads the committed
    # payment_mode and is rejected.
    locked_cb = (
        await db.execute(
            select(CabBooking).where(CabBooking.id == cb.id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked_cb is not None:
        cb = locked_cb

    # ── Duplicate payment guard ───────────────────────────────────────────────
    if cb.payment_mode:
        raise HTTPException(
            400,
            f"Payment already recorded for this booking (mode: {cb.payment_mode}). "
            "Duplicate payment collection is not allowed.",
        )

    mode = payload.payment_mode.upper()
    if mode not in {"CASH", "WALLET"}:
        raise HTTPException(
            400, "Only CASH or WALLET payment is supported from the partner portal."
        )

    collector = payload.payment_collected_by.upper()
    if collector not in {"DRIVER", "PARTNER"}:
        raise HTTPException(400, "payment_collected_by must be DRIVER or PARTNER.")

    final = float(cb.final_amount or cb.estimated_amount or 0)
    if final <= 0:
        raise HTTPException(
            400, "Final amount is not set. Close the trip before collecting payment."
        )

    commission = cb.platform_commission or Decimal("0.00")
    payout = cb.partner_payout or Decimal(str(final))

    coupon_q = await db.execute(
        select(_func2.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    advance_paid = await advance_service.get_advance_amount(db, cb.id)
    balance_due = advance_service.compute_balance_due(
        final_amount=final,
        coupon_discount=coupon_discount,
        advance_paid=advance_paid,
        gst_amount=float(getattr(cb, "gst_amount", 0) or 0),
        is_tax_invoice=bool(getattr(cb, "is_tax_invoice", False)),
    )

    cb.payment_mode = mode
    cb.payment_collected_by = collector
    cb.booking_status = "SETTLEMENT_PENDING"  # Move to settlement flow after payment

    if mode == "CASH":
        cb.cash_pending_at = "DRIVER"
        cb.cash_amount_due = Decimal(str(balance_due))
        await _log_timeline(
            db,
            mb.id,
            "PAYMENT_CASH_COLLECTED",
            f"Cash payment ₹{balance_due:.2f} collected by {collector}. "
            f"Fare: ₹{final:.2f} | Coupon: −₹{coupon_discount:.2f} | Advance: −₹{advance_paid:.2f}. "
            "Cash pending at: DRIVER.",
        )

    elif mode == "WALLET":
        wallet_row = (
            (
                await db.execute(
                    _text3(
                        "SELECT id, available_balance, wallet_status FROM customer_wallets WHERE customer_id = :cid FOR UPDATE"
                    ),
                    {"cid": mb.customer_id},
                )
            )
            .mappings()
            .one_or_none()
        )

        if not wallet_row:
            raise HTTPException(400, "Customer wallet not found.")
        if wallet_row["wallet_status"] != "ACTIVE":
            raise HTTPException(
                400,
                f"Customer wallet is '{wallet_row['wallet_status']}' — cannot process payment.",
            )

        avail = float(wallet_row["available_balance"] or 0)
        if avail < balance_due:
            raise HTTPException(
                400,
                f"Insufficient wallet balance. Required: ₹{balance_due:.2f} | Available: ₹{avail:.2f}.",
            )

        new_balance = round(avail - balance_due, 2)
        await db.execute(
            _text3(
                "UPDATE customer_wallets SET available_balance = :bal, updated_at = NOW() WHERE id = :wid"
            ),
            {"bal": new_balance, "wid": wallet_row["id"]},
        )
        await db.execute(
            _text3(
                """
                INSERT INTO customer_wallet_ledger
                    (customer_wallet_id, transaction_reference, reference_type,
                     debit_amount, credit_amount, balance_after, narration, created_at)
                VALUES (:wid, :ref, 'CAB_PAYMENT', :debit, 0, :bal_after, :narration, NOW())
            """
            ),
            {
                "wid": wallet_row["id"],
                "ref": cb.booking_number,
                "debit": balance_due,
                "bal_after": new_balance,
                "narration": f"CAB trip payment — {cb.booking_number} | Fare ₹{final:.2f} | Deducted ₹{balance_due:.2f}",
            },
        )
        cb.cash_pending_at = "NONE"
        await _log_timeline(
            db,
            mb.id,
            "PAYMENT_WALLET_COLLECTED",
            f"Wallet payment ₹{balance_due:.2f} deducted. New balance: ₹{new_balance:.2f}. "
            f"Commission: ₹{float(commission):.2f} | Partner payout: ₹{float(payout):.2f}.",
        )

    mb.payment_status = "PAID"
    # What the customer actually handed over: advance + the balance just taken.
    # This used to be set to the full fare, which overstated collections on
    # discounted bookings and destroyed the advance figure other code read back.
    mb.total_paid_amount = Decimal(str(round(advance_paid + balance_due, 2)))

    # Auto-generate invoice number (race-safe: use sequence, not COUNT)
    if not cb.invoice_number:
        from datetime import date as _date
        from sqlalchemy import text as _text4

        _today = _date.today()
        # Use MAX-based sequence to avoid COUNT race (two concurrent payments could get same count)
        _max_row = (
            await db.execute(
                _text4(
                    "SELECT MAX(CAST(SPLIT_PART(invoice_number, '-', 4) AS INTEGER)) FROM cab_bookings WHERE invoice_number IS NOT NULL AND invoice_number LIKE 'WT-INV-%'"
                )
            )
        ).scalar()
        _next_seq = (_max_row or 0) + 1
        cb.invoice_number = (
            f"WT-INV-{_today.strftime('%Y%m')}-{str(_next_seq).zfill(5)}"
        )
        await _log_timeline(
            db,
            mb.id,
            "INVOICE_AUTO_GENERATED",
            f"Invoice {cb.invoice_number} auto-generated at payment collection by partner. Mode: {mode}.",
        )

    try:
        await db.commit()
    except Exception as _commit_err:
        await db.rollback()
        cb_refresh = (
            await db.execute(
                select(CabBooking).where(CabBooking.booking_number == cb.booking_number)
            )
        ).scalar_one_or_none()
        if cb_refresh and cb_refresh.payment_mode:
            # Payment was already committed by a concurrent request — treat as duplicate
            raise HTTPException(
                400,
                f"Payment already recorded (concurrent request). Mode: {cb_refresh.payment_mode}.",
            )
        raise HTTPException(500, f"Payment commit failed: {str(_commit_err)}")

    await _notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=mb.id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="SETTLEMENT_PENDING",
        action="COLLECT_PAYMENT",
        payment_status="PAID",
        partner_ids=[partner_id],
    )

    # ── Email: payment received + invoice ready to the customer ──
    try:
        from app.infrastructure.email import send_event_email

        cust_row = (
            await db.execute(
                select(User.email, Customer.first_name, Customer.last_name)
                .join(Customer, Customer.user_id == User.id)
                .where(Customer.id == mb.customer_id)
            )
        ).first()
        if cust_row and cust_row.email:
            name = (
                " ".join(filter(None, [cust_row.first_name, cust_row.last_name]))
                or "there"
            )
            await send_event_email(
                db,
                event_type="payment_received",
                to_email=cust_row.email,
                to_name=name,
                context={
                    "name": name,
                    "service": "Cab",
                    "booking_number": cb.booking_number,
                    "message": (
                        f"Payment of ₹{balance_due:,.2f} received via {mode} for your trip. "
                        "Your tax invoice has been generated automatically."
                    ),
                    "details": [
                        ("Booking", cb.booking_number),
                        ("Trip fare", f"₹{final:,.2f}"),
                        ("Balance paid", f"₹{balance_due:,.2f}"),
                        ("Mode", mode),
                        ("Invoice no.", cb.invoice_number or ""),
                    ],
                    "amount": balance_due,
                    "amount_label": "Amount received",
                },
                related_type="CAB_BOOKING",
                related_id=cb.id,
            )
    except Exception:  # pragma: no cover — email must never break payments
        pass

    return {
        "success": True,
        "message": "Payment recorded successfully.",
        "payment_mode": mode,
        "payment_collected_by": collector,
        "final_amount": final,
        "coupon_discount": coupon_discount,
        "advance_paid": advance_paid,
        "balance_due": balance_due,
        "platform_commission": float(commission),
        "partner_payout": float(payout),
        "cash_pending_at": cb.cash_pending_at,
        "invoice_number": cb.invoice_number,
        "booking_status": cb.booking_status,
    }


# ── DOWNLOAD INVOICE ──────────────────────────────────────────────────────────


@router.get(
    "/me/bookings/{booking_number}/download-invoice",
    summary="Partner downloads PDF invoice for completed booking",
    tags=["Partner Bookings"],
)
async def partner_download_invoice(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    from fastapi import Response
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.invoice_pdf_service import generate_invoice_pdf
    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func4
    from datetime import timezone as _tz3

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    mb, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    if cb.booking_status not in ("COMPLETED", "SETTLEMENT_PENDING", "SETTLED"):
        raise HTTPException(
            400,
            f"Invoice only available for completed bookings (current: {cb.booking_status}).",
        )
    if not cb.payment_mode:
        raise HTTPException(400, "Record payment before downloading invoice.")
    if not cb.invoice_number:
        raise HTTPException(
            400,
            "Invoice number not found. Collect payment first to auto-generate invoice.",
        )

    config_rows = (
        (
            await db.execute(
                select(SystemConfiguration).where(
                    SystemConfiguration.config_key.in_(
                        [
                            "PLATFORM_NAME",
                            "PLATFORM_LOGO_URL",
                            "BUSINESS_LEGAL_NAME",
                            "BUSINESS_GST_NUMBER",
                            "BUSINESS_REGISTERED_ADDRESS",
                            "SUPPORT_EMAIL",
                            "SUPPORT_PHONE",
                            "GST_ENABLED",
                            "GST_RATE",
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    cfg = {row.config_key: (row.config_value or "") for row in config_rows}

    cust = (
        await db.execute(select(Customer).where(Customer.id == mb.customer_id))
    ).scalar_one_or_none()
    cust_user = None
    if cust:
        cust_user = (
            await db.execute(select(User).where(User.id == cust.user_id))
        ).scalar_one_or_none()

    city = (
        await db.execute(select(City).where(City.id == mb.city_id))
    ).scalar_one_or_none()

    cat_name = driver_name = vehicle_reg = vehicle_model = partner_name = None
    if cb.vehicle_category_id:
        vc = (
            await db.execute(
                select(VehicleCategory).where(
                    VehicleCategory.id == cb.vehicle_category_id
                )
            )
        ).scalar_one_or_none()
        cat_name = vc.category_name if vc else None

    p = (
        await db.execute(select(Partner).where(Partner.id == partner_id))
    ).scalar_one_or_none()
    if p:
        partner_name = p.business_name or f"Partner #{p.id}"

    if cb.assignments:
        # Active only — handovered/closed rows must not surface as current.
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        if latest.driver_id:
            d = (
                await db.execute(select(Driver).where(Driver.id == latest.driver_id))
            ).scalar_one_or_none()
            if d:
                driver_name = d.full_name
        if latest.vehicle_id:
            v = (
                await db.execute(select(Vehicle).where(Vehicle.id == latest.vehicle_id))
            ).scalar_one_or_none()
            if v:
                vehicle_reg = v.registration_number
                vehicle_model = (
                    f"{v.vehicle_brand or ''} {v.vehicle_model or ''}".strip() or None
                )

    coupon_q = await db.execute(
        select(_func4.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    advance_paid = await advance_service.get_advance_amount(db, cb.id)

    pdf_bytes = generate_invoice_pdf(
        invoice_number=cb.invoice_number,
        cab_booking_number=cb.booking_number,
        booking_number=mb.booking_number,
        invoice_date=datetime.now(_tz3.utc),
        customer_name=cust.full_name if cust else None,
        customer_mobile=cust_user.mobile_number if cust_user else None,
        city_name=city.name if city else None,
        pickup_location=cb.pickup_location,
        drop_location=cb.drop_location,
        trip_type=cb.trip_type,
        vehicle_category_name=cat_name,
        vehicle_reg=vehicle_reg,
        vehicle_model=vehicle_model,
        driver_name=driver_name,
        partner_name=partner_name,
        trip_started_at=cb.trip_started_at,
        trip_ended_at=cb.trip_ended_at,
        trip_start_km=float(cb.trip_start_km) if cb.trip_start_km is not None else None,
        trip_end_km=float(cb.trip_end_km) if cb.trip_end_km is not None else None,
        actual_distance=(
            float(cb.actual_distance) if cb.actual_distance is not None else None
        ),
        estimated_amount=float(cb.estimated_amount) if cb.estimated_amount else None,
        final_amount=float(cb.final_amount or 0),
        coupon_discount=coupon_discount,
        advance_paid=advance_paid,
        payment_mode=cb.payment_mode,
        payment_collected_by=cb.payment_collected_by,
        platform_commission=(
            float(cb.platform_commission) if cb.platform_commission else None
        ),
        is_tax_invoice=bool(getattr(cb, "is_tax_invoice", False)),
        gst_rate=float(cb.gst_rate) if getattr(cb, "gst_rate", None) else 0.0,
        gst_amount=float(cb.gst_amount) if getattr(cb, "gst_amount", None) else 0.0,
        platform_name=cfg.get("PLATFORM_NAME", "WayTero"),
        platform_logo_url=cfg.get("PLATFORM_LOGO_URL", ""),
        business_legal_name=cfg.get("BUSINESS_LEGAL_NAME", ""),
        business_gst_number=cfg.get("BUSINESS_GST_NUMBER", ""),
        business_registered_address=cfg.get("BUSINESS_REGISTERED_ADDRESS", ""),
        support_email=cfg.get("SUPPORT_EMAIL", ""),
        support_phone=cfg.get("SUPPORT_PHONE", ""),
    )

    filename = f"Invoice_{cb.invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ════════════════════════════════════════════════════════════════
# ADVANCE PAYMENT — partner side
#
# The partner can take an advance on a booking assigned to them, and the money
# then sits on the partner side of the ledger until settlement nets it off.
#
# Two asymmetries against the admin routes, both deliberate:
#   • received_by is restricted to PARTNER | DRIVER. A partner cannot record the
#     platform as having received money — that is the platform's own assertion,
#     and it decides the settlement direction.
#   • There is no void route. Releasing the one-advance-per-booking lock is a
#     platform decision; a partner who mis-keys an advance asks admin to void it.
#
# All other rules come from the shared service, so the two portals cannot drift.
#
# Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
# ════════════════════════════════════════════════════════════════


class PartnerCollectAdvanceRequest(BaseModel):
    amount: float
    payment_mode: str  # CASH | UPI
    received_by: str  # PARTNER | DRIVER
    reference_note: Optional[str] = None


@router.get(
    "/me/bookings/{booking_number}/advance",
    summary="Partner views the advance on a booking + collection eligibility",
    tags=["Partner Bookings"],
)
async def partner_get_advance(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    _, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    data = await advance_service.get_eligibility(db, cb.id)
    # The partner form never offers ADMIN, so filter it out of the options the
    # service reports rather than letting the UI render a receiver it can't send.
    data["allowed_receivers"] = [
        r for r in data["allowed_receivers"] if r != advance_service.RECEIVER_ADMIN
    ]
    data["modes_by_receiver"] = {
        r: m
        for r, m in data["modes_by_receiver"].items()
        if r != advance_service.RECEIVER_ADMIN
    }
    if data["can_collect"] and not data["allowed_receivers"]:
        data["can_collect"] = False
        data["blocked_reason"] = (
            "No driver is assigned yet — assign a driver before collecting an advance."
        )

    return success_response("Advance details retrieved successfully", data)


@router.post(
    "/me/bookings/{booking_number}/advance",
    summary="Partner collects an advance payment from the customer",
    tags=["Partner Bookings"],
)
async def partner_collect_advance(
    booking_number: str,
    payload: PartnerCollectAdvanceRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    _, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    receiver = (payload.received_by or "").strip().upper()
    if receiver not in advance_service.PARTNER_SIDE_RECEIVERS:
        raise HTTPException(
            400,
            "A partner can only record an advance as received by PARTNER or DRIVER. "
            "Ask admin to record advances collected by the platform.",
        )

    advance = await advance_service.create_advance(
        db,
        cab_booking_id=cb.id,
        amount=payload.amount,
        payment_mode=payload.payment_mode,
        received_by=receiver,
        user_id=current_user["sub"],
        source_role="PARTNER",
        reference_note=payload.reference_note,
    )

    return success_response(
        f"Advance of Rs. {float(advance['amount']):,.2f} recorded. "
        f"Receipt {advance['receipt_number']}.",
        {
            "receipt_number": advance["receipt_number"],
            "amount": float(advance["amount"]),
            "payment_mode": advance["payment_mode"],
            "received_by": advance["received_by"],
            "collected_at": (
                advance["collected_at"].isoformat() if advance["collected_at"] else None
            ),
            "fare": advance["fare"],
            "balance_after_advance": advance["balance_after_advance"],
        },
    )


@router.get(
    "/me/bookings/{booking_number}/advance/receipt",
    summary="Partner downloads the advance payment receipt PDF",
    tags=["Partner Bookings"],
)
async def partner_download_advance_receipt(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    _, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    doc = await advance_service.build_receipt_pdf(db, cb.id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


# ════════════════════════════════════════════════════════════════
# BREAKDOWN / SWAP — partner-side surface
# Doc Ref: Spec section "Cab Breakdown → Vehicle Swap (in-trip)"
#
# Partners get two endpoints (under the same /me/bookings/{n}/... path):
#   POST /me/bookings/{booking_number}/report-breakdown
#     — partner reports on driver's behalf; partner can self-serve when
#       the driver can't reach the app.
#   POST /me/bookings/{booking_number}/swap-vehicle
#     — same-partner swap, only to a vehicle/driver belonging to the
#       partner themselves. Honours PARTNER_SELF_SERVE_SWAP_ENABLED.
#
# Cross-partner handover is admin-only — partner can't hand off to another
# partner (admin must arrange that).
# ════════════════════════════════════════════════════════════════


class _PartnerReportBreakdownRequest(BaseModel):
    reason_code: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    notes: Optional[str] = None


class _PartnerSwapVehicleRequest(BaseModel):
    new_vehicle_id: int
    new_driver_id: int
    notes: Optional[str] = None


@router.post(
    "/me/bookings/{booking_number}/report-breakdown",
    tags=["Partner Bookings"],
    summary="Partner reports a vehicle breakdown for an active booking",
)
async def partner_report_breakdown(
    booking_number: str,
    payload: _PartnerReportBreakdownRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partner logs a breakdown on behalf of their driver. Validated the
    same way as the admin endpoint — the booking must be in DRIVER_ASSIGNED
    or STARTED, the partner must own the active assignment. Re-reporting
    an already-broken-down booking just refreshes the location/notes.
    """
    from decimal import Decimal
    from uuid import UUID as _UUID

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    _, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    snapshot = await breakdown_service.report_breakdown(
        db,
        cab_booking_id=cb.id,
        reported_by=breakdown_service.REPORTER_PARTNER,
        reported_by_user_id=(
            _UUID(current_user["sub"]) if current_user.get("sub") else None
        ),
        reporter_partner_id=partner_id,
        reporter_driver_id=None,
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
        "Breakdown reported by partner",
        snapshot,
    )


@router.post(
    "/me/bookings/{booking_number}/swap-vehicle",
    tags=["Partner Bookings"],
    summary="Partner self-serves a swap to their own other vehicle/driver",
)
async def partner_swap_vehicle(
    booking_number: str,
    payload: _PartnerSwapVehicleRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Same as admin POST /admin/cab-ops/breakdown/{cab_id}/swap-same-partner
    but called from the partner portal. Restricted to swapping within the
    partner's own fleet. Cross-partner handover is admin-only.

    Behaviour mirrors the spec: same partner → payout/commission unchanged.
    """
    from uuid import UUID as _UUID

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    _, cb, _ = await _get_partner_cab_booking(db, booking_number, partner_id)

    snapshot = await breakdown_service.swap_vehicle_same_partner(
        db,
        cab_booking_id=cb.id,
        new_vehicle_id=payload.new_vehicle_id,
        new_driver_id=payload.new_driver_id,
        performed_by=breakdown_service.REPORTER_PARTNER,
        performed_by_user_id=(
            _UUID(current_user["sub"]) if current_user.get("sub") else None
        ),
        performed_by_partner_id=partner_id,
    )
    return success_response(
        "Vehicle swapped within your fleet",
        snapshot,
    )


@router.get(
    "/me/breakdown/reasons",
    tags=["Partner Bookings"],
    summary="Breakdown reasons exposed to the partner portal",
)
async def list_breakdown_reasons_for_partner(
    current_user: dict = Depends(get_current_user),
):
    from app.modules.partner.constants import BREAKDOWN_REASONS

    return success_response(
        "Breakdown reasons",
        {"items": BREAKDOWN_REASONS},
    )
