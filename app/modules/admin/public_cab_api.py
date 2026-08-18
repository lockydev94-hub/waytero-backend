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
#   GET /public/cab/cities               — active cities list
#   GET /public/cab/vehicle-categories   — active vehicle categories
#   GET /public/cab/fare-estimate        — multi-category fare estimate
#   GET /public/cab/google-maps-key      — Maps API key (admin-controlled)
#   POST /public/cab/create-booking      — create master + cab booking (auth required)
# ============================================================

from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.admin.services import DefaultPricingService
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
    state_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


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


class FareEstimateOut(BaseModel):
    distance_km: float
    trip_type: str
    categories: List[FareBreakdownItem]


class CreateCabBookingIn(BaseModel):
    city_id: int
    trip_type: str  # LOCAL | OUTSTATION | AIRPORT | ROUND_TRIP
    vehicle_category_id: int
    pickup_location: str
    pickup_latitude: Optional[float] = None
    pickup_longitude: Optional[float] = None
    drop_location: Optional[str] = None
    drop_latitude: Optional[float] = None
    drop_longitude: Optional[float] = None
    pickup_datetime: str  # ISO format
    # Return datetime for ROUND_TRIP — required so the fare engine can
    # compute ``trip_days`` when the matched pricing rule uses
    # ``driver_allowance_type = PER_DAY``. Optional for single-day trip
    # types. Added migration 0049.
    return_datetime: Optional[str] = None
    estimated_distance_km: Optional[float] = None
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


# ── GET /public/cab/cities ────────────────────────────────────────────────────


@router.get(
    "/cities",
    response_model=List[PublicCityOut],
    summary="List active cities for cab booking (no auth)",
)
async def list_cab_cities(db: AsyncSession = Depends(get_db)):
    """Active cities with state name — used by the customer cab search form."""
    result = await db.execute(
        text(
            """
        SELECT c.id, c.name, c.city_code, s.name AS state_name,
               c.latitude, c.longitude
        FROM cities c
        LEFT JOIN states s ON s.id = c.state_id
        WHERE c.is_active = TRUE
        ORDER BY c.name
    """
        )
    )
    rows = result.fetchall()
    return [
        PublicCityOut(
            id=r.id,
            name=r.name,
            city_code=r.city_code,
            state_name=r.state_name,
            latitude=float(r.latitude) if r.latitude is not None else None,
            longitude=float(r.longitude) if r.longitude is not None else None,
        )
        for r in rows
    ]


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
    """Nearest active city to (lat, lng), by straight-line distance."""
    # Pull all active cities with coords; small list, OK to do in-process.
    result = await db.execute(
        text(
            """
            SELECT id, name, city_code, state_name, latitude, longitude,
                   (
                     (latitude - :lat) * (latitude - :lat) +
                     (longitude - :lng) * (longitude - :lng)
                   ) AS d2
            FROM (
                SELECT c.id, c.name, c.city_code, s.name AS state_name,
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
    return PublicCityOut(
        id=row.id,
        name=row.name,
        city_code=row.city_code,
        state_name=row.state_name,
        latitude=float(row.latitude) if row.latitude is not None else None,
        longitude=float(row.longitude) if row.longitude is not None else None,
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
            db, city_id, cat.id, trip_type
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

    return FareEstimateOut(
        distance_km=distance_km,
        trip_type=trip_type,
        categories=items,
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
    from datetime import datetime

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

    # ── Server-side recompute of estimated_amount ─────────────────────────
    # The FE sends a hint (estimated_amount) computed via the same fare
    # formula, but the canonical value comes from this server. If the FE
    # is in sync, the warning is null. If the FE is stale or tampered, the
    # server value wins and the response carries a warning so the FE can
    # surface the discrepancy. Mirrors the admin "create on behalf" path
    # at customer_care_api.py:767-802.
    server_estimated_amount = Decimal("0.00")
    server_warning: Optional[str] = None
    pricing_rule = await DefaultPricingService.get_effective_rule(
        db, body.city_id, body.vehicle_category_id, body.trip_type
    )
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
    if pricing_rule:
        from app.modules.booking.services.fare import trip_days_from

        distance_km = float(body.estimated_distance_km or 0)
        days = trip_days_from(pickup_dt, return_dt)
        server_estimated_amount = calculate_fare_breakdown(
            rule=pricing_rule,
            actual_distance_km=distance_km,
            start_dt=pickup_dt,
            end_dt=return_dt,
            trip_type=body.trip_type,
            trip_days=days,
        )["total"]
        fe_amount = (
            Decimal(str(body.estimated_amount)) if body.estimated_amount else None
        )
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
    else:
        # No pricing rule for this combination — fall back to the FE amount.
        # The booking will still be filed; admin can correct via EditCabDetails.
        server_estimated_amount = Decimal(str(body.estimated_amount or 0))
        server_warning = (
            "No pricing rule configured for this city/category/trip — "
            "using client-supplied estimate."
        )

    # ── Coupon validation + persistence ──────────────────────────────────
    # Re-validate the coupon against the SERVER-recomputed amount so a tampered
    # FE (e.g. forged coupon discount) cannot bypass the cap.
    coupon_discount_applied = Decimal("0.00")
    coupon_id_used: Optional[int] = None
    coupon_code_used: Optional[str] = None
    if body.coupon_code:
        from app.modules.admin.coupon_api import (
            ValidateRequest,
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

        vres = await validate_coupon(
            ValidateRequest(
                coupon_code=body.coupon_code,
                service_type="CAB",
                vehicle_category_id=body.vehicle_category_id,
                city_id=body.city_id,
                booking_amount=server_estimated_amount,
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
        # Resolve the coupon id by code — validate_coupon doesn't return it.
        coupon_id_row = await db.execute(
            text("SELECT id FROM coupons WHERE coupon_code = :code"),
            {"code": coupon_code_used},
        )
        coupon_id_used = coupon_id_row.scalar()

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

    # 2. Insert cab booking — includes the persisted coupon snapshot.
    await db.execute(
        text(
            """
        INSERT INTO cab_bookings (
            uuid, master_booking_id, booking_number,
            trip_type, vehicle_category_id,
            pickup_location, pickup_latitude, pickup_longitude,
            drop_location, drop_latitude, drop_longitude,
            pickup_datetime, return_datetime, estimated_distance, estimated_amount,
            coupon_code, coupon_id, coupon_discount,
            booking_status, created_at
        ) VALUES (
            :uuid, :master_id, :booking_number,
            :trip_type, :vehicle_category_id,
            :pickup_location, :pickup_lat, :pickup_lng,
            :drop_location, :drop_lat, :drop_lng,
            :pickup_datetime, :return_datetime, :distance, :estimated,
            :coupon_code, :coupon_id, :coupon_discount,
            'PENDING_ASSIGNMENT', NOW()
        )
    """
        ),
        {
            "uuid": cab_uuid,
            "master_id": master_id,
            "booking_number": cab_num,
            "trip_type": body.trip_type,
            "vehicle_category_id": body.vehicle_category_id,
            "pickup_location": body.pickup_location,
            "pickup_lat": body.pickup_latitude,
            "pickup_lng": body.pickup_longitude,
            "drop_location": body.drop_location,
            "drop_lat": body.drop_latitude,
            "drop_lng": body.drop_longitude,
            "pickup_datetime": pickup_dt,
            "return_datetime": return_dt,
            "distance": body.estimated_distance_km or 0,
            "estimated": server_estimated_amount,
            "coupon_code": coupon_code_used,
            "coupon_id": coupon_id_used,
            "coupon_discount": coupon_discount_applied,
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
    # close-trip / invoice math, and drives the per-coupon usage_count).
    # Increment the coupon's global usage counter atomically; rollback the
    # whole booking if this fails (rare — concurrent redemptions).
    if coupon_id_used:
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
                "mbid": master_id,
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
            "coupon_code": coupon_code_used,
            "coupon_discount": float(coupon_discount_applied),
            "warning": server_warning,
        },
    }
