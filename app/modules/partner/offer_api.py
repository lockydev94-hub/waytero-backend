# ============================================================
# WAYTERO — PARTNER OFFERS API (My Offers)
# File: app/modules/partner/offer_api.py
# Mounted at /partners/offers (require_partner via router.py)
# Doc Ref: migration 0077_offers; partners see offers covering their
# listings, adjust room types (>=1) and request to exit (admin approves).
# ============================================================

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_partner
from app.modules.admin.offer_models import (
    OfferHotelLink,
    OfferHotelRoomType,
    OfferTourPackage,
)
from app.modules.partner.models import Partner

router = APIRouter()


async def _partner_id(db: AsyncSession, user_uuid: str) -> int:
    p = (
        await db.execute(select(Partner).where(Partner.user_id == UUID(user_uuid)))
    ).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Partner profile not found.")
    return p.id


class SetRoomTypesIn(BaseModel):
    room_category_ids: list[int] = Field(min_length=1)


class ExitRequestIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


@router.get("", tags=["Partner Offers"])
async def list_my_offers(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_partner()),
):
    pid = await _partner_id(db, str(current_user["sub"]))

    hotel_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT o.id AS offer_id, o.slug, o.title, o.service_type, o.image_url,
                           o.valid_from, o.valid_to, o.is_published, co.coupon_code,
                           l.hotel_id AS link_id, h.hotel_name AS link_name,
                           l.status, l.exit_requested_at, l.exit_reason
                    FROM offer_hotel_links l
                    JOIN offers o ON o.id = l.offer_id
                    JOIN hotels h ON h.id = l.hotel_id
                    JOIN coupons co ON co.id = o.coupon_id
                    WHERE h.partner_id = :pid AND l.status <> 'REMOVED'
                    ORDER BY o.valid_to DESC, o.id
                    """
                ),
                {"pid": pid},
            )
        )
        .mappings()
        .all()
    )
    tour_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT o.id AS offer_id, o.slug, o.title, o.service_type, o.image_url,
                           o.valid_from, o.valid_to, o.is_published, co.coupon_code,
                           l.tour_package_id AS link_id, tp.package_name AS link_name,
                           l.status, l.exit_requested_at, l.exit_reason
                    FROM offer_tour_packages l
                    JOIN offers o ON o.id = l.offer_id
                    JOIN tour_packages tp ON tp.id = l.tour_package_id
                    JOIN coupons co ON co.id = o.coupon_id
                    WHERE tp.partner_id = :pid AND l.status <> 'REMOVED'
                    ORDER BY o.valid_to DESC, o.id
                    """
                ),
                {"pid": pid},
            )
        )
        .mappings()
        .all()
    )

    by_offer: dict[int, dict] = {}
    hotel_link_ids: list[tuple[int, int]] = []  # (offer_id, hotel_id)
    for kind, rows in (("hotel", hotel_rows), ("tour", tour_rows)):
        for row in rows:
            offer = by_offer.get(row["offer_id"])
            if offer is None:
                offer = {
                    "offer_id": row["offer_id"],
                    "slug": row["slug"],
                    "title": row["title"],
                    "service_type": row["service_type"],
                    "image_url": row["image_url"],
                    "valid_from": row["valid_from"].isoformat(),
                    "valid_to": row["valid_to"].isoformat(),
                    "is_published": row["is_published"],
                    "coupon_code": row["coupon_code"],
                    "links": [],
                }
                by_offer[row["offer_id"]] = offer
            link = {
                "kind": kind,
                "id": row["link_id"],
                "name": row["link_name"],
                "status": row["status"],
                "exit_requested_at": (
                    row["exit_requested_at"].isoformat()
                    if row["exit_requested_at"]
                    else None
                ),
                "exit_reason": row["exit_reason"],
            }
            if kind == "hotel":
                link["room_types"] = []
                hotel_link_ids.append((row["offer_id"], row["link_id"]))
            offer["links"].append(link)

    # One query per linked hotel is fine at this scale (a partner has few listings).
    for oid, hid in hotel_link_ids:
        cats = (
            (
                await db.execute(
                    text(
                        """
                        SELECT rc.id, rc.category_name,
                               (rt.room_category_id IS NOT NULL) AS included
                        FROM hotel_room_categories rc
                        LEFT JOIN offer_hotel_room_types rt
                             ON rt.room_category_id = rc.id AND rt.offer_id = :o
                        WHERE rc.hotel_id = :h AND rc.is_active
                        ORDER BY rc.id
                        """
                    ),
                    {"o": oid, "h": hid},
                )
            )
            .mappings()
            .all()
        )
        for offer in by_offer.values():
            for link in offer["links"]:
                if (
                    offer["offer_id"] == oid
                    and link["kind"] == "hotel"
                    and link["id"] == hid
                ):
                    link["room_types"] = [
                        {
                            "id": c["id"],
                            "name": c["category_name"],
                            "included": c["included"],
                        }
                        for c in cats
                    ]

    return list(by_offer.values())


async def _get_own_hotel_link(
    db: AsyncSession, offer_id: int, hotel_id: int, pid: int
) -> OfferHotelLink:
    link = (
        await db.execute(
            select(OfferHotelLink).where(
                OfferHotelLink.offer_id == offer_id,
                OfferHotelLink.hotel_id == hotel_id,
            )
        )
    ).scalar_one_or_none()
    if link is None or link.status == "REMOVED":
        raise HTTPException(404, "Offer does not include one of your hotels.")
    owned = (
        await db.execute(
            text("SELECT 1 FROM hotels WHERE id = :h AND partner_id = :p"),
            {"h": hotel_id, "p": pid},
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(403, "This hotel does not belong to your partner account.")
    return link


@router.put("/{offer_id}/hotels/{hotel_id}/room-types", tags=["Partner Offers"])
async def set_hotel_room_types(
    offer_id: int,
    hotel_id: int,
    payload: SetRoomTypesIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_partner()),
):
    pid = await _partner_id(db, str(current_user["sub"]))
    link = await _get_own_hotel_link(db, offer_id, hotel_id, pid)
    wanted = set(payload.room_category_ids)
    owned = (
        (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM hotel_room_categories WHERE hotel_id = :h AND id = ANY(CAST(:ids AS BIGINT[])) AND is_active"
                ),
                {"h": hotel_id, "ids": list(wanted)},
            )
        )
    ).scalar_one()
    if owned != len(wanted):
        raise HTTPException(400, "One or more room types do not belong to this hotel")
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
    return {"success": True, "room_category_ids": sorted(wanted), "status": link.status}


@router.post("/{offer_id}/hotels/{hotel_id}/exit-request", tags=["Partner Offers"])
async def request_hotel_exit(
    offer_id: int,
    hotel_id: int,
    payload: ExitRequestIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_partner()),
):
    pid = await _partner_id(db, str(current_user["sub"]))
    link = await _get_own_hotel_link(db, offer_id, hotel_id, pid)
    if link.status == "EXIT_REQUESTED":
        raise HTTPException(400, "Exit is already requested — awaiting admin approval.")
    link.status = "EXIT_REQUESTED"
    link.exit_requested_at = datetime.now(timezone.utc)
    link.exit_reason = payload.reason
    await db.flush()
    return {"success": True, "status": link.status}


async def _get_own_tour_link(
    db: AsyncSession, offer_id: int, tour_package_id: int, pid: int
) -> OfferTourPackage:
    link = (
        await db.execute(
            select(OfferTourPackage).where(
                OfferTourPackage.offer_id == offer_id,
                OfferTourPackage.tour_package_id == tour_package_id,
            )
        )
    ).scalar_one_or_none()
    if link is None or link.status == "REMOVED":
        raise HTTPException(404, "Offer does not include one of your tour packages.")
    owned = (
        await db.execute(
            text("SELECT 1 FROM tour_packages WHERE id = :t AND partner_id = :p"),
            {"t": tour_package_id, "p": pid},
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(
            403, "This tour package does not belong to your partner account."
        )
    return link


@router.post(
    "/{offer_id}/tours/{tour_package_id}/exit-request", tags=["Partner Offers"]
)
async def request_tour_exit(
    offer_id: int,
    tour_package_id: int,
    payload: ExitRequestIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_partner()),
):
    pid = await _partner_id(db, str(current_user["sub"]))
    link = await _get_own_tour_link(db, offer_id, tour_package_id, pid)
    if link.status == "EXIT_REQUESTED":
        raise HTTPException(400, "Exit is already requested — awaiting admin approval.")
    link.status = "EXIT_REQUESTED"
    link.exit_requested_at = datetime.now(timezone.utc)
    link.exit_reason = payload.reason
    await db.flush()
    return {"success": True, "status": link.status}
