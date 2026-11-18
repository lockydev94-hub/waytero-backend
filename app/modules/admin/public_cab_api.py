# ============================================================
# WAYTERO — PUBLIC CAB SEARCH API (no auth)
# File: app/modules/admin/public_cab_api.py
# Doc Ref:
#   BRD Part 3 §35 — Fare engine
#   BRD Part 3 §36 — City-based pricing
#   DB Schema Part 4 §7  — cab_bookings
#   API Doc §7 — Create Cab Booking
#
# Endpoints (no auth required):
#   GET /public/cab/cities               — active cities + bookable supply
#   GET /public/cab/cities/{id}/fare-matrix — whole-city fare sheet at a sample km
#   GET /public/cab/vehicle-categories   — active vehicle categories
#   GET /public/cab/fare-estimate        — multi-category fare estimate
#   GET /public/cab/google-maps-key      — Maps API key (admin-controlled)
#   POST /public/cab/create-booking      — create master + cab booking (auth required)
# ============================================================

from decimal import Decimal
import json
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.timezone import get_platform_tz_sync, to_platform_tz
from app.modules.admin.schemas.landing_pages import LandingFareRow
from app.modules.admin.services import DefaultPricingService
from app.modules.booking.services.cab_availability import (
    cab_booking_disabled_message,
    filter_enabled_cab_trip_types,
    is_cab_trip_type_enabled,
)
from app.modules.booking.services.fare import calculate_fare_breakdown

router = APIRouter()


class PublicGoogleMapsKeyOut(BaseModel):
    """Returned to the public website so the Maps JS SDK can be loaded.

    The key is read from the active GOOGLE_MAPS api_integrations row, which
    is admin-controlled in Settings → API Integrations. If the admin hasn't
    configured one, ``api_key`` is null and the frontend falls back to the
    city-picker UI.
    """

    api_key: Optional[str] = None
    map_id: Optional[str] = None
    is_active: bool = False


# ── Response Schemas ──────────────────────────────────────────────────────────


class PublicCityOut(BaseModel):
    id: int
    name: str
    city_code: Optional[str] = None
    state_id: Optional[int] = None
    state_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # Bookable supply for this city, so a landing page or hub can link the cab
    # vertical only where a fare can actually be quoted.
    vehicle_count: int = 0
    has_pricing: bool = False
    # city_specific — the city has its own fare rules
    # default       — only platform-standard rules apply here
    # none          — nothing to quote
    pricing_source: Literal["city_specific", "default", "none"] = "none"


class PublicVehicleCategoryOut(BaseModel):
    id: int
    category_name: str
    seating_capacity: Optional[int] = None
    luggage_capacity: Optional[int] = None
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    display_order: int = 0


class PublicTripTypeOut(BaseModel):
    """A trip type that has at least one active fare rule in any city.

    The frontend renders these as the trip-type sub-tabs in the hero search
    form (Local / Airport / Outstation / Round Trip / etc). Only trip types
    with a configured ``base_fare`` row — i.e. bookable — are returned, so
    the customer can never pick a route that won't quote.
    """

    code: str  # LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP
    display_name: str
    description: str
    icon: str  # free-form identifier; frontend maps to a Lucide icon


class FareBreakdownItem(BaseModel):
    vehicle_category_id: int
    category_name: str
    seating_capacity: Optional[int]
    image_url: Optional[str]
    icon_url: Optional[str]
    base_fare: Decimal
    minimum_km: int
    per_km_rate: Decimal
    driver_allowance: Decimal
    night_charge_applied: Decimal
    distance_charge: Decimal
    is_night: bool
    total: Decimal
    source: str  # "city_specific" | "default"
    # Migration 0049 — used by the results page to render the correct
    # breakdown rows (billable minimum for OUTSTATION / PER_DAY labels).
    actual_distance_km: Optional[float] = None
    billable_km: Optional[float] = None
    driver_allowance_type: Optional[str] = None
    trip_days: Optional[int] = None


class PackageFareItem(BaseModel):
    """One rate-card class, priced as a package for this search.

    This is the supplier card's own vehicle: the model names, the seats, the
    included hours/kilometres, and the price the card produces for this trip.
    The results page shows these when the city has a card, because that is what
    the desk quotes and what the booking will be priced at.
    """

    vehicle_class: str
    vehicle_label: str
    vehicle_models: Optional[str] = None
    seating_capacity: Optional[int] = None
    vehicle_category_id: int
    category_name: str
    rental_type: str
    included_hours: int
    included_km: int
    days: int
    price: Decimal
    extra_hours: Decimal
    extra_hour_rate: Decimal
    extra_hour_charge: Decimal
    extra_km: Decimal
    extra_km_rate: Decimal
    extra_km_charge: Decimal
    driver_allowance: Decimal
    midnight_charge: Decimal
    total: Decimal
    rate_source: str  # CITY | PLATFORM
    toll_included: bool
    parking_included: bool
    night_halt_included: bool


class FareEstimateOut(BaseModel):
    distance_km: float
    trip_type: str
    categories: List[FareBreakdownItem]
    # Empty when the city has no rate card, in which case the per-km rows above
    # are the whole answer.
    packages: List[PackageFareItem] = []


class CabStopIn(BaseModel):
    """One place on the route — pickup, drop or a stop between them.

    ``name`` is whatever the customer typed or Google returned; coordinates are
    optional but, when present, are what let the server place the point in a
    city without trusting the spelling.
    """

    name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city_id: Optional[int] = None


class TripCheckIn(BaseModel):
    """What the live validation needs: the type and the places it claims."""

    trip_type: str
    pickup: CabStopIn
    drop: Optional[CabStopIn] = None
    stops: List[CabStopIn] = []
    pickup_datetime: Optional[str] = None
    return_datetime: Optional[str] = None
    # Only used when the route cannot be measured — a local hire with no drop.
    distance_km: Optional[float] = None


class CreateCabBookingIn(BaseModel):
    city_id: int
    trip_type: str  # LOCAL | OUTSTATION | AIRPORT | ROUND_TRIP
    # Required for PER_KM. A PACKAGE booking names its vehicle by rate class and
    # the category is derived from that class's card row.
    vehicle_category_id: Optional[int] = None
    pickup_location: str
    pickup_latitude: Optional[float] = None
    pickup_longitude: Optional[float] = None
    drop_location: Optional[str] = None
    drop_latitude: Optional[float] = None
    drop_longitude: Optional[float] = None
    # Places the car visits between pickup and drop. The server measures each
    # leg, so a detour is part of the distance rather than a free extra.
    stops: List[CabStopIn] = []
    pickup_datetime: str  # ISO format
    # Return datetime for ROUND_TRIP — required so the fare engine can
    # compute ``trip_days`` when the matched pricing rule uses
    # ``driver_allowance_type = PER_DAY``. Optional for single-day trip
    # types. Added migration 0049.
    return_datetime: Optional[str] = None
    estimated_distance_km: Optional[float] = None
    # PACKAGE prices from the rate card, the same source the results page and
    # the admin desk use. PER_KM is the older distance engine and remains the
    # default for a client that does not ask for packages.
    pricing_mode: Optional[str] = None
    vehicle_class: Optional[str] = None
    hours: Optional[float] = None
    km: Optional[float] = None
    days: Optional[int] = None
    passenger_count: Optional[int] = 1
    special_instructions: Optional[str] = None
    # Quoted upfront fare (pre-GST, pre-coupon) from /public/cab/fare-estimate.
    # Treated as a hint only — the server recomputes via calculate_fare() so a
    # tampered FE amount cannot under-price a booking. The canonical value is
    # returned in the response so the FE can show "estimated by server: ₹X".
    estimated_amount: Optional[float] = None
    # Coupon code the customer applied (optional). When present, the server
    # re-validates the coupon against the recomputed amount and persists both
    # the coupon snapshot and the discount. The customer's quote page already
    # previewed the discount; this layer makes it authoritative.
    coupon_code: Optional[str] = None
    # Coupon id at preview time (optional, helps the server skip the lookup
    # when the FE already has it — e.g. after the validate-coupon call).
    coupon_id: Optional[int] = None
    # Offer attribution hint (migration 0077) — the server resolves the real
    # offer from the validated coupon + booking target; a forged slug is
    # ignored, never trusted.
    offer_slug: Optional[str] = None


class PublicCityFareMatrixOut(BaseModel):
    """Whole-city fare sheet for one sample distance — one call, no per-pair
    round trips."""

    city_id: int
    city_name: str
    distance_km: float
    fare_from: Optional[float] = None
    vehicle_count: int = 0
    trip_types: List[str] = []
    pricing_source: Literal["city_specific", "default", "none"] = "none"
    rows: List[LandingFareRow] = []


# ── GET /public/cab/cities ────────────────────────────────────────────────────


@router.get(
    "/cities",
    response_model=List[PublicCityOut],
    summary="List active cities for cab booking (no auth)",
)
async def list_cab_cities(db: AsyncSession = Depends(get_db)):
    """Active cities with state name — used by the customer cab search form.

    Also carries the bookable-supply signal (``vehicle_count`` /
    ``pricing_source``) so city landing pages only link the cab vertical where
    a fare can actually be quoted. A city with no own rules is still bookable
    on platform-standard pricing (BRD §36), which is why ``pricing_source``
    distinguishes the two cases rather than hiding the link.
    """
    result = await db.execute(
        text(
            """
        SELECT c.id, c.name, c.city_code, c.state_id, s.name AS state_name,
               c.latitude, c.longitude,
               (
                 SELECT COUNT(*)
                   FROM vehicle_categories vc
                  WHERE vc.is_active = TRUE
                    AND (
                      EXISTS (SELECT 1 FROM vehicle_pricing_rules r
                               WHERE r.city_id = c.id
                                 AND r.vehicle_category_id = vc.id)
                      OR EXISTS (SELECT 1 FROM default_vehicle_pricing_rules d
                                  WHERE d.vehicle_category_id = vc.id)
                    )
               ) AS vehicle_count,
               EXISTS (SELECT 1 FROM vehicle_pricing_rules r
                        WHERE r.city_id = c.id) AS has_city_rules,
               EXISTS (SELECT 1 FROM default_vehicle_pricing_rules) AS has_default_rules
        FROM cities c
        LEFT JOIN states s ON s.id = c.state_id
        WHERE c.is_active = TRUE
        ORDER BY c.name
    """
        )
    )
    rows = result.fetchall()
    out: List[PublicCityOut] = []
    for r in rows:
        vehicle_count = int(r.vehicle_count or 0)
        source = (
            "city_specific"
            if r.has_city_rules
            else ("default" if vehicle_count and r.has_default_rules else "none")
        )
        out.append(
            PublicCityOut(
                id=r.id,
                name=r.name,
                city_code=r.city_code,
                state_id=r.state_id,
                state_name=r.state_name,
                latitude=float(r.latitude) if r.latitude is not None else None,
                longitude=float(r.longitude) if r.longitude is not None else None,
                vehicle_count=vehicle_count,
                has_pricing=vehicle_count > 0,
                pricing_source=source,
            )
        )
    return out


# ── GET /public/cab/cities/{city_id}/fare-matrix ──────────────────────────────


@router.get(
    "/cities/{city_id}/fare-matrix",
    response_model=PublicCityFareMatrixOut,
    summary="Full fare sheet for a city at a sample distance (no auth)",
    description=(
        "Every active vehicle category × every bookable trip type, priced "
        "through the same fare engine the booking flow uses (city-specific "
        "rule first, platform default second — BRD §35, §36). Each row keeps "
        "its `source`, so the UI can say honestly when a city is quoted on "
        "platform-standard pricing rather than its own rates. "
        "This is the SSR-safe replacement for calling /fare-estimate once per "
        "trip type from a marketing page."
    ),
)
async def get_city_fare_matrix(
    city_id: int,
    distance_km: float = Query(
        10, gt=0, le=500, description="Sample distance in km for the estimate"
    ),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.admin.services.landing_pages import LandingPageService

    city = (
        (
            await db.execute(
                text(
                    "SELECT id, name FROM cities "
                    "WHERE id = :city_id AND is_active = TRUE"
                ),
                {"city_id": city_id},
            )
        )
        .mappings()
        .first()
    )
    if not city:
        raise HTTPException(status_code=404, detail="City not found")

    # Wider than the marketing cap: this endpoint is a fare sheet, so callers
    # get every trip type the city can quote instead of a trimmed table.
    matrix = await LandingPageService.cab_fare_matrix(
        db, city_id, distance_km, max_rows=100
    )
    sources = {row["source"] for row in matrix["rows"]}
    pricing_source = (
        "city_specific"
        if "city_specific" in sources
        else ("default" if sources else "none")
    )
    return PublicCityFareMatrixOut(
        city_id=int(city["id"]),
        city_name=city["name"],
        distance_km=matrix["sample_distance_km"],
        fare_from=matrix["fare_from"],
        vehicle_count=matrix["vehicle_count"],
        trip_types=matrix["trip_types"],
        pricing_source=pricing_source,
        rows=matrix["rows"],
    )


# ── GET /public/cab/cities/{city_id}/price-list ──────────────────────────────


class PricePackageOut(BaseModel):
    """One quotable package on the rate card (Local 8h/80km, Outstation, Daily)."""

    rental_type: str
    included_hours: int
    included_km: int
    price: Decimal
    extra_hour_rate: Decimal
    extra_km_rate: Decimal
    driver_allowance: Decimal
    midnight_return_charge: Decimal
    toll_included: bool
    parking_included: bool
    night_halt_included: bool


class PriceClassOut(BaseModel):
    """One supplier vehicle class with every package the city sells for it."""

    vehicle_class: str
    vehicle_label: str
    vehicle_models: Optional[str] = None
    seating_capacity: Optional[int] = None
    vehicle_category_id: int
    category_name: str
    image_url: Optional[str] = None
    # CITY — the city's own sheet prices this class; PLATFORM — the standard
    # card does. Stated so a price page never implies a local rate that is not.
    rate_source: Literal["CITY", "PLATFORM"] = "PLATFORM"
    packages: List[PricePackageOut] = []


class PublicCabPriceListOut(BaseModel):
    """The published cab price list for one city."""

    city_id: int
    city_name: str
    currency: str = "INR"
    rental_types: List[str] = []
    classes: List[PriceClassOut] = []
    updated_at: Optional[str] = None


_RENTAL_TYPE_ORDER = {"LOCAL": 0, "OUTSTATION": 1, "DAILY": 2}


@router.get(
    "/cities/{city_id}/price-list",
    response_model=PublicCabPriceListOut,
    summary="Published cab price list for a city (no auth)",
    description=(
        "The rate card exactly as it prices a booking: every active vehicle "
        "class, with each package the city sells (included hours/kilometres, "
        "price, extra-hour and extra-km rates, and what is included). This is "
        "the same sheet `create-booking` charges from, so the price page and "
        "the checkout cannot disagree. A class priced by the platform default "
        "says so, rather than looking like a city rate."
    ),
)
async def get_cab_price_list(city_id: int, db: AsyncSession = Depends(get_db)):
    from app.modules.booking.services.rental_booking import load_rate_card

    city = (
        (
            await db.execute(
                text(
                    "SELECT id, name FROM cities "
                    "WHERE id = :city_id AND is_active = TRUE"
                ),
                {"city_id": city_id},
            )
        )
        .mappings()
        .first()
    )
    if not city:
        raise HTTPException(status_code=404, detail="City not found")

    by_class, source = await load_rate_card(db, city_id)
    categories = {
        int(row["id"]): dict(row)
        for row in (
            (
                await db.execute(
                    text(
                        "SELECT id, category_name, seating_capacity, image_url "
                        "FROM vehicle_categories WHERE is_active = TRUE"
                    )
                )
            )
            .mappings()
            .all()
        )
    }

    def _money(value) -> Decimal:
        return Decimal(str(value or 0))

    classes: List[PriceClassOut] = []
    for vehicle_class, rows in by_class.items():
        sample = rows[0]
        category_id = sample.get("vehicle_category_id")
        # A class no fleet is matched to cannot be booked, so it is not a price
        # a customer can pay and does not belong on a price page.
        if category_id is None or int(category_id) not in categories:
            continue
        category = categories[int(category_id)]
        packages = [
            PricePackageOut(
                rental_type=str(row.get("rental_type") or "").upper(),
                included_hours=int(row.get("included_hours") or 0),
                included_km=int(row.get("included_km") or 0),
                price=_money(row.get("price")),
                extra_hour_rate=_money(row.get("extra_hour_rate")),
                extra_km_rate=_money(row.get("extra_km_rate")),
                driver_allowance=_money(row.get("driver_allowance")),
                midnight_return_charge=_money(row.get("midnight_return_charge")),
                toll_included=bool(row.get("toll_included")),
                parking_included=bool(row.get("parking_included")),
                night_halt_included=bool(row.get("night_halt_included")),
            )
            for row in rows
        ]
        packages.sort(
            key=lambda p: (
                _RENTAL_TYPE_ORDER.get(p.rental_type, 9),
                p.included_hours,
                p.price,
            )
        )
        classes.append(
            PriceClassOut(
                vehicle_class=vehicle_class,
                vehicle_label=sample.get("vehicle_label") or vehicle_class,
                vehicle_models=sample.get("vehicle_models"),
                seating_capacity=sample.get("seating_capacity")
                or category.get("seating_capacity"),
                vehicle_category_id=int(category_id),
                category_name=category["category_name"],
                image_url=category.get("image_url"),
                rate_source=source.get(vehicle_class, "PLATFORM"),
                packages=packages,
            )
        )

    # Cheapest class first, the same order the results page uses.
    classes.sort(
        key=lambda c: (
            min((p.price for p in c.packages), default=Decimal("999999")),
            c.seating_capacity or 99,
        )
    )
    rental_types = sorted(
        {p.rental_type for c in classes for p in c.packages},
        key=lambda t: (_RENTAL_TYPE_ORDER.get(t, 9), t),
    )

    return PublicCabPriceListOut(
        city_id=int(city["id"]),
        city_name=city["name"],
        rental_types=rental_types,
        classes=classes,
    )


# ── GET /public/cab/vehicle-categories ───────────────────────────────────────


@router.get(
    "/vehicle-categories",
    response_model=List[PublicVehicleCategoryOut],
    summary="List active vehicle categories (no auth)",
)
async def list_cab_vehicle_categories(db: AsyncSession = Depends(get_db)):
    """Active vehicle categories with media — displayed as cab type cards."""
    result = await db.execute(
        text(
            """
        SELECT id, category_name, seating_capacity, luggage_capacity,
               image_url, icon_url, display_order
        FROM vehicle_categories
        WHERE is_active = TRUE
        ORDER BY display_order ASC, id ASC
    """
        )
    )
    rows = result.fetchall()
    return [
        PublicVehicleCategoryOut(
            id=r.id,
            category_name=r.category_name,
            seating_capacity=r.seating_capacity,
            luggage_capacity=r.luggage_capacity,
            image_url=r.image_url,
            icon_url=r.icon_url,
            display_order=r.display_order,
        )
        for r in rows
    ]


# ── GET /public/cab/cities/match ──────────────────────────────────────────────


@router.get(
    "/cities/match",
    response_model=Optional[PublicCityOut],
    summary="Find the nearest active city to a (lat, lng) — no auth",
    description=(
        "Used by the customer-web hero form to derive a `city_id` after the "
        "user picks a Google Place. Returns the single nearest active city, "
        "or null if no active city has lat/lng populated yet. Straight-line "
        "haversine is fine here — it's only used to pick a pricing bucket."
    ),
)
async def match_nearest_city(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    db: AsyncSession = Depends(get_db),
):
    """Nearest active city to (lat, lng), by straight-line distance.

    Carries the same bookable-supply signal as ``/cities``. The customer form
    calls this right after a Google Place pick and treats ``has_pricing=false``
    as "we don't serve this area" — leaving it at its default made every pick
    look unserved, Bhubaneswar included.
    """
    # Pull all active cities with coords; small list, OK to do in-process.
    result = await db.execute(
        text(
            """
            SELECT id, name, city_code, state_id, state_name, latitude, longitude,
                   (
                     (latitude - :lat) * (latitude - :lat) +
                     (longitude - :lng) * (longitude - :lng)
                   ) AS d2,
                   (
                     SELECT COUNT(*)
                       FROM vehicle_categories vc
                      WHERE vc.is_active = TRUE
                        AND (
                          EXISTS (SELECT 1 FROM vehicle_pricing_rules r
                                   WHERE r.city_id = t.id
                                     AND r.vehicle_category_id = vc.id)
                          OR EXISTS (SELECT 1 FROM default_vehicle_pricing_rules d
                                      WHERE d.vehicle_category_id = vc.id)
                        )
                   ) AS vehicle_count,
                   EXISTS (SELECT 1 FROM vehicle_pricing_rules r
                            WHERE r.city_id = t.id) AS has_city_rules,
                   EXISTS (SELECT 1 FROM default_vehicle_pricing_rules) AS has_default_rules
            FROM (
                SELECT c.id, c.name, c.city_code, c.state_id, s.name AS state_name,
                       c.latitude, c.longitude
                FROM cities c
                LEFT JOIN states s ON s.id = c.state_id
                WHERE c.is_active = TRUE
                  AND c.latitude IS NOT NULL
                  AND c.longitude IS NOT NULL
            ) AS t
            ORDER BY d2 ASC
            LIMIT 1
        """
        ),
        {"lat": lat, "lng": lng},
    )
    row = result.fetchone()
    if row is None:
        return None
    vehicle_count = int(row.vehicle_count or 0)
    source = (
        "city_specific"
        if row.has_city_rules
        else ("default" if vehicle_count and row.has_default_rules else "none")
    )
    return PublicCityOut(
        id=row.id,
        name=row.name,
        city_code=row.city_code,
        state_id=row.state_id,
        state_name=row.state_name,
        latitude=float(row.latitude) if row.latitude is not None else None,
        longitude=float(row.longitude) if row.longitude is not None else None,
        vehicle_count=vehicle_count,
        has_pricing=vehicle_count > 0,
        pricing_source=source,
    )


# ── GET /public/cab/trip-types ────────────────────────────────────────────────


# Display metadata for each trip type code — what the customer sees in the UI.
# Frontend falls back to its own icon map for rendering.
_TRIP_TYPE_META: dict[str, tuple[str, str, str]] = {
    # code        display_name        description                       icon
    "LOCAL": ("Local", "In-city rides", "Car"),
    "AIRPORT": ("Airport", "Airport transfers", "Plane"),
    "OUTSTATION": ("Outstation", "One-way inter-city", "Navigation"),
    "ONE_WAY": ("One Way", "Drop-only inter-city", "ArrowRight"),
    "ROUND_TRIP": ("Round Trip", "Return journey", "Repeat2"),
    # The rate card's daily product. Sold per day (12h/100km each, driver
    # allowance and night halt included), so the customer buys days, not a
    # route: pickup plus when the rental ends.
    "DAILY": (
        "Cab Rent",
        "Daily rental with driver — 12h / 100km per day",
        "CalendarClock",
    ),
}


@router.get(
    "/trip-types",
    response_model=List[PublicTripTypeOut],
    summary="List active cab trip types that can be booked (no auth)",
    description=(
        "Returns the trip types that have at least one *active* pricing rule "
        "configured (either in a city-specific rule or in the platform "
        "default). The customer-web hero form renders these as the trip-type "
        "sub-tabs under the Cab tab. Only bookable trip types are returned."
    ),
)
async def list_active_trip_types(db: AsyncSession = Depends(get_db)):
    """Active trip types = union of (city-specific rules ∪ default rules).

    Doc Ref: BRD Part 3 §35 + §36 — fare engine uses default rules when no
    city-specific rule is found, so a trip type with only default rules is
    still bookable everywhere.
    """
    # One query — union distinct trip_types from both rule tables.
    result = await db.execute(
        text(
            """
            SELECT trip_type FROM vehicle_pricing_rules
            UNION
            SELECT trip_type FROM default_vehicle_pricing_rules
        """
        )
    )
    codes = sorted({row.trip_type for row in result.fetchall() if row.trip_type})

    # Cab Rent is priced from the rental rate card, not the per-km rule tables,
    # so its trip type would otherwise never be offered even when the card sells
    # daily packages. It is listed when at least one active card has them.
    if "DAILY" not in codes:
        has_daily_card = (
            await db.execute(
                text(
                    "SELECT 1 FROM cab_rental_rates "
                    "WHERE rental_type = 'DAILY' AND is_active = TRUE LIMIT 1"
                )
            )
        ).scalar()
        if has_daily_card:
            codes = sorted([*codes, "DAILY"])

    # The admin's settings are the list: a type switched off in Settings →
    # Cab Booking is not returned to the website, the customer app or sitemap.
    codes = await filter_enabled_cab_trip_types(db, codes)

    out: list[PublicTripTypeOut] = []
    for code in codes:
        meta = _TRIP_TYPE_META.get(code)
        if not meta:
            # Unknown trip type — still expose it, but with a sanitized label
            # derived from the code so the UI doesn't show raw enum strings.
            label = code.replace("_", " ").title()
            out.append(
                PublicTripTypeOut(
                    code=code,
                    display_name=label,
                    description=label,
                    icon="Navigation",
                )
            )
            continue
        display, desc, icon = meta
        out.append(
            PublicTripTypeOut(
                code=code,
                display_name=display,
                description=desc,
                icon=icon,
            )
        )
    return out


# ── Package fares (rate card) ────────────────────────────────────────────────


def rental_type_for(trip_type: str, days: int) -> str:
    """Which card product a website trip type is sold as.

    Airport transfers are a same-city hire, so they price as LOCAL. A one-way or
    same-day return is the outstation package. A round trip that spans days is
    the daily product, which is the only one the card sells per day.
    """
    trip = (trip_type or "").upper()
    if trip in {"DAILY", "RENTAL"}:
        return "DAILY"
    if trip in {"OUTSTATION", "ONE_WAY"}:
        return "OUTSTATION"
    if trip == "ROUND_TRIP":
        return "DAILY" if (days or 1) > 1 else "OUTSTATION"
    return "LOCAL"


async def _package_fares(
    db: AsyncSession,
    *,
    city_id: int,
    distance_km: float,
    trip_type: str,
    days: int,
) -> List[PackageFareItem]:
    """Every class on the card, priced for this search as a package.

    The website does not ask for duty hours, so the card's own package limits are
    used (5h/50km, 8h/80km, 12h/250km, …) and the searched distance is billed
    against them. Nothing here is a made-up figure: each row is
    ``calculate_rental_fare``'s answer for that class.
    """
    from app.modules.booking.services.rental_booking import load_rate_card
    from app.modules.booking.services.rental_rates import calculate_rental_fare

    by_class, source = await load_rate_card(db, city_id)
    if not by_class:
        return []

    categories = (
        (
            await db.execute(
                text(
                    "SELECT id, category_name, seating_capacity "
                    "FROM vehicle_categories WHERE is_active = TRUE"
                )
            )
        )
        .mappings()
        .all()
    )
    category_by_id = {int(row["id"]): dict(row) for row in categories}

    rental_type = rental_type_for(trip_type, days)
    items: List[PackageFareItem] = []
    for vehicle_class, rows in by_class.items():
        if not any(
            str(row.get("rental_type") or "").upper() == rental_type for row in rows
        ):
            continue
        sample = rows[0]
        category_id = sample.get("vehicle_category_id")
        # A class with no bookable category cannot be offered: nothing in a
        # partner's fleet is matched to it.
        if category_id is None or int(category_id) not in category_by_id:
            continue
        quote = calculate_rental_fare(
            rows,
            rental_type=rental_type,
            hours=0,
            km=distance_km,
            days=max(1, days or 1),
        )
        category = category_by_id[int(category_id)]
        items.append(
            PackageFareItem(
                vehicle_class=vehicle_class,
                vehicle_label=sample.get("vehicle_label") or vehicle_class,
                vehicle_models=sample.get("vehicle_models"),
                seating_capacity=sample.get("seating_capacity")
                or category.get("seating_capacity"),
                vehicle_category_id=int(category_id),
                category_name=category["category_name"],
                rental_type=rental_type,
                included_hours=quote.package_hours,
                included_km=quote.package_km,
                days=quote.days,
                price=quote.package_price,
                extra_hours=quote.extra_hours,
                extra_hour_rate=quote.extra_hour_rate,
                extra_hour_charge=quote.extra_hour_charge,
                extra_km=quote.extra_km,
                extra_km_rate=quote.extra_km_rate,
                extra_km_charge=quote.extra_km_charge,
                driver_allowance=quote.driver_allowance,
                midnight_charge=quote.midnight_charge,
                total=quote.total,
                rate_source=source.get(vehicle_class, "PLATFORM"),
                toll_included=quote.toll_included,
                parking_included=quote.parking_included,
                night_halt_included=quote.night_halt_included,
            )
        )

    # Cheapest first, the same order the per-km rows are presented in.
    return sorted(items, key=lambda item: item.total)


# ── POST /public/cab/trip-check ───────────────────────────────────────────────


@router.post(
    "/trip-check",
    summary="Does the chosen trip type match these places? (no auth)",
    description=(
        "The live guard behind both the website and the phone desk. It resolves "
        "each place to a city, says whether the picked trip type agrees with "
        "that (a Local trip cannot end in another city), measures the route "
        "including any stops, and returns an estimated drive time with the "
        "hints that follow from it. Blocking errors mean the booking must not "
        "be created under this type; create-booking runs the same check again."
    ),
)
async def check_public_trip(body: TripCheckIn, db: AsyncSession = Depends(get_db)):
    from datetime import datetime

    from app.modules.booking.services.trip_guard import TripPoint, check_trip

    body.trip_type = body.trip_type.strip().upper()
    if not await is_cab_trip_type_enabled(db, body.trip_type):
        raise HTTPException(
            status_code=400,
            detail=cab_booking_disabled_message(body.trip_type),
        )

    def _dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    result = await check_trip(
        db,
        trip_type=body.trip_type,
        pickup=TripPoint(
            name=body.pickup.name,
            latitude=body.pickup.latitude,
            longitude=body.pickup.longitude,
            city_id=body.pickup.city_id,
        ),
        drop=(
            TripPoint(
                name=body.drop.name,
                latitude=body.drop.latitude,
                longitude=body.drop.longitude,
                city_id=body.drop.city_id,
            )
            if body.drop and body.drop.name
            else None
        ),
        stops=[
            TripPoint(
                name=s.name,
                latitude=s.latitude,
                longitude=s.longitude,
                city_id=s.city_id,
            )
            for s in body.stops
            if s.name
        ],
        pickup_datetime=_dt(body.pickup_datetime),
        return_datetime=_dt(body.return_datetime),
        fallback_distance_km=body.distance_km,
    )
    return {"success": True, "message": "Trip checked.", "data": result.as_dict()}


# ── GET /public/cab/fare-estimate ─────────────────────────────────────────────


@router.get(
    "/fare-estimate",
    response_model=FareEstimateOut,
    summary="Get fare estimate for all vehicle categories (no auth)",
    description=(
        "Returns a fare breakdown for every active vehicle category "
        "for the given city, distance, and trip type. "
        "Uses city-specific pricing with fallback to default (BRD §35, §36). "
        "Pickup datetime is used for night charge calculation."
    ),
)
async def get_fare_estimate(
    city_id: int = Query(..., description="City ID"),
    distance_km: float = Query(..., description="Estimated distance in km"),
    trip_type: str = Query(
        "LOCAL", description="LOCAL | OUTSTATION | AIRPORT | ROUND_TRIP"
    ),
    pickup_datetime: Optional[str] = Query(
        None, description="ISO datetime for night charge calc"
    ),
    return_datetime: Optional[str] = Query(
        None,
        description="ISO datetime for ROUND_TRIP — lets the fare engine compute "
        "trip_days when the rule uses driver_allowance_type = PER_DAY",
    ),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime

    from app.modules.booking.services.fare import trip_days_from

    trip_type = trip_type.strip().upper()
    if not await is_cab_trip_type_enabled(db, trip_type):
        raise HTTPException(
            status_code=400,
            detail=cab_booking_disabled_message(trip_type),
        )

    # Parse pickup datetime for night charge
    start_dt = None
    if pickup_datetime:
        try:
            start_dt = datetime.fromisoformat(pickup_datetime.replace("Z", "+00:00"))
        except ValueError:
            start_dt = None

    # Parse return datetime for PER_DAY trip-days on ROUND_TRIP
    end_dt = None
    if return_datetime:
        try:
            end_dt = datetime.fromisoformat(return_datetime.replace("Z", "+00:00"))
        except ValueError:
            end_dt = None
    days = trip_days_from(start_dt, end_dt)
    rental_days = days
    if trip_type == "DAILY":
        # A Cab Rent is sold in 24-hour periods, not inclusive calendar days.
        from app.modules.booking.services.fare import rental_days_from

        rental_days = rental_days_from(start_dt, end_dt)
    # The search page quotes for a trip on a calendar day, so it must read the
    # fare sheet that is in force on that day. `start_dt` is the customer's chosen
    # pickup; with no pickup time the request's own day is all there is to go on.
    # Booking creation passes the same day, so a quoted figure and the price the
    # booking computes cannot come from two different sheets.
    estimate_day = (
        to_platform_tz(start_dt).date()
        if start_dt
        else datetime.now(get_platform_tz_sync()).date()
    )

    # Get active vehicle categories
    result = await db.execute(
        text(
            """
        SELECT id, category_name, seating_capacity, image_url, icon_url
        FROM vehicle_categories
        WHERE is_active = TRUE
        ORDER BY display_order ASC, id ASC
    """
        )
    )
    categories = result.fetchall()

    items: List[FareBreakdownItem] = []
    for cat in categories:
        rule = await DefaultPricingService.get_effective_rule(
            db, city_id, cat.id, trip_type, for_date=estimate_day
        )
        if not rule:
            continue

        breakdown = calculate_fare_breakdown(
            rule=rule,
            actual_distance_km=distance_km,
            start_dt=start_dt,
            end_dt=end_dt,
            trip_type=trip_type,
            trip_days=days,
        )

        items.append(
            FareBreakdownItem(
                vehicle_category_id=cat.id,
                category_name=cat.category_name,
                seating_capacity=cat.seating_capacity,
                image_url=cat.image_url,
                icon_url=cat.icon_url,
                base_fare=breakdown["base_fare"],
                minimum_km=breakdown["minimum_km"],
                per_km_rate=breakdown["per_km_rate"],
                driver_allowance=breakdown["driver_allowance"],
                night_charge_applied=breakdown["night_charge_applied"],
                distance_charge=breakdown["distance_charge"],
                is_night=breakdown["is_night"],
                total=breakdown["total"],
                source=rule.get("source", "default"),
                actual_distance_km=breakdown["actual_distance_km"],
                billable_km=breakdown["billable_km"],
                driver_allowance_type=breakdown["driver_allowance_type"],
                trip_days=breakdown["trip_days"],
            )
        )

    packages = await _package_fares(
        db,
        city_id=city_id,
        distance_km=distance_km,
        trip_type=trip_type,
        days=rental_days,
    )

    return FareEstimateOut(
        distance_km=distance_km,
        trip_type=trip_type,
        categories=items,
        packages=packages,
    )


# ── GET /public/cab/google-maps-key ───────────────────────────────────────────


@router.get(
    "/google-maps-key",
    response_model=PublicGoogleMapsKeyOut,
    summary="Get the admin-configured Google Maps API key (no auth)",
    description=(
        "Returns the API key + map_id from the active GOOGLE_MAPS "
        "api_integrations row so customer-web can bootstrap the Maps JS SDK. "
        "Returns null api_key when the admin hasn't configured Maps; the "
        "frontend treats that as 'use city fallback picker'."
    ),
)
async def get_public_google_maps_key(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(
            text(
                """
            SELECT configuration, is_active
            FROM api_integrations
            WHERE service_type = 'GOOGLE_MAPS'
            LIMIT 1
        """
            )
        )
        row = result.mappings().one_or_none()
    except Exception:
        # Table may not exist yet on a fresh DB; surface as "not configured"
        return PublicGoogleMapsKeyOut(api_key=None, map_id=None, is_active=False)

    if not row or not row.get("is_active"):
        return PublicGoogleMapsKeyOut(api_key=None, map_id=None, is_active=False)

    cfg = row.get("configuration") or {}
    if not isinstance(cfg, dict):
        return PublicGoogleMapsKeyOut(api_key=None, map_id=None, is_active=False)

    api_key = (cfg.get("api_key") or "").strip() or None
    map_id = (cfg.get("map_id") or "").strip() or None
    return PublicGoogleMapsKeyOut(api_key=api_key, map_id=map_id, is_active=True)


# ── POST /public/cab/create-booking ──────────────────────────────────────────


@router.post(
    "/create-booking",
    summary="Create master booking + cab service (auth required)",
    description=(
        "Creates a master booking (DRAFT) and attaches a cab service. "
        "Returns booking_number and cab_booking_number for the results page. "
        "Customer must be authenticated."
    ),
)
async def create_cab_booking(
    body: CreateCabBookingIn,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid
    from datetime import datetime, timezone

    from app.shared.utilities.mobile import require_real_mobile

    # A hand-built POST can name a type the website no longer shows. The
    # setting is enforced here, before any customer or fare work.
    body.trip_type = body.trip_type.strip().upper()
    if not await is_cab_trip_type_enabled(db, body.trip_type):
        raise HTTPException(
            status_code=400,
            detail=cab_booking_disabled_message(body.trip_type),
        )

    await require_real_mobile(db, current_user["sub"])

    # Resolve customer_id from user UUID, auto-creating the profile on first
    # booking. Mobile-OTP login creates the customer row inline; Firebase/email
    # login does not — so the first booking through this path will lazily
    # mint a customer profile + wallet so the booking can be filed.
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
        # Look up the user to seed the customer name.
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
        new_customer_uuid = str(_uuid.uuid4())

        first_name = user_row.get("first_name") if user_row else None
        last_name = user_row.get("last_name") if user_row else None

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
                    "uuid": new_customer_uuid,
                    "user_id": user_uuid_str,
                    "code": cust_code,
                    "first_name": first_name,
                    "last_name": last_name,
                    "city_id": body.city_id,
                },
            )
        ).fetchone()
        if inserted is None:
            raise RuntimeError("Customer row could not be created for booking")
        customer_id = inserted.id

        # Mirror the pattern used by customer_care_api / customer service:
        # every customer gets an ACTIVE wallet row with zero balance.
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

    # Generate booking numbers
    ts = datetime.utcnow()
    seq_result = await db.execute(text("SELECT nextval('master_bookings_id_seq')"))
    seq = seq_result.scalar()
    master_num = f"WT-MTB-{ts.year}-{seq:06d}"
    cab_num = f"WT-CAB-{ts.year}-{seq:06d}"

    master_uuid = str(_uuid.uuid4())
    cab_uuid = str(_uuid.uuid4())

    # Parse datetime
    try:
        pickup_dt = datetime.fromisoformat(body.pickup_datetime.replace("Z", "+00:00"))
    except Exception:
        pickup_dt = datetime.utcnow()

    # Parse return_datetime (used to compute trip_days for PER_DAY
    # driver-allowance rules on ROUND_TRIP). Migration 0049.
    return_dt: Optional[datetime] = None
    if body.return_datetime:
        try:
            return_dt = datetime.fromisoformat(
                body.return_datetime.replace("Z", "+00:00")
            )
        except ValueError:
            return_dt = None

    # How many days the trip spans — the daily package is sold per day, and the
    # per-km engine uses the same number for per-day allowances.
    from app.modules.booking.services.fare import trip_days_from

    days = trip_days_from(pickup_dt, return_dt)
    if body.trip_type == "DAILY":
        from app.modules.booking.services.fare import rental_days_from

        days = body.days or rental_days_from(pickup_dt, return_dt)

    # ── Does the booking type match the places? ────────────────────────────
    # The page runs this same check before it lets a card be clicked, but the
    # browser's answer is not authority: a hand-built POST could still send a
    # Local booking ending in another city, and it would be priced on the local
    # sheet. The check is repeated here and blocks the booking.
    from app.modules.booking.services.trip_guard import TripPoint, check_trip

    trip_check = await check_trip(
        db,
        trip_type=body.trip_type,
        pickup=TripPoint(
            name=body.pickup_location,
            latitude=body.pickup_latitude,
            longitude=body.pickup_longitude,
            city_id=body.city_id,
        ),
        drop=(
            TripPoint(
                name=body.drop_location,
                latitude=body.drop_latitude,
                longitude=body.drop_longitude,
            )
            if body.drop_location
            else None
        ),
        stops=[
            TripPoint(name=s.name, latitude=s.latitude, longitude=s.longitude)
            for s in body.stops
            if s.name
        ],
        pickup_datetime=pickup_dt,
        return_datetime=return_dt,
        fallback_distance_km=body.estimated_distance_km,
    )
    if trip_check.errors:
        raise HTTPException(status_code=400, detail=" ".join(trip_check.errors))

    # ── Server-side price of the trip ──────────────────────────────────────
    # This used to recompute the fare from `body.estimated_distance_km`, i.e.
    # from the browser's own number. Recomputing a tampered input is arithmetic,
    # not authority: halve the kilometres and the server faithfully halved the
    # fare, with nothing on the row to show who said it. The trip is now measured
    # backend-side — a published corridor's distance first, then Google — and the
    # client's figure survives only as the labelled fallback for a hire with no
    # destination to measure against.
    # Doc Ref: BRD Part 3 §35 — fare formula; Round 15 plan C2.
    from app.modules.booking.services.trip_pricing import (
        fare_columns,
        quote_cab_trip,
    )

    server_warning: Optional[str] = None
    # A rent has no per-km rule to fall back on: the card is the only place its
    # price exists, so an unstated mode on a Cab Rent booking means package.
    default_mode = "PACKAGE" if (body.trip_type or "").upper() == "DAILY" else "PER_KM"
    pricing_mode = (body.pricing_mode or default_mode).upper()
    if pricing_mode not in {"PACKAGE", "PER_KM"}:
        raise HTTPException(status_code=400, detail="Unknown pricing mode.")

    if return_dt is not None:
        back = return_dt if return_dt.tzinfo else return_dt.replace(tzinfo=timezone.utc)
        ahead = (
            pickup_dt if pickup_dt.tzinfo else pickup_dt.replace(tzinfo=timezone.utc)
        )
        if back <= ahead:
            raise HTTPException(
                status_code=400,
                detail="The return time is before the pickup time.",
            )

    quote = None
    if pricing_mode == "PACKAGE":
        # The page quoted a rate-card package, so the booking is priced by that
        # same calculator. The class is what was selected, not the category —
        # the card's class is the vehicle the customer was shown.
        from app.modules.booking.services.rental_booking import (
            quote_rental_package,
            rental_fare_columns,
        )

        if not body.vehicle_class:
            raise HTTPException(
                status_code=400, detail="Choose a vehicle for a package booking."
            )
        package_quote = await quote_rental_package(
            db,
            city_id=body.city_id,
            vehicle_class=body.vehicle_class,
            rental_type=rental_type_for(body.trip_type, days),
            hours=body.hours or 0,
            km=body.km if body.km is not None else (body.estimated_distance_km or 0),
            days=max(1, body.days or days or 1),
            pickup_datetime=pickup_dt,
        )
        if package_quote.errors:
            raise HTTPException(
                status_code=400,
                detail=" ".join(v.message for v in package_quote.errors),
            )
        server_estimated_amount = package_quote.total
        vehicle_category_id = package_quote.vehicle_category_id
        booking_trip_type = package_quote.rental_type
        distance_km = package_quote.km
        distance_source = "PACKAGE"
        fare_cols = rental_fare_columns(package_quote)
    else:
        if not body.vehicle_category_id:
            raise HTTPException(status_code=400, detail="Choose a vehicle category.")
        quote = await quote_cab_trip(
            db,
            pickup_location=body.pickup_location,
            drop_location=body.drop_location or "",
            origin_city_id=body.city_id,
            vehicle_category_id=body.vehicle_category_id,
            trip_type=body.trip_type,
            pickup_datetime=pickup_dt,
            return_datetime=return_dt,
            fallback_distance_km=(
                Decimal(str(body.estimated_distance_km))
                if body.estimated_distance_km
                else None
            ),
            # A stop is a real leg of the journey, so the server measures it
            # rather than pricing the direct pickup→drop line.
            stops=[s.name for s in body.stops if s.name],
        )
        server_estimated_amount = quote.total
        vehicle_category_id = body.vehicle_category_id
        booking_trip_type = body.trip_type
        distance_km = quote.distance.km
        distance_source = quote.distance.source
        fare_cols = fare_columns(quote)

    fe_amount = Decimal(str(body.estimated_amount)) if body.estimated_amount else None
    if fe_amount is not None and abs(fe_amount - server_estimated_amount) > Decimal(
        "0.50"
    ):
        # >₹0.50 deviation — surface as a warning. We don't reject because
        # legitimate re-renders of the search page can show slightly
        # different numbers (e.g. night-charge flip on second-pass).
        server_warning = (
            f"Server fare ₹{server_estimated_amount:.2f} differs from "
            f"client-quoted ₹{fe_amount:.2f}. Using server value."
        )
    if quote is not None and quote.distance.source == "CLIENT_STATED":
        # The customer is being charged on a number nobody measured. Say so in
        # the same field the page already reads, rather than letting the total
        # look as final as a corridor fare.
        server_warning = (
            (server_warning + " " if server_warning else "")
            + "Distance taken from the booking form — no destination was given to measure."
        )

    # ── Coupon validation + persistence ──────────────────────────────────
    # Re-validate the coupon against the SERVER-recomputed amount so a tampered
    # FE (e.g. forged coupon discount) cannot bypass the cap.
    coupon_discount_applied = Decimal("0.00")
    coupon_id_used: Optional[int] = None
    coupon_code_used: Optional[str] = None
    offer_id_used: Optional[int] = None
    if body.coupon_code:
        from app.modules.admin.coupon_api import (
            ValidateRequest,
            record_coupon_usage,
            validate_coupon,
        )

        # Look up the customer's mobile for the whitelist check (best-effort).
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

        # reserve=True locks the coupon (and, for partner-funded coupons, the
        # cohort partners) for this transaction so concurrent redemptions
        # cannot overshoot the usage cap or double-book the same fleet.
        vres = await validate_coupon(
            ValidateRequest(
                coupon_code=body.coupon_code,
                service_type="CAB",
                vehicle_category_id=vehicle_category_id,
                city_id=body.city_id,
                booking_amount=server_estimated_amount,
                customer_id=customer_id,
                customer_mobile=cust_mobile,
                pickup_datetime=pickup_dt,
                return_datetime=return_dt,
            ),
            db,
            reserve=True,
        )
        if not vres.valid:
            raise HTTPException(
                status_code=409,
                detail=f"Coupon not applied: {vres.message}",
            )
        coupon_discount_applied = Decimal(str(vres.discount_amount))
        coupon_code_used = body.coupon_code.upper().strip()
        coupon_id_used = vres.coupon_id
        from app.modules.admin.offer_service import resolve_offer_for_booking

        offer_id_used = await resolve_offer_for_booking(
            db,
            coupon_id=coupon_id_used,
            service_type="CAB",
            vehicle_category_id=vehicle_category_id,
            hotel_id=None,
            room_category_id=None,
            tour_package_id=None,
        )

    # 1. Insert master booking — total_amount is the pre-coupon fare so the
    # master row stays the "fare" view. The coupon discount is tracked on
    # cab_bookings (and on coupon_usages) so partner close-trip / invoice
    # math (which already reads coupon_usages) keeps working.
    await db.execute(
        text(
            """
        INSERT INTO master_bookings (
            uuid, booking_number, customer_id, city_id,
            booking_status, payment_status, total_amount,
            journey_start_date, created_at, updated_at
        ) VALUES (
            :uuid, :booking_number, :customer_id, :city_id,
            'DRAFT', 'PENDING', :estimated,
            :jstart, NOW(), NOW()
        )
    """
        ),
        {
            "uuid": master_uuid,
            "booking_number": master_num,
            "customer_id": customer_id,
            "city_id": body.city_id,
            "estimated": server_estimated_amount,
            "jstart": pickup_dt.date(),
        },
    )

    # Get master booking id
    mbr = await db.execute(
        text("SELECT id FROM master_bookings WHERE uuid = :uuid"), {"uuid": master_uuid}
    )
    master_id = mbr.scalar()

    # The route and the trip-type verdict are part of the booking's story: the
    # legs explain the distance, and the verdict records that the chosen type
    # was checked against the places rather than assumed.
    snapshot = dict(fare_cols.get("rate_snapshot") or {})
    snapshot["stops"] = [s.name for s in body.stops if s.name]
    snapshot["trip_validation"] = {
        "checked": True,
        "detected_type": trip_check.detected_type,
        "cities": trip_check.cities,
        "warnings": trip_check.warnings,
    }
    fare_cols["rate_snapshot"] = snapshot

    # 2. Insert cab booking — includes the persisted coupon snapshot and the
    #    server's own fare lines. The lines are stored rather than re-derived at
    #    invoice time so the page the customer agreed to, the invoice and the
    #    settlement line are provably the same arithmetic.
    await db.execute(
        text(
            """
        INSERT INTO cab_bookings (
            uuid, master_booking_id, booking_number,
            trip_type, vehicle_category_id,
            pickup_location, pickup_latitude, pickup_longitude,
            drop_location, drop_latitude, drop_longitude,
            stops, estimated_duration_minutes,
            pickup_datetime, return_datetime, estimated_distance, estimated_amount,
            coupon_code, coupon_id, coupon_discount, offer_id,
            pricing_city_id, distance_source, billing_distance_km,
            base_fare_amount, distance_charge_amount, driver_allowance_amount,
            night_charge_amount, toll_amount, waiting_charge_amount, rate_snapshot,
            booking_status, created_at
        ) VALUES (
            :uuid, :master_id, :booking_number,
            :trip_type, :vehicle_category_id,
            :pickup_location, :pickup_lat, :pickup_lng,
            :drop_location, :drop_lat, :drop_lng,
            CAST(:stops AS jsonb), :duration_minutes,
            :pickup_datetime, :return_datetime, :distance, :estimated,
            :coupon_code, :coupon_id, :coupon_discount, :offer_id,
            :pricing_city_id, :distance_source, :billing_distance_km,
            :base_fare_amount, :distance_charge_amount, :driver_allowance_amount,
            :night_charge_amount, :toll_amount, :waiting_charge_amount,
            CAST(:rate_snapshot AS jsonb),
            'PENDING_ASSIGNMENT', NOW()
        )
    """
        ),
        {
            "uuid": cab_uuid,
            "master_id": master_id,
            "booking_number": cab_num,
            "trip_type": booking_trip_type,
            "vehicle_category_id": vehicle_category_id,
            "pickup_location": body.pickup_location,
            "pickup_lat": body.pickup_latitude,
            "pickup_lng": body.pickup_longitude,
            "drop_location": body.drop_location,
            "drop_lat": body.drop_latitude,
            "drop_lng": body.drop_longitude,
            "stops": json.dumps([s.model_dump() for s in body.stops if s.name]),
            "duration_minutes": trip_check.duration_minutes,
            "pickup_datetime": pickup_dt,
            "return_datetime": return_dt,
            "distance": distance_km,
            "estimated": server_estimated_amount,
            "coupon_code": coupon_code_used,
            "coupon_id": coupon_id_used,
            "coupon_discount": coupon_discount_applied,
            "offer_id": offer_id_used,
            **{
                k: (json.dumps(v) if k == "rate_snapshot" else v)
                for k, v in fare_cols.items()
            },
        },
    )

    # 3. Insert booking_services link
    await db.execute(
        text(
            """
        INSERT INTO booking_services (
            master_booking_id, service_type, service_reference_id,
            service_status, created_at
        )
        SELECT :master_id, 'CAB', id, 'PENDING_ASSIGNMENT', NOW()
        FROM cab_bookings WHERE uuid = :cab_uuid
    """
        ),
        {"master_id": master_id, "cab_uuid": cab_uuid},
    )

    # 4. Record coupon usage (coupon_usages is the source of truth for
    # close-trip / invoice math, the per-coupon usage counter, and the
    # immutable funding/cohort snapshot the assignment lock reads).
    if coupon_id_used:
        cab_id_row = await db.execute(
            text("SELECT id FROM cab_bookings WHERE uuid = :uuid"),
            {"uuid": cab_uuid},
        )
        cab_id = cab_id_row.scalar()
        await record_coupon_usage(
            db,
            validation=vres,
            customer_id=customer_id,
            master_booking_id=master_id,
            cab_booking_id=cab_id,
        )

    await db.commit()

    # ── Realtime: a new customer cab booking needs admin acceptance ──
    # Notifies every active admin user (WS popup + ringtone when online;
    # durable inbox rows + the accept-queue when they log in later).
    try:
        from app.modules.notification.services.booking_notifications import (
            admin_booking_requested,
        )

        cab_id_row = await db.execute(
            text("SELECT id FROM cab_bookings WHERE uuid = :uuid"),
            {"uuid": cab_uuid},
        )
        cab_id = cab_id_row.scalar()
        await admin_booking_requested(
            db,
            master_booking_id=master_id,
            service_type="CAB",
            booking_number=cab_num,
            title=f"New cab booking: {cab_num}",
            body=(
                f"Pickup: {body.pickup_location}. Trip: {body.trip_type}. "
                f"Estimated: ₹{float(server_estimated_amount):,.2f}. "
                f"Accept to start assigning a partner."
            ),
            data={
                "service_id": cab_id,
                "pickup_location": body.pickup_location,
                "pickup_datetime": body.pickup_datetime,
                "trip_type": body.trip_type,
                "amount": float(server_estimated_amount),
            },
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
            cab_id_row = await db.execute(
                text("SELECT id FROM cab_bookings WHERE uuid = :uuid"),
                {"uuid": cab_uuid},
            )
            await send_event_email(
                db,
                event_type="booking_requested",
                to_email=cust_row["email"],
                to_name=cust_name,
                context={
                    "name": cust_name,
                    "service": "Cab",
                    "booking_number": cab_num,
                    "message": (
                        "We've received your cab booking and are confirming your ride. "
                        "You'll get another email once it's confirmed."
                    ),
                    "details": [
                        ("Trip type", body.trip_type or ""),
                        ("Pickup", body.pickup_location or ""),
                        ("Drop", body.drop_location or ""),
                        ("Pickup time", pickup_dt.strftime("%d %b %Y, %I:%M %p")),
                    ],
                    "amount": float(server_estimated_amount),
                    "amount_label": "Estimated fare",
                },
                related_type="CAB_BOOKING",
                related_id=cab_id_row.scalar(),
            )
    except Exception:  # pragma: no cover — email must never break bookings
        pass

    return {
        "success": True,
        "data": {
            "master_booking_number": master_num,
            "cab_booking_number": cab_num,
            "status": "DRAFT",
            "estimated_amount": float(server_estimated_amount),
            # So the page can say where the kilometres came from instead of
            # presenting an estimated fare as though it were measured.
            "distance_km": float(distance_km),
            "distance_source": distance_source,
            "coupon_code": coupon_code_used,
            "coupon_discount": float(coupon_discount_applied),
            "warning": server_warning,
        },
    }
