# ============================================================
# WAYTERO — PARTNER HOTEL BOOKING API
# File: app/modules/partner/hotel_booking_api.py
# Prefix: /partners  (registered in api/router.py)
# Doc Ref: BRD Part 4 §57-92, DB Schema Part 5 §12
# ============================================================

import json
import math
import re
import uuid as _uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, text as _text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_partner
from app.modules.booking.models import MasterBooking, BookingTimeline
from app.modules.hotel.models import (
    HotelReservation,
    Hotel,
    HotelRoomCategory,
    HotelRoom,
    HotelCheckin,
    HotelAdvancePayment,
)
from app.modules.hotel.services import billing as hotel_billing
from app.modules.hotel.services.quote import compute_hotel_quote, quote_to_response
from app.modules.hotel.services.hotel_helpers import (
    check_in_date_mismatch as _check_in_date_mismatch,
)
from app.modules.customer.models import Customer
from app.modules.partner.models import Partner
from app.modules.auth.models.user import User
from app.shared.responses.base import success_response

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _resolve_partner_id(db: AsyncSession, user_uuid: str) -> int:
    from uuid import UUID

    p = (
        await db.execute(select(Partner).where(Partner.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Partner profile not found.")
    return p.id


async def _get_hotel_reservation(
    db: AsyncSession, reservation_id: int, partner_id: int
) -> HotelReservation:
    hr = (
        await db.execute(
            select(HotelReservation).where(HotelReservation.id == reservation_id)
        )
    ).scalar_one_or_none()
    if not hr:
        raise HTTPException(404, "Hotel reservation not found.")
    hotel = (
        await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
    ).scalar_one_or_none()
    if not hotel or int(hotel.partner_id) != partner_id:
        raise HTTPException(403, "This reservation does not belong to your hotel.")
    return hr


async def _notify_hotel_update(
    db: AsyncSession,
    hr: HotelReservation,
    partner_id: int,
    *,
    action: str,
    payment_collected_status: Optional[str] = None,
) -> None:
    """Best-effort WS fan-out after a partner-side hotel lifecycle mutation.

    Fans out to every online admin user (whose hotel manage page must refresh)
    and to the partner's own user id (so the partner's other open tabs stay in
    step). Never awaited as a hard dependency by the caller.
    """
    from app.modules.notification.services.booking_notifications import (
        notify_hotel_booking_updated,
    )

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
        # Notification failures must never fail the HTTP response.
        pass


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


async def _customer_email_context(
    db: AsyncSession, hr: HotelReservation
) -> Optional[tuple]:
    """(email, name, hotel_name) for the reservation's customer — or None."""
    row = (
        await db.execute(
            select(
                User.email, Customer.first_name, Customer.last_name, Hotel.hotel_name
            )
            .select_from(MasterBooking)
            .join(Customer, Customer.id == MasterBooking.customer_id)
            .join(User, User.id == Customer.user_id)
            .join(
                HotelReservation, HotelReservation.master_booking_id == MasterBooking.id
            )
            .join(Hotel, Hotel.id == HotelReservation.hotel_id)
            .where(HotelReservation.id == hr.id)
        )
    ).first()
    if not row or not row.email:
        return None
    name = " ".join(filter(None, [row.first_name, row.last_name])) or "there"
    return row.email, name, row.hotel_name


async def _send_hotel_email(
    db: AsyncSession,
    hr: HotelReservation,
    event_type: str,
    *,
    message: str,
    extra_details: Optional[list] = None,
) -> None:
    """Generic customer email for a hotel reservation. Never raises — email
    failures are logged by the engine, not propagated."""
    try:
        from app.infrastructure.email import send_event_email

        info = await _customer_email_context(db, hr)
        if not info:
            return
        to_email, name, hotel_name = info
        details = [
            ("Hotel", hotel_name or ""),
            (
                "Check-in",
                hr.check_in_date.strftime("%d %b %Y") if hr.check_in_date else "",
            ),
            (
                "Check-out",
                hr.check_out_date.strftime("%d %b %Y") if hr.check_out_date else "",
            ),
            ("Rooms", str(hr.rooms_count or "")),
        ]
        if extra_details:
            details = details + [tuple(d) for d in extra_details]
        await send_event_email(
            db,
            event_type=event_type,
            to_email=to_email,
            to_name=name,
            context={
                "name": name,
                "service": "Hotel",
                "booking_number": hr.reservation_number or str(hr.id),
                "message": message,
                "details": details,
                **(
                    {"amount": float(hr.total_amount), "amount_label": "Stay amount"}
                    if hr.total_amount
                    else {}
                ),
            },
            related_type="HOTEL_RESERVATION",
            related_id=hr.id,
        )
    except Exception:  # pragma: no cover — email must never break bookings
        pass


async def _send_hotel_confirmation_email(
    db: AsyncSession, hr: HotelReservation
) -> None:
    """Confirmation email to the customer after a partner confirms/accepts.
    Never raises — email failures are logged, not propagated."""
    await _send_hotel_email(
        db,
        hr,
        "booking_confirmed",
        message=(
            "Your hotel booking is confirmed. Show the booking number at "
            "check-in and keep your ID proof handy."
        ),
    )


async def _sync_reservation_to_master(db: AsyncSession, hr: HotelReservation) -> None:
    """Roll hotel reservation totals & collections up onto the umbrella master booking.

    Partner endpoints mutate `hotel_reservations.total_amount` and insert ACTIVE
    advances without ever touching the master row. The shared helper
    `app.modules.booking.services.rollup_hotel_totals_into_master` does the
    re-roll, but it needs an instance of the master loaded — fetch and call it
    here so the admin `/bookings` list reflects the partner-collected payments
    too.
    """
    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == hr.master_booking_id)
        )
    ).scalar_one_or_none()
    if mb is None:
        return
    # Local import to avoid a circular import at module load.
    from app.modules.booking.services import rollup_hotel_totals_into_master

    await rollup_hotel_totals_into_master(db, hr.master_booking_id, mb)


async def _offer_coupon_snapshot(db: AsyncSession, hr: HotelReservation) -> tuple:
    """(offer, coupon) display snapshot for the accept banner / detail rows
    (migration 0077). coupon_usages is the immutable financial truth."""
    from sqlalchemy import text as _text

    offer = None
    if getattr(hr, "offer_id", None):
        orow = (
            (
                await db.execute(
                    _text("SELECT title, slug, image_url FROM offers WHERE id = :oid"),
                    {"oid": hr.offer_id},
                )
            )
            .mappings()
            .first()
        )
        if orow:
            offer = dict(orow)
    coupon = None
    crow = (
        (
            await db.execute(
                _text(
                    """
                    SELECT co.coupon_code, cu.coupon_type, cu.discount_applied,
                           cu.partner_share, cu.platform_share
                    FROM coupon_usages cu JOIN coupons co ON co.id = cu.coupon_id
                    WHERE cu.hotel_reservation_id = :rid
                    """
                ),
                {"rid": hr.id},
            )
        )
        .mappings()
        .first()
    )
    if crow:
        coupon = {
            "code": crow["coupon_code"],
            "type": crow["coupon_type"],
            "discount_amount": float(crow["discount_applied"]),
            "partner_share": float(crow["partner_share"]),
            "platform_share": float(crow["platform_share"]),
        }
    return offer, coupon


async def _build_detail(
    hr: HotelReservation, db: AsyncSession, *, recompute_bill: bool = False
) -> dict:
    from app.modules.hotel.services import platform_gst_enabled

    hotel = (
        await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
    ).scalar_one_or_none()
    hotel_name = hotel.hotel_name if hotel else None
    hotel_address = hotel.address if hotel else None

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

    checkin_rec = (
        await db.execute(
            select(HotelCheckin).where(HotelCheckin.reservation_id == hr.id)
        )
    ).scalar_one_or_none()

    advances = await hotel_billing.list_advances(db, hr.id)
    advance_total = hotel_billing.total_advance(advances)
    # ── Real-world bill ──
    # Detail view only: for a closed stay the recomputed bill (nights actually
    # stayed) is authoritative, so collect-payment / invoice amounts always
    # agree with what the endpoint validates against.
    _bill_total = None
    _bill_balance = None
    if recompute_bill and hr.reservation_status in {
        "CHECKED_OUT",
        "COMPLETED",
        "SETTLED",
    }:
        _bill = await hotel_billing.build_bill(db, hr)
        _bill_total = _bill.grand_total
        _bill_balance = _bill.balance_due
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

    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == hr.master_booking_id)
        )
    ).scalar_one_or_none()
    customer_name = customer_mobile = None
    if mb and mb.customer_id:
        cust = (
            await db.execute(select(Customer).where(Customer.id == mb.customer_id))
        ).scalar_one_or_none()
        if cust:
            customer_name = cust.full_name
            cust_user = (
                await db.execute(select(User).where(User.id == cust.user_id))
            ).scalar_one_or_none()
            if cust_user:
                customer_mobile = cust_user.mobile_number

    gst_on = await platform_gst_enabled(db)

    # ── Platform-local stamps ──
    # The portal partner may be browsing from anywhere; the actual stay
    # happens in the platform's operating zone (PLATFORM_TIMEZONE, default
    # Asia/Kolkata). The UI shows the local-side string so the partner sees
    # the same wall-clock a guest would read on the invoice.
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
    offer_snap, coupon_snap = await _offer_coupon_snapshot(db, hr)

    return {
        "id": hr.id,
        "reservation_number": hr.reservation_number or f"HR-{hr.id}",
        "master_booking_id": hr.master_booking_id,
        "hotel_id": hr.hotel_id,
        "hotel_name": hotel_name,
        "hotel_address": hotel_address,
        "room_category_name": room_category_name,
        "room_type": room_type,
        "check_in_date": hr.check_in_date.isoformat() if hr.check_in_date else None,
        "check_out_date": hr.check_out_date.isoformat() if hr.check_out_date else None,
        "nights": hr.nights,
        "rooms_count": hr.rooms_count,
        "adults_count": hr.adults_count,
        "children_count": hr.children_count,
        "extra_beds": 0,
        "special_requests": hr.special_requests,
        "base_amount": float(hr.base_amount or 0),
        # Zero is a real, reportable amount here — never collapse it to null, or the
        # UI cannot tell "no tax charged" apart from "tax unknown".
        "taxes_amount": float(hr.gst_amount or 0),
        "gst_percent": float(hr.gst_percent or 0),
        "is_tax_invoice": bool(hr.is_tax_invoice),
        # The platform-wide GST switch (system_configurations.GST_ENABLED). While it
        # is off no tax is charged, so the UI must hide the tax row entirely.
        "gst_enabled": gst_on,
        "additional_charges": float(hr.extra_charges or 0),
        "overtime_hours": float(hr.overtime_hours or 0),
        "overtime_charge": float(hr.overtime_charge or 0),
        "discount_amount": float(hr.discount_amount or 0),
        "coupon_code": hr.coupon_code,
        "coupon_discount": float(hr.coupon_discount or 0),
        "offer": offer_snap,
        "coupon": coupon_snap,
        # For a closed stay the recomputed bill (nights actually stayed) is
        # authoritative — legacy rows checked out before the recompute feature
        # stored the booked amount, which would disagree with what
        # collect-payment validates against.
        "final_amount": (
            float(_bill_total)
            if _bill_total is not None
            else float(hr.total_amount or 0)
        ),
        "actual_check_in_at": (
            hr.actual_check_in_at.isoformat() if hr.actual_check_in_at else None
        ),
        "actual_check_out_at": (
            hr.actual_check_out_at.isoformat() if hr.actual_check_out_at else None
        ),
        # Platform-local ISO stamps for the UI — the partner always reads the
        # time the same way the guest will, regardless of the device's zone.
        "check_in_at_local": check_in_at_local,
        "check_out_at_local": check_out_at_local,
        "platform_timezone": platform_tz_name,
        "check_in_id_proof": hr.check_in_id_proof,
        "reservation_status": hr.reservation_status,
        "hotel_confirmation_number": hr.hotel_confirmation_number,
        "cancellation_reason": hr.cancellation_reason,
        "cancellation_charge": (
            float(hr.cancellation_charge) if hr.cancellation_charge else None
        ),
        "refund_amount": float(hr.refund_amount) if hr.refund_amount else None,
        "allocated_rooms": checkin_rec.allocated_rooms if checkin_rec else None,
        "invoice_number": getattr(hr, "invoice_number", None),
        "invoice_url": getattr(hr, "invoice_url", None),
        "payment_collected_status": getattr(hr, "payment_collected_status", "PENDING"),
        "payment_collected_by": getattr(hr, "payment_collected_by", None),
        "advance_payments": advance_rows,
        "total_advance_paid": float(advance_total),
        "balance_due": (
            float(_bill_balance)
            if _bill_balance is not None
            else float(
                max(Decimal("0"), (hr.total_amount or Decimal("0")) - advance_total)
            )
        ),
        "customer_name": customer_name,
        "customer_mobile": customer_mobile,
        "created_at": hr.created_at.isoformat(),
    }


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class PartnerConfirmRequest(BaseModel):
    hotel_confirmation_number: Optional[str] = None


class PartnerCheckInRequest(BaseModel):
    id_proof: str
    id_number: str
    # Mandatory — real-world billing is driven by the recorded check-in stamp.
    actual_check_in_at: datetime
    room_ids: List[int] = []
    remarks: Optional[str] = None
    # Set to True by the modal when the user explicitly confirms a check-in
    # date that is more than 1 day away from the booked check-in date. The
    # backend still writes a remarks note recording the override so the
    # mismatch is visible on the timeline.
    confirm_date_mismatch: bool = False


class PartnerCheckOutRequest(BaseModel):
    additional_charges: float = 0.0
    check_out_notes: Optional[str] = None
    # Mandatory — room charge and overtime are computed from the actual
    # check-in → check-out window.
    actual_check_out_at: datetime


class PartnerAddChargesRequest(BaseModel):
    amount: float = Field(..., gt=0)
    description: str


class PartnerRecordAdvanceRequest(BaseModel):
    amount: float = Field(..., gt=0)
    payment_mode: str = Field(..., description="UPI | CASH | ONLINE | WALLET")
    reference_number: Optional[str] = None
    notes: Optional[str] = None


class PartnerCollectPaymentRequest(BaseModel):
    amount: float = Field(..., gt=0)
    payment_mode: str = Field(..., description="UPI | CASH | ONLINE")
    reference_number: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# LIST — GET /partners/me/hotel-bookings
# ════════════════════════════════════════════════════════════════


@router.get("/me/hotel-bookings")
async def list_hotel_bookings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    # All hotels belonging to this partner
    hotel_ids_q = select(Hotel.id).where(Hotel.partner_id == partner_id)
    hotel_ids = (await db.execute(hotel_ids_q)).scalars().all()

    q = select(HotelReservation).where(HotelReservation.hotel_id.in_(hotel_ids))
    if status:
        q = q.where(HotelReservation.reservation_status == status.upper())

    if search:
        s = f"%{search}%"
        matching_customers = (
            (
                await db.execute(
                    select(Customer.id)
                    .join(User, User.id == Customer.user_id)
                    .where(
                        (Customer.first_name + " " + Customer.last_name).ilike(s)
                        | User.mobile_number.ilike(s)
                    )
                )
            )
            .scalars()
            .all()
        )
        matching_master_ids = (
            (
                await db.execute(
                    select(MasterBooking.id).where(
                        MasterBooking.customer_id.in_(matching_customers)
                    )
                )
            )
            .scalars()
            .all()
        )
        q = q.where(
            HotelReservation.reservation_number.ilike(s)
            | HotelReservation.master_booking_id.in_(matching_master_ids)
        )

    total = (
        await db.execute(select(func.count()).select_from(q.subquery()))
    ).scalar() or 0
    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                q.order_by(desc(HotelReservation.created_at))
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for hr in rows:
        hotel = (
            await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
        ).scalar_one_or_none()
        mb = (
            await db.execute(
                select(MasterBooking).where(MasterBooking.id == hr.master_booking_id)
            )
        ).scalar_one_or_none()
        customer_name = customer_mobile = None
        if mb and mb.customer_id:
            cust = (
                await db.execute(select(Customer).where(Customer.id == mb.customer_id))
            ).scalar_one_or_none()
            if cust:
                customer_name = cust.full_name
                cu = (
                    await db.execute(select(User).where(User.id == cust.user_id))
                ).scalar_one_or_none()
                if cu:
                    customer_mobile = cu.mobile_number
        items.append(
            {
                "id": hr.id,
                "reservation_number": hr.reservation_number or f"HR-{hr.id}",
                "master_booking_id": hr.master_booking_id,
                "hotel_id": hr.hotel_id,
                "hotel_name": hotel.hotel_name if hotel else None,
                "hotel_code": hotel.hotel_code if hotel else None,
                "city_name": None,
                "customer_name": customer_name,
                "customer_mobile": customer_mobile,
                "check_in_date": (
                    hr.check_in_date.isoformat() if hr.check_in_date else None
                ),
                "check_out_date": (
                    hr.check_out_date.isoformat() if hr.check_out_date else None
                ),
                "nights": hr.nights,
                "rooms_count": hr.rooms_count,
                "adults_count": hr.adults_count,
                "total_amount": float(hr.total_amount or 0),
                "reservation_status": hr.reservation_status,
                "payment_collected_status": getattr(
                    hr, "payment_collected_status", "PENDING"
                ),
                "invoice_number": getattr(hr, "invoice_number", None),
                "created_at": hr.created_at.isoformat(),
            }
        )

    return success_response(
        "Hotel bookings fetched",
        {
            "items": items,
            "total": total,
            "page": page,
            "pages": math.ceil(total / page_size) if total > 0 else 1,
        },
    )


# ════════════════════════════════════════════════════════════════
# Hotel switch / split-stay partner read-only endpoints
# Doc Ref: Hotel Switch Spec; Migration: 0042_hotel_switch
#
# Partners don't initiate switches (the admin / customer-care flow does), but
# they do need to see them — both on the affected reservation's detail page
# (a yellow banner saying "guest moved to a different hotel on day X") and as
# a history list to audit all switches that touched any of their properties.
#
# IMPORTANT — route ordering: the two literal-path routes below MUST be
# declared ABOVE `@router.get("/me/hotel-bookings/{reservation_id}")`.
# FastAPI matches in registration order, so if the parameterized route comes
# first a request for `/me/hotel-bookings/switch-history` would try to coerce
# `"switch-history"` into `int` and return 422 before falling through.
# ════════════════════════════════════════════════════════════════


@router.get("/me/hotel-bookings/{reservation_id}/switch-banner")
async def partner_hotel_switch_banner(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Banner payload for a single reservation. Always returns 200; the UI
    shows nothing when `is_split_stay` is false (the common case). When the
    reservation is the original of a switch we surface the successor's
    reservation_number; when it's the successor, we surface the original's."""
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if not getattr(hr, "is_split_stay", False):
        return success_response(
            "No split banner",
            {"is_split_stay": False},
        )

    # Try to find the related side via original_reservation_id (this is the
    # successor) OR via the audit table (this is the original).
    related_id = getattr(hr, "original_reservation_id", None)
    related_number = None
    if related_id:
        rel = (
            await db.execute(
                select(HotelReservation.reservation_number).where(
                    HotelReservation.id == related_id
                )
            )
        ).scalar_one_or_none()
        related_number = rel

    if related_id is None:
        succ = (
            (
                await db.execute(
                    select(HotelReservation.reservation_number).where(
                        HotelReservation.original_reservation_id == hr.id
                    )
                )
            )
            .scalars()
            .first()
        )
        related_number = succ

    # Net advance remaining on this reservation = sum(active advances) -
    # sum(refunds). `refund_pending` counts how many advances are still flagged
    # MANUAL_PENDING so the partner UI can show "Refund not yet recorded by
    # admin" badges.
    from sqlalchemy import text as _text_banner

    adv_row = (
        (
            await db.execute(
                _text_banner(
                    "SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) AS net, "
                    "       COALESCE(SUM(CASE WHEN status = 'ACTIVE' "
                    "                            AND (amount - COALESCE(refunded_amount, 0)) > 0 "
                    "                            AND COALESCE(reference_number, '') = 'MANUAL_PENDING' "
                    "                       THEN 1 ELSE 0 END), 0) AS refund_pending "
                    "FROM hotel_advance_payments WHERE hotel_reservation_id = :rid"
                ),
                {"rid": int(hr.id)},
            )
        )
        .mappings()
        .one()
    )

    return success_response(
        "Switch banner",
        {
            "is_split_stay": True,
            "original_reservation_id": getattr(hr, "original_reservation_id", None),
            "related_reservation_number": related_number,
            "switched_at": (
                hr.switched_at.isoformat() if getattr(hr, "switched_at", None) else None
            ),
            "switched_reason": getattr(hr, "switched_reason", None),
            "split_advance_strategy": getattr(hr, "split_advance_strategy", None),
            "advance_remaining": float(adv_row["net"] or 0),
            "refund_pending": int(adv_row["refund_pending"] or 0),
        },
    )


@router.get("/me/hotel-bookings/switch-history")
async def partner_hotel_switch_history(
    hotel_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Paginated list of switch / split events touching any reservation in this
    partner's hotels. Optional `hotel_id` filter restricts to a single property.
    Empty list (with a friendly message) when the partner has never seen a
    switch — the UI shows the empty-state copy."""
    from sqlalchemy import text as _text

    partner_id = await _resolve_partner_id(db, current_user["sub"])

    hotel_ids_q = select(Hotel.id).where(Hotel.partner_id == partner_id)
    partner_hotel_ids = (await db.execute(hotel_ids_q)).scalars().all()
    if not partner_hotel_ids:
        return success_response(
            "No switch history",
            {"items": [], "page": page, "page_size": page_size, "total": 0},
        )

    where = ["(orig.hotel_id = ANY(:hotel_ids) OR new.hotel_id = ANY(:hotel_ids))"]
    params: dict = {"hotel_ids": list(partner_hotel_ids)}
    if hotel_id is not None:
        if int(hotel_id) not in {int(h) for h in partner_hotel_ids}:
            raise HTTPException(403, "This hotel does not belong to your account.")
        where = ["(orig.hotel_id = :hotel_id OR new.hotel_id = :hotel_id)"]
        params = {"hotel_id": int(hotel_id)}

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    rows = (
        (
            await db.execute(
                _text(
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
            _text(
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
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    return success_response(
        "Switch history fetched",
        {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": int(total or 0),
        },
    )


# ════════════════════════════════════════════════════════════════
# DETAIL — GET /partners/me/hotel-bookings/{reservation_id}
# ════════════════════════════════════════════════════════════════


@router.get("/me/hotel-bookings/{reservation_id}")
async def get_hotel_booking(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)
    return success_response(
        "Hotel booking detail", await _build_detail(hr, db, recompute_bill=True)
    )


# ════════════════════════════════════════════════════════════════
# CONFIRM — POST /partners/me/hotel-bookings/{reservation_id}/confirm
# Allowed from: AWAITING_HOTEL_CONFIRMATION
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/confirm")
async def partner_hotel_confirm(
    reservation_id: int,
    payload: PartnerConfirmRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

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
        hr.master_booking_id,
        "HOTEL_CONFIRMED",
        f"Hotel booking {hr.reservation_number or hr.id} confirmed by partner. "
        f"Confirmation#: {hr.hotel_confirmation_number or 'N/A'}",
    )
    await db.commit()
    await _send_hotel_confirmation_email(db, hr)
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_CONFIRMED")
    return success_response("Reservation confirmed", {"hotel_status": "CONFIRMED"})


# ════════════════════════════════════════════════════════════════
# ACCEPT — POST /partners/me/hotel-bookings/{reservation_id}/accept
# Realtime popup action: partner accepts a fresh website reservation.
# Allowed from: PENDING_PAYMENT / AWAITING_HOTEL_CONFIRMATION
# ════════════════════════════════════════════════════════════════


class PartnerHotelRejectRequest(BaseModel):
    reason: str = Field(..., min_length=2, max_length=500)


@router.post("/me/hotel-bookings/{reservation_id}/accept")
async def partner_hotel_accept_booking(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Partner accepts a website hotel booking (fixed assignment — no admin
    step in between). Moves PENDING_PAYMENT / AWAITING_HOTEL_CONFIRMATION
    straight to CONFIRMED so the partner can start managing it."""
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status not in ("PENDING_PAYMENT", "AWAITING_HOTEL_CONFIRMATION"):
        raise HTTPException(
            400,
            f"Cannot accept: status is '{hr.reservation_status}', "
            f"expected PENDING_PAYMENT or AWAITING_HOTEL_CONFIRMATION.",
        )

    hr.reservation_status = "CONFIRMED"
    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_CONFIRMED",
        f"Hotel booking {hr.reservation_number or hr.id} accepted by partner.",
    )
    await db.commit()
    await _send_hotel_confirmation_email(db, hr)
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_ACCEPTED")

    # Notify the partner's other tabs so their accept popup closes too.
    try:
        from app.modules.notification.realtime import manager as _rt

        await _rt.send_to_user(
            current_user["sub"],
            {
                "event": "BOOKING_PARTNER_RESPONDED",
                "data": {
                    "service_type": "HOTEL",
                    "reservation_id": hr.id,
                    "master_booking_id": hr.master_booking_id,
                    "decision": "ACCEPTED",
                },
            },
        )
    except Exception as _ws_exc:  # pragma: no cover
        import logging as _log

        _log.getLogger("waytero.partner").warning(
            "hotel_accept.ws_notify_failed err=%s", _ws_exc
        )

    # Tell online admins the booking is resolved so their accept modal
    # drops it + the ringtone stops (they don't need to accept anymore).
    try:
        from app.modules.notification.services.booking_notifications import (
            notify_admins_booking_resolved,
        )

        await notify_admins_booking_resolved(
            db,
            service_type="HOTEL",
            master_booking_id=hr.master_booking_id,
            service_number=hr.reservation_number or str(hr.id),
            decision="CONFIRMED",
        )
    except Exception:  # pragma: no cover - best-effort
        pass

    return success_response(
        "Reservation accepted", {"hotel_status": "CONFIRMED", "reservation_id": hr.id}
    )


# ════════════════════════════════════════════════════════════════
# REJECT — POST /partners/me/hotel-bookings/{reservation_id}/reject
# Allowed from: PENDING_PAYMENT / AWAITING_HOTEL_CONFIRMATION
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/reject")
async def partner_hotel_reject_booking(
    reservation_id: int,
    payload: PartnerHotelRejectRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Partner rejects a website hotel booking — moves it to CANCELLED so
    admin can step in (refund / reassign / follow up)."""
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status not in ("PENDING_PAYMENT", "AWAITING_HOTEL_CONFIRMATION"):
        raise HTTPException(
            400,
            f"Cannot reject: status is '{hr.reservation_status}', "
            f"expected PENDING_PAYMENT or AWAITING_HOTEL_CONFIRMATION.",
        )

    hr.reservation_status = "CANCELLED"
    hr.cancellation_reason = payload.reason.strip()
    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_REJECTED",
        f"Hotel booking {hr.reservation_number or hr.id} rejected by partner. Reason: {payload.reason.strip()}",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_REJECTED")

    try:
        from app.modules.notification.realtime import manager as _rt

        await _rt.send_to_user(
            current_user["sub"],
            {
                "event": "BOOKING_PARTNER_RESPONDED",
                "data": {
                    "service_type": "HOTEL",
                    "reservation_id": hr.id,
                    "master_booking_id": hr.master_booking_id,
                    "decision": "REJECTED",
                },
            },
        )
    except Exception as _ws_exc:  # pragma: no cover
        import logging as _log

        _log.getLogger("waytero.partner").warning(
            "hotel_reject.ws_notify_failed err=%s", _ws_exc
        )

    try:
        from app.modules.notification.services.booking_notifications import (
            notify_admins_booking_resolved,
        )

        await notify_admins_booking_resolved(
            db,
            service_type="HOTEL",
            master_booking_id=hr.master_booking_id,
            service_number=hr.reservation_number or str(hr.id),
            decision="CANCELLED",
        )
    except Exception:  # pragma: no cover - best-effort
        pass

    return success_response(
        "Reservation rejected", {"hotel_status": "CANCELLED", "reservation_id": hr.id}
    )


# ════════════════════════════════════════════════════════════════
# CHECK-IN — POST /partners/me/hotel-bookings/{reservation_id}/check-in
# Allowed from: CONFIRMED
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/check-in")
async def partner_hotel_check_in(
    reservation_id: int,
    payload: PartnerCheckInRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status != "CONFIRMED":
        raise HTTPException(
            400,
            f"Cannot check in: status is '{hr.reservation_status}', expected CONFIRMED.",
        )

    valid_id_proofs = {"AADHAAR", "PASSPORT", "DL"}
    if payload.id_proof.upper() not in valid_id_proofs:
        raise HTTPException(
            400, f"Invalid id_proof. Valid: {', '.join(valid_id_proofs)}"
        )
    if not payload.id_number.strip():
        raise HTTPException(400, "ID number is required.")

    # ── Check-in date guard ────────────────────────────────────────────────
    # The check is done BEFORE we mutate the row so a 409 response leaves the
    # reservation still in CONFIRMED. The mismatch descriptor is rendered in
    # the partner-portal modal; the user must click "Confirm anyway" to set
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
    # the partner already typed is preserved.
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
                errors.append(f"Room {r.room_number} is not in the booked category.")
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

    checkin_record = HotelCheckin(
        reservation_id=hr.id,
        check_in_datetime=hr.actual_check_in_at,
        allocated_rooms=allocated_rooms_json,
        remarks=checkin_remarks,
    )
    db.add(checkin_record)

    rooms_detail = (
        f" Rooms: {', '.join(allocated_room_numbers)}" if allocated_room_numbers else ""
    )
    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_CHECKED_IN",
        f"Partner checked in guest for {hr.reservation_number or hr.id}. "
        f"ID: {hr.check_in_id_proof} #{hr.check_in_id_number}.{rooms_detail}",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_CHECKED_IN")
    return success_response(
        "Guest checked in",
        {
            "hotel_status": "CHECKED_IN",
            "allocated_rooms": allocated_room_numbers,
        },
    )


# ════════════════════════════════════════════════════════════════
# IN-HOUSE — POST /partners/me/hotel-bookings/{reservation_id}/in-house
# Allowed from: CHECKED_IN
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/in-house")
async def partner_hotel_in_house(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status != "CHECKED_IN":
        raise HTTPException(
            400,
            f"Cannot mark in-house: status is '{hr.reservation_status}', expected CHECKED_IN.",
        )

    hr.reservation_status = "IN_HOUSE"
    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_IN_HOUSE",
        f"Hotel booking {hr.reservation_number or hr.id} — guest marked in-house by partner.",
    )
    await db.commit()
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_IN_HOUSE")
    return success_response("Guest marked in-house", {"hotel_status": "IN_HOUSE"})


# ════════════════════════════════════════════════════════════════
# ADD CHARGES — POST /partners/me/hotel-bookings/{reservation_id}/add-charges
# Allowed when: CHECKED_IN | IN_HOUSE | CHECKED_OUT
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/add-charges")
async def partner_hotel_add_charges(
    reservation_id: int,
    payload: PartnerAddChargesRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    allowed = {"CHECKED_IN", "IN_HOUSE", "CHECKED_OUT"}
    if hr.reservation_status not in allowed:
        raise HTTPException(
            400, f"Cannot add charges: status is '{hr.reservation_status}'."
        )
    if getattr(hr, "invoice_number", None):
        raise HTTPException(
            400,
            f"Invoice {hr.invoice_number} already generated. Charges cannot be added.",
        )

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

    # Recalculate payment status — adding charges may reopen a previously-paid bill.
    if bill.balance_due <= Decimal("0"):
        hr.payment_collected_status = "PAID"
    elif bill.advance_paid > Decimal("0"):
        hr.payment_collected_status = "PARTIAL"
    else:
        hr.payment_collected_status = "PENDING"

    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_CHARGES_ADDED",
        f"Partner added charge of Rs.{payload.amount} to {hr.reservation_number or hr.id}. "
        f"Reason: {payload.description}. New total: Rs.{hr.total_amount}",
    )
    await _sync_reservation_to_master(db, hr)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        partner_id,
        action="HOTEL_CHARGES_ADDED",
        payment_collected_status=hr.payment_collected_status,
    )
    return success_response(
        "Charges added",
        {
            "additional_charges": float(hr.extra_charges),
            "final_amount": float(hr.total_amount),
            "balance_due": float(bill.balance_due),
        },
    )


# ════════════════════════════════════════════════════════════════
# RECORD ADVANCE — POST /partners/me/hotel-bookings/{reservation_id}/record-advance
# CASH/UPI collected at property → received_by = PARTNER
# ════════════════════════════════════════════════════════════════

_ADVANCE_ALLOWED = {
    "PENDING_PAYMENT",
    "AWAITING_HOTEL_CONFIRMATION",
    "CONFIRMED",
    "CHECKED_IN",
    "IN_HOUSE",
    "CHECKED_OUT",
}


@router.post("/me/hotel-bookings/{reservation_id}/record-advance")
async def partner_hotel_record_advance(
    reservation_id: int,
    payload: PartnerRecordAdvanceRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status not in _ADVANCE_ALLOWED:
        raise HTTPException(
            400, f"Cannot record advance: status is '{hr.reservation_status}'."
        )

    mode = payload.payment_mode.strip().upper()
    if mode not in {"UPI", "CASH", "ONLINE", "WALLET"}:
        raise HTTPException(
            400,
            f"Unsupported payment mode '{payload.payment_mode}'. Valid: UPI, CASH, ONLINE, WALLET",
        )

    # Partner at property can only collect CASH or UPI physically
    received_by = "PARTNER" if mode in {"CASH", "UPI"} else "ADMIN"

    bill = await hotel_billing.build_bill(db, hr)
    amount = Decimal(str(payload.amount))
    if bill.balance_due <= Decimal("0"):
        raise HTTPException(
            400, "Nothing outstanding — this stay is already paid in full."
        )
    if amount > bill.balance_due:
        raise HTTPException(
            400,
            f"Advance of Rs.{amount} exceeds outstanding balance of Rs.{bill.balance_due}.",
        )

    receipt = await hotel_billing.next_receipt_number(db)
    adv = HotelAdvancePayment(
        hotel_reservation_id=hr.id,
        master_booking_id=hr.master_booking_id,
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
        hr.master_booking_id,
        "HOTEL_ADVANCE_RECEIVED",
        f"Partner recorded advance of Rs.{amount} via {mode} (held by {received_by}) "
        f"for {hr.reservation_number or hr.id}. Receipt {receipt}. "
        f"Balance now Rs.{refreshed.balance_due}.",
    )
    await _sync_reservation_to_master(db, hr)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        partner_id,
        action="HOTEL_ADVANCE_RECEIVED",
        payment_collected_status=hr.payment_collected_status,
    )
    return success_response(
        "Advance recorded",
        {
            "receipt_number": receipt,
            "amount": float(amount),
            "received_by": received_by,
            "total_advance_paid": float(refreshed.advance_paid),
            "balance_due": float(refreshed.balance_due),
            "payment_collected_status": hr.payment_collected_status,
        },
    )


# ════════════════════════════════════════════════════════════════
# CHECK-OUT — POST /partners/me/hotel-bookings/{reservation_id}/check-out
# Allowed from: IN_HOUSE
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/check-out")
async def partner_hotel_check_out(
    reservation_id: int,
    payload: PartnerCheckOutRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status != "IN_HOUSE":
        raise HTTPException(
            400,
            f"Cannot check out: status is '{hr.reservation_status}', expected IN_HOUSE.",
        )
    if payload.additional_charges < 0:
        raise HTTPException(400, "Additional charges cannot be negative.")

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

    checked_out_at = hotel_billing.as_utc(
        payload.actual_check_out_at or datetime.now(timezone.utc)
    )
    # Real-world guard: a guest cannot check out before they checked in.
    if hr.actual_check_in_at and checked_out_at < hr.actual_check_in_at:
        raise HTTPException(
            400,
            "Check-out time cannot be earlier than the recorded check-in time.",
        )
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

    # Recalculate payment status — checkout may reopen a previously-paid bill.
    if bill.balance_due <= Decimal("0"):
        hr.payment_collected_status = "PAID"
    elif bill.advance_paid > Decimal("0"):
        hr.payment_collected_status = "PARTIAL"
    else:
        hr.payment_collected_status = "PENDING"

    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_CHECKED_OUT",
        f"Partner checked out guest for {hr.reservation_number or hr.id}. "
        f"Additional charges: Rs.{payload.additional_charges}. "
        f"Final total: Rs.{hr.total_amount}.",
    )
    await _sync_reservation_to_master(db, hr)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        partner_id,
        action="HOTEL_CHECKED_OUT",
        payment_collected_status=hr.payment_collected_status,
    )
    return success_response(
        "Guest checked out",
        {
            "hotel_status": "CHECKED_OUT",
            "final_amount": float(hr.total_amount),
            "balance_due": float(bill.balance_due),
            "freed_rooms": freed_rooms,
        },
    )


# ════════════════════════════════════════════════════════════════
# COLLECT PAYMENT — POST /partners/me/hotel-bookings/{reservation_id}/collect-payment
# CASH/UPI collected at property → collected_by = PARTNER
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/collect-payment")
async def partner_hotel_collect_payment(
    reservation_id: int,
    payload: PartnerCollectPaymentRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status not in {"CHECKED_OUT", "COMPLETED"}:
        raise HTTPException(
            400,
            f"Cannot collect payment: status is '{hr.reservation_status}', expected CHECKED_OUT.",
        )
    if not getattr(hr, "invoice_number", None):
        raise HTTPException(400, "Generate the invoice before collecting payment.")

    bill = await hotel_billing.build_bill(db, hr)
    if bill.balance_due <= Decimal("0"):
        raise HTTPException(400, "This invoice is already paid in full.")

    mode = payload.payment_mode.strip().upper()
    if mode not in {"UPI", "CASH", "ONLINE"}:
        raise HTTPException(400, f"Unsupported payment mode '{payload.payment_mode}'.")

    collected_by = "PARTNER" if mode in {"CASH", "UPI"} else "ADMIN"

    amount = Decimal(str(payload.amount))
    if amount > bill.balance_due:
        raise HTTPException(
            400, f"Rs.{amount} exceeds outstanding balance of Rs.{bill.balance_due}."
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

    receipt = await hotel_billing.next_receipt_number(db)
    adv = HotelAdvancePayment(
        hotel_reservation_id=hr.id,
        master_booking_id=hr.master_booking_id,
        receipt_number=receipt,
        amount=amount,
        payment_mode=mode,
        received_by=collected_by,
        reference_number=(payload.reference_number or "").strip() or None,
        status="ACTIVE",
    )
    db.add(adv)
    await db.flush()

    refreshed = await hotel_billing.build_bill(db, hr)
    if refreshed.balance_due <= Decimal("0"):
        hr.payment_collected_status = "PAID"
        hr.payment_collected_by = collected_by
        hr.payment_mode = mode
    elif refreshed.advance_paid > Decimal("0"):
        hr.payment_collected_status = "PARTIAL"

    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_PAYMENT_COLLECTED",
        f"Partner collected Rs.{amount} via {mode} (held by {collected_by}) "
        f"for {hr.reservation_number or hr.id}. Balance now Rs.{refreshed.balance_due}.",
    )
    await _sync_reservation_to_master(db, hr)
    await db.commit()
    await _notify_hotel_update(
        db,
        hr,
        partner_id,
        action="HOTEL_PAYMENT_COLLECTED",
        payment_collected_status=hr.payment_collected_status,
    )
    return success_response(
        "Payment collected",
        {
            "receipt_number": receipt,
            "amount": float(amount),
            "collected_by": collected_by,
            "balance_due": float(refreshed.balance_due),
            "payment_collected_status": hr.payment_collected_status,
        },
    )


# ════════════════════════════════════════════════════════════════
# GENERATE INVOICE — POST /partners/me/hotel-bookings/{reservation_id}/generate-invoice
# Allowed from: CHECKED_OUT | COMPLETED
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/generate-invoice")
async def partner_hotel_generate_invoice(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status not in {"CHECKED_OUT", "COMPLETED"}:
        raise HTTPException(
            400,
            f"Cannot generate invoice: status is '{hr.reservation_status}', expected CHECKED_OUT.",
        )

    bill = await hotel_billing.build_bill(db, hr)

    if getattr(hr, "invoice_number", None):
        return success_response(
            "Invoice already generated",
            {
                "already_generated": True,
                "invoice_number": hr.invoice_number,
                "bill": bill.as_dict(),
            },
        )

    hr.invoice_number = await hotel_billing.next_invoice_number(db)
    hr.invoice_generated_at = datetime.now(timezone.utc)
    hr.taxable_amount = bill.taxable_amount
    hr.gst_percent = bill.gst_percent
    hr.gst_amount = bill.gst_amount
    hr.is_tax_invoice = bill.is_tax_invoice
    hr.total_amount = bill.grand_total

    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_INVOICE_GENERATED",
        f"Invoice {hr.invoice_number} generated by partner for {hr.reservation_number or hr.id}. "
        f"Total Rs.{bill.grand_total} (GST Rs.{bill.gst_amount}).",
    )
    await _sync_reservation_to_master(db, hr)
    await db.commit()
    await _send_hotel_email(
        db,
        hr,
        "invoice_issued",
        message="Your tax invoice is ready to download from your bookings.",
        extra_details=[("Invoice no.", hr.invoice_number or "")],
    )
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_INVOICE_GENERATED")
    return success_response(
        "Invoice generated",
        {
            "already_generated": False,
            "invoice_number": hr.invoice_number,
            "bill": bill.as_dict(),
        },
    )


# ════════════════════════════════════════════════════════════════
# MARK COMPLETE — POST /partners/me/hotel-bookings/{reservation_id}/complete
# Allowed from: CHECKED_OUT
# ════════════════════════════════════════════════════════════════


@router.post("/me/hotel-bookings/{reservation_id}/complete")
async def partner_hotel_complete(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Close out a stay so it becomes eligible for settlement.

    Stricter than the admin equivalent on purpose. Admin can complete a stay with
    money still owed because admin can also chase or write it off; a partner
    cannot, and COMPLETED is what makes the reservation settleable. Requiring the
    invoice and a zero balance first stops a partner from advancing a stay into
    settlement while the guest still owes the property.
    """
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status != "CHECKED_OUT":
        raise HTTPException(
            400,
            f"Cannot complete: status is '{hr.reservation_status}', expected CHECKED_OUT.",
        )
    if not getattr(hr, "invoice_number", None):
        raise HTTPException(
            400, "Generate the invoice before marking this stay complete."
        )

    bill = await hotel_billing.build_bill(db, hr)
    if bill.balance_due > Decimal("0"):
        raise HTTPException(
            400,
            f"Rs.{bill.balance_due} is still outstanding. Collect the balance before completing this stay.",
        )

    hr.reservation_status = "COMPLETED"
    await _log_timeline(
        db,
        hr.master_booking_id,
        "HOTEL_COMPLETED",
        f"Hotel booking {hr.reservation_number or hr.id} marked COMPLETED by partner. "
        f"Settlement may now be prepared.",
    )
    await _sync_reservation_to_master(db, hr)
    await db.commit()
    await _send_hotel_email(
        db,
        hr,
        "booking_completed",
        message="Your stay is complete — thank you for choosing WayTero. Your tax invoice is ready in your bookings.",
    )
    await _notify_hotel_update(db, hr, partner_id, action="HOTEL_COMPLETED")
    return success_response("Booking completed", {"hotel_status": "COMPLETED"})


# ════════════════════════════════════════════════════════════════
# DOWNLOAD INVOICE — GET /partners/me/hotel-bookings/{reservation_id}/download-invoice
# Allowed from: CHECKED_OUT | COMPLETED | SETTLED, once the invoice exists
# ════════════════════════════════════════════════════════════════


@router.get("/me/hotel-bookings/{reservation_id}/download-invoice")
async def partner_hotel_download_invoice(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Stream the same tax invoice PDF the admin side serves, scoped to this partner."""
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.invoice_pdf_service import generate_hotel_invoice_pdf

    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    if hr.reservation_status not in {"CHECKED_OUT", "COMPLETED", "SETTLED"}:
        raise HTTPException(
            400,
            f"Cannot download invoice: booking is '{hr.reservation_status}'. "
            "The stay must be CHECKED_OUT first.",
        )
    if not getattr(hr, "invoice_number", None):
        raise HTTPException(
            400, "Invoice not yet generated. Generate the invoice before downloading."
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
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    cfg = {row.config_key: (row.config_value or "") for row in config_rows}

    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == hr.master_booking_id)
        )
    ).scalar_one_or_none()

    cust = cust_user = None
    if mb and mb.customer_id:
        cust = (
            await db.execute(select(Customer).where(Customer.id == mb.customer_id))
        ).scalar_one_or_none()
        if cust:
            cust_user = (
                await db.execute(select(User).where(User.id == cust.user_id))
            ).scalar_one_or_none()

    hotel = (
        await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
    ).scalar_one_or_none()

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

    bill = await hotel_billing.build_bill(db, hr)

    pdf_bytes = generate_hotel_invoice_pdf(
        invoice_number=hr.invoice_number,
        hotel_booking_number=hr.reservation_number or f"HR-{hr.id}",
        booking_number=mb.booking_number if mb else None,
        invoice_date=hr.invoice_generated_at,
        customer_name=cust.full_name if cust else None,
        customer_mobile=cust_user.mobile_number if cust_user else None,
        hotel_name=hotel.hotel_name if hotel else None,
        hotel_address=hotel.address if hotel else None,
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

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{hr.invoice_number}.pdf"'
        },
    )


# ════════════════════════════════════════════════════════════════
# AVAILABLE ROOMS — GET /partners/me/hotel-bookings/{reservation_id}/available-rooms
# ════════════════════════════════════════════════════════════════


@router.get("/me/hotel-bookings/{reservation_id}/available-rooms")
async def partner_hotel_available_rooms(
    reservation_id: int,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hr = await _get_hotel_reservation(db, reservation_id, partner_id)

    q = (
        select(HotelRoom)
        .where(
            HotelRoom.hotel_id == hr.hotel_id,
            HotelRoom.room_status == "AVAILABLE",
            HotelRoom.is_active.is_(True),
        )
        .order_by(HotelRoom.room_number)
    )
    if hr.room_category_id:
        q = q.where(HotelRoom.room_category_id == hr.room_category_id)

    rooms = (await db.execute(q)).scalars().all()
    return success_response(
        "Available rooms",
        {
            "rooms_needed": hr.rooms_count or 1,
            "data": [
                {
                    "id": r.id,
                    "room_number": r.room_number,
                    "floor_number": r.floor_number,
                }
                for r in rooms
            ],
        },
    )


# ════════════════════════════════════════════════════════════════
# WALK-IN BOOKINGS — partner creates a stay for a walk-in guest
# Doc Ref: 03_HOTEL_TOUR.md HT1
# ════════════════════════════════════════════════════════════════


class WalkInQuoteRequest(BaseModel):
    room_category_id: int = Field(..., gt=0)
    check_in_date: date
    check_out_date: date
    rooms_count: int = Field(1, ge=1, le=30)
    adults_count: int = Field(1, ge=1, le=100)
    children_count: int = Field(0, ge=0, le=100)
    extra_beds: int = Field(0, ge=0, le=100)


class WalkInBookingRequest(WalkInQuoteRequest):
    guest_name: str = Field(..., min_length=2, max_length=150)
    mobile: str = Field(..., min_length=10, max_length=15)
    email: Optional[str] = Field(None, max_length=255)
    special_requests: Optional[str] = Field(None, max_length=2000)
    remarks: Optional[str] = Field(None, max_length=2000)


def _walkin_booking_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = _uuid.uuid4().hex[:4].upper()
    return f"WT-MTB-{ts}-{uid}"


def _walkin_reservation_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = _uuid.uuid4().hex[:6].upper()
    return f"HR-{ts}-{uid}"


def _validate_stay(payload: WalkInQuoteRequest) -> None:
    if payload.check_out_date <= payload.check_in_date:
        raise HTTPException(400, "Check-out must be after check-in.")
    if (payload.check_out_date - payload.check_in_date).days > 90:
        raise HTTPException(400, "A stay cannot be longer than 90 nights.")


async def _get_partner_hotel(
    db: AsyncSession, hotel_id: int, partner_id: int
) -> Hotel:
    hotel = (
        await db.execute(
            select(Hotel).where(
                Hotel.id == hotel_id, Hotel.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if not hotel or int(hotel.partner_id) != partner_id:
        raise HTTPException(404, "Hotel not found for this partner.")
    if str(hotel.status) != "ACTIVE":
        raise HTTPException(400, "Hotel is not active / bookable.")
    return hotel


async def _quote_walkin(
    db: AsyncSession, hotel: Hotel, payload: WalkInQuoteRequest
) -> dict:
    _validate_stay(payload)
    category = (
        await db.execute(
            select(HotelRoomCategory).where(
                HotelRoomCategory.id == payload.room_category_id,
                HotelRoomCategory.hotel_id == hotel.id,
                HotelRoomCategory.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not category:
        raise HTTPException(404, "Room category not found for this hotel.")
    return await compute_hotel_quote(
        db,
        hotel,
        payload.room_category_id,
        payload.check_in_date,
        payload.check_out_date,
        payload.rooms_count,
        adults_count=payload.adults_count,
        children_count=payload.children_count,
        extra_beds=payload.extra_beds,
    )


@router.get(
    "/me/hotels",
    summary="Active hotels owned by the authenticated partner, with room categories",
)
async def partner_hotels_for_walkin(
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Metadata for the walk-in form. Scoped to the partner's own ACTIVE
    hotels; unlike `/admin/hotels`, a partner cannot see another partner's
    property here."""
    partner_id = await _resolve_partner_id(db, current_user["sub"])

    hotel_rows = (
        (
            await db.execute(
                _text(
                    """
                    SELECT h.id, h.hotel_code, h.hotel_name, h.star_rating,
                           h.city_id, ci.name AS city_name, h.address, h.tax_mode
                      FROM hotels h
                      LEFT JOIN cities ci ON ci.id = h.city_id
                     WHERE h.partner_id = :pid
                       AND h.status = 'ACTIVE'
                       AND h.deleted_at IS NULL
                     ORDER BY h.hotel_name
                    """
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .all()
    )
    hotel_ids = [int(r["id"]) for r in hotel_rows]
    categories: dict[int, list] = {}
    if hotel_ids:
        cat_rows = (
            (
                await db.execute(
                    _text(
                        """
                        SELECT id, hotel_id, category_name, room_type, meal_plan,
                               base_occupancy, max_adults, max_children,
                               max_occupancy, extra_bed_allowed,
                               CAST(extra_bed_charge AS FLOAT) AS extra_bed_charge,
                               CAST(extra_adult_charge AS FLOAT) AS extra_adult_charge,
                               CAST(extra_child_charge AS FLOAT) AS extra_child_charge,
                               CAST(base_price AS FLOAT) AS base_price,
                               CAST(published_price AS FLOAT) AS published_price,
                               total_rooms
                          FROM hotel_room_categories
                         WHERE hotel_id = ANY(:hids) AND is_active = TRUE
                         ORDER BY display_order, category_name
                        """
                    ),
                    {"hids": hotel_ids},
                )
            )
            .mappings()
            .all()
        )
        for row in cat_rows:
            categories.setdefault(int(row["hotel_id"]), []).append(dict(row))

    return success_response(
        "Partner hotels",
        {
            "hotels": [
                {
                    **dict(r),
                    "room_categories": categories.get(int(r["id"]), []),
                }
                for r in hotel_rows
            ]
        },
    )


@router.post(
    "/me/hotels/{hotel_id}/walk-in-quote",
    summary="Price a walk-in stay without creating it",
)
async def partner_walkin_quote(
    hotel_id: int,
    payload: WalkInQuoteRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hotel = await _get_partner_hotel(db, hotel_id, partner_id)
    quote = await _quote_walkin(db, hotel, payload)
    return {"success": True, **quote_to_response(quote)}


@router.post(
    "/me/hotels/{hotel_id}/walk-in-bookings",
    status_code=201,
    summary="Create a confirmed walk-in reservation",
)
async def partner_create_walkin_booking(
    hotel_id: int,
    payload: WalkInBookingRequest,
    current_user: dict = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """A walk-in guest usually has no account. Match by mobile digits first;
    otherwise mint a CUSTOMER user + customer profile + wallet so the
    reservation has the same shape as every other hotel booking. Prices are
    always server-side, never accepted from the app."""
    partner_id = await _resolve_partner_id(db, current_user["sub"])
    hotel = await _get_partner_hotel(db, hotel_id, partner_id)

    digits = re.sub(r"\D", "", payload.mobile)
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise HTTPException(400, "Enter a valid 10-digit Indian mobile number.")

    quote = await _quote_walkin(db, hotel, payload)

    customer = (
        await db.execute(
            _text(
                """
                SELECT c.id
                  FROM customers c
                  JOIN users u ON u.id = c.user_id
                 WHERE RIGHT(regexp_replace(u.mobile_number, '[^0-9]', '', 'g'), 10) = :digits
                 LIMIT 1
                """
            ),
            {"digits": digits},
        )
    ).scalar_one_or_none()

    customer_created = False
    if customer is None:
        first_name, _, last_name = payload.guest_name.strip().partition(" ")
        new_uid = _uuid.uuid4()
        user_seq = (
            await db.execute(
                _text(
                    "SELECT COUNT(*) FROM users WHERE user_type = 'CUSTOMER'"
                )
            )
        ).scalar() or 0
        await db.execute(
            _text(
                """
                INSERT INTO users (
                    id, user_code, first_name, last_name, mobile_number, email,
                    user_type, status, is_active, is_mobile_verified,
                    is_email_verified, created_by, created_at, updated_at
                ) VALUES (
                    :id, :code, :first, :last, :mobile, :email,
                    'CUSTOMER', 'ACTIVE', TRUE, FALSE, FALSE,
                    :created_by, NOW(), NOW()
                )
                """
            ),
            {
                "id": str(new_uid),
                "code": f"CUS-{int(user_seq) + 1:06d}",
                "first": first_name[:100],
                "last": last_name[:100] or None,
                "mobile": digits,
                "email": payload.email,
                "created_by": str(current_user["sub"]),
            },
        )
        cust_seq = (
            await db.execute(_text("SELECT COUNT(*) FROM customers"))
        ).scalar() or 0
        customer = (
            await db.execute(
                _text(
                    """
                    INSERT INTO customers (
                        uuid, user_id, customer_code, first_name, last_name,
                        city_id, is_active, created_at, updated_at
                    ) VALUES (
                        :uuid, :user_id, :code, :first, :last,
                        :city_id, TRUE, NOW(), NOW()
                    ) RETURNING id
                    """
                ),
                {
                    "uuid": str(_uuid.uuid4()),
                    "user_id": str(new_uid),
                    "code": f"CT-{int(cust_seq) + 1:06d}",
                    "first": first_name[:100],
                    "last": last_name[:100] or None,
                    "city_id": int(hotel.city_id),
                },
            )
        ).scalar_one()
        customer_created = True
        await db.execute(
            _text(
                """
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance,
                     wallet_status, created_at, updated_at)
                VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
                ON CONFLICT (customer_id) DO NOTHING
                """
            ),
            {"cid": customer},
        )

    # Per-night inventory; dates without a row are open availability.
    try:
        upd = await db.execute(
            _text(
                """
                UPDATE hotel_inventory
                   SET booked_rooms = booked_rooms + :rooms,
                       available_rooms = available_rooms - :rooms,
                       updated_at = NOW()
                 WHERE room_category_id = :cid
                   AND inventory_date >= :dfrom
                   AND inventory_date < :dto
                   AND is_stop_sell = FALSE
                RETURNING inventory_date
                """
            ),
            {
                "rooms": payload.rooms_count,
                "cid": payload.room_category_id,
                "dfrom": payload.check_in_date,
                "dto": payload.check_out_date,
            },
        )
        upd.fetchall()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "Not enough rooms available for those dates.")

    booking_number = _walkin_booking_number()
    master_id = (
        await db.execute(
            _text(
                """
                INSERT INTO master_bookings (
                    uuid, booking_number, customer_id, city_id,
                    booking_status, payment_status,
                    total_amount, total_paid_amount, total_refund_amount,
                    journey_start_date, remarks, created_at, updated_at
                ) VALUES (
                    :uuid, :number, :customer_id, :city_id,
                    'CONFIRMED', 'PENDING',
                    :amount, 0, 0,
                    :check_in, :remarks, NOW(), NOW()
                ) RETURNING id
                """
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "number": booking_number,
                "customer_id": customer,
                "city_id": int(hotel.city_id),
                "amount": quote["total_amount"],
                "check_in": payload.check_in_date,
                "remarks": payload.remarks
                or f"Walk-in booking created by partner for {payload.guest_name}",
            },
        )
    ).scalar_one()

    service_id = (
        await db.execute(
            _text(
                """
                INSERT INTO booking_services (
                    master_booking_id, service_type, service_reference_id,
                    service_status, service_amount, created_at
                ) VALUES (:mb, 'HOTEL', 0, 'CONFIRMED', :amount, NOW())
                RETURNING id
                """
            ),
            {"mb": master_id, "amount": quote["total_amount"]},
        )
    ).scalar_one()

    reservation_number = _walkin_reservation_number()
    reservation_id = (
        await db.execute(
            _text(
                """
                INSERT INTO hotel_reservations (
                    uuid, master_booking_id, booking_service_id, hotel_id,
                    room_category_id, customer_id, reservation_number,
                    check_in_date, check_out_date, nights, rooms_count,
                    room_nights, adults_count, children_count,
                    base_amount, extra_charges, discount_amount, taxable_amount,
                    gst_percent, gst_amount, is_tax_invoice, total_amount,
                    platform_commission, partner_payout,
                    rate_snapshot, commission_config_snapshot,
                    reservation_status, special_requests, confirmed_at,
                    created_at, updated_at
                ) VALUES (
                    :uuid, :mb, :svc, :hotel_id,
                    :cat, :customer_id, :number,
                    :check_in, :check_out, :nights, :rooms,
                    :room_nights, :adults, :children,
                    :base, 0, 0, :taxable,
                    :gst_pct, :gst_amount, :is_tax_invoice, :total,
                    :commission, :payout,
                    CAST(:rate_snapshot AS JSONB), CAST(:commission_snapshot AS JSONB),
                    'CONFIRMED', :special, NOW(), NOW(), NOW()
                ) RETURNING id
                """
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "mb": master_id,
                "svc": service_id,
                "hotel_id": hotel_id,
                "cat": payload.room_category_id,
                "customer_id": customer,
                "number": reservation_number,
                "check_in": payload.check_in_date,
                "check_out": payload.check_out_date,
                "nights": quote["nights"],
                "rooms": payload.rooms_count,
                "room_nights": quote["room_nights"],
                "adults": payload.adults_count,
                "children": payload.children_count,
                "base": quote["base_amount"],
                "taxable": quote["taxable_amount"],
                "gst_pct": quote["gst_percent"],
                "gst_amount": quote["gst_amount"],
                "is_tax_invoice": quote["is_tax_invoice"],
                "total": quote["total_amount"],
                "commission": quote["platform_commission"],
                "payout": quote["partner_payout"],
                "rate_snapshot": json.dumps(quote["rate_snapshot"]),
                "commission_snapshot": json.dumps(quote["commission_snapshot"]),
                "special": payload.special_requests,
            },
        )
    ).scalar_one()

    await db.execute(
        _text(
            "UPDATE booking_services SET service_reference_id = :rid WHERE id = :sid"
        ),
        {"rid": reservation_id, "sid": service_id},
    )
    await db.execute(
        _text(
            """
            INSERT INTO hotel_reservation_guests (
                reservation_id, guest_name, mobile, id_type, id_number,
                is_primary, created_at
            ) VALUES (:rid, :name, :mobile, NULL, NULL, TRUE, NOW())
            """
        ),
        {
            "rid": reservation_id,
            "name": payload.guest_name.strip(),
            "mobile": digits,
        },
    )
    await _log_timeline(
        db,
        master_id,
        "BOOKING_CREATED",
        f"Walk-in booking created by partner for {payload.guest_name}",
    )

    return {
        "success": True,
        "master_booking_id": master_id,
        "booking_number": booking_number,
        "reservation_id": reservation_id,
        "reservation_number": reservation_number,
        "customer_id": customer,
        "customer_created": customer_created,
        "total_amount": float(quote["total_amount"]),
        "gst_amount": float(quote["gst_amount"]),
        "nights": quote["nights"],
        "rooms_count": payload.rooms_count,
        "message": "Walk-in booking created",
    }


# (Hotel switch / split-stay partner read-only endpoints moved above
# `@router.get("/me/hotel-bookings/{reservation_id}")` so the literal paths
# `switch-history` and `{reservation_id}/switch-banner` win the route match
# before FastAPI tries to coerce them into the int path parameter.)
