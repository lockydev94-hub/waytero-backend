# ============================================================
# WAY TERO — TOUR BOOKING MANAGEMENT SERVICE
# File: app/modules/tour/services/__init__.py
# Doc Ref: BRD Part 5 §6 (tour package management),
#          BRD Part 3 §45 (advance collection & settlement custody)
#
# Step-by-step manage flow shared by the admin + partner portals:
#   persons confirm → advance (optional) → itinerary check →
#   vehicle + driver assign → hotel/other details → receipt/invoice PDF →
#   start trip → modify (extra days → additional charge) →
#   complete → invoice + collect balance → settle (wallet movement).
#
# All money rules (who may receive, in what form, settlement custody)
# mirror the cab/hotel flows so the three services cannot drift apart.
# ============================================================

from __future__ import annotations

from datetime import date as _date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException, ResourceNotFoundException
from app.modules.booking.services import rollup_tour_totals_into_master
from app.modules.driver.models import Driver
from app.modules.tour.models import (
    TourAdvancePayment,
    TourBooking,
    TourBookingCharge,
    TourItinerary,
    TourPackage,
    TourPackagePricing,
)
from app.modules.vehicle.models import Vehicle
from app.modules.admin.models import CommissionRule, SystemConfiguration
from app.modules.master.models import City
from app.modules.partner.models import Partner

# Platform branding keys shared by every PDF document (mirrors the cab/hotel
# invoice service so all documents carry the same header identity).
_BRANDING_KEYS = [
    "PLATFORM_NAME",
    "PLATFORM_LOGO_URL",
    "BUSINESS_LEGAL_NAME",
    "BUSINESS_GST_NUMBER",
    "BUSINESS_REGISTERED_ADDRESS",
    "SUPPORT_EMAIL",
    "SUPPORT_PHONE",
]


async def _platform_branding(db: AsyncSession) -> dict:
    rows = (
        await db.scalars(
            select(SystemConfiguration).where(
                SystemConfiguration.config_key.in_(_BRANDING_KEYS)
            )
        )
    ).all()
    cfg = {row.config_key: (row.config_value or "") for row in rows}
    return {
        "platform_name": cfg.get("PLATFORM_NAME") or "WayTero",
        "platform_logo_url": cfg.get("PLATFORM_LOGO_URL") or "",
        "business_legal_name": cfg.get("BUSINESS_LEGAL_NAME") or "",
        "business_gst_number": cfg.get("BUSINESS_GST_NUMBER") or "",
        "business_registered_address": cfg.get("BUSINESS_REGISTERED_ADDRESS") or "",
        "support_email": cfg.get("SUPPORT_EMAIL") or "",
        "support_phone": cfg.get("SUPPORT_PHONE") or "",
    }


Q = Decimal("0.01")


def _q(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Q, rounding=ROUND_HALF_UP)


# ── Pricing (mirrors tour/api.py _price_for — kept here so the service is
#    importable without the API layer and vice-versa) ────────────────────────


async def _commission_percent(db: AsyncSession, package: TourPackage) -> Decimal:
    rule = await db.scalar(
        select(CommissionRule)
        .where(
            CommissionRule.service_type == "TOUR",
            CommissionRule.commission_type == "PERCENTAGE",
            CommissionRule.is_active.is_(True),
            or_(
                CommissionRule.city_id == package.city_id,
                CommissionRule.city_id.is_(None),
            ),
        )
        .order_by(CommissionRule.city_id.desc().nullslast(), CommissionRule.id.desc())
        .limit(1)
    )
    return Decimal(
        str(rule.commission_value if rule and rule.commission_value is not None else 10)
    )


async def price_for_persons(
    db: AsyncSession, package: TourPackage, persons: int
) -> tuple[Decimal, Decimal, Decimal]:
    """Total, commission, payout for `persons` travellers on a package."""
    rows = (
        await db.scalars(
            select(TourPackagePricing)
            .where(TourPackagePricing.package_id == package.id)
            .order_by(TourPackagePricing.persons_count)
        )
    ).all()
    if not rows:
        raise BusinessException(
            "This package has no active pricing slabs", code="NO_PRICING"
        )
    if persons < package.minimum_persons or (
        package.maximum_persons and persons > package.maximum_persons
    ):
        raise BusinessException(
            f"This package accepts {package.minimum_persons}–"
            f"{package.maximum_persons or 'unlimited'} travellers",
            code="PAX_OUT_OF_RANGE",
        )
    row = next((r for r in rows if r.persons_count >= persons), rows[-1])
    total = _q(row.package_price)
    percent = await _commission_percent(db, package)
    commission = (total * percent / Decimal("100")).quantize(Q, rounding=ROUND_HALF_UP)
    return total, commission, total - commission


# ── Receipt / invoice numbering ─────────────────────────────────────────────


async def _next_number(db: AsyncSession, prefix: str, table: str, column: str) -> str:
    """WT-TADV-YYYYMMDD-0001 style sequential number (per-day reset)."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    like = f"{prefix}-{today}-%"
    row = (
        await db.execute(
            text(
                f"SELECT MAX(CAST(SPLIT_PART({column}, '-', 4) AS INTEGER)) "
                f"FROM {table} WHERE {column} LIKE :like"
            ),
            {"like": like},
        )
    ).scalar_one_or_none()
    return f"{prefix}-{today}-{int(row or 0) + 1:04d}"


async def _next_receipt_number(db: AsyncSession) -> str:
    return await _next_number(db, "WT-TADV", "tour_advance_payments", "receipt_number")


async def _next_invoice_number(db: AsyncSession) -> str:
    return await _next_number(db, "WT-TINV", "tour_bookings", "invoice_number")


# ── Serialisation ───────────────────────────────────────────────────────────


async def _booking_dict(
    db: AsyncSession, booking: TourBooking, *, include_fleet: bool = False
) -> dict:
    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    partner_id = package.partner_id if package else None
    partner_name = (
        await db.scalar(select(Partner.business_name).where(Partner.id == partner_id))
        if partner_id
        else None
    )
    vehicle = (
        (
            await db.execute(
                select(Vehicle, Driver.full_name)
                .select_from(Vehicle)
                .outerjoin(Driver, Driver.id == booking.driver_id)
                .where(Vehicle.id == booking.vehicle_id)
            )
        ).one_or_none()
        if booking.vehicle_id
        else None
    )
    driver = (
        await db.scalar(select(Driver).where(Driver.id == booking.driver_id))
        if booking.driver_id
        else None
    )
    advances = (
        await db.scalars(
            select(TourAdvancePayment)
            .where(TourAdvancePayment.tour_booking_id == booking.id)
            .order_by(TourAdvancePayment.created_at.desc())
        )
    ).all()
    charges = (
        await db.scalars(
            select(TourBookingCharge)
            .where(TourBookingCharge.tour_booking_id == booking.id)
            .order_by(TourBookingCharge.created_at.desc())
        )
    ).all()
    data = {
        "id": booking.id,
        "booking_number": booking.booking_number,
        "master_booking_id": booking.master_booking_id,
        "package_id": booking.package_id,
        "package_name": package.package_name if package else None,
        "package_code": package.package_code if package else None,
        "destination": package.destination if package else None,
        "duration_days": package.duration_days if package else None,
        "duration_nights": package.duration_nights if package else None,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "travel_start_date": (
            booking.travel_start_date.isoformat() if booking.travel_start_date else None
        ),
        "travel_end_date": (
            booking.travel_end_date.isoformat() if booking.travel_end_date else None
        ),
        "pickup_location": booking.pickup_location,
        "pickup_datetime": (
            booking.pickup_datetime.isoformat() if booking.pickup_datetime else None
        ),
        "persons_count": booking.persons_count,
        "total_amount": float(booking.total_amount),
        "platform_commission": float(booking.platform_commission),
        "partner_payout": float(booking.partner_payout),
        "additional_amount": float(booking.additional_amount or 0),
        "additional_charge_note": booking.additional_charge_note,
        "advance_total": float(booking.advance_total or 0),
        "advance_received_by": booking.advance_received_by,
        "payment_status": booking.payment_status,
        "booking_status": booking.booking_status,
        "invoice_number": booking.invoice_number,
        "invoiced_at": booking.invoiced_at.isoformat() if booking.invoiced_at else None,
        "hotel_details": booking.hotel_details,
        "other_details": booking.other_details,
        "special_requests": booking.special_requests,
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
        "vehicle": (
            {
                "id": vehicle[0].id,
                "registration_number": vehicle[0].registration_number,
                "vehicle_brand": vehicle[0].vehicle_brand,
                "vehicle_model": vehicle[0].vehicle_model,
                "seating_capacity": vehicle[0].seating_capacity,
                "driver_name": vehicle[1] or None,
            }
            if vehicle
            else None
        ),
        "driver": (
            {
                "id": driver.id,
                "full_name": driver.full_name,
                "mobile": driver.mobile,
            }
            if driver
            else None
        ),
        "advances": [
            {
                "id": a.id,
                "receipt_number": a.receipt_number,
                "amount": float(a.amount),
                "payment_mode": a.payment_mode,
                "received_by": a.received_by,
                "reference_number": a.reference_number,
                "notes": a.notes,
                "status": a.status,
                "collected_by_role": a.collected_by_role,
                "collected_at": a.collected_at.isoformat() if a.collected_at else None,
                "void_reason": a.void_reason,
            }
            for a in advances
        ],
        "charges": [
            {
                "id": c.id,
                "label": c.label,
                "amount": float(c.amount),
                "reason": c.reason,
                "added_by_role": c.added_by_role,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in charges
        ],
    }
    if include_fleet and partner_id:
        vehicles = (
            await db.scalars(
                select(Vehicle)
                .where(
                    Vehicle.partner_id == partner_id,
                    Vehicle.status.in_(["ACTIVE", "ON_TRIP"]),
                )
                .order_by(Vehicle.registration_number)
            )
        ).all()
        drivers = (
            await db.scalars(
                select(Driver)
                .where(Driver.partner_id == partner_id, Driver.status.in_(["ACTIVE"]))
                .order_by(Driver.full_name)
            )
        ).all()
        data["fleet"] = {
            "vehicles": [
                {
                    "id": v.id,
                    "registration_number": v.registration_number,
                    "vehicle_brand": v.vehicle_brand,
                    "vehicle_model": v.vehicle_model,
                    "seating_capacity": v.seating_capacity,
                }
                for v in vehicles
            ],
            "drivers": [
                {"id": d.id, "full_name": d.full_name, "mobile": d.mobile}
                for d in drivers
            ],
        }
    return data


async def _get_booking(db: AsyncSession, tour_booking_id: int) -> TourBooking:
    booking = await db.scalar(
        select(TourBooking).where(TourBooking.id == tour_booking_id)
    )
    if not booking:
        raise ResourceNotFoundException("Tour booking", str(tour_booking_id))
    return booking


async def _customer_row(db: AsyncSession, master_booking_id: int) -> Optional[dict]:
    row = (
        (
            await db.execute(
                text(
                    "SELECT c.id, COALESCE(NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), ''), "
                    "'Customer') AS full_name, u.mobile_number AS mobile "
                    "FROM master_bookings mb "
                    "JOIN customers c ON c.id = mb.customer_id "
                    "LEFT JOIN users u ON u.id = c.user_id "
                    "WHERE mb.id = :mb"
                ),
                {"mb": master_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def build_advance_receipt(
    db: AsyncSession, *, tour_booking_id: int, advance_id: int
) -> dict:
    """Assemble the data for a tour advance receipt PDF and build it."""
    from app.modules.tour.services.pdf import build_tour_advance_receipt_pdf

    booking = await _get_booking(db, tour_booking_id)
    adv = await db.scalar(
        select(TourAdvancePayment).where(
            TourAdvancePayment.id == advance_id,
            TourAdvancePayment.tour_booking_id == booking.id,
        )
    )
    if not adv:
        raise ResourceNotFoundException("Tour advance", str(advance_id))
    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    customer = await _customer_row(db, booking.master_booking_id)
    partner_name = (
        await db.scalar(
            select(Partner.business_name).where(Partner.id == package.partner_id)
        )
        if package and package.partner_id
        else None
    )
    return build_tour_advance_receipt_pdf(
        receipt_number=adv.receipt_number,
        booking_number=booking.booking_number,
        package_name=package.package_name if package else "",
        destination=package.destination if package else "",
        travel_start_date=(
            booking.travel_start_date.isoformat() if booking.travel_start_date else None
        ),
        travel_end_date=(
            booking.travel_end_date.isoformat() if booking.travel_end_date else None
        ),
        persons_count=booking.persons_count,
        customer_name=customer["full_name"] if customer else None,
        customer_mobile=customer["mobile"] if customer else None,
        partner_name=partner_name,
        amount=float(adv.amount),
        payment_mode=adv.payment_mode,
        received_by=adv.received_by,
        collected_at=adv.collected_at,
        reference_number=adv.reference_number,
        **await _platform_branding(db),
    )


async def build_invoice(db: AsyncSession, *, tour_booking_id: int) -> dict:
    """Assemble the data for a tour booking invoice PDF and build it."""
    from app.modules.tour.services.pdf import build_tour_invoice_pdf

    booking = await _get_booking(db, tour_booking_id)
    invoice_number = booking.invoice_number or await _next_invoice_number(db)
    booking.invoice_number = invoice_number
    booking.invoiced_at = booking.invoiced_at or datetime.now(timezone.utc)
    await db.flush()

    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    customer = await _customer_row(db, booking.master_booking_id)
    partner_name = (
        await db.scalar(
            select(Partner.business_name).where(Partner.id == package.partner_id)
        )
        if package and package.partner_id
        else None
    )
    charges = (
        await db.scalars(
            select(TourBookingCharge)
            .where(TourBookingCharge.tour_booking_id == booking.id)
            .order_by(TourBookingCharge.created_at)
        )
    ).all()
    total = _q(booking.total_amount)
    paid = _q(booking.advance_total)
    return build_tour_invoice_pdf(
        invoice_number=invoice_number,
        booking_number=booking.booking_number,
        package_name=package.package_name if package else "",
        destination=package.destination if package else "",
        travel_start_date=(
            booking.travel_start_date.isoformat() if booking.travel_start_date else None
        ),
        travel_end_date=(
            booking.travel_end_date.isoformat() if booking.travel_end_date else None
        ),
        persons_count=booking.persons_count,
        customer_name=customer["full_name"] if customer else None,
        customer_mobile=customer["mobile"] if customer else None,
        partner_name=partner_name,
        invoice_date=booking.invoiced_at,
        total_amount=float(total),
        platform_commission=float(booking.platform_commission),
        partner_payout=float(booking.partner_payout),
        additional_amount=float(booking.additional_amount or 0),
        advance_total=float(paid),
        balance_due=float(max(total - paid, 0)),
        charges=[{"label": c.label, "amount": float(c.amount)} for c in charges],
        **await _platform_branding(db),
    )


async def build_itinerary(db: AsyncSession, *, tour_booking_id: int) -> dict:
    """Assemble the data for a tour itinerary PDF (customer + booking +
    day-by-day itinerary + current payment position + assigned fleet)."""
    from app.modules.tour.services.pdf import build_tour_itinerary_pdf

    booking = await _get_booking(db, tour_booking_id)
    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    customer = await _customer_row(db, booking.master_booking_id)

    mb = (
        await db.execute(
            text("SELECT booking_number FROM master_bookings WHERE id = :mb"),
            {"mb": booking.master_booking_id},
        )
    ).scalar_one_or_none()

    city_name = (
        await db.scalar(select(City.name).where(City.id == package.city_id))
        if package
        else None
    )
    partner_name = (
        await db.scalar(
            select(Partner.business_name).where(Partner.id == package.partner_id)
        )
        if package and package.partner_id
        else None
    )

    # Day-by-day itinerary (package catalogue), with overnight city names.
    itinerary_rows = (
        await db.scalars(
            select(TourItinerary)
            .where(TourItinerary.package_id == booking.package_id)
            .order_by(TourItinerary.day_number)
        )
    ).all()
    city_ids = {r.overnight_city_id for r in itinerary_rows if r.overnight_city_id}
    city_names: dict[int, str] = {}
    if city_ids:
        rows = (
            await db.execute(select(City.id, City.name).where(City.id.in_(city_ids)))
        ).all()
        city_names = {int(cid): name for cid, name in rows}
    itinerary = [
        {
            "day_number": r.day_number,
            "title": r.title,
            "description": r.description,
            "activities": r.activities or [],
            "overnight_city": city_names.get(r.overnight_city_id),
        }
        for r in itinerary_rows
    ]

    # Assigned fleet (if any).
    vehicle = None
    if booking.vehicle_id:
        v = await db.scalar(select(Vehicle).where(Vehicle.id == booking.vehicle_id))
        if v:
            vehicle = {
                "registration_number": v.registration_number,
                "vehicle_brand": v.vehicle_brand,
                "vehicle_model": v.vehicle_model,
                "seating_capacity": v.seating_capacity,
            }
    driver = None
    if booking.driver_id:
        d = await db.scalar(select(Driver).where(Driver.id == booking.driver_id))
        if d:
            driver = {"full_name": d.full_name, "mobile": d.mobile}

    total = _q(booking.total_amount)
    paid = _q(booking.advance_total or 0)
    return build_tour_itinerary_pdf(
        booking_number=booking.booking_number,
        master_booking_number=mb,
        package_name=package.package_name if package else "",
        package_code=package.package_code if package else None,
        destination=package.destination if package else "",
        city_name=city_name,
        duration_days=package.duration_days if package else None,
        duration_nights=package.duration_nights if package else None,
        travel_start_date=(
            booking.travel_start_date.isoformat() if booking.travel_start_date else None
        ),
        travel_end_date=(
            booking.travel_end_date.isoformat() if booking.travel_end_date else None
        ),
        persons_count=booking.persons_count,
        pickup_location=booking.pickup_location,
        pickup_datetime=booking.pickup_datetime,
        customer_name=customer["full_name"] if customer else None,
        customer_mobile=customer["mobile"] if customer else None,
        partner_name=partner_name,
        booking_status=booking.booking_status,
        payment_status=booking.payment_status,
        total_amount=float(total),
        additional_amount=float(booking.additional_amount or 0),
        advance_total=float(paid),
        balance_due=float(max(total - paid, 0)),
        invoice_number=booking.invoice_number,
        itinerary=itinerary,
        vehicle=vehicle,
        driver=driver,
        **await _platform_branding(db),
    )


def _ensure_manageable(booking: TourBooking) -> None:
    """Execution edits (trip, fleet, charges) are locked once the trip is
    over or cancelled — but money collection stays open through COMPLETED,
    so the balance can be collected after the trip finishes."""
    if booking.booking_status in ("COMPLETED", "SETTLED", "CANCELLED"):
        raise BusinessException(
            f"Bookings in '{booking.booking_status}' state cannot be edited.",
            code="BOOKING_LOCKED",
        )


def _refresh_payment_status(booking: TourBooking) -> None:
    """Recompute payment_status from what has been collected vs the current
    total (which grows when charges are added or the trip is repriced)."""
    total = _q(booking.total_amount)
    paid = _q(booking.advance_total or 0)
    booking.payment_status = (
        "PAID" if paid >= total else ("PARTIAL" if paid > 0 else "PENDING")
    )


def _ensure_payable(booking: TourBooking) -> None:
    """Money collection is allowed from booking through COMPLETED — it is only
    locked once settled or cancelled. This is what lets the admin/partner
    collect the full balance after the trip is completed."""
    if booking.booking_status in ("SETTLED", "CANCELLED"):
        raise BusinessException(
            f"Payments cannot be collected on '{booking.booking_status}' bookings.",
            code="BOOKING_LOCKED",
        )


# ── Manage payload ──────────────────────────────────────────────────────────


async def get_manage_payload(
    db: AsyncSession, tour_booking_id: int, *, include_fleet: bool = False
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    return await _booking_dict(db, booking, include_fleet=include_fleet)


# ── Advance collection ──────────────────────────────────────────────────────


async def collect_advance(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    amount: Decimal,
    payment_mode: str,
    received_by: str,
    user_id: str,
    source_role: str,
    reference_number: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    _ensure_payable(booking)

    mode = payment_mode.upper()
    receiver = received_by.upper()
    if mode not in ("CASH", "ONLINE", "UPI", "WALLET"):
        raise BusinessException(
            "payment_mode must be CASH, ONLINE, UPI or WALLET", code="INVALID_MODE"
        )
    if receiver not in ("ADMIN", "PARTNER", "DRIVER"):
        raise BusinessException(
            "received_by must be ADMIN, PARTNER or DRIVER", code="INVALID_RECEIVER"
        )
    if receiver != "ADMIN" and mode == "ONLINE":
        raise BusinessException(
            "Online payments can only be received by ADMIN (partner/driver hold cash or UPI).",
            code="INVALID_COLLECTION",
        )
    amount_d = _q(amount)
    if amount_d <= 0:
        raise BusinessException(
            "Advance amount must be positive", code="INVALID_AMOUNT"
        )
    balance_due = max(_q(booking.total_amount) - _q(booking.advance_total or 0), 0)
    if amount_d > balance_due:
        raise BusinessException(
            f"Amount ₹{float(amount_d):,.2f} exceeds the outstanding balance of ₹{float(balance_due):,.2f}.",
            code="OVERPAYMENT",
        )

    receipt_number = await _next_receipt_number(db)
    db.add(
        TourAdvancePayment(
            tour_booking_id=booking.id,
            master_booking_id=booking.master_booking_id,
            receipt_number=receipt_number,
            amount=amount_d,
            payment_mode=mode,
            received_by=receiver,
            reference_number=reference_number,
            notes=notes,
            collected_by_user_id=user_id,
            collected_by_role=source_role,
            collected_at=datetime.now(timezone.utc),
        )
    )
    new_total = _q(booking.advance_total) + amount_d
    booking.advance_total = new_total
    booking.advance_received_by = receiver
    _refresh_payment_status(booking)

    await db.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, 'ADVANCE_COLLECTED', :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "desc": (
                f"Advance of ₹{float(amount_d):,.2f} ({mode}) received by {receiver} on "
                f"{booking.booking_number}. Receipt {receipt_number}."
            ),
        },
    )
    await rollup_tour_totals_into_master(db, booking.master_booking_id)
    await db.flush()
    return {
        "receipt_number": receipt_number,
        "amount": float(amount_d),
        "payment_mode": mode,
        "received_by": receiver,
        "advance_total": float(new_total),
        "balance_after_advance": float(max(_q(booking.total_amount) - new_total, 0)),
        "payment_status": booking.payment_status,
    }


async def void_advance(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    advance_id: int,
    user_id: str,
    reason: str,
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    _ensure_payable(booking)
    adv = await db.scalar(
        select(TourAdvancePayment).where(
            TourAdvancePayment.id == advance_id,
            TourAdvancePayment.tour_booking_id == booking.id,
        )
    )
    if not adv:
        raise ResourceNotFoundException("Tour advance", str(advance_id))
    if adv.status != "ACTIVE":
        raise BusinessException("This advance is already voided", code="ALREADY_VOIDED")

    adv.status = "VOIDED"
    adv.voided_by_user_id = user_id
    adv.void_reason = reason
    adv.voided_at = datetime.now(timezone.utc)

    new_total = max(_q(booking.advance_total) - _q(adv.amount), 0)
    booking.advance_total = new_total
    if new_total <= 0:
        booking.advance_received_by = None
    _refresh_payment_status(booking)
    await db.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, 'ADVANCE_VOIDED', :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "desc": f"Advance {adv.receipt_number} voided on {booking.booking_number}. Reason: {reason}",
        },
    )
    await rollup_tour_totals_into_master(db, booking.master_booking_id)
    await db.flush()
    return {
        "receipt_number": adv.receipt_number,
        "advance_total": float(new_total),
        "payment_status": booking.payment_status,
    }


async def list_advances(db: AsyncSession, tour_booking_id: int) -> list[dict]:
    await _get_booking(db, tour_booking_id)
    rows = (
        await db.scalars(
            select(TourAdvancePayment)
            .where(TourAdvancePayment.tour_booking_id == tour_booking_id)
            .order_by(TourAdvancePayment.created_at.desc())
        )
    ).all()
    return [
        {
            "id": a.id,
            "receipt_number": a.receipt_number,
            "amount": float(a.amount),
            "payment_mode": a.payment_mode,
            "received_by": a.received_by,
            "reference_number": a.reference_number,
            "notes": a.notes,
            "status": a.status,
            "collected_by_role": a.collected_by_role,
            "collected_at": a.collected_at.isoformat() if a.collected_at else None,
            "void_reason": a.void_reason,
        }
        for a in rows
    ]


# ── Vehicle + driver assignment ─────────────────────────────────────────────


async def assign_vehicle_driver(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    vehicle_id: Optional[int],
    driver_id: Optional[int],
    user_id: str,
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    _ensure_manageable(booking)
    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    partner_id = package.partner_id if package else None

    vehicle = None
    if vehicle_id:
        vehicle = await db.scalar(select(Vehicle).where(Vehicle.id == vehicle_id))
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", str(vehicle_id))
        if partner_id and vehicle.partner_id != partner_id:
            raise BusinessException(
                "This vehicle does not belong to the package's partner.",
                code="VEHICLE_NOT_OWNED",
            )
    if driver_id:
        driver = await db.scalar(select(Driver).where(Driver.id == driver_id))
        if not driver:
            raise ResourceNotFoundException("Driver", str(driver_id))
        if partner_id and driver.partner_id != partner_id:
            raise BusinessException(
                "This driver does not belong to the package's partner.",
                code="DRIVER_NOT_OWNED",
            )
        if vehicle and driver.partner_id != vehicle.partner_id:
            raise BusinessException(
                "Driver and vehicle must belong to the same partner.",
                code="MISMATCHED_FLEET",
            )

    booking.vehicle_id = vehicle_id
    booking.driver_id = driver_id
    await db.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, 'FLEET_ASSIGNED', :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "desc": (
                f"Vehicle {vehicle.registration_number if vehicle else '—'} and "
                f"driver assigned on {booking.booking_number}."
            ),
        },
    )
    await db.flush()
    return {
        "vehicle_id": vehicle_id,
        "driver_id": driver_id,
        "vehicle_registration": vehicle.registration_number if vehicle else None,
    }


# ── Trip updates (dates / persons / pickup) with reprice ────────────────────


async def update_trip(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    travel_start_date: Optional[str] = None,
    travel_end_date: Optional[str] = None,
    persons_count: Optional[int] = None,
    pickup_location: Optional[str] = None,
    pickup_datetime: Optional[str] = None,
    hotel_details: Optional[str] = None,
    other_details: Optional[str] = None,
    user_id: str,
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    _ensure_manageable(booking)
    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    if not package:
        raise BusinessException("Package not found for this booking", code="NO_PACKAGE")

    changed: list[str] = []
    if travel_start_date is not None:
        booking.travel_start_date = _date.fromisoformat(travel_start_date)
        changed.append("travel date")
    if travel_end_date is not None:
        booking.travel_end_date = _date.fromisoformat(travel_end_date)
        changed.append("end date")
    if pickup_location is not None:
        booking.pickup_location = (pickup_location or "").strip() or None
        changed.append("pickup location")
    if pickup_datetime is not None:
        booking.pickup_datetime = (
            datetime.fromisoformat(pickup_datetime.replace("Z", "+00:00"))
            if pickup_datetime
            else None
        )
        changed.append("pickup time")
    if hotel_details is not None:
        booking.hotel_details = hotel_details or None
    if other_details is not None:
        booking.other_details = other_details or None

    # Person-count change reprices the package (slab pricing) and re-derives
    # the end date from the start + duration.
    if persons_count is not None and persons_count != booking.persons_count:
        total, commission, payout = await price_for_persons(db, package, persons_count)
        booking.persons_count = persons_count
        booking.total_amount = total
        booking.platform_commission = commission
        booking.partner_payout = payout
        booking.travel_end_date = booking.travel_start_date + timedelta(
            days=package.duration_days - 1
        )
        # The total changed — keep payment status in sync with what was collected.
        _refresh_payment_status(booking)
        changed.append(f"travellers ({persons_count})")

        # Keep the master booking total in sync.
        await db.execute(
            text("UPDATE master_bookings SET total_amount = :amt WHERE id = :mb"),
            {"amt": float(total), "mb": booking.master_booking_id},
        )

    if changed:
        await db.execute(
            text(
                "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
                "VALUES (:mb, 'BOOKING_UPDATED', :desc, NOW())"
            ),
            {
                "mb": booking.master_booking_id,
                "desc": f"{booking.booking_number} updated: {', '.join(changed)}.",
            },
        )
    await db.flush()
    return {
        "travel_start_date": (
            booking.travel_start_date.isoformat() if booking.travel_start_date else None
        ),
        "travel_end_date": (
            booking.travel_end_date.isoformat() if booking.travel_end_date else None
        ),
        "persons_count": booking.persons_count,
        "total_amount": float(booking.total_amount),
        "platform_commission": float(booking.platform_commission),
        "partner_payout": float(booking.partner_payout),
        "pickup_location": booking.pickup_location,
        "pickup_datetime": (
            booking.pickup_datetime.isoformat() if booking.pickup_datetime else None
        ),
    }


# ── Additional charges (trip modifications) ─────────────────────────────────


async def add_charge(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    label: str,
    amount: Decimal,
    reason: Optional[str],
    user_id: str,
    role: str,
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    _ensure_manageable(booking)
    amount_d = _q(amount)
    if amount_d <= 0:
        raise BusinessException("Charge amount must be positive", code="INVALID_AMOUNT")
    if not (label or "").strip():
        raise BusinessException("A charge label is required", code="LABEL_REQUIRED")

    db.add(
        TourBookingCharge(
            tour_booking_id=booking.id,
            label=label.strip(),
            amount=amount_d,
            reason=reason,
            added_by_user_id=user_id,
            added_by_role=role,
        )
    )
    # Split the extra amount by the booking's commission rate so payout and
    # commission stay consistent for settlement.
    total = _q(booking.total_amount)
    commission = _q(booking.platform_commission)
    rate = (commission / total) if total > 0 else Decimal("0")
    add_commission = (amount_d * rate).quantize(Q, rounding=ROUND_HALF_UP)
    add_payout = amount_d - add_commission

    booking.total_amount = total + amount_d
    booking.platform_commission = commission + add_commission
    booking.partner_payout = _q(booking.partner_payout) + add_payout
    booking.additional_amount = _q(booking.additional_amount) + amount_d
    booking.additional_charge_note = label.strip() + (
        f" — {reason.strip()}" if reason and reason.strip() else ""
    )
    # The total grew — a previously PAID booking may now have a balance due.
    _refresh_payment_status(booking)

    await db.execute(
        text("UPDATE master_bookings SET total_amount = :amt WHERE id = :mb"),
        {"amt": float(booking.total_amount), "mb": booking.master_booking_id},
    )
    await db.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, 'CHARGE_ADDED', :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "desc": f"Additional charge ₹{float(amount_d):,.2f} ({label.strip()}) added to {booking.booking_number}.",
        },
    )
    await db.flush()
    return {
        "label": label.strip(),
        "amount": float(amount_d),
        "total_amount": float(booking.total_amount),
        "platform_commission": float(booking.platform_commission),
        "partner_payout": float(booking.partner_payout),
    }


# ── Invoicing ───────────────────────────────────────────────────────────────


async def mark_invoiced(
    db: AsyncSession, *, tour_booking_id: int, user_id: str
) -> dict:
    booking = await _get_booking(db, tour_booking_id)
    invoice_number = booking.invoice_number or await _next_invoice_number(db)
    booking.invoice_number = invoice_number
    booking.invoiced_at = datetime.now(timezone.utc)
    await db.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, 'INVOICE_GENERATED', :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "desc": f"Invoice {invoice_number} generated for {booking.booking_number}.",
        },
    )
    await db.flush()
    return {
        "invoice_number": invoice_number,
        "invoiced_at": booking.invoiced_at.isoformat(),
    }


# ── Settlement (wallet movement) ────────────────────────────────────────────


async def settle_booking(
    db: AsyncSession, *, tour_booking_id: int, user_id: str
) -> dict:
    """Settle a COMPLETED tour booking: net the partner's payout against what
    the partner side physically holds, then credit/debit the wallet.

    net = partner_payout − partner_held
      net > 0 → the partner is owed money  → credit wallet
      net < 0 → the partner is holding ours → debit wallet (must have balance)

    Mirrors the cab/hotel settlement exactly. Does NOT commit — the caller's
    session dependency commits on success.
    """
    booking = await _get_booking(db, tour_booking_id)
    if booking.booking_status != "COMPLETED":
        raise BusinessException(
            f"Cannot settle: booking is '{booking.booking_status}'. Only COMPLETED bookings settle.",
            code="NOT_COMPLETED",
        )
    if not booking.invoice_number:
        raise BusinessException(
            "Generate the invoice before settling.", code="NO_INVOICE"
        )
    if booking.payment_status != "PAID":
        raise BusinessException(
            "Collect the full balance before settling this booking.", code="NOT_PAID"
        )

    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    partner_id = package.partner_id if package else None
    if not partner_id:
        raise BusinessException(
            "This package has no partner assigned. Cannot settle.", code="NO_PARTNER"
        )

    advances = (
        await db.scalars(
            select(TourAdvancePayment).where(
                TourAdvancePayment.tour_booking_id == booking.id,
                TourAdvancePayment.status == "ACTIVE",
            )
        )
    ).all()
    total_paid = sum(_q(a.amount) for a in advances)
    partner_held = sum(
        _q(a.amount) for a in advances if a.received_by in ("PARTNER", "DRIVER")
    )

    payout = _q(booking.partner_payout)
    net = (payout - partner_held).quantize(Q, rounding=ROUND_HALF_UP)

    wallet_row = (
        (
            await db.execute(
                text(
                    "SELECT id, available_balance, wallet_status FROM wallets WHERE partner_id = :pid FOR UPDATE"
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not wallet_row:
        raise BusinessException(
            "Partner does not have a wallet. Cannot process settlement.",
            code="NO_WALLET",
        )
    if wallet_row["wallet_status"] != "ACTIVE":
        raise BusinessException(
            f"Partner wallet is '{wallet_row['wallet_status']}'. Cannot process settlement.",
            code="WALLET_INACTIVE",
        )
    wallet_id = wallet_row["id"]
    balance = float(wallet_row["available_balance"] or 0)

    ref = booking.booking_number
    workings = (
        f"Payout ₹{float(payout):.2f} | Collected ₹{float(total_paid):.2f} "
        f"(partner holds ₹{float(partner_held):.2f}) | Net = ₹{float(net):.2f}"
    )
    new_bal = balance
    settlement_note = ""
    if net > 0:
        new_bal = round(balance + float(net), 2)
        await db.execute(
            text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
            {"bal": new_bal, "wid": wallet_id},
        )
        await db.execute(
            text(
                "INSERT INTO wallet_ledger (wallet_id, transaction_reference, reference_type, debit_amount, "
                "credit_amount, balance_after, narration, created_at) "
                "VALUES (:wid, :ref, 'SETTLEMENT', 0, :credit, :bal_after, :narration, NOW())"
            ),
            {
                "wid": wallet_id,
                "ref": ref,
                "credit": float(net),
                "bal_after": new_bal,
                "narration": f"Tour settlement credit — {ref} | {workings}",
            },
        )
        settlement_note = f"₹{float(net):,.2f} credited to partner wallet. Partner wallet balance: ₹{new_bal:,.2f}."
    elif net < 0:
        owed = abs(float(net))
        if balance < owed:
            raise BusinessException(
                f"Insufficient partner wallet balance to settle. Required: ₹{owed:,.2f} | "
                f"Partner wallet: ₹{balance:,.2f}. Partner must top up before settlement.",
                code="INSUFFICIENT_WALLET",
            )
        new_bal = round(balance - owed, 2)
        await db.execute(
            text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
            {"bal": new_bal, "wid": wallet_id},
        )
        await db.execute(
            text(
                "INSERT INTO wallet_ledger (wallet_id, transaction_reference, reference_type, debit_amount, "
                "credit_amount, balance_after, narration, created_at) "
                "VALUES (:wid, :ref, 'COMMISSION_DEBIT', :debit, 0, :bal_after, :narration, NOW())"
            ),
            {
                "wid": wallet_id,
                "ref": ref,
                "debit": owed,
                "bal_after": new_bal,
                "narration": f"Tour settlement debit — {ref} | {workings}",
            },
        )
        settlement_note = (
            f"₹{owed:,.2f} debited from partner wallet — partner side holds "
            f"₹{float(partner_held):,.2f} against a payout of ₹{float(payout):,.2f}. "
            f"Partner wallet balance: ₹{new_bal:,.2f}."
        )
    else:
        settlement_note = "No wallet movement — what the partner holds exactly matches what they are owed."

    booking.booking_status = "SETTLED"
    booking.payment_status = "PAID"
    # Master booking status closes like cab/hotel settlement; the tour service
    # status stays SETTLED (displayed in the "Service Status" column).
    await db.execute(
        text(
            "UPDATE master_bookings SET booking_status = 'CLOSED', payment_status = 'PAID' WHERE id = :mb"
        ),
        {"mb": booking.master_booking_id},
    )
    await db.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, 'BOOKING_SETTLED', :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "desc": f"{booking.booking_number} settled with partner. {settlement_note}",
        },
    )
    await db.flush()

    # Real-time fan-out: refresh tour + master booking pages on both portals.
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
            service_status="SETTLED",
            action="SETTLE",
            payment_status="PAID",
            partner_ids=[partner_id],
        )
    except Exception:  # pragma: no cover - best-effort
        pass

    return {
        "booking_status": booking.booking_status,
        "net_settlement": float(net),
        "wallet_direction": "CREDIT" if net > 0 else ("DEBIT" if net < 0 else "NONE"),
        "wallet_balance_after": new_bal,
        "settlement_note": settlement_note,
    }


__all__ = [
    "get_manage_payload",
    "collect_advance",
    "void_advance",
    "list_advances",
    "assign_vehicle_driver",
    "update_trip",
    "add_charge",
    "mark_invoiced",
    "settle_booking",
    "build_advance_receipt",
    "build_invoice",
    "build_itinerary",
    "price_for_persons",
]
