# ============================================================
# WAYTERO — PUBLIC LEADS API
# File: app/modules/admin/public_leads_api.py
# Doc Ref: Website Lead Capture §2 — Public API
# Prefix: /public  (registered in api/router.py)
#
# Public-facing website endpoints (no auth required):
#   POST /public/partner-applications  — "Become a Partner" form (/partner)
#   POST /public/contact-messages      — "Send us a message" form (/contact)
#   GET  /public/bookings/track        — booking status lookup (/track)
# ============================================================

import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import BusinessException, ResourceNotFoundException
from app.shared.responses.base import success_response

router = APIRouter()

_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


def _normalize_mobile(value: str) -> str:
    value = re.sub(r"[\s\-+]", "", value.strip())
    if value.startswith("91") and len(value) == 12:
        value = value[2:]
    if value.startswith("0") and len(value) == 11:
        value = value[1:]
    if not _MOBILE_RE.match(value):
        raise ValueError("Enter a valid 10-digit Indian mobile number")
    return value


# ── Schemas ──────────────────────────────────────────────────────


class PartnerApplicationRequest(BaseModel):
    business_name: str = Field(..., min_length=2, max_length=255)
    business_type: str = Field(..., min_length=2, max_length=50)
    contact_person: str = Field(..., min_length=2, max_length=255)
    mobile: str
    email: EmailStr
    city: Optional[str] = Field(None, max_length=150)
    details: Optional[str] = Field(None, max_length=2000)

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        return _normalize_mobile(v)


class ContactMessageRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    email: EmailStr
    mobile: Optional[str] = None
    subject: str = Field(..., min_length=3, max_length=300)
    message: str = Field(..., min_length=10, max_length=5000)

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return _normalize_mobile(v)


# ── Partner application ──────────────────────────────────────────


@router.post(
    "/partner-applications",
    status_code=201,
    tags=["Public Leads"],
    summary="Submit a partner application from the website",
)
async def submit_partner_application(
    body: PartnerApplicationRequest,
    db: AsyncSession = Depends(get_db),
):
    existing = (
        await db.execute(
            _text(
                """
                SELECT id FROM partner_applications
                WHERE mobile = :mobile AND status IN ('NEW', 'CONTACTED')
                LIMIT 1
                """
            ),
            {"mobile": body.mobile},
        )
    ).first()
    if existing:
        raise BusinessException(
            "An application with this mobile number is already under review. "
            "Our team will reach out within 48 hours."
        )

    row = (
        (
            await db.execute(
                _text(
                    """
                INSERT INTO partner_applications
                    (business_name, business_type, contact_person, mobile,
                     email, city, details)
                VALUES (:bn, :bt, :cp, :mobile, :email, :city, :details)
                RETURNING id, created_at
                """
                ),
                {
                    "bn": body.business_name.strip(),
                    "bt": body.business_type.strip(),
                    "cp": body.contact_person.strip(),
                    "mobile": body.mobile,
                    "email": str(body.email),
                    "city": body.city.strip() if body.city else None,
                    "details": body.details.strip() if body.details else None,
                },
            )
        )
        .mappings()
        .one()
    )

    return success_response(
        "Application submitted. Our partnerships team will contact you within 48 hours.",
        {"application_id": row["id"]},
    )


# ── Contact message ──────────────────────────────────────────────


@router.post(
    "/contact-messages",
    status_code=201,
    tags=["Public Leads"],
    summary="Submit a contact message from the website",
)
async def submit_contact_message(
    body: ContactMessageRequest,
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        _text(
            """
            INSERT INTO contact_messages (name, email, mobile, subject, message)
            VALUES (:name, :email, :mobile, :subject, :message)
            """
        ),
        {
            "name": body.name.strip(),
            "email": str(body.email),
            "mobile": body.mobile,
            "subject": body.subject.strip(),
            "message": body.message.strip(),
        },
    )
    return success_response("Message received. We'll get back to you within 24 hours.")


# ── Booking tracker ──────────────────────────────────────────────
# Public lookup keyed on booking number + the customer's registered
# mobile. The mobile match is mandatory — without it anyone could probe
# booking numbers. A mismatch returns 404 (same as "not found") so the
# endpoint never reveals whether a booking number exists.


def _mask_mobile(mobile: Optional[str]) -> Optional[str]:
    if not mobile or len(mobile) < 10:
        return None
    return mobile[:2] + "****" + mobile[-3:]


@router.get(
    "/bookings/track",
    tags=["Public Leads"],
    summary="Track a booking by number + registered mobile",
)
async def track_booking(
    booking_number: str = Query(..., min_length=4, max_length=100),
    mobile: str = Query(..., description="Registered 10-digit mobile"),
    db: AsyncSession = Depends(get_db),
):
    try:
        mobile = _normalize_mobile(mobile)
    except ValueError:
        raise BusinessException("Enter a valid 10-digit mobile number")

    bn = booking_number.strip().upper()

    # 1. Cab booking ─────────────────────────────────────────────
    cab = (
        (
            await db.execute(
                _text(
                    """
                SELECT
                    cb.booking_number, cb.booking_status, cb.trip_type,
                    cb.pickup_location, cb.drop_location, cb.pickup_datetime,
                    cb.final_amount, cb.estimated_amount,
                    mb.booking_number AS master_number, mb.journey_start_date,
                    u.mobile_number AS customer_mobile,
                    COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''), 'Guest') AS customer_name,
                    d.full_name  AS driver_name,
                    d.mobile     AS driver_mobile,
                    v.registration_number AS vehicle_number
                FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                JOIN customers c        ON c.id = mb.customer_id
                JOIN users u            ON u.id = c.user_id
                LEFT JOIN LATERAL (
                    SELECT cba.driver_id, cba.vehicle_id
                    FROM cab_booking_assignments cba
                    WHERE cba.cab_booking_id = cb.id AND cba.closed_at IS NULL
                    ORDER BY cba.assigned_at DESC
                    LIMIT 1
                ) act ON true
                LEFT JOIN drivers d     ON d.id = act.driver_id
                LEFT JOIN vehicles v    ON v.id = act.vehicle_id
                WHERE UPPER(cb.booking_number) = :bn
                """
                ),
                {"bn": bn},
            )
        )
        .mappings()
        .first()
    )

    if cab:
        if cab["customer_mobile"] != mobile:
            raise ResourceNotFoundException("Booking", booking_number)
        return success_response(
            "Booking found",
            {
                "service_type": "CAB",
                "booking_number": cab["booking_number"],
                "master_booking_number": cab["master_number"],
                "status": cab["booking_status"],
                "customer_name": cab["customer_name"],
                "trip": {
                    "trip_type": cab["trip_type"],
                    "pickup_location": cab["pickup_location"],
                    "drop_location": cab["drop_location"],
                    "pickup_datetime": (
                        cab["pickup_datetime"].isoformat()
                        if cab["pickup_datetime"]
                        else None
                    ),
                    "journey_start_date": (
                        cab["journey_start_date"].isoformat()
                        if cab["journey_start_date"]
                        else None
                    ),
                },
                "driver": (
                    {
                        "name": cab["driver_name"],
                        "mobile": _mask_mobile(cab["driver_mobile"]),
                        "vehicle_number": cab["vehicle_number"],
                    }
                    if cab["driver_name"] or cab["vehicle_number"]
                    else None
                ),
                "amount": float(cab["final_amount"] or cab["estimated_amount"] or 0),
            },
        )

    # 2. Hotel booking ───────────────────────────────────────────
    # NOTE: the legacy `hotel_bookings` table was dropped in migration
    # 0038; live hotel reservations live in `hotel_reservations`.
    htl = (
        (
            await db.execute(
                _text(
                    """
                SELECT
                    hr.reservation_number AS booking_number,
                    hr.reservation_status AS booking_status,
                    h.hotel_name, h.address AS hotel_address,
                    hr.check_in_date, hr.check_out_date,
                    hr.rooms_count AS num_rooms, hr.adults_count AS num_guests,
                    hr.total_amount AS final_amount,
                    mb.booking_number AS master_number,
                    u.mobile_number AS customer_mobile,
                    COALESCE(NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''), 'Guest') AS customer_name
                FROM hotel_reservations hr
                JOIN master_bookings mb ON mb.id = hr.master_booking_id
                JOIN hotels h          ON h.id = hr.hotel_id
                JOIN customers c       ON c.id = hr.customer_id
                JOIN users u           ON u.id = c.user_id
                WHERE UPPER(hr.reservation_number) = :bn
                """
                ),
                {"bn": bn},
            )
        )
        .mappings()
        .first()
    )

    if htl:
        if htl["customer_mobile"] != mobile:
            raise ResourceNotFoundException("Booking", booking_number)
        return success_response(
            "Booking found",
            {
                "service_type": "HOTEL",
                "booking_number": htl["booking_number"],
                "master_booking_number": htl["master_number"],
                "status": htl["booking_status"],
                "customer_name": htl["customer_name"],
                "stay": {
                    "hotel_name": htl["hotel_name"],
                    "hotel_address": htl["hotel_address"],
                    "check_in_date": (
                        htl["check_in_date"].isoformat()
                        if htl["check_in_date"]
                        else None
                    ),
                    "check_out_date": (
                        htl["check_out_date"].isoformat()
                        if htl["check_out_date"]
                        else None
                    ),
                    "num_rooms": htl["num_rooms"],
                    "num_guests": htl["num_guests"],
                },
                "amount": float(htl["final_amount"] or 0),
            },
        )

    raise ResourceNotFoundException("Booking", booking_number)
