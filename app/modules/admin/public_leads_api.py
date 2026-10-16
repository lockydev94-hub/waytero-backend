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
from app.modules.booking.services.booking_lookup import resolve_booking_by_ref
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
#
# The lookup itself lives in booking/services/booking_lookup.py, shared with
# the public feedback page. Its CAB and HOTEL payloads are unchanged here;
# the resolver also answers tour-booking and master-trip numbers, which the
# old inline SQL did not.


def _mask_mobile(mobile: Optional[str]) -> Optional[str]:
    if not mobile or len(mobile) < 10:
        return None
    return mobile[:2] + "****" + mobile[-3:]


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


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

    booking = await resolve_booking_by_ref(db, booking_number.strip().upper(), mobile)
    if booking is None:
        raise ResourceNotFoundException("Booking", booking_number)

    detail = booking["detail"]
    payload: dict = {
        "service_type": booking["service_type"],
        "booking_number": booking["booking_number"],
        "master_booking_number": booking["master_booking_number"],
        "status": booking["status"],
        "customer_name": booking["customer_name"],
        "amount": float(booking["amount"] or 0),
    }

    if booking["service_type"] == "CAB":
        payload["trip"] = {
            "trip_type": detail["trip_type"],
            "pickup_location": detail["pickup_location"],
            "drop_location": detail["drop_location"],
            "pickup_datetime": _iso(detail["pickup_datetime"]),
            "journey_start_date": _iso(detail["journey_start_date"]),
        }
        payload["driver"] = (
            {
                "name": detail["driver_name"],
                "mobile": _mask_mobile(detail["driver_mobile"]),
                "vehicle_number": detail["vehicle_number"],
            }
            if detail["driver_name"] or detail["vehicle_number"]
            else None
        )
    elif booking["service_type"] == "HOTEL":
        payload["stay"] = {
            "hotel_name": detail["hotel_name"],
            "hotel_address": detail["hotel_address"],
            "check_in_date": _iso(detail["check_in_date"]),
            "check_out_date": _iso(detail["check_out_date"]),
            "num_rooms": detail["num_rooms"],
            "num_guests": detail["num_guests"],
        }
    elif booking["service_type"] == "TOUR":
        payload["tour"] = {
            "package_name": detail["package_name"],
            "destination": detail["destination"],
            "travel_start_date": _iso(detail["travel_start_date"]),
            "travel_end_date": _iso(detail["travel_end_date"]),
            "persons_count": detail["persons_count"],
            "duration_days": detail["duration_days"],
            "duration_nights": detail["duration_nights"],
        }
    else:  # TRIP — the master booking number was used
        payload["trip"] = {
            "service_types": detail["service_types"],
            "journey_start_date": _iso(detail["journey_start_date"]),
            "journey_end_date": _iso(detail["journey_end_date"]),
        }

    return success_response("Booking found", payload)
