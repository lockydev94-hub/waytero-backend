# ============================================================
# WAYTERO — TRIP TYPE GUARD (does the booking type match the places?)
# File: app/modules/booking/services/trip_guard.py
# Doc Ref: BRD Part 3 §35 (fare formula), §36 (city-based pricing);
#          SUPPLIER_CAB_PRICE_MASTER_V1 §4.6 (customer website trip types).
#
# A trip type is a claim about the journey: LOCAL says both ends are in one
# city, OUTSTATION says the journey crosses a city boundary, a Cab Rent says
# there is no destination at all — only a duration. Until now nothing checked
# that claim. A customer could pick Local and drop in another city, and the
# booking would be priced on the local sheet: the cheapest possible answer to a
# question nobody asked. The same hole existed on the phone desk, where an agent
# picked the type from memory.
#
# This module answers the question the fare engine cannot: which city is each
# place actually in, and does the chosen type agree with that? It returns
# *findings* — blocking errors, warnings and hints — rather than raising, so a
# live search form can show them, and the create path can decide to reject.
#
# Distance is deliberately separate (booking/services/distance.py). Here the
# route is measured only to answer "how long will this take", and only as a
# speed-based estimate: a number shown as approximate is honest, a Google call
# per keystroke is not.
# ============================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import get_platform_tz_sync

# ── Average door-to-door speeds used for the ETA ─────────────────────────────
# These are the speeds the results page already assumes when it converts a
# straight-line estimate into a drive time. AIRPORT and LOCAL are slower because
# they are mostly urban; outstation runs are mostly highway. A Cab Rent is a mix
# of both, so it sits between them.
_AVG_SPEED_KMH = {
    "LOCAL": 20.0,
    "AIRPORT": 26.0,
    "OUTSTATION": 42.0,
    "ONE_WAY": 42.0,
    "ROUND_TRIP": 38.0,
    "DAILY": 33.0,
}
_DEFAULT_SPEED_KMH = 30.0

# Cities whose centre is further than this from a picked point are not treated
# as "the city of that point". A place 200 km from the nearest serviced city is
# genuinely outside the network, and guessing would attach it to the wrong sheet.
_NEAREST_CITY_MAX_KM = 60.0
# Straight-line distances miss road curvature; same 1.25 tolerance the customer
# search uses for its own fallback.
_ROAD_FACTOR = 1.25

# Hours included in one Cab Rent day on the supplier card (12h / 100km).
_RENTAL_INCLUDED_HOURS = 12
_RENTAL_INCLUDED_KM = 100


# ── Points and cities ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TripPoint:
    """One place on the route, as the caller described it."""

    name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city_id: Optional[int] = None


@dataclass
class ResolvedPoint:
    name: str
    city_id: Optional[int] = None
    city_name: Optional[str] = None
    # CITY_ID | COORDS | NAME | NONE — how the city was decided, so the UI can
    # say "we could not place this stop" instead of pretending.
    match: str = "NONE"
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "city_id": self.city_id,
            "city_name": self.city_name,
            "match": self.match,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


@dataclass
class _City:
    id: int
    name: str
    latitude: Optional[float]
    longitude: Optional[float]


async def _load_cities(db: AsyncSession) -> list[_City]:
    """Active cities with coordinates.

    Small enough (a few hundred rows) to read whole and match in Python, which
    keeps the word-boundary rules greppable and testable instead of hiding them
    in a regular expression inside SQL.
    """
    rows = (
        await db.execute(
            sa.text(
                """
                SELECT id, name, latitude, longitude
                FROM cities
                WHERE is_active = TRUE
                ORDER BY id
                """
            )
        )
    ).fetchall()
    return [
        _City(
            id=r.id,
            name=r.name,
            latitude=float(r.latitude) if r.latitude is not None else None,
            longitude=float(r.longitude) if r.longitude is not None else None,
        )
        for r in rows
    ]


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(h))


def _nearest_city(
    cities: Iterable[_City], lat: float, lng: float
) -> tuple[Optional[_City], float]:
    best: Optional[_City] = None
    best_km = float("inf")
    for city in cities:
        if city.latitude is None or city.longitude is None:
            continue
        km = _haversine_km((lat, lng), (city.latitude, city.longitude))
        if km < best_km:
            best, best_km = city, km
    return best, best_km


def _city_in_text(cities: Iterable[_City], text: str) -> Optional[_City]:
    """The longest city name that appears as a whole word in ``text``.

    "Bhubaneswar Airport" → Bhubaneswar. The word boundary matters: without it
    "Puri" would match "Purulia" and a Konark stop could be filed under the
    wrong city. Longest wins so "Navi Mumbai" beats "Mumbai".
    """
    haystack = f" {(text or '').lower()} "
    best: Optional[_City] = None
    for city in cities:
        needle = (city.name or "").strip().lower()
        if not needle:
            continue
        if f" {needle} " in haystack or f" {needle}," in haystack:
            if best is None or len(needle) > len(best.name):
                best = city
    if best is not None:
        return best
    # Punctuation-tolerant second pass: "Bhubaneswar-Airport", "(Puri)".
    import re

    for city in cities:
        needle = (city.name or "").strip()
        if not needle:
            continue
        if re.search(rf"\b{re.escape(needle)}\b", text or "", re.IGNORECASE):
            if best is None or len(needle) > len(best.name):
                best = city
    return best


async def resolve_point(
    db: AsyncSession,
    point: TripPoint,
    *,
    cities: Optional[list[_City]] = None,
) -> ResolvedPoint:
    """Which city is this place in?

    Explicit ``city_id`` first (the search form already resolved one), then
    coordinates (a Google Place carries them, so this is exact), then the
    location text. When none of those work the point stays unplaced and the
    caller is told so — a guess here would silently move a trip between sheets.
    """
    cities = cities if cities is not None else await _load_cities(db)

    # Coordinates beat the form's city choice, and a named city in the text beats
    # it too: the picker's ``city_id`` is where the search was filed, while a
    # Google Place or "Puri" in the address is evidence about this trip. The
    # chosen city is the last resort, not the first, or every stop would inherit
    # the pickup city and a cross-city trip would look local.
    if point.latitude is not None and point.longitude is not None:
        nearest, km = _nearest_city(cities, point.latitude, point.longitude)
        if nearest is not None and km <= _NEAREST_CITY_MAX_KM:
            return ResolvedPoint(
                name=point.name,
                city_id=nearest.id,
                city_name=nearest.name,
                match="COORDS",
                latitude=point.latitude,
                longitude=point.longitude,
            )

    by_name = _city_in_text(cities, point.name)
    if by_name is not None:
        return ResolvedPoint(
            name=point.name,
            city_id=by_name.id,
            city_name=by_name.name,
            match="NAME",
            latitude=point.latitude,
            longitude=point.longitude,
        )

    if point.city_id:
        chosen = next((c for c in cities if c.id == point.city_id), None)
        if chosen is not None:
            return ResolvedPoint(
                name=point.name,
                city_id=chosen.id,
                city_name=chosen.name,
                match="CITY_ID",
                latitude=point.latitude,
                longitude=point.longitude,
            )

    return ResolvedPoint(
        name=point.name,
        latitude=point.latitude,
        longitude=point.longitude,
    )


# ── Duration ─────────────────────────────────────────────────────────────────


def estimate_duration_minutes(
    distance_km: Optional[float], trip_type: str
) -> Optional[int]:
    """A drive time from the distance, at the type's average speed.

    Deliberately labelled as an estimate by the caller: it is not a Google
    route time, and a customer who is told "about 2h 10m" is being told
    something true about the trip even when the last kilometre is a traffic jam.
    """
    if distance_km is None or distance_km <= 0:
        return None
    speed = _AVG_SPEED_KMH.get((trip_type or "").upper(), _DEFAULT_SPEED_KMH)
    return max(5, int(round(float(distance_km) / speed * 60)))


async def _measure_named_route(
    db: AsyncSession, points: list[ResolvedPoint]
) -> Optional[float]:
    """Measure pickup → stop… → drop by place name through Google.

    Returns None rather than raising when there is no Maps key or a place cannot
    be geocoded: the caller already has a fallback distance from the form, and a
    validation endpoint must not 500 on a place it cannot find.
    """
    from app.modules.admin.cab_route_distance import resolve_route_distance

    total = 0.0
    for a, b in zip(points, points[1:]):
        if not a.name or not b.name:
            return None
        try:
            km, _source = await resolve_route_distance(db, a.name, b.name)
        except Exception:
            return None
        total += float(km)
    return round(total, 1) if total > 0 else None


def _leg_km(a: ResolvedPoint, b: ResolvedPoint) -> Optional[float]:
    if (
        a.latitude is not None
        and a.longitude is not None
        and b.latitude is not None
        and b.longitude is not None
    ):
        return round(
            _haversine_km((a.latitude, a.longitude), (b.latitude, b.longitude))
            * _ROAD_FACTOR,
            1,
        )
    return None


def route_distance_km(points: list[ResolvedPoint]) -> Optional[float]:
    """Sum of the legs between consecutive points, when every leg can be drawn.

    Stops are exactly this: extra legs. A Bhubaneswar → Konark → Puri journey is
    not the Bhubaneswar → Puri distance, and pricing it as one is the difference
    between a quote and a complaint.
    """
    legs = [_leg_km(a, b) for a, b in zip(points, points[1:])]
    if not legs or any(km is None for km in legs):
        return None
    return round(sum(km for km in legs if km is not None), 1)


# ── Validation ───────────────────────────────────────────────────────────────


@dataclass
class TripCheck:
    trip_type: str
    pickup: ResolvedPoint
    drop: Optional[ResolvedPoint] = None
    stops: list[ResolvedPoint] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    detected_type: Optional[str] = None
    distance_km: Optional[float] = None
    duration_minutes: Optional[int] = None
    arrival: Optional[datetime] = None

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def cities(self) -> list[str]:
        seen: list[str] = []
        for p in [self.pickup, *self.stops, self.drop]:
            if p is not None and p.city_name and p.city_name not in seen:
                seen.append(p.city_name)
        return seen

    @property
    def crosses_cities(self) -> bool:
        ids = {
            p.city_id
            for p in [self.pickup, *self.stops, self.drop]
            if p is not None and p.city_id is not None
        }
        return len(ids) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "trip_type": self.trip_type,
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "hints": self.hints,
            "detected_type": self.detected_type,
            "crosses_cities": self.crosses_cities,
            "cities": self.cities,
            "pickup": self.pickup.as_dict(),
            "drop": self.drop.as_dict() if self.drop else None,
            "stops": [s.as_dict() for s in self.stops],
            "distance_km": self.distance_km,
            "duration": {
                "minutes": self.duration_minutes,
                "arrival": self.arrival.isoformat() if self.arrival else None,
                "source": "AVERAGE_SPEED",
                "avg_speed_kmh": _AVG_SPEED_KMH.get(self.trip_type, _DEFAULT_SPEED_KMH),
            },
        }


def _naive_pair(a: datetime, b: datetime) -> tuple[datetime, datetime]:
    """Both instants on the same footing before comparing them.

    The public form sends an ISO string with an offset; the desk form sends one
    without. Comparing them raw raises TypeError, which would turn a validation
    question into a 500.
    """
    if a.tzinfo is None and b.tzinfo is not None:
        a = a.replace(tzinfo=b.tzinfo)
    elif b.tzinfo is None and a.tzinfo is not None:
        b = b.replace(tzinfo=a.tzinfo)
    return a, b


def _join_names(points: list[ResolvedPoint]) -> str:
    names = [p.name for p in points if p.name]
    return ", ".join(names)


def _detected_type(
    pickup_city: Optional[str],
    drop_city: Optional[str],
    has_return: bool,
) -> Optional[str]:
    if not pickup_city:
        return None
    if drop_city:
        return (
            "LOCAL"
            if pickup_city == drop_city
            else ("ROUND_TRIP" if has_return else "OUTSTATION")
        )
    return "DAILY" if has_return else None


async def check_trip(
    db: AsyncSession,
    *,
    trip_type: str,
    pickup: TripPoint,
    drop: Optional[TripPoint] = None,
    stops: Optional[list[TripPoint]] = None,
    pickup_datetime: Optional[datetime] = None,
    return_datetime: Optional[datetime] = None,
    fallback_distance_km: Optional[float] = None,
    allow_drop_for_duration: bool = False,
) -> TripCheck:
    """Decide whether the chosen trip type agrees with the places.

    Blocking ``errors`` mean the booking must not be created under this type.
    ``warnings`` are things the customer should know before paying. ``hints``
    are statements about the estimated duration (late arrival, extra rental
    hours) — never reasons to refuse the booking.
    """
    code = (trip_type or "").strip().upper()
    cities = await _load_cities(db)

    resolved_pickup = await resolve_point(db, pickup, cities=cities)
    resolved_drop = await resolve_point(db, drop, cities=cities) if drop else None
    resolved_stops = [await resolve_point(db, s, cities=cities) for s in (stops or [])]

    check = TripCheck(
        trip_type=code,
        pickup=resolved_pickup,
        drop=resolved_drop,
        stops=resolved_stops,
    )

    has_return = return_datetime is not None
    check.detected_type = _detected_type(
        resolved_pickup.city_name,
        resolved_drop.city_name if resolved_drop else None,
        has_return,
    )

    p_city = resolved_pickup.city_name
    d_city = resolved_drop.city_name if resolved_drop else None

    outside = [
        s for s in resolved_stops if s.city_name and p_city and s.city_name != p_city
    ]

    # ── Per-type rules ──────────────────────────────────────────────────────
    if code in {"LOCAL", "AIRPORT"}:
        label = "local ride" if code == "LOCAL" else "airport transfer"
        if p_city and d_city and p_city != d_city:
            check.errors.append(
                f"This is a {label} but it ends in {d_city}, not {p_city}. "
                f"Book it as an outstation trip"
                + (" or a round trip if you are coming back." if has_return else ".")
            )
        if outside:
            check.errors.append(
                f"Stop {_join_names(outside)} is outside {p_city}, so this cannot "
                f"be a {label}. Book it as an outstation trip."
            )
        if not resolved_drop:
            check.warnings.append(
                f"A {label} needs a drop location before it can be measured."
            )
        elif not d_city:
            check.warnings.append(
                f"We could not place “{resolved_drop.name}” in a city, so the "
                "distance and fare stay an estimate."
            )
        if code == "AIRPORT" and not has_return:
            check.hints.append(
                "Airport transfers include waiting for your flight only if the "
                "pickup time is the landing time."
            )

    elif code in {"OUTSTATION", "ONE_WAY"}:
        if not resolved_drop:
            check.errors.append(
                "An outstation trip needs a destination — add the drop location."
            )
        if p_city and d_city and p_city == d_city and not outside:
            check.errors.append(
                f"Pickup and drop are both in {p_city}, so this is not an "
                "outstation trip. Book a local ride — or a round trip for a "
                "full-day hire that comes back."
            )
        if p_city and d_city and p_city != d_city:
            check.hints.append(
                f"Outstation trip {p_city} → {d_city}."
                + (
                    f" Stops included: {_join_names(resolved_stops)}."
                    if resolved_stops
                    else ""
                )
            )
        if check.detected_type == "ROUND_TRIP":
            check.hints.append(
                "You gave a return time on a one-way trip. A round trip returns "
                "to the pickup — switch the type if the driver is bringing you back."
            )

    elif code == "ROUND_TRIP":
        if not resolved_drop:
            check.errors.append("A round trip needs a destination to return from.")
        if not has_return:
            check.errors.append(
                "A round trip needs a return date and time, or the driver "
                "allowance cannot be counted."
            )
        elif pickup_datetime and return_datetime:
            a, b = _naive_pair(pickup_datetime, return_datetime)
            if b <= a:
                check.errors.append("The return time is before the pickup time.")

    elif code == "DAILY":
        # The desk sells a DAILY package as a package, where the drop is only
        # context for the route. That is priced by the card, not by a route, so
        # a drop there is information rather than a contradiction.
        if resolved_drop and not allow_drop_for_duration:
            check.errors.append(
                "A Cab Rent is booked for a duration, not a destination. Remove "
                "the drop and add stops if the car will visit places."
            )
        if not has_return:
            check.errors.append(
                "Tell us when the rental ends — a Cab Rent is sold in 24-hour days."
            )
        elif pickup_datetime and return_datetime:
            a, b = _naive_pair(pickup_datetime, return_datetime)
            if b <= a:
                check.errors.append("The rental must end after it starts.")

    else:
        check.errors.append(
            f"“{trip_type}” is not a trip type this platform sells. Choose local, "
            "airport, outstation, one way, round trip or cab rent."
        )

    # ── Duration and the hints it produces ──────────────────────────────────
    route_points = [
        check.pickup,
        *check.stops,
        *([check.drop] if check.drop else []),
    ]
    measured = route_distance_km(route_points)
    if measured is None and len(route_points) > 1:
        # A typed stop has a name but no coordinates. Google can still measure
        # the chain, and the booking will be priced on exactly that chain, so
        # the page and the fare must not disagree about the kilometres.
        measured = await _measure_named_route(db, route_points)
    if code == "ROUND_TRIP" and measured is not None:
        # A round trip drives the route twice; quoting one leg is how a
        # day-long hire ends up priced like a one-way.
        measured = round(measured * 2, 1)
    if measured is not None and measured >= 1:
        check.distance_km = measured
    elif fallback_distance_km and fallback_distance_km > 0:
        check.distance_km = float(fallback_distance_km)

    check.duration_minutes = estimate_duration_minutes(check.distance_km, code)
    if check.duration_minutes is not None and pickup_datetime is not None:
        start = (
            pickup_datetime
            if pickup_datetime.tzinfo
            else pickup_datetime.replace(tzinfo=get_platform_tz_sync())
        )
        check.arrival = start + timedelta(minutes=check.duration_minutes)

        # Night window is 22:00–06:00 IST in the fare engine; say so before the
        # customer pays rather than at the invoice.
        if check.arrival.hour >= 22 or check.arrival.hour < 6:
            check.hints.append(
                f"This trip is expected to finish around "
                f"{check.arrival.astimezone(get_platform_tz_sync()):%I:%M %p}, in "
                "the 10 PM–6 AM night window, so a night charge may apply."
            )
        if (
            code in {"LOCAL", "AIRPORT"}
            and check.arrival.date() > start.astimezone(get_platform_tz_sync()).date()
        ):
            check.hints.append(
                "Expected to run past midnight — a round trip or cab rent may be "
                "the fairer way to book it."
            )
        if code == "DAILY":
            days = 1
            if return_datetime:
                a, b = _naive_pair(pickup_datetime, return_datetime)
                days = max(1, int(math.ceil((b - a).total_seconds() / 86400)))
            included = _RENTAL_INCLUDED_HOURS * days
            if check.duration_minutes > included * 60:
                check.hints.append(
                    f"Driving time is about {check.duration_minutes // 60}h "
                    f"{check.duration_minutes % 60:02d}m; the card includes "
                    f"{included}h over {days} day(s), so extra hours may be charged."
                )

    if resolved_stops and check.distance_km:
        check.hints.append(
            f"{len(resolved_stops)} stop(s) included — the distance covers the "
            "extra legs, not just pickup to drop."
        )

    if (
        code in {"OUTSTATION", "ONE_WAY", "ROUND_TRIP"}
        and pickup_datetime
        and return_datetime
        and (return_datetime.date() - pickup_datetime.date()).days >= 1
    ):
        check.hints.append(
            "This trip spans more than one day, so the driver allowance is "
            "charged per day."
        )

    return check
