# ============================================================
# WAYTERO — ADMIN ADVANCE PAYMENT API
# File: app/modules/admin/advance_api.py
# Prefix: /admin/advance
#
# Purpose:
#   Admin-side collection of advance payments on cab bookings — money the
#   customer pays before the trip runs. Booked from the booking detail page
#   after customer care creates the booking.
#
#   All rules (who may receive, in what form, how much, how often) live in
#   app/modules/booking/services so this and the partner-side routes cannot
#   drift apart. These routes only do auth, scoping and serialisation.
#
#   Admin is the only side that may void an advance, and only before final
#   payment is recorded — see the service for why.
#
# Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
#          BRD Part 6 §147 — Settlement net position
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.core.exceptions import ResourceNotFoundException
from app.modules.booking import services as advance_service
from app.shared.responses.base import success_response

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class CollectAdvanceIn(BaseModel):
    booking_number: str = Field(..., description="Cab booking number (WT-CAB-...)")
    amount: float = Field(..., gt=0, description="Advance amount in rupees")
    payment_mode: str = Field(..., description="CASH | ONLINE | UPI")
    received_by: str = Field(..., description="ADMIN | PARTNER | DRIVER")
    reference_note: Optional[str] = Field(
        None, description="UPI transaction reference, gateway reference, etc."
    )


class VoidAdvanceIn(BaseModel):
    booking_number: str
    reason: str = Field(
        ..., min_length=3, description="Why this advance is being cancelled"
    )


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _resolve_cab_booking_id(db: AsyncSession, booking_number: str) -> int:
    """
    Every money endpoint in this codebase is keyed on the human-readable cab
    booking number rather than the surrogate id; the service layer works on ids.
    """
    row = (
        (
            await db.execute(
                text("SELECT id FROM cab_bookings WHERE booking_number = :bn"),
                {"bn": booking_number},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise ResourceNotFoundException("Cab booking", booking_number)
    return int(row["id"])


# ════════════════════════════════════════════════════════════════
# GET ELIGIBILITY — GET /admin/advance/{booking_number}
# Drives the collect-advance form: whether it can open at all, and which
# receivers/modes are legal on this booking right now.
# ════════════════════════════════════════════════════════════════


@router.get("/{booking_number}")
async def get_advance(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cab_booking_id = await _resolve_cab_booking_id(db, booking_number)
    data = await advance_service.get_eligibility(db, cab_booking_id)
    return success_response("Advance details retrieved successfully", data)


# ════════════════════════════════════════════════════════════════
# COLLECT — POST /admin/advance/collect
# ════════════════════════════════════════════════════════════════


@router.post("/collect")
async def collect_advance(
    payload: CollectAdvanceIn,
    db: AsyncSession = Depends(get_db),
    # CCO is included because an advance is normally taken by the same customer
    # care agent who created the booking. Voiding one is admin-only.
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
):
    cab_booking_id = await _resolve_cab_booking_id(db, payload.booking_number)

    advance = await advance_service.create_advance(
        db,
        cab_booking_id=cab_booking_id,
        amount=payload.amount,
        payment_mode=payload.payment_mode,
        received_by=payload.received_by,
        user_id=current_user["sub"],
        source_role="ADMIN",
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


# ════════════════════════════════════════════════════════════════
# VOID — POST /admin/advance/void
# The escape hatch for a mis-keyed advance. Admin-only: the partner side has
# no equivalent route, because releasing the one-advance-per-booking lock is a
# platform decision.
# ════════════════════════════════════════════════════════════════


@router.post("/void")
async def void_advance(
    payload: VoidAdvanceIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    cab_booking_id = await _resolve_cab_booking_id(db, payload.booking_number)

    voided = await advance_service.void_advance(
        db,
        cab_booking_id=cab_booking_id,
        user_id=current_user["sub"],
        reason=payload.reason,
    )

    return success_response(
        f"Advance {voided['receipt_number']} voided. A corrected advance can now "
        "be recorded.",
        voided,
    )


# ════════════════════════════════════════════════════════════════
# RECEIPT PDF — GET /admin/advance/{booking_number}/receipt
# Streamed, not stored: the storage adapter is still a stub and the document is
# fully reproducible from the advance row.
# ════════════════════════════════════════════════════════════════


@router.get("/{booking_number}/receipt")
async def download_advance_receipt(
    booking_number: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cab_booking_id = await _resolve_cab_booking_id(db, booking_number)
    doc = await advance_service.build_receipt_pdf(db, cab_booking_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )
