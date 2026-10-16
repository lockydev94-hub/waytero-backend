# ============================================================
# WAYTERO — PUBLIC OFFERS API (no auth)
# File: app/modules/admin/public_offer_api.py
# Doc Ref: migration 0077_offers
#
#   GET /public/offers          → hero slider (live, gated)
#   GET /public/offers/{slug}   → offer detail page (200 even if paused)
#
# Never exposes partner internals, cohorts or funding shares — only the
# customer-facing discount truth already public on listings.
# ============================================================

from datetime import date
import re

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ResourceNotFoundException
from app.modules.admin import offer_service

router = APIRouter()

# A Cloudinary transform segment is a comma list of `key_value` pairs; a
# version segment (`v1785961951`) or a public_id never matches it.
_CL_TRANSFORM_SEGMENT = re.compile(r"^(?:[a-z]{1,5}_[^/,]+)(?:,[a-z]{1,5}_[^/,]+)*$")
_CL_PREFIX = "https://res.cloudinary.com/"
_CIRCLE_TRANSFORM = "w_512,h_512,c_fill,g_auto,f_webp"


def _circle_thumb(url: str | None) -> str | None:
    """Force an offer image to the square crop the hero circle renders.

    offers.image_url stores a full Cloudinary secure_url, so an asset chosen
    from the gallery keeps its native aspect ratio and the circle slices it.
    Injecting c_fill,g_auto here fixes every consumer at once. Anything that
    is not a plain Cloudinary upload URL, or already carries a transform, is
    returned untouched.
    """
    if not url or not url.startswith(_CL_PREFIX):
        return url
    parts = url[len(_CL_PREFIX):].split("/")
    # cloud / resource_type / upload / <transform?> / v<version> / public_id…
    if len(parts) < 4 or parts[2] != "upload":
        return url
    tail = parts[3:]
    if tail and _CL_TRANSFORM_SEGMENT.match(tail[0]):
        return url
    return _CL_PREFIX + "/".join(parts[:3] + [_CIRCLE_TRANSFORM] + tail)

_FUNDING_LABEL = {
    "PLATFORM": "Platform offer",
    "PARTNER_ONLY": "Partner offer",
    "PLATFORM_PARTNER": "Partner offer",
}

_LIVE_WHERE = """
    o.is_published AND o.valid_from <= CURRENT_DATE AND o.valid_to >= CURRENT_DATE
    AND c.is_active AND c.valid_from <= CURRENT_DATE AND c.valid_to >= CURRENT_DATE
"""


def _live_sql() -> str:
    # Coupon must be active and today inside both windows.
    return _LIVE_WHERE


@router.get("/offers", tags=["Public Offers"])
async def list_live_offers(db: AsyncSession = Depends(get_db)):
    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT o.slug, o.title, o.service_type, o.image_url, o.badge_label,
                           o.valid_to, c.coupon_code, c.discount_type, c.discount_value,
                           c.max_discount_amount
                    FROM offers o JOIN coupons c ON c.id = o.coupon_id
                    WHERE { _live_sql() }
                      AND (o.service_type = 'CAB' OR EXISTS (
                            SELECT 1 FROM offer_hotel_links l WHERE l.offer_id = o.id AND l.status = 'ACTIVE'
                            UNION ALL
                            SELECT 1 FROM offer_tour_packages t WHERE t.offer_id = o.id AND t.status = 'ACTIVE'))
                    ORDER BY o.sort_order, o.id
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "slug": r["slug"],
            "title": r["title"],
            "service_type": r["service_type"],
            "image_url": _circle_thumb(r["image_url"]),
            "coupon_code": r["coupon_code"],
            "badge": offer_service.derive_badge(
                r["discount_type"],
                r["discount_value"],
                r["max_discount_amount"],
                r["badge_label"],
            ),
            "valid_to": r["valid_to"],
        }
        for r in rows
    ]


@router.get("/offers/{slug}", tags=["Public Offers"])
async def get_live_offer(slug: str, db: AsyncSession = Depends(get_db)):
    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT o.id, o.slug, o.title, o.short_description, o.terms_and_conditions,
                           o.image_url, o.badge_label, o.service_type, o.valid_from, o.valid_to,
                           o.is_published, o.vehicle_category_id,
                           c.coupon_code, c.coupon_type, c.discount_type, c.discount_value,
                           c.max_discount_amount, c.min_booking_amount, c.max_usage_total,
                           c.current_usage_count, c.valid_from AS c_from, c.valid_to AS c_to,
                           c.is_active
                    FROM offers o JOIN coupons c ON c.id = o.coupon_id
                    WHERE o.slug = :slug
                    """
                ),
                {"slug": slug},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ResourceNotFoundException("Offer", slug)
    d = dict(row)

    today = date.today()
    is_available = bool(
        d["is_published"]
        and d["valid_from"] <= today <= d["valid_to"]
        and d["is_active"]
        and d["c_from"] <= today <= d["c_to"]
    )

    badge = offer_service.derive_badge(
        d["discount_type"],
        d["discount_value"],
        d["max_discount_amount"],
        d["badge_label"],
    )

    # Build the customer-facing booking target(s).
    target: dict = {"type": d["service_type"]}
    if d["service_type"] == "CAB":
        vc = (
            (
                await db.execute(
                    text(
                        "SELECT category_name AS name FROM vehicle_categories WHERE id = :v"
                    ),
                    {"v": d["vehicle_category_id"]},
                )
            )
            .mappings()
            .first()
        )
        target["vehicle_category_name"] = vc["name"] if vc else None
    elif d["service_type"] == "HOTEL":
        hotels = (
            (
                await db.execute(
                    text(
                        """
                        SELECT h.id, h.hotel_name AS name, h.slug, ci.name AS city,
                               COALESCE(jsonb_agg(jsonb_build_object('id', rc.id, 'name', rc.category_name))
                                        FILTER (WHERE rc.id IS NOT NULL), '[]'::jsonb) AS room_types
                        FROM offer_hotel_links l
                        JOIN hotels h ON h.id = l.hotel_id AND h.status = 'ACTIVE' AND h.deleted_at IS NULL
                        JOIN cities ci ON ci.id = h.city_id
                        LEFT JOIN offer_hotel_room_types rt ON rt.offer_id = l.offer_id
                        LEFT JOIN hotel_room_categories rc ON rc.id = rt.room_category_id AND rc.hotel_id = l.hotel_id AND rc.is_active
                        WHERE l.offer_id = :oid AND l.status = 'ACTIVE'
                        GROUP BY h.id, h.hotel_name, h.slug, ci.name
                        ORDER BY h.id
                        """
                    ),
                    {"oid": d["id"]},
                )
            )
            .mappings()
            .all()
        )
        target["hotels"] = [dict(h) for h in hotels]
        is_available = is_available and bool(hotels)
    else:  # TOUR
        tours = (
            (
                await db.execute(
                    text(
                        """
                        SELECT tp.id, tp.package_name AS name, tp.slug, tp.destination
                        FROM offer_tour_packages t
                        JOIN tour_packages tp ON tp.id = t.tour_package_id AND tp.status = 'ACTIVE'
                        WHERE t.offer_id = :oid AND t.status = 'ACTIVE'
                        ORDER BY tp.id
                        """
                    ),
                    {"oid": d["id"]},
                )
            )
            .mappings()
            .all()
        )
        target["packages"] = [dict(t) for t in tours]
        is_available = is_available and bool(tours)

    usages_left = None
    if d["max_usage_total"] is not None:
        usages_left = max(0, d["max_usage_total"] - d["current_usage_count"])

    return {
        "slug": d["slug"],
        "title": d["title"],
        "short_description": d["short_description"],
        "terms_and_conditions": d["terms_and_conditions"],
        "image_url": _circle_thumb(d["image_url"]),
        "service_type": d["service_type"],
        "badge": badge,
        "is_available": is_available,
        "valid_from": d["valid_from"],
        "valid_to": d["valid_to"],
        "coupon": {
            "code": d["coupon_code"],
            "discount_type": d["discount_type"],
            "discount_value": d["discount_value"],
            "max_discount_amount": d["max_discount_amount"],
            "min_booking_amount": d["min_booking_amount"],
            "funding_label": _FUNDING_LABEL.get(d["coupon_type"], "Offer"),
            "usages_left": usages_left,
        },
        "target": target,
    }
