# ============================================================
# WAYTERO — ADMIN BOOKING API
# File: app/modules/admin/booking_api.py
# Prefix: /admin/bookings  (registered in api/router.py)
# Doc Ref:
#   DB Schema Part 4 — Booking Engine
#   DB Schema Part 7 — Finance (payments, settlements)
#   Admin API §11 — Booking Operations
#   Booking API §3 — Status Flow
#   Booking API §8 — Cab Status Flow
# ============================================================

import json
import math
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_, desc, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.booking import services as advance_service
from app.modules.booking.services import rollup_hotel_totals_into_master
from app.modules.booking.models import (
    MasterBooking,
    BookingService,
    CabBooking,
    CabBookingAssignment,
    BookingTimeline,
    BookingNote,
)
from app.modules.hotel.constants import (
    HOTEL_ADVANCE_STRATEGIES,
    HOTEL_ADVANCE_REFUND_RECORDED,
    HOTEL_SPLIT_TYPES,
)
from app.modules.hotel.models import (
    HotelReservation,
    Hotel,
    HotelRoomCategory,
    HotelRoom,
    HotelCheckin,
    HotelAdvancePayment,
)
from app.modules.hotel.services import billing as hotel_billing
from app.modules.hotel.services.hotel_helpers import (
    check_in_date_mismatch as _check_in_date_mismatch,
)
from app.modules.hotel.services.switch import (
    quote_switch,
    split_stay_after_partial_checkin,
    switch_hotel_pre_checkin,
)
from app.modules.customer.models import Customer
from app.modules.partner.models import Partner, PartnerService
from app.modules.driver.models import Driver, DriverAvailability
from app.modules.vehicle.models import Vehicle, VehicleCategory
from app.modules.master.models import City
from app.modules.auth.models.user import User
from app.modules.tour.models import TourBooking, TourPackage


async def _customer_email_and_name(
    db: AsyncSession, customer_id: int
) -> tuple[str, Optional[str]]:
    """Resolve the customer's email + display name for transactional emails."""
    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT u.email, c.first_name, c.last_name
                    FROM customers c
                    JOIN users u ON u.id = c.user_id
                    WHERE c.id = :cid
                    """
                ),
                {"cid": customer_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        return "", None
    name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or None
    return (row["email"] or "").strip(), name


async def _send_booking_email(
    db: AsyncSession,
    *,
    event_type: str,
    customer_id: int,
    context: dict,
    related_type: str,
    related_id: int,
) -> None:
    """Fire a transactional email to a booking's customer. Never raises —
    the email engine logs failures and the booking flow must not break."""
    try:
        from app.infrastructure.email import send_event_email

        to_email, to_name = await _customer_email_and_name(db, customer_id)
        if not to_email:
            return
        context.setdefault("name", to_name or "there")
        await send_event_email(
            db,
            event_type=event_type,
            to_email=to_email,
            to_name=to_name,
            context=context,
            related_type=related_type,
            related_id=related_id,
        )
    except Exception:  # pragma: no cover — email must never break bookings
        pass


router = APIRouter(
    dependencies=[
        Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO", "FINANCE_MANAGER"))
    ]
)

# ── Valid status transitions (guards against wrong admin actions) ──
# Doc Ref: DB Schema Part 4 §4 & §8

MASTER_BOOKING_TRANSITIONS: dict[str, list[str]] = {
    "DRAFT": ["PENDING_PAYMENT", "CANCELLED"],
    "PENDING_PAYMENT": ["CONFIRMED", "CANCELLED"],
    "CONFIRMED": ["IN_PROGRESS", "CANCELLED"],
    "IN_PROGRESS": ["COMPLETED"],
    "COMPLETED": ["CLOSED"],
    "CLOSED": [],
    "CANCELLED": [],
}

CAB_BOOKING_TRANSITIONS: dict[str, list[str]] = {
    # PENDING_ASSIGNMENT now has THREE outgoing paths:
    #   - PENDING_PARTNER_ACCEPTANCE: normal flow (admin assigns, partner accepts)
    #   - ASSIGNED:                   legacy direct flow (kept so a future
    #                                 "skip acceptance" config flag works)
    #   - CANCELLED:                  terminal
    "PENDING_ASSIGNMENT": ["PENDING_PARTNER_ACCEPTANCE", "ASSIGNED", "CANCELLED"],
    "PENDING_PARTNER_ACCEPTANCE": ["ASSIGNED", "PENDING_ASSIGNMENT", "CANCELLED"],
    "ASSIGNED": ["DRIVER_ASSIGNED", "PENDING_ASSIGNMENT", "CANCELLED"],
    # Driver/vehicle on the road. BREAKDOWN_REPORTED is the in-trip
    # interruption point.
    "DRIVER_ASSIGNED": ["STARTED", "ASSIGNED", "CANCELLED", "BREAKDOWN_REPORTED"],
    "STARTED": ["COMPLETED", "BREAKDOWN_REPORTED"],
    # Breakdown lifecycle — see app/modules/booking/services/breakdown.py.
    # From BREAKDOWN_REPORTED the system either:
    #   - goes to AWAITING_SWAP while admin finds a replacement, OR
    #   - jumps straight to DRIVER_ASSIGNED (same-partner self-serve swap),
    #   - or to PENDING_PARTNER_ACCEPTANCE (handover to a different partner).
    "BREAKDOWN_REPORTED": [
        "AWAITING_SWAP",
        "DRIVER_ASSIGNED",
        "PENDING_PARTNER_ACCEPTANCE",
        "STARTED",
        "CANCELLED",
    ],
    "AWAITING_SWAP": ["DRIVER_ASSIGNED", "PENDING_PARTNER_ACCEPTANCE", "CANCELLED"],
    "COMPLETED": ["SETTLEMENT_PENDING"],
    "SETTLEMENT_PENDING": ["SETTLED"],
    "SETTLED": [],
    "CANCELLED": [],
}

HOTEL_BOOKING_TRANSITIONS: dict[str, list[str]] = {
    "PENDING_PAYMENT": ["AWAITING_HOTEL_CONFIRMATION", "CANCELLED"],
    "AWAITING_HOTEL_CONFIRMATION": ["CONFIRMED", "REJECTED", "CANCELLED"],
    "CONFIRMED": ["CHECKED_IN", "CANCELLED"],
    "CHECKED_IN": ["IN_HOUSE", "CANCELLED"],
    "IN_HOUSE": ["CHECKED_OUT", "NO_SHOW"],
    "CHECKED_OUT": ["COMPLETED"],
    "COMPLETED": ["SETTLED"],
    "SETTLED": [],
    "CANCELLED": [],
    "REJECTED": [],
    "NO_SHOW": [],
}


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class AdvanceOut(BaseModel):
    """The live advance on a cab booking. Doc Ref: BRD Part 3 §45."""

    id: int
    receipt_number: str
    amount: float
    payment_mode: str
    received_by: str
    reference_note: Optional[str] = None
    status: str
    collected_by_role: str
    collected_at: Optional[str] = None


class CabBookingOut(BaseModel):
    id: int
    booking_number: str
    trip_type: Optional[str]
    vehicle_category_id: Optional[int]
    vehicle_category_name: Optional[str]
    pickup_location: Optional[str]
    drop_location: Optional[str]
    pickup_datetime: Optional[datetime]
    estimated_distance: Optional[float]
    estimated_amount: Optional[float]
    final_amount: Optional[float]
    booking_status: str
    # Assignment info
    assigned_partner_id: Optional[int] = None
    assigned_partner_name: Optional[str] = None
    assigned_driver_id: Optional[int] = None
    assigned_driver_name: Optional[str] = None
    assigned_driver_mobile: Optional[str] = None
    assigned_vehicle_id: Optional[int] = None
    assigned_vehicle_reg: Optional[str] = None
    # Payment / advance
    payment_mode: Optional[str] = None
    payment_collected_by: Optional[str] = None
    advance: Optional[AdvanceOut] = None
    # Partner acceptance gate (migration 0039)
    # assigned_at = when the current (latest) assignment was created
    # acceptance_deadline = the partner must respond by this time
    # acceptance_status = "PENDING" | "ACCEPTED" | "REJECTED" | "TIMEOUT" | None
    assigned_at: Optional[datetime] = None
    acceptance_deadline: Optional[datetime] = None
    acceptance_status: Optional[str] = None
    partner_responded_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class BookingListItem(BaseModel):
    id: int
    booking_number: str
    customer_id: int
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    city_id: int
    city_name: Optional[str]
    booking_status: str
    payment_status: str
    total_amount: float
    total_paid_amount: float
    journey_start_date: Optional[str]
    journey_end_date: Optional[str]
    services: List[str]  # service types list e.g. ["CAB", "HOTEL"]
    cab_status: Optional[str]  # current cab booking status if any
    hotel_status: Optional[str]  # current hotel reservation status if any
    tour_status: Optional[str]  # current tour booking status if any
    created_at: datetime

    class Config:
        from_attributes = True


class HotelBookingOut(BaseModel):
    id: int
    booking_number: str
    hotel_id: Optional[int]
    hotel_name: Optional[str]
    hotel_address: Optional[str]
    room_category_name: Optional[str]
    room_category_id: Optional[int] = None
    room_type: Optional[str]
    meal_plan: Optional[str]
    check_in_date: Optional[str]
    check_out_date: Optional[str]
    num_nights: Optional[int]
    num_rooms: Optional[int]
    num_guests: Optional[int]
    adults_count: Optional[int] = None
    children_count: Optional[int] = None
    extra_beds: Optional[int] = None
    special_requests: Optional[str] = None
    base_amount: Optional[float]
    taxes_amount: Optional[float]
    additional_charges: Optional[float]
    final_amount: Optional[float]
    actual_check_in_at: Optional[str]
    actual_check_out_at: Optional[str]
    # Platform-local stamps for the UI so admin/partner always read the time
    # in the same zone the guest will see on the invoice.
    check_in_at_local: Optional[str] = None
    check_out_at_local: Optional[str] = None
    platform_timezone: Optional[str] = None
    check_in_id_proof: Optional[str]
    booking_status: str
    hotel_confirmation_number: Optional[str]
    voucher_url: Optional[str]
    cancellation_reason: Optional[str]
    cancellation_charge: Optional[float]
    refund_amount: Optional[float]
    allocated_rooms: Optional[str] = (
        None  # JSON list of room numbers e.g. '["101","102"]'
    )
    # ── Billing / custody (migration 0035) ──
    coupon_code: Optional[str] = None
    coupon_discount: Optional[float] = None
    overtime_hours: Optional[float] = None
    overtime_charge: Optional[float] = None
    invoice_number: Optional[str] = None
    invoice_url: Optional[str] = None
    payment_collected_status: Optional[str] = None
    payment_mode: Optional[str] = None
    payment_collected_by: Optional[str] = None
    advance_payments: List[dict] = []
    total_advance_paid: Optional[float] = None
    balance_due: Optional[float] = None
    # ── Occupancy breakdown (frozen at booking time in rate_snapshot) ──
    # base_amount lumps room tariff + extra-person/bed surcharge; surface the
    # split so the detail-page Payment panel can itemise it like the bill.
    room_tariff: Optional[float] = None
    occupancy: Optional[dict] = None

    class Config:
        from_attributes = True


class BookingDetailOut(BaseModel):
    id: int
    booking_number: str
    customer_id: int
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    customer_email: Optional[str]
    city_id: int
    city_name: Optional[str]
    booking_status: str
    payment_status: str
    total_amount: float
    total_paid_amount: float
    total_refund_amount: float
    journey_start_date: Optional[str]
    journey_end_date: Optional[str]
    remarks: Optional[str]
    created_at: datetime
    updated_at: datetime
    services: List[str] = []  # service type codes e.g. ["CAB", "HOTEL"]
    cab_bookings: List[CabBookingOut] = []
    hotel_bookings: List[HotelBookingOut] = []
    tour_bookings: List[dict] = []
    timeline: List[dict] = []

    class Config:
        from_attributes = True


class PaginatedBookings(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: List[BookingListItem]


class AssignPartnerRequest(BaseModel):
    partner_id: int


class AssignDriverRequest(BaseModel):
    driver_id: int
    vehicle_id: int


class ReassignRequest(BaseModel):
    partner_id: int
    reason: Optional[str] = None


class RescheduleRequest(BaseModel):
    pickup_datetime: datetime
    reason: Optional[str] = None


class CancelBookingRequest(BaseModel):
    reason: str
    cancellation_charge: float = 0.0
    refund_amount: float = 0.0


class AddNoteRequest(BaseModel):
    note: str


class UpdateFinalAmountRequest(BaseModel):
    final_amount: float
    reason: Optional[str] = None


class EditCabDetailsRequest(BaseModel):
    """
    Admin-editable cab booking fields.
    Only trip details can be changed — not payment or assignment fields.
    pickup_datetime is handled separately via /reschedule.
    Doc Ref: DB Schema Part 4 §7, Booking API §7
    """

    pickup_location: Optional[str] = None
    drop_location: Optional[str] = None
    estimated_distance: Optional[float] = None
    estimated_amount: Optional[float] = None
    trip_type: Optional[str] = None
    vehicle_category_id: Optional[int] = None


class BookingStatsOut(BaseModel):
    total: int
    pending_assignment: int
    confirmed: int
    in_progress: int
    completed: int
    cancelled: int
    today_bookings: int


# ── Hotel Action Request Schemas ───────────────────────────────


class HotelConfirmRequest(BaseModel):
    """Admin confirms or records hotel acceptance."""

    hotel_confirmation_number: Optional[str] = None


class HotelRejectRequest(BaseModel):
    reason: str
    refund_amount: float = 0.0


class HotelCheckInRequest(BaseModel):
    id_proof: str  # AADHAAR | PASSPORT | DL
    id_number: str
    # Mandatory — the real-world billing below is driven by the recorded
    # check-in stamp, so a check-in without one cannot be priced correctly.
    actual_check_in_at: datetime
    room_ids: List[int] = (
        []
    )  # physical room IDs to assign (optional if none configured)
    remarks: Optional[str] = None
    # Set to True by the modal when the user explicitly confirms a check-in
    # date that is more than 1 day away from the booked check-in date. The
    # backend still writes a remarks note recording the override so the
    # mismatch is visible on the timeline.
    confirm_date_mismatch: bool = False


class HotelCheckOutRequest(BaseModel):
    additional_charges: float = 0.0
    check_out_notes: Optional[str] = None
    # Mandatory — the room charge and overtime are computed from the actual
    # check-in → check-out window, so the check-out stamp must be recorded.
    actual_check_out_at: datetime


class HotelAddChargesRequest(BaseModel):
    amount: float
    description: str


class HotelCancelRequest(BaseModel):
    reason: str
    cancellation_charge: float = 0.0
    refund_amount: float = 0.0


class HotelNoShowRequest(BaseModel):
    refund_amount: float = 0.0


class HotelRecordAdvanceRequest(BaseModel):
    """
    Advance taken from the customer before final billing.
    received_by is the custody signal that later drives settlement direction.
    """

    amount: float = Field(..., gt=0)
    payment_mode: str = Field(..., description="UPI | ONLINE | CASH | WALLET")
    received_by: str = Field("ADMIN", description="ADMIN | PARTNER")
    reference_number: Optional[str] = None
    notes: Optional[str] = None


class HotelCollectPaymentRequest(BaseModel):
    """
    Final balance collected against the invoice.
    ONLINE always lands with the platform, so collected_by is forced to ADMIN
    for it; CASH and UPI may be taken at the property by the partner.
    """

    amount: float = Field(..., gt=0)
    payment_mode: str = Field(..., description="ONLINE | UPI | CASH")
    collected_by: str = Field("ADMIN", description="ADMIN | PARTNER")
    reference_number: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _get_master_booking(db: AsyncSession, booking_id: int) -> MasterBooking:
    q = (
        select(MasterBooking)
        .options(
            selectinload(MasterBooking.services),
            selectinload(MasterBooking.cab_bookings).selectinload(
                CabBooking.assignments
            ),
            selectinload(MasterBooking.timelines),
            selectinload(MasterBooking.notes),
        )
        .where(MasterBooking.id == booking_id)
    )
    result = await db.execute(q)
    mb = result.scalar_one_or_none()
    if not mb:
        raise HTTPException(status_code=404, detail="Booking not found")
    return mb


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


async def _get_int_config(db: AsyncSession, key: str, default: int) -> int:
    """
    Read an integer-valued runtime config from system_configurations.
    Falls back to `default` if the row is missing or the value is not
    a valid integer. Centralises the parsing so callers don't need to
    repeat the try/except dance.
    """
    from app.modules.admin.models import SystemConfiguration

    row = (
        await db.execute(
            select(SystemConfiguration.config_value).where(
                SystemConfiguration.config_key == key
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return default
    try:
        return int(str(row).strip())
    except (TypeError, ValueError):
        return default


async def _close_pending_partner_assignment(
    db: AsyncSession, cb: CabBooking, reason_code: str, description_suffix: str
) -> Optional[CabBookingAssignment]:
    """
    When admin reassigns or the sweeper times out, the row currently
    sitting in PENDING_PARTNER_ACCEPTANCE needs to be closed so the
    audit trail shows why it ended. Returns the row that was closed
    (or None if there was no pending row).
    """
    if cb.booking_status != "PENDING_PARTNER_ACCEPTANCE":
        return None
    if not cb.assignments:
        return None
    # Use the active (closed_at IS NULL) row so a closed handover row is
    # never mistaken for the pending one.
    pending_row = next(
        (
            a
            for a in cb.assignments
            if a.closed_at is None and a.accepted_at is None and a.rejected_at is None
        ),
        None,
    )
    if pending_row is None:
        return None
    now = datetime.now(timezone.utc)
    pending_row.rejected_at = now
    pending_row.rejection_reason_code = reason_code
    pending_row.closed_at = now
    pending_row.close_reason = (
        "ADMIN_REASSIGN" if reason_code == "ADMIN_REASSIGN" else "TIMEOUT"
    )
    cb.partner_responded_at = now
    cb.acceptance_deadline = None
    cb.pending_partner_id = None
    cb.booking_status = "PENDING_ASSIGNMENT"
    if description_suffix:
        await _log_timeline(
            db,
            cb.master_booking_id,
            "PARTNER_ASSIGNMENT_CLOSED",
            description_suffix,
        )
    return pending_row


async def _build_cab_out(cb: CabBooking, db: AsyncSession) -> CabBookingOut:
    """Build CabBookingOut with latest assignment info."""
    cat_name = None
    if cb.vehicle_category_id:
        r = await db.execute(
            select(VehicleCategory).where(VehicleCategory.id == cb.vehicle_category_id)
        )
        vc = r.scalar_one_or_none()
        if vc:
            cat_name = vc.category_name

    partner_id = partner_name = driver_id = driver_name = driver_mobile = vehicle_id = (
        vehicle_reg
    ) = None
    assigned_at: Optional[datetime] = None
    acceptance_status: Optional[str] = None

    if cb.assignments:
        # Use the active assignment row (closed_at IS NULL) so a closed
        # handover row doesn't get surfaced as "current" after migration 0041.
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        partner_id = latest.partner_id
        assigned_at = latest.assigned_at
        # Derive acceptance_status from the latest assignment row + cab state
        # so the FE can render the right badge without extra queries.
        if cb.booking_status == "PENDING_PARTNER_ACCEPTANCE":
            acceptance_status = "PENDING"
        elif latest.accepted_at is not None:
            acceptance_status = "ACCEPTED"
        elif latest.rejected_at is not None:
            acceptance_status = "REJECTED"
        elif latest.rejection_reason_code == "TIMEOUT":
            acceptance_status = "TIMEOUT"

        pr = await db.execute(select(Partner).where(Partner.id == latest.partner_id))
        p = pr.scalar_one_or_none()
        if p:
            partner_name = p.business_name or f"Partner #{p.id}"

        if latest.driver_id:
            driver_id = latest.driver_id
            dr = await db.execute(select(Driver).where(Driver.id == latest.driver_id))
            d = dr.scalar_one_or_none()
            if d:
                driver_name = d.full_name
                driver_mobile = d.mobile

        if latest.vehicle_id:
            vehicle_id = latest.vehicle_id
            vr = await db.execute(
                select(Vehicle).where(Vehicle.id == latest.vehicle_id)
            )
            v = vr.scalar_one_or_none()
            if v:
                vehicle_reg = v.registration_number

    advance = advance_service.advance_summary(
        await advance_service.get_active_advance(db, cb.id)
    )

    return CabBookingOut(
        id=cb.id,
        booking_number=cb.booking_number,
        trip_type=cb.trip_type,
        vehicle_category_id=cb.vehicle_category_id,
        vehicle_category_name=cat_name,
        pickup_location=cb.pickup_location,
        drop_location=cb.drop_location,
        pickup_datetime=cb.pickup_datetime,
        estimated_distance=(
            float(cb.estimated_distance) if cb.estimated_distance else None
        ),
        estimated_amount=float(cb.estimated_amount) if cb.estimated_amount else None,
        final_amount=float(cb.final_amount) if cb.final_amount else None,
        booking_status=cb.booking_status,
        assigned_partner_id=partner_id,
        assigned_partner_name=partner_name,
        assigned_driver_id=driver_id,
        assigned_driver_name=driver_name,
        assigned_driver_mobile=driver_mobile,
        assigned_vehicle_id=vehicle_id,
        assigned_vehicle_reg=vehicle_reg,
        payment_mode=cb.payment_mode,
        payment_collected_by=cb.payment_collected_by,
        advance=AdvanceOut(**advance) if advance else None,
        # Partner acceptance gate (migration 0039)
        assigned_at=assigned_at,
        acceptance_deadline=cb.acceptance_deadline,
        acceptance_status=acceptance_status,
        partner_responded_at=cb.partner_responded_at,
    )


async def _build_hotel_out(hr: HotelReservation, db: AsyncSession) -> HotelBookingOut:
    """Serialise a HotelReservation row to the API schema."""
    hotel_name = hotel_address = None
    if hr.hotel_id:
        h = (
            await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
        ).scalar_one_or_none()
        if h:
            hotel_name = h.hotel_name
            hotel_address = h.address

    room_category_name = room_type = None
    if hr.room_category_id:
        rc = (
            await db.execute(
                select(HotelRoomCategory).where(
                    HotelRoomCategory.id == hr.room_category_id
                )
            )
        ).scalar_one_or_none()
        if rc:
            room_category_name = rc.category_name
            room_type = rc.room_type

    # Pull allocated rooms from HotelCheckin record if it exists
    checkin_rec = (
        await db.execute(
            select(HotelCheckin).where(HotelCheckin.reservation_id == hr.id)
        )
    ).scalar_one_or_none()

    # Advances drive the balance shown on the detail page.
    advances = await hotel_billing.list_advances(db, hr.id)
    advance_total = hotel_billing.total_advance(advances)
    advance_rows = [
        {
            "id": a.id,
            "receipt_number": a.receipt_number,
            "amount": float(a.amount),
            "refunded_amount": float(a.refunded_amount or 0),
            "payment_mode": a.payment_mode,
            "received_by": a.received_by,
            "reference_number": a.reference_number,
            "notes": a.notes,
            "collected_at": a.collected_at.isoformat() if a.collected_at else None,
        }
        for a in advances
    ]

    # Extra beds live inside the frozen rate snapshot (occupancy block), not as
    # a column — surface the count so the edit modal can prefill it and an
    # untouched save doesn't silently drop a previously-charged bed.
    _snap = hr.rate_snapshot if isinstance(hr.rate_snapshot, dict) else {}
    _occ = _snap.get("occupancy") if isinstance(_snap.get("occupancy"), dict) else {}
    extra_beds = int(_occ.get("extra_beds") or 0)

    # Peel the frozen occupancy surcharge off base_amount to recover the pure
    # room tariff (base_amount already includes the surcharge). Only expose the
    # occupancy block when a real surcharge was charged, so the panel falls back
    # to a single "Base" line for plain bookings and legacy rows (null snapshot).
    _surcharge = float(_occ.get("surcharge") or 0)
    _room_tariff = None
    _occ_out = None
    if hr.base_amount is not None and _surcharge > 0:
        _room_tariff = float(hr.base_amount) - _surcharge
        _occ_out = _occ

    # ── Real-world bill ───────────────────────────────────────────
    # For a closed stay the recomputed bill (nights actually stayed) is
    # authoritative. Legacy rows checked out before the recompute feature
    # stored the booked amount, which would otherwise disagree with what
    # collect-payment / invoice validate against — surface the recomputed
    # totals so the UI is always consistent.
    _bill_total = None
    _bill_balance = None
    if hr.reservation_status in {"CHECKED_OUT", "COMPLETED", "SETTLED"}:
        _bill = await hotel_billing.build_bill(db, hr)
        _bill_total = _bill.grand_total
        _bill_balance = _bill.balance_due

    # ── Platform-local stamps ──
    # Same idea as the partner detail payload: every reader (admin / partner /
    # customer) sees the stay stamped in the platform's operating zone, never
    # in the operator's local zone — guests would otherwise be charged for
    # the wrong day.
    from app.core.timezone import get_platform_tz_sync, to_platform_tz

    try:
        platform_tz_name = str(get_platform_tz_sync())
    except Exception:
        platform_tz_name = "Asia/Kolkata"
    check_in_at_local = (
        to_platform_tz(hr.actual_check_in_at).isoformat()
        if hr.actual_check_in_at is not None
        else None
    )
    check_out_at_local = (
        to_platform_tz(hr.actual_check_out_at).isoformat()
        if hr.actual_check_out_at is not None
        else None
    )

    return HotelBookingOut(
        id=hr.id,
        booking_number=hr.reservation_number or f"HR-{hr.id}",
        hotel_id=hr.hotel_id,
        hotel_name=hotel_name,
        hotel_address=hotel_address,
        room_category_name=room_category_name,
        room_category_id=hr.room_category_id,
        room_type=room_type,
        meal_plan=None,
        check_in_date=hr.check_in_date.isoformat() if hr.check_in_date else None,
        check_out_date=hr.check_out_date.isoformat() if hr.check_out_date else None,
        num_nights=hr.nights,
        num_rooms=hr.rooms_count,
        num_guests=hr.adults_count,
        adults_count=hr.adults_count,
        children_count=hr.children_count,
        extra_beds=extra_beds,
        special_requests=hr.special_requests,
        base_amount=float(hr.base_amount) if hr.base_amount else None,
        taxes_amount=float(hr.gst_amount) if hr.gst_amount else None,
        additional_charges=float(hr.extra_charges) if hr.extra_charges else None,
        final_amount=(
            float(_bill_total)
            if _bill_total is not None
            else (float(hr.total_amount) if hr.total_amount else None)
        ),
        actual_check_in_at=(
            hr.actual_check_in_at.isoformat() if hr.actual_check_in_at else None
        ),
        actual_check_out_at=(
            hr.actual_check_out_at.isoformat() if hr.actual_check_out_at else None
        ),
        check_in_at_local=check_in_at_local,
        check_out_at_local=check_out_at_local,
        platform_timezone=platform_tz_name,
        check_in_id_proof=hr.check_in_id_proof,
        booking_status=hr.reservation_status,
        hotel_confirmation_number=hr.hotel_confirmation_number,
        voucher_url=hr.voucher_url,
        cancellation_reason=hr.cancellation_reason,
        cancellation_charge=(
            float(hr.cancellation_charge) if hr.cancellation_charge else None
        ),
        refund_amount=float(hr.refund_amount) if hr.refund_amount else None,
        allocated_rooms=checkin_rec.allocated_rooms if checkin_rec else None,
        # ── Billing / custody ──
        coupon_code=getattr(hr, "coupon_code", None),
        coupon_discount=(
            float(hr.coupon_discount) if getattr(hr, "coupon_discount", None) else None
        ),
        overtime_hours=(
            float(hr.overtime_hours) if getattr(hr, "overtime_hours", None) else None
        ),
        overtime_charge=(
            float(hr.overtime_charge) if getattr(hr, "overtime_charge", None) else None
        ),
        invoice_number=getattr(hr, "invoice_number", None),
        invoice_url=getattr(hr, "invoice_url", None),
        payment_collected_status=getattr(hr, "payment_collected_status", None),
        payment_mode=getattr(hr, "payment_mode", None),
        payment_collected_by=getattr(hr, "payment_collected_by", None),
        advance_payments=advance_rows,
        total_advance_paid=float(advance_total),
        balance_due=(
            float(_bill_balance)
            if _bill_balance is not None
            else float(
                max(Decimal("0"), (hr.total_amount or Decimal("0")) - advance_total)
            )
        ),
        room_tariff=_room_tariff,
        occupancy=_occ_out,
    )


# EDIT CAB DETAILS — PATCH /admin/bookings/{booking_id}/cab/{cab_id}/edit
# Admin can correct pickup/drop location, estimated distance, amount,
# trip type and vehicle category.
# Guard: Not allowed once trip has STARTED.
# Doc Ref: DB Schema Part 4 §7 (cab_bookings table), Booking API §7
# ════════════════════════════════════════════════════════════════


@router.patch("/{booking_id}/cab/{cab_id}/edit", tags=["Admin Bookings"])
async def edit_cab_details(
    booking_id: int,
    cab_id: int,
    payload: EditCabDetailsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin correction of cab booking trip details.
    Allowed fields: pickup_location, drop_location, estimated_distance,
                    estimated_amount, trip_type, vehicle_category_id.
    Guard: blocked once status is STARTED or beyond (trip is live).
    Guard: vehicle_category_id must exist and be active.
    Only provided (non-None) fields are updated.
    """
    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    # Status guard — cannot edit live/completed trips
    blocked = {"STARTED", "COMPLETED", "SETTLEMENT_PENDING", "SETTLED", "CANCELLED"}
    if cb.booking_status in blocked:
        raise HTTPException(
            400,
            f"Cannot edit cab details: trip is '{cb.booking_status}'. "
            "Editing is only allowed before the trip starts.",
        )

    # Validate vehicle_category_id if provided
    if payload.vehicle_category_id is not None:
        cat = (
            await db.execute(
                select(VehicleCategory).where(
                    VehicleCategory.id == payload.vehicle_category_id
                )
            )
        ).scalar_one_or_none()
        if not cat:
            raise HTTPException(
                404, f"Vehicle category #{payload.vehicle_category_id} not found."
            )
        if not cat.is_active:
            raise HTTPException(
                400, f"Vehicle category '{cat.category_name}' is inactive."
            )

    # Validate trip_type if provided
    valid_trip_types = {"LOCAL", "AIRPORT", "OUTSTATION", "ONE_WAY", "ROUND_TRIP"}
    if (
        payload.trip_type is not None
        and payload.trip_type.upper() not in valid_trip_types
    ):
        raise HTTPException(
            400,
            f"Invalid trip_type '{payload.trip_type}'. Valid: {', '.join(valid_trip_types)}",
        )

    # Track changes for timeline
    changes = []

    if payload.pickup_location is not None and payload.pickup_location.strip():
        old_val = cb.pickup_location
        cb.pickup_location = payload.pickup_location.strip()
        changes.append(f"Pickup: '{old_val}' → '{cb.pickup_location}'")

    if payload.drop_location is not None and payload.drop_location.strip():
        old_val = cb.drop_location
        cb.drop_location = payload.drop_location.strip()
        changes.append(f"Drop: '{old_val}' → '{cb.drop_location}'")

    if payload.estimated_distance is not None:
        if payload.estimated_distance < 0:
            raise HTTPException(400, "estimated_distance cannot be negative.")
        old_val = float(cb.estimated_distance or 0)
        cb.estimated_distance = payload.estimated_distance
        changes.append(f"Distance: {old_val} km → {payload.estimated_distance} km")

    if payload.estimated_amount is not None:
        if payload.estimated_amount < 0:
            raise HTTPException(400, "estimated_amount cannot be negative.")
        old_val = float(cb.estimated_amount or 0)
        cb.estimated_amount = payload.estimated_amount
        # Keep master booking total in sync
        mb.total_amount = cb.estimated_amount
        changes.append(f"Est. Amount: ₹{old_val} → ₹{payload.estimated_amount}")

    if payload.trip_type is not None:
        old_val = cb.trip_type
        cb.trip_type = payload.trip_type.upper()
        changes.append(f"Trip Type: {old_val} → {cb.trip_type}")

    if payload.vehicle_category_id is not None:
        old_val = cb.vehicle_category_id
        cb.vehicle_category_id = payload.vehicle_category_id
        changes.append(
            f"Vehicle Category ID: {old_val} → {payload.vehicle_category_id}"
        )

    if not changes:
        return {"success": True, "message": "No changes provided", "changes": []}

    await _log_timeline(
        db,
        booking_id,
        "CAB_DETAILS_EDITED",
        f"Admin updated cab {cb.booking_number}. Changes: {'; '.join(changes)}",
    )
    await db.commit()
    return {"success": True, "message": "Cab details updated", "changes": changes}


# ════════════════════════════════════════════════════════════════
# LIST BOOKINGS  — GET /admin/bookings
# Doc Ref: Admin API §11
# ════════════════════════════════════════════════════════════════


@router.get("", response_model=PaginatedBookings, tags=["Admin Bookings"])
async def list_bookings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    booking_status: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    service_type: Optional[str] = Query(None, description="CAB | HOTEL | TOUR"),
    city_id: Optional[int] = Query(None),
    search: Optional[str] = Query(
        None, description="booking_number, customer name, or mobile"
    ),
    journey_date: Optional[str] = Query(
        None, description="Filter by journey_start_date YYYY-MM-DD"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated list of all master bookings with enriched customer/city data.
    Supports filtering by status, payment, service type, city, and search.
    """
    q = select(MasterBooking).options(selectinload(MasterBooking.services))

    if booking_status:
        q = q.where(MasterBooking.booking_status == booking_status.upper())
    if payment_status:
        q = q.where(MasterBooking.payment_status == payment_status.upper())
    if city_id:
        q = q.where(MasterBooking.city_id == city_id)
    if journey_date:
        from datetime import date as _date

        q = q.where(
            MasterBooking.journey_start_date == _date.fromisoformat(journey_date)
        )

    # Search across booking_number, customer name/mobile
    # Note: mobile is on User model (joined via customer.user_id)
    if search:
        s = f"%{search}%"
        cust_q = (
            select(Customer.id)
            .join(User, User.id == Customer.user_id)
            .where(
                or_(
                    Customer.first_name.ilike(s),
                    Customer.last_name.ilike(s),
                    User.mobile_number.ilike(s),
                )
            )
        )
        cust_ids = (await db.execute(cust_q)).scalars().all()
        q = q.where(
            or_(
                MasterBooking.booking_number.ilike(s),
                MasterBooking.customer_id.in_(cust_ids),
            )
        )

    # Service type sub-filter
    if service_type:
        svc_ids_q = select(BookingService.master_booking_id).where(
            BookingService.service_type == service_type.upper()
        )
        svc_ids = (await db.execute(svc_ids_q)).scalars().all()
        q = q.where(MasterBooking.id.in_(svc_ids))

    total = (
        await db.execute(select(func.count()).select_from(q.subquery()))
    ).scalar() or 0
    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                q.order_by(desc(MasterBooking.created_at))
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    # Enrich with customer + city names
    items = []
    for mb in rows:
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

        # Collect service types and current cab status
        svc_types = list({s.service_type for s in mb.services})
        cab_status = None
        if "CAB" in svc_types:
            cab_q = (
                select(CabBooking).where(CabBooking.master_booking_id == mb.id).limit(1)
            )
            cab = (await db.execute(cab_q)).scalar_one_or_none()
            if cab:
                cab_status = cab.booking_status

        # Current hotel reservation status (the hotel lifecycle lives on the
        # reservation, not on master_bookings.booking_status, which stays coarse).
        hotel_status = None
        if "HOTEL" in svc_types:
            hr_q = (
                select(HotelReservation.reservation_status)
                .where(HotelReservation.master_booking_id == mb.id)
                .limit(1)
            )
            hotel_status = (await db.execute(hr_q)).scalar_one_or_none()

        # Current tour booking status (the tour lifecycle lives on the
        # tour booking, not on master_bookings.booking_status).
        tour_status = None
        if "TOUR" in svc_types:
            tour_q = (
                select(TourBooking.booking_status)
                .where(TourBooking.master_booking_id == mb.id)
                .limit(1)
            )
            tour_status = (await db.execute(tour_q)).scalar_one_or_none()

        items.append(
            BookingListItem(
                id=mb.id,
                booking_number=mb.booking_number,
                customer_id=mb.customer_id,
                customer_name=cust.full_name if cust else None,
                customer_mobile=cust_user.mobile_number if cust_user else None,
                city_id=mb.city_id,
                city_name=city.name if city else None,
                booking_status=mb.booking_status,
                payment_status=mb.payment_status,
                total_amount=float(mb.total_amount or 0),
                total_paid_amount=float(mb.total_paid_amount or 0),
                journey_start_date=(
                    mb.journey_start_date.isoformat() if mb.journey_start_date else None
                ),
                journey_end_date=(
                    mb.journey_end_date.isoformat() if mb.journey_end_date else None
                ),
                services=svc_types,
                cab_status=cab_status,
                hotel_status=hotel_status,
                tour_status=tour_status,
                created_at=mb.created_at,
            )
        )

    return PaginatedBookings(
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size) if total > 0 else 1,
        items=items,
    )


# ════════════════════════════════════════════════════════════════
# BOOKING STATS  — GET /admin/bookings/stats
# ════════════════════════════════════════════════════════════════


@router.get("/stats", response_model=BookingStatsOut, tags=["Admin Bookings"])
async def booking_stats(db: AsyncSession = Depends(get_db)):
    """Dashboard stats: total, by status, today count."""
    from datetime import date as _date

    today = _date.today()
    total = (await db.execute(select(func.count(MasterBooking.id)))).scalar() or 0
    today_count = (
        await db.execute(
            select(func.count(MasterBooking.id)).where(
                func.date(MasterBooking.created_at) == today
            )
        )
    ).scalar() or 0

    def _count(status: str):
        return select(func.count(MasterBooking.id)).where(
            MasterBooking.booking_status == status
        )

    pending_assign = (
        await db.execute(
            select(func.count(CabBooking.id)).where(
                CabBooking.booking_status == "PENDING_ASSIGNMENT"
            )
        )
    ).scalar() or 0

    return BookingStatsOut(
        total=total,
        pending_assignment=pending_assign,
        confirmed=(await db.execute(_count("CONFIRMED"))).scalar() or 0,
        in_progress=(await db.execute(_count("IN_PROGRESS"))).scalar() or 0,
        completed=(await db.execute(_count("COMPLETED"))).scalar() or 0,
        cancelled=(await db.execute(_count("CANCELLED"))).scalar() or 0,
        today_bookings=today_count,
    )


# ════════════════════════════════════════════════════════════════


# LIST PENDING ACCEPTANCE — GET /admin/bookings/pending-acceptance
# Returns cab bookings waiting for the partner to accept/reject,
# sorted by deadline ascending (most urgent first). Useful for an
# admin "Awaiting Partner" tab on the bookings list.
# Doc Ref: BRD Part 3 §42
# ════════════════════════════════════════════════════════════════


@router.get("/pending-acceptance", tags=["Admin Bookings"])
async def list_pending_acceptance(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated list of cab bookings in PENDING_PARTNER_ACCEPTANCE,
    enriched with assigned partner name and time-remaining-seconds.
    Ordered by acceptance_deadline ASC so the soonest-deadline
    cabs show first.
    """
    from app.modules.partner.constants import SYSTEM_REJECT_REASON_TIMEOUT

    base = (
        select(CabBooking)
        .options(
            selectinload(CabBooking.assignments),
            selectinload(CabBooking.master_booking),
        )
        .where(CabBooking.booking_status == "PENDING_PARTNER_ACCEPTANCE")
        .order_by(CabBooking.acceptance_deadline.asc().nullslast())
    )

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0

    offset = (page - 1) * page_size
    rows = (await db.execute(base.offset(offset).limit(page_size))).scalars().all()

    now = datetime.now(timezone.utc)
    items: list[dict] = []
    for cb in rows:
        cab_out = await _build_cab_out(cb, db)
        deadline_iso = (
            cb.acceptance_deadline.isoformat() if cb.acceptance_deadline else None
        )
        seconds_remaining = None
        if cb.acceptance_deadline:
            delta = cb.acceptance_deadline - now
            seconds_remaining = max(0, int(delta.total_seconds()))
        items.append(
            {
                **cab_out.model_dump(),
                "seconds_remaining": seconds_remaining,
                "acceptance_deadline": deadline_iso,
                # Surface the audit code that would mark the row on timeout —
                # the FE can decide whether to render an "about to time out" hint.
                "timeout_reason_code": SYSTEM_REJECT_REASON_TIMEOUT,
            }
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "items": items,
    }


# ════════════════════════════════════════════════════════════════
# ADMIN ACCEPTANCE QUEUE — GET /admin/bookings/accept-queue
# Website bookings waiting for the admin to accept (realtime modal).
# This is the catch-up feed the admin portal pulls on login so
# bookings that arrived while nobody was logged in still surface.
# ════════════════════════════════════════════════════════════════


@router.get("/accept-queue", tags=["Admin Bookings"])
async def list_admin_accept_queue(
    db: AsyncSession = Depends(get_db),
):
    """
    Bookings placed on the customer website that are waiting for admin
    acceptance:

      CAB   — master DRAFT + cab PENDING_ASSIGNMENT (needs partner)
      HOTEL — master DRAFT + reservation PENDING_PAYMENT
      TOUR  — tour booking PENDING_CONFIRMATION

    Limited to the last 72 hours so stale rows don't pile up forever.
    Ordered newest-first. Once accepted, rows drop out of the queue.
    """
    items: list[dict] = []

    # ── CAB ──
    rows = (
        (
            await db.execute(
                text(
                    """
            SELECT mb.id AS master_booking_id,
                   mb.booking_number AS master_booking_number,
                   cb.id AS service_id,
                   cb.booking_number AS service_number,
                   cb.pickup_location AS headline,
                   cb.pickup_datetime AS booking_datetime,
                   cb.estimated_amount AS amount,
                   COALESCE(c.first_name || ' ' || c.last_name, '') AS customer_name,
                   ci.name AS city_name,
                   mb.created_at
            FROM master_bookings mb
            JOIN cab_bookings cb ON cb.master_booking_id = mb.id
            LEFT JOIN customers c ON c.id = mb.customer_id
            LEFT JOIN cities ci ON ci.id = mb.city_id
            WHERE mb.booking_status = 'DRAFT'
              AND cb.booking_status = 'PENDING_ASSIGNMENT'
              AND mb.created_at > NOW() - INTERVAL '72 hours'
            ORDER BY mb.created_at DESC
            """
                )
            )
        )
        .mappings()
        .all()
    )
    for r in rows:
        items.append(
            {
                "master_booking_id": int(r["master_booking_id"]),
                "master_booking_number": r["master_booking_number"],
                "service_type": "CAB",
                "service_id": int(r["service_id"]),
                "service_number": r["service_number"],
                "headline": r["headline"],
                "datetime": (
                    r["booking_datetime"].isoformat() if r["booking_datetime"] else None
                ),
                "amount": float(r["amount"] or 0),
                "customer_name": (r["customer_name"] or "").strip() or None,
                "city_name": r["city_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    # ── HOTEL ──
    rows = (
        (
            await db.execute(
                text(
                    """
            SELECT mb.id AS master_booking_id,
                   mb.booking_number AS master_booking_number,
                   hr.id AS service_id,
                   hr.reservation_number AS service_number,
                   h.hotel_name AS headline,
                   hr.check_in_date AS booking_datetime,
                   hr.total_amount AS amount,
                   COALESCE(c.first_name || ' ' || c.last_name, '') AS customer_name,
                   ci.name AS city_name,
                   mb.created_at
            FROM master_bookings mb
            JOIN hotel_reservations hr ON hr.master_booking_id = mb.id
            JOIN hotels h ON h.id = hr.hotel_id
            LEFT JOIN customers c ON c.id = mb.customer_id
            LEFT JOIN cities ci ON ci.id = mb.city_id
            WHERE mb.booking_status = 'DRAFT'
              AND hr.reservation_status = 'PENDING_PAYMENT'
              AND mb.created_at > NOW() - INTERVAL '72 hours'
            ORDER BY mb.created_at DESC
            """
                )
            )
        )
        .mappings()
        .all()
    )
    for r in rows:
        items.append(
            {
                "master_booking_id": int(r["master_booking_id"]),
                "master_booking_number": r["master_booking_number"],
                "service_type": "HOTEL",
                "service_id": int(r["service_id"]),
                "service_number": r["service_number"],
                "headline": r["headline"],
                "datetime": (
                    r["booking_datetime"].isoformat() if r["booking_datetime"] else None
                ),
                "amount": float(r["amount"] or 0),
                "customer_name": (r["customer_name"] or "").strip() or None,
                "city_name": r["city_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    # ── TOUR ──
    rows = (
        (
            await db.execute(
                text(
                    """
            SELECT mb.id AS master_booking_id,
                   mb.booking_number AS master_booking_number,
                   tb.id AS service_id,
                   tb.booking_number AS service_number,
                   tp.package_name AS headline,
                   tb.travel_start_date AS booking_datetime,
                   tb.total_amount AS amount,
                   COALESCE(c.first_name || ' ' || c.last_name, '') AS customer_name,
                   tp.destination AS city_name,
                   mb.created_at
            FROM master_bookings mb
            JOIN tour_bookings tb ON tb.master_booking_id = mb.id
            JOIN tour_packages tp ON tp.id = tb.package_id
            LEFT JOIN customers c ON c.id = mb.customer_id
            WHERE tb.booking_status = 'PENDING_CONFIRMATION'
              AND mb.created_at > NOW() - INTERVAL '72 hours'
            ORDER BY mb.created_at DESC
            """
                )
            )
        )
        .mappings()
        .all()
    )
    for r in rows:
        items.append(
            {
                "master_booking_id": int(r["master_booking_id"]),
                "master_booking_number": r["master_booking_number"],
                "service_type": "TOUR",
                "service_id": int(r["service_id"]),
                "service_number": r["service_number"],
                "headline": r["headline"],
                "datetime": (
                    r["booking_datetime"].isoformat() if r["booking_datetime"] else None
                ),
                "amount": float(r["amount"] or 0),
                "customer_name": (r["customer_name"] or "").strip() or None,
                "city_name": r["city_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    return {"total": len(items), "items": items}


# BOOKING DETAIL  — GET /admin/bookings/{booking_id}
# ════════════════════════════════════════════════════════════════


@router.get("/{booking_id}", response_model=BookingDetailOut, tags=["Admin Bookings"])
async def get_booking(booking_id: int, db: AsyncSession = Depends(get_db)):
    mb = await _get_master_booking(db, booking_id)
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

    cab_outs = [await _build_cab_out(cb, db) for cb in mb.cab_bookings]
    hotel_rows = (
        (
            await db.execute(
                select(HotelReservation).where(
                    HotelReservation.master_booking_id == booking_id
                )
            )
        )
        .scalars()
        .all()
    )
    hotel_outs = [await _build_hotel_out(hr, db) for hr in hotel_rows]
    tour_rows = (
        await db.execute(
            select(TourBooking, TourPackage.package_name, TourPackage.destination)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .where(TourBooking.master_booking_id == booking_id)
        )
    ).all()
    tour_outs = [
        {
            "id": tour.id,
            "booking_number": tour.booking_number,
            "package_id": tour.package_id,
            "package_name": package_name,
            "destination": destination,
            "travel_start_date": (
                tour.travel_start_date.isoformat() if tour.travel_start_date else None
            ),
            "travel_end_date": (
                tour.travel_end_date.isoformat() if tour.travel_end_date else None
            ),
            "persons_count": tour.persons_count,
            "total_amount": float(tour.total_amount or 0),
            "platform_commission": float(tour.platform_commission or 0),
            "partner_payout": float(tour.partner_payout or 0),
            "payment_status": tour.payment_status,
            "booking_status": tour.booking_status,
            "special_requests": tour.special_requests,
        }
        for tour, package_name, destination in tour_rows
    ]

    timeline = [
        {
            "id": t.id,
            "event_type": t.event_type,
            "event_description": t.event_description,
            "event_timestamp": t.event_timestamp.isoformat(),
        }
        for t in sorted(mb.timelines, key=lambda x: x.event_timestamp, reverse=True)
    ]

    return BookingDetailOut(
        id=mb.id,
        booking_number=mb.booking_number,
        customer_id=mb.customer_id,
        customer_name=cust.full_name if cust else None,
        customer_mobile=cust_user.mobile_number if cust_user else None,
        customer_email=cust_user.email if cust_user else None,
        city_id=mb.city_id,
        city_name=city.name if city else None,
        booking_status=mb.booking_status,
        payment_status=mb.payment_status,
        total_amount=float(mb.total_amount or 0),
        total_paid_amount=float(mb.total_paid_amount or 0),
        total_refund_amount=float(mb.total_refund_amount or 0),
        journey_start_date=(
            mb.journey_start_date.isoformat() if mb.journey_start_date else None
        ),
        journey_end_date=(
            mb.journey_end_date.isoformat() if mb.journey_end_date else None
        ),
        remarks=mb.remarks,
        created_at=mb.created_at,
        updated_at=mb.updated_at,
        services=list({s.service_type for s in mb.services}),
        cab_bookings=cab_outs,
        hotel_bookings=hotel_outs,
        tour_bookings=tour_outs,
        timeline=timeline,
    )


# ════════════════════════════════════════════════════════════════
# ASSIGN PARTNER — POST /admin/bookings/{booking_id}/cab/{cab_id}/assign-partner
# Doc Ref: Admin API §11, Booking API §16
# Guard: cab must be PENDING_ASSIGNMENT
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/cab/{cab_id}/assign-partner", tags=["Admin Bookings"])
async def assign_partner(
    booking_id: int,
    cab_id: int,
    payload: AssignPartnerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """
    Assign an APPROVED partner to a cab booking.

    New flow (migration 0039): the cab is placed in
    PENDING_PARTNER_ACCEPTANCE with a configurable deadline
    (system_configurations.PARTNER_ACCEPTANCE_TIMEOUT_MINUTES,
    default 10). The partner must accept or reject before the
    deadline; if they do nothing, a Celery sweeper reverts the cab
    to PENDING_ASSIGNMENT.

    Reassigning while the cab is already in PENDING_PARTNER_ACCEPTANCE
    closes the previous pending row with reason ADMIN_REASSIGN so the
    audit trail reflects why that assignment ended.
    """
    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    # Status guard — assign from PENDING_ASSIGNMENT or replace a partner who
    # is already in the acceptance window.
    if cb.booking_status not in (
        "PENDING_ASSIGNMENT",
        "ASSIGNED",
        "PENDING_PARTNER_ACCEPTANCE",
    ):
        raise HTTPException(
            400,
            f"Cannot assign partner: cab is currently '{cb.booking_status}'. "
            f"Only PENDING_ASSIGNMENT / ASSIGNED / PENDING_PARTNER_ACCEPTANCE cabs can be assigned/reassigned.",
        )

    # Partner guard
    partner = (
        await db.execute(select(Partner).where(Partner.id == payload.partner_id))
    ).scalar_one_or_none()
    if not partner:
        raise HTTPException(404, "Partner not found")
    if partner.status not in ("APPROVED", "ACTIVE"):
        raise HTTPException(
            400,
            f"Partner is not eligible for assignment (status: {partner.status}). Partner must be APPROVED or ACTIVE.",
        )

    # If the cab is already sitting with a partner in the acceptance window,
    # close that row first so it doesn't linger as an open accepted_at.
    if cb.booking_status == "PENDING_PARTNER_ACCEPTANCE":
        from app.modules.partner.constants import SYSTEM_REJECT_REASON_ADMIN_REASSIGN

        await _close_pending_partner_assignment(
            db,
            cb,
            reason_code=SYSTEM_REJECT_REASON_ADMIN_REASSIGN,
            description_suffix=(
                f"Admin reassigned cab {cb.booking_number} before partner "
                f"responded; previous assignment auto-closed."
            ),
        )

    timeout_minutes = await _get_int_config(
        db, "PARTNER_ACCEPTANCE_TIMEOUT_MINUTES", 10
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)

    # Create assignment record (carries the deadline for audit + UI)
    assignment = CabBookingAssignment(
        cab_booking_id=cb.id,
        partner_id=payload.partner_id,
        assigned_at=datetime.now(timezone.utc),
        assignment_type="MANUAL",
        acceptance_deadline=deadline,
    )
    db.add(assignment)

    cb.booking_status = "PENDING_PARTNER_ACCEPTANCE"
    cb.acceptance_deadline = deadline
    cb.pending_partner_id = payload.partner_id
    cb.partner_responded_at = None

    await _log_timeline(
        db,
        booking_id,
        "PARTNER_ASSIGNED",
        f"Partner '{partner.business_name or partner.id}' assigned to cab "
        f"{cb.booking_number}. Awaiting acceptance by {deadline.isoformat()} "
        f"({timeout_minutes} min).",
    )
    await db.commit()

    # ── Realtime + push (Doc Ref: BRD Part 7 §155) ──
    # Push to the partner's WebSocket + FCM so they see the assignment
    # request immediately (popup + push notification), then the FE
    # opens the Accept/Reject dialog with the live countdown.
    try:
        from app.modules.notification.services.booking_notifications import (
            partner_assignment_requested,
        )

        await partner_assignment_requested(
            db,
            partner_id=payload.partner_id,
            master_booking_id=booking_id,
            cab_booking_number=cb.booking_number,
            pickup_location=cb.pickup_location,
            pickup_datetime=(
                cb.pickup_datetime.isoformat() if cb.pickup_datetime else None
            ),
            deadline_iso=deadline.isoformat(),
        )
    except Exception as _notif_exc:  # pragma: no cover - never block the API
        import logging as _log

        _log.getLogger("waytero.booking").warning(
            "assign_partner.notify_failed err=%s", _notif_exc
        )

    return {
        "success": True,
        "message": "Partner assigned; awaiting acceptance",
        "cab_status": "PENDING_PARTNER_ACCEPTANCE",
        "acceptance_deadline": deadline.isoformat(),
        "acceptance_timeout_minutes": timeout_minutes,
    }


# ════════════════════════════════════════════════════════════════
# ASSIGN DRIVER — POST /admin/bookings/{booking_id}/cab/{cab_id}/assign-driver
# Doc Ref: Booking API §17
# Guard: cab must be ASSIGNED (partner already set), driver must be APPROVED
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/cab/{cab_id}/assign-driver", tags=["Admin Bookings"])
async def assign_driver(
    booking_id: int,
    cab_id: int,
    payload: AssignDriverRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """
    Assign driver + vehicle to an already partner-assigned cab booking.
    Guard: cab must be ASSIGNED.
    Guard: driver must be APPROVED.
    Guard: vehicle must be APPROVED and belong to assigned partner.
    """
    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    if cb.booking_status != "ASSIGNED":
        raise HTTPException(
            400,
            f"Cannot assign driver: cab is '{cb.booking_status}'. "
            "Partner must be assigned first (status must be ASSIGNED).",
        )

    # Get latest assignment for partner_id
    latest_assign = (
        await db.execute(
            select(CabBookingAssignment)
            .where(CabBookingAssignment.cab_booking_id == cb.id)
            .order_by(desc(CabBookingAssignment.assigned_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if not latest_assign:
        raise HTTPException(400, "No partner assignment found. Assign a partner first.")

    # Driver guard
    driver = (
        await db.execute(select(Driver).where(Driver.id == payload.driver_id))
    ).scalar_one_or_none()
    if not driver:
        raise HTTPException(404, "Driver not found")
    if driver.status not in ("APPROVED", "ACTIVE"):
        raise HTTPException(
            400,
            f"Driver is not eligible (status: {driver.status}). Driver must be APPROVED or ACTIVE.",
        )

    # Vehicle guard
    vehicle = (
        await db.execute(select(Vehicle).where(Vehicle.id == payload.vehicle_id))
    ).scalar_one_or_none()
    if not vehicle:
        raise HTTPException(404, "Vehicle not found")
    if vehicle.status not in ("APPROVED", "ACTIVE"):
        raise HTTPException(
            400,
            f"Vehicle is not eligible (status: {vehicle.status}). Vehicle must be APPROVED or ACTIVE.",
        )
    if vehicle.partner_id != latest_assign.partner_id:
        raise HTTPException(
            400,
            f"Vehicle does not belong to assigned partner. "
            f"Vehicle belongs to partner_id={vehicle.partner_id}, assigned partner_id={latest_assign.partner_id}.",
        )

    # Update assignment with driver + vehicle
    latest_assign.driver_id = payload.driver_id
    latest_assign.vehicle_id = payload.vehicle_id
    cb.booking_status = "DRIVER_ASSIGNED"

    await _log_timeline(
        db,
        booking_id,
        "DRIVER_ASSIGNED",
        f"Driver '{driver.full_name}' and vehicle '{vehicle.registration_number}' assigned to cab {cb.booking_number}",
    )
    await db.commit()
    return {
        "success": True,
        "message": "Driver assigned successfully",
        "cab_status": "DRIVER_ASSIGNED",
    }


# ════════════════════════════════════════════════════════════════
# REASSIGN PARTNER — POST /admin/bookings/{booking_id}/cab/{cab_id}/reassign
# Doc Ref: Admin API §11
# Guard: Cannot reassign if trip has STARTED
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/cab/{cab_id}/reassign", tags=["Admin Bookings"])
async def reassign_partner(
    booking_id: int,
    cab_id: int,
    payload: ReassignRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """
    Reassign a different partner to a cab booking.

    The new partner enters the same acceptance window as a fresh
    assign: the cab is set to PENDING_PARTNER_ACCEPTANCE with a
    deadline. If the cab was already sitting in PENDING_PARTNER_ACCEPTANCE
    with the previous partner, that row is closed with reason
    ADMIN_REASSIGN so the audit trail is preserved.

    Guard: trip must not have STARTED or beyond.
    """
    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    blocked_statuses = {
        "STARTED",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if cb.booking_status in blocked_statuses:
        raise HTTPException(
            400,
            f"Cannot reassign: trip is '{cb.booking_status}'. "
            "Reassignment is only allowed before trip starts.",
        )

    partner = (
        await db.execute(select(Partner).where(Partner.id == payload.partner_id))
    ).scalar_one_or_none()
    if not partner:
        raise HTTPException(404, "Partner not found")
    if partner.status not in ("APPROVED", "ACTIVE"):
        raise HTTPException(
            400,
            f"Partner is not eligible for reassignment (status: {partner.status}). Partner must be APPROVED or ACTIVE.",
        )

    # Close any pending row first so the audit trail is consistent.
    if cb.booking_status == "PENDING_PARTNER_ACCEPTANCE":
        from app.modules.partner.constants import SYSTEM_REJECT_REASON_ADMIN_REASSIGN

        await _close_pending_partner_assignment(
            db,
            cb,
            reason_code=SYSTEM_REJECT_REASON_ADMIN_REASSIGN,
            description_suffix=(
                f"Admin reassigned cab {cb.booking_number} before partner "
                f"responded; previous assignment auto-closed."
            ),
        )

    timeout_minutes = await _get_int_config(
        db, "PARTNER_ACCEPTANCE_TIMEOUT_MINUTES", 10
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)

    # New assignment record (previous ones kept for audit)
    assignment = CabBookingAssignment(
        cab_booking_id=cb.id,
        partner_id=payload.partner_id,
        assigned_at=datetime.now(timezone.utc),
        assignment_type="MANUAL",
        acceptance_deadline=deadline,
    )
    db.add(assignment)
    cb.booking_status = "PENDING_PARTNER_ACCEPTANCE"
    cb.acceptance_deadline = deadline
    cb.pending_partner_id = payload.partner_id
    cb.partner_responded_at = None

    reason_text = f" Reason: {payload.reason}" if payload.reason else ""
    await _log_timeline(
        db,
        booking_id,
        "PARTNER_REASSIGNED",
        f"Reassigned to partner '{partner.business_name or partner.id}'. "
        f"Awaiting acceptance by {deadline.isoformat()} ({timeout_minutes} min).{reason_text}",
    )
    await db.commit()

    # ── Realtime + push (Doc Ref: BRD Part 7 §155) ──
    # Two parallel notifications:
    #   1. Notify the partner who was just removed (so their popup
    #      closes — the cab is no longer theirs).
    #   2. Notify the newly-assigned partner (so they get the popup +
    #      push for the new acceptance window).
    try:
        from app.modules.notification.realtime import manager as _rt
        from app.modules.notification.services.booking_notifications import (
            partner_assignment_requested,
        )
        from app.modules.partner.models import Partner as _Partner
        from sqlalchemy import select as _sa_select

        # 1. Notify the old partner (the row that was just closed).
        if (
            cb.pending_partner_id is not None
            and cb.pending_partner_id != payload.partner_id
        ):
            old_user = (
                await db.execute(
                    _sa_select(_Partner.user_id).where(
                        _Partner.id == cb.pending_partner_id
                    )
                )
            ).scalar_one_or_none()
            if old_user is not None:
                await _rt.send_to_user(
                    old_user,
                    {
                        "event": "BOOKING_PARTNER_RESPONDED",
                        "data": {
                            "cab_booking_number": cb.booking_number,
                            "master_booking_id": booking_id,
                            "decision": "ADMIN_REASSIGNED",
                            "cab_status": "PENDING_PARTNER_ACCEPTANCE",
                        },
                    },
                )

        # 2. Notify the new partner (popup + push).
        await partner_assignment_requested(
            db,
            partner_id=payload.partner_id,
            master_booking_id=booking_id,
            cab_booking_number=cb.booking_number,
            pickup_location=cb.pickup_location,
            pickup_datetime=(
                cb.pickup_datetime.isoformat() if cb.pickup_datetime else None
            ),
            deadline_iso=deadline.isoformat(),
        )

        await _rt.broadcast(
            {
                "event": "BOOKING_ADMIN_REFRESH",
                "data": {
                    "cab_booking_number": cb.booking_number,
                    "master_booking_id": booking_id,
                    "new_status": "PENDING_PARTNER_ACCEPTANCE",
                    "reason": "PARTNER_REASSIGNED",
                },
            }
        )
    except Exception as _notif_exc:  # pragma: no cover
        import logging as _log

        _log.getLogger("waytero.booking").warning(
            "reassign_partner.notify_failed err=%s", _notif_exc
        )

    from app.modules.notification.services.booking_notifications import (
        notify_booking_updated,
    )

    await notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=booking_id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status="PENDING_PARTNER_ACCEPTANCE",
        action="PARTNER_REASSIGNED",
        partner_ids=[payload.partner_id],
    )

    return {
        "success": True,
        "message": "Partner reassigned; awaiting acceptance",
        "cab_status": "PENDING_PARTNER_ACCEPTANCE",
        "acceptance_deadline": deadline.isoformat(),
        "acceptance_timeout_minutes": timeout_minutes,
    }


# ════════════════════════════════════════════════════════════════


class AdminAcceptBookingRequest(BaseModel):
    service_type: Optional[str] = Field(
        None, description="CAB | HOTEL | TOUR — the pending service to accept"
    )


@router.post("/{booking_id}/accept", tags=["Admin Bookings"])
async def admin_accept_booking(
    booking_id: int,
    payload: AdminAcceptBookingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """
    Accept a website booking from the admin realtime accept modal.

    Transitions (per service):
      CAB   → master DRAFT→CONFIRMED; cab stays PENDING_ASSIGNMENT so the
              admin can assign a partner right after.
      HOTEL → reservation PENDING_PAYMENT→CONFIRMED; master→CONFIRMED.
      TOUR  → booking PENDING_CONFIRMATION→CONFIRMED; master→CONFIRMED.

    Returns the master + service statuses so the FE can update its queue.
    """
    mb = await _get_master_booking(db, booking_id)
    service_type = (payload.service_type or "").upper()

    if service_type == "CAB":
        cb = next(
            (c for c in mb.cab_bookings if c.booking_status == "PENDING_ASSIGNMENT"),
            None,
        )
        if not cb:
            raise HTTPException(409, "No pending cab booking found to accept")
        mb.booking_status = "CONFIRMED"
        await _log_timeline(
            db,
            booking_id,
            "BOOKING_ACCEPTED",
            f"Cab {cb.booking_number} accepted by admin. Ready for partner assignment.",
        )
        await db.commit()
        # ── Email: booking confirmation to the customer ──
        await _send_booking_email(
            db,
            event_type="booking_confirmed",
            customer_id=mb.customer_id,
            context={
                "service": "Cab",
                "booking_number": cb.booking_number,
                "message": (
                    f"Your cab booking {cb.booking_number} is confirmed. A partner is "
                    "being assigned — driver details will be shared before pickup."
                ),
                "details": [
                    ("Trip type", cb.trip_type or ""),
                    ("Pickup", cb.pickup_location or ""),
                    ("Drop", cb.drop_location or ""),
                    (
                        "Pickup time",
                        (
                            cb.pickup_datetime.strftime("%d %b %Y, %I:%M %p")
                            if cb.pickup_datetime
                            else ""
                        ),
                    ),
                ],
                **(
                    {"amount": float(cb.final_amount), "amount_label": "Fare"}
                    if cb.final_amount
                    else {}
                ),
            },
            related_type="CAB_BOOKING",
            related_id=cb.id,
        )
        return {
            "success": True,
            "message": "Cab booking accepted",
            "booking_status": mb.booking_status,
            "service_status": cb.booking_status,
        }

    if service_type == "HOTEL":
        hr = (
            await db.execute(
                select(HotelReservation).where(
                    HotelReservation.master_booking_id == booking_id,
                    HotelReservation.reservation_status == "PENDING_PAYMENT",
                )
            )
        ).scalar_one_or_none()
        if not hr:
            raise HTTPException(409, "No pending hotel reservation found to accept")
        hr.reservation_status = "CONFIRMED"
        mb.booking_status = "CONFIRMED"
        await _log_timeline(
            db,
            booking_id,
            "HOTEL_CONFIRMED",
            f"Hotel booking {hr.reservation_number or hr.id} accepted by admin.",
        )
        await db.commit()
        # ── Email: booking confirmation to the customer ──
        hotel_name = await db.scalar(
            select(Hotel.hotel_name).where(Hotel.id == hr.hotel_id)
        )
        await _send_booking_email(
            db,
            event_type="booking_confirmed",
            customer_id=mb.customer_id,
            context={
                "service": "Hotel",
                "booking_number": hr.reservation_number or str(hr.id),
                "message": (
                    "Your hotel booking is confirmed. Show the booking number at "
                    "check-in and keep your ID proof handy."
                ),
                "details": [
                    ("Hotel", hotel_name or ""),
                    (
                        "Check-in",
                        (
                            hr.check_in_date.strftime("%d %b %Y")
                            if hr.check_in_date
                            else ""
                        ),
                    ),
                    (
                        "Check-out",
                        (
                            hr.check_out_date.strftime("%d %b %Y")
                            if hr.check_out_date
                            else ""
                        ),
                    ),
                    ("Rooms", str(hr.rooms_count or "")),
                ],
                **(
                    {"amount": float(hr.total_amount), "amount_label": "Stay amount"}
                    if hr.total_amount
                    else {}
                ),
            },
            related_type="HOTEL_RESERVATION",
            related_id=hr.id,
        )
        # The partner's fixed-assignment popup is still open on their side —
        # tell them the admin already accepted so it closes + ringtone stops.
        try:
            partner_user_id = await db.scalar(
                select(Partner.user_id)
                .join(Hotel, Hotel.partner_id == Partner.id)
                .where(Hotel.id == hr.hotel_id)
            )
            if partner_user_id:
                from app.modules.notification.realtime import manager as _rt

                await _rt.send_to_user(
                    partner_user_id,
                    {
                        "event": "BOOKING_PARTNER_RESPONDED",
                        "data": {
                            "service_type": "HOTEL",
                            "reservation_id": hr.id,
                            "master_booking_id": booking_id,
                            "decision": "CONFIRMED",
                        },
                    },
                )
        except Exception:  # pragma: no cover - WS is best-effort
            pass
        return {
            "success": True,
            "message": "Hotel booking accepted",
            "booking_status": mb.booking_status,
            "service_status": hr.reservation_status,
        }

    if service_type == "TOUR":
        tb = (
            await db.execute(
                select(TourBooking).where(
                    TourBooking.master_booking_id == booking_id,
                    TourBooking.booking_status == "PENDING_CONFIRMATION",
                )
            )
        ).scalar_one_or_none()
        if not tb:
            raise HTTPException(409, "No pending tour booking found to accept")
        tb.booking_status = "CONFIRMED"
        mb.booking_status = "CONFIRMED"
        await _log_timeline(
            db,
            booking_id,
            "BOOKING_ACCEPTED",
            f"Tour booking {tb.booking_number} accepted by admin.",
        )
        await db.commit()
        # ── Email: booking confirmation to the customer ──
        package_name = await db.scalar(
            select(TourPackage.package_name).where(TourPackage.id == tb.package_id)
        )
        await _send_booking_email(
            db,
            event_type="booking_confirmed",
            customer_id=mb.customer_id,
            context={
                "service": "Tour package",
                "booking_number": tb.booking_number,
                "message": (
                    "Your tour package is confirmed. The itinerary and partner "
                    "details are available in your bookings."
                ),
                "details": [
                    ("Package", package_name or ""),
                    (
                        "Travel start",
                        (
                            tb.travel_start_date.strftime("%d %b %Y")
                            if tb.travel_start_date
                            else ""
                        ),
                    ),
                    ("Travellers", str(tb.persons_count or "")),
                ],
                **(
                    {"amount": float(tb.total_amount), "amount_label": "Package amount"}
                    if tb.total_amount
                    else {}
                ),
            },
            related_type="TOUR_BOOKING",
            related_id=tb.id,
        )
        # The package's partner has the fixed-assignment popup open — close it
        # (and stop their ringtone) since the admin already accepted.
        try:
            partner_user_id = await db.scalar(
                select(Partner.user_id)
                .join(TourPackage, TourPackage.partner_id == Partner.id)
                .where(TourPackage.id == tb.package_id)
            )
            if partner_user_id:
                from app.modules.notification.realtime import manager as _rt

                await _rt.send_to_user(
                    partner_user_id,
                    {
                        "event": "BOOKING_PARTNER_RESPONDED",
                        "data": {
                            "service_type": "TOUR",
                            "tour_booking_id": tb.id,
                            "master_booking_id": booking_id,
                            "decision": "CONFIRMED",
                        },
                    },
                )
        except Exception:  # pragma: no cover - WS is best-effort
            pass
        return {
            "success": True,
            "message": "Tour booking accepted",
            "booking_status": mb.booking_status,
            "service_status": tb.booking_status,
        }

    raise HTTPException(422, "service_type must be one of CAB, HOTEL, TOUR")


# ════════════════════════════════════════════════════════════════
# RESCHEDULE — POST /admin/bookings/{booking_id}/cab/{cab_id}/reschedule
# Guard: Cannot reschedule if trip has STARTED
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/cab/{cab_id}/reschedule", tags=["Admin Bookings"])
async def reschedule_cab(
    booking_id: int,
    cab_id: int,
    payload: RescheduleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """Reschedule pickup datetime. Guard: trip must not have started."""
    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    if cb.booking_status in {
        "STARTED",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }:
        raise HTTPException(
            400,
            f"Cannot reschedule: trip is '{cb.booking_status}'. Reschedule is only allowed before trip starts.",
        )

    old_dt = cb.pickup_datetime
    cb.pickup_datetime = payload.pickup_datetime
    reason_text = f" Reason: {payload.reason}" if payload.reason else ""

    await _log_timeline(
        db,
        booking_id,
        "BOOKING_RESCHEDULED",
        f"Pickup rescheduled from {old_dt} to {payload.pickup_datetime}.{reason_text}",
    )
    await db.commit()

    from app.modules.notification.services.booking_notifications import (
        notify_booking_updated,
    )

    await notify_booking_updated(
        db,
        service_type="CAB",
        master_booking_id=booking_id,
        service_id=cb.id,
        booking_number=cb.booking_number,
        service_status=cb.booking_status,
        action="RESCHEDULE",
        payment_status=mb.payment_status,
        partner_ids=[a.partner_id for a in (cb.assignments or [])],
    )
    return {
        "success": True,
        "message": "Pickup rescheduled",
        "new_pickup_datetime": payload.pickup_datetime.isoformat(),
    }


# ════════════════════════════════════════════════════════════════
# UPDATE FINAL AMOUNT — POST /admin/bookings/{booking_id}/cab/{cab_id}/final-amount
# Doc Ref: Finance §22 — Business Rules
# Guard: Only when COMPLETED or SETTLEMENT_PENDING
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/cab/{cab_id}/final-amount", tags=["Admin Bookings"])
async def update_final_amount(
    booking_id: int,
    cab_id: int,
    payload: UpdateFinalAmountRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    """
    Set/update the final trip amount (actual billing after trip complete).
    Guard: Only allowed when cab is COMPLETED or SETTLEMENT_PENDING.
    """
    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    if cb.booking_status not in {"COMPLETED", "SETTLEMENT_PENDING"}:
        raise HTTPException(
            400,
            f"Final amount can only be set when cab is COMPLETED or SETTLEMENT_PENDING. "
            f"Current status: '{cb.booking_status}'.",
        )
    if payload.final_amount <= 0:
        raise HTTPException(400, "Final amount must be greater than 0.")

    old = float(cb.final_amount or 0)
    cb.final_amount = Decimal(str(payload.final_amount))
    # Update master booking total_amount to reflect final
    mb.total_amount = cb.final_amount

    reason_text = f" Reason: {payload.reason}" if payload.reason else ""
    await _log_timeline(
        db,
        booking_id,
        "AMOUNT_UPDATED",
        f"Final amount updated from ₹{old} to ₹{payload.final_amount}.{reason_text}",
    )
    await db.commit()
    return {
        "success": True,
        "message": "Final amount updated",
        "final_amount": payload.final_amount,
    }


# ════════════════════════════════════════════════════════════════
# SETTLEMENT FLOW — superseded by POST /admin/settlements/settle
#
# The old "mark settlement pending" / "mark settled" endpoints were removed:
# they flipped status strings without any payment/wallet/TDS/coupon math,
# which let a booking be marked SETTLED while the partner was never paid and
# GST/invoice were never recorded. The single supported path today is:
#
#   COMPLETED ─(collect payment)─> SETTLEMENT_PENDING ─(POST /admin/settlements/settle)─> SETTLED
#
#   * collect-payment lives on the trip-assist console and the partner portal
#     (it records payment_mode/collector, generates the invoice, and moves the
#     cab to SETTLEMENT_PENDING).
#   * settle lives at /admin/settlements/settle and does the full wallet
#     movement, TDS and coupon disbursement before flipping to SETTLED.
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
# CAB INVOICE DOWNLOAD — GET /admin/bookings/{booking_id}/cab/{cab_id}/download-invoice
#
# Mirrors the partner-side partner_download_invoice (partner/booking_api.py)
# but without partner ownership guard — admins can download any cab invoice.
# Same guards: booking must be COMPLETED / SETTLEMENT_PENDING / SETTLED,
# payment must be recorded, and invoice_number must be present (auto-set on
# payment). Reuses generate_invoice_pdf from invoice_pdf_service.
# Doc Ref: BRD Part 3 §45 (settlement), §48 (tax invoice)
# ════════════════════════════════════════════════════════════════


@router.get("/{booking_id}/cab/{cab_id}/download-invoice", tags=["Admin Bookings"])
async def admin_download_cab_invoice(
    booking_id: int,
    cab_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Stream a premium PDF invoice for a completed + paid cab booking.

    Same flow as the partner download endpoint, but admin-gated and not
    partner-scoped. The frontend button on CabBookingDetailPage calls this
    when booking.payment_status === "PAID" and cab.invoice_number is set.
    """
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.invoice_pdf_service import generate_invoice_pdf
    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func_cab_inv
    from datetime import timezone as _tz_cab_inv

    mb = await _get_master_booking(db, booking_id)
    cb = next((c for c in mb.cab_bookings if c.id == cab_id), None)
    if not cb:
        raise HTTPException(
            404, f"Cab booking {cab_id} not found on master {booking_id}."
        )

    if cb.booking_status not in ("COMPLETED", "SETTLEMENT_PENDING", "SETTLED"):
        raise HTTPException(
            400,
            f"Invoice only available for completed bookings "
            f"(current: {cb.booking_status}).",
        )
    if not cb.payment_mode:
        raise HTTPException(400, "Record payment before downloading invoice.")
    if not cb.invoice_number:
        raise HTTPException(
            400,
            "Invoice number not found. Collect payment first to auto-generate invoice.",
        )

    # ── Platform config ──────────────────────────────────────────────
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

    # ── Customer + city ─────────────────────────────────────────────
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

    # ── Vehicle category + driver / vehicle / partner ────────────────
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

    # Pick the partner/admin from the latest assignment. Admins see all
    # cabs regardless of which partner has it now, so we just attach the
    # most recent assignment's partner for the invoice line.
    if cb.assignments:
        latest = sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        if latest.partner_id:
            p = (
                await db.execute(select(Partner).where(Partner.id == latest.partner_id))
            ).scalar_one_or_none()
            if p:
                partner_name = p.business_name or f"Partner #{p.id}"
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

    # ── Coupon + advance ────────────────────────────────────────────
    coupon_q = await db.execute(
        select(_func_cab_inv.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    advance_paid = await advance_service.get_advance_amount(db, cb.id)

    pdf_bytes = generate_invoice_pdf(
        invoice_number=cb.invoice_number,
        cab_booking_number=cb.booking_number,
        booking_number=mb.booking_number,
        invoice_date=datetime.now(_tz_cab_inv.utc),
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
# CANCEL BOOKING — POST /admin/bookings/{booking_id}/cancel
# Doc Ref: Admin API §11, Booking API §20
# Guard: Cannot cancel if already IN_PROGRESS/COMPLETED/CLOSED
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/cancel", tags=["Admin Bookings"])
async def cancel_booking(
    booking_id: int,
    payload: CancelBookingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Force-cancel a master booking (admin action).
    Guard: Cannot cancel if booking is IN_PROGRESS, COMPLETED, or CLOSED.

    Migration 0043: this endpoint now delegates to the cancellation
    policy engine (app.modules.booking.services.cancellation_policy).
    The admin no longer types a charge + refund — those are computed
    from the live cab / hotel ladders and persisted on the cancellation
    row with a `policy_snapshot` so the audit trail is permanent.
    """
    from uuid import UUID as _UUID
    from app.modules.booking.services.cancellation_policy import (
        apply_cab_cancellation,
        apply_hotel_cancellation,
        CANCEL_SOURCE_ADMIN,
    )

    mb = await _get_master_booking(db, booking_id)

    blocked = {"IN_PROGRESS", "COMPLETED", "CLOSED", "CANCELLED"}
    if mb.booking_status in blocked:
        raise HTTPException(
            400,
            f"Cannot cancel: booking is '{mb.booking_status}'. "
            "Active trips (IN_PROGRESS/COMPLETED/CLOSED) cannot be cancelled.",
        )

    if not payload.reason or not payload.reason.strip():
        raise HTTPException(400, "Cancellation reason is required.")

    actor_uid = _UUID(current_user["sub"])
    actor_role = current_user.get("role", "ADMIN")

    total_charge = Decimal("0")
    total_refund = Decimal("0")
    cab_results = []
    hotel_results = []

    for cb in mb.cab_bookings:
        if cb.booking_status in {"COMPLETED", "SETTLED", "CANCELLED"}:
            continue
        result = await apply_cab_cancellation(
            db,
            cab_booking_id=cb.id,
            master_booking_id=booking_id,
            cancellation_reason=payload.reason,
            cancelled_by_user_id=actor_uid,
            cancelled_by_role=actor_role,
            cancelled_source=CANCEL_SOURCE_ADMIN,
        )
        cab_results.append(result)
        total_charge += Decimal(result["charge"])
        total_refund += Decimal(result["refund_amount"])

    # Cancel all live hotel reservations under this master.
    from app.modules.hotel.models import HotelReservation as _HR

    hotel_q = (
        (await db.execute(select(_HR).where(_HR.master_booking_id == booking_id)))
        .scalars()
        .all()
    )
    for hr in hotel_q:
        if hr.reservation_status in {
            "CHECKED_IN",
            "IN_HOUSE",
            "CHECKED_OUT",
            "COMPLETED",
            "SETTLED",
            "CANCELLED",
            "REJECTED",
        }:
            continue
        result = await apply_hotel_cancellation(
            db,
            hotel_reservation_id=hr.id,
            master_booking_id=booking_id,
            cancellation_reason=payload.reason,
            cancelled_by_user_id=actor_uid,
            cancelled_by_role=actor_role,
            cancelled_source=CANCEL_SOURCE_ADMIN,
        )
        hotel_results.append(result)
        total_charge += Decimal(result["charge"])
        total_refund += Decimal(result["refund_amount"])

    # Legacy booking_cancellations row (per-master) — engine has already
    # written one via apply_cab_cancellation / apply_hotel_cancellation for
    # the FIRST service, but if there were zero cabs/hotels live at cancel
    # time we still want the row. We do an upsert keyed on master_booking_id
    # so it's idempotent.
    from sqlalchemy import text as _text

    await db.execute(
        _text(
            """
            INSERT INTO booking_cancellations
                (master_booking_id, cancelled_by, cancellation_reason,
                 cancellation_charge, refund_amount, cancelled_at,
                 cancelled_source, cancelled_by_role)
            VALUES (:m, :uid, :reason, :charge, :refund, NOW(), 'ADMIN', :role)
            ON CONFLICT (master_booking_id) DO UPDATE
              SET cancellation_charge = EXCLUDED.cancellation_charge,
                  refund_amount       = EXCLUDED.refund_amount,
                  cancellation_reason = EXCLUDED.cancellation_reason
            """
        ),
        {
            "m": booking_id,
            "uid": str(actor_uid),
            "reason": payload.reason,
            "charge": total_charge,
            "refund": total_refund,
            "role": actor_role,
        },
    )
    # Flip master status if every service is now terminal.
    if not cab_results and not hotel_results:
        mb.booking_status = "CANCELLED"
    await _log_timeline(
        db,
        booking_id,
        "BOOKING_CANCELLED",
        f"Admin cancelled booking. Reason: {payload.reason}. "
        f"Total charge: ₹{total_charge}, Total refund: ₹{total_refund}.",
    )
    await db.commit()

    from app.modules.notification.services.booking_notifications import (
        notify_booking_updated,
    )

    # Fan out per service so cab + hotel detail pages on both portals refresh.
    for cb in mb.cab_bookings:
        if cb.booking_status == "CANCELLED":
            await notify_booking_updated(
                db,
                service_type="CAB",
                master_booking_id=booking_id,
                service_id=cb.id,
                booking_number=cb.booking_number,
                service_status="CANCELLED",
                action="ADMIN_CANCEL",
                payment_status=mb.payment_status,
                partner_ids=[a.partner_id for a in (cb.assignments or [])],
            )
    for hr in hotel_q:
        if hr.reservation_status == "CANCELLED":
            await notify_booking_updated(
                db,
                service_type="HOTEL",
                master_booking_id=booking_id,
                service_id=hr.id,
                booking_number=hr.reservation_number or str(hr.id),
                service_status="CANCELLED",
                action="ADMIN_CANCEL",
                payment_status=None,
            )
    return {
        "success": True,
        "message": "Booking cancelled",
        "booking_status": "CANCELLED",
        "cancellation_charge": float(total_charge),
        "refund_amount": float(total_refund),
        "cabs_cancelled": len(cab_results),
        "hotels_cancelled": len(hotel_results),
    }


# ════════════════════════════════════════════════════════════════
# ADD INTERNAL NOTE — POST /admin/bookings/{booking_id}/notes
# Doc Ref: DB Schema Part 4 §18 — booking_notes
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/notes", tags=["Admin Bookings"])
async def add_note(
    booking_id: int, payload: AddNoteRequest, db: AsyncSession = Depends(get_db)
):
    """Add an internal CCO/Admin note to a booking."""
    await _get_master_booking(db, booking_id)
    note = BookingNote(
        master_booking_id=booking_id,
        note=payload.note.strip(),
        created_at=datetime.now(timezone.utc),
    )
    db.add(note)
    await db.commit()
    return {"success": True, "message": "Note added"}


@router.get("/{booking_id}/notes", tags=["Admin Bookings"])
async def get_notes(booking_id: int, db: AsyncSession = Depends(get_db)):
    """Get all internal notes for a booking."""
    mb = await _get_master_booking(db, booking_id)
    return [
        {"id": n.id, "note": n.note, "created_at": n.created_at.isoformat()}
        for n in sorted(mb.notes, key=lambda x: x.created_at, reverse=True)
    ]


# ════════════════════════════════════════════════════════════════
# LIST PARTNERS FOR ASSIGNMENT — GET /admin/bookings/resources/partners
# Returns APPROVED partners, optionally filtered by city
# ════════════════════════════════════════════════════════════════


@router.get("/resources/partners", tags=["Admin Bookings"])
async def list_assignable_partners(
    city_id: Optional[int] = Query(None),
    vehicle_category_id: Optional[int] = Query(
        None, description="Filter partners that have vehicles matching this category"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    List APPROVED partners for assignment with enriched data:
    - has_cab_service: whether partner has CAB service registered
    - available_vehicles: count of APPROVED vehicles matching vehicle_category_id (if provided)
    - partner_type, mobile for display
    """
    q = select(Partner).where(Partner.status.in_(["APPROVED", "ACTIVE"]))
    if city_id:
        q = q.where(Partner.city_id == city_id)
    partners = (await db.execute(q.order_by(Partner.business_name))).scalars().all()

    # Fetch active cab booking vehicle_ids (vehicles already assigned to active trips)
    active_vehicle_ids_q = (
        select(CabBookingAssignment.vehicle_id)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .where(
            CabBooking.booking_status.in_(["ASSIGNED", "DRIVER_ASSIGNED", "STARTED"]),
            CabBookingAssignment.vehicle_id.is_not(None),
        )
    )
    active_vehicle_ids = set((await db.execute(active_vehicle_ids_q)).scalars().all())

    result = []
    for p in partners:
        # Check CAB service
        cab_svc = (
            await db.execute(
                select(PartnerService).where(
                    and_(
                        PartnerService.partner_id == p.id,
                        PartnerService.service_type == "CAB",
                        PartnerService.is_active.is_(True),
                    )
                )
            )
        ).scalar_one_or_none()

        # Count available vehicles (APPROVED, matching category, not on active trip)
        veh_q = select(func.count(Vehicle.id)).where(
            and_(Vehicle.partner_id == p.id, Vehicle.status.in_(["APPROVED", "ACTIVE"]))
        )
        if vehicle_category_id:
            veh_q = veh_q.where(Vehicle.vehicle_category_id == vehicle_category_id)
        total_vehicles = (await db.execute(veh_q)).scalar() or 0

        # Count free vehicles (not in active assignments)
        # Note: use notin_ with list to avoid SQLAlchemy bug with empty sets
        free_veh_conditions = [
            Vehicle.partner_id == p.id,
            Vehicle.status.in_(["APPROVED", "ACTIVE"]),
        ]
        if active_vehicle_ids:
            free_veh_conditions.append(Vehicle.id.notin_(list(active_vehicle_ids)))
        free_veh_q = select(func.count(Vehicle.id)).where(and_(*free_veh_conditions))
        if vehicle_category_id:
            free_veh_q = free_veh_q.where(
                Vehicle.vehicle_category_id == vehicle_category_id
            )
        free_vehicles = (await db.execute(free_veh_q)).scalar() or 0

        result.append(
            {
                "id": p.id,
                "name": p.business_name or f"Partner #{p.id}",
                "owner_name": p.owner_name,
                "mobile": p.mobile,
                "city_id": p.city_id,
                "partner_type": p.partner_type,
                "has_cab_service": cab_svc is not None,
                "total_vehicles": total_vehicles,
                "free_vehicles": free_vehicles,
            }
        )

    return result


# ════════════════════════════════════════════════════════════════
# PARTNER DETAIL — GET /admin/bookings/resources/partners/{partner_id}
# ════════════════════════════════════════════════════════════════


@router.get("/resources/partners/{partner_id}", tags=["Admin Bookings"])
async def get_partner_detail(
    partner_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get partner profile details for display in the assign-driver modal."""
    partner = (
        await db.execute(select(Partner).where(Partner.id == partner_id))
    ).scalar_one_or_none()
    if not partner:
        raise HTTPException(404, "Partner not found")
    city = (
        await db.execute(select(City).where(City.id == partner.city_id))
    ).scalar_one_or_none()
    return {
        "id": partner.id,
        "name": partner.business_name or f"Partner #{partner.id}",
        "owner_name": partner.owner_name,
        "mobile": partner.mobile,
        "email": partner.email,
        "partner_code": partner.partner_code,
        "city_id": partner.city_id,
        "city_name": city.name if city else None,
        "status": partner.status,
    }


# ════════════════════════════════════════════════════════════════
# LIST DRIVERS FOR ASSIGNMENT — GET /admin/bookings/resources/drivers
# Returns APPROVED drivers for a given partner
# ════════════════════════════════════════════════════════════════


@router.get("/resources/drivers", tags=["Admin Bookings"])
async def list_assignable_drivers(
    partner_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """
    List APPROVED drivers for a partner with availability status and busy flag.
    is_busy=True means driver is currently on an active trip assignment.
    Doc Ref: Driver §2, DriverAvailability §6-7, Dispatch Rules §25
    """
    # Find driver_ids active on trips (ASSIGNED, DRIVER_ASSIGNED, STARTED)
    active_driver_ids_q = (
        select(CabBookingAssignment.driver_id)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .where(
            CabBooking.booking_status.in_(["ASSIGNED", "DRIVER_ASSIGNED", "STARTED"]),
            CabBookingAssignment.driver_id.is_not(None),
        )
    )
    active_driver_ids = set((await db.execute(active_driver_ids_q)).scalars().all())

    drivers = (
        (
            await db.execute(
                select(Driver)
                .where(
                    and_(
                        Driver.partner_id == partner_id,
                        Driver.status.in_(["APPROVED", "ACTIVE"]),
                    )
                )
                .order_by(Driver.full_name)
            )
        )
        .scalars()
        .all()
    )

    result = []
    for d in drivers:
        avail_row = (
            await db.execute(
                select(DriverAvailability).where(DriverAvailability.driver_id == d.id)
            )
        ).scalar_one_or_none()
        avail_status = avail_row.availability_status if avail_row else "OFFLINE"
        is_busy = d.id in active_driver_ids

        result.append(
            {
                "id": d.id,
                "name": d.full_name,
                "mobile": d.mobile,
                "license_number": d.license_number,
                "license_expiry_date": (
                    d.license_expiry_date.isoformat() if d.license_expiry_date else None
                ),
                "joining_date": d.joining_date.isoformat() if d.joining_date else None,
                "availability_status": avail_status,
                "is_busy": is_busy,
            }
        )
    return result


# ════════════════════════════════════════════════════════════════
# LIST VEHICLES FOR ASSIGNMENT — GET /admin/bookings/resources/vehicles
# Returns APPROVED vehicles for a given partner, filtered by category
# ════════════════════════════════════════════════════════════════


@router.get("/resources/vehicles", tags=["Admin Bookings"])
async def list_assignable_vehicles(
    partner_id: int = Query(...),
    vehicle_category_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    List APPROVED/ACTIVE vehicles for a partner.
    Excludes vehicles currently assigned to active trips (ASSIGNED/DRIVER_ASSIGNED/STARTED)
    AND vehicles in MAINTENANCE / ON_TRIP — those statuses are set by the
    breakdown flow (migration 0041) and must not be re-assignable.
    Each vehicle includes is_busy flag for UI display.
    """
    # Get vehicle_ids on ACTIVE (closed_at IS NULL) assignments only —
    # closed handover rows no longer mark the vehicle as busy.
    active_vehicle_ids_q = (
        select(CabBookingAssignment.vehicle_id)
        .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
        .where(
            CabBooking.booking_status.in_(["ASSIGNED", "DRIVER_ASSIGNED", "STARTED"]),
            CabBookingAssignment.vehicle_id.is_not(None),
            CabBookingAssignment.closed_at.is_(None),
        )
    )
    active_vehicle_ids = set((await db.execute(active_vehicle_ids_q)).scalars().all())

    q = select(Vehicle).where(
        and_(
            Vehicle.partner_id == partner_id,
            Vehicle.status.in_(["APPROVED", "ACTIVE"]),
            Vehicle.status.notin_(["MAINTENANCE", "ON_TRIP", "SUSPENDED"]),
        )
    )
    if vehicle_category_id:
        q = q.where(Vehicle.vehicle_category_id == vehicle_category_id)
    vehicles = (
        (await db.execute(q.order_by(Vehicle.registration_number))).scalars().all()
    )

    # Build category name cache to avoid N+1
    cat_cache: dict[int, str] = {}
    result = []
    for v in vehicles:
        cat_name = None
        if v.vehicle_category_id:
            if v.vehicle_category_id not in cat_cache:
                cat_r = await db.execute(
                    select(VehicleCategory).where(
                        VehicleCategory.id == v.vehicle_category_id
                    )
                )
                cat = cat_r.scalar_one_or_none()
                cat_cache[v.vehicle_category_id] = cat.category_name if cat else ""
            cat_name = cat_cache[v.vehicle_category_id]
        result.append(
            {
                "id": v.id,
                "registration_number": v.registration_number,
                "vehicle_category_id": v.vehicle_category_id,
                "vehicle_category_name": cat_name,
                "vehicle_brand": v.vehicle_brand,
                "vehicle_model": v.vehicle_model,
                "fuel_type": v.fuel_type,
                "seating_capacity": v.seating_capacity,
                "is_busy": v.id in active_vehicle_ids,
            }
        )
    return result


# ════════════════════════════════════════════════════════════════
# HOTEL BOOKING ENDPOINTS
# Prefix: /admin/bookings/{booking_id}/hotel/{hotel_id}/...
# Doc Ref: BRD Part 4 §57-92
# Status flow: PENDING_PAYMENT → AWAITING_HOTEL_CONFIRMATION →
#   CONFIRMED → CHECKED_IN → IN_HOUSE → CHECKED_OUT → COMPLETED → SETTLED
#   (+ CANCELLED, NO_SHOW, REJECTED)
# ════════════════════════════════════════════════════════════════


async def _get_hotel_booking(
    db: AsyncSession, booking_id: int, hotel_id: int
) -> HotelReservation:
    hr = (
        await db.execute(
            select(HotelReservation).where(
                HotelReservation.id == hotel_id,
                HotelReservation.master_booking_id == booking_id,
            )
        )
    ).scalar_one_or_none()
    if not hr:
        raise HTTPException(404, "Hotel booking not found in this master booking")
    return hr


async def _notify_hotel_update(
    db: AsyncSession,
    hr: HotelReservation,
    *,
    action: str,
    payment_collected_status: Optional[str] = None,
) -> None:
    """Best-effort WS fan-out after an admin-side hotel lifecycle mutation.

    Resolves the hotel's partner (so the owning partner portal refreshes) and
    fans out to every online admin user. Deliberately wrapped in try/except and
    never awaited as a hard dependency by the caller.
    """
    from app.modules.notification.services.booking_notifications import (
        notify_hotel_booking_updated,
    )

    partner_id = None
    try:
        partner_id = (
            await db.execute(select(Hotel.partner_id).where(Hotel.id == hr.hotel_id))
        ).scalar_one_or_none()
    except Exception:
        partner_id = None

    try:
        await notify_hotel_booking_updated(
            db,
            master_booking_id=int(hr.master_booking_id),
            reservation_id=int(hr.id),
            reservation_number=str(hr.reservation_number or hr.id),
            hotel_status=str(hr.reservation_status or "UNKNOWN"),
            action=action,
            partner_id=partner_id,
            payment_collected_status=payment_collected_status,
        )
    except Exception:
        # Never let a notification failure fail the HTTP response.
        pass


@router.post("/{booking_id}/hotel/{hotel_id}/confirm", tags=["Admin Hotel Bookings"])
async def hotel_confirm(
    booking_id: int,
    hotel_id: int,
    payload: HotelConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin records hotel acceptance — moves to CONFIRMED.
    Allowed from: AWAITING_HOTEL_CONFIRMATION
    """
    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status != "AWAITING_HOTEL_CONFIRMATION":
        raise HTTPException(
            400,
            f"Cannot confirm: status is '{hr.reservation_status}', expected AWAITING_HOTEL_CONFIRMATION.",
        )

    hr.reservation_status = "CONFIRMED"
    if payload.hotel_confirmation_number:
        hr.hotel_confirmation_number = payload.hotel_confirmation_number.strip()

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_CONFIRMED",
        f"Hotel booking {hr.reservation_number or hr.id} confirmed. Confirmation#: {hr.hotel_confirmation_number or 'N/A'}",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_CONFIRMED")
    return {
        "success": True,
        "hotel_status": "CONFIRMED",
        "hotel_confirmation_number": hr.hotel_confirmation_number,
    }


@router.post("/{booking_id}/hotel/{hotel_id}/reject", tags=["Admin Hotel Bookings"])
async def hotel_reject(
    booking_id: int,
    hotel_id: int,
    payload: HotelRejectRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin records hotel rejection — moves to REJECTED.
    Allowed from: AWAITING_HOTEL_CONFIRMATION
    Records refund amount for processing.
    """
    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status != "AWAITING_HOTEL_CONFIRMATION":
        raise HTTPException(
            400,
            f"Cannot reject: status is '{hr.reservation_status}', expected AWAITING_HOTEL_CONFIRMATION.",
        )
    if not payload.reason.strip():
        raise HTTPException(400, "Rejection reason is required.")

    hr.reservation_status = "REJECTED"
    hr.cancellation_reason = payload.reason.strip()
    hr.refund_amount = Decimal(str(payload.refund_amount))

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_REJECTED",
        f"Hotel booking {hr.reservation_number or hr.id} rejected. Reason: {payload.reason}. Refund: ₹{payload.refund_amount}",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_REJECTED")
    return {"success": True, "hotel_status": "REJECTED"}


@router.get(
    "/{booking_id}/hotel/{hotel_id}/available-rooms", tags=["Admin Hotel Bookings"]
)
async def hotel_available_rooms(
    booking_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns rooms that are AVAILABLE and ACTIVE for the hotel/category of this booking.
    Used by the check-in modal to let admin assign physical rooms to the guest.
    """
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    q = (
        select(HotelRoom)
        .where(
            HotelRoom.hotel_id == hr.hotel_id,
            HotelRoom.room_status == "AVAILABLE",
            HotelRoom.is_active,
        )
        .order_by(HotelRoom.room_number)
    )
    if hr.room_category_id:
        q = q.where(HotelRoom.room_category_id == hr.room_category_id)

    rooms = (await db.execute(q)).scalars().all()

    return {
        "success": True,
        "rooms_needed": hr.rooms_count or 1,
        "data": [
            {"id": r.id, "room_number": r.room_number, "floor_number": r.floor_number}
            for r in rooms
        ],
    }


@router.post("/{booking_id}/hotel/{hotel_id}/check-in", tags=["Admin Hotel Bookings"])
async def hotel_check_in(
    booking_id: int,
    hotel_id: int,
    payload: HotelCheckInRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin records guest check-in.
    Allowed from: CONFIRMED
    Records ID proof type and number for verification.
    """
    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status != "CONFIRMED":
        raise HTTPException(
            400,
            f"Cannot check in: status is '{hr.reservation_status}', expected CONFIRMED.",
        )

    valid_id_proofs = {"AADHAAR", "PASSPORT", "DL"}
    if payload.id_proof.upper() not in valid_id_proofs:
        raise HTTPException(
            400,
            f"Invalid id_proof '{payload.id_proof}'. Valid: {', '.join(valid_id_proofs)}",
        )
    if not payload.id_number.strip():
        raise HTTPException(400, "ID number is required for check-in.")

    # ── Check-in date guard ────────────────────────────────────────────────
    # The check is done BEFORE we mutate the row so a 409 response leaves the
    # reservation still in CONFIRMED. The mismatch descriptor is rendered in
    # the admin-portal modal; the user must click "Confirm anyway" to set
    # `confirm_date_mismatch=True`, which records the override on the
    # hotel_checkins.remarks timeline entry.
    actual_ci = payload.actual_check_in_at or datetime.now(timezone.utc)
    mismatch = _check_in_date_mismatch(hr, actual_ci)
    if mismatch and not payload.confirm_date_mismatch:
        raise HTTPException(
            status_code=409,
            detail={
                "code": mismatch["code"],
                "booking_date": mismatch["booking_date"],
                "actual_date": mismatch["actual_date"],
                "delta_days": mismatch["delta_days"],
                "message": mismatch["message"],
            },
        )

    hr.reservation_status = "CHECKED_IN"
    hr.check_in_id_proof = payload.id_proof.upper()
    hr.check_in_id_number = payload.id_number.strip()
    hr.actual_check_in_at = hotel_billing.as_utc(actual_ci)

    # If the user did override, compose a remarks note that includes the booked
    # vs. actual date drift so the timeline shows it for any future audit. The
    # note is stored on the HotelCheckin record (not HotelReservation, which has
    # no dedicated `check_in_id_number_remarks` field) and any free-text remark
    # the admin already typed is preserved.
    checkin_remarks: Optional[str] = (
        payload.remarks.strip() if payload.remarks else None
    )
    if mismatch and payload.confirm_date_mismatch:
        override_note = (
            f"checkin_date_mismatch: booked={mismatch['booking_date']} "
            f"actual={mismatch['actual_date']} delta_days={mismatch['delta_days']}"
        )
        checkin_remarks = (
            (checkin_remarks + " | " + override_note)
            if checkin_remarks
            else override_note
        )

    # ── Room assignment (optional — graceful skip when no rooms configured) ──
    allocated_rooms_json: Optional[str] = None
    allocated_room_numbers: list = []

    if payload.room_ids:
        rooms_needed = hr.rooms_count or 1
        if len(payload.room_ids) != rooms_needed:
            raise HTTPException(
                400,
                f"This booking requires {rooms_needed} room(s); you selected {len(payload.room_ids)}.",
            )

        rooms = (
            (
                await db.execute(
                    select(HotelRoom).where(HotelRoom.id.in_(payload.room_ids))
                )
            )
            .scalars()
            .all()
        )

        if len(rooms) != len(payload.room_ids):
            raise HTTPException(400, "One or more selected room IDs not found.")

        errors = []
        for r in rooms:
            if r.hotel_id != hr.hotel_id:
                errors.append(f"Room {r.room_number} does not belong to this hotel.")
            elif hr.room_category_id and r.room_category_id != hr.room_category_id:
                errors.append(
                    f"Room {r.room_number} is not in the booked room category."
                )
            elif r.room_status != "AVAILABLE":
                errors.append(
                    f"Room {r.room_number} is not available (status: {r.room_status})."
                )
            elif not r.is_active:
                errors.append(f"Room {r.room_number} is inactive.")
        if errors:
            raise HTTPException(400, "; ".join(errors))

        for r in rooms:
            r.room_status = "OCCUPIED"

        allocated_room_numbers = [r.room_number for r in rooms]
        allocated_rooms_json = json.dumps(allocated_room_numbers)

    # Create HotelCheckin record to store assignment
    checkin_record = HotelCheckin(
        reservation_id=hr.id,
        check_in_datetime=hr.actual_check_in_at,
        allocated_rooms=allocated_rooms_json,
        remarks=checkin_remarks,
    )
    db.add(checkin_record)

    rooms_detail = (
        f" Rooms assigned: {', '.join(allocated_room_numbers)}"
        if allocated_room_numbers
        else ""
    )
    await _log_timeline(
        db,
        booking_id,
        "HOTEL_CHECKED_IN",
        f"Guest checked in for hotel booking {hr.reservation_number or hr.id}. "
        f"ID: {hr.check_in_id_proof} #{hr.check_in_id_number}.{rooms_detail}",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_CHECKED_IN")
    return {
        "success": True,
        "hotel_status": "CHECKED_IN",
        "checked_in_at": hr.actual_check_in_at.isoformat(),
        "allocated_rooms": allocated_room_numbers,
    }


@router.post("/{booking_id}/hotel/{hotel_id}/in-house", tags=["Admin Hotel Bookings"])
async def hotel_in_house(
    booking_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Marks guest as IN_HOUSE (staying). Operational tracking step.
    Allowed from: CHECKED_IN
    """
    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status != "CHECKED_IN":
        raise HTTPException(
            400,
            f"Cannot mark in-house: status is '{hr.reservation_status}', expected CHECKED_IN.",
        )

    hr.reservation_status = "IN_HOUSE"
    await _log_timeline(
        db,
        booking_id,
        "HOTEL_IN_HOUSE",
        f"Hotel booking {hr.reservation_number or hr.id} — guest marked as in-house.",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_IN_HOUSE")
    return {"success": True, "hotel_status": "IN_HOUSE"}


@router.post("/{booking_id}/hotel/{hotel_id}/check-out", tags=["Admin Hotel Bookings"])
async def hotel_check_out(
    booking_id: int,
    hotel_id: int,
    payload: HotelCheckOutRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin records guest check-out.
    Allowed from: IN_HOUSE
    Optional additional charges (extra bed, food, damage etc.) are added to the booking.
    """
    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status != "IN_HOUSE":
        raise HTTPException(
            400,
            f"Cannot check out: status is '{hr.reservation_status}', expected IN_HOUSE.",
        )
    if payload.additional_charges < 0:
        raise HTTPException(400, "Additional charges cannot be negative.")

    # ── Free allocated rooms back to AVAILABLE ──────────────────
    checkin_rec = (
        await db.execute(
            select(HotelCheckin).where(HotelCheckin.reservation_id == hr.id)
        )
    ).scalar_one_or_none()

    freed_rooms: list = []
    if checkin_rec and checkin_rec.allocated_rooms:
        room_numbers = json.loads(checkin_rec.allocated_rooms)
        if room_numbers:
            occupied_rooms = (
                (
                    await db.execute(
                        select(HotelRoom).where(
                            HotelRoom.hotel_id == hr.hotel_id,
                            HotelRoom.room_number.in_(room_numbers),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for r in occupied_rooms:
                r.room_status = "AVAILABLE"
                freed_rooms.append(r.room_number)

    checked_out_at = payload.actual_check_out_at or datetime.now(timezone.utc)

    # Real-world guard: a guest cannot check out before they checked in. The
    # recorded check-in stamp (if present) is the floor for the check-out time.
    if hr.actual_check_in_at and checked_out_at < hr.actual_check_in_at:
        raise HTTPException(
            400,
            "Check-out time cannot be earlier than the recorded check-in time.",
        )

    # Price the stay exactly the way the check-out preview showed it to the
    # admin: room + late check-out + extras - discount, then GST on top.
    # Computed before extra_charges is mutated, since build_bill treats
    # projected_extra_charges as an addition to what is already on the row.
    bill = await hotel_billing.build_bill(
        db,
        hr,
        projected_check_out_at=checked_out_at,
        projected_extra_charges=Decimal(str(payload.additional_charges)),
    )

    hr.reservation_status = "CHECKED_OUT"
    hr.actual_check_out_at = checked_out_at
    # Persist the real-world room charge (nights actually stayed) so detail
    # pages and reports read the billed amount, not the booked snapshot.
    hr.base_amount = bill.room_charge
    hr.extra_charges = bill.extra_charges
    hr.overtime_hours = bill.overtime.overtime_hours
    hr.overtime_charge = bill.overtime.charge
    hr.taxable_amount = bill.taxable_amount
    hr.gst_percent = bill.gst_percent
    hr.gst_amount = bill.gst_amount
    hr.is_tax_invoice = bill.is_tax_invoice
    hr.total_amount = bill.grand_total
    # Keep the stored night counts in step with the recomputed room charge so
    # the detail pages and per-room-night commission match the real stay.
    hr.nights = bill.actual_nights
    hr.room_nights = bill.actual_nights * (hr.rooms_count or 1)
    if payload.check_out_notes:
        hr.check_out_notes = payload.check_out_notes.strip()

    # Recalculate payment status — check-out (overtime / extra charges) may
    # reopen a previously-paid bill, and the collect-payment step gates on it.
    if bill.balance_due <= Decimal("0"):
        hr.payment_collected_status = "PAID"
    elif bill.advance_paid > Decimal("0"):
        hr.payment_collected_status = "PARTIAL"
    else:
        hr.payment_collected_status = "PENDING"

    detail = f"Additional charges: ₹{payload.additional_charges}"
    if bill.overtime.charge > Decimal("0"):
        detail += f". Late check-out: ₹{bill.overtime.charge} ({bill.overtime.reason})"
    if payload.check_out_notes:
        detail += f". Notes: {payload.check_out_notes}"
    if freed_rooms:
        detail += f". Rooms freed: {', '.join(freed_rooms)}"
    await _log_timeline(
        db,
        booking_id,
        "HOTEL_CHECKED_OUT",
        f"Guest checked out from hotel booking {hr.reservation_number or hr.id}. {detail}",
    )
    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        action="HOTEL_CHECKED_OUT",
        payment_collected_status=hr.payment_collected_status,
    )
    return {
        "success": True,
        "hotel_status": "CHECKED_OUT",
        "final_amount": float(hr.total_amount),
        "checked_out_at": hr.actual_check_out_at.isoformat(),
        "overtime_charge": float(bill.overtime.charge),
        "gst_amount": float(bill.gst_amount),
        "balance_due": float(bill.balance_due),
        "freed_rooms": freed_rooms,
    }


@router.post("/{booking_id}/hotel/{hotel_id}/complete", tags=["Admin Hotel Bookings"])
async def hotel_complete(
    booking_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Mark hotel booking as COMPLETED (triggers settlement preparation).
    Allowed from: CHECKED_OUT
    """
    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status != "CHECKED_OUT":
        raise HTTPException(
            400,
            f"Cannot complete: status is '{hr.reservation_status}', expected CHECKED_OUT.",
        )

    hr.reservation_status = "COMPLETED"
    # Re-roll totals/collections onto the master. Complete runs after check-out;
    # if no advance was recorded between checkout and complete, the master could
    # still be stale. The check-out sync may also have been skipped if the
    # partner drove the lifecycle (see rollup_hotel_totals_into_master docstring).
    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await _log_timeline(
        db,
        booking_id,
        "HOTEL_COMPLETED",
        f"Hotel booking {hr.reservation_number or hr.id} marked COMPLETED. Settlement may now be prepared.",
    )
    await db.commit()
    # ── Email: stay completed to the customer ──
    try:
        hotel_name = await db.scalar(
            select(Hotel.hotel_name).where(Hotel.id == hr.hotel_id)
        )
        await _send_booking_email(
            db,
            event_type="booking_completed",
            customer_id=mb.customer_id,
            context={
                "service": "Hotel",
                "booking_number": hr.reservation_number or str(hr.id),
                "message": (
                    "Your stay is complete — thank you for choosing WayTero. Your tax "
                    "invoice is available in your bookings."
                ),
                "details": [
                    ("Hotel", hotel_name or ""),
                    (
                        "Check-in",
                        (
                            hr.check_in_date.strftime("%d %b %Y")
                            if hr.check_in_date
                            else ""
                        ),
                    ),
                    (
                        "Check-out",
                        (
                            hr.check_out_date.strftime("%d %b %Y")
                            if hr.check_out_date
                            else ""
                        ),
                    ),
                ],
                **(
                    {"amount": float(hr.total_amount), "amount_label": "Stay amount"}
                    if hr.total_amount
                    else {}
                ),
            },
            related_type="HOTEL_RESERVATION",
            related_id=hr.id,
        )
    except Exception:  # pragma: no cover — email must never break completion
        pass
    await _notify_hotel_update(db, hr, action="HOTEL_COMPLETED")
    return {"success": True, "hotel_status": "COMPLETED"}


@router.post("/{booking_id}/hotel/{hotel_id}/settle", tags=["Admin Hotel Bookings"])
async def hotel_settle(
    booking_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Settle a hotel booking: freeze commission, move the partner wallet, record
    TDS/coupon, and mark SETTLED. Allowed from COMPLETED with the invoice
    generated and the balance fully collected.

    Delegates to the shared settlement engine so this button and the Settlements
    page take the identical financial path (net position, TDS for COMPANY
    partners, coupon disbursement). See settlement_api._settle_hotel_reservation.
    """
    from app.modules.admin.settlement_api import _settle_hotel_reservation

    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)
    hotel = (
        await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
    ).scalar_one_or_none()
    if not hotel:
        raise HTTPException(404, "Hotel not found for this reservation.")

    result = await _settle_hotel_reservation(db, mb, hr, hotel)
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_SETTLED")
    return {
        "success": True,
        "hotel_status": "SETTLED",
        "booking_status": mb.booking_status,
        "partner_wallet_balance": result.get("partner_wallet_balance"),
        "platform_commission": result.get("platform_commission"),
        "partner_payout": result.get("partner_payout"),
        "tds_deducted": result.get("tds_deducted"),
        "coupon_disbursement_id": result.get("coupon_disbursement_id"),
        "settlement_note": result.get("settlement_note"),
        "position": result.get("position"),
    }


@router.post("/{booking_id}/hotel/{hotel_id}/no-show", tags=["Admin Hotel Bookings"])
async def hotel_no_show(
    booking_id: int,
    hotel_id: int,
    payload: HotelNoShowRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Mark hotel booking as NO_SHOW — guest did not arrive.
    Allowed from: CONFIRMED or IN_HOUSE
    Refund amount governed by hotel cancellation policy.
    """
    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status not in {"CONFIRMED", "CHECKED_IN", "IN_HOUSE"}:
        raise HTTPException(
            400, f"Cannot mark no-show: status is '{hr.reservation_status}'."
        )

    hr.reservation_status = "NO_SHOW"
    hr.refund_amount = Decimal(str(payload.refund_amount))

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_NO_SHOW",
        f"Hotel booking {hr.reservation_number or hr.id} — guest no-show. Refund: ₹{payload.refund_amount}",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_NO_SHOW")
    return {"success": True, "hotel_status": "NO_SHOW"}


@router.post("/{booking_id}/hotel/{hotel_id}/cancel", tags=["Admin Hotel Bookings"])
async def hotel_cancel(
    booking_id: int,
    hotel_id: int,
    payload: HotelCancelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Admin cancels a hotel booking.
    Blocked once guest is CHECKED_IN / IN_HOUSE / CHECKED_OUT / COMPLETED / SETTLED.

    Migration 0043: delegates to the cancellation policy engine so the
    charge/refund and the policy_snapshot are persisted, advances are
    auto-refunded to the customer wallet, and notifications + WS fire.
    """
    from uuid import UUID as _UUID
    from app.modules.booking.services.cancellation_policy import (
        apply_hotel_cancellation,
        CANCEL_SOURCE_ADMIN,
    )

    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    blocked = {
        "CHECKED_IN",
        "IN_HOUSE",
        "CHECKED_OUT",
        "COMPLETED",
        "SETTLED",
        "CANCELLED",
    }
    if hr.reservation_status in blocked:
        raise HTTPException(
            400, f"Cannot cancel hotel booking: status is '{hr.reservation_status}'."
        )
    if not payload.reason.strip():
        raise HTTPException(400, "Cancellation reason is required.")

    result = await apply_hotel_cancellation(
        db,
        hotel_reservation_id=hotel_id,
        master_booking_id=booking_id,
        cancellation_reason=payload.reason.strip(),
        cancelled_by_user_id=_UUID(current_user["sub"]),
        cancelled_by_role=current_user.get("role", "ADMIN"),
        cancelled_source=CANCEL_SOURCE_ADMIN,
    )
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_CANCELLED")
    return {
        "success": True,
        "hotel_status": "CANCELLED",
        "cancellation_charge": float(result["charge"]),
        "refund_amount": float(result["refund_amount"]),
        "tier_label": result["tier_label"],
    }


@router.post(
    "/{booking_id}/hotel/{hotel_id}/add-charges", tags=["Admin Hotel Bookings"]
)
async def hotel_add_charges(
    booking_id: int,
    hotel_id: int,
    payload: HotelAddChargesRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Add supplementary charges (extra bed, food, laundry, damage etc.) to a hotel booking.
    Allowed when guest is CHECKED_IN, IN_HOUSE, or CHECKED_OUT.
    Recomputes final_amount.
    """
    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    allowed = {"CHECKED_IN", "IN_HOUSE", "CHECKED_OUT"}
    if hr.reservation_status not in allowed:
        raise HTTPException(
            400,
            f"Cannot add charges: status is '{hr.reservation_status}'. "
            "Charges can only be added when guest is CHECKED_IN, IN_HOUSE, or CHECKED_OUT.",
        )
    if payload.amount <= 0:
        raise HTTPException(400, "Charge amount must be greater than 0.")
    if hr.invoice_number:
        raise HTTPException(
            400,
            f"Invoice {hr.invoice_number} has already been generated. "
            "Charges cannot be added to an invoiced stay.",
        )

    # Re-price through the billing service so the new charge is taxed at the
    # same slab as the stay, rather than being appended tax-free to the total.
    bill = await hotel_billing.build_bill(
        db,
        hr,
        projected_extra_charges=Decimal(str(payload.amount)),
    )
    hr.extra_charges = bill.extra_charges
    hr.taxable_amount = bill.taxable_amount
    hr.gst_percent = bill.gst_percent
    hr.gst_amount = bill.gst_amount
    hr.is_tax_invoice = bill.is_tax_invoice
    hr.total_amount = bill.grand_total

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_CHARGES_ADDED",
        f"Additional charge of ₹{payload.amount} added to {hr.reservation_number or hr.id}. "
        f"Reason: {payload.description}. New total: ₹{hr.total_amount}",
    )
    await db.commit()

    # Re-price mutated hr.total_amount — re-sync the master so its totals,
    # payment_status and the BookingService.service_amount line stay in step
    # with the reservation. Without this, the master drifts until the next
    # invoice generation. The helper is a no-op for non-HOTEL masters.
    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_CHARGES_ADDED")

    return {
        "success": True,
        "additional_charges": float(hr.extra_charges),
        "gst_amount": float(hr.gst_amount),
        "final_amount": float(hr.total_amount),
        "balance_due": float(bill.balance_due),
    }


# ════════════════════════════════════════════════════════════════
# HOTEL BILLING — advances, check-out preview, invoice, collection
# Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
#          BRD Part 4 §57-92 — Hotel Booking Lifecycle
# Migration: 0035_hotel_payments
# ════════════════════════════════════════════════════════════════

# Advances may be taken from confirmation up to the moment the bill is closed.
_ADVANCE_ALLOWED = {
    "PENDING_PAYMENT",
    "AWAITING_HOTEL_CONFIRMATION",
    "CONFIRMED",
    "CHECKED_IN",
    "IN_HOUSE",
    "CHECKED_OUT",
}


@router.get(
    "/{booking_id}/hotel/{hotel_id}/checkout-preview", tags=["Admin Hotel Bookings"]
)
async def hotel_checkout_preview(
    booking_id: int,
    hotel_id: int,
    additional_charges: float = 0.0,
    actual_check_out_at: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Dry-run the full bill without mutating anything.

    Powers the check-out modal: room tariff, late-check-out overtime, extras,
    discount, GST (only when the platform switch is on) and every advance
    already received, ending in the balance the desk must collect.

    Safe to call repeatedly as the admin edits charges or the check-out time.
    """
    await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    bill = await hotel_billing.build_bill(
        db,
        hr,
        projected_check_out_at=actual_check_out_at or datetime.now(timezone.utc),
        projected_extra_charges=Decimal(str(additional_charges or 0)),
    )
    return {
        "success": True,
        "hotel_status": hr.reservation_status,
        "booking_number": hr.reservation_number or f"HR-{hr.id}",
        "nights": hr.nights,
        "rooms": hr.rooms_count,
        "check_in_date": hr.check_in_date.isoformat() if hr.check_in_date else None,
        "check_out_date": hr.check_out_date.isoformat() if hr.check_out_date else None,
        "actual_check_in_at": (
            hr.actual_check_in_at.isoformat() if hr.actual_check_in_at else None
        ),
        "bill": bill.as_dict(),
    }


@router.post(
    "/{booking_id}/hotel/{hotel_id}/record-advance", tags=["Admin Hotel Bookings"]
)
async def hotel_record_advance(
    booking_id: int,
    hotel_id: int,
    payload: HotelRecordAdvanceRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Record an advance received from the customer.

    A stay may take several advances (deposit at booking, top-ups during the
    stay), so unlike the cab side there is no single-active constraint.

    received_by is custody, not bookkeeping colour: ONLINE and WALLET always
    settle into the platform account, so they are forced to ADMIN. CASH and UPI
    can genuinely be taken at the property by the partner.
    """
    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status not in _ADVANCE_ALLOWED:
        raise HTTPException(
            400,
            f"Cannot record an advance: status is '{hr.reservation_status}'.",
        )

    mode = payload.payment_mode.strip().upper()
    if mode not in {"UPI", "ONLINE", "CASH", "WALLET"}:
        raise HTTPException(400, f"Unsupported payment mode '{payload.payment_mode}'.")

    received_by = payload.received_by.strip().upper()
    if received_by not in {"ADMIN", "PARTNER"}:
        raise HTTPException(400, "received_by must be ADMIN or PARTNER.")
    if mode in {"ONLINE", "WALLET"}:
        # These land in the platform account by definition.
        received_by = "ADMIN"

    # Never let advances exceed the bill — that is a refund, not an advance.
    # Gate on the live balance rather than payment_collected_status: charges
    # added after a stay was fully prepaid reopen a genuine balance.
    bill = await hotel_billing.build_bill(db, hr)
    amount = Decimal(str(payload.amount))
    if bill.balance_due <= Decimal("0"):
        raise HTTPException(
            400, "Nothing outstanding — this stay is already paid in full."
        )
    if amount > bill.balance_due:
        raise HTTPException(
            400,
            f"Advance of Rs.{amount} exceeds the outstanding balance of Rs.{bill.balance_due}.",
        )

    # ── WALLET: debit the customer's prepaid wallet under a row lock ──────────
    if mode == "WALLET":
        if not mb.customer_id:
            raise HTTPException(400, "No customer on this booking — cannot use wallet.")
        wallet_row = (
            (
                await db.execute(
                    text(
                        "SELECT id, available_balance, wallet_status "
                        "FROM customer_wallets WHERE customer_id = :cid FOR UPDATE"
                    ),
                    {"cid": mb.customer_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not wallet_row:
            raise HTTPException(
                400, "Customer does not have a wallet. Cannot process wallet payment."
            )
        if wallet_row["wallet_status"] != "ACTIVE":
            raise HTTPException(
                400,
                f"Customer wallet is '{wallet_row['wallet_status']}' — cannot process payment.",
            )
        avail = Decimal(str(wallet_row["available_balance"] or 0))
        if avail < amount:
            raise HTTPException(
                400,
                f"Insufficient wallet balance. Required: Rs.{amount} | "
                f"Available: Rs.{avail} | Shortfall: Rs.{amount - avail}. "
                "Customer must recharge their wallet before payment.",
            )
        _new_balance = (avail - amount).quantize(Decimal("0.01"))
        await db.execute(
            text(
                "UPDATE customer_wallets SET available_balance = :bal, updated_at = NOW() "
                "WHERE id = :wid"
            ),
            {"bal": _new_balance, "wid": wallet_row["id"]},
        )
        await db.execute(
            text(
                """
                INSERT INTO customer_wallet_ledger
                    (customer_wallet_id, transaction_reference, reference_type,
                     debit_amount, credit_amount, balance_after, narration, created_at)
                VALUES (:wid, :ref, 'HOTEL_ADVANCE', :debit, 0, :bal_after, :narration, NOW())
            """
            ),
            {
                "wid": wallet_row["id"],
                "ref": hr.reservation_number or f"HR-{hr.id}",
                "debit": amount,
                "bal_after": _new_balance,
                "narration": (
                    f"Hotel advance — {hr.reservation_number or hr.id} | Deducted Rs.{amount}"
                ),
            },
        )

    receipt = await hotel_billing.next_receipt_number(db)
    adv = HotelAdvancePayment(
        hotel_reservation_id=hr.id,
        master_booking_id=booking_id,
        receipt_number=receipt,
        amount=amount,
        payment_mode=mode,
        received_by=received_by,
        reference_number=(payload.reference_number or "").strip() or None,
        notes=(payload.notes or "").strip() or None,
        status="ACTIVE",
    )
    db.add(adv)
    await db.flush()

    refreshed = await hotel_billing.build_bill(db, hr)
    if refreshed.balance_due <= Decimal("0"):
        hr.payment_collected_status = "PAID"
    elif refreshed.advance_paid > Decimal("0"):
        hr.payment_collected_status = "PARTIAL"

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_ADVANCE_RECEIVED",
        f"Advance of Rs.{amount} received via {mode} (held by {received_by}) "
        f"for {hr.reservation_number or hr.id}. Receipt {receipt}. "
        f"Balance now Rs.{refreshed.balance_due}.",
    )
    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        action="HOTEL_ADVANCE_RECEIVED",
        payment_collected_status=hr.payment_collected_status,
    )


# ════════════════════════════════════════════════════════════════
# PER-MASTER SYNC — POST /admin/bookings/{booking_id}/sync-hotel-payment
# Re-roll hotel payment fields for a single master booking. Useful when
# the operator knows which booking is stale and wants to fix it without
# scanning the entire HOTEL-masters table.
#
# Doc Ref: BRD Part 3 §45 (advance collection & settlement custody).
# ════════════════════════════════════════════════════════════════


@router.post("/{booking_id}/sync-hotel-payment", tags=["Admin Bookings"])
async def sync_master_hotel_payment(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    """Re-run ``rollup_hotel_totals_into_master`` for a single master booking and
    return the before/after values so the operator can see what changed.
    No-op when the master has no HOTEL service.
    """
    mb = await _get_master_booking(db, booking_id)

    before_total = float(mb.total_amount or 0)
    before_paid = float(mb.total_paid_amount or 0)
    before_status = mb.payment_status

    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await db.commit()

    after_total = float(mb.total_amount or 0)
    after_paid = float(mb.total_paid_amount or 0)
    after_status = mb.payment_status

    changed = (
        before_total != after_total
        or before_paid != after_paid
        or before_status != after_status
    )
    return {
        "success": True,
        "message": (
            f"Hotel payment sync {'updated' if changed else 'no-op for'} "
            f"master booking {mb.booking_number}."
        ),
        "master_booking_id": booking_id,
        "booking_number": mb.booking_number,
        "changed": changed,
        "before": {
            "total_amount": before_total,
            "total_paid_amount": before_paid,
            "payment_status": before_status,
        },
        "after": {
            "total_amount": after_total,
            "total_paid_amount": after_paid,
            "payment_status": after_status,
        },
    }


# ════════════════════════════════════════════════════════════════
# BACKFILL — POST /admin/bookings/backfill-hotel-payment-sync
# One-shot re-roll for legacy master_bookings rows whose payment_status /
# total_paid_amount drifted out of sync with their hotel_reservations rows.
# Idempotent: re-runs the same rollup_hotel_totals_into_master calculation, so healthy
# rows produce unchanged values.
#
# Doc Ref: BRD Part 3 §45 (advance collection & settlement custody) +
#          Part 7 §155 (operational backfill endpoints).
# ════════════════════════════════════════════════════════════════


@router.post("/backfill-hotel-payment-sync", tags=["Admin Bookings"])
async def backfill_hotel_payment_sync(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    """Walk every master booking whose service list contains HOTEL and re-run
    ``rollup_hotel_totals_into_master``. Returns counts plus the per-master before /
    after totals so the run is auditable. Restricted to admin / super-admin.
    """
    # Only master bookings that have at least one HOTEL BookingService row.
    master_ids = (
        (
            await db.execute(
                select(BookingService.master_booking_id)
                .where(BookingService.service_type == "HOTEL")
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    masters_synced = 0
    masters_skipped = 0
    details: list[dict] = []
    for mb_id in master_ids:
        mb = (
            await db.execute(select(MasterBooking).where(MasterBooking.id == mb_id))
        ).scalar_one_or_none()
        if mb is None:
            masters_skipped += 1
            continue

        before_total = float(mb.total_amount or 0)
        before_paid = float(mb.total_paid_amount or 0)
        before_status = mb.payment_status

        # The helper re-rolls hotel reservation totals and active advances onto
        # this master and updates payment_status. Early-returns when the master
        # has no HOTEL service — but we already filtered to HOTEL masters above,
        # so the helper will run.
        await rollup_hotel_totals_into_master(db, mb.id, mb)
        # Flush so per-iteration SELECTs don't see stale identity-map values.
        await db.flush()

        after_total = float(mb.total_amount or 0)
        after_paid = float(mb.total_paid_amount or 0)
        after_status = mb.payment_status

        changed = (
            before_total != after_total
            or before_paid != after_paid
            or before_status != after_status
        )
        if changed:
            masters_synced += 1
            await _log_timeline(
                db,
                mb.id,
                "PAYMENT_SYNC_BACKFILL",
                f"Re-rolled hotel payment sync. "
                f"total {before_total:.2f}→{after_total:.2f}, "
                f"paid {before_paid:.2f}→{after_paid:.2f}, "
                f"status {before_status}→{after_status}.",
            )
        else:
            masters_skipped += 1

        details.append(
            {
                "master_booking_id": mb.id,
                "booking_number": mb.booking_number,
                "changed": changed,
                "before": {
                    "total_amount": before_total,
                    "total_paid_amount": before_paid,
                    "payment_status": before_status,
                },
                "after": {
                    "total_amount": after_total,
                    "total_paid_amount": after_paid,
                    "payment_status": after_status,
                },
            }
        )

    await db.commit()

    return {
        "success": True,
        "message": (
            f"Hotel payment sync backfill complete. "
            f"{masters_synced} updated, {masters_skipped} unchanged."
        ),
        "masters_examined": len(master_ids),
        "masters_synced": masters_synced,
        "masters_skipped": masters_skipped,
        "details": details,
    }


@router.post(
    "/{booking_id}/hotel/{hotel_id}/generate-invoice", tags=["Admin Hotel Bookings"]
)
async def hotel_generate_invoice(
    booking_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Freeze the bill into an invoice once the guest has checked out.

    Writes the computed GST back onto the reservation so the invoice stays
    reproducible even if the tariff or tax config changes later (BRD Rule 25).
    Re-calling returns the existing invoice rather than issuing a duplicate
    number.
    """
    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status not in {"CHECKED_OUT", "COMPLETED"}:
        raise HTTPException(
            400,
            f"Cannot invoice: status is '{hr.reservation_status}', expected CHECKED_OUT.",
        )

    bill = await hotel_billing.build_bill(db, hr)

    if hr.invoice_number:
        return {
            "success": True,
            "already_generated": True,
            "invoice_number": hr.invoice_number,
            "invoice_url": hr.invoice_url,
            "bill": bill.as_dict(),
        }

    hr.invoice_number = await hotel_billing.next_invoice_number(db)
    hr.invoice_generated_at = datetime.now(timezone.utc)
    hr.taxable_amount = bill.taxable_amount
    hr.gst_percent = bill.gst_percent
    hr.gst_amount = bill.gst_amount
    hr.is_tax_invoice = bill.is_tax_invoice
    hr.total_amount = bill.grand_total

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_INVOICE_GENERATED",
        f"Invoice {hr.invoice_number} generated for {hr.reservation_number or hr.id}. "
        f"Total Rs.{bill.grand_total} (GST Rs.{bill.gst_amount}). "
        f"Balance to collect Rs.{bill.balance_due}.",
    )
    await db.commit()

    # hr.total_amount was rewritten from the bill — re-sync the master so its
    # total_amount / total_paid_amount / payment_status and the BookingService
    # service_amount line stay in step. Without this, the master drifts from
    # the reservation until the next collection event. The helper is a no-op
    # for non-HOTEL masters.
    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await db.commit()

    await _notify_hotel_update(db, hr, action="HOTEL_INVOICE_GENERATED")

    # ── Email: invoice issued to the customer ──
    hotel_name = await db.scalar(
        select(Hotel.hotel_name).where(Hotel.id == hr.hotel_id)
    )
    await _send_booking_email(
        db,
        event_type="invoice_issued",
        customer_id=mb.customer_id,
        context={
            "service": "Hotel",
            "booking_number": hr.reservation_number or str(hr.id),
            "message": "Your tax invoice is ready to download from your bookings.",
            "details": [
                ("Hotel", hotel_name or ""),
                ("Invoice no.", hr.invoice_number or ""),
            ],
            **(
                {"amount": float(hr.total_amount), "amount_label": "Stay amount"}
                if hr.total_amount
                else {}
            ),
        },
        related_type="HOTEL_RESERVATION",
        related_id=hr.id,
    )

    return {
        "success": True,
        "already_generated": False,
        "invoice_number": hr.invoice_number,
        "invoice_url": hr.invoice_url,
        "bill": bill.as_dict(),
    }


@router.post(
    "/{booking_id}/hotel/{hotel_id}/collect-payment", tags=["Admin Hotel Bookings"]
)
async def hotel_collect_payment(
    booking_id: int,
    hotel_id: int,
    payload: HotelCollectPaymentRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Record the final balance collected against the invoice.

    Custody, again, is the point. ONLINE and WALLET are platform rails — the money
    reaches WayTero directly, so collected_by is forced to ADMIN. CASH and UPI can
    be taken at the property, in which case the partner is holding platform money
    and the settlement step nets it back. WALLET additionally debits the customer's
    prepaid wallet (balance guard enforced). Recorded as a payment row in the same
    table as advances so the ledger for this stay stays in one place.
    """
    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    if hr.reservation_status not in {"CHECKED_OUT", "COMPLETED"}:
        raise HTTPException(
            400,
            f"Cannot collect payment: status is '{hr.reservation_status}', expected CHECKED_OUT.",
        )
    if not hr.invoice_number:
        raise HTTPException(400, "Generate the invoice before collecting payment.")
    if hr.payment_collected_status == "PAID":
        raise HTTPException(400, "This invoice is already paid in full.")

    mode = payload.payment_mode.strip().upper()
    if mode not in {"ONLINE", "UPI", "CASH", "WALLET"}:
        raise HTTPException(400, f"Unsupported payment mode '{payload.payment_mode}'.")

    collected_by = payload.collected_by.strip().upper()
    if collected_by not in {"ADMIN", "PARTNER"}:
        raise HTTPException(400, "collected_by must be ADMIN or PARTNER.")
    if mode in {"ONLINE", "WALLET"}:
        # These settle into the platform account by definition.
        collected_by = "ADMIN"

    bill = await hotel_billing.build_bill(db, hr)
    amount = Decimal(str(payload.amount))
    if amount > bill.balance_due:
        raise HTTPException(
            400,
            f"Rs.{amount} exceeds the outstanding balance of Rs.{bill.balance_due}.",
        )

    # Heal the stored bill to the recomputed real-world stay (nights actually
    # stayed), so the list view / reports read the same amounts this payment is
    # validated against. Legacy rows checked out before the recompute feature
    # stored the booked amount; this keeps stored totals in sync going forward.
    if hr.total_amount != bill.grand_total:
        hr.base_amount = bill.room_charge
        hr.extra_charges = bill.extra_charges
        hr.overtime_hours = bill.overtime.overtime_hours
        hr.overtime_charge = bill.overtime.charge
        hr.taxable_amount = bill.taxable_amount
        hr.gst_percent = bill.gst_percent
        hr.gst_amount = bill.gst_amount
        hr.is_tax_invoice = bill.is_tax_invoice
        hr.total_amount = bill.grand_total

    # ── WALLET: debit the customer's prepaid wallet under a row lock ──────────
    # The money is only "collected" if the wallet can actually cover it, so the
    # guard runs before the payment row is written. Deduct exactly `amount`
    # (which the admin can set below the full balance for a part-payment).
    if mode == "WALLET":
        if not mb.customer_id:
            raise HTTPException(400, "No customer on this booking — cannot use wallet.")
        wallet_row = (
            (
                await db.execute(
                    text(
                        "SELECT id, available_balance, wallet_status "
                        "FROM customer_wallets WHERE customer_id = :cid FOR UPDATE"
                    ),
                    {"cid": mb.customer_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not wallet_row:
            raise HTTPException(
                400, "Customer does not have a wallet. Cannot process wallet payment."
            )
        if wallet_row["wallet_status"] != "ACTIVE":
            raise HTTPException(
                400,
                f"Customer wallet is '{wallet_row['wallet_status']}' — cannot process payment.",
            )
        avail = Decimal(str(wallet_row["available_balance"] or 0))
        if avail < amount:
            raise HTTPException(
                400,
                f"Insufficient wallet balance. Required: Rs.{amount} | "
                f"Available: Rs.{avail} | Shortfall: Rs.{amount - avail}. "
                "Customer must recharge their wallet before payment.",
            )
        new_balance = (avail - amount).quantize(Decimal("0.01"))
        await db.execute(
            text(
                "UPDATE customer_wallets SET available_balance = :bal, updated_at = NOW() "
                "WHERE id = :wid"
            ),
            {"bal": new_balance, "wid": wallet_row["id"]},
        )
        await db.execute(
            text(
                """
                INSERT INTO customer_wallet_ledger
                    (customer_wallet_id, transaction_reference, reference_type,
                     debit_amount, credit_amount, balance_after, narration, created_at)
                VALUES (:wid, :ref, 'HOTEL_PAYMENT', :debit, 0, :bal_after, :narration, NOW())
            """
            ),
            {
                "wid": wallet_row["id"],
                "ref": hr.invoice_number,
                "debit": amount,
                "bal_after": new_balance,
                "narration": (
                    f"Hotel stay payment — {hr.reservation_number or hr.id} | "
                    f"Invoice {hr.invoice_number} | Deducted Rs.{amount}"
                ),
            },
        )

    receipt = await hotel_billing.next_receipt_number(db)
    db.add(
        HotelAdvancePayment(
            hotel_reservation_id=hr.id,
            master_booking_id=booking_id,
            receipt_number=receipt,
            amount=amount,
            payment_mode=mode,
            received_by=collected_by,
            reference_number=(payload.reference_number or "").strip() or None,
            notes=f"Final payment against invoice {hr.invoice_number}",
            status="ACTIVE",
        )
    )
    await db.flush()

    refreshed = await hotel_billing.build_bill(db, hr)
    hr.payment_mode = mode
    hr.payment_collected_by = collected_by
    hr.payment_reference = (payload.reference_number or "").strip() or None
    hr.payment_collected_at = datetime.now(timezone.utc)
    hr.payment_collected_status = (
        "PAID" if refreshed.balance_due <= Decimal("0") else "PARTIAL"
    )

    await _log_timeline(
        db,
        booking_id,
        "HOTEL_PAYMENT_COLLECTED",
        f"Rs.{amount} collected via {mode} by {collected_by} against invoice "
        f"{hr.invoice_number}. Receipt {receipt}. "
        f"Balance now Rs.{refreshed.balance_due}.",
    )
    await rollup_hotel_totals_into_master(db, booking_id, mb)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        action="HOTEL_PAYMENT_COLLECTED",
        payment_collected_status=hr.payment_collected_status,
    )

    return {
        "success": True,
        "receipt_number": receipt,
        "amount": float(amount),
        "payment_mode": mode,
        "collected_by": collected_by,
        "balance_due": float(refreshed.balance_due),
        "payment_collected_status": hr.payment_collected_status,
        "fully_paid": refreshed.balance_due <= Decimal("0"),
    }


@router.get(
    "/{booking_id}/hotel/{hotel_id}/download-invoice", tags=["Admin Hotel Bookings"]
)
async def hotel_download_invoice_pdf(
    booking_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate and stream a premium PDF invoice for a completed + paid hotel stay.

    Flow:
      1. Validate: booking must be CHECKED_OUT/COMPLETED and invoice generated.
      2. Load platform branding from system_configurations.
      3. Build PDF in-memory via ReportLab (generate_hotel_invoice_pdf).
      4. Stream as application/pdf with Content-Disposition: attachment.
    """
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.invoice_pdf_service import generate_hotel_invoice_pdf

    mb = await _get_master_booking(db, booking_id)
    hr = await _get_hotel_booking(db, booking_id, hotel_id)

    # Guard: must be checked out and invoice generated
    if hr.reservation_status not in ("CHECKED_OUT", "COMPLETED", "SETTLED"):
        raise HTTPException(
            400,
            f"Cannot generate invoice: booking is '{hr.reservation_status}'. "
            "Stay must be CHECKED_OUT before downloading invoice.",
        )
    if not hr.invoice_number:
        raise HTTPException(
            400, "Invoice not yet generated. Generate the invoice before downloading."
        )

    # ── Load platform config from system_configurations ────────────────────
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
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    cfg = {row.config_key: (row.config_value or "") for row in config_rows}

    # ── Load customer ───────────────────────────────────────────────────────
    cust = (
        await db.execute(select(Customer).where(Customer.id == mb.customer_id))
    ).scalar_one_or_none()
    cust_user = None
    if cust:
        cust_user = (
            await db.execute(select(User).where(User.id == cust.user_id))
        ).scalar_one_or_none()

    # ── Load hotel + room category ──────────────────────────────────────────
    hotel_name = hotel_address = None
    if hr.hotel_id:
        h = (
            await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
        ).scalar_one_or_none()
        if h:
            hotel_name = h.hotel_name
            hotel_address = h.address

    room_category_name = room_type = None
    if hr.room_category_id:
        rc = (
            await db.execute(
                select(HotelRoomCategory).where(
                    HotelRoomCategory.id == hr.room_category_id
                )
            )
        ).scalar_one_or_none()
        if rc:
            room_category_name = rc.category_name
            room_type = rc.room_type

    # ── Rebuild the bill for amounts ────────────────────────────────────────
    bill = await hotel_billing.build_bill(db, hr)

    # ── Generate PDF ────────────────────────────────────────────────────────
    pdf_bytes = generate_hotel_invoice_pdf(
        invoice_number=hr.invoice_number,
        hotel_booking_number=hr.reservation_number or f"HR-{hr.id}",
        booking_number=mb.booking_number,
        invoice_date=hr.invoice_generated_at,
        customer_name=cust.full_name if cust else None,
        customer_mobile=cust_user.mobile_number if cust_user else None,
        hotel_name=hotel_name,
        hotel_address=hotel_address,
        room_category_name=room_category_name,
        room_type=room_type,
        meal_plan=None,
        check_in_date=hr.check_in_date.isoformat() if hr.check_in_date else None,
        check_out_date=hr.check_out_date.isoformat() if hr.check_out_date else None,
        actual_check_in_at=hr.actual_check_in_at,
        actual_check_out_at=hr.actual_check_out_at,
        # Real nights stayed — differs from the booked window when the guest
        # checked in/out on different dates than reserved.
        num_nights=bill.actual_nights,
        num_rooms=hr.rooms_count,
        num_guests=hr.adults_count,
        room_charge=float(bill.room_charge),
        overtime_charge=float(bill.overtime.charge),
        extra_charges=float(bill.extra_charges),
        discount_amount=float(bill.discount),
        coupon_code=hr.coupon_code,
        taxable_amount=float(bill.taxable_amount),
        gst_percent=float(bill.gst_percent),
        gst_amount=float(bill.gst_amount),
        is_tax_invoice=bill.is_tax_invoice,
        grand_total=float(bill.grand_total),
        advance_paid=float(bill.advance_paid),
        payment_mode=hr.payment_mode,
        payment_collected_by=hr.payment_collected_by,
        occupancy=bill.occupancy,
        platform_name=cfg.get("PLATFORM_NAME", "WayTero"),
        platform_logo_url=cfg.get("PLATFORM_LOGO_URL", ""),
        business_legal_name=cfg.get("BUSINESS_LEGAL_NAME", ""),
        business_gst_number=cfg.get("BUSINESS_GST_NUMBER", ""),
        business_registered_address=cfg.get("BUSINESS_REGISTERED_ADDRESS", ""),
        support_email=cfg.get("SUPPORT_EMAIL", ""),
        support_phone=cfg.get("SUPPORT_PHONE", ""),
    )

    filename = f"{hr.invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ════════════════════════════════════════════════════════════════
# Hotel switch / mid-stay split endpoints
# Doc Ref: Hotel Switch Spec; Migration: 0042_hotel_switch
#
# The four endpoints below drive the two flows in services/switch.py:
#   - quote  →  returns the money moves + inventory check before the admin
#               commits.
#   - switch →  pre-checkin whole-reservation switch (status ∈ PENDING_PAYMENT /
#               AWAITING_HOTEL_CONFIRMATION / CONFIRMED). Branches to the
#               post-checkin split if status ∈ {CHECKED_IN, IN_HOUSE}.
#   - advance-refund →  two-step Razorpay flow: this endpoint records the
#                       gateway reference on the original advance and flips
#                       status to REFUNDED when fully refunded.
#   - switch-events →  paginated audit page for the new /admin/bookings/hotel/
#                       switches surface.
# ════════════════════════════════════════════════════════════════


class _HotelSwitchTargetPayload(BaseModel):
    target_hotel_id: int
    target_room_category_id: int
    target_check_in: date
    target_check_out: date
    adults_count: int = 1
    children_count: int = 0
    extra_beds: int = 0


class _HotelSwitchCommitPayload(_HotelSwitchTargetPayload):
    strategy: str  # ROLLOVER | NONE
    reason: str
    notes: Optional[str] = None


class _HotelAdvanceRefundPayload(BaseModel):
    advance_payment_id: int
    refund_amount: float
    gateway_reference: Optional[str] = None
    notes: Optional[str] = None


async def _load_hotel_reservation(
    db: AsyncSession, reservation_id: int
) -> HotelReservation:
    """Thin loader used by the switch endpoints. Returns the ORM row, 404 if
    missing — no lock; the switch service itself takes SELECT FOR UPDATE on
    the rows it mutates."""
    hr = (
        await db.execute(
            select(HotelReservation).where(HotelReservation.id == reservation_id)
        )
    ).scalar_one_or_none()
    if hr is None:
        raise HTTPException(
            status_code=404, detail=f"Hotel reservation {reservation_id} not found"
        )
    return hr


@router.post(
    "/hotel/{reservation_id}/switch/quote",
    tags=["Admin Hotel Switches"],
)
async def hotel_switch_quote(
    reservation_id: int,
    payload: _HotelSwitchTargetPayload,
    db: AsyncSession = Depends(get_db),
):
    """Preview-only quote for a hotel switch. Returns target_total, the
    available advance on the original, the suggested strategy, and whether
    the target hotel has rooms for the requested window. Does NOT mutate."""
    hr = await _load_hotel_reservation(db, reservation_id)
    return await quote_switch(
        db,
        hr,
        target_hotel_id=payload.target_hotel_id,
        target_room_category_id=payload.target_room_category_id,
        target_check_in=payload.target_check_in,
        target_check_out=payload.target_check_out,
        adults_count=payload.adults_count,
        children_count=payload.children_count,
        extra_beds=payload.extra_beds,
    )


@router.post(
    "/hotel/{reservation_id}/switch",
    tags=["Admin Hotel Switches"],
)
async def hotel_switch_commit(
    reservation_id: int,
    payload: _HotelSwitchCommitPayload,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """Commit a hotel switch. Branches between PRE_CHECKIN_SWITCH and
    POST_CHECKIN_SPLIT based on the original reservation's status."""
    if payload.strategy not in HOTEL_ADVANCE_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown advance strategy '{payload.strategy}'. "
                f"Allowed: {', '.join(HOTEL_ADVANCE_STRATEGIES)}"
            ),
        )
    hr = await _load_hotel_reservation(db, reservation_id)
    actor_id = None
    sub = (current_user or {}).get("sub")
    if sub:
        try:
            from uuid import UUID as _UUID

            actor_id = _UUID(str(sub))
        except (ValueError, TypeError):
            actor_id = None

    status_upper = str(hr.reservation_status or "").upper()
    if status_upper in {"CHECKED_IN", "IN_HOUSE"}:
        new_res = await split_stay_after_partial_checkin(
            db,
            hr,
            target_hotel_id=payload.target_hotel_id,
            target_room_category_id=payload.target_room_category_id,
            target_check_in=payload.target_check_in,
            target_check_out=payload.target_check_out,
            strategy=payload.strategy,
            reason=payload.reason,
            actor_id=actor_id,
            notes=payload.notes,
            adults_count=payload.adults_count,
            children_count=payload.children_count,
            extra_beds=payload.extra_beds,
        )
        split_type = "POST_CHECKIN_SPLIT"
    else:
        new_res = await switch_hotel_pre_checkin(
            db,
            hr,
            target_hotel_id=payload.target_hotel_id,
            target_room_category_id=payload.target_room_category_id,
            target_check_in=payload.target_check_in,
            target_check_out=payload.target_check_out,
            strategy=payload.strategy,
            reason=payload.reason,
            actor_id=actor_id,
            notes=payload.notes,
            adults_count=payload.adults_count,
            children_count=payload.children_count,
            extra_beds=payload.extra_beds,
        )
        split_type = "PRE_CHECKIN_SWITCH"

    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_SWITCHED_ORIGINAL")
    await _notify_hotel_update(db, new_res, action="HOTEL_SWITCH_CREATED")
    return {
        "success": True,
        "split_type": split_type,
        "original_reservation_id": int(hr.id),
        "original_reservation_number": hr.reservation_number,
        "new_reservation_id": int(new_res.id),
        "new_reservation_number": new_res.reservation_number,
        "new_master_booking_id": int(new_res.master_booking_id),
        "strategy": payload.strategy,
        "reason": payload.reason,
        "message": "Hotel switch committed",
    }


@router.post(
    "/hotel/{reservation_id}/advance-refund",
    tags=["Admin Hotel Switches"],
)
async def hotel_advance_refund(
    reservation_id: int,
    payload: _HotelAdvanceRefundPayload,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    """Record a Razorpay refund on an advance payment. Two-step flow:
    the switch marks the advance as fully pending-refund (`reference_number
    = 'MANUAL_PENDING'`); the admin hits this endpoint after the gateway
    confirms the refund and the platform receives the funds back.

    Status flips to REFUNDED only when the cumulative refunded_amount
    reaches the advance's amount.
    """
    hr = await _load_hotel_reservation(db, reservation_id)
    adv = (
        (
            await db.execute(
                text(
                    "SELECT id, amount, COALESCE(refunded_amount, 0) AS refunded_amount, "
                    "       status, hotel_reservation_id, reference_number "
                    "FROM hotel_advance_payments WHERE id = :id"
                ),
                {"id": payload.advance_payment_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if adv is None:
        raise HTTPException(status_code=404, detail="Advance payment not found")
    if int(adv["hotel_reservation_id"]) != int(reservation_id):
        raise HTTPException(
            status_code=409,
            detail="Advance does not belong to this reservation",
        )

    already = Decimal(str(adv["refunded_amount"] or 0))
    amount = Decimal(str(payload.refund_amount))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="refund_amount must be > 0")
    full_amount = Decimal(str(adv["amount"] or 0))
    new_refunded = hotel_billing.money(already + amount)
    if new_refunded > full_amount:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Refund {float(amount)} exceeds the remaining "
                f"{float(full_amount - already)} on advance {int(adv['id'])}."
            ),
        )
    new_status = "REFUNDED" if new_refunded >= full_amount else "ACTIVE"

    await db.execute(
        text(
            "UPDATE hotel_advance_payments "
            "   SET refunded_amount = :refund, "
            "       reference_number = COALESCE(:ref, reference_number), "
            "       status = :status, "
            "       notes = COALESCE(:notes, notes) "
            " WHERE id = :id"
        ),
        {
            "refund": new_refunded,
            "ref": payload.gateway_reference,
            "status": new_status,
            "notes": payload.notes,
            "id": int(adv["id"]),
        },
    )

    mb_id = (
        await db.execute(
            text("SELECT master_booking_id FROM hotel_reservations WHERE id = :id"),
            {"id": int(reservation_id)},
        )
    ).scalar_one()
    await _log_timeline(
        db,
        int(mb_id),
        HOTEL_ADVANCE_REFUND_RECORDED,
        (
            f"Advance {int(adv['id'])} refund {float(amount)} recorded "
            f"(status {new_status})"
        ),
    )

    await rollup_hotel_totals_into_master(db, int(mb_id))
    await db.commit()
    await _notify_hotel_update(db, hr, action="HOTEL_ADVANCE_REFUNDED")

    return {
        "success": True,
        "advance_payment_id": int(adv["id"]),
        "refunded_amount": float(new_refunded),
        "status": new_status,
    }


@router.get(
    "/hotel/switch-events",
    tags=["Admin Hotel Switches"],
)
async def list_hotel_switch_events(
    master_booking_id: Optional[int] = None,
    original_reservation_id: Optional[int] = None,
    new_reservation_id: Optional[int] = None,
    split_type: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Paginated audit page for hotel switch / split events. Joins both
    reservation numbers so the UI can show "original → new" without a second
    round-trip."""
    if split_type and split_type not in HOTEL_SPLIT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown split_type '{split_type}'. Allowed: {', '.join(HOTEL_SPLIT_TYPES)}",
        )

    where = ["1=1"]
    params: dict = {}
    if master_booking_id:
        where.append(
            "(orig.master_booking_id = :mb_id OR new.master_booking_id = :mb_id)"
        )
        params["mb_id"] = master_booking_id
    if original_reservation_id:
        where.append("e.original_reservation_id = :orig_id")
        params["orig_id"] = original_reservation_id
    if new_reservation_id:
        where.append("e.new_reservation_id = :new_id")
        params["new_id"] = new_reservation_id
    if split_type:
        where.append("e.split_type = :stype")
        params["stype"] = split_type
    if date_from:
        where.append("e.created_at >= :dfrom")
        params["dfrom"] = datetime.combine(
            date_from, datetime.min.time(), tzinfo=timezone.utc
        )
    if date_to:
        where.append("e.created_at < :dto")
        params["dto"] = datetime.combine(
            date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        )

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    rows = (
        (
            await db.execute(
                text(
                    f"""
                SELECT
                  e.*,
                  orig.reservation_number AS original_reservation_number,
                  orig.hotel_id           AS original_hotel_id,
                  new.reservation_number  AS new_reservation_number,
                  new.hotel_id            AS new_hotel_id
                FROM hotel_reservation_split_events e
                JOIN hotel_reservations orig ON orig.id = e.original_reservation_id
                JOIN hotel_reservations new  ON new.id = e.new_reservation_id
                WHERE {where_sql}
                ORDER BY e.created_at DESC
                LIMIT :limit OFFSET :offset
                """
                ),
                {**params, "limit": page_size, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    total = (
        await db.execute(
            text(
                f"SELECT COUNT(*) AS n FROM hotel_reservation_split_events e "
                f"JOIN hotel_reservations orig ON orig.id = e.original_reservation_id "
                f"JOIN hotel_reservations new  ON new.id = e.new_reservation_id "
                f"WHERE {where_sql}"
            ),
            params,
        )
    ).scalar_one()

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "split_type": str(r["split_type"]),
                "original_reservation_id": int(r["original_reservation_id"]),
                "original_reservation_number": r["original_reservation_number"],
                "original_hotel_id": int(r["original_hotel_id"]),
                "new_reservation_id": int(r["new_reservation_id"]),
                "new_reservation_number": r["new_reservation_number"],
                "new_hotel_id": int(r["new_hotel_id"]),
                "nights_transferred": int(r["nights_transferred"] or 0),
                "original_nights_consumed": int(r["original_nights_consumed"] or 0),
                "original_final_amount": float(r["original_final_amount"] or 0),
                "new_total_amount": float(r["new_total_amount"] or 0),
                "refund_issued": float(r["refund_issued"] or 0),
                "advance_redistributed": float(r["advance_redistributed"] or 0),
                "advance_split_strategy": str(r["advance_split_strategy"] or ""),
                "notes": r["notes"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    return {
        "success": True,
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": int(total or 0),
    }
