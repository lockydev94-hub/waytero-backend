# ============================================================
# WAYTERO — ADMIN OFFERS API
# File: app/modules/admin/offer_api.py
# Doc Ref: BRD Part 6 §129; migration 0077_offers
# Mounted at /admin/offers (require_roles SUPER_ADMIN/ADMIN via router.py)
# ============================================================

import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.admin.coupon_models import Coupon
from app.modules.admin.offer_models import (
    Offer,
    OfferHotelLink,
    OfferHotelRoomType,
    OfferTourPackage,
)
from app.modules.admin import offer_service

router = APIRouter()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:150] or "offer"


class LinkHotelIn(BaseModel):
    hotel_id: int = Field(gt=0)
    room_category_ids: list[int] = Field(min_length=1)


class OfferCreate(BaseModel):
    coupon_id: int = Field(gt=0)
    service_type: str
    vehicle_category_id: int | None = Field(None, gt=0)
    title: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(None, max_length=160)
    short_description: str | None = None
    terms_and_conditions: str = ""
    image_url: str = Field(min_length=1)
    badge_label: str | None = Field(None, max_length=60)
    valid_from: date
    valid_to: date
    is_published: bool = False
    sort_order: int = Field(0, ge=-32768, le=32767)
    # Inline targets so the admin editor can create offer+links in one call.
    hotels: list[LinkHotelIn] = Field(default_factory=list)
    tour_package_ids: list[int] = Field(default_factory=list)


class OfferUpdate(BaseModel):
    coupon_id: int | None = Field(None, gt=0)
    service_type: str | None = None
    vehicle_category_id: int | None = Field(None, gt=0)
    title: str | None = Field(None, min_length=1, max_length=255)
    slug: str | None = Field(None, max_length=160)
    short_description: str | None = None
    terms_and_conditions: str | None = None
    image_url: str | None = Field(None, min_length=1)
    badge_label: str | None = Field(None, max_length=60)
    valid_from: date | None = None
    valid_to: date | None = None
    is_published: bool | None = None
    sort_order: int | None = Field(None, ge=-32768, le=32767)


class SetRoomTypesIn(BaseModel):
    room_category_ids: list[int] = Field(min_length=1)


class LinkTourIn(BaseModel):
    tour_package_id: int = Field(gt=0)


class ResolveExitIn(BaseModel):
    approve: bool


async def _load_offer(db: AsyncSession, offer_id: int, *, lock=False) -> Offer:
    q = (
        select(Offer)
        .options(
            selectinload(Offer.hotel_links),
            selectinload(Offer.tour_links),
            selectinload(Offer.room_type_rows),
        )
        .where(Offer.id == offer_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        q = q.with_for_update()
    offer = (await db.execute(q)).scalar_one_or_none()
    if offer is None:
        raise ResourceNotFoundException("Offer", offer_id)
    return offer


async def _validate_shape(payload_service_type, payload_vehicle_category_id):
    if payload_service_type not in offer_service.SERVICE_TYPES:
        raise ValidationException("service_type must be CAB, HOTEL or TOUR")
    if payload_service_type == "CAB" and payload_vehicle_category_id is None:
        raise ValidationException("A CAB offer needs a vehicle category")
    if payload_service_type != "CAB" and payload_vehicle_category_id is not None:
        raise ValidationException("Only CAB offers carry a vehicle category")


async def _unique_slug(db: AsyncSession, base: str, exclude_id: int | None) -> str:
    slug = _slugify(base)
    candidate = slug
    n = 2
    while True:
        q = select(Offer.id).where(Offer.slug == candidate)
        if exclude_id is not None:
            q = q.where(Offer.id != exclude_id)
        if not (await db.execute(q.limit(1))).first():
            return candidate
        candidate = f"{slug}-{n}"
        n += 1


async def _save_offer(db, payload, current_user, offer: Offer | None = None) -> Offer:
    st = (
        payload.service_type
        if payload.service_type is not None
        else (offer.service_type if offer else None)
    )
    vc = (
        payload.vehicle_category_id
        if "vehicle_category_id" in payload.model_fields_set
        else (offer.vehicle_category_id if offer else None)
    )
    if payload.__class__.__name__ == "OfferCreate":
        st, vc = payload.service_type, payload.vehicle_category_id
    await _validate_shape(st, vc)
    coupon_id = (
        payload.coupon_id
        if getattr(payload, "coupon_id", None)
        else (offer.coupon_id if offer else None)
    )
    coupon = await offer_service._coupon_brief(db, coupon_id)
    if coupon is None:
        raise ValidationException("Selected coupon does not exist")
    valid_from = getattr(payload, "valid_from", None) or (
        offer.valid_from if offer else None
    )
    valid_to = getattr(payload, "valid_to", None) or (offer.valid_to if offer else None)
    if valid_from and valid_to and valid_to < valid_from:
        raise ValidationException("End date cannot precede start date")
    await offer_service.window_contained(coupon, valid_from, valid_to)

    eligible, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, coupon, st, vehicle_category_id=vc, require_target=False
    )
    if not eligible:
        raise ValidationException("; ".join(reasons))
    if offer is not None and offer.coupon_id != coupon_id:
        # Coupon swap: every already-linked listing must stay eligible
        # (cohort/city/service rules are per-coupon).
        for link in offer.hotel_links:
            if link.status == "REMOVED":
                continue
            ok, rs, _ = await offer_service.evaluate_offer_eligibility(
                db, coupon, "HOTEL", hotel_id=link.hotel_id
            )
            if not ok:
                raise ValidationException(
                    f"Hotel #{link.hotel_id} is not eligible for the new coupon: {'; '.join(rs)}"
                )
        for link in offer.tour_links:
            if link.status == "REMOVED":
                continue
            ok, rs, _ = await offer_service.evaluate_offer_eligibility(
                db, coupon, "TOUR", tour_package_id=link.tour_package_id
            )
            if not ok:
                raise ValidationException(
                    f"Tour package #{link.tour_package_id} is not eligible for the new coupon: {'; '.join(rs)}"
                )

    if offer is None:
        title = payload.title
        slug = payload.slug or await _unique_slug(db, title, None)
        if payload.slug:
            slug = await _unique_slug(db, payload.slug, None)
        offer = Offer(
            slug=slug,
            coupon_id=coupon_id,
            service_type=st,
            vehicle_category_id=vc,
            title=title,
            short_description=payload.short_description,
            terms_and_conditions=payload.terms_and_conditions or "",
            image_url=payload.image_url,
            badge_label=payload.badge_label,
            valid_from=valid_from,
            valid_to=valid_to,
            is_published=payload.is_published,
            sort_order=payload.sort_order,
            created_by=str(current_user["sub"]),
        )
        db.add(offer)
        await db.flush()
        for h in payload.hotels:
            if st != "HOTEL":
                raise ValidationException("Only HOTEL offers link hotels")
            await _assert_hotel_link_eligible(
                db, offer, h.hotel_id, h.room_category_ids, coupon
            )
            db.add(
                OfferHotelLink(offer_id=offer.id, hotel_id=h.hotel_id, status="ACTIVE")
            )
            for rc in sorted(set(h.room_category_ids)):
                db.add(OfferHotelRoomType(offer_id=offer.id, room_category_id=rc))
        for tpid in payload.tour_package_ids:
            if st != "TOUR":
                raise ValidationException("Only TOUR offers link tour packages")
            ok, rs, _ = await offer_service.evaluate_offer_eligibility(
                db, coupon, "TOUR", tour_package_id=tpid
            )
            if not ok:
                raise ValidationException("; ".join(rs))
            db.add(
                OfferTourPackage(
                    offer_id=offer.id, tour_package_id=tpid, status="ACTIVE"
                )
            )
        await db.flush()
    else:
        if payload.slug and payload.slug != offer.slug:
            offer.slug = await _unique_slug(db, payload.slug, offer.id)
        if payload.title:
            offer.title = payload.title
        for field in (
            "short_description",
            "terms_and_conditions",
            "image_url",
            "badge_label",
            "sort_order",
        ):
            if field in payload.model_fields_set:
                setattr(offer, field, getattr(payload, field))
        offer.service_type = st
        offer.vehicle_category_id = vc
        offer.coupon_id = coupon_id
        offer.valid_from = valid_from
        offer.valid_to = valid_to
        if payload.is_published is not None:
            offer.is_published = payload.is_published
        await db.flush()

    if offer.is_published:
        await offer_service.assert_publishable(db, offer)
    return offer


@router.get("", tags=["Offers"])
async def list_offers(
    is_published: bool | None = None,
    service_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    where = []
    params = {}
    if is_published is not None:
        where.append("o.is_published = :pub")
        params["pub"] = is_published
    if service_type:
        where.append("o.service_type = :svc")
        params["svc"] = service_type.upper()
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT o.id, o.slug, o.title, o.service_type, o.image_url, o.badge_label,
                           o.valid_from, o.valid_to, o.is_published, o.sort_order,
                           c.coupon_code, c.coupon_type, c.is_active AS coupon_active,
                           c.discount_type, c.discount_value, c.max_discount_amount,
                           o.vehicle_category_id,
                           (SELECT COUNT(*) FROM offer_hotel_links l WHERE l.offer_id = o.id AND l.status = 'ACTIVE') AS hotel_links,
                           (SELECT COUNT(*) FROM offer_tour_packages t WHERE t.offer_id = o.id AND t.status = 'ACTIVE') AS tour_links,
                           (SELECT COUNT(*) FROM offer_hotel_links l WHERE l.offer_id = o.id AND l.status = 'EXIT_REQUESTED')
                           + (SELECT COUNT(*) FROM offer_tour_packages t WHERE t.offer_id = o.id AND t.status = 'EXIT_REQUESTED') AS exit_requests,
                           (o.valid_from <= CURRENT_DATE AND o.valid_to >= CURRENT_DATE
                            AND c.is_active AND c.valid_from <= CURRENT_DATE AND c.valid_to >= CURRENT_DATE) AS live_now
                    FROM offers o JOIN coupons c ON c.id = o.coupon_id
                    {wsql}
                    ORDER BY o.sort_order, o.id DESC
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        d = dict(r)
        d["badge"] = offer_service.derive_badge(
            d["discount_type"],
            d["discount_value"],
            d["max_discount_amount"],
            d["badge_label"],
        )
        if d["is_published"]:
            d["status"] = (
                "LIVE"
                if d["live_now"]
                else ("SCHEDULED" if d["valid_from"] > date.today() else "EXPIRED")
            )
        else:
            d["status"] = "DRAFT"
        d.pop("live_now", None)
        out.append(d)
    return out


@router.post("", status_code=201, tags=["Offers"])
async def create_offer(
    payload: OfferCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    offer = await _save_offer(db, payload, current_user)
    return await get_offer(offer.id, db)


@router.get("/eligible-coupons", tags=["Offers"])
async def eligible_coupons(
    service_type: str = Query(...),
    vehicle_category_id: int | None = Query(None, gt=0),
    hotel_id: int | None = Query(None, gt=0),
    tour_package_id: int | None = Query(None, gt=0),
    db: AsyncSession = Depends(get_db),
):
    st = service_type.upper()
    if st not in offer_service.SERVICE_TYPES:
        raise ValidationException("service_type must be CAB, HOTEL or TOUR")
    coupons = (
        (
            await db.execute(
                select(Coupon)
                .where(Coupon.is_active.is_(True))
                .order_by(Coupon.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    items = []
    for coupon in coupons:
        eligible, reasons, partner = await offer_service.evaluate_offer_eligibility(
            db,
            coupon,
            st,
            vehicle_category_id=vehicle_category_id,
            hotel_id=hotel_id,
            tour_package_id=tour_package_id,
        )
        items.append(
            {
                "coupon_id": coupon.id,
                "coupon_code": coupon.coupon_code,
                "title": coupon.title,
                "coupon_type": coupon.coupon_type,
                "valid_from": coupon.valid_from,
                "valid_to": coupon.valid_to,
                "eligible": eligible,
                "reasons": reasons,
                "partner": partner,
            }
        )
    return {"service_type": st, "items": items}


@router.get("/{offer_id}", tags=["Offers"])
async def get_offer(offer_id: int, db: AsyncSession = Depends(get_db)):
    offer = await _load_offer(db, offer_id)
    coupon = await offer_service._coupon_brief(db, offer.coupon_id)
    hotels = (
        (
            await db.execute(
                text(
                    """
                    SELECT l.id AS link_id, l.hotel_id, l.status, l.exit_requested_at, l.exit_reason,
                           h.hotel_name, h.slug, h.city_id, h.partner_id,
                           COALESCE(jsonb_agg(jsonb_build_object('id', rc.id, 'name', rc.category_name))
                                    FILTER (WHERE rc.id IS NOT NULL), '[]'::jsonb) AS room_types
                    FROM offer_hotel_links l
                    JOIN hotels h ON h.id = l.hotel_id
                    LEFT JOIN offer_hotel_room_types rt ON rt.offer_id = l.offer_id
                    LEFT JOIN hotel_room_categories rc ON rc.id = rt.room_category_id AND rc.hotel_id = l.hotel_id
                    WHERE l.offer_id = :oid
                    GROUP BY l.id, l.hotel_id, l.status, l.exit_requested_at, l.exit_reason,
                             h.hotel_name, h.slug, h.city_id, h.partner_id
                    ORDER BY l.id
                    """
                ),
                {"oid": offer_id},
            )
        )
        .mappings()
        .all()
    )
    tours = (
        (
            await db.execute(
                text(
                    """
                    SELECT t.id AS link_id, t.tour_package_id, t.status, t.exit_requested_at, t.exit_reason,
                           tp.package_name, tp.slug, tp.destination, tp.city_id, tp.partner_id
                    FROM offer_tour_packages t JOIN tour_packages tp ON tp.id = t.tour_package_id
                    WHERE t.offer_id = :oid ORDER BY t.id
                    """
                ),
                {"oid": offer_id},
            )
        )
        .mappings()
        .all()
    )
    vehicle = None
    if offer.vehicle_category_id:
        vehicle = (
            (
                await db.execute(
                    text(
                        "SELECT id, category_name AS name FROM vehicle_categories WHERE id = :v"
                    ),
                    {"v": offer.vehicle_category_id},
                )
            )
            .mappings()
            .first()
        )
    return {
        "id": offer.id,
        "slug": offer.slug,
        "coupon_id": offer.coupon_id,
        "coupon_code": coupon.coupon_code if coupon else None,
        "coupon_type": coupon.coupon_type if coupon else None,
        "discount_type": coupon.discount_type if coupon else None,
        "discount_value": coupon.discount_value if coupon else None,
        "max_discount_amount": coupon.max_discount_amount if coupon else None,
        "min_booking_amount": coupon.min_booking_amount if coupon else None,
        "service_type": offer.service_type,
        "vehicle_category_id": offer.vehicle_category_id,
        "vehicle_category_name": vehicle["name"] if vehicle else None,
        "title": offer.title,
        "short_description": offer.short_description,
        "terms_and_conditions": offer.terms_and_conditions,
        "image_url": offer.image_url,
        "badge_label": offer.badge_label,
        "badge": (
            offer_service.derive_badge(
                coupon.discount_type,
                coupon.discount_value,
                coupon.max_discount_amount,
                offer.badge_label,
            )
            if coupon
            else offer.badge_label
        ),
        "valid_from": offer.valid_from,
        "valid_to": offer.valid_to,
        "is_published": offer.is_published,
        "sort_order": offer.sort_order,
        "is_live": await offer_service.offer_is_live(db, offer, coupon),
        "hotels": [dict(h) for h in hotels],
        "tour_packages": [dict(t) for t in tours],
    }


@router.put("/{offer_id}", tags=["Offers"])
async def update_offer(
    offer_id: int,
    payload: OfferUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    offer = await _load_offer(db, offer_id, lock=True)
    # Guard: cannot change service type out from under existing links.
    if payload.service_type and payload.service_type.upper() != offer.service_type:
        if (
            offer.hotel_links
            or offer.tour_links
            or (payload.vehicle_category_id is not None and offer.service_type != "CAB")
        ):
            raise ValidationException(
                "Delete the linked listings before changing the offer's service type"
            )
    payload.service_type = (
        payload.service_type.upper() if payload.service_type else None
    )
    offer = await _save_offer(db, payload, current_user, offer=offer)
    return await get_offer(offer.id, db)


@router.delete("/{offer_id}", status_code=204, tags=["Offers"])
async def delete_offer(offer_id: int, db: AsyncSession = Depends(get_db)):
    offer = await _load_offer(db, offer_id, lock=True)
    await db.delete(offer)


# ------------------------------------------------------------------ hotels


async def _assert_hotel_link_eligible(db, offer, hotel_id, room_category_ids, coupon):
    eligible, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, coupon, "HOTEL", hotel_id=hotel_id
    )
    if not eligible:
        raise ValidationException("; ".join(reasons))
    owned = (
        (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM hotel_room_categories WHERE hotel_id = :h AND id = ANY(CAST(:ids AS BIGINT[])) AND is_active"
                ),
                {"h": hotel_id, "ids": room_category_ids},
            )
        )
    ).scalar_one()
    if owned != len(set(room_category_ids)):
        raise ValidationException("One or more room types do not belong to this hotel")


@router.post("/{offer_id}/hotels", status_code=201, tags=["Offers"])
async def link_hotel(
    offer_id: int,
    payload: LinkHotelIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    offer = await _load_offer(db, offer_id, lock=True)
    if offer.service_type != "HOTEL":
        raise ValidationException("Only HOTEL offers link hotels")
    coupon = await offer_service._coupon_brief(db, offer.coupon_id)
    await _assert_hotel_link_eligible(
        db, offer, payload.hotel_id, payload.room_category_ids, coupon
    )
    existing = (
        await db.execute(
            select(OfferHotelLink).where(
                OfferHotelLink.offer_id == offer_id,
                OfferHotelLink.hotel_id == payload.hotel_id,
            )
        )
    ).scalar_one_or_none()
    if existing and existing.status != "REMOVED":
        raise ValidationException("This hotel is already linked to the offer")
    if existing:
        existing.status = "ACTIVE"
        existing.exit_requested_at = existing.exit_reason = None
        link = existing
    else:
        link = OfferHotelLink(
            offer_id=offer_id, hotel_id=payload.hotel_id, status="ACTIVE"
        )
        db.add(link)
    await db.flush()
    await db.execute(
        text(
            "DELETE FROM offer_hotel_room_types WHERE offer_id = :o AND room_category_id = ANY(CAST(:ids AS BIGINT[]))"
        ),
        {"o": offer_id, "ids": payload.room_category_ids},
    )
    for rc in sorted(set(payload.room_category_ids)):
        db.add(OfferHotelRoomType(offer_id=offer_id, room_category_id=rc))
    await db.flush()
    return await get_offer(offer_id, db)


@router.put("/{offer_id}/hotels/{hotel_id}/room-types", tags=["Offers"])
async def set_hotel_room_types(
    offer_id: int,
    hotel_id: int,
    payload: SetRoomTypesIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    offer = await _load_offer(db, offer_id, lock=True)
    link = (
        await db.execute(
            select(OfferHotelLink).where(
                OfferHotelLink.offer_id == offer_id, OfferHotelLink.hotel_id == hotel_id
            )
        )
    ).scalar_one_or_none()
    if link is None or link.status == "REMOVED":
        raise ResourceNotFoundException("Offer hotel link", hotel_id)
    coupon = await offer_service._coupon_brief(db, offer.coupon_id)
    await _assert_hotel_link_eligible(
        db, offer, hotel_id, payload.room_category_ids, coupon
    )
    current = set(
        (
            await db.execute(
                text(
                    """
                    SELECT rt.room_category_id FROM offer_hotel_room_types rt
                    JOIN hotel_room_categories rc ON rc.id = rt.room_category_id
                    WHERE rt.offer_id = :o AND rc.hotel_id = :h
                    """
                ),
                {"o": offer_id, "h": hotel_id},
            )
        )
        .scalars()
        .all()
    )
    wanted = set(payload.room_category_ids)
    for rc in current - wanted:
        await db.execute(
            text(
                "DELETE FROM offer_hotel_room_types WHERE offer_id = :o AND room_category_id = :r"
            ),
            {"o": offer_id, "r": rc},
        )
    for rc in wanted - current:
        db.add(OfferHotelRoomType(offer_id=offer_id, room_category_id=rc))
    await db.flush()
    return await get_offer(offer_id, db)


@router.delete("/{offer_id}/hotels/{hotel_id}", tags=["Offers"])
async def unlink_hotel(
    offer_id: int,
    hotel_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    await _load_offer(db, offer_id, lock=True)
    link = (
        await db.execute(
            select(OfferHotelLink).where(
                OfferHotelLink.offer_id == offer_id, OfferHotelLink.hotel_id == hotel_id
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundException("Offer hotel link", hotel_id)
    await db.delete(link)
    # drop this hotel's room types (they are scoped by hotel via room_categories)
    await db.execute(
        text(
            """
            DELETE FROM offer_hotel_room_types rt
            WHERE rt.offer_id = :o AND rt.room_category_id IN
              (SELECT id FROM hotel_room_categories WHERE hotel_id = :h)
            """
        ),
        {"o": offer_id, "h": hotel_id},
    )
    await db.flush()
    return {"success": True}


@router.put("/{offer_id}/hotels/{hotel_id}/exit", tags=["Offers"])
async def resolve_hotel_exit(
    offer_id: int,
    hotel_id: int,
    payload: ResolveExitIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    await _load_offer(db, offer_id, lock=True)
    link = (
        await db.execute(
            select(OfferHotelLink).where(
                OfferHotelLink.offer_id == offer_id, OfferHotelLink.hotel_id == hotel_id
            )
        )
    ).scalar_one_or_none()
    if link is None or link.status != "EXIT_REQUESTED":
        raise ValidationException("No pending exit request for this hotel")
    now = datetime.now(timezone.utc)
    if payload.approve:
        link.status = "REMOVED"
        link.exit_resolved_at = now
        link.exit_resolved_by = str(current_user["sub"])
        await db.execute(
            text(
                "DELETE FROM offer_hotel_room_types rt WHERE rt.offer_id = :o AND rt.room_category_id IN (SELECT id FROM hotel_room_categories WHERE hotel_id = :h)"
            ),
            {"o": offer_id, "h": hotel_id},
        )
    else:
        link.status = "ACTIVE"
        link.exit_requested_at = None
        link.exit_reason = None
    await db.flush()
    return await get_offer(offer_id, db)


# ------------------------------------------------------------------ tours


@router.post("/{offer_id}/tours", status_code=201, tags=["Offers"])
async def link_tour(
    offer_id: int,
    payload: LinkTourIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    offer = await _load_offer(db, offer_id, lock=True)
    if offer.service_type != "TOUR":
        raise ValidationException("Only TOUR offers link tour packages")
    coupon = await offer_service._coupon_brief(db, offer.coupon_id)
    eligible, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, coupon, "TOUR", tour_package_id=payload.tour_package_id
    )
    if not eligible:
        raise ValidationException("; ".join(reasons))
    existing = (
        await db.execute(
            select(OfferTourPackage).where(
                OfferTourPackage.offer_id == offer_id,
                OfferTourPackage.tour_package_id == payload.tour_package_id,
            )
        )
    ).scalar_one_or_none()
    if existing and existing.status != "REMOVED":
        raise ValidationException("This tour package is already linked")
    if existing:
        existing.status = "ACTIVE"
        existing.exit_requested_at = existing.exit_reason = None
    else:
        db.add(
            OfferTourPackage(
                offer_id=offer_id,
                tour_package_id=payload.tour_package_id,
                status="ACTIVE",
            )
        )
    await db.flush()
    return await get_offer(offer_id, db)


@router.delete("/{offer_id}/tours/{tour_package_id}", tags=["Offers"])
async def unlink_tour(
    offer_id: int,
    tour_package_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    await _load_offer(db, offer_id, lock=True)
    link = (
        await db.execute(
            select(OfferTourPackage).where(
                OfferTourPackage.offer_id == offer_id,
                OfferTourPackage.tour_package_id == tour_package_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise ResourceNotFoundException("Offer tour link", tour_package_id)
    await db.delete(link)
    await db.flush()
    return {"success": True}


@router.put("/{offer_id}/tours/{tour_package_id}/exit", tags=["Offers"])
async def resolve_tour_exit(
    offer_id: int,
    tour_package_id: int,
    payload: ResolveExitIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    await _load_offer(db, offer_id, lock=True)
    link = (
        await db.execute(
            select(OfferTourPackage).where(
                OfferTourPackage.offer_id == offer_id,
                OfferTourPackage.tour_package_id == tour_package_id,
            )
        )
    ).scalar_one_or_none()
    if link is None or link.status != "EXIT_REQUESTED":
        raise ValidationException("No pending exit request for this tour package")
    if payload.approve:
        link.status = "REMOVED"
        link.exit_resolved_at = datetime.now(timezone.utc)
        link.exit_resolved_by = str(current_user["sub"])
    else:
        link.status = "ACTIVE"
        link.exit_requested_at = None
        link.exit_reason = None
    await db.flush()
    return await get_offer(offer_id, db)
