# ============================================================
# WAYTERO — ADMIN CANCELLATION API
# File: app/modules/admin/cancellation_api.py
# Prefix: /admin/cancellation  (registered in api/router.py)
#
# Doc Ref:
#   BRD Part 3 §46 (Cab cancellation rules)
#   BRD Part 3 §47 (Refund rules)
#   BRD Part 4 §82 (Hotel cancellation policy)
#   BRD Part 7 §134 (BOOKING_CANCELLED notification)
#   SRS Part 3 §99/§100 (Cancellation + refund engine)
#   SRS Part 5 §184/§185 (Hotel cancellation/refund engine)
#   API Doc §19/§20/§22 (Cancel service / master / refund-preview)
#
# This router is the single admin-facing entry-point for the new
# cancellation engine. It exposes:
#
#   1. Refund preview (admin, partner, customer can all hit this for
#      transparency) — does not mutate anything.
#   2. Admin force-cancel for a cab or hotel reservation — runs the
#      policy math, persists the snapshot, auto-refunds advances to the
#      customer wallet, writes the timeline, fires notifications + WS,
#      writes an audit row.
#   3. Partner cancellation request review — list / approve / reject.
#   4. Cab ladder policy editor (admin-only) — every edit appends a row
#      to cancellation_policy_versions so the audit trail answers "what
#      policy was in effect at the moment booking #N was cancelled".
#   5. Read-only view of a hotel's ladder (hotel owners edit via the
#      partner portal; admins view here).
# ============================================================

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.admin.services.audit_logger import AuditLogger
from app.modules.booking.services.cancellation_policy import (
    CANCEL_SOURCE_ADMIN,
    CANCEL_SOURCE_PARTNER_REQUEST,
    apply_cab_cancellation,
    apply_hotel_cancellation,
    apply_tour_cancellation,
    get_cab_policy_effective,
    get_tour_policy_effective,
    quote_cab_cancellation,
    quote_hotel_cancellation,
    quote_tour_cancellation,
    record_policy_version,
)

logger = logging.getLogger("waytero.cancellation.admin")

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class CabRefundPreviewOut(BaseModel):
    """Server-computed preview — same shape as the engine's as_dict()."""

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
    is_no_show: bool
    policy_snapshot: Dict[str, Any]


class AdminCancelCabIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class AdminCancelHotelIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


class AdminCancelTourIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)


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


class CabLadderUpdate(BaseModel):
    """Admin edits ONE cab ladder key at a time. Each PUT appends an audit row."""

    new_value: str = Field(
        ...,
        description="Numeric value as a string — the policy engine parses it lazily",
    )
    change_reason: Optional[str] = Field(
        None,
        max_length=500,
        description="Free-text reason, recorded in cancellation_policy_versions",
    )


class RequestReviewIn(BaseModel):
    decision: str = Field(..., description="APPROVED | REJECTED | WITHDRAWN")
    note: Optional[str] = Field(None, max_length=1000)


class TourPackagePolicyIn(BaseModel):
    """Per-tour-package cancellation ladder (BRD Part 5 §119).

    A package without a row falls back to the global tour ladder.
    """

    cancellation_free_days: int = Field(30, ge=0, le=3650)
    cancellation_tier_1_days: int = Field(15, ge=0, le=3650)
    refund_percent_tier_1: float = Field(100, ge=0, le=100)
    cancellation_tier_2_days: int = Field(7, ge=0, le=3650)
    refund_percent_tier_2: float = Field(75, ge=0, le=100)
    refund_percent_tier_3: float = Field(50, ge=0, le=100)
    refund_percent_last_minute: float = Field(0, ge=0, le=100)
    cancellation_policy_text: Optional[str] = Field(None, max_length=5000)


class PaginatedCancelRequests(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: List[Dict[str, Any]]


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _fire_cancellation_notifications(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_id: Optional[int],
    hotel_reservation_id: Optional[int],
    tour_booking_id: Optional[int] = None,
    cancelled_source: str,
    cancelled_by_role: str,
    refund_amount: Decimal,
    tier_label: str,
) -> None:
    """Best-effort WS + push + inbox notifications on cancel.

    Never raises — notification failure must not block the API. Mirrors
    the pattern used by booking_notifications.partner_assignment_requested.
    """
    try:
        from app.modules.notification.services.booking_notifications import (
            _customer_user_id,
        )
        from app.modules.notification.services import dispatch
        from app.modules.notification.realtime import manager as _rt

        cust_uid = await _customer_user_id(db, master_booking_id)
        title = "Your booking has been cancelled"
        ref_label = (
            f"cab #{cab_booking_id}"
            if cab_booking_id
            else (
                f"hotel reservation #{hotel_reservation_id}"
                if hotel_reservation_id
                else f"tour booking #{tour_booking_id}"
            )
        )
        body = (
            f"Your booking ({ref_label}) was cancelled by "
            f"{cancelled_by_role.lower()}. Refund ₹{float(refund_amount):,.0f} "
            f"has been credited to your wallet. Tier: {tier_label}."
        )
        if cust_uid is not None:
            try:
                await dispatch(
                    db,
                    user_id=cust_uid,
                    event_type="BOOKING_CANCELLED",
                    title=title,
                    body=body,
                    data={
                        "master_booking_id": master_booking_id,
                        "cab_booking_id": cab_booking_id,
                        "hotel_reservation_id": hotel_reservation_id,
                        "tour_booking_id": tour_booking_id,
                        "cancelled_source": cancelled_source,
                        "refund_amount": float(refund_amount),
                        "tier_label": tier_label,
                    },
                    booking_id=master_booking_id,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("cancel.notify.dispatch_failed err=%s", exc)

        # Real-time fanout — open admin / partner tabs refresh on this event.
        ws_payload = {
            "event": "BOOKING_CANCELLED",
            "data": {
                "master_booking_id": master_booking_id,
                "cab_booking_id": cab_booking_id,
                "hotel_reservation_id": hotel_reservation_id,
                "tour_booking_id": tour_booking_id,
                "cancelled_source": cancelled_source,
                "cancelled_by_role": cancelled_by_role,
                "refund_amount": float(refund_amount),
                "tier_label": tier_label,
            },
        }
        try:
            await _rt.broadcast(ws_payload)
        except Exception as exc:  # pragma: no cover
            logger.warning("cancel.ws.broadcast_failed err=%s", exc)
    except Exception as exc:  # pragma: no cover
        logger.warning("cancel.notify.unexpected err=%s", exc)


# ════════════════════════════════════════════════════════════════
# 1. REFUND PREVIEWS — read-only
# ════════════════════════════════════════════════════════════════


@router.get(
    "/cab/{cab_booking_id}/refund-preview",
    response_model=CabRefundPreviewOut,
    tags=["Admin Cancellation"],
    summary="Preview the cancellation charge + refund for a cab booking (no DB writes).",
)
async def preview_cab_cancellation(
    cab_booking_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # Visible to admin, CCO, partner (own), customer (own) — caller is
    # responsible for ownership scoping in the higher-level wrappers.
    cb = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, final_amount, estimated_amount,
                       pickup_datetime
                FROM cab_bookings WHERE id = :id
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    if not cb:
        raise HTTPException(404, "Cab booking not found")

    cab_total = Decimal(cb[2] or cb[3] or 0)
    advance_row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM advance_payments
                WHERE cab_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    advance_total = Decimal(advance_row[0]) if advance_row else Decimal("0")

    post_assign_row = (
        await db.execute(
            text(
                """
                SELECT EXISTS(SELECT 1 FROM cab_booking_assignments WHERE cab_booking_id = :id)
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    is_post_assignment = bool(post_assign_row and post_assign_row[0])

    quote = await quote_cab_cancellation(
        db,
        cab_booking_id=cab_booking_id,
        cab_total_amount=cab_total,
        advance_paid_total=advance_total,
        is_post_assignment=is_post_assignment,
    )
    hours_to_pickup = (quote.policy_snapshot.get("inputs") or {}).get("hours_to_pickup")
    pickup_at = (quote.policy_snapshot.get("inputs") or {}).get("pickup_at")
    return CabRefundPreviewOut(
        cab_booking_id=cab_booking_id,
        charge=float(quote.charge),
        refund_amount=float(quote.refund_amount),
        refund_percent=float(quote.refund_percent),
        tier_label=quote.tier_label,
        cab_total_amount=float(cab_total),
        advance_paid_total=float(advance_total),
        pickup_at=pickup_at,
        hours_to_pickup=hours_to_pickup,
        is_post_assignment=is_post_assignment,
        policy_snapshot=quote.policy_snapshot,
    )


async def _send_cancellation_emails(
    db: AsyncSession,
    *,
    master_booking_id: int,
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

        row = (
            (
                await db.execute(
                    text(
                        """
                    SELECT u.email, c.first_name, c.last_name
                    FROM master_bookings mb
                    JOIN customers c ON c.id = mb.customer_id
                    JOIN users u ON u.id = c.user_id
                    WHERE mb.id = :mbid
                    """
                    ),
                    {"mbid": master_booking_id},
                )
            )
            .mappings()
            .first()
        )
        if not row or not row["email"]:
            return
        name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or "there"

        await send_event_email(
            db,
            event_type="booking_cancelled",
            to_email=row["email"],
            to_name=name,
            context={
                "name": name,
                "service": service,
                "booking_number": booking_number,
                "message": (
                    f"Your {service.lower()} booking {booking_number} has been cancelled. "
                    "Any refund due has been processed to your WayTero Wallet per the "
                    "cancellation policy in effect at the time."
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
                to_email=row["email"],
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
    "/hotel/{reservation_id}/refund-preview",
    response_model=HotelRefundPreviewOut,
    tags=["Admin Cancellation"],
    summary="Preview the cancellation charge + refund for a hotel reservation (no DB writes).",
)
async def preview_hotel_cancellation(
    reservation_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    is_no_show: bool = Query(False),
):
    hr = (
        await db.execute(
            text(
                """
                SELECT id, total_amount FROM hotel_reservations WHERE id = :id
                """
            ),
            {"id": reservation_id},
        )
    ).first()
    if not hr:
        raise HTTPException(404, "Hotel reservation not found")

    advance_row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM hotel_advance_payments
                WHERE reservation_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": reservation_id},
        )
    ).first()
    advance_total = Decimal(advance_row[0]) if advance_row else Decimal("0")

    quote = await quote_hotel_cancellation(
        db,
        hotel_reservation_id=reservation_id,
        hotel_total_amount=Decimal(hr[1] or 0),
        advance_paid_total=advance_total,
        is_no_show=is_no_show,
    )
    inputs = quote.policy_snapshot.get("inputs") or {}
    return HotelRefundPreviewOut(
        hotel_reservation_id=reservation_id,
        charge=float(quote.charge),
        refund_amount=float(quote.refund_amount),
        refund_percent=float(quote.refund_percent),
        tier_label=quote.tier_label,
        hotel_total_amount=float(hr[1] or 0),
        advance_paid_total=float(advance_total),
        check_in_date=inputs.get("check_in_date"),
        hours_to_checkin=inputs.get("hours_to_checkin"),
        is_no_show=is_no_show,
        policy_snapshot=quote.policy_snapshot,
    )


@router.get(
    "/tour/{tour_booking_id}/refund-preview",
    response_model=TourRefundPreviewOut,
    tags=["Admin Cancellation"],
    summary="Preview the cancellation charge + refund for a tour booking (no DB writes).",
)
async def preview_tour_cancellation(
    tour_booking_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    tb = (
        await db.execute(
            text(
                """
                SELECT id, total_amount, travel_start_date FROM tour_bookings
                WHERE id = :id
                """
            ),
            {"id": tour_booking_id},
        )
    ).first()
    if not tb:
        raise HTTPException(404, "Tour booking not found")

    advance_row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM tour_advance_payments
                WHERE tour_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": tour_booking_id},
        )
    ).first()
    advance_total = Decimal(advance_row[0]) if advance_row else Decimal("0")

    quote = await quote_tour_cancellation(
        db,
        tour_booking_id=tour_booking_id,
        tour_total_amount=Decimal(tb[1] or 0),
        advance_paid_total=advance_total,
    )
    inputs = quote.policy_snapshot.get("inputs") or {}
    return TourRefundPreviewOut(
        tour_booking_id=tour_booking_id,
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


# ════════════════════════════════════════════════════════════════
# 2. ADMIN FORCE-CANCEL — runs the engine + persists + notifies
# ════════════════════════════════════════════════════════════════


@router.post(
    "/cab/{cab_booking_id}/cancel",
    tags=["Admin Cancellation"],
    summary="Admin force-cancel a cab booking. Runs the policy engine and auto-refunds.",
)
async def admin_cancel_cab(
    cab_booking_id: int,
    payload: AdminCancelCabIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    cb = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, booking_status, booking_number
                FROM cab_bookings WHERE id = :id
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    if not cb:
        raise HTTPException(404, "Cab booking not found")
    blocked = {
        "IN_PROGRESS",
        "STARTED",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if cb[2] in blocked:
        raise HTTPException(
            400,
            f"Cannot cancel: cab is in '{cb[2]}'. Active or terminal trips cannot be cancelled.",
        )

    result = await apply_cab_cancellation(
        db,
        cab_booking_id=cab_booking_id,
        master_booking_id=int(cb[1]),
        cancellation_reason=payload.reason,
        cancelled_by_user_id=UUID(current_user["sub"]),
        cancelled_by_role=current_user.get("role", "ADMIN"),
        cancelled_source=CANCEL_SOURCE_ADMIN,
    )

    # Audit + notify. Both best-effort — engine has already committed.
    try:
        await AuditLogger.log_booking_event(
            db,
            action_type="CANCELLATION_ADMIN_FORCE",
            booking_id=int(cb[1]),
            user_id=UUID(current_user["sub"]),
            old_values={"cab_booking_status": cb[2]},
            new_values={
                "cab_booking_status": "CANCELLED",
                "charge": float(result["charge"]),
                "refund_amount": float(result["refund_amount"]),
                "tier_label": result["tier_label"],
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except AttributeError:
        # log_booking_event may not exist yet on the AuditLogger — fall back
        # to log_event with the BOOKING module constant. (See audit_logger.py.)
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_BOOKING,
            action_type="CANCELLATION_ADMIN_FORCE",
            user_id=current_user["sub"],
            entity_name="cab_booking",
            entity_id=cab_booking_id,
            old_values={"cab_booking_status": cb[2]},
            new_values={
                "cab_booking_status": "CANCELLED",
                "charge": float(result["charge"]),
                "refund_amount": float(result["refund_amount"]),
                "tier_label": result["tier_label"],
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    await _fire_cancellation_notifications(
        db,
        master_booking_id=int(cb[1]),
        cab_booking_id=cab_booking_id,
        hotel_reservation_id=None,
        cancelled_source=CANCEL_SOURCE_ADMIN,
        cancelled_by_role=current_user.get("role", "ADMIN"),
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )
    await db.commit()

    # ── Email: cancelled + refund to the customer ──
    await _send_cancellation_emails(
        db,
        master_booking_id=int(cb[1]),
        service="Cab",
        booking_number=cb[3],
        refund_amount=float(result["refund_amount"]),
        charge=float(result["charge"]),
        related_type="CAB_BOOKING",
        related_id=cab_booking_id,
    )

    # Real-time fan-out: refresh booking pages on both portals.
    try:
        from app.modules.notification.services.booking_notifications import (
            notify_booking_updated,
        )
        from app.modules.booking.models import CabBooking as _NCB
        from sqlalchemy import select as _nc_select

        _cb_row = (
            await db.execute(_nc_select(_NCB).where(_NCB.id == cab_booking_id))
        ).scalar_one_or_none()
        await notify_booking_updated(
            db,
            service_type="CAB",
            master_booking_id=int(cb[1]),
            service_id=cab_booking_id,
            booking_number=(_cb_row.booking_number if _cb_row else str(cab_booking_id)),
            service_status="CANCELLED",
            action="ADMIN_CANCEL",
            payment_status=None,
            partner_ids=(
                [a.partner_id for a in (_cb_row.assignments or [])] if _cb_row else []
            ),
        )
    except Exception:  # pragma: no cover - best-effort
        pass

    return {
        "success": True,
        "message": "Cab cancelled; refund credited to customer wallet.",
        **result,
    }


@router.post(
    "/hotel/{reservation_id}/cancel",
    tags=["Admin Cancellation"],
    summary="Admin force-cancel a hotel reservation. Runs the policy engine and auto-refunds.",
)
async def admin_cancel_hotel(
    reservation_id: int,
    payload: AdminCancelHotelIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    hr = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, reservation_status, reservation_number
                FROM hotel_reservations WHERE id = :id
                """
            ),
            {"id": reservation_id},
        )
    ).first()
    if not hr:
        raise HTTPException(404, "Hotel reservation not found")
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
        raise HTTPException(
            400,
            f"Cannot cancel: reservation is in '{hr[2]}'. Active or terminal reservations cannot be cancelled.",
        )

    result = await apply_hotel_cancellation(
        db,
        hotel_reservation_id=reservation_id,
        master_booking_id=int(hr[1]),
        cancellation_reason=payload.reason,
        cancelled_by_user_id=UUID(current_user["sub"]),
        cancelled_by_role=current_user.get("role", "ADMIN"),
        cancelled_source=CANCEL_SOURCE_ADMIN,
    )

    try:
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_HOTEL,
            action_type="CANCELLATION_ADMIN_FORCE",
            user_id=current_user["sub"],
            entity_name="hotel_reservation",
            entity_id=reservation_id,
            old_values={"reservation_status": hr[2]},
            new_values={
                "reservation_status": "CANCELLED",
                "charge": float(result["charge"]),
                "refund_amount": float(result["refund_amount"]),
                "tier_label": result["tier_label"],
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("cancel.audit_failed err=%s", exc)

    await _fire_cancellation_notifications(
        db,
        master_booking_id=int(hr[1]),
        cab_booking_id=None,
        hotel_reservation_id=reservation_id,
        cancelled_source=CANCEL_SOURCE_ADMIN,
        cancelled_by_role=current_user.get("role", "ADMIN"),
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )
    await db.commit()

    # ── Email: cancelled + refund to the customer ──
    await _send_cancellation_emails(
        db,
        master_booking_id=int(hr[1]),
        service="Hotel",
        booking_number=hr[3] or str(reservation_id),
        refund_amount=float(result["refund_amount"]),
        charge=float(result["charge"]),
        related_type="HOTEL_RESERVATION",
        related_id=reservation_id,
    )
    return {
        "success": True,
        "message": "Hotel reservation cancelled; refund credited to customer wallet.",
        **result,
    }


@router.post(
    "/tour/{tour_booking_id}/cancel",
    tags=["Admin Cancellation"],
    summary="Admin force-cancel a tour booking. Runs the policy engine and auto-refunds.",
)
async def admin_cancel_tour(
    tour_booking_id: int,
    payload: AdminCancelTourIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    tb = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, booking_status, booking_number
                FROM tour_bookings WHERE id = :id
                """
            ),
            {"id": tour_booking_id},
        )
    ).first()
    if not tb:
        raise HTTPException(404, "Tour booking not found")
    blocked = {
        "IN_PROGRESS",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if tb[2] in blocked:
        raise HTTPException(
            400,
            f"Cannot cancel: tour booking is in '{tb[2]}'. Active or terminal trips cannot be cancelled.",
        )

    result = await apply_tour_cancellation(
        db,
        tour_booking_id=tour_booking_id,
        master_booking_id=int(tb[1]),
        cancellation_reason=payload.reason,
        cancelled_by_user_id=UUID(current_user["sub"]),
        cancelled_by_role=current_user.get("role", "ADMIN"),
        cancelled_source=CANCEL_SOURCE_ADMIN,
    )

    try:
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_BOOKING,
            action_type="CANCELLATION_ADMIN_FORCE",
            user_id=current_user["sub"],
            entity_name="tour_booking",
            entity_id=tour_booking_id,
            old_values={"booking_status": tb[2]},
            new_values={
                "booking_status": "CANCELLED",
                "charge": float(result["charge"]),
                "refund_amount": float(result["refund_amount"]),
                "tier_label": result["tier_label"],
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("cancel.audit_failed err=%s", exc)

    await _fire_cancellation_notifications(
        db,
        master_booking_id=int(tb[1]),
        cab_booking_id=None,
        hotel_reservation_id=None,
        tour_booking_id=tour_booking_id,
        cancelled_source=CANCEL_SOURCE_ADMIN,
        cancelled_by_role=current_user.get("role", "ADMIN"),
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )
    await db.commit()

    # ── Email: cancelled + refund to the customer ──
    await _send_cancellation_emails(
        db,
        master_booking_id=int(tb[1]),
        service="Tour package",
        booking_number=tb[3],
        refund_amount=float(result["refund_amount"]),
        charge=float(result["charge"]),
        related_type="TOUR_BOOKING",
        related_id=tour_booking_id,
    )
    return {
        "success": True,
        "message": "Tour booking cancelled; refund credited to customer wallet.",
        **result,
    }


# ════════════════════════════════════════════════════════════════
# 3. CAB LADDER POLICY EDITOR (admin) + READ-ONLY HOTEL LADDER
# ════════════════════════════════════════════════════════════════


CAB_LADDER_KEYS = {
    "CANCELLATION_FREE_HOURS_CAB",
    "CANCELLATION_TIER_1_HOURS_CAB",
    "CANCELLATION_TIER_1_PERCENT_CAB",
    "CANCELLATION_TIER_2_HOURS_CAB",
    "CANCELLATION_TIER_2_PERCENT_CAB",
    "CANCELLATION_SAME_DAY_PERCENT_CAB",
    "CANCELLATION_AFTER_ASSIGNMENT_PERCENT_CAB",
}


@router.get(
    "/policy/cab",
    tags=["Admin Cancellation"],
    summary="Read the current global cab cancellation ladder.",
)
async def get_cab_ladder(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await get_cab_policy_effective(db)


@router.put(
    "/policy/cab/{config_key}",
    tags=["Admin Cancellation"],
    summary="Update one cab ladder key. Records an audit row in cancellation_policy_versions.",
)
async def update_cab_ladder_key(
    config_key: str,
    payload: CabLadderUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    if config_key not in CAB_LADDER_KEYS:
        raise HTTPException(
            400,
            f"Unknown cab ladder key '{config_key}'. Valid keys: {sorted(CAB_LADDER_KEYS)}",
        )

    # Light validation — must parse as int or decimal.
    try:
        if "PERCENT" in config_key:
            v = Decimal(payload.new_value)
            if v < 0 or v > 100:
                raise ValueError("percent must be between 0 and 100")
        else:
            v = int(payload.new_value)
            if v < 0:
                raise ValueError("hours must be >= 0")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid value for {config_key}: {exc}")

    existing = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": config_key},
        )
    ).first()
    previous_value = existing[0] if existing else None

    # Audit BEFORE write — so even a crash leaves the trail.
    await record_policy_version(
        db,
        config_key=config_key,
        previous_value=previous_value,
        new_value=payload.new_value,
        changed_by_user_id=UUID(current_user["sub"]),
        change_reason=payload.change_reason,
    )
    if existing is None:
        await db.execute(
            text(
                """
                INSERT INTO system_configurations (config_key, config_value, description, updated_at)
                VALUES (:k, :v, :d, NOW())
                """
            ),
            {
                "k": config_key,
                "v": payload.new_value,
                "d": "Cab cancellation ladder (admin)",
            },
        )
    else:
        await db.execute(
            text(
                "UPDATE system_configurations SET config_value = :v, updated_at = NOW() WHERE config_key = :k"
            ),
            {"k": config_key, "v": payload.new_value},
        )

    try:
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_CONFIG,
            action_type="CANCELLATION_POLICY_UPDATED",
            user_id=current_user["sub"],
            entity_name="system_config",
            entity_id=None,
            old_values={"config_key": config_key, "value": previous_value},
            new_values={"config_key": config_key, "value": payload.new_value},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("policy_edit.audit_failed err=%s", exc)

    await db.commit()
    return {
        "success": True,
        "message": f"Cab ladder key '{config_key}' updated.",
        "config_key": config_key,
        "previous_value": previous_value,
        "new_value": payload.new_value,
    }


@router.get(
    "/policy/cab/history",
    tags=["Admin Cancellation"],
    summary="Audit history of cab ladder edits.",
)
async def get_cab_ladder_history(
    config_key: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if config_key and config_key not in CAB_LADDER_KEYS:
        raise HTTPException(400, f"Unknown cab ladder key '{config_key}'")

    q = "SELECT id, config_key, previous_value, new_value, changed_by_user_id, change_reason, created_at FROM cancellation_policy_versions WHERE 1=1"
    params: Dict[str, Any] = {}
    if config_key:
        q += " AND config_key = :k"
        params["k"] = config_key
    q += " ORDER BY created_at DESC"
    total = (
        await db.execute(text(f"SELECT COUNT(*) FROM ({q}) sub"), params)
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                text(q + " LIMIT :lim OFFSET :off"),
                {**params, "lim": page_size, "off": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "config_key": r["config_key"],
            "previous_value": r["previous_value"],
            "new_value": r["new_value"],
            "changed_by_user_id": (
                str(r["changed_by_user_id"]) if r["changed_by_user_id"] else None
            ),
            "change_reason": r["change_reason"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total) + page_size - 1) // page_size if total else 1,
        "items": items,
    }


# ════════════════════════════════════════════════════════════════
# 3b. TOUR LADDER EDITOR (global) + PER-PACKAGE POLICY
# Doc Ref: BRD Part 5 §119
# ════════════════════════════════════════════════════════════════


TOUR_LADDER_KEYS = {
    "TOUR_CANCELLATION_FREE_DAYS",
    "TOUR_CANCELLATION_TIER_1_DAYS",
    "TOUR_CANCELLATION_TIER_1_PERCENT",
    "TOUR_CANCELLATION_TIER_2_DAYS",
    "TOUR_CANCELLATION_TIER_2_PERCENT",
    "TOUR_CANCELLATION_TIER_3_PERCENT",
    "TOUR_CANCELLATION_LAST_MINUTE_PERCENT",
}


@router.get(
    "/policy/tour",
    tags=["Admin Cancellation"],
    summary="Read the current global tour cancellation ladder.",
)
async def get_tour_ladder(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    return await get_tour_policy_effective(db)


@router.put(
    "/policy/tour/{config_key}",
    tags=["Admin Cancellation"],
    summary="Update one global tour ladder key. Records an audit row in cancellation_policy_versions.",
)
async def update_tour_ladder_key(
    config_key: str,
    payload: CabLadderUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    if config_key not in TOUR_LADDER_KEYS:
        raise HTTPException(
            400,
            f"Unknown tour ladder key '{config_key}'. Valid keys: {sorted(TOUR_LADDER_KEYS)}",
        )

    try:
        if "PERCENT" in config_key:
            v = Decimal(payload.new_value)
            if v < 0 or v > 100:
                raise ValueError("percent must be between 0 and 100")
        else:
            v = int(payload.new_value)
            if v < 0:
                raise ValueError("days must be >= 0")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid value for {config_key}: {exc}")

    existing = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": config_key},
        )
    ).first()
    previous_value = existing[0] if existing else None

    await record_policy_version(
        db,
        config_key=config_key,
        previous_value=previous_value,
        new_value=payload.new_value,
        changed_by_user_id=UUID(current_user["sub"]),
        change_reason=payload.change_reason,
    )
    if existing is None:
        await db.execute(
            text(
                """
                INSERT INTO system_configurations (config_key, config_value, description, updated_at)
                VALUES (:k, :v, :d, NOW())
                """
            ),
            {
                "k": config_key,
                "v": payload.new_value,
                "d": "Tour cancellation ladder (admin)",
            },
        )
    else:
        await db.execute(
            text(
                "UPDATE system_configurations SET config_value = :v, updated_at = NOW() WHERE config_key = :k"
            ),
            {"k": config_key, "v": payload.new_value},
        )

    try:
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_CONFIG,
            action_type="CANCELLATION_POLICY_UPDATED",
            user_id=current_user["sub"],
            entity_name="system_config",
            entity_id=None,
            old_values={"config_key": config_key, "value": previous_value},
            new_values={"config_key": config_key, "value": payload.new_value},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("policy_edit.audit_failed err=%s", exc)

    await db.commit()
    return {
        "success": True,
        "message": f"Tour ladder key '{config_key}' updated.",
        "config_key": config_key,
        "previous_value": previous_value,
        "new_value": payload.new_value,
    }


@router.get(
    "/policy/tour/history",
    tags=["Admin Cancellation"],
    summary="Audit history of global tour ladder edits.",
)
async def get_tour_ladder_history(
    config_key: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if config_key and config_key not in TOUR_LADDER_KEYS:
        raise HTTPException(400, f"Unknown tour ladder key '{config_key}'")

    q = "SELECT id, config_key, previous_value, new_value, changed_by_user_id, change_reason, created_at FROM cancellation_policy_versions WHERE config_key LIKE 'TOUR_CANCELLATION_%'"
    params: Dict[str, Any] = {}
    if config_key:
        q += " AND config_key = :k"
        params["k"] = config_key
    q += " ORDER BY created_at DESC"
    total = (
        await db.execute(text(f"SELECT COUNT(*) FROM ({q}) sub"), params)
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                text(q + " LIMIT :lim OFFSET :off"),
                {**params, "lim": page_size, "off": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "config_key": r["config_key"],
            "previous_value": r["previous_value"],
            "new_value": r["new_value"],
            "changed_by_user_id": (
                str(r["changed_by_user_id"]) if r["changed_by_user_id"] else None
            ),
            "change_reason": r["change_reason"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total) + page_size - 1) // page_size if total else 1,
        "items": items,
    }


@router.get(
    "/policy/tour-package/{package_id}",
    tags=["Admin Cancellation"],
    summary="Read the cancellation ladder for a tour package (defaults if none set).",
)
async def get_tour_package_policy(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT tpp.id, tpp.tour_package_id, tpp.cancellation_free_days,
                       tpp.cancellation_tier_1_days, tpp.refund_percent_tier_1,
                       tpp.cancellation_tier_2_days, tpp.refund_percent_tier_2,
                       tpp.refund_percent_tier_3, tpp.refund_percent_last_minute,
                       tpp.cancellation_policy_text, tpp.updated_at
                FROM tour_package_policies tpp
                WHERE tpp.tour_package_id = :id
                """
                ),
                {"id": package_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(
            404,
            "No cancellation policy row for this tour package yet — the global tour ladder applies.",
        )
    return dict(row)


@router.put(
    "/policy/tour-package/{package_id}",
    tags=["Admin Cancellation"],
    summary="Upsert the cancellation ladder for a tour package.",
)
async def update_tour_package_policy(
    package_id: int,
    payload: TourPackagePolicyIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    pkg = (
        await db.execute(
            text("SELECT id FROM tour_packages WHERE id = :id"), {"id": package_id}
        )
    ).first()
    if not pkg:
        raise HTTPException(404, "Tour package not found")

    existing = (
        await db.execute(
            text("SELECT id FROM tour_package_policies WHERE tour_package_id = :id"),
            {"id": package_id},
        )
    ).first()
    if existing is None:
        await db.execute(
            text(
                """
                INSERT INTO tour_package_policies
                    (tour_package_id, cancellation_free_days, cancellation_tier_1_days,
                     refund_percent_tier_1, cancellation_tier_2_days, refund_percent_tier_2,
                     refund_percent_tier_3, refund_percent_last_minute,
                     cancellation_policy_text, created_at, updated_at)
                VALUES (:pid, :fd, :t1d, :t1p, :t2d, :t2p, :t3p, :lm, :txt, NOW(), NOW())
                """
            ),
            {
                "pid": package_id,
                "fd": payload.cancellation_free_days,
                "t1d": payload.cancellation_tier_1_days,
                "t1p": payload.refund_percent_tier_1,
                "t2d": payload.cancellation_tier_2_days,
                "t2p": payload.refund_percent_tier_2,
                "t3p": payload.refund_percent_tier_3,
                "lm": payload.refund_percent_last_minute,
                "txt": payload.cancellation_policy_text,
            },
        )
    else:
        await db.execute(
            text(
                """
                UPDATE tour_package_policies
                   SET cancellation_free_days     = :fd,
                       cancellation_tier_1_days   = :t1d,
                       refund_percent_tier_1      = :t1p,
                       cancellation_tier_2_days   = :t2d,
                       refund_percent_tier_2      = :t2p,
                       refund_percent_tier_3      = :t3p,
                       refund_percent_last_minute = :lm,
                       cancellation_policy_text   = :txt,
                       updated_at                  = NOW()
                 WHERE tour_package_id = :pid
                """
            ),
            {
                "pid": package_id,
                "fd": payload.cancellation_free_days,
                "t1d": payload.cancellation_tier_1_days,
                "t1p": payload.refund_percent_tier_1,
                "t2d": payload.cancellation_tier_2_days,
                "t2p": payload.refund_percent_tier_2,
                "t3p": payload.refund_percent_tier_3,
                "lm": payload.refund_percent_last_minute,
                "txt": payload.cancellation_policy_text,
            },
        )

    try:
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_CONFIG,
            action_type="CANCELLATION_POLICY_UPDATED",
            user_id=current_user["sub"],
            entity_name="tour_package_policy",
            entity_id=package_id,
            old_values={"had_row": existing is not None},
            new_values=payload.model_dump(),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("policy_edit.audit_failed err=%s", exc)

    await db.commit()
    return {
        "success": True,
        "message": f"Tour package #{package_id} cancellation policy saved.",
        "tour_package_id": package_id,
    }


@router.get(
    "/policy/hotel/{hotel_id}",
    tags=["Admin Cancellation"],
    summary="Read the cancellation ladder for a single hotel.",
)
async def get_hotel_ladder(
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, hotel_id, cancellation_free_hours, refund_percent_tier_1,
                       cancellation_tier_2_hours, refund_percent_tier_2,
                       cancellation_tier_3_hours, refund_percent_tier_3,
                       refund_percent_same_day, no_show_refund_percent,
                       cancellation_policy_text, updated_at
                FROM hotel_policies WHERE hotel_id = :id
                """
                ),
                {"id": hotel_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(404, "No cancellation policy row for this hotel yet.")
    return dict(row)


# ════════════════════════════════════════════════════════════════
# 4. PARTNER CANCELLATION REQUEST INBOX (admin review)
# ════════════════════════════════════════════════════════════════


@router.get(
    "/requests",
    response_model=PaginatedCancelRequests,
    tags=["Admin Cancellation"],
    summary="Paginated inbox of partner cancellation requests (filter by status).",
)
async def list_cancel_requests(
    status: Optional[str] = Query(
        None, description="PENDING | APPROVED | REJECTED | WITHDRAWN | AUTO_CLOSED"
    ),
    booking_type: Optional[str] = Query(None, description="CAB | HOTEL | TOUR"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    q = (
        "SELECT r.*, "
        "       mb.booking_number AS master_booking_number, "
        "       cb.booking_number AS cab_booking_number, "
        "       hr.reservation_number AS hotel_reservation_number, "
        "       tb.booking_number AS tour_booking_number "
        "FROM booking_cancellation_requests r "
        "LEFT JOIN master_bookings mb ON mb.id = r.master_booking_id "
        "LEFT JOIN cab_bookings cb ON cb.id = r.cab_booking_id "
        "LEFT JOIN hotel_reservations hr ON hr.id = r.hotel_reservation_id "
        "LEFT JOIN tour_bookings tb ON tb.id = r.tour_booking_id "
        "WHERE 1=1"
    )
    params: Dict[str, Any] = {}
    if status:
        q += " AND r.status = :s"
        params["s"] = status.upper()
    if booking_type:
        q += " AND r.booking_type = :bt"
        params["bt"] = booking_type.upper()
    q += " ORDER BY r.requested_at DESC"

    total = (
        await db.execute(text(f"SELECT COUNT(*) FROM ({q}) sub"), params)
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                text(q + " LIMIT :lim OFFSET :off"),
                {**params, "lim": page_size, "off": (page - 1) * page_size},
            )
        )
        .mappings()
        .all()
    )
    items = [
        {
            "id": int(r["id"]),
            "booking_type": r["booking_type"],
            "master_booking_id": r["master_booking_id"],
            "cab_booking_id": r["cab_booking_id"],
            "hotel_reservation_id": r["hotel_reservation_id"],
            "tour_booking_id": r["tour_booking_id"],
            "master_booking_number": r["master_booking_number"],
            "cab_booking_number": r["cab_booking_number"],
            "hotel_reservation_number": r["hotel_reservation_number"],
            "tour_booking_number": r["tour_booking_number"],
            "requested_by_user_id": (
                str(r["requested_by_user_id"]) if r["requested_by_user_id"] else None
            ),
            "requested_at": (
                r["requested_at"].isoformat() if r["requested_at"] else None
            ),
            "requested_reason": r["requested_reason"],
            "requested_reason_code": r["requested_reason_code"],
            "refund_preview_json": r["refund_preview_json"],
            "status": r["status"],
            "reviewed_by_user_id": (
                str(r["reviewed_by_user_id"]) if r["reviewed_by_user_id"] else None
            ),
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "review_note": r["review_note"],
        }
        for r in rows
    ]
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": (int(total) + page_size - 1) // page_size if total else 1,
        "items": items,
    }


@router.post(
    "/requests/{request_id}/review",
    tags=["Admin Cancellation"],
    summary="Approve / reject / withdraw a partner cancellation request.",
)
async def review_cancel_request(
    request_id: int,
    payload: RequestReviewIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    decision = (payload.decision or "").upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise HTTPException(400, "decision must be APPROVED or REJECTED")

    req = (
        (
            await db.execute(
                text(
                    """
                SELECT id, booking_type, cab_booking_id, hotel_reservation_id,
                       tour_booking_id, master_booking_id, status,
                       refund_preview_json, requested_by_user_id
                FROM booking_cancellation_requests WHERE id = :id
                """
                ),
                {"id": request_id},
            )
        )
        .mappings()
        .first()
    )
    if not req:
        raise HTTPException(404, "Cancellation request not found")
    if req["status"] not in {"PENDING"}:
        raise HTTPException(
            400, f"Request is already '{req['status']}' — no further action."
        )

    actor_uid = UUID(current_user["sub"])
    actor_role = current_user.get("role", "ADMIN")

    if decision == "REJECTED":
        await db.execute(
            text(
                """
                UPDATE booking_cancellation_requests
                   SET status = 'REJECTED',
                       reviewed_by_user_id = :uid,
                       reviewed_at = NOW(),
                       review_note = :note
                 WHERE id = :id
                """
            ),
            {"uid": str(actor_uid), "note": payload.note, "id": request_id},
        )
        await _log_req_timeline(
            db, int(req["master_booking_id"]), req, decision, actor_uid, payload.note
        )
        await db.commit()
        return {
            "success": True,
            "message": "Cancellation request rejected.",
            "request_id": request_id,
            "status": "REJECTED",
        }

    # APPROVED — run the policy engine for the right booking type.
    # TOCTOU guard: the service may have started / been paid / settled / been
    # cancelled by someone else while the request sat in the queue. Re-check
    # the live status and refuse to force-cancel a non-cancellable trip.
    if req["booking_type"] == "CAB":
        _cur = (
            await db.execute(
                text("SELECT booking_status FROM cab_bookings WHERE id = :id"),
                {"id": req["cab_booking_id"]},
            )
        ).first()
        _cur_status = (_cur[0] if _cur else None) or ""
        if _cur_status in {
            "STARTED",
            "COMPLETED",
            "SETTLEMENT_PENDING",
            "SETTLED",
            "CANCELLED",
            "IN_PROGRESS",
        }:
            raise HTTPException(
                409,
                f"Cannot approve cancellation: cab is now '{_cur_status}'. "
                "Cancellation is only allowed before the trip starts.",
            )
        result = await apply_cab_cancellation(
            db,
            cab_booking_id=int(req["cab_booking_id"]),
            master_booking_id=int(req["master_booking_id"]),
            cancellation_reason=f"Partner-requested cancellation (approved). Original reason: {req['requested_reason']}",
            cancelled_by_user_id=actor_uid,
            cancelled_by_role=actor_role,
            cancelled_source=CANCEL_SOURCE_PARTNER_REQUEST,
        )
    elif req["booking_type"] == "HOTEL":
        _cur = (
            await db.execute(
                text(
                    "SELECT reservation_status FROM hotel_reservations WHERE id = :id"
                ),
                {"id": req["hotel_reservation_id"]},
            )
        ).first()
        _cur_status = (_cur[0] if _cur else None) or ""
        if _cur_status in {
            "CHECKED_IN",
            "IN_HOUSE",
            "CHECKED_OUT",
            "COMPLETED",
            "SETTLED",
            "CANCELLED",
            "REJECTED",
        }:
            raise HTTPException(
                409,
                f"Cannot approve cancellation: reservation is now '{_cur_status}'. "
                "Cancellation is only allowed before check-in.",
            )
        result = await apply_hotel_cancellation(
            db,
            hotel_reservation_id=int(req["hotel_reservation_id"]),
            master_booking_id=int(req["master_booking_id"]),
            cancellation_reason=f"Partner-requested cancellation (approved). Original reason: {req['requested_reason']}",
            cancelled_by_user_id=actor_uid,
            cancelled_by_role=actor_role,
            cancelled_source=CANCEL_SOURCE_PARTNER_REQUEST,
        )
    else:
        _cur = (
            await db.execute(
                text("SELECT booking_status FROM tour_bookings WHERE id = :id"),
                {"id": req["tour_booking_id"]},
            )
        ).first()
        _cur_status = (_cur[0] if _cur else None) or ""
        if _cur_status in {
            "IN_PROGRESS",
            "COMPLETED",
            "SETTLEMENT_PENDING",
            "SETTLED",
            "CANCELLED",
        }:
            raise HTTPException(
                409,
                f"Cannot approve cancellation: tour booking is now '{_cur_status}'. "
                "Cancellation is only allowed before the trip starts.",
            )
        result = await apply_tour_cancellation(
            db,
            tour_booking_id=int(req["tour_booking_id"]),
            master_booking_id=int(req["master_booking_id"]),
            cancellation_reason=f"Partner-requested cancellation (approved). Original reason: {req['requested_reason']}",
            cancelled_by_user_id=actor_uid,
            cancelled_by_role=actor_role,
            cancelled_source=CANCEL_SOURCE_PARTNER_REQUEST,
        )

    await db.execute(
        text(
            """
            UPDATE booking_cancellation_requests
               SET status = 'APPROVED',
                   reviewed_by_user_id = :uid,
                   reviewed_at = NOW(),
                   review_note = :note
             WHERE id = :id
            """
        ),
        {"uid": str(actor_uid), "note": payload.note, "id": request_id},
    )
    await _log_req_timeline(
        db, int(req["master_booking_id"]), req, decision, actor_uid, payload.note
    )
    await _fire_cancellation_notifications(
        db,
        master_booking_id=int(req["master_booking_id"]),
        cab_booking_id=int(req["cab_booking_id"]) if req["cab_booking_id"] else None,
        hotel_reservation_id=(
            int(req["hotel_reservation_id"]) if req["hotel_reservation_id"] else None
        ),
        tour_booking_id=(
            int(req["tour_booking_id"]) if req["tour_booking_id"] else None
        ),
        cancelled_source=CANCEL_SOURCE_PARTNER_REQUEST,
        cancelled_by_role=actor_role,
        refund_amount=Decimal(result["refund_amount"]),
        tier_label=result["tier_label"],
    )

    try:
        await AuditLogger.log_event(
            db,
            module_name=AuditLogger.MODULE_BOOKING,
            action_type="CANCELLATION_REQUEST_APPROVED",
            user_id=current_user["sub"],
            entity_name="cancellation_request",
            entity_id=request_id,
            old_values={"status": "PENDING"},
            new_values={
                "status": "APPROVED",
                "refund_amount": float(result["refund_amount"]),
                "tier_label": result["tier_label"],
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("cancel.audit_failed err=%s", exc)

    await db.commit()
    return {
        "success": True,
        "message": "Cancellation approved and applied.",
        "request_id": request_id,
        "status": "APPROVED",
        **result,
    }


async def _log_req_timeline(
    db: AsyncSession,
    master_booking_id: int,
    req: Dict[str, Any],
    decision: str,
    actor_uid: UUID,
    note: Optional[str],
) -> None:
    """Write a booking_timeline entry for a request review."""
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp, created_by)
            VALUES (:m, :evt, :desc, NOW(), :uid)
            """
        ),
        {
            "m": master_booking_id,
            "evt": f"CANCELLATION_REQUEST_{decision}",
            "desc": (
                f"Cancellation request #{req['id']} {decision.lower()} by admin. "
                f"Reason: {req['requested_reason'][:200]}. Note: {(note or '')[:200]}"
            ),
            "uid": str(actor_uid),
        },
    )


@router.get(
    "/requests/counts",
    tags=["Admin Cancellation"],
    summary="Counts by status for the admin sidebar badge.",
)
async def cancel_request_counts(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT status, COUNT(*) AS c FROM booking_cancellation_requests
                GROUP BY status
                """
                )
            )
        )
        .mappings()
        .all()
    )
    by_status = {r["status"]: int(r["c"]) for r in rows}
    return {
        "pending": by_status.get("PENDING", 0),
        "approved": by_status.get("APPROVED", 0),
        "rejected": by_status.get("REJECTED", 0),
        "withdrawn": by_status.get("WITHDRAWN", 0),
        "auto_closed": by_status.get("AUTO_CLOSED", 0),
        "total": sum(by_status.values()),
    }
