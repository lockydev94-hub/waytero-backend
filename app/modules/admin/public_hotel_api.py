# ============================================================
# WAYTERO — PUBLIC HOTEL SEARCH & BOOKING API
# File: app/modules/admin/public_hotel_api.py
# Doc Ref:
#   BRD Part 4 §57-92 — Hotel module
#   SRS Part 5 §153-194 — Hotel booking
#   API Doc 09_HOTEL_API
#   Docs/21_Hotel_Module_Implementation/02_BACKEND_API.md
#
# Customer-facing counterpart of public_cab_api.py. Everything that is
# customer-visible on the marketing site comes from here; the admin/partner
# CRUD lives under /admin/hotels (app/modules/hotel/admin_api.py).
#
# Endpoints:
#   GET  /public/hotel/cities        — active cities that have bookable hotels
#   GET  /public/hotel/search        — city/place based hotel list + prices
#   GET  /public/hotel/{slug}        — hotel details (gallery, rooms, policy)
#   POST /public/hotel/quote         — server-side price for a stay (no auth)
#   POST /public/hotel/create-booking — create master + hotel reservation (auth)
#
# Hotel money is never computed on the client: stays are priced through the
# shared hotel pricing engine (app/modules/hotel/services/quote.py) so the
# customer sees exactly what the admin/partner/settlement flows see.
# ============================================================

import json
import uuid as _uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.hotel.services.pricing import (
    RatePlanInput,
    resolve_stay_rates,
)
from app.modules.hotel.services.quote import (
    compute_hotel_quote,
    quote_to_response,
)

router = APIRouter()

_HZERO = Decimal("0.00")


# ============================================================
# RESPONSE SCHEMAS
# ============================================================


class PublicHotelCityOut(BaseModel):
    id: int
    name: str
    city_code: Optional[str] = None
    state_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    hotel_count: int = 0


class PublicHotelSearchItem(BaseModel):
    id: int
    uuid: str
    slug: Optional[str] = None
    hotel_name: str
    star_rating: Optional[int] = None
    city_id: int
    city_name: str
    state_name: Optional[str] = None
    address: Optional[str] = None
    landmark: Optional[str] = None
    is_featured: bool = False
    primary_image_url: Optional[str] = None
    average_rating: float = 0
    total_reviews: int = 0
    starting_price: float = 0
    room_category_count: int = 0
    amenities: List[str] = Field(default_factory=list)


class PublicHotelSearchOut(BaseModel):
    items: List[PublicHotelSearchItem]
    total: int
    page: int
    page_size: int


class PublicHotelRoomCategoryOut(BaseModel):
    id: int
    category_name: str
    room_type: Optional[str] = None
    description: Optional[str] = None
    base_occupancy: int
    max_adults: int
    max_children: int
    max_occupancy: int
    bed_type: Optional[str] = None
    room_size_sqft: Optional[int] = None
    view_type: Optional[str] = None
    meal_plan: str
    is_refundable: bool = True
    base_price: float
    published_price: Optional[float] = None
    extra_bed_allowed: bool = False
    extra_bed_charge: float = 0
    extra_adult_charge: float = 0
    extra_child_charge: float = 0
    total_rooms: int = 0
    images: List[str] = Field(default_factory=list)
    # Availability for the requested stay window (null = open / no inventory
    # rows exist for those dates, so the room is bookable).
    available_rooms: Optional[int] = None
    stop_sell: bool = False
    # Resolved per-night rate for the requested stay window (rate plans +
    # inventory overrides applied on base_price). Empty when no dates were
    # sent — the card then falls back to base_price. nightly_rate is the
    # first night's rate (the figure the room card advertises as "/night").
    nightly_rate: Optional[float] = None
    nightly_rates: List[dict] = Field(default_factory=list)


class PublicHotelPolicyOut(BaseModel):
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    early_check_in_allowed: bool = False
    late_check_out_allowed: bool = False
    cancellation_free_hours: Optional[int] = None
    refund_percent_tier_1: Optional[float] = None
    cancellation_tier_2_hours: Optional[int] = None
    refund_percent_tier_2: Optional[float] = None
    cancellation_tier_3_hours: Optional[int] = None
    refund_percent_tier_3: Optional[float] = None
    refund_percent_same_day: Optional[float] = None
    couples_allowed: bool = True
    unmarried_couples_allowed: bool = False
    local_id_accepted: bool = True
    pets_allowed: bool = False
    smoking_allowed: bool = False
    alcohol_allowed: bool = True
    house_rules: Optional[str] = None
    cancellation_policy_text: Optional[str] = None


class PublicHotelDetailsOut(BaseModel):
    id: int
    uuid: str
    slug: Optional[str] = None
    hotel_name: str
    # Per-hotel SEO metadata (maintained under admin → hotel → SEO tab).
    # customer-web uses these for DB-driven meta tags on the hotel detail
    # page, falling back to static defaults when null.
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    hotel_type: Optional[str] = None
    star_rating: Optional[int] = None
    description: Optional[str] = None
    short_description: Optional[str] = None
    city_id: int
    city_name: str
    state_name: Optional[str] = None
    address: Optional[str] = None
    address_line_2: Optional[str] = None
    landmark: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_featured: bool = False
    average_rating: float = 0
    total_reviews: int = 0
    total_rooms: int = 0
    images: List[str] = Field(default_factory=list)
    amenities: List[str] = Field(default_factory=list)
    room_categories: List[PublicHotelRoomCategoryOut] = Field(default_factory=list)
    policy: Optional[PublicHotelPolicyOut] = None


class HotelQuoteRequest(BaseModel):
    hotel_id: int
    room_category_id: int
    check_in_date: date
    check_out_date: date
    rooms_count: int = Field(default=1, ge=1, le=30)
    adults_count: int = Field(default=1, ge=1)
    children_count: int = Field(default=0, ge=0)
    extra_beds: int = Field(default=0, ge=0)


class CreateHotelBookingIn(BaseModel):
    hotel_id: int
    room_category_id: int
    check_in_date: date
    check_out_date: date
    rooms_count: int = Field(default=1, ge=1, le=30)
    adults_count: int = Field(default=1, ge=1)
    children_count: int = Field(default=0, ge=0)
    extra_beds: int = Field(default=0, ge=0)
    # Primary guest details — saved to the reservation guest roster.
    primary_guest_name: str = Field(..., min_length=1, max_length=255)
    primary_guest_mobile: Optional[str] = Field(None, max_length=15)
    special_requests: Optional[str] = None
    # Quoted total (pre-coupon) from /public/hotel/quote. Hint only — the
    # server re-prices via compute_hotel_quote and the canonical amount is
    # returned in the response.
    estimated_amount: Optional[float] = None
    coupon_code: Optional[str] = None
    coupon_id: Optional[int] = None


# ============================================================
# HELPERS
# ============================================================


def _reservation_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = str(_uuid.uuid4()).replace("-", "")[:6].upper()
    return f"HR-{ts}-{uid}"


async def _load_active_hotel(db: AsyncSession, hotel_id: int):
    """Only ACTIVE, non-deleted hotels are bookable from the customer site."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT h.*, ci.name AS city_name, s.name AS state_name "
                    "FROM hotels h "
                    "LEFT JOIN cities ci ON ci.id = h.city_id "
                    "LEFT JOIN states s ON s.id = h.state_id "
                    "WHERE h.id = :hid AND h.deleted_at IS NULL"
                ),
                {"hid": hotel_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Hotel not found")
    if str(row["status"]) != "ACTIVE":
        raise HTTPException(
            status_code=400,
            detail="This hotel is not accepting bookings right now",
        )
    return row


async def _hotel_by_slug(db: AsyncSession, slug: str):
    row = (
        (
            await db.execute(
                text(
                    "SELECT h.*, ci.name AS city_name, s.name AS state_name "
                    "FROM hotels h "
                    "LEFT JOIN cities ci ON ci.id = h.city_id "
                    "LEFT JOIN states s ON s.id = h.state_id "
                    "WHERE h.slug = :slug AND h.deleted_at IS NULL"
                ),
                {"slug": slug},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Hotel not found")
    if str(row["status"]) != "ACTIVE":
        raise HTTPException(
            status_code=400,
            detail="This hotel is not accepting bookings right now",
        )
    return row


async def _category_belongs(db: AsyncSession, hotel_id: int, category_id: int) -> bool:
    row = (
        await db.execute(
            text(
                "SELECT id FROM hotel_room_categories "
                "WHERE id = :cid AND hotel_id = :hid AND is_active = TRUE"
            ),
            {"cid": category_id, "hid": hotel_id},
        )
    ).scalar_one_or_none()
    return row is not None


async def _availability_window(
    db: AsyncSession,
    category_id: int,
    check_in: date,
    check_out: date,
) -> dict[str, Any]:
    """Per-night availability for a stay window.

    Returns min available_rooms across the nights that have inventory rows
    (None when no rows exist = open availability), plus whether any night is
    on stop-sell. The DB CHECK(available_rooms >= 0) is the hard oversell
    guard at booking time; this is the soft pre-check for the UI.
    """
    rows = (
        (
            await db.execute(
                text(
                    "SELECT inventory_date, available_rooms, is_stop_sell "
                    "FROM hotel_inventory "
                    "WHERE room_category_id = :cid "
                    "  AND inventory_date >= :dfrom AND inventory_date < :dto "
                    "ORDER BY inventory_date"
                ),
                {"cid": category_id, "dfrom": check_in, "dto": check_out},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return {"min_available": None, "stop_sell": False, "nights": []}
    available = [int(r["available_rooms"]) for r in rows]
    stop_sell = any(bool(r["is_stop_sell"]) for r in rows)
    return {
        "min_available": min(available) if available else None,
        "stop_sell": stop_sell,
        "nights": [
            {
                "date": r["inventory_date"].isoformat(),
                "available_rooms": int(r["available_rooms"]),
                "stop_sell": bool(r["is_stop_sell"]),
            }
            for r in rows
        ],
    }


def _hotel_orm_from_row(row) -> Any:
    """Rebuild a light Hotel ORM instance from a raw SELECT h.* row.

    compute_hotel_quote reads hotel.tax_mode / hotel.hotel_name off the ORM
    object; the raw mapping is cheaper than a second SELECT for these paths.
    """
    from app.modules.hotel.models import Hotel as HotelORM

    obj = HotelORM()
    for col in row:
        if hasattr(obj, col):
            setattr(obj, col, row[col])
    return obj


async def _nightly_rates_for_window(
    db: AsyncSession,
    category_id: int,
    check_in: date,
    check_out: date,
) -> List[dict]:
    """Resolve the per-night room rate for a stay window through the same
    engine the quote uses (base_price → rate plans → inventory overrides).
    Returns [{date, rate}] — empty when the window has no nights.

    This is what makes the details-page card agree with the quote/bookings
    page: a rate plan (e.g. "Summer Special" ABSOLUTE) or a per-date override
    can make the actual nightly rate differ from the raw base_price.
    """
    if check_out <= check_in:
        return []
    cat = (
        (
            await db.execute(
                text(
                    "SELECT base_price FROM hotel_room_categories "
                    "WHERE id = :cid AND is_active = TRUE"
                ),
                {"cid": category_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not cat:
        return []

    base_price = Decimal(str(cat["base_price"] or 0))

    plan_rows = (
        (
            await db.execute(
                text(
                    "SELECT id, plan_name, plan_type, priority, date_from, date_to, "
                    "       day_of_week_mask, rate_mode, rate_value, min_nights, is_active "
                    "FROM hotel_room_rate_plans "
                    "WHERE room_category_id = :cid AND is_active = TRUE "
                    "  AND date_from <= :dto AND date_to >= :dfrom"
                ),
                {"cid": category_id, "dfrom": check_in, "dto": check_out},
            )
        )
        .mappings()
        .all()
    )
    plans = [
        RatePlanInput(
            id=int(p["id"]),
            plan_name=str(p["plan_name"]),
            plan_type=str(p["plan_type"]),
            priority=int(p["priority"] or 0),
            date_from=p["date_from"],
            date_to=p["date_to"],
            day_of_week_mask=p["day_of_week_mask"],
            rate_mode=str(p["rate_mode"]),
            rate_value=Decimal(str(p["rate_value"] or 0)),
            min_nights=int(p["min_nights"] or 1),
            is_active=bool(p["is_active"]),
        )
        for p in plan_rows
    ]

    ovr_rows = (
        (
            await db.execute(
                text(
                    "SELECT inventory_date, rate_override FROM hotel_inventory "
                    "WHERE room_category_id = :cid "
                    "  AND inventory_date >= :dfrom AND inventory_date < :dto "
                    "  AND rate_override IS NOT NULL"
                ),
                {"cid": category_id, "dfrom": check_in, "dto": check_out},
            )
        )
        .mappings()
        .all()
    )
    overrides = {
        r["inventory_date"]: Decimal(str(r["rate_override"])) for r in ovr_rows
    }

    quote = resolve_stay_rates(
        check_in=check_in,
        check_out=check_out,
        base_price=base_price,
        rate_plans=plans,
        inventory_overrides=overrides,
    )
    return [
        {"date": night.stay_date.isoformat(), "rate": float(night.rate)}
        for night in quote.nights
    ]


def _assert_available(avail: dict[str, Any], rooms_count: int) -> None:
    """Raise 409 when the stay window is not bookable for the room count."""
    if avail.get("stop_sell"):
        raise HTTPException(
            status_code=409,
            detail="Rooms are on stop-sell for part of the selected stay",
        )
    min_avail = avail.get("min_available")
    if min_avail is not None and int(min_avail) < rooms_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only {min_avail} room(s) available for part of the selected "
                f"dates — choose fewer rooms or different dates."
            ),
        )


# ============================================================
# GET /public/hotel/cities
# ============================================================


@router.get(
    "/cities",
    response_model=List[PublicHotelCityOut],
    summary="Active cities that have bookable hotels (no auth)",
)
async def list_hotel_cities(db: AsyncSession = Depends(get_db)):
    """Cities with at least one ACTIVE, non-deleted hotel — drives the hotel
    search form's city dropdown and the popular-destinations strip."""
    result = await db.execute(
        text(
            """
        SELECT c.id, c.name, c.city_code, s.name AS state_name,
               c.latitude, c.longitude,
               (SELECT COUNT(*) FROM hotels h
                 WHERE h.city_id = c.id AND h.status = 'ACTIVE'
                   AND h.deleted_at IS NULL) AS hotel_count
        FROM cities c
        LEFT JOIN states s ON s.id = c.state_id
        WHERE c.is_active = TRUE
          AND EXISTS (
            SELECT 1 FROM hotels h
            WHERE h.city_id = c.id AND h.status = 'ACTIVE'
              AND h.deleted_at IS NULL
          )
        ORDER BY hotel_count DESC, c.name
        """
        )
    )
    return [
        PublicHotelCityOut(
            id=r.id,
            name=r.name,
            city_code=r.city_code,
            state_name=r.state_name,
            latitude=float(r.latitude) if r.latitude is not None else None,
            longitude=float(r.longitude) if r.longitude is not None else None,
            hotel_count=int(r.hotel_count or 0),
        )
        for r in result.fetchall()
    ]


# ============================================================
# GET /public/hotel/search
# ============================================================


@router.get(
    "/search",
    response_model=PublicHotelSearchOut,
    summary="Search hotels by city or place (no auth)",
)
async def search_hotels(
    city_id: Optional[int] = Query(None, description="Filter by city"),
    q: Optional[str] = Query(
        None,
        description="Free-text place search — matches hotel name, landmark, "
        "address, or city name (e.g. 'Goa', 'Marine Drive', 'Taj')",
    ),
    check_in: Optional[date] = Query(None),
    check_out: Optional[date] = Query(None),
    star_rating: Optional[int] = Query(None, ge=1, le=7),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    is_featured: Optional[bool] = Query(None),
    sort_by: str = Query(
        "recommended", description="recommended | price_asc | price_desc | rating"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """City/place based hotel list with starting nightly price, rating, image.

    `starting_price` is the cheapest active room category's base price —
    the OTA-standard "Starts from ₹X" figure. Per-stay pricing (rate plans,
    GST, availability) happens on the details/quote endpoints.
    """
    where = ["h.status = 'ACTIVE'", "h.deleted_at IS NULL"]
    params: dict[str, Any] = {}

    if city_id is not None:
        where.append("h.city_id = :city_id")
        params["city_id"] = city_id
    if q and q.strip():
        where.append(
            "(h.hotel_name ILIKE :q OR h.address ILIKE :q "
            "OR COALESCE(h.landmark, '') ILIKE :q OR ci.name ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"
    if star_rating is not None:
        where.append("h.star_rating = :star")
        params["star"] = star_rating
    if is_featured is not None:
        where.append("h.is_featured = :featured")
        params["featured"] = is_featured
    if min_price is not None:
        where.append(
            "(SELECT MIN(rc.base_price) FROM hotel_room_categories rc "
            " WHERE rc.hotel_id = h.id AND rc.is_active = TRUE "
            "   AND rc.base_price > 0) >= :min_price"
        )
        params["min_price"] = min_price
    if max_price is not None:
        where.append(
            "(SELECT MIN(rc.base_price) FROM hotel_room_categories rc "
            " WHERE rc.hotel_id = h.id AND rc.is_active = TRUE "
            "   AND rc.base_price > 0) <= :max_price"
        )
        params["max_price"] = max_price

    where_sql = " AND ".join(where)

    total = (
        (
            await db.execute(
                text(
                    f"""
                SELECT COUNT(*) FROM hotels h
                LEFT JOIN cities ci ON ci.id = h.city_id
                WHERE {where_sql}
                """
                ),
                params,
            )
        ).scalar()
        or 0
    )

    order_map = {
        "price_asc": "starting_price ASC NULLS LAST, h.star_rating DESC NULLS LAST, h.hotel_name",
        "price_desc": "starting_price DESC NULLS LAST, h.star_rating DESC NULLS LAST, h.hotel_name",
        "rating": "average_rating DESC, total_reviews DESC, h.hotel_name",
        "recommended": "h.is_featured DESC, average_rating DESC, total_reviews DESC, h.hotel_name",
    }
    order_sql = order_map.get(sort_by, order_map["recommended"])

    rows = await db.execute(
        text(
            f"""
        SELECT h.id, h.uuid, h.slug, h.hotel_name, h.star_rating,
               h.city_id, ci.name AS city_name, s.name AS state_name,
               h.address, h.landmark, h.is_featured,
               COALESCE((
                 SELECT MIN(rc.base_price) FROM hotel_room_categories rc
                 WHERE rc.hotel_id = h.id AND rc.is_active = TRUE
                   AND rc.base_price > 0
               ), 0) AS starting_price,
               (SELECT COUNT(*) FROM hotel_room_categories rc
                WHERE rc.hotel_id = h.id AND rc.is_active = TRUE) AS room_category_count,
               (SELECT image_url FROM hotel_images i
                WHERE i.hotel_id = h.id AND i.is_primary = TRUE
                ORDER BY i.display_order LIMIT 1) AS primary_image_url,
               COALESCE(hr.average_rating, 0) AS average_rating,
               COALESCE(hr.total_reviews, 0) AS total_reviews,
               COALESCE((
                 SELECT json_agg(a.amenity_name ORDER BY a.display_order)
                 FROM hotel_amenity_mappings m
                 JOIN hotel_amenities a ON a.id = m.amenity_id
                 WHERE m.hotel_id = h.id
               ), '[]'::json) AS amenities
        FROM hotels h
        LEFT JOIN cities ci ON ci.id = h.city_id
        LEFT JOIN states s ON s.id = h.state_id
        LEFT JOIN hotel_ratings hr ON hr.hotel_id = h.id
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT :limit OFFSET :offset
        """
        ),
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )

    items: List[PublicHotelSearchItem] = []
    for r in rows.mappings().all():
        amenities_raw = r.get("amenities")
        if isinstance(amenities_raw, list):
            amenity_names = [str(a) for a in amenities_raw][:4]
        else:
            amenity_names = []
        items.append(
            PublicHotelSearchItem(
                id=int(r["id"]),
                uuid=str(r["uuid"]),
                slug=r["slug"],
                hotel_name=r["hotel_name"],
                star_rating=r["star_rating"],
                city_id=int(r["city_id"]),
                city_name=r["city_name"],
                state_name=r["state_name"],
                address=r["address"],
                landmark=r["landmark"],
                is_featured=bool(r["is_featured"]),
                primary_image_url=r["primary_image_url"],
                average_rating=float(r["average_rating"] or 0),
                total_reviews=int(r["total_reviews"] or 0),
                starting_price=float(r["starting_price"] or 0),
                room_category_count=int(r["room_category_count"] or 0),
                amenities=amenity_names,
            )
        )

    return PublicHotelSearchOut(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
    )


# ============================================================
# GET /public/hotel/{slug}  (declared after literal routes)
# ============================================================


@router.get(
    "/{slug}",
    response_model=PublicHotelDetailsOut,
    summary="Hotel details: gallery, amenities, room categories, policy (no auth)",
)
async def get_hotel_details(
    slug: str,
    check_in: Optional[date] = Query(None),
    check_out: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Full customer-facing detail page payload. Room categories carry their
    availability for the requested stay window when check_in/check_out are
    given, so the UI can disable sold-out categories up front."""
    hotel = await _hotel_by_slug(db, slug)
    hid = int(hotel["id"])

    images = (
        await db.execute(
            text(
                "SELECT image_url, thumbnail_url FROM hotel_images "
                "WHERE hotel_id = :hid ORDER BY is_primary DESC, display_order, id"
            ),
            {"hid": hid},
        )
    ).fetchall()
    image_urls = [r[0] for r in images]

    amenity_names = (
        (
            await db.execute(
                text(
                    "SELECT a.amenity_name FROM hotel_amenity_mappings m "
                    "JOIN hotel_amenities a ON a.id = m.amenity_id "
                    "WHERE m.hotel_id = :hid AND a.is_active = TRUE "
                    "ORDER BY a.display_order, a.amenity_name"
                ),
                {"hid": hid},
            )
        )
        .scalars()
        .all()
    )

    cats = (
        (
            await db.execute(
                text(
                    "SELECT id, category_name, room_type, description, "
                    "       base_occupancy, max_adults, max_children, max_occupancy, "
                    "       bed_type, room_size_sqft, view_type, meal_plan, is_refundable, "
                    "       base_price, published_price, "
                    "       extra_bed_allowed, extra_bed_charge, "
                    "       extra_adult_charge, extra_child_charge, total_rooms "
                    "FROM hotel_room_categories "
                    "WHERE hotel_id = :hid AND is_active = TRUE "
                    "ORDER BY display_order, base_price, id"
                ),
                {"hid": hid},
            )
        )
        .mappings()
        .all()
    )

    cat_images: dict[int, list[str]] = {}
    if cats:
        rows = await db.execute(
            text(
                "SELECT room_category_id, image_url FROM hotel_room_category_images "
                "WHERE room_category_id = ANY(:cids) "
                "ORDER BY is_primary DESC, display_order, id"
            ),
            {"cids": [int(c["id"]) for c in cats]},
        )
        for r in rows.fetchall():
            cat_images.setdefault(int(r[0]), []).append(r[1])

    room_categories: List[PublicHotelRoomCategoryOut] = []
    for c in cats:
        avail = None
        stop_sell = False
        nightly_rates: List[dict] = []
        if check_in is not None and check_out is not None:
            a = await _availability_window(db, int(c["id"]), check_in, check_out)
            avail = a["min_available"]
            stop_sell = a["stop_sell"]
            nightly_rates = await _nightly_rates_for_window(
                db, int(c["id"]), check_in, check_out
            )
        room_categories.append(
            PublicHotelRoomCategoryOut(
                id=int(c["id"]),
                category_name=c["category_name"],
                room_type=c["room_type"],
                description=c["description"],
                base_occupancy=int(c["base_occupancy"] or 1),
                max_adults=int(c["max_adults"] or 1),
                max_children=int(c["max_children"] or 0),
                max_occupancy=int(c["max_occupancy"] or 1),
                bed_type=c["bed_type"],
                room_size_sqft=c["room_size_sqft"],
                view_type=c["view_type"],
                meal_plan=c["meal_plan"] or "EP",
                is_refundable=bool(c["is_refundable"]),
                base_price=float(c["base_price"] or 0),
                published_price=(
                    float(c["published_price"])
                    if c["published_price"] is not None
                    else None
                ),
                extra_bed_allowed=bool(c["extra_bed_allowed"]),
                extra_bed_charge=float(c["extra_bed_charge"] or 0),
                extra_adult_charge=float(c["extra_adult_charge"] or 0),
                extra_child_charge=float(c["extra_child_charge"] or 0),
                total_rooms=int(c["total_rooms"] or 0),
                images=cat_images.get(int(c["id"]), []),
                available_rooms=avail,
                stop_sell=stop_sell,
                nightly_rate=float(nightly_rates[0]["rate"]) if nightly_rates else None,
                nightly_rates=nightly_rates,
            )
        )

    policy_row = (
        (
            await db.execute(
                text("SELECT * FROM hotel_policies WHERE hotel_id = :hid"),
                {"hid": hid},
            )
        )
        .mappings()
        .one_or_none()
    )

    policy: Optional[PublicHotelPolicyOut] = None
    if policy_row:
        policy = PublicHotelPolicyOut(
            check_in_time=policy_row.get("check_in_time"),
            check_out_time=policy_row.get("check_out_time"),
            early_check_in_allowed=bool(policy_row.get("early_check_in_allowed")),
            late_check_out_allowed=bool(policy_row.get("late_check_out_allowed")),
            cancellation_free_hours=policy_row.get("cancellation_free_hours"),
            refund_percent_tier_1=(
                float(policy_row["refund_percent_tier_1"])
                if policy_row.get("refund_percent_tier_1") is not None
                else None
            ),
            cancellation_tier_2_hours=policy_row.get("cancellation_tier_2_hours"),
            refund_percent_tier_2=(
                float(policy_row["refund_percent_tier_2"])
                if policy_row.get("refund_percent_tier_2") is not None
                else None
            ),
            cancellation_tier_3_hours=policy_row.get("cancellation_tier_3_hours"),
            refund_percent_tier_3=(
                float(policy_row["refund_percent_tier_3"])
                if policy_row.get("refund_percent_tier_3") is not None
                else None
            ),
            refund_percent_same_day=(
                float(policy_row["refund_percent_same_day"])
                if policy_row.get("refund_percent_same_day") is not None
                else None
            ),
            couples_allowed=bool(policy_row.get("couples_allowed", True)),
            unmarried_couples_allowed=bool(policy_row.get("unmarried_couples_allowed")),
            local_id_accepted=bool(policy_row.get("local_id_accepted", True)),
            pets_allowed=bool(policy_row.get("pets_allowed")),
            smoking_allowed=bool(policy_row.get("smoking_allowed")),
            alcohol_allowed=bool(policy_row.get("alcohol_allowed", True)),
            house_rules=policy_row.get("house_rules"),
            cancellation_policy_text=policy_row.get("cancellation_policy_text"),
        )

    rating = (
        (
            await db.execute(
                text(
                    "SELECT average_rating, total_reviews FROM hotel_ratings "
                    "WHERE hotel_id = :hid"
                ),
                {"hid": hid},
            )
        )
        .mappings()
        .one_or_none()
    )

    return PublicHotelDetailsOut(
        id=hid,
        uuid=str(hotel["uuid"]),
        slug=hotel["slug"],
        hotel_name=hotel["hotel_name"],
        seo_title=hotel.get("seo_title"),
        seo_description=hotel.get("seo_description"),
        seo_keywords=hotel.get("seo_keywords"),
        hotel_type=hotel["hotel_type"],
        star_rating=hotel["star_rating"],
        description=hotel["description"],
        short_description=hotel["short_description"],
        city_id=int(hotel["city_id"]),
        city_name=hotel["city_name"],
        state_name=hotel["state_name"],
        address=hotel["address"],
        address_line_2=hotel["address_line_2"],
        landmark=hotel["landmark"],
        postal_code=hotel["postal_code"],
        latitude=(float(hotel["latitude"]) if hotel["latitude"] is not None else None),
        longitude=(
            float(hotel["longitude"]) if hotel["longitude"] is not None else None
        ),
        is_featured=bool(hotel["is_featured"]),
        average_rating=float(rating["average_rating"] or 0) if rating else 0,
        total_reviews=int(rating["total_reviews"] or 0) if rating else 0,
        total_rooms=int(hotel["total_rooms"] or 0),
        images=image_urls,
        amenities=[str(a) for a in amenity_names],
        room_categories=room_categories,
        policy=policy,
    )


# ============================================================
# POST /public/hotel/quote
# ============================================================


@router.post(
    "/quote",
    summary="Server-side price for a hotel stay (no auth)",
    description=(
        "Prices a stay through the shared hotel pricing engine — per-night "
        "rate plans, inventory rate overrides, tariff-based GST slabs and the "
        "resolved commission. Also validates per-night availability for the "
        "requested room count. The customer never computes hotel money."
    ),
)
async def hotel_quote(
    payload: HotelQuoteRequest,
    db: AsyncSession = Depends(get_db),
):
    if payload.check_out_date <= payload.check_in_date:
        raise HTTPException(status_code=400, detail="Check-out must be after check-in")

    hotel = await _load_active_hotel(db, payload.hotel_id)
    if not await _category_belongs(db, payload.hotel_id, payload.room_category_id):
        raise HTTPException(
            status_code=404, detail="Room category not found for this hotel"
        )

    avail = await _availability_window(
        db, payload.room_category_id, payload.check_in_date, payload.check_out_date
    )
    _assert_available(avail, payload.rooms_count)

    q = await compute_hotel_quote(
        db,
        _hotel_orm_from_row(hotel),
        payload.room_category_id,
        payload.check_in_date,
        payload.check_out_date,
        payload.rooms_count,
        adults_count=payload.adults_count,
        children_count=payload.children_count,
        extra_beds=payload.extra_beds,
    )
    resp = quote_to_response(q)
    resp["availability"] = {
        "min_available": avail["min_available"],
        "stop_sell": avail["stop_sell"],
    }
    return {"success": True, **resp}


# ============================================================
# POST /public/hotel/create-booking (auth required)
# ============================================================


@router.post(
    "/create-booking",
    summary="Create master booking + hotel reservation (auth required)",
    description=(
        "Files a master booking (DRAFT) and a hotel reservation "
        "(PENDING_PAYMENT), links the HOTEL service, decrements per-night "
        "inventory and snapshots the rate + commission in force. Mirrors the "
        "cab booking flow — the customer must be authenticated."
    ),
)
async def create_hotel_booking(
    body: CreateHotelBookingIn,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.check_out_date <= body.check_in_date:
        raise HTTPException(status_code=400, detail="Check-out must be after check-in")

    # ── Load hotel first — its city seeds the lazily-created customer ─────
    hotel = await _load_active_hotel(db, body.hotel_id)
    if not await _category_belongs(db, body.hotel_id, body.room_category_id):
        raise HTTPException(
            status_code=404, detail="Room category not found for this hotel"
        )

    # ── Resolve customer, lazily creating the profile + wallet on first
    #    booking (same pattern as public_cab_api.create_cab_booking).
    user_uuid_str = str(current_user["sub"])
    cust = (
        await db.execute(
            text("SELECT id FROM customers WHERE user_id = :uid"),
            {"uid": user_uuid_str},
        )
    ).fetchone()

    if cust:
        customer_id = cust.id
    else:
        user_row = (
            (
                await db.execute(
                    text(
                        "SELECT first_name, last_name, mobile_number, email "
                        "FROM users WHERE id = :uid"
                    ),
                    {"uid": user_uuid_str},
                )
            )
            .mappings()
            .one_or_none()
        )

        cust_count = (
            await db.execute(text("SELECT COUNT(*) FROM customers"))
        ).scalar() or 0
        cust_code = f"CT-{cust_count + 1:06d}"
        inserted = (
            await db.execute(
                text(
                    """
                    INSERT INTO customers (
                        uuid, user_id, customer_code,
                        first_name, last_name, city_id,
                        is_active, created_at, updated_at
                    ) VALUES (
                        :uuid, :user_id, :code,
                        :first_name, :last_name, :city_id,
                        TRUE, NOW(), NOW()
                    )
                    RETURNING id
                """
                ),
                {
                    "uuid": str(_uuid.uuid4()),
                    "user_id": user_uuid_str,
                    "code": cust_code,
                    "first_name": user_row.get("first_name") if user_row else None,
                    "last_name": user_row.get("last_name") if user_row else None,
                    "city_id": int(hotel["city_id"]),
                },
            )
        ).fetchone()
        if inserted is None:
            raise RuntimeError("Customer row could not be created for booking")
        customer_id = inserted.id

        await db.execute(
            text(
                """
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance, wallet_status, created_at, updated_at)
                VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
                ON CONFLICT (customer_id) DO NOTHING
            """
            ),
            {"cid": customer_id},
        )

    q = await compute_hotel_quote(
        db,
        _hotel_orm_from_row(hotel),
        body.room_category_id,
        body.check_in_date,
        body.check_out_date,
        body.rooms_count,
        adults_count=body.adults_count,
        children_count=body.children_count,
        extra_beds=body.extra_beds,
    )

    # ── Decrement inventory per night. Dates without an inventory row are
    #    open (skipped); the DB CHECK(available_rooms >= 0) is the hard
    #    oversell guard and surfaces as a 409.
    try:
        upd = await db.execute(
            text(
                """
            UPDATE hotel_inventory
               SET booked_rooms    = booked_rooms + :rooms,
                   available_rooms = available_rooms - :rooms,
                   updated_at = NOW()
             WHERE room_category_id = :cid
               AND inventory_date >= :dfrom AND inventory_date < :dto
               AND is_stop_sell = FALSE
            RETURNING inventory_date
        """
            ),
            {
                "rooms": body.rooms_count,
                "cid": body.room_category_id,
                "dfrom": body.check_in_date,
                "dto": body.check_out_date,
            },
        )
        upd.fetchall()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Not enough rooms available for the selected dates",
        )

    # ── Master booking (DRAFT, payment pending — mirrors the cab flow) ────
    ts = datetime.utcnow()
    seq = (await db.execute(text("SELECT nextval('master_bookings_id_seq')"))).scalar()
    master_num = f"WT-MTB-{ts.year}-{seq:06d}"
    hotel_num = f"WT-HTL-{ts.year}-{seq:06d}"

    mb_r = await db.execute(
        text(
            """
        INSERT INTO master_bookings (
            uuid, booking_number, customer_id, city_id,
            booking_status, payment_status, total_amount,
            journey_start_date, journey_end_date, remarks, created_at, updated_at
        ) VALUES (
            :uuid, :bnum, :cid, :city_id,
            'DRAFT', 'PENDING', :amount,
            :jstart, :jend, :remarks, NOW(), NOW()
        )
        RETURNING id
    """
        ),
        {
            "uuid": str(_uuid.uuid4()),
            "bnum": master_num,
            "cid": customer_id,
            "city_id": int(hotel["city_id"]),
            "amount": q["total_amount"],
            "jstart": body.check_in_date,
            "jend": body.check_out_date,
            "remarks": "Hotel booking placed by customer on the website",
        },
    )
    mb_id = mb_r.scalar_one()

    # ── Booking service link (HOTEL) ──────────────────────────────────────
    svc_r = await db.execute(
        text(
            """
        INSERT INTO booking_services (
            master_booking_id, service_type, service_reference_id,
            service_status, service_amount, created_at
        ) VALUES (:mb_id, 'HOTEL', 0, 'PENDING_PAYMENT', :amount, NOW())
        RETURNING id
    """
        ),
        {"mb_id": mb_id, "amount": q["total_amount"]},
    )
    svc_id = svc_r.scalar_one()

    # ── Hotel reservation ─────────────────────────────────────────────────
    res_number = _reservation_number()
    res_r = await db.execute(
        text(
            """
        INSERT INTO hotel_reservations (
            uuid, master_booking_id, booking_service_id, hotel_id, room_category_id,
            customer_id, reservation_number,
            check_in_date, check_out_date, nights, rooms_count, room_nights,
            adults_count, children_count,
            base_amount, extra_charges, discount_amount, taxable_amount,
            gst_percent, gst_amount, is_tax_invoice, total_amount,
            platform_commission, partner_payout,
            rate_snapshot, commission_config_snapshot,
            reservation_status, special_requests, created_at, updated_at
        ) VALUES (
            :uuid, :mb_id, :svc_id, :hotel_id, :cat_id,
            :cust_id, :res_num,
            :cin, :cout, :nights, :rooms, :room_nights,
            :adults, :children,
            :base, 0, 0, :taxable,
            :gst_pct, :gst_amt, :is_tax_inv, :total,
            :commission, :payout,
            CAST(:rate_snap AS JSONB), CAST(:comm_snap AS JSONB),
            'PENDING_PAYMENT', :special, NOW(), NOW()
        )
        RETURNING id
    """
        ),
        {
            "uuid": str(_uuid.uuid4()),
            "mb_id": mb_id,
            "svc_id": svc_id,
            "hotel_id": body.hotel_id,
            "cat_id": body.room_category_id,
            "cust_id": customer_id,
            "res_num": res_number,
            "cin": body.check_in_date,
            "cout": body.check_out_date,
            "nights": q["nights"],
            "rooms": body.rooms_count,
            "room_nights": q["room_nights"],
            "adults": body.adults_count,
            "children": body.children_count,
            "base": q["base_amount"],
            "taxable": q["taxable_amount"],
            "gst_pct": q["gst_percent"],
            "gst_amt": q["gst_amount"],
            "is_tax_inv": q["is_tax_invoice"],
            "total": q["total_amount"],
            "commission": q["platform_commission"],
            "payout": q["partner_payout"],
            "rate_snap": json.dumps(q["rate_snapshot"]),
            "comm_snap": json.dumps(q["commission_snapshot"]),
            "special": body.special_requests,
        },
    )
    res_id = res_r.scalar_one()

    # Point the service link at the reservation.
    await db.execute(
        text("UPDATE booking_services SET service_reference_id = :rid WHERE id = :sid"),
        {"rid": res_id, "sid": svc_id},
    )

    # ── Coupon validation + persistence (mirrors the cab flow) ────────────
    coupon_discount_applied = _HZERO
    coupon_id_used: Optional[int] = None
    coupon_code_used: Optional[str] = None
    if body.coupon_code:
        from app.modules.admin.coupon_api import (
            ValidateRequest,
            validate_coupon,
        )

        cust_mobile = (
            await db.execute(
                text(
                    """
                    SELECT u.mobile_number FROM customers c
                    JOIN users u ON u.id = c.user_id
                    WHERE c.id = :cid
                    """
                ),
                {"cid": customer_id},
            )
        ).scalar()

        vres = await validate_coupon(
            ValidateRequest(
                coupon_code=body.coupon_code,
                service_type="HOTEL",
                city_id=int(hotel["city_id"]),
                booking_amount=q["total_amount"],
                customer_id=customer_id,
                customer_mobile=cust_mobile,
            ),
            db,
        )
        if not vres.valid:
            raise HTTPException(
                status_code=409,
                detail=f"Coupon not applied: {vres.message}",
            )
        coupon_discount_applied = Decimal(str(vres.discount_amount))
        coupon_code_used = body.coupon_code.upper().strip()
        coupon_id_row = await db.execute(
            text("SELECT id FROM coupons WHERE coupon_code = :code"),
            {"code": coupon_code_used},
        )
        coupon_id_used = coupon_id_row.scalar()

    if coupon_id_used:
        await db.execute(
            text(
                "UPDATE hotel_reservations "
                "SET coupon_code = :code, coupon_discount = :disc "
                "WHERE id = :rid"
            ),
            {
                "code": coupon_code_used,
                "disc": coupon_discount_applied,
                "rid": res_id,
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO coupon_usages
                    (coupon_id, customer_id, master_booking_id, discount_applied, used_at)
                VALUES (:cid, :cust, :mbid, :disc, NOW())
                """
            ),
            {
                "cid": coupon_id_used,
                "cust": customer_id,
                "mbid": mb_id,
                "disc": coupon_discount_applied,
            },
        )
        await db.execute(
            text(
                """
                UPDATE coupons
                SET current_usage_count = current_usage_count + 1,
                    updated_at = NOW()
                WHERE id = :cid
                """
            ),
            {"cid": coupon_id_used},
        )

    # ── Primary guest roster row ──────────────────────────────────────────
    await db.execute(
        text(
            """
        INSERT INTO hotel_reservation_guests (
            reservation_id, guest_name, mobile, is_primary, created_at
        ) VALUES (:rid, :name, :mobile, TRUE, NOW())
    """
        ),
        {
            "rid": res_id,
            "name": body.primary_guest_name.strip(),
            "mobile": body.primary_guest_mobile,
        },
    )

    # ── Timeline ──────────────────────────────────────────────────────────
    await db.execute(
        text(
            """
        INSERT INTO booking_timelines (
            master_booking_id, event_type, event_description, event_timestamp, created_by
        ) VALUES (:mb_id, 'BOOKING_CREATED', 'Hotel booking placed by customer on the website', NOW(), :by)
    """
        ),
        {"mb_id": mb_id, "by": user_uuid_str},
    )

    await db.commit()

    # ── Realtime: notify admins (accept modal) + the hotel's partner ──
    # The hotel's owner partner is the FIXED assignee for hotel bookings —
    # they get their own WS popup to accept/reject and start managing.
    try:
        from app.modules.notification.services.booking_notifications import (
            admin_booking_requested,
            partner_hotel_booking_requested,
        )

        await admin_booking_requested(
            db,
            master_booking_id=mb_id,
            service_type="HOTEL",
            booking_number=hotel_num,
            title=f"New hotel booking: {hotel_num}",
            body=(
                f"Hotel {hotel['hotel_name']} · "
                f"{body.check_in_date} → {body.check_out_date}. "
                f"Accept to confirm."
            ),
            data={
                "service_id": res_id,
                "hotel_name": hotel["hotel_name"],
                "check_in_date": body.check_in_date.isoformat(),
                "check_out_date": body.check_out_date.isoformat(),
                "amount": float(q["total_amount"]),
            },
        )
        partner_id = hotel.get("partner_id")
        if partner_id:
            await partner_hotel_booking_requested(
                db,
                partner_id=int(partner_id),
                master_booking_id=mb_id,
                reservation_id=res_id,
                reservation_number=res_number,
                hotel_name=str(hotel["hotel_name"] or ""),
                check_in_date=body.check_in_date.isoformat(),
                check_out_date=body.check_out_date.isoformat(),
                total_amount=float(q["total_amount"]),
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
            (
                await db.execute(
                    text(
                        """
                        SELECT u.email, c.first_name, c.last_name
                        FROM customers c
                        JOIN users u ON u.id = c.user_id
                        WHERE c.id = :cid
                        """
                    ),
                    {"cid": customer_id},
                )
            )
            .mappings()
            .first()
        )
        if cust_row and cust_row["email"]:
            cust_name = (
                " ".join(filter(None, [cust_row["first_name"], cust_row["last_name"]]))
                or "there"
            )
            guests = f"{body.adults_count} adults"
            if body.children_count:
                guests += f", {body.children_count} children"
            await send_event_email(
                db,
                event_type="booking_requested",
                to_email=cust_row["email"],
                to_name=cust_name,
                context={
                    "name": cust_name,
                    "service": "Hotel",
                    "booking_number": res_number,
                    "message": (
                        "Your hotel booking has been received and the hotel is "
                        "confirming your stay. You'll get another email once it's "
                        "confirmed."
                    ),
                    "details": [
                        ("Hotel", hotel["hotel_name"] or ""),
                        ("Check-in", body.check_in_date.strftime("%d %b %Y")),
                        ("Check-out", body.check_out_date.strftime("%d %b %Y")),
                        ("Rooms", str(body.rooms_count)),
                        ("Guests", guests),
                    ],
                    "amount": float(q["total_amount"]),
                    "amount_label": "Stay amount",
                },
                related_type="HOTEL_RESERVATION",
                related_id=res_id,
            )
    except Exception:  # pragma: no cover — email must never break bookings
        pass

    return {
        "success": True,
        "data": {
            "master_booking_number": master_num,
            "hotel_booking_number": hotel_num,
            "reservation_number": res_number,
            "status": "PENDING_PAYMENT",
            "total_amount": float(q["total_amount"]),
            "nights": q["nights"],
            "rooms_count": body.rooms_count,
            "coupon_code": coupon_code_used,
            "coupon_discount": float(coupon_discount_applied),
        },
    }
