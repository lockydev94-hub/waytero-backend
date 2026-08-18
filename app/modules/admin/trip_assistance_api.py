# ============================================================
# WAYTERO — ADMIN TRIP ASSISTANCE API
# File: app/modules/admin/trip_assistance_api.py
# Prefix: /admin/trip-assist  (registered in api/router.py)
#
# Purpose:
#   Dedicated module allowing admin to assist drivers who cannot
#   self-start or self-close trips via the driver app.
#   Admin can:
#     1. Lookup booking by number → see full details
#     2. Start trip (record KM + datetime)
#     3. Close trip (record end KM + datetime → calc actual distance)
#     4. Collect payment (cash or online)
#     5. Generate invoice
#     6. Settle booking (with proper cash-pending logic)
#
# Cash flow logic (BRD Part 6 §147, §129):
#   - CASH collected: cash stays pending at DRIVER
#   - Partner must collect from driver (partner portal — future)
#   - When admin settles: cash moves from DRIVER → PARTNER pending
#   - ONLINE collected via platform: deduct commission → credit partner wallet
#
# Doc Ref:
#   BRD Part 3 §43-45, §50
#   BRD Part 6 §129, §137-143, §147
#   DB Schema Part 4 §7-8
#   DB Schema Part 7 §8-14
# ============================================================

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.modules.booking import services as advance_service
from app.modules.booking.services import breakdown as breakdown_service
from app.modules.booking.models import (
    MasterBooking,
    CabBooking,
    BookingTimeline,
)
from app.modules.customer.models import Customer
from app.modules.partner.models import Partner
from app.modules.driver.models import Driver
from app.modules.vehicle.models import Vehicle, VehicleCategory
from app.modules.master.models import City
from app.modules.auth.models.user import User

from app.core.dependencies import require_roles

# Trip-assistance is a live ops console (start/close/collect-payment/swap).
# It mutates financial state, so the whole router is admin/CCO-gated rather
# than trusting each endpoint to remember auth.
router = APIRouter(dependencies=[Depends(require_roles("SUPER_ADMIN", "ADMIN", "CCO"))])


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


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


async def _get_cab_booking_by_number(db: AsyncSession, booking_number: str):
    """
    Lookup cab_booking by booking_number (WT-CAB-XXXXXX format).
    Returns (master_booking, cab_booking, assignment).
    Raises 404 if not found.
    """
    q = select(CabBooking).where(
        CabBooking.booking_number == booking_number.strip().upper()
    )
    cb = (await db.execute(q)).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, f"Cab booking '{booking_number}' not found.")

    mb_q = (
        select(MasterBooking)
        .options(
            selectinload(MasterBooking.services),
            selectinload(MasterBooking.cab_bookings).selectinload(
                CabBooking.assignments
            ),
            selectinload(MasterBooking.timelines),
        )
        .where(MasterBooking.id == cb.master_booking_id)
    )
    mb = (await db.execute(mb_q)).scalar_one_or_none()
    if not mb:
        raise HTTPException(404, "Master booking not found.")

    return mb, cb


async def _build_full_detail(
    db: AsyncSession, mb: MasterBooking, cb: CabBooking
) -> dict:
    """Build enriched trip assistance detail payload."""
    # Customer
    cust = (
        await db.execute(select(Customer).where(Customer.id == mb.customer_id))
    ).scalar_one_or_none()
    cust_user = None
    if cust:
        cust_user = (
            await db.execute(select(User).where(User.id == cust.user_id))
        ).scalar_one_or_none()

    # City
    city = (
        await db.execute(select(City).where(City.id == mb.city_id))
    ).scalar_one_or_none()

    # Vehicle category
    cat_name = None
    if cb.vehicle_category_id:
        vc = (
            await db.execute(
                select(VehicleCategory).where(
                    VehicleCategory.id == cb.vehicle_category_id
                )
            )
        ).scalar_one_or_none()
        cat_name = vc.category_name if vc else None

    # Latest assignment
    partner_name = partner_mobile = driver_name = driver_mobile = vehicle_reg = (
        vehicle_model
    ) = None
    partner_id = driver_id = vehicle_id = None

    if cb.assignments:
        # Use the active assignment row (closed_at IS NULL) so a closed
        # handover row isn't surfaced as current.
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        partner_id = latest.partner_id
        p = (
            await db.execute(select(Partner).where(Partner.id == latest.partner_id))
        ).scalar_one_or_none()
        if p:
            partner_name = p.business_name or f"Partner #{p.id}"
            partner_mobile = p.mobile

        if latest.driver_id:
            driver_id = latest.driver_id
            d = (
                await db.execute(select(Driver).where(Driver.id == latest.driver_id))
            ).scalar_one_or_none()
            if d:
                driver_name = d.full_name
                driver_mobile = d.mobile

        if latest.vehicle_id:
            vehicle_id = latest.vehicle_id
            v = (
                await db.execute(select(Vehicle).where(Vehicle.id == latest.vehicle_id))
            ).scalar_one_or_none()
            if v:
                vehicle_reg = v.registration_number
                vehicle_model = (
                    f"{v.vehicle_brand or ''} {v.vehicle_model or ''}".strip() or None
                )

    # Timeline
    timeline = [
        {
            "id": t.id,
            "event_type": t.event_type,
            "event_description": t.event_description,
            "event_timestamp": t.event_timestamp.isoformat(),
        }
        for t in sorted(mb.timelines, key=lambda x: x.event_timestamp, reverse=True)
    ]

    # Coupon discount — sum all discount_applied for this master_booking_id
    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func

    coupon_q = await db.execute(
        select(_func.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)

    # Advance — the live advance_payments row, not total_paid_amount. That field
    # is a running "paid so far" total and is overwritten at final payment.
    advance = await advance_service.get_active_advance(db, cb.id)
    advance_paid = float(advance["amount"]) if advance else 0.0

    return {
        # Master booking
        "master_booking_id": mb.id,
        "booking_number": mb.booking_number,
        "booking_status": mb.booking_status,
        "payment_status": mb.payment_status,
        "total_amount": float(mb.total_amount or 0),
        "total_paid_amount": float(mb.total_paid_amount or 0),
        # Coupon & advance
        "coupon_discount": coupon_discount,
        "advance_paid": advance_paid,
        "advance": advance_service.advance_summary(advance),
        # Customer
        "customer_name": cust.full_name if cust else None,
        "customer_mobile": cust_user.mobile_number if cust_user else None,
        # City
        "city_name": city.name if city else None,
        # Cab booking
        "cab_booking_id": cb.id,
        "cab_booking_number": cb.booking_number,
        "cab_status": cb.booking_status,
        "trip_type": cb.trip_type,
        "vehicle_category_name": cat_name,
        "pickup_location": cb.pickup_location,
        "drop_location": cb.drop_location,
        "pickup_datetime": (
            cb.pickup_datetime.isoformat() if cb.pickup_datetime else None
        ),
        "estimated_distance": (
            float(cb.estimated_distance) if cb.estimated_distance else None
        ),
        "estimated_amount": float(cb.estimated_amount) if cb.estimated_amount else None,
        "final_amount": float(cb.final_amount) if cb.final_amount else None,
        # Trip start/end
        "trip_start_km": float(cb.trip_start_km) if cb.trip_start_km else None,
        "trip_started_at": (
            cb.trip_started_at.isoformat() if cb.trip_started_at else None
        ),
        "trip_end_km": float(cb.trip_end_km) if cb.trip_end_km else None,
        "trip_ended_at": cb.trip_ended_at.isoformat() if cb.trip_ended_at else None,
        "actual_distance": float(cb.actual_distance) if cb.actual_distance else None,
        # Payment
        "payment_mode": cb.payment_mode,
        "payment_collected_by": cb.payment_collected_by,
        "cash_pending_at": cb.cash_pending_at or "NONE",
        "platform_commission": (
            float(cb.platform_commission) if cb.platform_commission else None
        ),
        "partner_payout": float(cb.partner_payout) if cb.partner_payout else None,
        # Invoice
        "invoice_number": cb.invoice_number,
        "invoice_url": cb.invoice_url,
        # GST / Tax (migration 0024 — 0/false for pre-tax bookings)
        "gst_rate": (
            float(cb.gst_rate) if getattr(cb, "gst_rate", None) is not None else 0.0
        ),
        "gst_amount": (
            float(cb.gst_amount) if getattr(cb, "gst_amount", None) is not None else 0.0
        ),
        "is_tax_invoice": bool(getattr(cb, "is_tax_invoice", False)),
        # Assignment
        "partner_id": partner_id,
        "partner_name": partner_name,
        "partner_mobile": partner_mobile,
        "driver_id": driver_id,
        "driver_name": driver_name,
        "driver_mobile": driver_mobile,
        "vehicle_id": vehicle_id,
        "vehicle_reg": vehicle_reg,
        "vehicle_model": vehicle_model,
        # Timeline
        "timeline": timeline,
    }


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class TripStartRequest(BaseModel):
    booking_number: str = Field(
        ..., description="Cab booking number e.g. WT-CAB-202600001"
    )
    start_km: float = Field(..., ge=0, description="Vehicle odometer at trip start")
    start_datetime: datetime = Field(..., description="Trip start datetime (ISO)")


class TripCloseRequest(BaseModel):
    booking_number: str
    end_km: float = Field(..., ge=0, description="Vehicle odometer at trip end")
    end_datetime: datetime
    final_amount: float = Field(..., gt=0, description="Actual billing amount")


class CollectPaymentRequest(BaseModel):
    booking_number: str
    payment_mode: str = Field(..., description="CASH | ONLINE | WALLET")
    payment_collected_by: str = Field(..., description="DRIVER | PARTNER | PLATFORM")
    # For ONLINE: commission percentage to deduct
    commission_percent: float = Field(0.0, ge=0, le=100)


class GenerateInvoiceRequest(BaseModel):
    booking_number: str


# ════════════════════════════════════════════════════════════════
# LOOKUP BOOKING — GET /admin/trip-assist/lookup?booking_number=
# Admin searches by booking number to pull full trip details
# ════════════════════════════════════════════════════════════════


@router.get("/lookup", tags=["Trip Assistance"])
async def lookup_booking(
    booking_number: str = Query(..., description="Cab booking number (WT-CAB-...)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Lookup a cab booking by its booking number.
    Returns full booking + assignment + trip + billing details.
    Used by admin to pull the booking into the assistance console.
    """
    mb, cb = await _get_cab_booking_by_number(db, booking_number)
    return await _build_full_detail(db, mb, cb)


# ════════════════════════════════════════════════════════════════
# LIST ACTIVE TRIPS — GET /admin/trip-assist/active
# Shows all bookings that are in DRIVER_ASSIGNED or STARTED state
# ════════════════════════════════════════════════════════════════


@router.get("/active", tags=["Trip Assistance"])
async def list_active_trips(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    List all cab bookings that need admin trip assistance.

    Three buckets shown:
      1. DRIVER_ASSIGNED      — trip not yet started; admin may start on driver's behalf
      2. STARTED              — trip in progress; admin may close on driver's behalf
      3. COMPLETED + payment_mode IS NULL
                              — trip closed but payment not collected yet; admin must
                                collect payment on driver's / partner's behalf
      4. COMPLETED + cash_pending_at = 'DRIVER'
                              — trip done, driver collected CASH but hasn't handed it over
                                to partner yet; admin needs to track / follow up

    Excluded (no action needed — cleanly resolved):
      - COMPLETED with payment_mode set and cash_pending_at IN ('NONE', 'PARTNER', 'PLATFORM')
      - SETTLED / CANCELLED / any other terminal state
    """
    import math
    from sqlalchemy import or_, and_

    # Bucket 1 & 2: trip in progress
    in_progress_cond = CabBooking.booking_status.in_(["DRIVER_ASSIGNED", "STARTED"])

    # Bucket 3: trip completed but payment NOT yet collected at all
    payment_not_collected_cond = and_(
        CabBooking.booking_status == "COMPLETED",
        CabBooking.payment_mode == None,  # noqa: E711
    )

    # Bucket 4: trip completed, CASH collected by driver but not yet handed over to partner
    cash_pending_cond = and_(
        CabBooking.booking_status == "COMPLETED",
        CabBooking.cash_pending_at == "DRIVER",
    )

    filter_cond = or_(in_progress_cond, payment_not_collected_cond, cash_pending_cond)

    total_q = select(func.count(CabBooking.id)).where(filter_cond)
    total = (await db.execute(total_q)).scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                select(CabBooking)
                .options(selectinload(CabBooking.assignments))
                .where(filter_cond)
                .order_by(CabBooking.pickup_datetime.asc().nullslast())
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for cb in rows:
        mb = (
            await db.execute(
                select(MasterBooking)
                .options(
                    selectinload(MasterBooking.cab_bookings).selectinload(
                        CabBooking.assignments
                    ),
                    selectinload(MasterBooking.timelines),
                    selectinload(MasterBooking.services),
                )
                .where(MasterBooking.id == cb.master_booking_id)
            )
        ).scalar_one_or_none()
        if mb:
            items.append(await _build_full_detail(db, mb, cb))

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "items": items,
    }


# ════════════════════════════════════════════════════════════════
# START TRIP — POST /admin/trip-assist/start-trip
# Admin starts trip on behalf of driver
# Guard: cab must be DRIVER_ASSIGNED
# Records: trip_start_km, trip_started_at
# Doc Ref: BRD Part 3 §43 — Driver arrives, OTP verified, trip starts
# ════════════════════════════════════════════════════════════════


@router.post("/start-trip", tags=["Trip Assistance"])
async def admin_start_trip(
    payload: TripStartRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin starts a trip on behalf of a driver.
    Required: vehicle KM reading + start datetime.
    Guard: cab_booking must be in DRIVER_ASSIGNED status.
    Records trip_start_km, trip_started_at.
    Transitions cab_booking → STARTED, master_booking → IN_PROGRESS.
    """
    mb, cb = await _get_cab_booking_by_number(db, payload.booking_number)

    if cb.booking_status != "DRIVER_ASSIGNED":
        raise HTTPException(
            400,
            f"Cannot start trip: cab is '{cb.booking_status}'. "
            "Trip can only be started when status is DRIVER_ASSIGNED.",
        )

    if payload.start_km < 0:
        raise HTTPException(400, "Start KM cannot be negative.")

    cb.trip_start_km = Decimal(str(payload.start_km))
    cb.trip_started_at = payload.start_datetime
    cb.booking_status = "STARTED"

    # Migration 0041: snapshot start km onto the active assignment + flip
    # vehicle/driver availability to ON_TRIP.
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

    # Move master booking to IN_PROGRESS
    if mb.booking_status not in ("COMPLETED", "CLOSED", "CANCELLED"):
        mb.booking_status = "IN_PROGRESS"

    await _log_timeline(
        db,
        mb.id,
        "TRIP_STARTED_BY_ADMIN",
        f"Admin started trip for cab {cb.booking_number}. "
        f"Start KM: {payload.start_km}, Started at: {payload.start_datetime.isoformat()}",
    )
    # get_db auto-commits on success.

    return {
        "success": True,
        "message": "Trip started successfully by admin.",
        "cab_status": "STARTED",
        "trip_start_km": payload.start_km,
        "trip_started_at": payload.start_datetime.isoformat(),
    }


# ════════════════════════════════════════════════════════════════
# CLOSE TRIP — POST /admin/trip-assist/close-trip
# Admin closes trip on behalf of driver
# Guard: cab must be STARTED
# Records: trip_end_km, trip_ended_at, actual_distance, final_amount
# Doc Ref: BRD Part 3 §45 — Trip Completion
# ════════════════════════════════════════════════════════════════


@router.post("/close-trip", tags=["Trip Assistance"])
async def admin_close_trip(
    payload: TripCloseRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Admin closes/completes a trip on behalf of driver.
    BRD Part 3 §45 — Trip Completion.

    Validations (in order):
      1. Booking must be in STARTED status.
      2. end_km must be >= trip_start_km.
      3. end_datetime must be >= trip_started_at.
      4. final_amount must be > 0.
      5. Fare sanity check: final_amount must not deviate more than 50%
         from the pricing-rule-calculated fare (warns in response if pricing
         rule unavailable; hard-rejects >50% deviation when rule is found).

    On success:
      - Records trip_end_km, trip_ended_at, actual_distance, final_amount.
      - Auto-calculates platform_commission + partner_payout from commission rules.
      - Transitions cab → COMPLETED, master_booking → COMPLETED.
    """
    from app.modules.admin.services import DefaultPricingService
    from app.modules.admin.models import CommissionRule

    mb, cb = await _get_cab_booking_by_number(db, payload.booking_number)

    # ── Guard 1: status ───────────────────────────────────────────────────────
    if cb.booking_status != "STARTED":
        raise HTTPException(
            400,
            f"Cannot close trip: cab booking is '{cb.booking_status}'. "
            "Trip must be in STARTED status to close.",
        )

    # ── Guard 2: end_km >= start_km ───────────────────────────────────────────
    start_km = float(cb.trip_start_km) if cb.trip_start_km is not None else None
    if start_km is not None and payload.end_km < start_km:
        raise HTTPException(
            400,
            f"End KM ({payload.end_km:.1f}) cannot be less than start KM "
            f"({start_km:.1f}). Odometer can only increase.",
        )

    # ── Guard 3: end_datetime >= trip_started_at ──────────────────────────────
    if cb.trip_started_at:
        # Make both timezone-aware for comparison
        from datetime import timezone as _tz

        end_dt = payload.end_datetime
        start_dt = cb.trip_started_at
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=_tz.utc)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=_tz.utc)
        if end_dt < start_dt:
            raise HTTPException(
                400,
                f"End datetime ({end_dt.strftime('%Y-%m-%d %H:%M')}) cannot be "
                f"before trip start time ({start_dt.strftime('%Y-%m-%d %H:%M')}).",
            )

    # ── Guard 4: final_amount > 0 ─────────────────────────────────────────────
    if payload.final_amount <= 0:
        raise HTTPException(400, "Final amount must be greater than 0.")

    # ── Guard 5: fare sanity check against pricing rule ───────────────────────
    # Doc Ref: BRD Part 3 §35 — full fare formula (base + distance +
    # driver_allowance + night_charge) lives in
    # app.modules.booking.services.fare so all four calculation sites agree.
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
                        f"from the calculated fare ₹{calculated_fare:.2f} "
                        f"(allowed ±50%). Please review before closing. "
                        f"Formula: ₹{float(effective['base_fare']):.2f} base + "
                        f"max(0, {actual_dist:.1f} − {float(effective['minimum_km'] or 0):.0f}) km "
                        f"× ₹{float(effective['per_km_rate']):.2f}/km + "
                        f"₹{float(effective['driver_allowance'] or 0):.2f} allowance + "
                        f"₹{float(effective['night_charge'] or 0):.2f} night (if applicable).",
                    )
                elif deviation_pct > 20:
                    fare_warning = (
                        f"Note: Final amount ₹{payload.final_amount:.2f} deviates "
                        f"{deviation_pct:.1f}% from calculated fare ₹{calculated_fare:.2f}."
                    )

    # ── Compute actual distance ───────────────────────────────────────────────
    actual_distance = (
        Decimal(str(payload.end_km)) - cb.trip_start_km
        if cb.trip_start_km is not None
        else Decimal(str(payload.end_km))
    )
    if actual_distance < 0:
        actual_distance = Decimal("0.00")

    # ── Auto-calculate platform commission + partner payout ───────────────────
    # Lookup: partner's city commission rule (CAB, PERCENTAGE) → fallback general → default 10%
    commission_percent = 10.0
    commission_source = "default_10pct"
    partner_id = None
    if cb.assignments:
        # Active assignment only — closed (handovered) rows must not influence
        # the commission lookup.
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest_assign = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        partner_id = latest_assign.partner_id
    if partner_id:
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

    # ── GST calculation (BRD §155, Migration 0024) ────────────────────────────
    # Read GST_ENABLED and GST_RATE from system_configurations at trip-close time.
    # If GST is enabled: gst_amount = final_amount × gst_rate / 100
    # is_tax_invoice = True only when GST is enabled (partner_type distinction
    # affects settlement reporting, handled at settlement time; the invoice
    # always shows GST when enabled, regardless of partner type).
    from app.modules.admin.models import SystemConfiguration

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

    # ── Write all fields ──────────────────────────────────────────────────────
    cb.trip_end_km = Decimal(str(payload.end_km))
    cb.trip_ended_at = payload.end_datetime
    cb.final_amount = final_dec
    cb.actual_distance = actual_distance
    cb.platform_commission = platform_commission
    cb.partner_payout = partner_payout
    cb.booking_status = "COMPLETED"
    # Migration 0041: close the active assignment and release vehicle/driver.
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
    # GST fields (migration 0024)
    cb.gst_rate = Decimal(str(gst_rate_pct))
    cb.gst_amount = gst_amount_dec
    cb.is_tax_invoice = gst_enabled

    mb.total_amount = final_dec
    mb.booking_status = "COMPLETED"

    # ── Fetch coupon discount + advance paid for this booking ────────────────
    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func

    coupon_q = await db.execute(
        select(_func.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    advance_paid = await advance_service.get_advance_amount(db, cb.id)
    # Balance is computed on the gross (fare + GST when it is a tax invoice), so
    # it matches the customer-facing invoice PDF. See compute_balance_due.
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
        "TRIP_CLOSED_BY_ADMIN",
        f"Trip closed for {cb.booking_number}. "
        f"End KM: {payload.end_km:.1f} | "
        f"Distance: {float(actual_distance):.1f} km | "
        f"Final: ₹{payload.final_amount:.2f} | "
        f"Coupon discount: ₹{coupon_discount:.2f} | "
        f"Advance paid: ₹{advance_paid:.2f} | "
        f"Balance due: ₹{balance_due:.2f} | "
        f"Commission ({commission_percent}%, {commission_source}): ₹{float(platform_commission):.2f} | "
        f"Partner payout: ₹{float(partner_payout):.2f}"
        + (f" | {fare_warning}" if fare_warning else ""),
    )
    # get_db auto-commits on success.

    # Real-time fan-out: refresh booking pages on both portals.
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
            service_status="COMPLETED",
            action="ADMIN_CLOSE_TRIP",
            payment_status=mb.payment_status,
            partner_ids=(
                [a.partner_id for a in cb.assignments] if cb.assignments else None
            ),
        )
    except Exception:  # pragma: no cover - best-effort
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
        "gst_enabled": gst_enabled,
        "gst_rate": gst_rate_pct,
        "gst_amount": float(gst_amount_dec),
    }


# ════════════════════════════════════════════════════════════════
# CUSTOMER WALLET BALANCE — GET /admin/trip-assist/customer-wallet
# Returns the customer's wallet balance for a booking
# ════════════════════════════════════════════════════════════════


@router.get("/customer-wallet", tags=["Trip Assistance"])
async def get_customer_wallet(
    booking_number: str = Query(..., description="Cab booking number"),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch customer wallet balance for a booking.
    Used by Collect Payment modal to show live balance before WALLET deduction.
    """
    mb, cb = await _get_cab_booking_by_number(db, booking_number)

    wallet_row = (
        (
            await db.execute(
                text(
                    """
            SELECT id, available_balance, hold_balance, wallet_status
            FROM customer_wallets
            WHERE customer_id = :cid
        """
                ),
                {"cid": mb.customer_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not wallet_row:
        # Wallet missing — auto-create it now (idempotent safety net)
        await db.execute(
            text(
                """
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance, wallet_status, created_at, updated_at)
                VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
                ON CONFLICT (customer_id) DO NOTHING
            """
            ),
            {"cid": mb.customer_id},
        )
        # get_db auto-commits on success.
        # Re-fetch after creation
        wallet_row = (
            (
                await db.execute(
                    text(
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


# ════════════════════════════════════════════════════════════════
# COLLECT PAYMENT — POST /admin/trip-assist/collect-payment
# Guard: cab must be COMPLETED
# Records payment_mode, payment_collected_by, cash_pending_at,
# platform_commission, partner_payout
#
# Cash logic (BRD Part 6 §129):
#   CASH collected by DRIVER → cash_pending_at = DRIVER
#   Partner must collect cash from driver (partner portal — future)
#   When partner portal exists: driver marks cash handed over → PARTNER
#   ONLINE via platform → commission deducted → partner_payout calculated
# ════════════════════════════════════════════════════════════════


@router.post("/collect-payment", tags=["Trip Assistance"])
async def admin_collect_payment(
    payload: CollectPaymentRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Record how payment was collected after trip completion.
    BRD Part 6 §129 (wallet hold), §147 (settlement cycle).

    CASH   → cash_pending_at = DRIVER; partner collects from driver later
    WALLET → deduct balance_due from customer wallet (balance guard enforced)

    Note: ONLINE is a driver-app flow — not accepted via admin portal.

    Commission is always looked up from commission rules (not accepted from payload).
    """
    mb, cb = await _get_cab_booking_by_number(db, payload.booking_number)

    # ── Guard: status ─────────────────────────────────────────────────────────
    if cb.booking_status != "COMPLETED":
        raise HTTPException(
            400,
            f"Cannot record payment: cab is '{cb.booking_status}'. "
            "Payment can only be collected after trip is COMPLETED.",
        )

    # ── Guard: duplicate payment ──────────────────────────────────────────────
    # Prevents double-recording if admin clicks the button twice or a retry hits
    if cb.payment_mode:
        raise HTTPException(
            400,
            f"Payment already recorded for this booking (mode: {cb.payment_mode}). "
            "Duplicate payment collection is not allowed.",
        )

    # ── Guard: mode ───────────────────────────────────────────────────────────
    mode = payload.payment_mode.upper()
    if mode not in {"CASH", "WALLET"}:
        raise HTTPException(
            400,
            "Admin portal only supports CASH or WALLET payment. "
            "ONLINE payments are recorded by the driver app.",
        )

    valid_collectors = {"DRIVER", "PARTNER", "PLATFORM"}
    collector = payload.payment_collected_by.upper()
    if collector not in valid_collectors:
        raise HTTPException(
            400,
            f"Invalid payment_collected_by. Must be one of: {', '.join(valid_collectors)}",
        )

    final = float(cb.final_amount or cb.estimated_amount or 0)
    if final <= 0:
        raise HTTPException(
            400, "Final amount is not set. Close the trip before collecting payment."
        )

    # ── Look up commission from rules (never from payload) ────────────────────
    # commission and partner_payout were already computed at close-trip — don't recompute
    commission = cb.platform_commission or Decimal("0.00")
    payout = cb.partner_payout or Decimal(str(final))

    # ── Fetch coupon discount + advance to compute balance_due ────────────────
    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func2

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

    # ── Write payment fields via explicit SQL UPDATE (avoids ORM identity-map
    # confusion from selectinload(MasterBooking.cab_bookings) loading a second
    # CabBooking instance for the same row — plain ORM mutation can silently lose
    # the change when two Python objects track the same DB row) ─────────────────
    if mode == "CASH":
        await db.execute(
            text(
                """
                UPDATE cab_bookings
                SET payment_mode = :mode,
                    payment_collected_by = :collector,
                    cash_pending_at = 'DRIVER',
                    cash_amount_due = :cash_due
                WHERE id = :cb_id
            """
            ),
            {
                "mode": mode,
                "collector": collector,
                "cash_due": balance_due,
                "cb_id": cb.id,
            },
        )
        # Refresh cb so subsequent reads (invoice_number check, return value) are accurate
        await db.refresh(cb)

        await _log_timeline(
            db,
            mb.id,
            "PAYMENT_CASH_COLLECTED",
            f"Cash payment ₹{balance_due:.2f} (balance due) collected by {collector}. "
            f"Full fare: ₹{final:.2f} | Coupon: −₹{coupon_discount:.2f} | "
            f"Advance: −₹{advance_paid:.2f}. Cash pending at: DRIVER.",
        )

    # ── WALLET flow ───────────────────────────────────────────────────────────
    elif mode == "WALLET":
        # 1. Load customer wallet
        wallet_row = (
            (
                await db.execute(
                    text(
                        "SELECT id, available_balance, wallet_status FROM customer_wallets WHERE customer_id = :cid FOR UPDATE"
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

        # 2. Balance guard — wallet must cover the balance_due
        avail = float(wallet_row["available_balance"] or 0)
        amount_to_deduct = balance_due  # only deduct what's still owed
        if avail < amount_to_deduct:
            raise HTTPException(
                400,
                f"Insufficient wallet balance. "
                f"Required: ₹{amount_to_deduct:.2f} | "
                f"Available: ₹{avail:.2f} | "
                f"Shortfall: ₹{amount_to_deduct - avail:.2f}. "
                "Customer must recharge their wallet before payment.",
            )

        # 3. Deduct from wallet
        new_balance = round(avail - amount_to_deduct, 2)
        await db.execute(
            text(
                """
                UPDATE customer_wallets
                SET available_balance = :bal, updated_at = NOW()
                WHERE id = :wid
            """
            ),
            {"bal": new_balance, "wid": wallet_row["id"]},
        )

        # 4. Write ledger entry
        await db.execute(
            text(
                """
                INSERT INTO customer_wallet_ledger
                    (customer_wallet_id, transaction_reference, reference_type,
                     debit_amount, credit_amount, balance_after, narration, created_at)
                VALUES
                    (:wid, :ref, 'CAB_PAYMENT',
                     :debit, 0, :bal_after,
                     :narration, NOW())
            """
            ),
            {
                "wid": wallet_row["id"],
                "ref": cb.booking_number,
                "debit": amount_to_deduct,
                "bal_after": new_balance,
                "narration": f"CAB trip payment — {cb.booking_number} | "
                f"Fare ₹{final:.2f} | Coupon −₹{coupon_discount:.2f} | "
                f"Advance −₹{advance_paid:.2f} | Deducted ₹{amount_to_deduct:.2f}",
            },
        )

        await db.execute(
            text(
                """
                UPDATE cab_bookings
                SET payment_mode = :mode,
                    payment_collected_by = :collector,
                    cash_pending_at = 'NONE'
                WHERE id = :cb_id
            """
            ),
            {"mode": mode, "collector": collector, "cb_id": cb.id},
        )
        await db.refresh(cb)

        await _log_timeline(
            db,
            mb.id,
            "PAYMENT_WALLET_COLLECTED",
            f"Wallet payment ₹{amount_to_deduct:.2f} deducted from customer wallet. "
            f"New wallet balance: ₹{new_balance:.2f}. "
            f"Commission: ₹{float(commission):.2f} | Partner payout: ₹{float(payout):.2f}.",
        )

    # ── Update master booking payment status (explicit SQL — avoids ORM flush issues)
    # total_paid_amount is what the customer has actually handed over: the advance
    # plus the balance just collected. It used to be set to the full fare, which
    # both overstated collections on discounted bookings and destroyed the advance
    # figure that the settlement and invoice code read back out of it.
    await db.execute(
        text(
            """
            UPDATE master_bookings
            SET payment_status    = 'PAID',
                total_paid_amount = :amount
            WHERE id = :mb_id
        """
        ),
        {"amount": round(advance_paid + balance_due, 2), "mb_id": mb.id},
    )
    await db.refresh(mb)

    # ── Auto-generate invoice number at payment time ───────────────────────
    # BRD Part 3 §45 — invoice is created when payment is recorded, not as a
    # separate manual step. Admin simply downloads it afterwards.
    if not cb.invoice_number:
        from datetime import date as _date

        _today = _date.today()
        _max_row = (
            await db.execute(
                text(
                    "SELECT MAX(CAST(SPLIT_PART(invoice_number, '-', 4) AS INTEGER)) FROM cab_bookings WHERE invoice_number IS NOT NULL AND invoice_number LIKE 'WT-INV-%'"
                )
            )
        ).scalar()
        _next_seq = (_max_row or 0) + 1
        _new_invoice = f"WT-INV-{_today.strftime('%Y%m')}-{str(_next_seq).zfill(5)}"
        await db.execute(
            text("UPDATE cab_bookings SET invoice_number = :inv WHERE id = :cb_id"),
            {"inv": _new_invoice, "cb_id": cb.id},
        )
        await db.refresh(cb)
        await _log_timeline(
            db,
            mb.id,
            "INVOICE_AUTO_GENERATED",
            f"Invoice {_new_invoice} auto-generated at payment collection. "
            f"Mode: {mode} | Collected by: {collector}.",
        )

    # get_db auto-commits on success.

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
    }


# ════════════════════════════════════════════════════════════════
# GENERATE INVOICE — POST /admin/trip-assist/generate-invoice
# Creates invoice_number, marks ready for PDF generation
# Guard: cab must be COMPLETED and payment recorded
# ════════════════════════════════════════════════════════════════


@router.post("/generate-invoice", tags=["Trip Assistance"])
async def generate_invoice(
    payload: GenerateInvoiceRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate invoice number for a completed booking.
    If invoice already exists, returns existing invoice details.
    For PDF: invoke from frontend using Cloudinary after this call.
    Doc Ref: BRD Part 3 §45, DB Schema Part 7 §15
    """
    mb, cb = await _get_cab_booking_by_number(db, payload.booking_number)

    if cb.booking_status not in ("COMPLETED", "SETTLEMENT_PENDING", "SETTLED"):
        raise HTTPException(
            400,
            f"Cannot generate invoice: cab is '{cb.booking_status}'. "
            "Invoice can only be generated after trip is COMPLETED.",
        )

    if not cb.payment_mode:
        raise HTTPException(400, "Record payment before generating invoice.")

    # If invoice already generated, return it
    if cb.invoice_number:
        return {
            "success": True,
            "already_exists": True,
            "invoice_number": cb.invoice_number,
            "invoice_url": cb.invoice_url,
            "message": "Invoice already generated.",
        }

    # Generate invoice number: WT-INV-YYYYMM-XXXXX
    from datetime import date

    now = date.today()
    count_q = select(func.count(CabBooking.id)).where(
        CabBooking.invoice_number.is_not(None)
    )
    count = (await db.execute(count_q)).scalar() or 0
    invoice_number = f"WT-INV-{now.strftime('%Y%m')}-{str(count + 1).zfill(5)}"

    cb.invoice_number = invoice_number

    await _log_timeline(
        db,
        mb.id,
        "INVOICE_GENERATED",
        f"Invoice {invoice_number} generated for cab {cb.booking_number}. "
        f"Amount: ₹{float(cb.final_amount or 0):.2f}",
    )
    # get_db auto-commits on success.

    # ── Email: invoice issued to the customer ──
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
            cust_name = (
                " ".join(filter(None, [cust_row.first_name, cust_row.last_name]))
                or "there"
            )
            await send_event_email(
                db,
                event_type="invoice_issued",
                to_email=cust_row.email,
                to_name=cust_name,
                context={
                    "name": cust_name,
                    "service": "Cab",
                    "booking_number": cb.booking_number,
                    "message": "Your tax invoice is ready to download from your bookings.",
                    "details": [("Invoice no.", invoice_number)],
                    **(
                        {
                            "amount": float(cb.final_amount),
                            "amount_label": "Trip fare",
                        }
                        if cb.final_amount
                        else {}
                    ),
                },
                related_type="CAB_BOOKING",
                related_id=cb.id,
            )
    except Exception:  # pragma: no cover — email must never break the flow
        pass

    return {
        "success": True,
        "already_exists": False,
        "invoice_number": invoice_number,
        "invoice_url": cb.invoice_url,
        "message": "Invoice number generated. Upload PDF to set invoice_url.",
    }


# ════════════════════════════════════════════════════════════════
# UPDATE INVOICE URL — POST /admin/trip-assist/update-invoice-url
# After PDF uploaded to Cloudinary, store URL
# ════════════════════════════════════════════════════════════════


class UpdateInvoiceUrlRequest(BaseModel):
    booking_number: str
    invoice_url: str


@router.post("/update-invoice-url", tags=["Trip Assistance"])
async def update_invoice_url(
    payload: UpdateInvoiceUrlRequest,
    db: AsyncSession = Depends(get_db),
):
    mb, cb = await _get_cab_booking_by_number(db, payload.booking_number)
    cb.invoice_url = payload.invoice_url.strip()
    await _log_timeline(
        db,
        mb.id,
        "INVOICE_URL_UPDATED",
        f"Invoice PDF URL saved for {cb.booking_number}.",
    )
    # get_db auto-commits on success.
    return {"success": True, "invoice_url": cb.invoice_url}


# ════════════════════════════════════════════════════════════════
# DOWNLOAD INVOICE PDF — GET /admin/trip-assist/download-invoice
# Generates and streams a premium PDF invoice on demand.
# Invoice number is auto-generated at payment time, so by the
# time admin reaches this endpoint it will always exist.
# Platform branding is read from system_configurations.
# ════════════════════════════════════════════════════════════════


@router.get("/download-invoice", tags=["Trip Assistance"])
async def download_invoice_pdf(
    booking_number: str = Query(..., description="Cab booking number (WT-CAB-...)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate and stream a premium PDF invoice for a completed + paid booking.

    Flow:
      1. Validate: booking must be COMPLETED/SETTLED and payment recorded.
      2. Load platform branding from system_configurations.
      3. Build PDF in-memory via ReportLab (invoice_pdf_service).
      4. Stream as application/pdf with Content-Disposition: attachment.
    """
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.invoice_pdf_service import generate_invoice_pdf
    from app.modules.admin.coupon_models import CouponUsage
    from sqlalchemy import func as _func3

    mb, cb = await _get_cab_booking_by_number(db, booking_number)

    # Guard: must be completed and payment recorded
    if cb.booking_status not in ("COMPLETED", "SETTLEMENT_PENDING", "SETTLED"):
        raise HTTPException(
            400,
            f"Cannot generate invoice: booking is '{cb.booking_status}'. "
            "Trip must be COMPLETED before downloading invoice.",
        )
    if not cb.payment_mode:
        raise HTTPException(400, "Payment must be recorded before downloading invoice.")
    if not cb.invoice_number:
        raise HTTPException(
            400,
            "Invoice number not found. This should not happen — "
            "invoice is auto-generated when payment is collected.",
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
                            "GST_ENABLED",
                            "GST_RATE",
                            "COMMISSION_GST_RATE",
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    cfg = {row.config_key: (row.config_value or "") for row in config_rows}

    # ── Load booking details ───────────────────────────────────────────────
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

    cat_name = None
    if cb.vehicle_category_id:
        vc = (
            await db.execute(
                select(VehicleCategory).where(
                    VehicleCategory.id == cb.vehicle_category_id
                )
            )
        ).scalar_one_or_none()
        cat_name = vc.category_name if vc else None

    # Assignment details
    partner_name = driver_name = vehicle_reg = vehicle_model = None
    if cb.assignments:
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
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

    # Coupon + advance
    coupon_q = await db.execute(
        select(_func3.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    advance_paid = await advance_service.get_advance_amount(db, cb.id)

    # ── Generate PDF ───────────────────────────────────────────────────────
    from datetime import timezone as _tz2

    pdf_bytes = generate_invoice_pdf(
        invoice_number=cb.invoice_number,
        cab_booking_number=cb.booking_number,
        booking_number=mb.booking_number,
        invoice_date=datetime.now(_tz2.utc),
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
        # GST fields (migration 0024)
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
# COMMISSION PREVIEW — GET /admin/trip-assist/commission-preview
# Returns applicable commission percentage for a booking's partner
# Looks up commission_rules for partner's city + CAB service type
# Fallback: returns default 10% if no rule found
# ════════════════════════════════════════════════════════════════


@router.get("/commission-preview", tags=["Trip Assistance"])
async def get_commission_preview(
    booking_number: str = Query(..., description="Cab booking number"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the applicable commission percentage for a booking.
    Lookup order:
      1. CommissionRule for partner's city + CAB service + PERCENTAGE type
      2. CommissionRule for any city + CAB + PERCENTAGE (group-level rule)
      3. Default: 10%
    """
    from app.modules.admin.models import CommissionRule

    mb, cb = await _get_cab_booking_by_number(db, booking_number)

    # Get latest assignment partner — active only.
    partner_id = None
    if cb.assignments:
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        partner_id = latest.partner_id

    commission_percent = 10.0
    rule_source = "default"

    if partner_id:
        # Get partner city
        p = (
            await db.execute(select(Partner).where(Partner.id == partner_id))
        ).scalar_one_or_none()
        if p and p.city_id:
            # Try city-specific CAB PERCENTAGE rule
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
                rule_source = f"city_rule:{city_rule.id}"
            else:
                # Try general CAB PERCENTAGE rule (no city filter)
                general_rule = (
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
                if general_rule:
                    commission_percent = float(general_rule.commission_value or 10)
                    rule_source = f"general_rule:{general_rule.id}"

    return {
        "booking_number": booking_number,
        "partner_id": partner_id,
        "commission_percent": commission_percent,
        "rule_source": rule_source,
    }


# ════════════════════════════════════════════════════════════════
# SETTLEMENT — handled exclusively by POST /admin/settlements/settle
#
# The old POST /admin/trip-assist/settle endpoint was removed: it flipped
# the cab to SETTLED / master to CLOSED without crediting the partner's
# wallet, deducting TDS/commission or disbursing coupons — a fake
# settlement. The trip-assist console now stops at "collect payment"
# (which records the payment mode and generates the invoice); the actual
# wallet math happens on the Settlements page via the real settlement
# engine, matching the removal of /mark-settled.
# Doc Ref: BRD Part 6 §147 settlement cycle
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
# GET PRICING PREVIEW — GET /admin/trip-assist/pricing
# Returns city+category pricing rule so CloseTrip modal can
# auto-calculate final_amount = base_fare + per_km_rate × distance
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
# GET PRICING PREVIEW — GET /admin/trip-assist/pricing
# Returns city+category pricing rule so CloseTrip modal can
# auto-calculate final_amount = base_fare + per_km_rate × distance
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
# GET PRICING PREVIEW — GET /admin/trip-assist/pricing
# Returns city+category pricing rule so CloseTrip modal can
# auto-calculate final_amount = base_fare + per_km_rate × distance
# ════════════════════════════════════════════════════════════════


@router.get("/pricing", tags=["Trip Assistance"])
async def get_trip_pricing(
    booking_number: str = Query(..., description="Cab booking number (WT-CAB-...)"),
    end_km: Optional[float] = Query(
        None, description="End odometer reading (optional, for live preview)"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch pricing rule for a booking's city + vehicle_category + trip_type.
    If end_km is supplied, also compute:
      actual_distance = end_km - trip_start_km
      calculated_amount = shared fare engine (base + billable-km distance +
      driver allowance by type + night/toll/waiting) — see fare.py

    Returns:
      pricing_rule: base_fare, per_km_rate, minimum_km, driver_allowance,
                    night_charge, driver_allowance_type
      calculated_amount: computed if end_km given
      actual_distance: computed if end_km given
    """
    mb, cb = await _get_cab_booking_by_number(db, booking_number)

    # Load city
    city = (
        await db.execute(select(City).where(City.id == mb.city_id))
    ).scalar_one_or_none()

    # Load pricing rule — uses DefaultPricingService for city-specific → default fallback
    # Formula: base_fare + max(0, actual_km - minimum_km) * per_km_rate
    # minimum_km = KM INCLUDED IN BASE FARE (free km within base price)
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
    elif cb.vehicle_category_id and mb.city_id and not cb.trip_type:
        # No trip_type set — try to get any rule for this city+category
        from app.modules.vehicle.models import VehiclePricingRule

        r = (
            await db.execute(
                select(VehiclePricingRule)
                .where(
                    VehiclePricingRule.city_id == mb.city_id,
                    VehiclePricingRule.vehicle_category_id == cb.vehicle_category_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if r:
            rule_data = {
                "base_fare": float(r.base_fare or 0),
                "per_km_rate": float(r.per_km_rate or 0),
                "minimum_km": int(r.minimum_km or 0),
                "driver_allowance": float(r.driver_allowance or 0),
                "night_charge": float(r.night_charge or 0),
                "driver_allowance_type": r.driver_allowance_type or "PER_TRIP",
                "night_charge_type": r.night_charge_type or "FIXED",
                "toll": float(r.toll or 0),
                "source": "city_specific_no_trip_type",
            }

    # Compute preview if end_km provided — uses the shared fare engine so
    # PER_DAY / PER_KM / billable-minimum rules price identically to the
    # booking engine. Doc Ref: BRD Part 3 §35.
    calculated_amount = None
    actual_distance = None
    if end_km is not None and cb.trip_start_km is not None:
        actual_distance = max(0.0, end_km - float(cb.trip_start_km))
        if rule_data:
            from app.modules.booking.services.fare import calculate_fare, trip_days_from

            calculated_amount = float(
                calculate_fare(
                    rule_data,
                    actual_distance,
                    start_dt=cb.trip_started_at,
                    end_dt=cb.return_datetime or cb.trip_started_at,
                    trip_type=cb.trip_type,
                    trip_days=trip_days_from(cb.pickup_datetime, cb.return_datetime),
                )
            )

    return {
        "booking_number": booking_number,
        "city_name": city.name if city else None,
        "trip_type": cb.trip_type,
        "vehicle_category": cb.vehicle_category_id,
        "trip_start_km": float(cb.trip_start_km) if cb.trip_start_km else None,
        "estimated_distance": (
            float(cb.estimated_distance) if cb.estimated_distance else None
        ),
        "estimated_amount": float(cb.estimated_amount) if cb.estimated_amount else None,
        "pricing_rule": rule_data,
        "actual_distance": actual_distance,
        "calculated_amount": calculated_amount,
    }
