# ============================================================
# WAYTERO — ADMIN PAYMENT MANAGEMENT API
# File: app/modules/admin/payment_api.py
# Prefix: /admin/payments  (registered in api/router.py)
#
# Purpose:
#   Complete admin-facing payment management: list all platform payments
#   (gateway + advance), view detail, detect duplicates, initiate refunds
#   for advance payments based on booking status, and analytics summary.
#
# Payment Model in WayTero:
#   Two kinds of money flow exist today:
#     1. advance_payments — cash/UPI/ONLINE collected from customer before
#        trip end (tracked in advance_payments table, migration 0027)
#     2. master_bookings.total_paid_amount — full/final payment amount (may
#        link to gateway in a future migration; today it's a numeric field)
#
#   The finance tables (payments, refunds — DB Schema Part 7) are defined in
#   the database but NOT yet populated by booking flows. The advance_payments
#   table IS populated. This API therefore serves BOTH sources:
#     - advance_payments rows (fully operational)
#     - master_bookings payment snapshot (for total payment visibility)
#
#   Refund logic:
#     Advance refund is allowed only when:
#       a) booking status is CANCELLED or booking status is DRAFT/PENDING_PAYMENT
#          (trip never started) AND advance received_by = ADMIN (platform holds
#          the money — if partner/driver holds it, refund is handled offline)
#       b) advance status = ACTIVE (not already VOIDED)
#     Refund workflow: void the advance + record a refund_reason note on the
#     master booking + update master booking's total_refund_amount.
#
# Doc Ref:
#   DB Schema Part 7 §3 (payments), §7 (refunds), §22 (business rules)
#   Payment API §15 (initiate refund), §19 (refund status flow)
#   Admin API §14 (payment management)
#   BRD Part 6 §151–153 (refund management)
#   BRD Part 3 §47 (refund rules — admin configurable)
#   advance_api.py — advance collection & void
# ============================================================

import math
from datetime import datetime, timezone, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.shared.responses.base import success_response

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class InitiateRefundIn(BaseModel):
    advance_payment_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=5, description="Reason for refund")
    refund_amount: Optional[float] = Field(
        None,
        gt=0,
        description="Partial refund amount; omit or null for full advance refund",
    )


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


def _format_dt(dt) -> Optional[str]:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _advance_refund_eligible(
    booking_status: str,
    received_by: str,
    advance_status: str,
    refundable_balance: float = 0.0,
) -> tuple[bool, str]:
    """
    Returns (eligible: bool, reason: str).
    Refund is only possible when:
      - Advance is ACTIVE (not already VOIDED)
      - Some balance remains un-refunded (partial refunds leave the row ACTIVE)
      - Platform holds the money (received_by == ADMIN)
      - Booking is in a pre-trip state (CANCELLED, DRAFT, PENDING_PAYMENT)
        OR the booking was confirmed but trip never started (not COMPLETED/CLOSED).
    Doc Ref: BRD Part 6 §151, BRD Part 3 §47
    """
    if advance_status != "ACTIVE":
        return False, "Advance already voided — no refund possible"
    if refundable_balance <= 0:
        return False, "Advance is already fully refunded"
    if received_by != "ADMIN":
        return False, (
            f"Advance was received by {received_by}, not ADMIN. "
            "Refund must be handled offline directly with the partner/driver."
        )
    NON_REFUNDABLE = {"COMPLETED", "CLOSED"}
    if booking_status in NON_REFUNDABLE:
        return False, (
            f"Booking is {booking_status}. Advance refunds are not permitted "
            "after trip completion."
        )
    return True, "Eligible for refund"


# ════════════════════════════════════════════════════════════════
# PAYMENT STATS SUMMARY — GET /admin/payments/stats
# ════════════════════════════════════════════════════════════════


@router.get("/stats")
async def payment_stats(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Platform-wide payment analytics:
    - total collected (advance payments)
    - today's collection
    - pending refund count
    - voided advances
    - duplicate advance bookings (same booking_number, ACTIVE, > 1 row — data
      integrity check)
    - booking payment status breakdown
    """
    today = date.today()

    # Total advance collected (ACTIVE only — voided does not represent real money)
    total_collected_row = (
        (
            await db.execute(
                text(
                    """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN amount ELSE 0 END), 0) AS total_collected,
                COALESCE(SUM(CASE WHEN status = 'ACTIVE' AND DATE(collected_at) = :today THEN amount ELSE 0 END), 0) AS today_collected,
                COUNT(CASE WHEN status = 'ACTIVE' THEN 1 END) AS total_advances,
                COUNT(CASE WHEN status = 'VOIDED' THEN 1 END) AS voided_count,
                COUNT(CASE WHEN status = 'ACTIVE' AND received_by = 'ADMIN' THEN 1 END) AS admin_held_count
            FROM advance_payments
        """
                ),
                {"today": today},
            )
        )
        .mappings()
        .one()
    )

    # Booking payment status breakdown
    status_breakdown = (
        (
            await db.execute(
                text(
                    """
            SELECT payment_status, COUNT(*) AS cnt
            FROM master_bookings
            GROUP BY payment_status
            ORDER BY cnt DESC
        """
                )
            )
        )
        .mappings()
        .all()
    )

    # Duplicate detection: same booking_number with more than one ACTIVE advance
    # (should never happen due to partial unique index, but admin needs to know)
    dup_row = (
        (
            await db.execute(
                text(
                    """
            SELECT COUNT(*) AS dup_count
            FROM (
                SELECT booking_number
                FROM advance_payments
                WHERE status = 'ACTIVE'
                GROUP BY booking_number
                HAVING COUNT(*) > 1
            ) sub
        """
                )
            )
        )
        .mappings()
        .one()
    )

    # Pending potential refunds: ACTIVE advances where booking is CANCELLED
    pending_refund_row = (
        (
            await db.execute(
                text(
                    """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(ap.amount), 0) AS total_amount
            FROM advance_payments ap
            JOIN cab_bookings cb ON cb.id = ap.cab_booking_id
            JOIN master_bookings mb ON mb.id = ap.master_booking_id
            WHERE ap.status = 'ACTIVE'
              AND ap.received_by = 'ADMIN'
              AND mb.booking_status = 'CANCELLED'
        """
                )
            )
        )
        .mappings()
        .one()
    )

    return success_response(
        "Payment statistics retrieved",
        {
            "total_advance_collected": float(total_collected_row["total_collected"]),
            "today_advance_collected": float(total_collected_row["today_collected"]),
            "total_active_advances": int(total_collected_row["total_advances"]),
            "voided_advances": int(total_collected_row["voided_count"]),
            "admin_held_advances": int(total_collected_row["admin_held_count"]),
            "duplicate_advance_bookings": int(dup_row["dup_count"]),
            "pending_refund_count": int(pending_refund_row["cnt"]),
            "pending_refund_amount": float(pending_refund_row["total_amount"]),
            "booking_payment_status_breakdown": [
                {"status": r["payment_status"], "count": int(r["cnt"])}
                for r in status_breakdown
            ],
        },
    )


# ════════════════════════════════════════════════════════════════
# LIST PAYMENTS — GET /admin/payments
# ════════════════════════════════════════════════════════════════


@router.get("")
async def list_payments(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=5, le=100),
    search: Optional[str] = Query(
        None, description="Booking number / receipt / customer name/phone"
    ),
    status_filter: Optional[str] = Query(None, alias="status"),
    received_by: Optional[str] = Query(None, description="ADMIN | PARTNER | DRIVER"),
    payment_mode: Optional[str] = Query(None, description="CASH | ONLINE | UPI"),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    booking_status: Optional[str] = Query(
        None, description="Master booking status filter"
    ),
    payment_status: Optional[str] = Query(
        None, description="Master booking payment status filter"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    List all advance payments with full booking + customer context.
    Supports search, status, mode, date range, and booking status filters.
    """
    filters = ["1=1"]
    params: dict = {}

    if search:
        filters.append(
            """(
            ap.booking_number ILIKE :search
            OR ap.receipt_number ILIKE :search
            OR (u.first_name || ' ' || COALESCE(u.last_name, '')) ILIKE :search
            OR u.mobile_number ILIKE :search
            OR mb.booking_number ILIKE :search
        )"""
        )
        params["search"] = f"%{search}%"

    if status_filter:
        filters.append("ap.status = :adv_status")
        params["adv_status"] = status_filter

    if received_by:
        filters.append("ap.received_by = :received_by")
        params["received_by"] = received_by

    if payment_mode:
        filters.append("ap.payment_mode = :payment_mode")
        params["payment_mode"] = payment_mode

    if start_date:
        filters.append("DATE(ap.collected_at) >= :start_date")
        params["start_date"] = start_date

    if end_date:
        filters.append("DATE(ap.collected_at) <= :end_date")
        params["end_date"] = end_date

    if booking_status:
        filters.append("mb.booking_status = :booking_status")
        params["booking_status"] = booking_status

    if payment_status:
        filters.append("mb.payment_status = :payment_status")
        params["payment_status"] = payment_status

    where = " AND ".join(filters)

    count_row = (
        (
            await db.execute(
                text(
                    f"""
            SELECT COUNT(*) AS cnt
            FROM advance_payments ap
            JOIN cab_bookings cb ON cb.id = ap.cab_booking_id
            JOIN master_bookings mb ON mb.id = ap.master_booking_id
            JOIN customers c ON c.id = mb.customer_id
            LEFT JOIN users u ON u.id = c.user_id
            WHERE {where}
        """
                ),
                params,
            )
        )
        .mappings()
        .one()
    )
    total = int(count_row["cnt"])

    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset

    rows = (
        (
            await db.execute(
                text(
                    f"""
            SELECT
                ap.id,
                ap.receipt_number,
                ap.booking_number     AS cab_booking_number,
                mb.booking_number     AS master_booking_number,
                ap.amount,
                ap.payment_mode,
                ap.received_by,
                ap.reference_note,
                ap.status             AS advance_status,
                ap.refunded_amount,
                ap.collected_by_role,
                ap.collected_at,
                ap.voided_at,
                ap.void_reason,
                mb.id                 AS master_booking_id,
                mb.booking_status,
                mb.payment_status,
                mb.total_amount,
                mb.total_paid_amount,
                mb.total_refund_amount,
                mb.journey_start_date,
                c.id                  AS customer_id,
                (u.first_name || ' ' || COALESCE(u.last_name, '')) AS customer_name,
                u.mobile_number       AS customer_mobile,
                u.email               AS customer_email
            FROM advance_payments ap
            JOIN cab_bookings cb ON cb.id = ap.cab_booking_id
            JOIN master_bookings mb ON mb.id = ap.master_booking_id
            JOIN customers c ON c.id = mb.customer_id
            LEFT JOIN users u ON u.id = c.user_id
            WHERE {where}
            ORDER BY ap.collected_at DESC NULLS LAST, ap.id DESC
            LIMIT :limit OFFSET :offset
        """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    items = []
    for r in rows:
        refunded = float(r["refunded_amount"] or 0)
        refundable = float(r["amount"]) - refunded
        eligible, reason = _advance_refund_eligible(
            r["booking_status"], r["received_by"], r["advance_status"], refundable
        )
        items.append(
            {
                "id": r["id"],
                "receipt_number": r["receipt_number"],
                "cab_booking_number": r["cab_booking_number"],
                "master_booking_number": r["master_booking_number"],
                "master_booking_id": r["master_booking_id"],
                "amount": float(r["amount"]),
                "payment_mode": r["payment_mode"],
                "received_by": r["received_by"],
                "reference_note": r["reference_note"],
                "advance_status": r["advance_status"],
                "refunded_amount": refunded,
                "refundable_balance": max(refundable, 0.0),
                "collected_by_role": r["collected_by_role"],
                "collected_at": _format_dt(r["collected_at"]),
                "voided_at": _format_dt(r["voided_at"]),
                "void_reason": r["void_reason"],
                "booking_status": r["booking_status"],
                "payment_status": r["payment_status"],
                "total_amount": float(r["total_amount"] or 0),
                "total_paid_amount": float(r["total_paid_amount"] or 0),
                "total_refund_amount": float(r["total_refund_amount"] or 0),
                "journey_start_date": (
                    r["journey_start_date"].isoformat()
                    if r["journey_start_date"]
                    else None
                ),
                "customer_id": r["customer_id"],
                "customer_name": r["customer_name"],
                "customer_mobile": r["customer_mobile"],
                "customer_email": r["customer_email"],
                "refund_eligible": eligible,
                "refund_block_reason": None if eligible else reason,
            }
        )

    return success_response(
        "Payments retrieved",
        {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": math.ceil(total / page_size) if total else 1,
        },
    )


# ════════════════════════════════════════════════════════════════
# PAYMENT DETAIL — GET /admin/payments/{advance_id}
# ════════════════════════════════════════════════════════════════


@router.get("/{advance_id}")
async def get_payment_detail(
    advance_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Full detail of a single advance payment with full booking snapshot,
    all advance payments on the same booking (audit trail), and refund
    eligibility.
    """
    row = (
        (
            await db.execute(
                text(
                    """
            SELECT
                ap.id, ap.receipt_number, ap.booking_number AS cab_booking_number,
                ap.amount, ap.payment_mode, ap.received_by, ap.reference_note,
                ap.status AS advance_status, ap.refunded_amount, ap.collected_by_role,
                ap.collected_at, ap.voided_at, ap.void_reason,
                ap.master_booking_id,
                mb.booking_number AS master_booking_number,
                mb.booking_status, mb.payment_status,
                mb.total_amount, mb.total_paid_amount, mb.total_refund_amount,
                mb.journey_start_date, mb.journey_end_date, mb.remarks,
                mb.created_at AS booking_created_at,
                c.id AS customer_id,
                (u.first_name || ' ' || COALESCE(u.last_name, '')) AS customer_name,
                u.mobile_number AS customer_mobile, u.email AS customer_email,
                cb.id AS cab_booking_id, cb.booking_status AS cab_status,
                cb.trip_type, vc.category_name AS vehicle_category_name,
                cb.pickup_location, cb.drop_location,
                cb.pickup_datetime
            FROM advance_payments ap
            JOIN master_bookings mb ON mb.id = ap.master_booking_id
            JOIN cab_bookings cb ON cb.id = ap.cab_booking_id
            LEFT JOIN vehicle_categories vc ON vc.id = cb.vehicle_category_id
            JOIN customers c ON c.id = mb.customer_id
            LEFT JOIN users u ON u.id = c.user_id
            WHERE ap.id = :aid
        """
                ),
                {"aid": advance_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not row:
        raise HTTPException(
            status_code=404, detail=f"Advance payment {advance_id} not found"
        )

    # All advances on the same master booking (for full audit trail)
    all_advances = (
        (
            await db.execute(
                text(
                    """
            SELECT id, receipt_number, booking_number, amount, payment_mode,
                   received_by, status, refunded_amount, collected_by_role, collected_at,
                   voided_at, void_reason
            FROM advance_payments
            WHERE master_booking_id = :mbid
            ORDER BY collected_at DESC NULLS LAST
        """
                ),
                {"mbid": row["master_booking_id"]},
            )
        )
        .mappings()
        .all()
    )

    # Booking notes (for context)
    notes = (
        (
            await db.execute(
                text(
                    """
            SELECT n.note, n.created_at, (u.first_name || ' ' || COALESCE(u.last_name, '')) AS author
            FROM booking_notes n
            LEFT JOIN users u ON u.id = n.created_by
            WHERE n.master_booking_id = :mbid
            ORDER BY n.created_at DESC
            LIMIT 10
        """
                ),
                {"mbid": row["master_booking_id"]},
            )
        )
        .mappings()
        .all()
    )

    detail_refunded = float(row["refunded_amount"] or 0)
    detail_refundable = float(row["amount"]) - detail_refunded
    eligible, reason = _advance_refund_eligible(
        row["booking_status"],
        row["received_by"],
        row["advance_status"],
        detail_refundable,
    )

    return success_response(
        "Payment detail retrieved",
        {
            "id": row["id"],
            "receipt_number": row["receipt_number"],
            "cab_booking_number": row["cab_booking_number"],
            "master_booking_id": row["master_booking_id"],
            "master_booking_number": row["master_booking_number"],
            "amount": float(row["amount"]),
            "payment_mode": row["payment_mode"],
            "received_by": row["received_by"],
            "reference_note": row["reference_note"],
            "advance_status": row["advance_status"],
            "refunded_amount": detail_refunded,
            "refundable_balance": max(detail_refundable, 0.0),
            "collected_by_role": row["collected_by_role"],
            "collected_at": _format_dt(row["collected_at"]),
            "voided_at": _format_dt(row["voided_at"]),
            "void_reason": row["void_reason"],
            "booking": {
                "booking_status": row["booking_status"],
                "payment_status": row["payment_status"],
                "cab_status": row["cab_status"],
                "trip_type": row["trip_type"],
                "vehicle_category_name": row["vehicle_category_name"],
                "pickup_datetime": _format_dt(row["pickup_datetime"]),
                "pickup_location": row["pickup_location"],
                "drop_location": row["drop_location"],
                "total_amount": float(row["total_amount"] or 0),
                "total_paid_amount": float(row["total_paid_amount"] or 0),
                "total_refund_amount": float(row["total_refund_amount"] or 0),
                "journey_start_date": (
                    row["journey_start_date"].isoformat()
                    if row["journey_start_date"]
                    else None
                ),
                "journey_end_date": (
                    row["journey_end_date"].isoformat()
                    if row["journey_end_date"]
                    else None
                ),
                "remarks": row["remarks"],
                "created_at": _format_dt(row["booking_created_at"]),
            },
            "customer": {
                "id": row["customer_id"],
                "name": row["customer_name"],
                "mobile": row["customer_mobile"],
                "email": row["customer_email"],
            },
            "all_advances_on_booking": [
                {
                    "id": a["id"],
                    "receipt_number": a["receipt_number"],
                    "cab_booking_number": a["booking_number"],
                    "amount": float(a["amount"]),
                    "payment_mode": a["payment_mode"],
                    "received_by": a["received_by"],
                    "status": a["status"],
                    "refunded_amount": float(a["refunded_amount"] or 0),
                    "collected_by_role": a["collected_by_role"],
                    "collected_at": _format_dt(a["collected_at"]),
                    "voided_at": _format_dt(a["voided_at"]),
                    "void_reason": a["void_reason"],
                }
                for a in all_advances
            ],
            "recent_notes": [
                {
                    "text": n["note"],
                    "type": "NOTE",
                    "author": n["author"],
                    "created_at": _format_dt(n["created_at"]),
                }
                for n in notes
            ],
            "refund_eligible": eligible,
            "refund_block_reason": None if eligible else reason,
        },
    )


# ════════════════════════════════════════════════════════════════
# INITIATE REFUND — POST /admin/payments/refund
# Doc Ref: BRD Part 6 §151–153, Payment API §15, §19
# ════════════════════════════════════════════════════════════════


@router.post("/refund")
async def initiate_refund(
    payload: InitiateRefundIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    """
    Initiate a refund for an advance payment.

    Logic (Doc Ref: BRD Part 6 §151–153 + advance_api.py void logic):
      1. Load advance payment + master booking
      2. Validate refund eligibility (ADMIN held, not VOIDED, booking not completed)
      3. Validate refund_amount <= advance amount
      4. Void the advance (sets status = VOIDED, records void reason)
      5. Update master_booking.total_refund_amount
      6. Add booking note recording the refund action
    """
    # Step 1: Load advance — FOR UPDATE so concurrent refunds on the same
    # advance serialize: the second one blocks until the first commits, then
    # re-reads the updated refunded_amount/status and is rejected (S7).
    adv_row = (
        (
            await db.execute(
                text(
                    """
            SELECT ap.*, mb.booking_status, mb.total_refund_amount, mb.id AS mb_id
            FROM advance_payments ap
            JOIN master_bookings mb ON mb.id = ap.master_booking_id
            WHERE ap.id = :aid
            FOR UPDATE
        """
                ),
                {"aid": payload.advance_payment_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    if not adv_row:
        raise HTTPException(status_code=404, detail="Advance payment not found")

    # Step 2: Eligibility check
    advance_amount = float(adv_row["amount"])
    already_refunded = float(adv_row["refunded_amount"] or 0)
    refundable = advance_amount - already_refunded
    eligible, reason = _advance_refund_eligible(
        adv_row["booking_status"],
        adv_row["received_by"],
        adv_row["status"],
        refundable,
    )
    if not eligible:
        raise HTTPException(status_code=400, detail=reason)

    # Step 3: Amount validation — against the balance still un-refunded, since a
    # prior partial refund may have already drawn down this advance.
    refund_amount = payload.refund_amount if payload.refund_amount else refundable
    if refund_amount > refundable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refund amount ₹{refund_amount:,.2f} exceeds the refundable balance "
                f"₹{refundable:,.2f} (advance ₹{advance_amount:,.2f}, already refunded "
                f"₹{already_refunded:,.2f})."
            ),
        )

    now = datetime.now(timezone.utc)
    admin_user_id = current_user["sub"]
    new_refunded_on_advance = already_refunded + refund_amount
    is_partial = new_refunded_on_advance < advance_amount

    # Step 4: Void the advance only once it is fully refunded. A partial refund
    # leaves it ACTIVE so the remaining balance stays collectible.
    await db.execute(
        text(
            """
            UPDATE advance_payments
            SET refunded_amount = :refunded
            WHERE id = :aid
        """
        ),
        {"refunded": new_refunded_on_advance, "aid": payload.advance_payment_id},
    )

    if is_partial:
        note_suffix = (
            f"Partial refund — advance remains ACTIVE with "
            f"₹{advance_amount - new_refunded_on_advance:,.2f} retained."
        )
    else:
        note_suffix = "Advance fully refunded and voided."
        await db.execute(
            text(
                """
                UPDATE advance_payments
                SET status = 'VOIDED',
                    voided_by_user_id = :uid,
                    void_reason = :reason,
                    voided_at = :now
                WHERE id = :aid
            """
            ),
            {
                "uid": admin_user_id,
                "reason": f"REFUND: {payload.reason}",
                "now": now,
                "aid": payload.advance_payment_id,
            },
        )

    # Step 5: Update total_refund_amount on master booking
    existing_refund = float(adv_row["total_refund_amount"] or 0)
    new_refund_total = existing_refund + refund_amount
    await db.execute(
        text(
            """
            UPDATE master_bookings
            SET total_refund_amount = :new_total,
                updated_at = :now
            WHERE id = :mbid
        """
        ),
        {"new_total": new_refund_total, "now": now, "mbid": adv_row["mb_id"]},
    )

    # Step 6: Booking note
    note_text = (
        f"REFUND INITIATED by admin ({admin_user_id[:8]}…): "
        f"₹{refund_amount:,.2f} of advance receipt {adv_row['receipt_number']} "
        f"({adv_row['payment_mode']}, received by {adv_row['received_by']}). "
        f"Reason: {payload.reason}. {note_suffix}"
    )
    await db.execute(
        text(
            """
            INSERT INTO booking_notes (master_booking_id, note, created_by, created_at)
            VALUES (:mbid, :txt, :uid, :now)
        """
        ),
        {
            "mbid": adv_row["mb_id"],
            "txt": note_text,
            "uid": admin_user_id,
            "now": now,
        },
    )

    return success_response(
        f"Refund of ₹{refund_amount:,.2f} initiated for advance {adv_row['receipt_number']}. "
        + note_suffix,
        {
            "receipt_number": adv_row["receipt_number"],
            "refund_amount": refund_amount,
            "advance_amount": advance_amount,
            "advance_refunded_amount": new_refunded_on_advance,
            "advance_refundable_balance": advance_amount - new_refunded_on_advance,
            "is_partial": is_partial,
            "new_total_refund_amount": new_refund_total,
        },
    )


# ════════════════════════════════════════════════════════════════
# DUPLICATE DETECTION — GET /admin/payments/duplicates
# Doc Ref: Payment API §28 PAYMENT_004, Security §29
# ════════════════════════════════════════════════════════════════


@router.get("/duplicates/list")
async def list_duplicate_advances(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Find advance payments that might indicate duplicate collection:
      1. Same booking_number with > 1 ACTIVE advance row (should be 0 — index
         prevents it, but worth surfacing if the index was dropped/bypassed)
      2. Same customer + same amount + same payment_mode within 5 minutes
         (fraud / double-click scenario)
    """
    # Case 1: index violation
    dup1 = (
        (
            await db.execute(
                text(
                    """
            SELECT ap.booking_number, COUNT(*) AS dup_count,
                   SUM(ap.amount) AS total_amount,
                   array_agg(ap.receipt_number ORDER BY ap.collected_at) AS receipts,
                   MIN(ap.collected_at) AS first_at
            FROM advance_payments ap
            WHERE ap.status = 'ACTIVE'
            GROUP BY ap.booking_number
            HAVING COUNT(*) > 1
            ORDER BY dup_count DESC
            LIMIT 50
        """
                )
            )
        )
        .mappings()
        .all()
    )

    # Case 2: same customer, same amount, same mode within 5 min
    dup2 = (
        (
            await db.execute(
                text(
                    """
            SELECT
                a1.id AS id1, a1.receipt_number AS receipt1,
                a2.id AS id2, a2.receipt_number AS receipt2,
                a1.booking_number, a1.amount, a1.payment_mode,
                (u.first_name || ' ' || COALESCE(u.last_name, '')) AS customer_name, u.mobile_number AS customer_mobile,
                a1.collected_at AS t1, a2.collected_at AS t2,
                ABS(EXTRACT(EPOCH FROM (a2.collected_at - a1.collected_at))) AS diff_seconds
            FROM advance_payments a1
            JOIN advance_payments a2
              ON a2.id > a1.id
              AND a2.master_booking_id != a1.master_booking_id
              AND ABS(EXTRACT(EPOCH FROM (a2.collected_at - a1.collected_at))) < 300
              AND a2.amount = a1.amount
              AND a2.payment_mode = a1.payment_mode
            JOIN master_bookings mb ON mb.id = a1.master_booking_id
            JOIN customers c ON c.id = mb.customer_id
            LEFT JOIN users u ON u.id = c.user_id
            JOIN master_bookings mb2 ON mb2.id = a2.master_booking_id
            WHERE a1.status = 'ACTIVE' AND a2.status = 'ACTIVE'
              AND mb2.customer_id = mb.customer_id
            ORDER BY a1.collected_at DESC
            LIMIT 30
        """
                )
            )
        )
        .mappings()
        .all()
    )

    return success_response(
        "Duplicate analysis complete",
        {
            "index_violations": [
                {
                    "cab_booking_number": r["booking_number"],
                    "duplicate_count": int(r["dup_count"]),
                    "total_amount": float(r["total_amount"]),
                    "receipts": list(r["receipts"]),
                    "first_collected_at": _format_dt(r["first_at"]),
                }
                for r in dup1
            ],
            "rapid_repeat_charges": [
                {
                    "id1": r["id1"],
                    "receipt1": r["receipt1"],
                    "id2": r["id2"],
                    "receipt2": r["receipt2"],
                    "cab_booking_number": r["booking_number"],
                    "amount": float(r["amount"]),
                    "payment_mode": r["payment_mode"],
                    "customer_name": r["customer_name"],
                    "customer_mobile": r["customer_mobile"],
                    "t1": _format_dt(r["t1"]),
                    "t2": _format_dt(r["t2"]),
                    "seconds_apart": int(r["diff_seconds"]),
                }
                for r in dup2
            ],
            "has_issues": len(dup1) > 0 or len(dup2) > 0,
        },
    )
