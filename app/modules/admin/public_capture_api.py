# ============================================================
# WAYTERO — PUBLIC CAPTURE API
# File: app/modules/admin/public_capture_api.py
# Doc Ref: Website Lead Capture §2 — Public API; migrations 0082, 0083
# Prefix: /public  (registered in api/router.py)
#
# No-auth endpoints for the three capture surfaces on the website:
#   POST /public/enquiries                    — "Enquiry Now" modal
#   POST /public/callback-requests            — "Request a Callback" card
#   GET  /public/bookings/feedback-lookup     — masked step 1 of the feedback page
#   POST /public/feedback                     — step 2 of the feedback page
#
# Every one of these is open to the internet, so every one carries a
# per-IP RateLimit (advisory — see app/core/rate_limit.py) and re-checks its
# own rules server-side. The client's validation is a courtesy.
#
# Booking identity is never taken on trust: the feedback POST re-resolves
# booking_number + mobile through the same resolver the lookup used, so there
# is no step-1 token to steal or replay.
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import (
    BusinessException,
    ResourceNotFoundException,
    ValidationException,
)
from app.core.rate_limit import RateLimit
from app.modules.admin.public_leads_api import _mask_mobile, _normalize_mobile
from app.modules.booking.services.booking_lookup import resolve_booking_by_ref
from app.shared.responses.base import success_response

router = APIRouter()

SERVICE_TYPES = ("CAB", "HOTEL", "TOUR", "OTHER")


def _service_type(value: str) -> str:
    upper = value.strip().upper()
    if upper not in SERVICE_TYPES:
        raise ValidationException(
            f"service_type must be one of {', '.join(SERVICE_TYPES)}",
            details={"field": "service_type"},
        )
    return upper


# ── Schemas ──────────────────────────────────────────────────────


class EnquiryRequest(BaseModel):
    first_name: str = Field(..., min_length=2, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    email: EmailStr
    mobile: str
    service_type: str = Field(..., description="CAB | HOTEL | TOUR | OTHER")
    message: str = Field(..., min_length=5, max_length=4000)

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        return _normalize_mobile(v)

    @field_validator("service_type")
    @classmethod
    def validate_service_type(cls, v: str) -> str:
        return _service_type(v)


class CallbackRequestCreate(BaseModel):
    mobile: str
    name: Optional[str] = Field(None, max_length=150)

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        return _normalize_mobile(v)


class FeedbackRequest(BaseModel):
    booking_number: str = Field(..., min_length=4, max_length=100)
    mobile: str
    rating: int = Field(..., ge=1, le=5)
    review: Optional[str] = Field(None, max_length=2000)
    guest_name: str = Field(..., min_length=2, max_length=150)
    guest_email: Optional[EmailStr] = None

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        return _normalize_mobile(v)


# ── Enquiry ──────────────────────────────────────────────────────


@router.post(
    "/enquiries",
    status_code=201,
    tags=["Public Capture"],
    summary="Submit a website enquiry",
    dependencies=[Depends(RateLimit("enquiry", 5, 600))],
)
async def submit_enquiry(
    body: EnquiryRequest,
    db: AsyncSession = Depends(get_db),
):
    row = (
        (
            await db.execute(
                _text(
                    """
                    INSERT INTO enquiries
                        (first_name, last_name, email, mobile, service_type, message)
                    VALUES (:first, :last, :email, :mobile, :stype, :message)
                    RETURNING id
                    """
                ),
                {
                    "first": body.first_name.strip(),
                    "last": body.last_name.strip() if body.last_name else None,
                    "email": str(body.email),
                    "mobile": body.mobile,
                    "stype": body.service_type,
                    "message": body.message.strip(),
                },
            )
        )
        .mappings()
        .one()
    )
    return success_response(
        "Thank you. Our team will contact you shortly.",
        {"enquiry_id": row["id"]},
    )


# ── Callback request ─────────────────────────────────────────────


@router.post(
    "/callback-requests",
    status_code=201,
    tags=["Public Capture"],
    summary="Request a call back",
    dependencies=[Depends(RateLimit("callback", 3, 600))],
)
async def submit_callback_request(
    body: CallbackRequestCreate,
    db: AsyncSession = Depends(get_db),
):
    open_row = (
        await db.execute(
            _text(
                """
                SELECT id FROM callback_requests
                WHERE mobile = :mobile AND status IN ('NEW', 'CALLED', 'NOT_PICKED')
                LIMIT 1
                """
            ),
            {"mobile": body.mobile},
        )
    ).first()
    if open_row:
        raise BusinessException(
            "You have already asked us to call this number and it is still "
            "awaiting action. Please give our team a little time."
        )

    row = (
        (
            await db.execute(
                _text(
                    """
                    INSERT INTO callback_requests (mobile, name)
                    VALUES (:mobile, :name)
                    RETURNING id
                    """
                ),
                {
                    "mobile": body.mobile,
                    "name": body.name.strip() if body.name else None,
                },
            )
        )
        .mappings()
        .one()
    )
    return success_response(
        "Got it. We'll call you on this number shortly.",
        {"callback_id": row["id"]},
    )


# ── Guest feedback ───────────────────────────────────────────────


async def _resolve_for_feedback(
    db: AsyncSession, booking_number: str, mobile: str
) -> dict:
    """Resolve and verify, or raise the one generic not-found message."""
    booking = await resolve_booking_by_ref(db, booking_number.strip().upper(), mobile)
    if booking is None:
        # Same body for "no such booking" and "wrong mobile" — see the
        # resolver's note. This endpoint must not confirm booking numbers.
        raise ResourceNotFoundException("Booking", booking_number)
    return booking


def _summary_of(booking: dict) -> str:
    detail = booking["detail"]
    if booking["service_type"] == "CAB":
        return f"{detail['pickup_location'] or ''} → {detail['drop_location'] or ''}".strip(
            " →"
        )
    if booking["service_type"] == "HOTEL":
        return detail["hotel_name"] or ""
    if booking["service_type"] == "TOUR":
        return detail["package_name"] or ""
    types = (detail["service_types"] or "").replace(",", ", ")
    return f"Complete trip ({types})" if types else "Complete trip"


@router.get(
    "/bookings/feedback-lookup",
    tags=["Public Capture"],
    summary="Verify a booking reference before collecting feedback",
    dependencies=[Depends(RateLimit("feedback_lookup", 10, 300))],
)
async def feedback_lookup(
    booking_number: str = Query(..., min_length=4, max_length=100),
    mobile: str = Query(..., description="Registered 10-digit mobile"),
    db: AsyncSession = Depends(get_db),
):
    try:
        mobile = _normalize_mobile(mobile)
    except ValueError:
        raise BusinessException("Enter a valid 10-digit mobile number")

    booking = await _resolve_for_feedback(db, booking_number, mobile)
    detail = booking["detail"]

    # The date the traveller recognises: check-in, tour start, or the trip
    # window — whichever service this booking actually is.
    journey_date = next(
        (
            detail[key]
            for key in (
                "check_in_date",
                "travel_start_date",
                "journey_start_date",
                "pickup_datetime",
            )
            if detail.get(key)
        ),
        None,
    )

    # Step 1 shows just enough for the traveller to recognise their own trip.
    # No address, no driver contact, no full mobile.
    return success_response(
        "Booking found",
        {
            "service_type": booking["service_type"],
            "booking_number": booking["booking_number"],
            "summary": _summary_of(booking),
            "customer_name": booking["customer_name"],
            "customer_mobile": _mask_mobile(booking["customer_mobile"]),
            "journey_date": journey_date.isoformat() if journey_date else None,
            "amount": float(booking["amount"] or 0),
        },
    )


@router.post(
    "/feedback",
    status_code=201,
    tags=["Public Capture"],
    summary="Submit feedback for a booking",
    dependencies=[Depends(RateLimit("feedback", 5, 3600))],
)
async def submit_feedback(
    body: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
):
    booking = await _resolve_for_feedback(db, body.booking_number, body.mobile)

    params = {
        "mb": booking["master_booking_id"],
        "cust": booking["customer_id"],
        "rating": body.rating,
        "review": body.review.strip() if body.review else None,
        "guest_name": body.guest_name.strip(),
        "guest_mobile": body.mobile,
        "guest_email": str(body.guest_email) if body.guest_email else None,
        "ref": body.booking_number.strip().upper(),
    }
    insert = _text(
        """
        INSERT INTO booking_reviews
            (master_booking_id, customer_id, rating, review, source, status,
             guest_name, guest_mobile, guest_email, booking_ref_submitted)
        VALUES
            (:mb, :cust, :rating, :review, 'GUEST_WEB', 'PENDING',
             :guest_name, :guest_mobile, :guest_email, :ref)
        RETURNING id
        """
    )
    try:
        # uniq_booking_reviews_live_per_booking (migration 0083) is the
        # duplicate rule. The savepoint keeps the outer transaction usable
        # after the constraint fires.
        async with db.begin_nested():
            row = (await db.execute(insert, params)).mappings().one()
    except IntegrityError:
        raise BusinessException(
            "You've already shared feedback for this booking. Thank you — our "
            "team is reviewing it."
        )

    return success_response(
        "Thank you for your feedback! Our team will review it shortly.",
        {"feedback_id": row["id"]},
    )
