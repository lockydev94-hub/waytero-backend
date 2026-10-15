# ============================================================
# WAYTERO — OFFER SERVICE (shared validation core)
# File: app/modules/admin/offer_service.py
# Doc Ref: BRD Part 6 §129; migration 0077_offers
#
# One validator used by three callers so the admin editor, the save-time
# guard and the booking-time coupon scope can never disagree:
#   * admin create/update offer
#   * GET /admin/offers/eligible-coupons (live editor panel)
#   * validate_coupon offer-scope check (Phase 2)
# Offers never compute a discount — the coupon engine owns all money.
# ============================================================

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.modules.admin.coupon_models import Coupon
from app.modules.admin.offer_models import (
    Offer,
    OfferHotelLink,
    OfferTourPackage,
)

SERVICE_TYPES = {"CAB", "HOTEL", "TOUR"}
PARTNER_FUNDED_TYPES = {"PARTNER_ONLY", "PLATFORM_PARTNER"}


def derive_badge(
    discount_type: str,
    discount_value: Decimal,
    cap: Decimal | None,
    override: str | None,
) -> str:
    """Marketing label from the coupon's real terms; override wins if set."""
    if override:
        return override
    value = (
        int(discount_value)
        if discount_value == discount_value.to_integral_value()
        else discount_value
    )
    if discount_type == "FLAT":
        return f"FLAT ₹{value} OFF"
    label = f"UPTO {value}% OFF"
    if cap is not None:
        capv = int(cap) if cap == cap.to_integral_value() else cap
        label += f" (₹{capv} max)"
    return label


async def _coupon_brief(db: AsyncSession, coupon_id: int) -> Coupon | None:
    return (
        await db.execute(select(Coupon).where(Coupon.id == coupon_id))
    ).scalar_one_or_none()


async def _listing_partner(
    db: AsyncSession, service_type: str, target_id: int
) -> dict | None:
    """Resolve partner/city/status for a hotel or tour package server-side
    (anti-forgery: the client never supplies partner_id)."""
    if service_type == "HOTEL":
        row = (
            (
                await db.execute(
                    text(
                        """
                    SELECT h.partner_id, h.city_id, h.status,
                           EXISTS (SELECT 1 FROM partners p JOIN partner_services ps
                                 ON ps.partner_id = p.id
                                 WHERE p.id = h.partner_id AND ps.service_type = 'HOTEL'
                                   AND ps.is_active AND p.status IN ('APPROVED','ACTIVE')) AS granted
                    FROM hotels h WHERE h.id = :tid AND h.deleted_at IS NULL
                    """
                    ),
                    {"tid": target_id},
                )
            )
            .mappings()
            .first()
        )
    elif service_type == "TOUR":
        row = (
            (
                await db.execute(
                    text(
                        """
                    SELECT tp.partner_id, tp.city_id, tp.status,
                           EXISTS (SELECT 1 FROM partners p JOIN partner_services ps
                                 ON ps.partner_id = p.id
                                 WHERE p.id = tp.partner_id AND ps.service_type = 'TOUR'
                                   AND ps.is_active AND p.status IN ('APPROVED','ACTIVE')) AS granted
                    FROM tour_packages tp WHERE tp.id = :tid
                    """
                    ),
                    {"tid": target_id},
                )
            )
            .mappings()
            .first()
        )
    else:
        return None
    return dict(row) if row else None


async def _in_cohort(db: AsyncSession, coupon_id: int, partner_id: int) -> bool:
    return bool(
        (
            await db.execute(
                text(
                    """
                    SELECT 1 FROM coupon_partner_rules
                    WHERE coupon_id = :cid AND partner_id = :pid AND is_active
                    """
                ),
                {"cid": coupon_id, "pid": partner_id},
            )
        ).first()
    )


async def _city_allowed(db: AsyncSession, coupon: Coupon, city_id: int | None) -> bool:
    if not coupon.is_city_specific or city_id is None:
        return True
    return bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM coupon_city_rules WHERE coupon_id = :cid AND city_id = :city"
                ),
                {"cid": coupon.id, "city": city_id},
            )
        ).first()
    )


async def coupon_service_rules_cover(
    db: AsyncSession, coupon_id: int, service_type: str, vehicle_category_id: int | None
) -> bool:
    """No rules = coupon covers all services. Otherwise a matching rule with a
    NULL (any) or equal vehicle category is required for CAB."""
    rows = (
        (
            await db.execute(
                text(
                    "SELECT service_type, vehicle_category_id FROM coupon_service_rules WHERE coupon_id = :cid"
                ),
                {"cid": coupon_id},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return True
    for r in rows:
        if r["service_type"] != service_type:
            continue
        if (
            service_type != "CAB"
            or r["vehicle_category_id"] is None
            or r["vehicle_category_id"] == vehicle_category_id
        ):
            return True
    return False


async def evaluate_offer_eligibility(
    db: AsyncSession,
    coupon: Coupon,
    service_type: str,
    *,
    vehicle_category_id: int | None = None,
    hotel_id: int | None = None,
    tour_package_id: int | None = None,
    require_target: bool = True,
) -> tuple[bool, list[str], dict | None]:
    """Return (eligible, reasons, partner_info). partner_info is
    {partner_id, in_cohort, granted} for HOTEL/TOUR, else None.
    require_target=False is used when saving the offer itself: HOTEL/TOUR
    targets are validated per-link, not on the master row."""
    reasons: list[str] = []
    if not coupon.is_active:
        reasons.append("Coupon is inactive")
    partner_info = None
    if service_type == "CAB":
        if vehicle_category_id is None:
            reasons.append("Select a cab category")
        elif not await coupon_service_rules_cover(
            db, coupon.id, "CAB", vehicle_category_id
        ):
            reasons.append("Coupon is not valid for the selected cab category")
    elif service_type in ("HOTEL", "TOUR"):
        target_id = hotel_id if service_type == "HOTEL" else tour_package_id
        if target_id is None:
            if require_target:
                reasons.append(
                    f"Select a {'hotel' if service_type == 'HOTEL' else 'tour package'}"
                )
        else:
            listing = await _listing_partner(db, service_type, target_id)
            if listing is None:
                reasons.append("That listing no longer exists")
            else:
                if listing["status"] != "ACTIVE":
                    reasons.append("That listing is not currently active")
                if not listing["granted"]:
                    reasons.append(
                        "The listing's partner is not approved for this service"
                    )
                partner_info = {
                    "partner_id": listing["partner_id"],
                    "granted": listing["granted"],
                    "in_cohort": True,
                }
                if coupon.coupon_type in PARTNER_FUNDED_TYPES:
                    in_cohort = await _in_cohort(db, coupon.id, listing["partner_id"])
                    partner_info["in_cohort"] = in_cohort
                    if not in_cohort:
                        reasons.append(
                            "The listing's partner has not joined this coupon"
                        )
                if not await _city_allowed(db, coupon, listing["city_id"]):
                    reasons.append(
                        "The listing is outside the coupon's eligible cities"
                    )
    if coupon.apply_to not in ("ALL", service_type):
        reasons.insert(0, "Coupon does not apply to this service")
    return (not reasons, reasons, partner_info)


async def offer_is_live(db: AsyncSession, offer: Offer, coupon: Coupon | None) -> bool:
    """Read-time display gate for a single offer (window + coupon truth)."""
    if not offer.is_published:
        return False
    today = date.today()
    if not offer.valid_from <= today <= offer.valid_to:
        return False
    if coupon is None or not coupon.is_active:
        return False
    return coupon.valid_from <= today <= coupon.valid_to


async def booking_scope_block(
    db: AsyncSession,
    coupon_id: int,
    service_type: str,
    *,
    vehicle_category_id: int | None = None,
    hotel_id: int | None = None,
    room_category_id: int | None = None,
    tour_package_id: int | None = None,
) -> str | None:
    """If the coupon has any LIVE offer for this service, the booking target
    must be covered by one of them; returns an error message or None. This
    closes the manual-code-entry bypass for hotel/tour room-type scoping."""
    live_offers = (
        (
            await db.execute(
                text(
                    """
                    SELECT o.id FROM offers o JOIN coupons c ON c.id = o.coupon_id
                    WHERE o.coupon_id = :cid AND o.service_type = :svc AND o.is_published
                      AND o.valid_from <= CURRENT_DATE AND o.valid_to >= CURRENT_DATE
                      AND c.is_active AND c.valid_from <= CURRENT_DATE AND c.valid_to >= CURRENT_DATE
                    """
                ),
                {"cid": coupon_id, "svc": service_type},
            )
        )
        .scalars()
        .all()
    )
    if not live_offers:
        return None
    ids = list(live_offers)
    if service_type == "CAB":
        covered = (
            await db.execute(
                text(
                    "SELECT 1 FROM offers WHERE id = ANY(CAST(:ids AS BIGINT[])) AND vehicle_category_id = :vc LIMIT 1"
                ),
                {"ids": ids, "vc": vehicle_category_id},
            )
        ).first()
    elif service_type == "HOTEL":
        covered = (
            await db.execute(
                text(
                    """
                    SELECT 1 FROM offer_hotel_links l JOIN offer_hotel_room_types rt
                      ON rt.offer_id = l.offer_id AND rt.room_category_id = :rc
                    WHERE l.offer_id = ANY(CAST(:ids AS BIGINT[])) AND l.hotel_id = :hid
                      AND l.status = 'ACTIVE' LIMIT 1
                    """
                ),
                {"ids": ids, "hid": hotel_id, "rc": room_category_id},
            )
        ).first()
    else:  # TOUR
        covered = (
            await db.execute(
                text(
                    "SELECT 1 FROM offer_tour_packages WHERE offer_id = ANY(CAST(:ids AS BIGINT[])) AND tour_package_id = :tp AND status = 'ACTIVE' LIMIT 1"
                ),
                {"ids": ids, "tp": tour_package_id},
            )
        ).first()
    if not covered:
        return "This coupon is valid only for the listings/rooms included in the offer"
    return None


async def resolve_offer_for_booking(
    db: AsyncSession,
    *,
    coupon_id: int | None,
    service_type: str,
    vehicle_category_id: int | None,
    hotel_id: int | None,
    room_category_id: int | None,
    tour_package_id: int | None,
) -> int | None:
    """Best-effort attribution: pick a live offer whose target matches the
    booking. Never raises — a NULL result just means no badge."""
    if coupon_id is None:
        return None
    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT o.id, o.vehicle_category_id FROM offers o JOIN coupons c ON c.id = o.coupon_id
                    WHERE o.coupon_id = :cid AND o.service_type = :svc AND o.is_published
                      AND o.valid_from <= CURRENT_DATE AND o.valid_to >= CURRENT_DATE
                      AND c.is_active AND c.valid_from <= CURRENT_DATE AND c.valid_to >= CURRENT_DATE
                    """
                ),
                {"cid": coupon_id, "svc": service_type},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    if service_type == "CAB":
        match = next(
            (r for r in rows if r["vehicle_category_id"] == vehicle_category_id), None
        )
        return match["id"] if match else None
    ids = [r["id"] for r in rows]
    if service_type == "HOTEL":
        return (
            await db.execute(
                text(
                    """
                    SELECT l.offer_id FROM offer_hotel_links l JOIN offer_hotel_room_types rt
                      ON rt.offer_id = l.offer_id AND rt.room_category_id = :rc
                    WHERE l.offer_id = ANY(CAST(:ids AS BIGINT[])) AND l.hotel_id = :hid
                      AND l.status = 'ACTIVE' ORDER BY l.offer_id LIMIT 1
                    """
                ),
                {"ids": ids, "hid": hotel_id, "rc": room_category_id},
            )
        ).scalar()
    return (
        await db.execute(
            text(
                "SELECT offer_id FROM offer_tour_packages WHERE offer_id = ANY(CAST(:ids AS BIGINT[])) AND tour_package_id = :tp AND status = 'ACTIVE' ORDER BY offer_id LIMIT 1"
            ),
            {"ids": ids, "tp": tour_package_id},
        )
    ).scalar()


async def assert_publishable(db: AsyncSession, offer: Offer) -> None:
    """Publish requires T&C and at least one live target link for HOTEL/TOUR."""
    if not (offer.terms_and_conditions or "").strip():
        raise ValidationException("Add terms and conditions before publishing")
    if offer.service_type == "HOTEL":
        active = (
            await db.execute(
                select(OfferHotelLink.id)
                .where(
                    OfferHotelLink.offer_id == offer.id,
                    OfferHotelLink.status == "ACTIVE",
                )
                .limit(1)
            )
        ).first()
        if not active:
            raise ValidationException("Link at least one hotel before publishing")
    elif offer.service_type == "TOUR":
        active = (
            await db.execute(
                select(OfferTourPackage.id)
                .where(
                    OfferTourPackage.offer_id == offer.id,
                    OfferTourPackage.status == "ACTIVE",
                )
                .limit(1)
            )
        ).first()
        if not active:
            raise ValidationException(
                "Link at least one tour package before publishing"
            )


async def window_contained(coupon: Coupon, valid_from: date, valid_to: date) -> None:
    if not (coupon.valid_from <= valid_from and valid_to <= coupon.valid_to):
        raise ValidationException(
            "Offer dates must fall inside the coupon's validity window"
        )
