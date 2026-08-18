# ============================================================
# WAYTERO — PARTNER CANCELLATION REQUEST API
# File: app/modules/partner/cancellation_request_api.py
# Prefix: /partners  (registered in api/router.py)
#
# Doc Ref:
#   BRD Part 3 §46 (Partner cancellation)
#   SRS Part 3 §99 (Cancellation engine — request flow)
#
# Partners cannot cancel a booking directly. They submit a request and
# admin approves / rejects from the /admin/cancellation/requests inbox.
# On approval the engine runs and the result is recorded.
#
# Endpoints:
#   POST  /me/bookings/{cab_booking_number}/cancellation-request
#         — request admin to cancel an assigned cab booking.
#   POST  /me/hotel-bookings/{reservation_id}/cancellation-request
#         — request admin to cancel an assigned hotel reservation.
#   POST  /me/cancellation-requests/{request_id}/withdraw
#         — partner withdraws their own PENDING request.
#   GET   /me/cancellation-requests
#         — partner-side view of their own requests.
# ============================================================

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.booking.services.cancellation_policy import (
    quote_cab_cancellation,
    quote_hotel_cancellation,
    quote_tour_cancellation,
)
from app.modules.partner.models import Partner

logger = logging.getLogger("waytero.cancellation.partner")

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class PartnerCancelRequestIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)
    reason_code: Optional[str] = Field(
        None,
        description=(
            "Optional code from the partner reject-reasons vocabulary "
            "(VEHICLE_UNAVAILABLE / DRIVER_UNAVAILABLE / CAPACITY_ISSUE / "
            "OUTSIDE_SERVICE_AREA / PRICING_DISAGREEMENT / OTHER)"
        ),
    )


class PartnerCancelWithdrawIn(BaseModel):
    note: Optional[str] = Field(None, max_length=500)


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _resolve_partner(db: AsyncSession, user_uuid: str) -> Partner:
    p = (
        await db.execute(select(Partner).where(Partner.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Partner profile not found.")
    return p


async def _total_cab_advance(db: AsyncSession, cab_booking_id: int) -> Decimal:
    row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM advance_payments WHERE cab_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    return Decimal(row[0]) if row else Decimal("0")


async def _total_tour_advance(db: AsyncSession, tour_booking_id: int) -> Decimal:
    row = (
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
    return Decimal(row[0]) if row else Decimal("0")


async def _total_hotel_advance(db: AsyncSession, reservation_id: int) -> Decimal:
    row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0)
                FROM hotel_advance_payments WHERE reservation_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": reservation_id},
        )
    ).first()
    return Decimal(row[0]) if row else Decimal("0")


async def _cab_post_assignment(db: AsyncSession, cab_booking_id: int) -> bool:
    row = (
        await db.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM cab_booking_assignments WHERE cab_booking_id = :id)"
            ),
            {"id": cab_booking_id},
        )
    ).first()
    return bool(row and row[0])


async def _insert_request(
    db: AsyncSession,
    *,
    booking_type: str,
    master_booking_id: Optional[int],
    cab_booking_id: Optional[int],
    hotel_reservation_id: Optional[int],
    tour_booking_id: Optional[int] = None,
    requested_by_user_id: UUID,
    requested_reason: str,
    requested_reason_code: Optional[str],
    refund_preview_json: Dict[str, Any],
) -> int:
    import json

    row = (
        await db.execute(
            text(
                """
                INSERT INTO booking_cancellation_requests
                    (booking_type, master_booking_id, cab_booking_id, hotel_reservation_id,
                     tour_booking_id, requested_by_user_id, requested_reason,
                     requested_reason_code, refund_preview_json, status, requested_at)
                VALUES (:bt, :mb, :cb, :hr, :tb, :uid, :reason, :code,
                        CAST(:snap AS JSONB), 'PENDING', NOW())
                RETURNING id
                """
            ),
            {
                "bt": booking_type,
                "mb": master_booking_id,
                "cb": cab_booking_id,
                "hr": hotel_reservation_id,
                "tb": tour_booking_id,
                "uid": str(requested_by_user_id),
                "reason": requested_reason,
                "code": requested_reason_code,
                "snap": json.dumps(
                    refund_preview_json, default=str, ensure_ascii=False
                ),
            },
        )
    ).first()
    return int(row[0])


# ════════════════════════════════════════════════════════════════
# 1. CAB — request / withdraw
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/bookings/{cab_booking_number}/cancellation-request",
    tags=["Partner Cancellation"],
    summary="Submit a request to admin to cancel an assigned cab booking.",
)
async def request_cab_cancellation(
    cab_booking_number: str,
    payload: PartnerCancelRequestIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner = await _resolve_partner(db, current_user["sub"])

    cb = (
        await db.execute(
            text(
                """
                SELECT cb.id, cb.master_booking_id, cb.booking_status, cb.final_amount,
                       cb.estimated_amount
                FROM cab_bookings cb
                WHERE cb.booking_number = :bn
                """
            ),
            {"bn": cab_booking_number.strip().upper()},
        )
    ).first()
    if not cb:
        raise HTTPException(404, f"Booking '{cab_booking_number}' not found.")

    # Scope: partner must own at least one assignment for this cab, and
    # the latest assignment must still be active (closed_at IS NULL).
    own = (
        await db.execute(
            text(
                """
                SELECT 1 FROM cab_booking_assignments
                WHERE cab_booking_id = :cid AND partner_id = :pid
                  AND closed_at IS NULL
                LIMIT 1
                """
            ),
            {"cid": cb[0], "pid": partner.id},
        )
    ).first()
    if not own:
        raise HTTPException(
            403,
            "You are not currently assigned to this booking — only the active "
            "partner may request cancellation.",
        )

    blocked = {
        "IN_PROGRESS",
        "STARTED",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if cb[2] in blocked:
        raise HTTPException(400, f"Cannot request cancellation: cab is '{cb[2]}'.")

    # Reject duplicates — partner already has a PENDING request for this cab.
    dupe = (
        await db.execute(
            text(
                """
                SELECT id FROM booking_cancellation_requests
                WHERE cab_booking_id = :cid AND status = 'PENDING'
                LIMIT 1
                """
            ),
            {"cid": cb[0]},
        )
    ).first()
    if dupe:
        raise HTTPException(
            409,
            f"A cancellation request is already pending for this cab (request #{dupe[0]}). "
            "Withdraw it first if you want to change the reason.",
        )

    cab_total = Decimal(cb[3] or cb[4] or 0)
    advance_total = await _total_cab_advance(db, int(cb[0]))
    is_post = await _cab_post_assignment(db, int(cb[0]))
    quote = await quote_cab_cancellation(
        db,
        cab_booking_id=int(cb[0]),
        cab_total_amount=cab_total,
        advance_paid_total=advance_total,
        is_post_assignment=is_post,
    )

    preview = {
        "cab_booking_id": int(cb[0]),
        "master_booking_id": int(cb[1]),
        "charge": float(quote.charge),
        "refund_amount": float(quote.refund_amount),
        "refund_percent": float(quote.refund_percent),
        "tier_label": quote.tier_label,
        "policy_snapshot": quote.policy_snapshot,
        "advance_paid_total": float(advance_total),
        "is_post_assignment": is_post,
    }

    request_id = await _insert_request(
        db,
        booking_type="CAB",
        master_booking_id=int(cb[1]),
        cab_booking_id=int(cb[0]),
        hotel_reservation_id=None,
        requested_by_user_id=UUID(current_user["sub"]),
        requested_reason=payload.reason,
        requested_reason_code=payload.reason_code,
        refund_preview_json=preview,
    )
    # Timeline breadcrumb so admin / customer see the request right away.
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp, created_by)
            VALUES (:m, 'CANCELLATION_REQUESTED',
                    :desc, NOW(), :uid)
            """
        ),
        {
            "m": int(cb[1]),
            "desc": (
                f"Partner requested cab cancellation (request #{request_id}). "
                f"Reason: {payload.reason[:200]}"
            ),
            "uid": current_user["sub"],
        },
    )
    await db.commit()
    return {
        "success": True,
        "message": "Cancellation request submitted — admin will review and act per policy.",
        "request_id": request_id,
        "preview": preview,
    }


# ════════════════════════════════════════════════════════════════
# 2. HOTEL — request / withdraw
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/hotel-bookings/{reservation_id}/cancellation-request",
    tags=["Partner Cancellation"],
    summary="Submit a request to admin to cancel an assigned hotel reservation.",
)
async def request_hotel_cancellation(
    reservation_id: int,
    payload: PartnerCancelRequestIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner = await _resolve_partner(db, current_user["sub"])

    hr = (
        await db.execute(
            text(
                """
                SELECT hr.id, hr.master_booking_id, hr.reservation_status,
                       hr.total_amount, h.partner_id
                FROM hotel_reservations hr
                JOIN hotels h ON h.id = hr.hotel_id
                WHERE hr.id = :id
                """
            ),
            {"id": reservation_id},
        )
    ).first()
    if not hr:
        raise HTTPException(404, "Hotel reservation not found.")
    if int(hr[4]) != partner.id:
        raise HTTPException(403, "This reservation belongs to a different partner.")

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
            400, f"Cannot request cancellation: reservation is '{hr[2]}'."
        )

    dupe = (
        await db.execute(
            text(
                """
                SELECT id FROM booking_cancellation_requests
                WHERE hotel_reservation_id = :rid AND status = 'PENDING'
                LIMIT 1
                """
            ),
            {"rid": reservation_id},
        )
    ).first()
    if dupe:
        raise HTTPException(
            409,
            f"A cancellation request is already pending for this reservation (request #{dupe[0]}).",
        )

    hotel_total = Decimal(hr[3] or 0)
    advance_total = await _total_hotel_advance(db, int(hr[0]))
    quote = await quote_hotel_cancellation(
        db,
        hotel_reservation_id=int(hr[0]),
        hotel_total_amount=hotel_total,
        advance_paid_total=advance_total,
    )

    preview = {
        "hotel_reservation_id": int(hr[0]),
        "master_booking_id": int(hr[1]),
        "charge": float(quote.charge),
        "refund_amount": float(quote.refund_amount),
        "refund_percent": float(quote.refund_percent),
        "tier_label": quote.tier_label,
        "policy_snapshot": quote.policy_snapshot,
        "advance_paid_total": float(advance_total),
    }

    request_id = await _insert_request(
        db,
        booking_type="HOTEL",
        master_booking_id=int(hr[1]),
        cab_booking_id=None,
        hotel_reservation_id=int(hr[0]),
        requested_by_user_id=UUID(current_user["sub"]),
        requested_reason=payload.reason,
        requested_reason_code=payload.reason_code,
        refund_preview_json=preview,
    )
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp, created_by)
            VALUES (:m, 'CANCELLATION_REQUESTED',
                    :desc, NOW(), :uid)
            """
        ),
        {
            "m": int(hr[1]),
            "desc": (
                f"Partner requested hotel cancellation (request #{request_id}). "
                f"Reason: {payload.reason[:200]}"
            ),
            "uid": current_user["sub"],
        },
    )
    await db.commit()
    return {
        "success": True,
        "message": "Cancellation request submitted — admin will review and act per policy.",
        "request_id": request_id,
        "preview": preview,
    }


# ════════════════════════════════════════════════════════════════
# 2b. TOUR — request (Doc Ref: BRD Part 5 §119)
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/tour-bookings/{tour_booking_id}/cancellation-request",
    tags=["Partner Cancellation"],
    summary="Submit a request to admin to cancel one of my tour bookings.",
)
async def request_tour_cancellation(
    tour_booking_id: int,
    payload: PartnerCancelRequestIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    partner = await _resolve_partner(db, current_user["sub"])

    tb = (
        await db.execute(
            text(
                """
                SELECT tb.id, tb.master_booking_id, tb.booking_status,
                       tb.total_amount, tp.partner_id
                FROM tour_bookings tb
                JOIN tour_packages tp ON tp.id = tb.package_id
                WHERE tb.id = :id
                """
            ),
            {"id": tour_booking_id},
        )
    ).first()
    if not tb:
        raise HTTPException(404, "Tour booking not found.")
    if int(tb[4]) != partner.id:
        raise HTTPException(403, "This tour booking belongs to a different partner.")

    blocked = {
        "IN_PROGRESS",
        "COMPLETED",
        "SETTLEMENT_PENDING",
        "SETTLED",
        "CANCELLED",
    }
    if tb[2] in blocked:
        raise HTTPException(
            400, f"Cannot request cancellation: tour booking is '{tb[2]}'."
        )

    dupe = (
        await db.execute(
            text(
                """
                SELECT id FROM booking_cancellation_requests
                WHERE tour_booking_id = :tid AND status = 'PENDING'
                LIMIT 1
                """
            ),
            {"tid": tour_booking_id},
        )
    ).first()
    if dupe:
        raise HTTPException(
            409,
            f"A cancellation request is already pending for this tour booking (request #{dupe[0]}).",
        )

    tour_total = Decimal(tb[3] or 0)
    advance_total = await _total_tour_advance(db, int(tb[0]))
    quote = await quote_tour_cancellation(
        db,
        tour_booking_id=int(tb[0]),
        tour_total_amount=tour_total,
        advance_paid_total=advance_total,
    )

    preview = {
        "tour_booking_id": int(tb[0]),
        "master_booking_id": int(tb[1]),
        "charge": float(quote.charge),
        "refund_amount": float(quote.refund_amount),
        "refund_percent": float(quote.refund_percent),
        "tier_label": quote.tier_label,
        "policy_snapshot": quote.policy_snapshot,
        "advance_paid_total": float(advance_total),
    }

    request_id = await _insert_request(
        db,
        booking_type="TOUR",
        master_booking_id=int(tb[1]),
        cab_booking_id=None,
        hotel_reservation_id=None,
        tour_booking_id=int(tb[0]),
        requested_by_user_id=UUID(current_user["sub"]),
        requested_reason=payload.reason,
        requested_reason_code=payload.reason_code,
        refund_preview_json=preview,
    )
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp, created_by)
            VALUES (:m, 'CANCELLATION_REQUESTED',
                    :desc, NOW(), :uid)
            """
        ),
        {
            "m": int(tb[1]),
            "desc": (
                f"Partner requested tour cancellation (request #{request_id}). "
                f"Reason: {payload.reason[:200]}"
            ),
            "uid": current_user["sub"],
        },
    )
    await db.commit()
    return {
        "success": True,
        "message": "Cancellation request submitted — admin will review and act per policy.",
        "request_id": request_id,
        "preview": preview,
    }


# ════════════════════════════════════════════════════════════════
# 3. WITHDRAW a PENDING request
# ════════════════════════════════════════════════════════════════


@router.post(
    "/me/cancellation-requests/{request_id}/withdraw",
    tags=["Partner Cancellation"],
    summary="Withdraw your own PENDING cancellation request.",
)
async def withdraw_cancellation_request(
    request_id: int,
    payload: PartnerCancelWithdrawIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    req = (
        await db.execute(
            text(
                """
                SELECT id, status, requested_by_user_id, master_booking_id
                FROM booking_cancellation_requests WHERE id = :id
                """
            ),
            {"id": request_id},
        )
    ).first()
    if not req:
        raise HTTPException(404, "Cancellation request not found.")
    if str(req[1]) != str(UUID(current_user["sub"])):
        raise HTTPException(403, "You can only withdraw your own requests.")
    if req[2] != "PENDING":
        raise HTTPException(400, f"Cannot withdraw — request is '{req[2]}'.")

    await db.execute(
        text(
            """
            UPDATE booking_cancellation_requests
               SET status = 'WITHDRAWN',
                   reviewed_at = NOW(),
                   review_note = :note
             WHERE id = :id
            """
        ),
        {"note": payload.note, "id": request_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp, created_by)
            VALUES (:m, 'CANCELLATION_REQUEST_WITHDRAWN', :desc, NOW(), :uid)
            """
        ),
        {
            "m": int(req[3]),
            "desc": f"Partner withdrew cancellation request #{request_id}.",
            "uid": current_user["sub"],
        },
    )
    await db.commit()
    return {
        "success": True,
        "message": "Cancellation request withdrawn.",
        "request_id": request_id,
        "status": "WITHDRAWN",
    }


# ════════════════════════════════════════════════════════════════
# 4. PARTNER INBOX — their own requests
# ════════════════════════════════════════════════════════════════


@router.get(
    "/me/cancellation-requests",
    tags=["Partner Cancellation"],
    summary="List my own cancellation requests (PENDING + history).",
)
async def list_my_cancellation_requests(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
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
        "WHERE r.requested_by_user_id = :uid"
    )
    params: Dict[str, Any] = {"uid": current_user["sub"]}
    if status:
        q += " AND r.status = :s"
        params["s"] = status.upper()
    q += " ORDER BY r.requested_at DESC LIMIT 50"
    rows = (await db.execute(text(q), params)).mappings().all()
    return [
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
            "requested_reason": r["requested_reason"],
            "requested_reason_code": r["requested_reason_code"],
            "refund_preview_json": r["refund_preview_json"],
            "status": r["status"],
            "requested_at": (
                r["requested_at"].isoformat() if r["requested_at"] else None
            ),
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "review_note": r["review_note"],
        }
        for r in rows
    ]
