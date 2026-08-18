# ============================================================
# WAYTERO — CUSTOMER BOOKING + CANCELLATION API
# File: app/modules/customer/cancellation_api.py
# Prefix: /customers  (registered in api/router.py)
#
# Doc Ref:
#   BRD Part 3 §46 (Customer cancellation)
#   BRD Part 4 §82 (Hotel cancellation policy)
#   SRS Part 3 §99 (Cancellation engine — customer path)
#
# Endpoints (customer-self):
#   GET   /me/bookings                       — list own master bookings
#   GET   /me/bookings/{booking_number}      — booking detail
#   GET   /me/bookings/{booking_number}/refund-preview
#                                             — preview cab refund
#   POST  /me/bookings/{booking_number}/cancel
#                                             — cancel the WHOLE master booking
#   GET   /me/hotel-bookings/{reservation_number}/refund-preview
#                                             — preview hotel refund
#   POST  /me/hotel-bookings/{reservation_number}/cancel
#                                             — cancel a single hotel reservation
#   GET   /me/cab-bookings/{cab_booking_number}/refund-preview
#                                             — preview cab refund
#   POST  /me/cab-bookings/{cab_booking_number}/cancel
#                                             — cancel a single cab booking
#
# Customers can self-cancel per the policy (cab BRD §46, hotel BRD §82).
# The engine applies the same rules as the admin path: charge / refund is
# computed from the live ladder, advances are auto-credited to the wallet,
# notifications + WS are fired.
# ============================================================

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.timezone import get_platform_tz_name, to_platform_tz
from app.modules.booking.services.cancellation_policy import (
    CANCEL_SOURCE_CUSTOMER,
    apply_cab_cancellation,
    apply_hotel_cancellation,
    apply_tour_cancellation,
    quote_cab_cancellation,
    quote_hotel_cancellation,
    quote_tour_cancellation,
)
from app.modules.booking.services import rollup_tour_totals_into_master
from app.modules.customer.models import Customer

logger = logging.getLogger("waytero.cancellation.customer")

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class CancelIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class CancelCabBookingIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class CancelHotelReservationIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class RefundPreviewOut(BaseModel):
    cab_booking_id: int
    charge: float
    refund_amount: float
    refund_percent: float
    tier_label: str
    cab_total_amount: float
    advance_paid_total: float
    pickup_at: Optional[str]
    hours_to_pickup: Optional[float]
    is_post_assignment: bool
    policy_snapshot: Dict[str, Any]


class HotelRefundPreviewOut(BaseModel):
    hotel_reservation_id: int
    charge: float
    refund_amount: float
    refund_percent: float
    tier_label: str
    hotel_total_amount: float
    advance_paid_total: float
    check_in_date: Optional[str]
    hours_to_checkin: Optional[float]
    policy_snapshot: Dict[str, Any]


class TourRefundPreviewOut(BaseModel):
    tour_booking_id: int
    charge: float
    refund_amount: float
    refund_percent: float
    tier_label: str
    tour_total_amount: float
    advance_paid_total: float
    travel_start_date: Optional[str]
    days_to_travel: Optional[int]
    policy_snapshot: Dict[str, Any]


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _resolve_customer(db: AsyncSession, user_uuid: str) -> Customer:
    c = (
        await db.execute(select(Customer).where(Customer.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Customer profile not found.")
    return c


async def _ensure_customer_owns_master(
    db: AsyncSession, customer_id: int, master_booking_id: int
) -> None:
    owner = (
        await db.execute(
            text("SELECT customer_id FROM master_bookings WHERE id = :id"),
            {"id": master_booking_id},
        )
    ).first()
    if not owner:
        raise HTTPException(404, "Booking not found.")
    if int(owner[0]) != customer_id:
        raise HTTPException(403, "You can only act on your own bookings.")


async def _fire_customer_notifications(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_id: Optional[int],
    hotel_reservation_id: Optional[int],
    refund_amount: Decimal,
    tier_label: str,
) -> None:
    try:
        from app.modules.notification.services.booking_notifications import (
            _customer_user_id,
        )
        from app.modules.notification.services import dispatch

        uid = await _customer_user_id(db, master_booking_id)
        if uid is None:
            return
        ref_label = (
            f"cab #{cab_booking_id}"
            if cab_booking_id
            else f"hotel reservation #{hotel_reservation_id}"
        )
        await dispatch(
            db,
            user_id=uid,
            event_type="BOOKING_CANCELLED",
            title="Your booking has been cancelled",
            body=(
                f"You cancelled {ref_label}. Refund ₹{float(refund_amount):,.0f} "
                f"has been credited to your wallet. Tier: {tier_label}."
            ),
            data={
                "master_booking_id": master_booking_id,
                "cab_booking_id": cab_booking_id,
                "hotel_reservation_id": hotel_reservation_id,
                "cancelled_source": "CUSTOMER",
                "refund_amount": float(refund_amount),
                "tier_label": tier_label,
            },
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("customer.cancel.notify_failed err=%s", exc)


# ════════════════════════════════════════════════════════════════
# 1. CUSTOMER BOOKING LIST + DETAIL
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/bookings",
    tags=["Customer Bookings"],
    summary="List my own bookings.",
)
async def list_my_bookings(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT mb.id, mb.booking_number, mb.booking_status, mb.payment_status,
                       mb.total_amount, mb.total_paid_amount, mb.journey_start_date,
                       mb.journey_end_date, mb.created_at
                FROM master_bookings mb
                WHERE mb.customer_id = :cid
                ORDER BY mb.created_at DESC
                LIMIT 50
                """
                ),
                {"cid": cust.id},
            )
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        # service types
        svc = (
            (
                await db.execute(
                    text(
                        "SELECT DISTINCT service_type FROM booking_services WHERE master_booking_id = :m"
                    ),
                    {"m": r["id"]},
                )
            )
            .scalars()
            .all()
        )
        out.append(
            {
                "id": int(r["id"]),
                "booking_number": r["booking_number"],
                "booking_status": r["booking_status"],
                "payment_status": r["payment_status"],
                "total_amount": float(r["total_amount"] or 0),
                "total_paid_amount": float(r["total_paid_amount"] or 0),
                "services": list(svc),
                "journey_start_date": (
                    r["journey_start_date"].isoformat()
                    if r["journey_start_date"]
                    else None
                ),
                "journey_end_date": (
                    r["journey_end_date"].isoformat() if r["journey_end_date"] else None
                ),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return out


async def _send_customer_cancel_emails(
    db: AsyncSession,
    cust: Customer,
    *,
    service: str,
    booking_number: str,
    refund_amount: float,
    charge: float,
    related_type: str,
    related_id: int,
) -> None:
    """Cancellation + (when > 0) refund emails to the customer.
    Never raises — email failures are logged by the engine, not propagated."""
    try:
        from app.infrastructure.email import send_event_email

        email_row = (
            await db.execute(
                text("SELECT email FROM users WHERE id = :uid"),
                {"uid": str(cust.user_id)},
            )
        ).first()
        to_email = (email_row[0] or "").strip() if email_row else ""
        if not to_email:
            return
        name = (
            " ".join(
                filter(None, [str(cust.first_name or ""), str(cust.last_name or "")])
            )
            or "there"
        )

        await send_event_email(
            db,
            event_type="booking_cancelled",
            to_email=to_email,
            to_name=name,
            context={
                "name": name,
                "service": service,
                "booking_number": booking_number,
                "message": (
                    f"Your {service.lower()} booking {booking_number} has been cancelled. "
                    "Any refund due has been credited to your WayTero Wallet per the "
                    "cancellation policy in effect."
                ),
                "details": [
                    ("Cancellation charge", f"₹{charge:,.2f}" if charge else "None"),
                    (
                        "Refund to wallet",
                        f"₹{refund_amount:,.2f}" if refund_amount else "None",
                    ),
                ],
            },
            related_type=related_type,
            related_id=related_id,
        )
        if float(refund_amount or 0) > 0:
            await send_event_email(
                db,
                event_type="refund_processed",
                to_email=to_email,
                to_name=name,
                context={
                    "name": name,
                    "service": service,
                    "booking_number": booking_number,
                    "message": (
                        "Your refund has been credited to your WayTero Travel Wallet "
                        "and is available for any future booking."
                    ),
                    "details": [
                        ("Booking", booking_number),
                        ("Refund amount", f"₹{refund_amount:,.2f}"),
                    ],
                    "amount": refund_amount,
                    "amount_label": "Refund credited",
                },
                related_type=related_type,
                related_id=related_id,
            )
    except Exception:  # pragma: no cover — email must never break cancellations
        pass


@router.get(
    "/me/bookings/{booking_number}",
    tags=["Customer Bookings"],
    summary="Booking detail with cabs + hotels.",
)
async def get_my_booking(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    mb = (
        (
            await db.execute(
                text(
                    """
                SELECT id, booking_number, booking_status, payment_status,
                       total_amount, total_paid_amount, total_refund_amount,
                       journey_start_date, journey_end_date, remarks, created_at
                FROM master_bookings
                WHERE customer_id = :cid AND booking_number = :bn
                """
                ),
                {"cid": cust.id, "bn": booking_number.strip().upper()},
            )
        )
        .mappings()
        .first()
    )
    if not mb:
        raise HTTPException(404, "Booking not found.")

    cbs = (
        (
            await db.execute(
                text(
                    """
                SELECT id, booking_number, trip_type, pickup_location, drop_location,
                       pickup_datetime, trip_started_at, trip_ended_at, actual_distance,
                       booking_status, final_amount, estimated_amount,
                       invoice_number, invoice_url, payment_mode, payment_collected_by,
                       cash_amount_due, coupon_code, coupon_discount,
                       gst_rate, gst_amount, is_tax_invoice
                FROM cab_bookings WHERE master_booking_id = :m
                ORDER BY id
                """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )
    hrs = (
        (
            await db.execute(
                text(
                    """
                SELECT hr.id, hr.reservation_number, hr.reservation_status,
                       hr.total_amount, hr.check_in_date, hr.check_out_date,
                       hr.nights, hr.rooms_count, hr.adults_count,
                       hr.actual_check_in_at, hr.actual_check_out_at,
                       hr.hotel_confirmation_number,
                       hr.hotel_id, hr.room_category_id,
                       hr.invoice_number, hr.invoice_url, hr.invoice_generated_at,
                       hr.payment_collected_status, hr.payment_collected_by, hr.payment_mode,
                       hr.base_amount, hr.extra_charges, hr.discount_amount,
                       hr.coupon_code, hr.coupon_discount,
                       hr.taxable_amount, hr.gst_percent, hr.gst_amount,
                       hr.is_tax_invoice, hr.cancellation_charge, hr.refund_amount,
                       h.hotel_name, h.address AS hotel_address,
                       rc.category_name AS room_category_name, rc.room_type
                FROM hotel_reservations hr
                LEFT JOIN hotels h ON h.id = hr.hotel_id
                LEFT JOIN hotel_room_categories rc ON rc.id = hr.room_category_id
                WHERE hr.master_booking_id = :m
                ORDER BY hr.id
                """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )

    # All ACTIVE advances across this booking's reservations — grouped per
    # reservation below so the UI can show the payment trail and balance due.
    adv_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT hotel_reservation_id, receipt_number, amount, payment_mode,
                           received_by, reference_number, collected_at
                    FROM hotel_advance_payments
                    WHERE master_booking_id = :m AND status = 'ACTIVE'
                    ORDER BY collected_at, id
                    """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )
    adv_by_res: Dict[int, List[Dict[str, Any]]] = {}
    for a in adv_rows:
        adv_by_res.setdefault(int(a["hotel_reservation_id"]), []).append(
            {
                "receipt_number": a["receipt_number"],
                "amount": float(a["amount"] or 0),
                "payment_mode": a["payment_mode"],
                "received_by": a["received_by"],
                "reference_number": a["reference_number"],
                "collected_at": (
                    a["collected_at"].isoformat() if a["collected_at"] else None
                ),
            }
        )

    # Cab advances (advance_payments table) — grouped per cab booking.
    cab_adv_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT cab_booking_id, receipt_number, amount, payment_mode,
                           received_by, collected_at
                    FROM advance_payments
                    WHERE master_booking_id = :m AND status = 'ACTIVE'
                    ORDER BY collected_at, id
                    """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )
    cab_adv_by_cab: Dict[int, List[Dict[str, Any]]] = {}
    for a in cab_adv_rows:
        cab_adv_by_cab.setdefault(int(a["cab_booking_id"]), []).append(
            {
                "receipt_number": a["receipt_number"],
                "amount": float(a["amount"] or 0),
                "payment_mode": a["payment_mode"],
                "received_by": a["received_by"],
                "collected_at": (
                    a["collected_at"].isoformat() if a["collected_at"] else None
                ),
            }
        )

    # ── Tour bookings + their payment trail + charges ─────────────────
    tour_rows = (
        (
            await db.execute(
                text(
                    """
                SELECT tb.id, tb.booking_number, tb.booking_status, tb.payment_status,
                       tb.travel_start_date, tb.travel_end_date, tb.persons_count,
                       tb.total_amount, tb.platform_commission, tb.partner_payout,
                       tb.pickup_location, tb.pickup_datetime, tb.hotel_details,
                       tb.other_details, tb.additional_amount, tb.additional_charge_note,
                       tb.advance_total, tb.advance_received_by, tb.invoice_number,
                       tb.invoiced_at, tb.special_requests, tb.package_id,
                       p.package_name, p.destination, p.duration_days, p.duration_nights
                FROM tour_bookings tb
                LEFT JOIN tour_packages p ON p.id = tb.package_id
                WHERE tb.master_booking_id = :m
                ORDER BY tb.id
                """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )

    tour_adv_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT tour_booking_id, receipt_number, amount, payment_mode,
                           received_by, reference_number, notes, collected_at
                    FROM tour_advance_payments
                    WHERE master_booking_id = :m AND status = 'ACTIVE'
                    ORDER BY collected_at, id
                    """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )
    tour_adv_by_booking: Dict[int, List[Dict[str, Any]]] = {}
    for a in tour_adv_rows:
        tour_adv_by_booking.setdefault(int(a["tour_booking_id"]), []).append(
            {
                "receipt_number": a["receipt_number"],
                "amount": float(a["amount"] or 0),
                "payment_mode": a["payment_mode"],
                "received_by": a["received_by"],
                "reference_number": a["reference_number"],
                "notes": a["notes"],
                "collected_at": (
                    a["collected_at"].isoformat() if a["collected_at"] else None
                ),
            }
        )

    tour_charge_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT tour_booking_id, label, amount, reason, added_by_role, created_at
                    FROM tour_booking_charges
                    WHERE tour_booking_id IN (
                        SELECT id FROM tour_bookings WHERE master_booking_id = :m
                    )
                    ORDER BY created_at, id
                    """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )
    tour_charges_by_booking: Dict[int, List[Dict[str, Any]]] = {}
    for c in tour_charge_rows:
        tour_charges_by_booking.setdefault(int(c["tour_booking_id"]), []).append(
            {
                "label": c["label"],
                "amount": float(c["amount"] or 0),
                "reason": c["reason"],
                "added_by_role": c["added_by_role"],
                "created_at": c["created_at"].isoformat() if c["created_at"] else None,
            }
        )

    # Itinerary day count per package — a live day-by-day catalogue makes
    # the itinerary PDF possible, so surface it for the download button.
    itin_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT p.id AS package_id, COUNT(ti.id) AS day_count
                    FROM tour_bookings tb
                    JOIN tour_packages p ON p.id = tb.package_id
                    LEFT JOIN tour_itineraries ti ON ti.package_id = p.id
                    WHERE tb.master_booking_id = :m
                    GROUP BY p.id
                    """
                ),
                {"m": mb["id"]},
            )
        )
        .mappings()
        .all()
    )
    itin_days = {int(r["package_id"]): int(r["day_count"] or 0) for r in itin_rows}

    gst_row = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = 'GST_ENABLED'"
            )
        )
    ).scalar()
    gst_enabled = str(gst_row or "").strip().lower() in ("true", "1", "yes")
    platform_timezone = await get_platform_tz_name(db)

    hotel_bookings = []
    for h in hrs:
        advances = adv_by_res.get(int(h["id"]), [])
        advance_paid = sum(a["amount"] for a in advances)
        total_amt = float(h["total_amount"] or 0)
        hotel_bookings.append(
            {
                "id": int(h["id"]),
                "reservation_number": h["reservation_number"],
                "reservation_status": h["reservation_status"],
                "hotel_name": h["hotel_name"],
                "hotel_address": h["hotel_address"],
                "room_category_name": h["room_category_name"],
                "room_type": h["room_type"],
                "nights": int(h["nights"] or 0),
                "rooms_count": int(h["rooms_count"] or 1),
                "adults_count": int(h["adults_count"] or 1),
                "check_in_date": (
                    h["check_in_date"].isoformat() if h["check_in_date"] else None
                ),
                "check_out_date": (
                    h["check_out_date"].isoformat() if h["check_out_date"] else None
                ),
                # Actual stamps the property recorded — these are what matter
                # once the stay has started, not the dates booked.
                "actual_check_in_at": (
                    h["actual_check_in_at"].isoformat()
                    if h["actual_check_in_at"]
                    else None
                ),
                "actual_check_out_at": (
                    h["actual_check_out_at"].isoformat()
                    if h["actual_check_out_at"]
                    else None
                ),
                # Platform-local re-projection (the stamp a guest reads on the
                # invoice) + the IANA zone name so the UI can label it.
                "check_in_at_local": (
                    to_platform_tz(h["actual_check_in_at"]).isoformat()
                    if h["actual_check_in_at"]
                    else None
                ),
                "check_out_at_local": (
                    to_platform_tz(h["actual_check_out_at"]).isoformat()
                    if h["actual_check_out_at"]
                    else None
                ),
                "platform_timezone": platform_timezone,
                "hotel_confirmation_number": h["hotel_confirmation_number"],
                "invoice_number": h["invoice_number"],
                "invoice_url": h["invoice_url"],
                "invoice_generated_at": (
                    h["invoice_generated_at"].isoformat()
                    if h["invoice_generated_at"]
                    else None
                ),
                "payment_collected_status": h["payment_collected_status"],
                "payment_collected_by": h["payment_collected_by"],
                "payment_mode": h["payment_mode"],
                "base_amount": float(h["base_amount"] or 0),
                "extra_charges": float(h["extra_charges"] or 0),
                "discount_amount": float(h["discount_amount"] or 0),
                "coupon_code": h["coupon_code"],
                "coupon_discount": float(h["coupon_discount"] or 0),
                "taxable_amount": float(h["taxable_amount"] or 0),
                "gst_percent": float(h["gst_percent"] or 0),
                "gst_amount": float(h["gst_amount"] or 0),
                "is_tax_invoice": bool(h["is_tax_invoice"]),
                "gst_enabled": gst_enabled,
                "total_amount": total_amt,
                "cancellation_charge": float(h["cancellation_charge"] or 0),
                "refund_amount": float(h["refund_amount"] or 0),
                "advance_payments": advances,
                "total_advance_paid": advance_paid,
                "balance_due": max(total_amt - advance_paid, 0),
            }
        )

    cab_bookings = []
    for c in cbs:
        advances = cab_adv_by_cab.get(int(c["id"]), [])
        advance_paid = sum(a["amount"] for a in advances)
        final_amt = float(c["final_amount"] or c["estimated_amount"] or 0)
        cab_bookings.append(
            {
                "id": int(c["id"]),
                "booking_number": c["booking_number"],
                "trip_type": c["trip_type"],
                "pickup_location": c["pickup_location"],
                "drop_location": c["drop_location"],
                "pickup_datetime": (
                    c["pickup_datetime"].isoformat() if c["pickup_datetime"] else None
                ),
                "trip_started_at": (
                    c["trip_started_at"].isoformat() if c["trip_started_at"] else None
                ),
                "trip_ended_at": (
                    c["trip_ended_at"].isoformat() if c["trip_ended_at"] else None
                ),
                "actual_distance": (
                    float(c["actual_distance"])
                    if c["actual_distance"] is not None
                    else None
                ),
                "booking_status": c["booking_status"],
                "final_amount": float(c["final_amount"] or 0),
                "estimated_amount": float(c["estimated_amount"] or 0),
                "invoice_number": c["invoice_number"],
                "invoice_url": c["invoice_url"],
                "payment_mode": c["payment_mode"],
                "payment_collected_by": c["payment_collected_by"],
                "cash_amount_due": (
                    float(c["cash_amount_due"]) if c["cash_amount_due"] else None
                ),
                "coupon_code": c["coupon_code"],
                "coupon_discount": float(c["coupon_discount"] or 0),
                "gst_rate": float(c["gst_rate"] or 0),
                "gst_amount": float(c["gst_amount"] or 0),
                "is_tax_invoice": bool(c["is_tax_invoice"]),
                "advance_payments": advances,
                "advance_paid_total": advance_paid,
                "balance_due": max(final_amt - advance_paid, 0),
            }
        )

    tour_bookings = []
    for t in tour_rows:
        advances = tour_adv_by_booking.get(int(t["id"]), [])
        advance_paid = float(t["advance_total"] or 0) or sum(
            a["amount"] for a in advances
        )
        total_amt = float(t["total_amount"] or 0)
        tour_bookings.append(
            {
                "id": int(t["id"]),
                "booking_number": t["booking_number"],
                "booking_status": t["booking_status"],
                "payment_status": t["payment_status"],
                "package_name": t["package_name"],
                "destination": t["destination"],
                "duration_days": int(t["duration_days"] or 0),
                "duration_nights": int(t["duration_nights"] or 0),
                "travel_start_date": (
                    t["travel_start_date"].isoformat()
                    if t["travel_start_date"]
                    else None
                ),
                "travel_end_date": (
                    t["travel_end_date"].isoformat() if t["travel_end_date"] else None
                ),
                "persons_count": int(t["persons_count"] or 1),
                "pickup_location": t["pickup_location"],
                "pickup_datetime": (
                    t["pickup_datetime"].isoformat() if t["pickup_datetime"] else None
                ),
                "hotel_details": t["hotel_details"],
                "other_details": t["other_details"],
                "special_requests": t["special_requests"],
                "total_amount": total_amt,
                "platform_commission": float(t["platform_commission"] or 0),
                "partner_payout": float(t["partner_payout"] or 0),
                "additional_amount": float(t["additional_amount"] or 0),
                "additional_charge_note": t["additional_charge_note"],
                "advance_total": advance_paid,
                "advance_received_by": t["advance_received_by"],
                "invoice_number": t["invoice_number"],
                "invoiced_at": (
                    t["invoiced_at"].isoformat() if t["invoiced_at"] else None
                ),
                "advance_payments": advances,
                "charges": tour_charges_by_booking.get(int(t["id"]), []),
                "itinerary_days": itin_days.get(int(t["package_id"] or 0), 0),
                "balance_due": max(total_amt - advance_paid, 0),
            }
        )

    return {
        "id": int(mb["id"]),
        "booking_number": mb["booking_number"],
        "booking_status": mb["booking_status"],
        "payment_status": mb["payment_status"],
        "total_amount": float(mb["total_amount"] or 0),
        "total_paid_amount": float(mb["total_paid_amount"] or 0),
        "total_refund_amount": float(mb["total_refund_amount"] or 0),
        "journey_start_date": (
            mb["journey_start_date"].isoformat() if mb["journey_start_date"] else None
        ),
        "journey_end_date": (
            mb["journey_end_date"].isoformat() if mb["journey_end_date"] else None
        ),
        "remarks": mb["remarks"],
        "created_at": mb["created_at"].isoformat() if mb["created_at"] else None,
        "cab_bookings": cab_bookings,
        "hotel_bookings": hotel_bookings,
        "tour_bookings": tour_bookings,
    }


# ════════════════════════════════════════════════════════════════
# 2. CAB REFUND-PREVIEW + CANCEL (customer-self)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/cab-bookings/{cab_booking_number}/refund-preview",
    response_model=RefundPreviewOut,
    tags=["Customer Cancellation"],
    summary="Preview the cancellation refund for one of my cab bookings.",
)
async def preview_my_cab_cancellation(
    cab_booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    cb = (
        await db.execute(
            text(
                """
                SELECT cb.id, cb.master_booking_id, cb.final_amount, cb.estimated_amount,
                       cb.pickup_datetime, cb.booking_status
                FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                WHERE cb.booking_number = :bn AND mb.customer_id = :cid
                """
            ),
            {"bn": cab_booking_number.strip().upper(), "cid": cust.id},
        )
    ).first()
    if not cb:
        raise HTTPException(404, "Cab booking not found / not yours.")

    cab_total = Decimal(cb[2] or cb[3] or 0)
    advance_row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM advance_payments WHERE cab_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": cb[0]},
        )
    ).first()
    advance_total = Decimal(advance_row[0]) if advance_row else Decimal("0")
    pa = (
        await db.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM cab_booking_assignments WHERE cab_booking_id = :id)"
            ),
            {"id": cb[0]},
        )
    ).first()
    is_post_assignment = bool(pa and pa[0])
    quote = await quote_cab_cancellation(
        db,
        cab_booking_id=int(cb[0]),
        cab_total_amount=cab_total,
        advance_paid_total=advance_total,
        is_post_assignment=is_post_assignment,
    )
    inputs = quote.policy_snapshot.get("inputs") or {}
    return RefundPreviewOut(
        cab_booking_id=int(cb[0]),
        charge=float(quote.charge),
        refund_amount=float(quote.refund_amount),
        refund_percent=float(quote.refund_percent),
        tier_label=quote.tier_label,
        cab_total_amount=float(cab_total),
        advance_paid_total=float(advance_total),
        pickup_at=inputs.get("pickup_at"),
        hours_to_pickup=inputs.get("hours_to_pickup"),
        is_post_assignment=is_post_assignment,
        policy_snapshot=quote.policy_snapshot,
    )


@router.post(
    "/me/cab-bookings/{cab_booking_number}/cancel",
    tags=["Customer Cancellation"],
    summary="Cancel one of my cab bookings. Runs the policy engine and auto-refunds.",
)
async def cancel_my_cab(
    cab_booking_number: str,
    payload: CancelIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    cb = (
        await db.execute(
            text(
                """
                SELECT cb.id, cb.master_booking_id, cb.booking_status
                FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                WHERE cb.booking_number = :bn AND mb.customer_id = :cid
                """
            ),
            {"bn": cab_booking_number.strip().upper(), "cid": cust.id},
        )
    ).first()
    if not cb:
        raise HTTPException(404, "Cab booking not found / not yours.")
    blocked = {
        "IN_PROGRESS",
        "STARTED",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if cb[2] in blocked:
        raise HTTPException(400, f"Cannot cancel: cab is '{cb[2]}'.")

    result = await apply_cab_cancellation(
        db,
        cab_booking_id=int(cb[0]),
        master_booking_id=int(cb[1]),
        cancellation_reason=payload.reason,
        cancelled_by_user_id=UUID(current_user["sub"]),
        cancelled_by_role="CUSTOMER",
        cancelled_source=CANCEL_SOURCE_CUSTOMER,
    )
    await _fire_customer_notifications(
        db,
        master_booking_id=int(cb[1]),
        cab_booking_id=int(cb[0]),
        hotel_reservation_id=None,
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )
    await db.commit()
    await _send_customer_cancel_emails(
        db,
        cust,
        service="Cab",
        booking_number=cab_booking_number.strip().upper(),
        refund_amount=float(result["refund_amount"]),
        charge=float(result["charge"]),
        related_type="CAB_BOOKING",
        related_id=int(cb[0]),
    )
    return {
        "success": True,
        "message": "Cab cancelled. Refund credited to your wallet.",
        **result,
    }


# ════════════════════════════════════════════════════════════════
# 3. HOTEL REFUND-PREVIEW + CANCEL (customer-self)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/hotel-bookings/{reservation_number}/refund-preview",
    response_model=HotelRefundPreviewOut,
    tags=["Customer Cancellation"],
    summary="Preview the cancellation refund for one of my hotel reservations.",
)
async def preview_my_hotel_cancellation(
    reservation_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    hr = (
        await db.execute(
            text(
                """
                SELECT hr.id, hr.total_amount, hr.check_in_date, hr.reservation_status
                FROM hotel_reservations hr
                JOIN master_bookings mb ON mb.id = hr.master_booking_id
                WHERE hr.reservation_number = :rn AND mb.customer_id = :cid
                """
            ),
            {"rn": reservation_number.strip().upper(), "cid": cust.id},
        )
    ).first()
    if not hr:
        raise HTTPException(404, "Reservation not found / not yours.")

    advance_row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM hotel_advance_payments WHERE reservation_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": hr[0]},
        )
    ).first()
    advance_total = Decimal(advance_row[0]) if advance_row else Decimal("0")
    quote = await quote_hotel_cancellation(
        db,
        hotel_reservation_id=int(hr[0]),
        hotel_total_amount=Decimal(hr[1] or 0),
        advance_paid_total=advance_total,
    )
    inputs = quote.policy_snapshot.get("inputs") or {}
    return HotelRefundPreviewOut(
        hotel_reservation_id=int(hr[0]),
        charge=float(quote.charge),
        refund_amount=float(quote.refund_amount),
        refund_percent=float(quote.refund_percent),
        tier_label=quote.tier_label,
        hotel_total_amount=float(hr[1] or 0),
        advance_paid_total=float(advance_total),
        check_in_date=inputs.get("check_in_date"),
        hours_to_checkin=inputs.get("hours_to_checkin"),
        policy_snapshot=quote.policy_snapshot,
    )


@router.post(
    "/me/hotel-bookings/{reservation_number}/cancel",
    tags=["Customer Cancellation"],
    summary="Cancel one of my hotel reservations. Runs the policy engine and auto-refunds.",
)
async def cancel_my_hotel(
    reservation_number: str,
    payload: CancelIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    hr = (
        await db.execute(
            text(
                """
                SELECT hr.id, hr.master_booking_id, hr.reservation_status
                FROM hotel_reservations hr
                JOIN master_bookings mb ON mb.id = hr.master_booking_id
                WHERE hr.reservation_number = :rn AND mb.customer_id = :cid
                """
            ),
            {"rn": reservation_number.strip().upper(), "cid": cust.id},
        )
    ).first()
    if not hr:
        raise HTTPException(404, "Reservation not found / not yours.")
    blocked = {
        "CHECKED_IN",
        "IN_HOUSE",
        "CHECKED_OUT",
        "COMPLETED",
        "SETTLED",
        "CANCELLED",
        "REJECTED",
    }
    if hr[2] in blocked:
        raise HTTPException(400, f"Cannot cancel: reservation is '{hr[2]}'.")

    result = await apply_hotel_cancellation(
        db,
        hotel_reservation_id=int(hr[0]),
        master_booking_id=int(hr[1]),
        cancellation_reason=payload.reason,
        cancelled_by_user_id=UUID(current_user["sub"]),
        cancelled_by_role="CUSTOMER",
        cancelled_source=CANCEL_SOURCE_CUSTOMER,
    )
    await _fire_customer_notifications(
        db,
        master_booking_id=int(hr[1]),
        cab_booking_id=None,
        hotel_reservation_id=int(hr[0]),
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )
    await db.commit()
    await _send_customer_cancel_emails(
        db,
        cust,
        service="Hotel",
        booking_number=reservation_number.strip().upper(),
        refund_amount=float(result["refund_amount"]),
        charge=float(result["charge"]),
        related_type="HOTEL_RESERVATION",
        related_id=int(hr[0]),
    )
    return {
        "success": True,
        "message": "Hotel reservation cancelled. Refund credited to your wallet.",
        **result,
    }


# ════════════════════════════════════════════════════════════════
# 3b. TOUR REFUND-PREVIEW + CANCEL (customer-self)
# Doc Ref: BRD Part 5 §119 — Tour cancellation policy
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/tour-bookings/{booking_number}/refund-preview",
    response_model=TourRefundPreviewOut,
    tags=["Customer Cancellation"],
    summary="Preview the cancellation refund for one of my tour bookings.",
)
async def preview_my_tour_cancellation(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    tb = (
        await db.execute(
            text(
                """
                SELECT tb.id, tb.total_amount, tb.travel_start_date, tb.booking_status
                FROM tour_bookings tb
                JOIN master_bookings mb ON mb.id = tb.master_booking_id
                WHERE tb.booking_number = :bn AND mb.customer_id = :cid
                """
            ),
            {"bn": booking_number.strip().upper(), "cid": cust.id},
        )
    ).first()
    if not tb:
        raise HTTPException(404, "Tour booking not found / not yours.")

    advance_row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM tour_advance_payments WHERE tour_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": tb[0]},
        )
    ).first()
    advance_total = Decimal(advance_row[0]) if advance_row else Decimal("0")
    quote = await quote_tour_cancellation(
        db,
        tour_booking_id=int(tb[0]),
        tour_total_amount=Decimal(tb[1] or 0),
        advance_paid_total=advance_total,
    )
    inputs = quote.policy_snapshot.get("inputs") or {}
    return TourRefundPreviewOut(
        tour_booking_id=int(tb[0]),
        charge=float(quote.charge),
        refund_amount=float(quote.refund_amount),
        refund_percent=float(quote.refund_percent),
        tier_label=quote.tier_label,
        tour_total_amount=float(tb[1] or 0),
        advance_paid_total=float(advance_total),
        travel_start_date=inputs.get("travel_start_date"),
        days_to_travel=inputs.get("days_to_travel"),
        policy_snapshot=quote.policy_snapshot,
    )


@router.post(
    "/me/tour-bookings/{booking_number}/cancel",
    tags=["Customer Cancellation"],
    summary="Cancel one of my tour bookings. Runs the policy engine and auto-refunds.",
)
async def cancel_my_tour(
    booking_number: str,
    payload: CancelIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    tb = (
        await db.execute(
            text(
                """
                SELECT tb.id, tb.master_booking_id, tb.booking_status
                FROM tour_bookings tb
                JOIN master_bookings mb ON mb.id = tb.master_booking_id
                WHERE tb.booking_number = :bn AND mb.customer_id = :cid
                """
            ),
            {"bn": booking_number.strip().upper(), "cid": cust.id},
        )
    ).first()
    if not tb:
        raise HTTPException(404, "Tour booking not found / not yours.")
    blocked = {
        "IN_PROGRESS",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if tb[2] in blocked:
        raise HTTPException(400, f"Cannot cancel: tour booking is '{tb[2]}'.")

    result = await apply_tour_cancellation(
        db,
        tour_booking_id=int(tb[0]),
        master_booking_id=int(tb[1]),
        cancellation_reason=payload.reason,
        cancelled_by_user_id=UUID(current_user["sub"]),
        cancelled_by_role="CUSTOMER",
        cancelled_source=CANCEL_SOURCE_CUSTOMER,
    )
    await _fire_customer_notifications(
        db,
        master_booking_id=int(tb[1]),
        cab_booking_id=None,
        hotel_reservation_id=None,
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )
    await db.commit()
    await _send_customer_cancel_emails(
        db,
        cust,
        service="Tour package",
        booking_number=booking_number.strip().upper(),
        refund_amount=float(result["refund_amount"]),
        charge=float(result["charge"]),
        related_type="TOUR_BOOKING",
        related_id=int(tb[0]),
    )
    return {
        "success": True,
        "message": "Tour booking cancelled. Refund credited to your wallet.",
        **result,
    }


# ════════════════════════════════════════════════════════════════
# 4. WHOLE-BOOKING CANCEL (cancels every live cab + every live hotel)
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/bookings/{booking_number}/cancel",
    tags=["Customer Cancellation"],
    summary="Cancel a master booking in full (every live cab + hotel).",
)
async def cancel_my_booking(
    booking_number: str,
    payload: CancelIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cust = await _resolve_customer(db, current_user["sub"])
    mb = (
        await db.execute(
            text(
                """
                SELECT id, booking_status FROM master_bookings
                WHERE customer_id = :cid AND booking_number = :bn
                """
            ),
            {"cid": cust.id, "bn": booking_number.strip().upper()},
        )
    ).first()
    if not mb:
        raise HTTPException(404, "Booking not found / not yours.")
    blocked = {"CANCELLED", "COMPLETED", "CLOSED"}
    if mb[1] in blocked:
        raise HTTPException(400, f"Cannot cancel: booking is '{mb[1]}'.")

    results: List[Dict[str, Any]] = []
    cabs = (
        (
            await db.execute(
                text(
                    """
                SELECT id, booking_status FROM cab_bookings
                WHERE master_booking_id = :m
                """
                ),
                {"m": mb[0]},
            )
        )
        .mappings()
        .all()
    )
    for c in cabs:
        if c["booking_status"] in {
            "IN_PROGRESS",
            "STARTED",
            "COMPLETED",
            "SETTLEMENT_PENDING",
            "SETTLED",
            "CANCELLED",
        }:
            continue
        r = await apply_cab_cancellation(
            db,
            cab_booking_id=int(c["id"]),
            master_booking_id=int(mb[0]),
            cancellation_reason=payload.reason,
            cancelled_by_user_id=UUID(current_user["sub"]),
            cancelled_by_role="CUSTOMER",
            cancelled_source=CANCEL_SOURCE_CUSTOMER,
        )
        results.append({"type": "CAB", "cab_booking_id": int(c["id"]), **r})

    hrs = (
        (
            await db.execute(
                text(
                    """
                SELECT id, reservation_status FROM hotel_reservations
                WHERE master_booking_id = :m
                """
                ),
                {"m": mb[0]},
            )
        )
        .mappings()
        .all()
    )
    for h in hrs:
        if h["reservation_status"] in {
            "CHECKED_IN",
            "IN_HOUSE",
            "CHECKED_OUT",
            "COMPLETED",
            "SETTLED",
            "CANCELLED",
            "REJECTED",
        }:
            continue
        r = await apply_hotel_cancellation(
            db,
            hotel_reservation_id=int(h["id"]),
            master_booking_id=int(mb[0]),
            cancellation_reason=payload.reason,
            cancelled_by_user_id=UUID(current_user["sub"]),
            cancelled_by_role="CUSTOMER",
            cancelled_source=CANCEL_SOURCE_CUSTOMER,
        )
        results.append({"type": "HOTEL", "hotel_reservation_id": int(h["id"]), **r})

    tours = (
        (
            await db.execute(
                text(
                    """
                SELECT id, booking_status FROM tour_bookings
                WHERE master_booking_id = :m
                """
                ),
                {"m": mb[0]},
            )
        )
        .mappings()
        .all()
    )
    for t in tours:
        if t["booking_status"] in {
            "IN_PROGRESS",
            "COMPLETED",
            "SETTLEMENT_PENDING",
            "SETTLED",
            "CANCELLED",
        }:
            continue
        r = await apply_tour_cancellation(
            db,
            tour_booking_id=int(t["id"]),
            master_booking_id=int(mb[0]),
            cancellation_reason=payload.reason,
            cancelled_by_user_id=UUID(current_user["sub"]),
            cancelled_by_role="CUSTOMER",
            cancelled_source=CANCEL_SOURCE_CUSTOMER,
        )
        results.append({"type": "TOUR", "tour_booking_id": int(t["id"]), **r})

    if not results:
        raise HTTPException(
            400, "No live services to cancel — booking is already terminal."
        )

    total_refund = sum((Decimal(r["refund_amount"]) for r in results), Decimal("0"))
    await _fire_customer_notifications(
        db,
        master_booking_id=int(mb[0]),
        cab_booking_id=None,
        hotel_reservation_id=None,
        refund_amount=total_refund,
        tier_label="multi",
    )
    # Roll up any cancelled tours into the master totals.
    await rollup_tour_totals_into_master(db, master_booking_id=int(mb[0]))
    await db.commit()
    return {
        "success": True,
        "message": "Booking cancelled in full. Refunds credited to your wallet.",
        "cancellations": results,
        "total_refund": float(total_refund),
    }


# ════════════════════════════════════════════════════════════════
# 5. HOTEL INVOICE DOWNLOAD (customer-self)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/hotel-bookings/{reservation_number}/download-invoice",
    tags=["Customer Bookings"],
    summary="Download the PDF tax invoice for one of my hotel reservations.",
)
async def customer_hotel_download_invoice(
    reservation_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Stream the same tax invoice PDF the partner/admin sides serve, scoped
    to the reservation's owner. Mirrors partner_hotel_download_invoice but
    the ownership check is the master-booking join on the customer instead
    of a partner guard."""
    from app.modules.admin.invoice_pdf_service import generate_hotel_invoice_pdf
    from app.modules.admin.models import SystemConfiguration
    from app.modules.auth.models.user import User
    from app.modules.booking.models import MasterBooking
    from app.modules.hotel.models import Hotel, HotelReservation, HotelRoomCategory
    from app.modules.hotel.services import billing as hotel_billing

    cust = await _resolve_customer(db, current_user["sub"])
    hr = (
        await db.execute(
            select(HotelReservation)
            .join(MasterBooking, MasterBooking.id == HotelReservation.master_booking_id)
            .where(
                HotelReservation.reservation_number
                == reservation_number.strip().upper(),
                MasterBooking.customer_id == cust.id,
            )
        )
    ).scalar_one_or_none()
    if not hr:
        raise HTTPException(404, "Reservation not found / not yours.")

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
    cfg: Dict[str, str] = {}
    for row in config_rows:
        cfg[str(row.config_key)] = str(row.config_value or "")

    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == hr.master_booking_id)
        )
    ).scalar_one_or_none()
    cust_user = None
    if mb and mb.customer_id:
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
            room_category_name = str(rc.category_name)
            room_type = str(rc.room_type)

    bill = await hotel_billing.build_bill(db, hr)

    # ORM attributes are typed as Column[...] under mypy although at runtime
    # they are plain scalars — alias hr to Any so the pdf builder's arguments
    # keep their real signature instead of tripping arg-type checks.
    h: Any = hr
    pdf_bytes = generate_hotel_invoice_pdf(
        invoice_number=h.invoice_number,
        hotel_booking_number=h.reservation_number or f"HR-{h.id}",
        booking_number=str(mb.booking_number) if mb else "",
        invoice_date=h.invoice_generated_at,
        customer_name=cust.full_name if cust else None,
        customer_mobile=(
            getattr(cust_user, "mobile_number", None) if cust_user else None
        ),
        hotel_name=getattr(hotel, "hotel_name", None) if hotel else None,
        hotel_address=getattr(hotel, "address", None) if hotel else None,
        room_category_name=room_category_name,
        room_type=room_type,
        meal_plan=None,
        check_in_date=h.check_in_date.isoformat() if h.check_in_date else None,
        check_out_date=h.check_out_date.isoformat() if h.check_out_date else None,
        actual_check_in_at=h.actual_check_in_at,
        actual_check_out_at=h.actual_check_out_at,
        num_nights=bill.actual_nights,
        num_rooms=h.rooms_count,
        num_guests=h.adults_count,
        room_charge=float(bill.room_charge),
        overtime_charge=float(bill.overtime.charge),
        extra_charges=float(bill.extra_charges),
        discount_amount=float(bill.discount),
        coupon_code=h.coupon_code,
        taxable_amount=float(bill.taxable_amount),
        gst_percent=float(bill.gst_percent),
        gst_amount=float(bill.gst_amount),
        is_tax_invoice=bill.is_tax_invoice,
        grand_total=float(bill.grand_total),
        advance_paid=float(bill.advance_paid),
        payment_mode=h.payment_mode,
        payment_collected_by=h.payment_collected_by,
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
            "Content-Disposition": f'attachment; filename="{h.invoice_number}.pdf"'
        },
    )


# ════════════════════════════════════════════════════════════════
# 6. CAB INVOICE DOWNLOAD (customer-self)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/cab-bookings/{cab_booking_number}/download-invoice",
    tags=["Customer Bookings"],
    summary="Download the PDF tax invoice for one of my completed cab trips.",
)
async def customer_cab_download_invoice(
    cab_booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Stream the same cab tax invoice the admin/partner sides serve, scoped
    to the booking's owner. Mirrors admin_download_cab_invoice with the
    ownership check being the master-booking join on the customer.
    Doc Ref: BRD Part 3 §45 (settlement), §48 (tax invoice)"""
    from datetime import datetime as _datetime_cab_inv
    from datetime import timezone as _tz_cab_inv
    from sqlalchemy import func as _func_cab_inv

    from app.modules.admin.invoice_pdf_service import generate_invoice_pdf
    from app.modules.admin.models import SystemConfiguration
    from app.modules.admin.coupon_models import CouponUsage
    from app.modules.auth.models.user import User
    from app.modules.booking.models import AdvancePayment, CabBooking, MasterBooking
    from app.modules.driver.models import Driver
    from app.modules.master.models import City
    from app.modules.partner.models import Partner
    from app.modules.vehicle.models import Vehicle, VehicleCategory

    cust = await _resolve_customer(db, current_user["sub"])
    cb = (
        await db.execute(
            select(CabBooking)
            .join(MasterBooking, MasterBooking.id == CabBooking.master_booking_id)
            .where(
                CabBooking.booking_number == cab_booking_number.strip().upper(),
                MasterBooking.customer_id == cust.id,
            )
        )
    ).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, "Cab booking not found / not yours.")

    if cb.booking_status not in ("COMPLETED", "SETTLEMENT_PENDING", "SETTLED"):
        raise HTTPException(
            400,
            f"Invoice only available for completed trips "
            f"(current: {cb.booking_status}).",
        )
    if not cb.payment_mode:
        raise HTTPException(400, "Record payment before downloading the invoice.")
    if not cb.invoice_number:
        raise HTTPException(
            400,
            "Invoice number not found. Collect payment first to auto-generate invoice.",
        )

    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == cb.master_booking_id)
        )
    ).scalar_one_or_none()
    # A customer-owned cab always sits under a master booking.
    assert mb is not None

    cust_user = (
        await db.execute(select(User).where(User.id == cust.user_id))
    ).scalar_one_or_none()

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
    cfg = {str(row.config_key): str(row.config_value or "") for row in config_rows}

    city = (
        await db.execute(select(City).where(City.id == mb.city_id))
    ).scalar_one_or_none()

    # Vehicle category + latest-assignment partner / driver / vehicle.
    cat_name = driver_name = vehicle_reg = vehicle_model = partner_name = None
    if cb.vehicle_category_id:
        vc = (
            await db.execute(
                select(VehicleCategory).where(
                    VehicleCategory.id == cb.vehicle_category_id
                )
            )
        ).scalar_one_or_none()
        if vc:
            cat_name = getattr(vc, "category_name", None)
    if cb.assignments:
        latest = sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        if latest.partner_id:
            p = (
                await db.execute(select(Partner).where(Partner.id == latest.partner_id))
            ).scalar_one_or_none()
            if p:
                partner_name = getattr(p, "business_name", None) or f"Partner #{p.id}"
        if latest.driver_id:
            d = (
                await db.execute(select(Driver).where(Driver.id == latest.driver_id))
            ).scalar_one_or_none()
            if d:
                driver_name = getattr(d, "full_name", None)
        if latest.vehicle_id:
            v = (
                await db.execute(select(Vehicle).where(Vehicle.id == latest.vehicle_id))
            ).scalar_one_or_none()
            if v:
                vehicle_reg = getattr(v, "registration_number", None)
                vehicle_model = (
                    f"{getattr(v, 'vehicle_brand', None) or ''} "
                    f"{getattr(v, 'vehicle_model', None) or ''}".strip()
                    or None
                )

    coupon_q = await db.execute(
        select(_func_cab_inv.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)
    adv_q = await db.execute(
        select(
            _func_cab_inv.coalesce(
                _func_cab_inv.sum(
                    AdvancePayment.amount
                    - _func_cab_inv.coalesce(AdvancePayment.refunded_amount, 0)
                ),
                0,
            )
        ).where(
            AdvancePayment.cab_booking_id == cb.id,
            AdvancePayment.status == "ACTIVE",
        )
    )
    advance_paid = float(adv_q.scalar() or 0)

    # ORM attributes are typed as Column[...] under mypy — alias to Any so the
    # pdf builder's arguments keep their real signatures.
    x: Any = cb
    pdf_bytes = generate_invoice_pdf(
        invoice_number=x.invoice_number,
        cab_booking_number=x.booking_number,
        booking_number=str(mb.booking_number),
        invoice_date=_datetime_cab_inv.now(_tz_cab_inv.utc),
        customer_name=cust.full_name if cust else None,
        customer_mobile=(
            getattr(cust_user, "mobile_number", None) if cust_user else None
        ),
        city_name=getattr(city, "name", None) if city else None,
        pickup_location=x.pickup_location,
        drop_location=x.drop_location,
        trip_type=x.trip_type,
        vehicle_category_name=cat_name,
        vehicle_reg=vehicle_reg,
        vehicle_model=vehicle_model,
        driver_name=driver_name,
        partner_name=partner_name,
        trip_started_at=x.trip_started_at,
        trip_ended_at=x.trip_ended_at,
        trip_start_km=float(x.trip_start_km) if x.trip_start_km is not None else None,
        trip_end_km=float(x.trip_end_km) if x.trip_end_km is not None else None,
        actual_distance=(
            float(x.actual_distance) if x.actual_distance is not None else None
        ),
        estimated_amount=float(x.estimated_amount) if x.estimated_amount else None,
        final_amount=float(x.final_amount or 0),
        coupon_discount=coupon_discount,
        advance_paid=advance_paid,
        payment_mode=x.payment_mode,
        payment_collected_by=x.payment_collected_by,
        platform_commission=(
            float(x.platform_commission) if x.platform_commission else None
        ),
        is_tax_invoice=bool(getattr(x, "is_tax_invoice", False)),
        gst_rate=float(getattr(x, "gst_rate", 0) or 0),
        gst_amount=float(getattr(x, "gst_amount", 0) or 0),
        platform_name=cfg.get("PLATFORM_NAME", "WayTero"),
        platform_logo_url=cfg.get("PLATFORM_LOGO_URL", ""),
        business_legal_name=cfg.get("BUSINESS_LEGAL_NAME", ""),
        business_gst_number=cfg.get("BUSINESS_GST_NUMBER", ""),
        business_registered_address=cfg.get("BUSINESS_REGISTERED_ADDRESS", ""),
        support_email=cfg.get("SUPPORT_EMAIL", ""),
        support_phone=cfg.get("SUPPORT_PHONE", ""),
    )

    filename = f"Invoice_{x.invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ════════════════════════════════════════════════════════════════
# 7. TOUR ITINERARY + INVOICE DOWNLOAD (customer-self)
# ════════════════════════════════════════════════════════════════


async def _customer_tour_booking(
    db: AsyncSession, customer_id: int, tour_booking_number: str
) -> tuple[int, str]:
    """Resolve one of the customer's tour bookings → (id, booking_status)."""
    row = (
        await db.execute(
            text(
                """
                SELECT tb.id, tb.booking_status
                FROM tour_bookings tb
                JOIN master_bookings mb ON mb.id = tb.master_booking_id
                WHERE tb.booking_number = :bn AND mb.customer_id = :cid
                """
            ),
            {"bn": tour_booking_number.strip().upper(), "cid": customer_id},
        )
    ).first()
    if not row:
        raise HTTPException(404, "Tour booking not found / not yours.")
    return int(row[0]), str(row[1])


@router.get(
    "/me/tour-bookings/{tour_booking_number}/download-itinerary",
    tags=["Customer Bookings"],
    summary="Download the PDF itinerary for one of my tour bookings.",
)
async def customer_tour_download_itinerary(
    tour_booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Stream the day-by-day itinerary PDF (same builder the partner portal
    uses) for a live / upcoming tour of the calling customer."""
    from app.modules.tour.services import build_itinerary

    cust = await _resolve_customer(db, current_user["sub"])
    tour_id, status = await _customer_tour_booking(
        db, int(cust.id), tour_booking_number
    )
    if status == "CANCELLED":
        raise HTTPException(400, "This tour was cancelled — no itinerary to show.")
    doc = await build_itinerary(db, tour_booking_id=tour_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@router.get(
    "/me/tour-bookings/{tour_booking_number}/download-invoice",
    tags=["Customer Bookings"],
    summary="Download the PDF invoice for one of my tour bookings.",
)
async def customer_tour_download_invoice(
    tour_booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Stream the tour invoice PDF for a booking the partner has already
    invoiced (invoice_number set at build time)."""
    from app.modules.tour.services import build_invoice

    cust = await _resolve_customer(db, current_user["sub"])
    tour_id, _status = await _customer_tour_booking(
        db, int(cust.id), tour_booking_number
    )
    has_invoice = (
        await db.execute(
            text("SELECT invoice_number FROM tour_bookings WHERE id = :id"),
            {"id": tour_id},
        )
    ).scalar_one_or_none()
    if not has_invoice:
        raise HTTPException(
            400,
            "Invoice not generated yet. The partner generates it once the trip is done.",
        )
    doc = await build_invoice(db, tour_booking_id=tour_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )
