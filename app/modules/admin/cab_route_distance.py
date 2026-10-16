# ============================================================
# WAYTERO — CAB ROUTE DISTANCE RESOLVER
# File: app/modules/admin/cab_route_distance.py
# Doc Ref: migration 0079_cab_routes (distance_km, distance_source)
#
# Distance is the one number a route cannot do without, and hand-typing it is
# how a route page ends up quoting a fare for the wrong journey. The admin
# editor therefore asks Google first, falls back to a straight-line estimate,
# and only refuses the save when neither can produce a number.
#
# Reads the same active GOOGLE_MAPS api_integrations row as the city geocoder
# (admin/api.py) instead of importing it, so the legacy module stays untouched.
# ============================================================

import math
from decimal import Decimal, ROUND_HALF_UP

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException

_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TIMEOUT = 10.0

# Straight-line distance misses road curvature; 1.25 is the tolerance the
# customer-side cab search already uses for the same fallback.
_HAVERSINE_ROAD_FACTOR = Decimal("1.25")
_KM_QUANTUM = Decimal("0.1")


async def get_active_google_maps_key(db: AsyncSession) -> str | None:
    """The admin-configured Maps key, or None when absent/inactive."""
    try:
        row = (
            (
                await db.execute(
                    sa.text(
                        """
                        SELECT configuration, is_active
                        FROM api_integrations
                        WHERE service_type = 'GOOGLE_MAPS'
                        LIMIT 1
                        """
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    except Exception:
        return None
    if not row or not row.get("is_active"):
        return None
    cfg = row.get("configuration") or {}
    if not isinstance(cfg, dict):
        return None
    return (cfg.get("api_key") or "").strip() or None


def _quantise(metres: float) -> Decimal:
    return (Decimal(str(metres)) / Decimal(1000)).quantize(
        _KM_QUANTUM, rounding=ROUND_HALF_UP
    )


def _address(label: str, hint: str | None) -> str:
    label = label.strip()
    return f"{label}, {hint}, India" if hint else f"{label}, India"


async def google_distance_km(
    db: AsyncSession,
    origin: str,
    destination: str,
    *,
    origin_hint: str | None = None,
    destination_hint: str | None = None,
) -> Decimal | None:
    """Driving distance from the Distance Matrix API.

    Every failure — no key, quota, HTTP error, ZERO_RESULTS — returns None so
    the caller can fall back instead of blocking an admin's save.
    """
    api_key = await get_active_google_maps_key(db)
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                _MATRIX_URL,
                params={
                    "origins": _address(origin, origin_hint),
                    "destinations": _address(destination, destination_hint),
                    "mode": "driving",
                    "units": "metric",
                    "key": api_key,
                },
            )
            data = r.json()
    except Exception:
        return None
    if (data or {}).get("status") != "OK":
        return None
    rows = data.get("rows") or []
    if not rows:
        return None
    elements = rows[0].get("elements") or []
    if not elements or elements[0].get("status") != "OK":
        return None
    value = (elements[0].get("distance") or {}).get("value")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return _quantise(value)


async def _geocode(api_key: str, address: str) -> tuple[float, float] | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                _GEOCODE_URL,
                params={
                    "address": address,
                    "components": "country:IN",
                    "key": api_key,
                },
            )
            data = r.json()
    except Exception:
        return None
    if (data or {}).get("status") != "OK":
        return None
    results = data.get("results") or []
    if not results:
        return None
    loc = (results[0].get("geometry") or {}).get("location") or {}
    lat, lng = loc.get("lat"), loc.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    return (float(lat), float(lng))


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(h))


async def resolve_route_distance(
    db: AsyncSession,
    origin: str,
    destination: str,
    *,
    origin_hint: str | None = None,
    destination_hint: str | None = None,
) -> tuple[Decimal, str]:
    """(kilometres, source) via Google, then geocode + straight-line estimate.

    Raises ValidationException only when neither path yields a number, so the
    admin is never handed a silently wrong distance.
    """
    google = await google_distance_km(
        db,
        origin,
        destination,
        origin_hint=origin_hint,
        destination_hint=destination_hint,
    )
    if google is not None:
        return (google, "GOOGLE")

    api_key = await get_active_google_maps_key(db)
    if api_key:
        a = await _geocode(api_key, _address(origin, origin_hint))
        b = await _geocode(api_key, _address(destination, destination_hint))
        if a and b:
            estimated = (
                Decimal(str(_haversine_km(a, b))) * _HAVERSINE_ROAD_FACTOR
            ).quantize(_KM_QUANTUM, rounding=ROUND_HALF_UP)
            if estimated > 0:
                return (estimated, "HAVERSINE")

    raise ValidationException(
        "Could not determine the distance for these places. Check the spelling "
        "or enter the distance manually."
    )
