# ============================================================
# WAYTERO — TRIP DISTANCE AUTHORITY
# File: app/modules/booking/services/distance.py
# Doc Ref: Round 15 plan C2; migration 0086 (distance_source, billing_distance_km,
#          cab_routes.toll_charge); BRD Part 3 §35 — fare engine formula.
#
# The distance is the number the whole cab price is built on, and until now the
# server accepted it from whoever asked: `public_cab_api.py` priced a booking
# straight off `body.estimated_distance_km`, and the phone desk priced off
# `CabBookingCreateByAdmin.estimated_distance` — a field an agent types. Both
# then called the fare engine, which is how a booking ends up with a total that
# is arithmetically correct and factually wrong.
#
# This module is the single place a trip's kilometres are decided. It reuses the
# route editor's resolver (`admin/cab_route_distance.py`) rather than writing a
# second Google client, because that one already gets the key from the active
# `api_integrations` row, already degrades from Distance Matrix to a straight-line
# estimate, and already raises instead of inventing a number.
#
# Order of preference, and why:
#   1. A configured `cab_routes` row for the city pair. The owner set that
#      distance by hand after Google reported it, so it is the most authoritative
#      figure available, and it is the only place the toll lives — which is the
#      owner's "some routes have toll, it is not included in the booking, add it
#      as an extra". Matching a route therefore answers distance, toll and whose
#      city's rule sheet prices the trip, in one row the admin already maintains.
#   2. Google, then geocode + straight-line. For anything that is not a
#      published corridor — every LOCAL and within-city AIRPORT trip — there is no
#      route row to consult.
#   3. Nothing. `ValidationException` (400) when neither can produce a number. A
#      ₹0.00 estimate is not a cheap booking, it is a booking nobody will take,
#      and it is what the old code inserted when the distance field was empty.
#
# The returned `source` is persisted on the booking so the invoice, the admin
# screen and support can all say where the kilometres came from. A trip priced
# from a straight-line estimate is a trip whose price is approximate, and the
# customer is entitled to know that before agreeing to it.
# ============================================================

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.modules.admin.cab_route_distance import resolve_route_distance

# A configured corridor's distance, and the toll published with it.
_ROUTE_SQL = sa.text(
    """
    SELECT id, distance_km, toll_charge, pricing_city_id
    FROM cab_routes
    WHERE is_active = TRUE
      AND (
            (origin_city_id = :city_a AND destination_city_id = :city_b)
         OR (origin_city_id = :city_b AND destination_city_id = :city_a)
      )
    ORDER BY distance_km ASC
    LIMIT 1
    """
)


@dataclass(frozen=True)
class TripDistance:
    """What the trip measured, how it was measured, and what it costs to price."""

    km: Decimal
    source: str
    # The route row that supplied the figure, when one did.
    route_id: Optional[int] = None
    # Toll collected on that corridor. 0 means "this trip has no configured toll",
    # which is a different claim from "unknown" — an unmatched pair has no toll
    # data at all and the caller must say so rather than print ₹0.
    toll: Decimal = Decimal("0.00")
    toll_configured: bool = False
    # Which city's pricing rules apply. A published corridor already carries the
    # admin's answer (`cab_routes.pricing_city_id`); otherwise it is the pickup
    # city, which is the owner's rule that the price depends on the pickup city.
    pricing_city_id: Optional[int] = None


async def resolve_trip_distance(
    db: AsyncSession,
    *,
    pickup_location: str,
    drop_location: str,
    origin_city_id: int,
    destination_city_id: Optional[int] = None,
) -> TripDistance:
    """The server's own answer for how far this trip is, in km.

    `destination_city_id` is omitted for a within-city trip — there is no
    corridor to match, so Google answers it. Passing the same city twice is
    treated the same way rather than matching a route that starts and ends in
    the same place.
    """
    pickup = (pickup_location or "").strip()
    drop = (drop_location or "").strip()
    if not pickup or not drop:
        raise ValidationException(
            "Pickup and drop locations are both needed to price a cab trip."
        )

    pair_a = origin_city_id
    pair_b = (
        destination_city_id
        if destination_city_id and destination_city_id != origin_city_id
        else None
    )

    if pair_b is not None:
        row = (
            (await db.execute(_ROUTE_SQL, {"city_a": pair_a, "city_b": pair_b}))
            .mappings()
            .one_or_none()
        )
        if row and row["distance_km"] is not None:
            toll = Decimal(str(row["toll_charge"])) if row["toll_charge"] is not None else None
            return TripDistance(
                km=Decimal(str(row["distance_km"])),
                source="CONFIGURED_ROUTE",
                route_id=row["id"],
                toll=toll if toll is not None else Decimal("0.00"),
                toll_configured=toll is not None,
                pricing_city_id=row["pricing_city_id"],
            )

    try:
        km, source = await resolve_route_distance(
            db,
            pickup,
            drop,
            origin_hint=None,
            destination_hint=None,
        )
    except ValidationException:
        # The route editor's wording invites the operator to type a distance,
        # which is the exact thing this module exists to stop. Same failure,
        # stated for a booking instead of for a CMS form.
        raise ValidationException(
            "These places could not be measured, so the trip cannot be priced. "
            "Check the spelling of the pickup and drop locations."
        ) from None
    return TripDistance(
        km=Decimal(str(km)),
        source=source,
        toll=Decimal("0.00"),
        toll_configured=False,
        pricing_city_id=origin_city_id,
    )
