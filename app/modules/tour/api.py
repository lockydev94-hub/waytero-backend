"""Tour package catalog, approval and fixed-package booking APIs.

Routes are mounted in ``app.api.router`` under the admin, partner, public and
customer-care prefixes.  The same package payload is intentionally used by all
surfaces so an approval edit cannot silently diverge from the customer page.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.admin.models import CommissionRule
from app.modules.auth.models.user import User
from app.modules.booking.models import BookingService, MasterBooking
from app.modules.booking.services.numbering import next_master_booking_number
from app.modules.customer.models import Customer
from app.modules.master.models import City
from app.modules.partner.models import Partner, PartnerService
from app.modules.tour.models import (
    TourBooking,
    TourItinerary,
    TourPackage,
    TourPackageExclusion,
    TourPackageInclusion,
    TourPackageMedia,
    TourPackagePricing,
    TourParticipant,
)


class ItineraryIn(BaseModel):
    day_number: int = Field(ge=1)
    title: str = Field(min_length=2, max_length=255)
    description: Optional[str] = None
    activities: list[str] = Field(default_factory=list)
    overnight_city_id: Optional[int] = None


class PricingIn(BaseModel):
    persons_count: int = Field(ge=1)
    package_price: Decimal = Field(gt=0)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class PackageIn(BaseModel):
    partner_id: Optional[int] = None
    package_name: str = Field(min_length=3, max_length=255)
    package_type: str = "FIXED"
    destination: str = Field(min_length=2, max_length=255)
    city_id: int
    duration_days: int = Field(ge=1, le=90)
    duration_nights: int = Field(ge=0, le=89)
    minimum_persons: int = Field(default=1, ge=1)
    maximum_persons: Optional[int] = Field(default=None, ge=1)
    short_description: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    terms_and_conditions: Optional[str] = None
    itinerary: list[ItineraryIn] = Field(default_factory=list)
    inclusions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    pricing: list[PricingIn] = Field(default_factory=list)
    media: list[dict[str, Any]] = Field(default_factory=list)


class PackageUpdate(BaseModel):
    package_name: Optional[str] = Field(default=None, min_length=3, max_length=255)
    package_type: Optional[str] = None
    destination: Optional[str] = None
    city_id: Optional[int] = None
    duration_days: Optional[int] = Field(default=None, ge=1, le=90)
    duration_nights: Optional[int] = Field(default=None, ge=0, le=89)
    minimum_persons: Optional[int] = Field(default=None, ge=1)
    maximum_persons: Optional[int] = Field(default=None, ge=1)
    short_description: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    terms_and_conditions: Optional[str] = None
    itinerary: Optional[list[ItineraryIn]] = None
    inclusions: Optional[list[str]] = None
    exclusions: Optional[list[str]] = None
    pricing: Optional[list[PricingIn]] = None
    media: Optional[list[dict[str, Any]]] = None


class StatusIn(BaseModel):
    status: str
    rejection_reason: Optional[str] = None


class ParticipantIn(BaseModel):
    participant_name: str = Field(min_length=2, max_length=255)
    mobile: Optional[str] = None
    age: Optional[int] = Field(default=None, ge=0, le=120)
    gender: Optional[str] = None
    id_type: Optional[str] = None
    id_number: Optional[str] = None


class BookingIn(BaseModel):
    package_id: int
    travel_start_date: date
    persons_count: int = Field(ge=1)
    special_requests: Optional[str] = None
    participants: list[ParticipantIn] = Field(default_factory=list)


class TourAdvanceIn(BaseModel):
    amount: float = Field(gt=0)
    payment_mode: str  # CASH | ONLINE | UPI | WALLET
    received_by: str = "ADMIN"  # ADMIN | PARTNER | DRIVER
    reference_number: Optional[str] = None
    notes: Optional[str] = None


class TourVehicleAssignIn(BaseModel):
    vehicle_id: Optional[int] = None
    driver_id: Optional[int] = None


class TourTripUpdateIn(BaseModel):
    travel_start_date: Optional[str] = None
    travel_end_date: Optional[str] = None
    persons_count: Optional[int] = Field(default=None, ge=1)
    pickup_location: Optional[str] = None
    pickup_datetime: Optional[str] = None
    hotel_details: Optional[str] = None
    other_details: Optional[str] = None


class TourChargeIn(BaseModel):
    label: str = Field(min_length=2, max_length=255)
    amount: float = Field(gt=0)
    reason: Optional[str] = None


ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN", "CCO", "VERIFICATION_OFFICER")
PARTNER_ROLES = ("PARTNER", "ADMIN", "SUPER_ADMIN")


def _actor_uuid(user: dict) -> Optional[UUID]:
    raw = user.get("sub")
    return UUID(str(raw)) if raw else None


async def _partner_id(db: AsyncSession, user: dict) -> Optional[int]:
    actor = _actor_uuid(user)
    if not actor:
        return None
    return await db.scalar(select(Partner.id).where(Partner.user_id == actor))


async def _customer_id(db: AsyncSession, user: dict) -> Optional[int]:
    actor = _actor_uuid(user)
    if not actor:
        return None
    return await db.scalar(select(Customer.id).where(Customer.user_id == actor))


def _slug(name: str, code: str) -> str:
    import re

    clean = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{clean}-{code.lower()}"


async def _new_code(db: AsyncSession) -> str:
    count = await db.scalar(select(func.count(TourPackage.id))) or 0
    return f"TP-{datetime.now(timezone.utc):%Y%m%d}-{int(count) + 1:04d}"


async def _replace_children(
    db: AsyncSession, package_id: int, payload: PackageUpdate | PackageIn
) -> None:
    for model in (
        TourItinerary,
        TourPackageInclusion,
        TourPackageExclusion,
        TourPackagePricing,
        TourPackageMedia,
    ):
        rows = (
            await db.scalars(select(model).where(model.package_id == package_id))
        ).all()
        for row in rows:
            await db.delete(row)
    await db.flush()
    for row in payload.itinerary or []:
        db.add(
            TourItinerary(
                package_id=package_id,
                day_number=row.day_number,
                title=row.title,
                description=row.description,
                activities=row.activities,
                overnight_city_id=row.overnight_city_id,
            )
        )
    for value in payload.inclusions or []:
        if value.strip():
            db.add(
                TourPackageInclusion(
                    package_id=package_id, inclusion_text=value.strip()
                )
            )
    for value in payload.exclusions or []:
        if value.strip():
            db.add(
                TourPackageExclusion(
                    package_id=package_id, exclusion_text=value.strip()
                )
            )
    for row in payload.pricing or []:
        db.add(
            TourPackagePricing(
                package_id=package_id,
                persons_count=row.persons_count,
                package_price=row.package_price,
                effective_from=row.effective_from,
                effective_to=row.effective_to,
            )
        )
    for i, row in enumerate(payload.media or []):
        url = str(row.get("media_url") or row.get("url") or "").strip()
        if url:
            db.add(
                TourPackageMedia(
                    package_id=package_id,
                    media_type=str(row.get("media_type", "IMAGE")),
                    media_url=url,
                    caption=row.get("caption"),
                    display_order=i,
                    is_primary=bool(row.get("is_primary", i == 0)),
                )
            )


async def _package_payload(
    db: AsyncSession, package: TourPackage, include_private: bool = True
) -> dict[str, Any]:
    city_name = await db.scalar(select(City.name).where(City.id == package.city_id))
    partner = (
        await db.execute(
            select(Partner.business_name, Partner.partner_code).where(
                Partner.id == package.partner_id
            )
        )
    ).one_or_none()
    itineraries = (
        await db.scalars(
            select(TourItinerary)
            .where(TourItinerary.package_id == package.id)
            .order_by(TourItinerary.day_number)
        )
    ).all()
    inclusions = (
        await db.scalars(
            select(TourPackageInclusion)
            .where(TourPackageInclusion.package_id == package.id)
            .order_by(TourPackageInclusion.id)
        )
    ).all()
    exclusions = (
        await db.scalars(
            select(TourPackageExclusion)
            .where(TourPackageExclusion.package_id == package.id)
            .order_by(TourPackageExclusion.id)
        )
    ).all()
    pricing = (
        await db.scalars(
            select(TourPackagePricing)
            .where(TourPackagePricing.package_id == package.id)
            .order_by(TourPackagePricing.persons_count)
        )
    ).all()
    media = (
        await db.scalars(
            select(TourPackageMedia)
            .where(TourPackageMedia.package_id == package.id)
            .order_by(TourPackageMedia.display_order)
        )
    ).all()
    data = {
        "id": package.id,
        "uuid": str(package.uuid),
        "package_code": package.package_code,
        "slug": package.slug,
        "package_name": package.package_name,
        "package_type": package.package_type,
        "destination": package.destination,
        "city_id": package.city_id,
        "city_name": city_name,
        "duration_days": package.duration_days,
        "duration_nights": package.duration_nights,
        "minimum_persons": package.minimum_persons,
        "maximum_persons": package.maximum_persons,
        "short_description": package.short_description,
        "description": package.description,
        "terms_and_conditions": package.terms_and_conditions,
        "status": package.status,
        "partner_id": package.partner_id,
        "partner_name": (partner[0] if partner else None),
        "partner_code": (partner[1] if partner else None),
        "rejection_reason": package.rejection_reason,
        "submitted_at": package.submitted_at,
        "approved_at": package.approved_at,
        "activated_at": package.activated_at,
        "created_at": package.created_at,
        "updated_at": package.updated_at,
        "itinerary": [
            {
                "id": x.id,
                "day_number": x.day_number,
                "title": x.title,
                "description": x.description,
                "activities": x.activities or [],
                "overnight_city_id": x.overnight_city_id,
            }
            for x in itineraries
        ],
        "inclusions": [{"id": x.id, "text": x.inclusion_text} for x in inclusions],
        "exclusions": [{"id": x.id, "text": x.exclusion_text} for x in exclusions],
        "pricing": [
            {
                "id": x.id,
                "persons_count": x.persons_count,
                "package_price": float(x.package_price),
                "effective_from": x.effective_from,
                "effective_to": x.effective_to,
            }
            for x in pricing
        ],
        "media": [
            {
                "id": x.id,
                "media_type": x.media_type,
                "media_url": x.media_url,
                "caption": x.caption,
                "is_primary": x.is_primary,
            }
            for x in media
        ],
    }
    if not include_private:
        data.pop("rejection_reason", None)
        data.pop("partner_id", None)
        data.pop("partner_code", None)
    return data


async def _find_package(
    db: AsyncSession, package_id: int, *, own_partner_id: Optional[int] = None
) -> TourPackage:
    stmt = select(TourPackage).where(TourPackage.id == package_id)
    if own_partner_id is not None:
        stmt = stmt.where(TourPackage.partner_id == own_partner_id)
    package = await db.scalar(stmt)
    if not package:
        raise HTTPException(404, "Tour package not found")
    return package


async def _save_package(
    db: AsyncSession,
    payload: PackageIn,
    user: dict,
    *,
    partner_id: int,
    status_value: str,
) -> TourPackage:
    code = await _new_code(db)
    package = TourPackage(
        partner_id=partner_id,
        package_code=code,
        slug=_slug(payload.package_name, code),
        package_name=payload.package_name,
        package_type=payload.package_type.upper(),
        destination=payload.destination,
        city_id=payload.city_id,
        duration_days=payload.duration_days,
        duration_nights=payload.duration_nights,
        minimum_persons=payload.minimum_persons,
        maximum_persons=payload.maximum_persons,
        short_description=payload.short_description,
        description=payload.description,
        terms_and_conditions=payload.terms_and_conditions,
        status=status_value,
        created_by=_actor_uuid(user),
        submitted_at=(
            datetime.now(timezone.utc)
            if status_value in {"PENDING_APPROVAL", "ACTIVE"}
            else None
        ),
        approved_at=(datetime.now(timezone.utc) if status_value == "ACTIVE" else None),
        activated_at=(datetime.now(timezone.utc) if status_value == "ACTIVE" else None),
    )
    db.add(package)
    await db.flush()
    await _replace_children(db, package.id, payload)
    return package


async def _commission(db: AsyncSession, package: TourPackage) -> Decimal:
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
        .order_by(
            CommissionRule.city_id.desc().nullslast(), desc(CommissionRule.created_at)
        )
        .limit(1)
    )
    return Decimal(
        str(rule.commission_value if rule and rule.commission_value is not None else 10)
    ).quantize(Decimal("0.01"))


async def _price_for(
    db: AsyncSession, package: TourPackage, persons: int
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    rows = (
        await db.scalars(
            select(TourPackagePricing)
            .where(TourPackagePricing.package_id == package.id)
            .order_by(TourPackagePricing.persons_count)
        )
    ).all()
    if not rows:
        raise HTTPException(422, "This package has no active pricing slabs")
    if persons < package.minimum_persons or (
        package.maximum_persons and persons > package.maximum_persons
    ):
        raise HTTPException(
            422,
            f"This package accepts {package.minimum_persons}–{package.maximum_persons or 'unlimited'} travellers",
        )
    row = next((r for r in rows if r.persons_count >= persons), rows[-1])
    total = Decimal(row.package_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    percent = await _commission(db, package)
    commission = (total * percent / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return total, commission, total - commission, percent


async def _list_packages(
    db: AsyncSession,
    *,
    statuses: Optional[list[str]] = None,
    partner_id: Optional[int] = None,
    search: Optional[str] = None,
    city_id: Optional[int] = None,
    include_private: bool = True,
) -> list[dict[str, Any]]:
    stmt = select(TourPackage).order_by(desc(TourPackage.created_at))
    if statuses:
        stmt = stmt.where(TourPackage.status.in_(statuses))
    if partner_id is not None:
        stmt = stmt.where(TourPackage.partner_id == partner_id)
    if city_id is not None:
        stmt = stmt.where(TourPackage.city_id == city_id)
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                TourPackage.package_name.ilike(term),
                TourPackage.destination.ilike(term),
                TourPackage.package_code.ilike(term),
            )
        )
    rows = (await db.scalars(stmt)).all()
    return [
        await _package_payload(db, row, include_private=include_private) for row in rows
    ]


admin_router = APIRouter()
partner_router = APIRouter()
public_router = APIRouter()
care_router = APIRouter()


@admin_router.get("/packages")
async def admin_packages(
    status_filter: Optional[str] = Query(None, alias="status"),
    partner_id: Optional[int] = None,
    search: Optional[str] = None,
    city_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    return await _list_packages(
        db,
        statuses=[status_filter] if status_filter else None,
        partner_id=partner_id,
        search=search,
        city_id=city_id,
    )


@admin_router.post("/packages", status_code=status.HTTP_201_CREATED)
async def admin_create_package(
    payload: PackageIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    if not payload.partner_id:
        raise HTTPException(422, "Select a partner before creating a tour package")
    partner = await db.scalar(select(Partner).where(Partner.id == payload.partner_id))
    if not partner:
        raise HTTPException(404, "Partner not found")
    # Admin-created packages may skip the partner-approval step and go live
    # immediately when the operator trusts the content. The default is to land
    # in DRAFT for admin review/edit before activation. Callers can override
    # by passing status in payload through PATCH later, or by using the
    # dedicated status endpoint after creation.
    tour_service = await db.scalar(
        select(PartnerService.id).where(
            PartnerService.partner_id == payload.partner_id,
            PartnerService.service_type == "TOUR",
            PartnerService.is_active.is_(True),
        )
    )
    if tour_service is None:
        raise HTTPException(
            422, "Enable the TOUR service for this partner before adding a package"
        )
    # Admin-created packages default to ACTIVE — they have been authored by
    # the operator team and should appear on the public catalog immediately.
    package = await _save_package(
        db, payload, user, partner_id=payload.partner_id, status_value="ACTIVE"
    )
    return await _package_payload(db, package)


@admin_router.get("/packages/{package_id}")
async def admin_package(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    return await _package_payload(db, await _find_package(db, package_id))


@admin_router.patch("/packages/{package_id}")
async def admin_update_package(
    package_id: int,
    payload: PackageUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    package = await _find_package(db, package_id)
    values = payload.model_dump(
        exclude_unset=True,
        exclude={"itinerary", "inclusions", "exclusions", "pricing", "media"},
    )
    for key, value in values.items():
        setattr(package, key, value)
    if payload.package_name:
        package.slug = _slug(payload.package_name, package.package_code)
    if any(
        x is not None
        for x in (
            payload.itinerary,
            payload.inclusions,
            payload.exclusions,
            payload.pricing,
            payload.media,
        )
    ):
        source = PackageIn.model_validate(
            {
                **{
                    k: getattr(package, k)
                    for k in (
                        "package_name",
                        "package_type",
                        "destination",
                        "city_id",
                        "duration_days",
                        "duration_nights",
                        "minimum_persons",
                        "maximum_persons",
                        "short_description",
                        "description",
                        "terms_and_conditions",
                    )
                },
                **payload.model_dump(exclude_unset=True),
            }
        )
        await _replace_children(db, package.id, source)
    return await _package_payload(db, package)


@admin_router.post("/packages/{package_id}/status")
async def admin_package_status(
    package_id: int,
    payload: StatusIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    package = await _find_package(db, package_id)
    desired = payload.status.upper()
    if desired not in {
        "DRAFT",
        "PENDING_APPROVAL",
        "APPROVED",
        "ACTIVE",
        "INACTIVE",
        "SUSPENDED",
        "REJECTED",
    }:
        raise HTTPException(422, "Unsupported tour package status")
    if desired == "APPROVED":
        package.approved_at = datetime.now(timezone.utc)
        package.approved_by = _actor_uuid(user)
    if desired == "ACTIVE":
        package.activated_at = datetime.now(timezone.utc)
    package.status = desired
    package.rejection_reason = (
        payload.rejection_reason if desired in {"REJECTED", "DRAFT"} else None
    )
    return await _package_payload(db, package)


@admin_router.get("/bookings")
async def admin_tour_bookings(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    stmt = (
        select(TourBooking, TourPackage, MasterBooking, Customer)
        .join(TourPackage, TourPackage.id == TourBooking.package_id)
        .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
        .join(Customer, Customer.id == MasterBooking.customer_id)
        .order_by(desc(TourBooking.created_at))
    )
    if status_filter:
        stmt = stmt.where(TourBooking.booking_status == status_filter)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": b.id,
            "booking_number": b.booking_number,
            "package_id": p.id,
            "package_name": p.package_name,
            "customer_id": c.id,
            "customer_name": " ".join(filter(None, [c.first_name, c.last_name])),
            "travel_start_date": b.travel_start_date,
            "travel_end_date": b.travel_end_date,
            "persons_count": b.persons_count,
            "total_amount": float(b.total_amount),
            "platform_commission": float(b.platform_commission),
            "partner_payout": float(b.partner_payout),
            "payment_status": b.payment_status,
            "booking_status": b.booking_status,
            "created_at": b.created_at,
        }
        for b, p, _, c in rows
    ]


@admin_router.post("/bookings/{booking_id}/status")
async def admin_booking_status(
    booking_id: int,
    payload: StatusIn,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    booking = await db.scalar(select(TourBooking).where(TourBooking.id == booking_id))
    if not booking:
        raise HTTPException(404, "Tour booking not found")
    if payload.status.upper() not in {
        "PENDING_CONFIRMATION",
        "CONFIRMED",
        "IN_PROGRESS",
        "COMPLETED",
        "CANCELLED",
        "SETTLEMENT_PENDING",
        "SETTLED",
    }:
        raise HTTPException(422, "Unsupported tour booking status")
    if payload.status.upper() == "IN_PROGRESS" and not (
        booking.vehicle_id and booking.driver_id
    ):
        raise HTTPException(422, "Assign a vehicle and driver before starting the trip")
    if payload.status.upper() == "COMPLETED" and booking.payment_status != "PAID":
        raise HTTPException(422, "Collect the full payment before completing the tour.")
    booking.booking_status = payload.status.upper()
    return {"id": booking.id, "booking_status": booking.booking_status}


@admin_router.get("/bookings/{booking_id}")
async def admin_booking_detail(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    """Return the full tour booking (with master, customer, participants,
    package) so the admin booking-detail page can render every section
    without follow-up calls. Mirrors the hotel booking detail shape."""
    row = (
        await db.execute(
            select(TourBooking, TourPackage, MasterBooking, Customer, User)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
            .join(Customer, Customer.id == MasterBooking.customer_id)
            .join(User, User.id == Customer.user_id)
            .where(TourBooking.id == booking_id)
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Tour booking not found")
    tour, package, master, customer, user_row = row
    participants = (
        await db.scalars(
            select(TourParticipant)
            .where(TourParticipant.tour_booking_id == tour.id)
            .order_by(TourParticipant.id)
        )
    ).all()
    return {
        "id": tour.id,
        "uuid": str(tour.uuid),
        "booking_number": tour.booking_number,
        "master_booking_id": master.id,
        "master_booking_number": master.booking_number,
        "package": {
            "id": package.id,
            "package_code": package.package_code,
            "package_name": package.package_name,
            "destination": package.destination,
            "duration_days": package.duration_days,
            "duration_nights": package.duration_nights,
            "minimum_persons": package.minimum_persons,
            "maximum_persons": package.maximum_persons,
            "slug": package.slug,
        },
        "customer": {
            "id": customer.id,
            "name": " ".join(filter(None, [customer.first_name, customer.last_name])),
            "mobile": user_row.mobile_number,
            "email": user_row.email,
        },
        "customer_id": customer.id,
        "customer_name": " ".join(
            filter(None, [customer.first_name, customer.last_name])
        ),
        "customer_mobile": user_row.mobile_number,
        "customer_email": user_row.email,
        "travel_start_date": tour.travel_start_date,
        "travel_end_date": tour.travel_end_date,
        "persons_count": tour.persons_count,
        "total_amount": float(tour.total_amount),
        "platform_commission": float(tour.platform_commission),
        "partner_payout": float(tour.partner_payout),
        "payment_status": tour.payment_status,
        "booking_status": tour.booking_status,
        "special_requests": tour.special_requests,
        "participants": [
            {
                "id": p.id,
                "participant_name": p.participant_name,
                "mobile": p.mobile,
                "age": p.age,
                "gender": p.gender,
            }
            for p in participants
        ],
        "created_at": tour.created_at,
        "updated_at": tour.updated_at,
    }


# ════════════════════════════════════════════════════════════════
# TOUR BOOKING MANAGEMENT (admin) — mirrors the cab/hotel manage flow.
# Advance collection, vehicle/driver assignment, trip edits, additional
# charges, invoice PDF and settlement. Doc Ref: BRD Part 5 §6, Part 3 §45.
# ════════════════════════════════════════════════════════════════


@admin_router.get("/bookings/{booking_id}/manage")
async def admin_tour_booking_manage(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import get_manage_payload

    return await get_manage_payload(db, booking_id, include_fleet=True)


@admin_router.post("/bookings/{booking_id}/advance")
async def admin_tour_advance_collect(
    booking_id: int,
    payload: TourAdvanceIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import collect_advance

    result = await collect_advance(
        db,
        tour_booking_id=booking_id,
        amount=payload.amount,
        payment_mode=payload.payment_mode,
        received_by=payload.received_by,
        user_id=user["sub"],
        source_role="ADMIN",
        reference_number=payload.reference_number,
        notes=payload.notes,
    )
    return {
        "success": True,
        "message": f"Advance of ₹{result['amount']:,.2f} recorded. Receipt {result['receipt_number']}.",
        "data": result,
    }


@admin_router.post("/bookings/{booking_id}/advance/{advance_id}/void")
async def admin_tour_advance_void(
    booking_id: int,
    advance_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import void_advance

    reason = (payload.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(
            422, "A reason (min 3 chars) is required to void an advance"
        )
    result = await void_advance(
        db,
        tour_booking_id=booking_id,
        advance_id=advance_id,
        user_id=user["sub"],
        reason=reason,
    )
    return {
        "success": True,
        "message": f"Advance {result['receipt_number']} voided.",
        "data": result,
    }


@admin_router.get("/bookings/{booking_id}/advance/{advance_id}/receipt")
async def admin_tour_advance_receipt(
    booking_id: int,
    advance_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import build_advance_receipt

    doc = await build_advance_receipt(
        db, tour_booking_id=booking_id, advance_id=advance_id
    )
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@admin_router.post("/bookings/{booking_id}/vehicle")
async def admin_tour_assign_vehicle(
    booking_id: int,
    payload: TourVehicleAssignIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import assign_vehicle_driver

    result = await assign_vehicle_driver(
        db,
        tour_booking_id=booking_id,
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        user_id=user["sub"],
    )
    return {"success": True, "message": "Vehicle & driver assigned.", "data": result}


@admin_router.patch("/bookings/{booking_id}")
async def admin_tour_booking_update(
    booking_id: int,
    payload: TourTripUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import update_trip

    values = payload.model_dump(exclude_unset=True)
    result = await update_trip(
        db, tour_booking_id=booking_id, user_id=user["sub"], **values
    )
    return {"success": True, "message": "Booking updated.", "data": result}


@admin_router.post("/bookings/{booking_id}/charges")
async def admin_tour_add_charge(
    booking_id: int,
    payload: TourChargeIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import add_charge

    result = await add_charge(
        db,
        tour_booking_id=booking_id,
        label=payload.label,
        amount=payload.amount,
        reason=payload.reason,
        user_id=user["sub"],
        role="ADMIN",
    )
    return {"success": True, "message": "Additional charge added.", "data": result}


@admin_router.get("/bookings/{booking_id}/invoice")
async def admin_tour_invoice(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import build_invoice

    doc = await build_invoice(db, tour_booking_id=booking_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@admin_router.get("/bookings/{booking_id}/itinerary")
async def admin_tour_itinerary(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import build_itinerary

    doc = await build_itinerary(db, tour_booking_id=booking_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@admin_router.post("/bookings/{booking_id}/settle")
async def admin_tour_settle(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    from app.modules.tour.services import settle_booking

    result = await settle_booking(db, tour_booking_id=booking_id, user_id=user["sub"])
    return {"success": True, "message": "Booking settled with partner.", "data": result}


@admin_router.get("/stats")
async def admin_tour_stats(
    db: AsyncSession = Depends(get_db), _: dict = Depends(require_roles(*ADMIN_ROLES))
):
    """KPI numbers for the tour console header cards."""
    from datetime import date as _date

    today = _date.today()
    first_of_month = today.replace(day=1)
    counts_rows = (
        await db.execute(
            select(TourPackage.status, func.count(TourPackage.id)).group_by(
                TourPackage.status
            )
        )
    ).all()
    counts = {row[0]: int(row[1]) for row in counts_rows}
    total_bookings = await db.scalar(select(func.count(TourBooking.id))) or 0
    today_bookings = (
        await db.scalar(
            select(func.count(TourBooking.id)).where(
                func.date(TourBooking.created_at) == today
            )
        )
        or 0
    )
    revenue_today = (
        await db.scalar(
            select(func.coalesce(func.sum(TourBooking.total_amount), 0)).where(
                func.date(TourBooking.created_at) == today
            )
        )
    ) or 0
    revenue_mtd = (
        await db.scalar(
            select(func.coalesce(func.sum(TourBooking.total_amount), 0)).where(
                func.date(TourBooking.created_at) >= first_of_month
            )
        )
    ) or 0
    return {
        "active": counts.get("ACTIVE", 0),
        "pending_review": counts.get("PENDING_APPROVAL", 0),
        "draft": counts.get("DRAFT", 0),
        "rejected": counts.get("REJECTED", 0),
        "suspended": counts.get("SUSPENDED", 0),
        "inactive": counts.get("INACTIVE", 0),
        "total_packages": sum(counts.values()),
        "total_bookings": int(total_bookings),
        "today_bookings": int(today_bookings),
        "revenue_today": float(revenue_today),
        "revenue_mtd": float(revenue_mtd),
    }


@partner_router.get("/packages")
async def partner_packages(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    partner_id = await _partner_id(db, user)
    if partner_id is None:
        return []
    return await _list_packages(db, partner_id=partner_id)


@partner_router.get("/cities")
async def partner_tour_cities(
    db: AsyncSession = Depends(get_db), _: dict = Depends(require_roles(*PARTNER_ROLES))
):
    rows = (
        await db.execute(
            select(City.id, City.name)
            .where(City.is_active.is_(True))
            .order_by(City.name)
        )
    ).all()
    return [{"id": row.id, "name": row.name} for row in rows]


@partner_router.post("/packages", status_code=status.HTTP_201_CREATED)
async def partner_create_package(
    payload: PackageIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    service = await db.scalar(
        select(PartnerService.id).where(
            PartnerService.partner_id == partner_id,
            PartnerService.service_type == "TOUR",
            PartnerService.is_active.is_(True),
        )
    )
    if service is None:
        raise HTTPException(403, "Tour packages are not enabled for this partner")
    package = await _save_package(
        db, payload, user, partner_id=partner_id, status_value="DRAFT"
    )
    return await _package_payload(db, package)


@partner_router.get("/packages/{package_id}")
async def partner_package(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    return await _package_payload(
        db,
        await _find_package(db, package_id, own_partner_id=await _partner_id(db, user)),
    )


class PartnerTourPolicyIn(BaseModel):
    """Per-package cancellation ladder (BRD Part 5 §119). Absent fields keep
    the current stored values; all fields absent keeps defaults/global."""

    cancellation_free_days: Optional[int] = Field(None, ge=0, le=3650)
    cancellation_tier_1_days: Optional[int] = Field(None, ge=0, le=3650)
    refund_percent_tier_1: Optional[float] = Field(None, ge=0, le=100)
    cancellation_tier_2_days: Optional[int] = Field(None, ge=0, le=3650)
    refund_percent_tier_2: Optional[float] = Field(None, ge=0, le=100)
    refund_percent_tier_3: Optional[float] = Field(None, ge=0, le=100)
    refund_percent_last_minute: Optional[float] = Field(None, ge=0, le=100)
    cancellation_policy_text: Optional[str] = Field(None, max_length=5000)


@partner_router.get("/packages/{package_id}/cancellation-policy")
async def partner_tour_package_policy(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    """Read my package's cancellation ladder. No row yet → return the
    BRD §119 defaults so the UI can prefill, plus a flag that the global
    ladder is currently in effect."""
    package = await _find_package(
        db, package_id, own_partner_id=await _partner_id(db, user)
    )
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, tour_package_id, cancellation_free_days,
                       cancellation_tier_1_days, refund_percent_tier_1,
                       cancellation_tier_2_days, refund_percent_tier_2,
                       refund_percent_tier_3, refund_percent_last_minute,
                       cancellation_policy_text, updated_at
                FROM tour_package_policies WHERE tour_package_id = :id
                """
                ),
                {"id": package.id},
            )
        )
        .mappings()
        .first()
    )
    if row:
        return {**dict(row), "has_policy": True}
    return {
        "id": None,
        "tour_package_id": package.id,
        "cancellation_free_days": 30,
        "cancellation_tier_1_days": 15,
        "refund_percent_tier_1": 100.0,
        "cancellation_tier_2_days": 7,
        "refund_percent_tier_2": 75.0,
        "refund_percent_tier_3": 50.0,
        "refund_percent_last_minute": 0.0,
        "cancellation_policy_text": None,
        "updated_at": None,
        "has_policy": False,
    }


@partner_router.put("/packages/{package_id}/cancellation-policy")
async def partner_update_tour_package_policy(
    package_id: int,
    payload: PartnerTourPolicyIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    """Upsert my package's cancellation ladder. The policy engine reads this
    row before the global ladder, so this is the per-package override."""
    package = await _find_package(
        db, package_id, own_partner_id=await _partner_id(db, user)
    )
    vals = payload.model_dump(exclude_none=True)
    if not vals:
        return {
            "success": True,
            "message": "No fields to update.",
            "tour_package_id": package.id,
        }
    existing = (
        await db.execute(
            text("SELECT id FROM tour_package_policies WHERE tour_package_id = :id"),
            {"id": package.id},
        )
    ).first()
    if existing is None:
        defaults = {
            "cancellation_free_days": 30,
            "cancellation_tier_1_days": 15,
            "refund_percent_tier_1": 100.0,
            "cancellation_tier_2_days": 7,
            "refund_percent_tier_2": 75.0,
            "refund_percent_tier_3": 50.0,
            "refund_percent_last_minute": 0.0,
        }
        merged = {**defaults, **vals}
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
                "pid": package.id,
                "fd": merged["cancellation_free_days"],
                "t1d": merged["cancellation_tier_1_days"],
                "t1p": merged["refund_percent_tier_1"],
                "t2d": merged["cancellation_tier_2_days"],
                "t2p": merged["refund_percent_tier_2"],
                "t3p": merged["refund_percent_tier_3"],
                "lm": merged["refund_percent_last_minute"],
                "txt": vals.get("cancellation_policy_text"),
            },
        )
    else:
        set_clause = ", ".join(f"{k} = :{k}" for k in vals)
        await db.execute(
            text(
                f"UPDATE tour_package_policies SET {set_clause}, updated_at = NOW() "
                "WHERE tour_package_id = :pid"
            ),
            {**vals, "pid": package.id},
        )
    await db.commit()
    return {
        "success": True,
        "message": "Tour package cancellation policy saved.",
        "tour_package_id": package.id,
    }


@partner_router.patch("/packages/{package_id}")
async def partner_update_package(
    package_id: int,
    payload: PackageUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    package = await _find_package(
        db, package_id, own_partner_id=await _partner_id(db, user)
    )
    if package.status in {"ACTIVE", "SUSPENDED"}:
        raise HTTPException(409, "Active packages must be edited by admin")
    values = payload.model_dump(
        exclude_unset=True,
        exclude={"itinerary", "inclusions", "exclusions", "pricing", "media"},
    )
    for key, value in values.items():
        setattr(package, key, value)
    if payload.package_name:
        package.slug = _slug(payload.package_name, package.package_code)
    if any(
        x is not None
        for x in (
            payload.itinerary,
            payload.inclusions,
            payload.exclusions,
            payload.pricing,
            payload.media,
        )
    ):
        source = PackageIn.model_validate(
            {
                **{
                    k: getattr(package, k)
                    for k in (
                        "package_name",
                        "package_type",
                        "destination",
                        "city_id",
                        "duration_days",
                        "duration_nights",
                        "minimum_persons",
                        "maximum_persons",
                        "short_description",
                        "description",
                        "terms_and_conditions",
                    )
                },
                **payload.model_dump(exclude_unset=True),
            }
        )
        await _replace_children(db, package.id, source)
    return await _package_payload(db, package)


@partner_router.post("/packages/{package_id}/submit")
async def partner_submit_package(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    package = await _find_package(
        db, package_id, own_partner_id=await _partner_id(db, user)
    )
    # Validation before submit: must have at least one pricing slab and
    # itinerary day so the admin can review a real product.
    pricing_count = (
        await db.scalar(
            select(func.count(TourPackagePricing.id)).where(
                TourPackagePricing.package_id == package.id
            )
        )
        or 0
    )
    itinerary_count = (
        await db.scalar(
            select(func.count(TourItinerary.id)).where(
                TourItinerary.package_id == package.id
            )
        )
        or 0
    )
    if pricing_count == 0:
        raise HTTPException(
            422, "Add at least one pricing slab before submitting for review"
        )
    if itinerary_count == 0:
        raise HTTPException(
            422, "Add a day-by-day itinerary before submitting for review"
        )
    package.status = "PENDING_APPROVAL"
    package.submitted_at = datetime.now(timezone.utc)
    package.rejection_reason = None
    return await _package_payload(db, package)


# Partner-managed tour booking workflow (mirrors the hotel flow):
#   PENDING_CONFIRMATION → (partner accepts) → CONFIRMED
#   CONFIRMED            → (partner starts tour) → IN_PROGRESS
#   IN_PROGRESS          → (partner completes) → COMPLETED
#   PENDING_CONFIRMATION | CONFIRMED → (partner cancels) → CANCELLED
TOUR_PARTNER_TRANSITIONS: dict[str, set[str]] = {
    "PENDING_CONFIRMATION": {"CONFIRMED", "CANCELLED"},
    "CONFIRMED": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}

_TOUR_STATUS_EVENTS = {
    "CONFIRMED": ("BOOKING_ACCEPTED", "accepted"),
    "IN_PROGRESS": ("TOUR_STARTED", "marked as in progress"),
    "COMPLETED": ("TOUR_COMPLETED", "completed"),
    "CANCELLED": ("TOUR_CANCELLED", "cancelled"),
}


@partner_router.get("/bookings")
async def partner_tour_bookings(
    package_id: Optional[int] = None,
    status: Optional[str] = Query(None, description="Filter by booking_status"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    """Partner's tour bookings across all their packages, with status
    filter, search and pagination. Powers the Bookings → Tours tab."""
    partner_id = await _partner_id(db, user)
    if partner_id is None:
        return {"items": [], "total": 0, "page": 1, "pages": 1}
    stmt = (
        select(TourBooking, TourPackage, MasterBooking, Customer, User)
        .join(TourPackage, TourPackage.id == TourBooking.package_id)
        .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
        .join(Customer, Customer.id == MasterBooking.customer_id)
        .join(User, User.id == Customer.user_id)
        .where(TourPackage.partner_id == partner_id)
        .order_by(desc(TourBooking.created_at))
    )
    if package_id is not None:
        stmt = stmt.where(TourBooking.package_id == package_id)
    if status:
        stmt = stmt.where(TourBooking.booking_status == status.upper())
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                TourBooking.booking_number.ilike(term),
                TourPackage.package_name.ilike(term),
                MasterBooking.booking_number.ilike(term),
                (Customer.first_name + " " + Customer.last_name).ilike(term),
            )
        )
    total = (await db.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
    rows = (
        await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).all()
    items = [
        {
            "id": b.id,
            "booking_number": b.booking_number,
            "master_booking_id": master.id,
            "master_booking_number": master.booking_number,
            "package_id": p.id,
            "package_name": p.package_name,
            "destination": p.destination,
            "customer_id": c.id,
            "customer_name": " ".join(filter(None, [c.first_name, c.last_name])),
            "customer_mobile": u.mobile_number,
            "travel_start_date": b.travel_start_date,
            "travel_end_date": b.travel_end_date,
            "persons_count": b.persons_count,
            "total_amount": float(b.total_amount),
            "platform_commission": float(b.platform_commission),
            "partner_payout": float(b.partner_payout),
            "payment_status": b.payment_status,
            "booking_status": b.booking_status,
            "created_at": b.created_at,
        }
        for b, p, master, c, u in rows
    ]
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "pages": (int(total) + page_size - 1) // page_size if total else 1,
    }


@partner_router.get("/bookings/{booking_id}")
async def partner_tour_booking_detail(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    """Full tour booking detail for the owning partner: package, customer,
    participants, money split, special requests and the booking timeline.
    Mirrors the admin tour detail but ownership-scoped to the package."""
    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    row = (
        await db.execute(
            select(TourBooking, TourPackage, MasterBooking, Customer, User)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .join(MasterBooking, MasterBooking.id == TourBooking.master_booking_id)
            .join(Customer, Customer.id == MasterBooking.customer_id)
            .join(User, User.id == Customer.user_id)
            .where(TourBooking.id == booking_id)
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Tour booking not found")
    tour, package, master, customer, user_row = row
    if package.partner_id != partner_id:
        raise HTTPException(403, "This booking does not belong to your tour packages")

    participants = (
        await db.scalars(
            select(TourParticipant)
            .where(TourParticipant.tour_booking_id == tour.id)
            .order_by(TourParticipant.id)
        )
    ).all()

    from sqlalchemy import text as _text

    timeline_rows = (
        (
            await db.execute(
                _text(
                    "SELECT event_type, event_description, event_timestamp "
                    "FROM booking_timelines WHERE master_booking_id = :mb "
                    "ORDER BY event_timestamp DESC, id DESC"
                ),
                {"mb": master.id},
            )
        )
        .mappings()
        .all()
    )

    return {
        "id": tour.id,
        "uuid": str(tour.uuid),
        "booking_number": tour.booking_number,
        "master_booking_id": master.id,
        "master_booking_number": master.booking_number,
        "package": {
            "id": package.id,
            "package_code": package.package_code,
            "package_name": package.package_name,
            "destination": package.destination,
            "duration_days": package.duration_days,
            "duration_nights": package.duration_nights,
            "minimum_persons": package.minimum_persons,
            "maximum_persons": package.maximum_persons,
            "slug": package.slug,
        },
        "customer": {
            "id": customer.id,
            "name": " ".join(filter(None, [customer.first_name, customer.last_name])),
            "mobile": user_row.mobile_number,
            "email": user_row.email,
        },
        "customer_id": customer.id,
        "customer_name": " ".join(
            filter(None, [customer.first_name, customer.last_name])
        ),
        "customer_mobile": user_row.mobile_number,
        "customer_email": user_row.email,
        "travel_start_date": tour.travel_start_date,
        "travel_end_date": tour.travel_end_date,
        "persons_count": tour.persons_count,
        "total_amount": float(tour.total_amount),
        "platform_commission": float(tour.platform_commission),
        "partner_payout": float(tour.partner_payout),
        "payment_status": tour.payment_status,
        "booking_status": tour.booking_status,
        "special_requests": tour.special_requests,
        "participants": [
            {
                "id": p.id,
                "participant_name": p.participant_name,
                "mobile": p.mobile,
                "age": p.age,
                "gender": p.gender,
                "id_type": p.id_type,
                "id_number": p.id_number,
            }
            for p in participants
        ],
        "timeline": [
            {
                "event_type": t["event_type"],
                "event_description": t["event_description"],
                "event_timestamp": t["event_timestamp"],
            }
            for t in timeline_rows
        ],
        "created_at": tour.created_at,
        "updated_at": tour.updated_at,
    }


class PartnerTourBookingStatusIn(BaseModel):
    status: str  # CONFIRMED | IN_PROGRESS | COMPLETED | CANCELLED
    reason: Optional[str] = None


@partner_router.post("/bookings/{booking_id}/status")
async def partner_tour_booking_status(
    booking_id: int,
    payload: PartnerTourBookingStatusIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    """Partner manages a tour booking lifecycle (fixed assignment — no
    admin step in between, mirroring the hotel flow):

      Accept:   PENDING_CONFIRMATION → CONFIRMED
      Start:    CONFIRMED → IN_PROGRESS
      Complete: IN_PROGRESS → COMPLETED
      Cancel:   PENDING_CONFIRMATION | CONFIRMED → CANCELLED (reason required)

    The master booking status is kept in sync, a timeline row is written,
    and the partner's other tabs are told to close any accept popup.
    """
    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")

    booking = await db.scalar(select(TourBooking).where(TourBooking.id == booking_id))
    if not booking:
        raise HTTPException(404, "Tour booking not found")
    package = await db.scalar(
        select(TourPackage).where(TourPackage.id == booking.package_id)
    )
    if not package or package.partner_id != partner_id:
        raise HTTPException(403, "This booking does not belong to your tour packages")

    desired = payload.status.upper()
    allowed = TOUR_PARTNER_TRANSITIONS.get(booking.booking_status, set())
    if desired not in allowed:
        raise HTTPException(
            400,
            f"Cannot move booking from '{booking.booking_status}' to '{desired}'. "
            f"Allowed transitions: {sorted(allowed) or 'none'}.",
        )
    if desired == "CANCELLED" and not (payload.reason or "").strip():
        raise HTTPException(422, "A reason is required to cancel a tour booking")
    if desired == "IN_PROGRESS" and not (booking.vehicle_id and booking.driver_id):
        raise HTTPException(422, "Assign a vehicle and driver before starting the tour")
    if desired == "COMPLETED" and booking.payment_status != "PAID":
        raise HTTPException(422, "Collect the full payment before completing the tour.")

    booking.booking_status = desired
    if desired == "CANCELLED" and payload.reason:
        booking.special_requests = (
            f"{(booking.special_requests or '').strip()} "
            f"[Cancelled: {payload.reason.strip()}]"
        ).strip()

    # Keep the master booking status in sync.
    master = await db.scalar(
        select(MasterBooking).where(MasterBooking.id == booking.master_booking_id)
    )
    if master:
        _master_map = {
            "CONFIRMED": "CONFIRMED",
            "IN_PROGRESS": "IN_PROGRESS",
            "COMPLETED": "COMPLETED",
            "CANCELLED": "CANCELLED",
        }
        target_master = _master_map[desired]
        if master.booking_status != target_master:
            master.booking_status = target_master

    event_type, verb = _TOUR_STATUS_EVENTS[desired]
    await db.execute(
        text(
            "INSERT INTO booking_timelines "
            "(master_booking_id, event_type, event_description, event_timestamp) "
            "VALUES (:mb, :evt, :desc, NOW())"
        ),
        {
            "mb": booking.master_booking_id,
            "evt": event_type,
            "desc": (
                f"Tour booking {booking.booking_number} {verb} by partner."
                + (
                    f" Reason: {payload.reason.strip()}"
                    if desired == "CANCELLED" and payload.reason
                    else ""
                )
            ),
        },
    )
    await db.commit()

    # ── Email: confirm / complete / cancel to the customer ──
    if master is not None and desired in ("CONFIRMED", "COMPLETED", "CANCELLED"):
        try:
            from app.infrastructure.email import send_event_email

            cust_row = (
                await db.execute(
                    select(User.email, Customer.first_name, Customer.last_name)
                    .join(Customer, Customer.user_id == User.id)
                    .where(Customer.id == master.customer_id)
                )
            ).first()
            if cust_row and cust_row.email:
                package_name = await db.scalar(
                    select(TourPackage.package_name).where(
                        TourPackage.id == booking.package_id
                    )
                )
                name = (
                    " ".join(filter(None, [cust_row.first_name, cust_row.last_name]))
                    or "there"
                )
                if desired == "CONFIRMED":
                    evt, msg = "booking_confirmed", (
                        "Your tour package is confirmed. The itinerary and partner "
                        "details are available in your bookings."
                    )
                elif desired == "COMPLETED":
                    evt, msg = "booking_completed", (
                        "Your tour is complete! Thank you for travelling with WayTero. "
                        "Your tax invoice is available in your bookings."
                    )
                else:
                    evt, msg = "booking_cancelled", (
                        "Your tour booking has been cancelled. Any refund due has been "
                        "processed to your WayTero Wallet per the cancellation policy."
                    )
                await send_event_email(
                    db,
                    event_type=evt,
                    to_email=cust_row.email,
                    to_name=name,
                    context={
                        "name": name,
                        "service": "Tour package",
                        "booking_number": booking.booking_number,
                        "message": msg,
                        "details": [
                            ("Package", package_name or ""),
                            (
                                "Travel start",
                                (
                                    booking.travel_start_date.strftime("%d %b %Y")
                                    if booking.travel_start_date
                                    else ""
                                ),
                            ),
                            ("Travellers", str(booking.persons_count or "")),
                        ],
                        **(
                            {
                                "amount": float(booking.total_amount),
                                "amount_label": "Package amount",
                            }
                            if booking.total_amount
                            else {}
                        ),
                    },
                    related_type="TOUR_BOOKING",
                    related_id=booking.id,
                )
        except Exception:  # pragma: no cover — email must never break bookings
            pass

    # Close the accept popup in the partner's other tabs.
    try:
        from app.modules.notification.realtime import manager as _rt

        await _rt.send_to_user(
            user["sub"],
            {
                "event": "BOOKING_PARTNER_RESPONDED",
                "data": {
                    "service_type": "TOUR",
                    "tour_booking_id": booking.id,
                    "master_booking_id": booking.master_booking_id,
                    "decision": desired,
                },
            },
        )
    except Exception:  # pragma: no cover
        pass

    # Tell online admins the booking is resolved so their accept modal
    # drops it + the ringtone stops (the partner handled it, not the admin).
    try:
        from app.modules.notification.services.booking_notifications import (
            notify_admins_booking_resolved,
        )

        await notify_admins_booking_resolved(
            db,
            service_type="TOUR",
            master_booking_id=booking.master_booking_id,
            service_number=booking.booking_number,
            decision=desired,
        )
    except Exception:  # pragma: no cover - best-effort
        pass

    # Real-time fan-out: refresh the booking pages on both portals.
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
            service_status=desired,
            action="PARTNER_STATUS_CHANGE",
            payment_status=booking.payment_status,
            partner_ids=[partner_id],
        )
    except Exception:  # pragma: no cover - best-effort
        pass

    return {"id": booking.id, "booking_status": booking.booking_status}


# ════════════════════════════════════════════════════════════════
# TOUR BOOKING MANAGEMENT (partner) — same manage flow as admin, scoped to
# the partner's own packages and fleet. Settlement stays admin-only.
# ════════════════════════════════════════════════════════════════


async def _partner_owns_tour_booking(
    db: AsyncSession, booking_id: int, partner_id: int
) -> TourBooking:
    row = (
        await db.execute(
            select(TourBooking, TourPackage.partner_id)
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .where(TourBooking.id == booking_id)
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Tour booking not found")
    if row[1] != partner_id:
        raise HTTPException(403, "This booking does not belong to your tour packages")
    return row[0]


@partner_router.get("/bookings/{booking_id}/manage")
async def partner_tour_booking_manage(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import get_manage_payload

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    return await get_manage_payload(db, booking_id, include_fleet=True)


@partner_router.post("/bookings/{booking_id}/advance")
async def partner_tour_advance_collect(
    booking_id: int,
    payload: TourAdvanceIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import collect_advance

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    # The partner side physically collects — force the custody marker so the
    # settlement math nets the money they hold.
    received_by = (payload.received_by or "PARTNER").upper()
    if received_by not in ("PARTNER", "DRIVER"):
        received_by = "PARTNER"
    result = await collect_advance(
        db,
        tour_booking_id=booking_id,
        amount=payload.amount,
        payment_mode=payload.payment_mode,
        received_by=received_by,
        user_id=user["sub"],
        source_role="PARTNER",
        reference_number=payload.reference_number,
        notes=payload.notes,
    )
    return {
        "success": True,
        "message": f"Advance of ₹{result['amount']:,.2f} recorded. Receipt {result['receipt_number']}.",
        "data": result,
    }


@partner_router.post("/bookings/{booking_id}/advance/{advance_id}/void")
async def partner_tour_advance_void(
    booking_id: int,
    advance_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import void_advance

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    reason = (payload.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(
            422, "A reason (min 3 chars) is required to void an advance"
        )
    result = await void_advance(
        db,
        tour_booking_id=booking_id,
        advance_id=advance_id,
        user_id=user["sub"],
        reason=reason,
    )
    return {
        "success": True,
        "message": f"Advance {result['receipt_number']} voided.",
        "data": result,
    }


@partner_router.get("/bookings/{booking_id}/advance/{advance_id}/receipt")
async def partner_tour_advance_receipt(
    booking_id: int,
    advance_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import build_advance_receipt

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    doc = await build_advance_receipt(
        db, tour_booking_id=booking_id, advance_id=advance_id
    )
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@partner_router.post("/bookings/{booking_id}/vehicle")
async def partner_tour_assign_vehicle(
    booking_id: int,
    payload: TourVehicleAssignIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import assign_vehicle_driver

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    result = await assign_vehicle_driver(
        db,
        tour_booking_id=booking_id,
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        user_id=user["sub"],
    )
    return {"success": True, "message": "Vehicle & driver assigned.", "data": result}


@partner_router.patch("/bookings/{booking_id}")
async def partner_tour_booking_update(
    booking_id: int,
    payload: TourTripUpdateIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import update_trip

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    values = payload.model_dump(exclude_unset=True)
    result = await update_trip(
        db, tour_booking_id=booking_id, user_id=user["sub"], **values
    )
    return {"success": True, "message": "Booking updated.", "data": result}


@partner_router.post("/bookings/{booking_id}/charges")
async def partner_tour_add_charge(
    booking_id: int,
    payload: TourChargeIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import add_charge

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    result = await add_charge(
        db,
        tour_booking_id=booking_id,
        label=payload.label,
        amount=payload.amount,
        reason=payload.reason,
        user_id=user["sub"],
        role="PARTNER",
    )
    return {"success": True, "message": "Additional charge added.", "data": result}


@partner_router.get("/bookings/{booking_id}/invoice")
async def partner_tour_invoice(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import build_invoice

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    doc = await build_invoice(db, tour_booking_id=booking_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@partner_router.get("/bookings/{booking_id}/itinerary")
async def partner_tour_itinerary(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    from app.modules.tour.services import build_itinerary

    partner_id = await _partner_id(db, user)
    if partner_id is None:
        raise HTTPException(403, "Partner profile not found")
    await _partner_owns_tour_booking(db, booking_id, partner_id)
    doc = await build_itinerary(db, tour_booking_id=booking_id)
    return Response(
        content=doc["pdf"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc["filename"]}"'},
    )


@partner_router.get("/stats")
async def partner_tour_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_roles(*PARTNER_ROLES)),
):
    partner_id = await _partner_id(db, user)
    if partner_id is None:
        return {
            "active": 0,
            "pending_review": 0,
            "draft": 0,
            "rejected": 0,
            "total_bookings": 0,
            "revenue_lifetime": 0.0,
        }
    counts_rows = (
        await db.execute(
            select(TourPackage.status, func.count(TourPackage.id))
            .where(TourPackage.partner_id == partner_id)
            .group_by(TourPackage.status)
        )
    ).all()
    counts = {row[0]: int(row[1]) for row in counts_rows}
    bookings_count = (
        await db.scalar(
            select(func.count(TourBooking.id))
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .where(TourPackage.partner_id == partner_id)
        )
    ) or 0
    revenue = (
        await db.scalar(
            select(func.coalesce(func.sum(TourBooking.partner_payout), 0))
            .join(TourPackage, TourPackage.id == TourBooking.package_id)
            .where(
                TourPackage.partner_id == partner_id,
                TourBooking.booking_status.in_(["COMPLETED", "SETTLED"]),
            )
        )
    ) or 0
    return {
        "active": counts.get("ACTIVE", 0),
        "pending_review": counts.get("PENDING_APPROVAL", 0),
        "draft": counts.get("DRAFT", 0),
        "rejected": counts.get("REJECTED", 0),
        "suspended": counts.get("SUSPENDED", 0),
        "total_packages": sum(counts.values()),
        "total_bookings": int(bookings_count),
        "revenue_lifetime": float(revenue),
    }


@public_router.get("/popular-destinations")
async def public_popular_destinations(db: AsyncSession = Depends(get_db)):
    """Top 6 cities with the most ACTIVE tour packages — used by the customer
    homepage destinations grid and the listing page sidebar."""
    rows = (
        await db.execute(
            select(City.id, City.name, func.count(TourPackage.id).label("cnt"))
            .join(TourPackage, TourPackage.city_id == City.id)
            .where(TourPackage.status == "ACTIVE", City.is_active.is_(True))
            .group_by(City.id, City.name)
            .order_by(desc("cnt"), City.name)
            .limit(6)
        )
    ).all()
    items = []
    for cid, name, cnt in rows:
        # Pull a representative primary image from any package in this city
        media_url = await db.scalar(
            select(TourPackageMedia.media_url)
            .join(TourPackage, TourPackage.id == TourPackageMedia.package_id)
            .where(
                TourPackage.city_id == cid,
                TourPackage.status == "ACTIVE",
                TourPackageMedia.is_primary.is_(True),
            )
            .order_by(desc(TourPackage.created_at))
            .limit(1)
        )
        items.append(
            {
                "city_id": cid,
                "name": name,
                "package_count": int(cnt),
                "image_url": media_url,
            }
        )
    return {"items": items}


@public_router.get("/suggestions")
async def public_tour_suggestions(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(8, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    """Autocomplete feed for the hero Tours search.

    Matches ACTIVE tour packages by package name, destination, package code
    or the package's city name (so "Puri", "Bhubaneswar" and "Andaman" all
    surface the packages a customer can actually book). Returns lightweight
    rows for the dropdown — selecting one deep-links straight to
    /tours/{slug}.
    """
    term = f"%{q.strip()}%"
    rows = (
        await db.execute(
            select(TourPackage, City)
            .join(City, City.id == TourPackage.city_id)
            .where(
                TourPackage.status == "ACTIVE",
                or_(
                    TourPackage.package_name.ilike(term),
                    TourPackage.destination.ilike(term),
                    TourPackage.package_code.ilike(term),
                    City.name.ilike(term),
                ),
            )
            .order_by(desc(TourPackage.created_at))
            .limit(limit)
        )
    ).all()
    items = []
    for package, city in rows:
        primary = (
            await db.scalars(
                select(TourPackageMedia)
                .where(
                    TourPackageMedia.package_id == package.id,
                    TourPackageMedia.is_primary.is_(True),
                )
                .limit(1)
            )
        ).first()
        pricing = (
            await db.scalars(
                select(TourPackagePricing)
                .where(TourPackagePricing.package_id == package.id)
                .order_by(TourPackagePricing.persons_count)
            )
        ).all()
        items.append(
            {
                "id": package.id,
                "slug": package.slug,
                "package_name": package.package_name,
                "destination": package.destination,
                "city_name": city.name,
                "duration_days": package.duration_days,
                "duration_nights": package.duration_nights,
                "starting_price": float(
                    min((x.package_price for x in pricing), default=0)
                ),
                "primary_image_url": primary.media_url if primary else None,
            }
        )
    return {"items": items}


@public_router.get("/packages")
async def public_packages(
    destination: Optional[str] = None,
    city_id: Optional[int] = None,
    persons: Optional[int] = None,
    duration: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    package_type: Optional[str] = None,
    sort: Optional[str] = None,  # "price_asc" | "price_desc" | "newest"
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=48),
    db: AsyncSession = Depends(get_db),
):
    items = await _list_packages(
        db,
        statuses=["ACTIVE"],
        search=destination,
        city_id=city_id,
        include_private=False,
    )
    if duration:
        try:
            lo, hi = duration.split("-")
            items = [x for x in items if int(lo) <= x["duration_days"] <= int(hi)]
        except (ValueError, AttributeError):
            pass
    if persons:
        items = [
            x
            for x in items
            if x["minimum_persons"] <= persons <= (x["maximum_persons"] or persons)
        ]
    if package_type:
        items = [
            x
            for x in items
            if (x.get("package_type") or "").upper() == package_type.upper()
        ]
    # Filter / sort by price using the starting slab.
    for item in items:
        item["primary_image_url"] = next(
            (m["media_url"] for m in item["media"] if m["is_primary"]),
            item["media"][0]["media_url"] if item["media"] else None,
        )
        item["starting_price"] = min(
            (p["package_price"] for p in item["pricing"]), default=0
        )
    if min_price is not None:
        items = [x for x in items if x["starting_price"] >= min_price]
    if max_price is not None:
        items = [x for x in items if x["starting_price"] <= max_price]
    if sort == "price_asc":
        items.sort(key=lambda x: x["starting_price"])
    elif sort == "price_desc":
        items.sort(key=lambda x: x["starting_price"], reverse=True)
    elif sort == "newest":
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@public_router.get("/packages/{slug}")
async def public_package(slug: str, db: AsyncSession = Depends(get_db)):
    package = await db.scalar(
        select(TourPackage).where(
            TourPackage.slug == slug, TourPackage.status == "ACTIVE"
        )
    )
    if not package:
        raise HTTPException(404, "Tour package not found")
    data = await _package_payload(db, package, include_private=False)
    data["primary_image_url"] = next(
        (m["media_url"] for m in data["media"] if m["is_primary"]),
        data["media"][0]["media_url"] if data["media"] else None,
    )
    data["starting_price"] = min(
        (p["package_price"] for p in data["pricing"]), default=0
    )
    # Related: other ACTIVE packages in the same city, excluding current.
    related_rows = (
        await db.scalars(
            select(TourPackage)
            .where(
                TourPackage.city_id == package.city_id,
                TourPackage.status == "ACTIVE",
                TourPackage.id != package.id,
            )
            .order_by(desc(TourPackage.created_at))
            .limit(4)
        )
    ).all()
    related = []
    for r in related_rows:
        primary = (
            await db.scalars(
                select(TourPackageMedia).where(
                    TourPackageMedia.package_id == r.id,
                    TourPackageMedia.is_primary.is_(True),
                )
            )
        ).first()
        pricing = (
            await db.scalars(
                select(TourPackagePricing)
                .where(TourPackagePricing.package_id == r.id)
                .order_by(TourPackagePricing.persons_count)
            )
        ).all()
        related.append(
            {
                "id": r.id,
                "slug": r.slug,
                "package_name": r.package_name,
                "destination": r.destination,
                "duration_days": r.duration_days,
                "duration_nights": r.duration_nights,
                "image_url": primary.media_url if primary else None,
                "starting_price": float(
                    min((p.package_price for p in pricing), default=0)
                ),
            }
        )
    data["related"] = related
    return data


@public_router.get("/packages/{slug}/related")
async def public_related(slug: str, db: AsyncSession = Depends(get_db)):
    """Other ACTIVE packages in the same city as ``slug`` (max 6). Used by
    the customer-web detail page 'Other tours in {city}' strip."""
    package = await db.scalar(
        select(TourPackage).where(
            TourPackage.slug == slug, TourPackage.status == "ACTIVE"
        )
    )
    if not package:
        raise HTTPException(404, "Tour package not found")
    related_rows = (
        await db.scalars(
            select(TourPackage)
            .where(
                TourPackage.city_id == package.city_id,
                TourPackage.status == "ACTIVE",
                TourPackage.id != package.id,
            )
            .order_by(desc(TourPackage.created_at))
            .limit(6)
        )
    ).all()
    items = []
    for r in related_rows:
        primary = (
            await db.scalars(
                select(TourPackageMedia).where(
                    TourPackageMedia.package_id == r.id,
                    TourPackageMedia.is_primary.is_(True),
                )
            )
        ).first()
        pricing = (
            await db.scalars(
                select(TourPackagePricing)
                .where(TourPackagePricing.package_id == r.id)
                .order_by(TourPackagePricing.persons_count)
            )
        ).all()
        items.append(
            {
                "id": r.id,
                "slug": r.slug,
                "package_name": r.package_name,
                "destination": r.destination,
                "duration_days": r.duration_days,
                "duration_nights": r.duration_nights,
                "minimum_persons": r.minimum_persons,
                "package_type": r.package_type,
                "partner_name": r.partner.business_name if r.partner else None,
                "image_url": primary.media_url if primary else None,
                "starting_price": float(
                    min((p.package_price for p in pricing), default=0)
                ),
            }
        )
    return {"items": items}


@public_router.get("/packages/{package_id}/quote")
async def public_quote(
    package_id: int, persons: int = Query(..., ge=1), db: AsyncSession = Depends(get_db)
):
    package = await db.scalar(
        select(TourPackage).where(
            TourPackage.id == package_id, TourPackage.status == "ACTIVE"
        )
    )
    if not package:
        raise HTTPException(404, "Tour package not found")
    total, commission, payout, percent = await _price_for(db, package, persons)
    return {
        "package_id": package.id,
        "persons_count": persons,
        "total_amount": float(total),
        "platform_commission": float(commission),
        "partner_payout": float(payout),
        "commission_percent": float(percent),
        "currency": "INR",
    }


@public_router.post("/bookings", status_code=status.HTTP_201_CREATED)
async def create_public_booking(
    payload: BookingIn,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    customer_id = await _customer_id(db, user)
    if customer_id is None:
        raise HTTPException(403, "Customer profile not found")
    package = await db.scalar(
        select(TourPackage).where(
            TourPackage.id == payload.package_id, TourPackage.status == "ACTIVE"
        )
    )
    if not package:
        raise HTTPException(404, "Tour package not found")
    if len(payload.participants) > payload.persons_count:
        raise HTTPException(422, "Participant list cannot exceed traveller count")
    total, commission, payout, _ = await _price_for(db, package, payload.persons_count)
    end_date = payload.travel_start_date + timedelta(days=package.duration_days - 1)
    master = MasterBooking(
        booking_number=await next_master_booking_number(db),
        customer_id=customer_id,
        city_id=package.city_id,
        booking_status="PENDING_PAYMENT",
        payment_status="PENDING",
        total_amount=total,
        journey_start_date=payload.travel_start_date,
        journey_end_date=end_date,
        remarks=payload.special_requests,
    )
    db.add(master)
    await db.flush()
    tour = TourBooking(
        master_booking_id=master.id,
        package_id=package.id,
        booking_number=f"TO-{datetime.now(timezone.utc):%Y%m%d}-{str(master.id).zfill(5)}",
        travel_start_date=payload.travel_start_date,
        travel_end_date=end_date,
        persons_count=payload.persons_count,
        total_amount=total,
        platform_commission=commission,
        partner_payout=payout,
        special_requests=payload.special_requests,
    )
    db.add(tour)
    await db.flush()
    db.add(
        BookingService(
            master_booking_id=master.id,
            service_type="TOUR",
            service_reference_id=tour.id,
            service_status=tour.booking_status,
            service_amount=total,
        )
    )
    for p in payload.participants:
        db.add(TourParticipant(tour_booking_id=tour.id, **p.model_dump()))

    # ── Realtime: notify admins (accept modal) + the package's partner ──
    # The package's owner partner is the FIXED assignee for tour bookings —
    # they get their own WS popup to accept/reject and start managing.
    try:
        from app.modules.notification.services.booking_notifications import (
            admin_booking_requested,
            partner_tour_booking_requested,
        )

        await admin_booking_requested(
            db,
            master_booking_id=master.id,
            service_type="TOUR",
            booking_number=tour.booking_number,
            title=f"New tour booking: {tour.booking_number}",
            body=(
                f"{package.package_name} · {payload.travel_start_date} · "
                f"{payload.persons_count} pax. Accept to confirm."
            ),
            data={
                "service_id": tour.id,
                "package_name": package.package_name,
                "travel_start_date": payload.travel_start_date.isoformat(),
                "persons_count": payload.persons_count,
                "amount": float(total),
            },
        )
        if package.partner_id:
            await partner_tour_booking_requested(
                db,
                partner_id=package.partner_id,
                master_booking_id=master.id,
                tour_booking_id=tour.id,
                tour_booking_number=tour.booking_number,
                package_name=package.package_name or "",
                travel_start_date=payload.travel_start_date.isoformat(),
                persons_count=payload.persons_count,
                total_amount=float(total),
            )
    except Exception:  # pragma: no cover - never block the booking
        pass

    # ── Email: booking-received confirmation to the customer (if they have
    #    an email on file). Uses the admin-configured SMTP (Settings → Email)
    #    and the branded WayTero template with the platform logo. Never
    #    raises — the engine logs failures and the booking must not break.
    try:
        from app.infrastructure.email import send_event_email

        cust_row = (
            await db.execute(
                select(User.email, Customer.first_name, Customer.last_name)
                .join(Customer, Customer.user_id == User.id)
                .where(Customer.id == customer_id)
            )
        ).first()
        if cust_row and cust_row.email:
            cust_name = (
                " ".join(filter(None, [cust_row.first_name, cust_row.last_name]))
                or "there"
            )
            await send_event_email(
                db,
                event_type="booking_requested",
                to_email=cust_row.email,
                to_name=cust_name,
                context={
                    "name": cust_name,
                    "service": "Tour",
                    "booking_number": tour.booking_number,
                    "message": (
                        "Your tour booking has been received and the tour operator is "
                        "confirming your trip. You'll get another email once it's "
                        "confirmed."
                    ),
                    "details": [
                        ("Package", package.package_name or ""),
                        (
                            "Travel start",
                            payload.travel_start_date.strftime("%d %b %Y"),
                        ),
                        ("Travel end", end_date.strftime("%d %b %Y")),
                        ("Travellers", str(payload.persons_count)),
                    ],
                    "amount": float(total),
                    "amount_label": "Tour fare",
                },
                related_type="TOUR_BOOKING",
                related_id=tour.id,
            )
    except Exception:  # pragma: no cover — email must never break bookings
        pass

    return {
        "id": tour.id,
        "booking_number": tour.booking_number,
        "master_booking_number": master.booking_number,
        "package_name": package.package_name,
        "travel_start_date": tour.travel_start_date,
        "travel_end_date": tour.travel_end_date,
        "persons_count": tour.persons_count,
        "total_amount": float(total),
        "booking_status": tour.booking_status,
        "payment_status": tour.payment_status,
    }


@care_router.get("/tour-booking/{customer_id}/meta")
async def care_tour_meta(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    packages = await _list_packages(db, statuses=["ACTIVE"])
    customer = await db.scalar(select(Customer).where(Customer.id == customer_id))
    if not customer:
        raise HTTPException(404, "Customer not found")
    return {
        "customer": {
            "id": customer.id,
            "name": " ".join(filter(None, [customer.first_name, customer.last_name])),
        },
        "packages": packages,
    }


@care_router.post(
    "/tour-booking/{customer_id}/create", status_code=status.HTTP_201_CREATED
)
async def care_create_tour(
    customer_id: int,
    payload: BookingIn,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(*ADMIN_ROLES)),
):
    customer = await db.scalar(select(Customer).where(Customer.id == customer_id))
    if not customer:
        raise HTTPException(404, "Customer not found")
    package = await db.scalar(
        select(TourPackage).where(
            TourPackage.id == payload.package_id, TourPackage.status == "ACTIVE"
        )
    )
    if not package:
        raise HTTPException(404, "Active tour package not found")
    total, commission, payout, _ = await _price_for(db, package, payload.persons_count)
    end_date = payload.travel_start_date + timedelta(days=package.duration_days - 1)
    master = MasterBooking(
        booking_number=await next_master_booking_number(db),
        customer_id=customer_id,
        city_id=package.city_id,
        booking_status="CONFIRMED",
        payment_status="PENDING",
        total_amount=total,
        journey_start_date=payload.travel_start_date,
        journey_end_date=end_date,
        remarks=payload.special_requests,
    )
    db.add(master)
    await db.flush()
    tour = TourBooking(
        master_booking_id=master.id,
        package_id=package.id,
        booking_number=f"TO-{datetime.now(timezone.utc):%Y%m%d}-{str(master.id).zfill(5)}",
        travel_start_date=payload.travel_start_date,
        travel_end_date=end_date,
        persons_count=payload.persons_count,
        total_amount=total,
        platform_commission=commission,
        partner_payout=payout,
        booking_status="CONFIRMED",
        special_requests=payload.special_requests,
    )
    db.add(tour)
    await db.flush()
    db.add(
        BookingService(
            master_booking_id=master.id,
            service_type="TOUR",
            service_reference_id=tour.id,
            service_status=tour.booking_status,
            service_amount=total,
        )
    )
    for p in payload.participants:
        db.add(TourParticipant(tour_booking_id=tour.id, **p.model_dump()))
    return {
        "id": tour.id,
        "booking_number": tour.booking_number,
        "master_booking_number": master.booking_number,
        "total_amount": float(total),
        "booking_status": tour.booking_status,
    }
