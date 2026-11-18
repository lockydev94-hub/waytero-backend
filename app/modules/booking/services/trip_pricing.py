# ============================================================
# WAYTERO — CAB TRIP PRICING (server-owned quote)
# File: app/modules/booking/services/trip_pricing.py
# Doc Ref: Round 15 plan C2; BRD Part 3 §35 — fare formula; migration 0086.
#
# One call answers "what does this trip cost, and what do I tell the customer it
# is made of". It composes the three things that were previously done separately
# and inconsistently at five call sites: measure the trip
# (booking/services/distance), pick the rule sheet (DefaultPricingService),
# arithmetic (booking/services/fare).
#
# The order matters and is the point. Distance is resolved *first*, because a
# published corridor carries the city whose rules price it
# (`cab_routes.pricing_city_id`) and the toll that corridor actually has. Asking
# the rule sheet first would price a Bhubaneswar→Puri trip on whichever city the
# form happened to be filed in, which is how "the website said one thing and the
# invoice said another" gets made.
#
# Nothing here rounds a number for display or accepts a client's figure. The
# total is the sum of the persisted lines by construction
# (fare.calculate_fare_breakdown quantises each line before summing), so the
# booking row, the invoice and the settlement line can all be reconciled with a
# calculator.
# ============================================================

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.core.timezone import to_platform_tz
from app.modules.admin.services import DefaultPricingService
from app.modules.booking.services.distance import TripDistance, resolve_trip_distance
from app.modules.booking.services.fare import (
    calculate_fare_breakdown,
    trip_days_from,
)

# What a caller must be told alongside the number, in the customer's terms.
# These are statements about the arithmetic that actually happened, not
# marketing copy: each one is produced from a value on the quote.
_ESTIMATE_NOTE = (
    "Distance estimated in a straight line — Google could not route this pair, "
    "so the fare is approximate and will be corrected at trip end."
)
_TOLL_UNCONFIGURED_NOTE = (
    "No toll is configured for this journey. If the route has a toll plaza, "
    "that amount is collected separately and added to the trip."
)
# Provenance, not a promise. The fare engine re-prices from the distance actually
# driven at trip end, but this module does not know whether that step has run for
# this row, so the note only states where the kilometres came from.
_CLIENT_STATED_NOTE = (
    "These kilometres were taken from the booking form — without a destination "
    "to measure there is nothing the server could check them against."
)


@dataclass(frozen=True)
class TripQuote:
    """The server's answer for one cab trip."""

    distance: TripDistance
    rule: dict[str, Any]
    breakdown: dict[str, Any]
    total: Decimal
    notes: list[str] = field(default_factory=list)

    @property
    def pricing_city_id(self) -> Optional[int]:
        return self.distance.pricing_city_id

    @property
    def billable_km(self) -> float:
        return self.breakdown["billable_km"]

    def line(self, key: str) -> Decimal:
        return Decimal(str(self.breakdown[key]))


async def quote_cab_trip(
    db: AsyncSession,
    *,
    pickup_location: str,
    drop_location: str,
    origin_city_id: int,
    vehicle_category_id: int,
    trip_type: str,
    pickup_datetime: Optional[datetime] = None,
    return_datetime: Optional[datetime] = None,
    destination_city_id: Optional[int] = None,
    waiting_minutes: Optional[int] = None,
    fallback_distance_km: Optional[Decimal] = None,
    stops: Optional[list[str]] = None,
) -> TripQuote:
    """Price a cab trip from the server's own measurement of it.

    `waiting_minutes` is the trip's own waiting time, not the rule's: a pricing
    row is a city's settings and cannot know that this customer asked the driver
    to wait 40 minutes. Passing nothing bills no waiting at all.

    `fallback_distance_km` is what the website may offer when the journey cannot
    be measured at all — a local hire with no destination, or Google refusing the
    pair. It is priced and then recorded as `CLIENT_STATED`, so the booking row
    says whose word the kilometres are. The phone desk never passes it: an agent
    quoting a fare over the phone has a destination, and a figure typed by the
    agent is the thing this round removed.

    `stops` are places between pickup and drop. A trip that visits them is longer
    than the pickup→drop distance, so each leg is measured and summed; pricing a
    detour as the direct route is a discount nobody asked for. A published
    corridor is only used for a two-point trip, because a corridor row describes
    the direct journey, not the detour.

    Raises ValidationException when the trip cannot be measured *and* no fallback
    was offered, or when no rule sheet covers the combination — both of which the
    caller must fix rather than price around.
    """
    route_stops = [s for s in (stops or []) if (s or "").strip()]
    try:
        if route_stops:
            distance = await _measure_with_stops(
                db,
                pickup_location=pickup_location,
                drop_location=drop_location,
                stops=route_stops,
                origin_city_id=origin_city_id,
            )
        else:
            distance = await resolve_trip_distance(
                db,
                pickup_location=pickup_location,
                drop_location=drop_location,
                origin_city_id=origin_city_id,
                destination_city_id=destination_city_id,
            )
    except ValidationException:
        if fallback_distance_km is None or fallback_distance_km <= 0:
            raise
        distance = TripDistance(
            km=Decimal(str(fallback_distance_km)),
            source="CLIENT_STATED",
            pricing_city_id=origin_city_id,
        )

    pricing_city = distance.pricing_city_id or origin_city_id
    # The platform-local calendar day the trip happens on — that is the day a fare
    # sheet with an effective window has to cover. Not the day of the request, and
    # not the UTC day, which would put a 01:00 IST pickup on yesterday's sheet.
    trip_day = to_platform_tz(pickup_datetime).date() if pickup_datetime else None
    rule = await DefaultPricingService.get_effective_rule(
        db, pricing_city, vehicle_category_id, trip_type, for_date=trip_day
    )
    if rule is None:
        raise ValidationException(
            "No fare is configured for that cab category and trip type in this "
            "city. Set the price before booking, or the customer cannot be quoted."
        )

    breakdown = calculate_fare_breakdown(
        rule=rule,
        actual_distance_km=float(distance.km),
        start_dt=pickup_datetime,
        end_dt=return_datetime or pickup_datetime,
        trip_type=trip_type,
        trip_days=trip_days_from(pickup_datetime, return_datetime),
        # A corridor's published toll beats the city's flat figure; a city's rule
        # still answers for trips on no published corridor. Neither claims anything
        # when the toll was never configured — see the note below.
        toll=distance.toll if distance.toll_configured else None,
        actual_waiting_minutes=waiting_minutes,
    )

    return TripQuote(
        distance=distance,
        rule=rule,
        breakdown=breakdown,
        total=Decimal(str(breakdown["total"])),
        notes=_notes_for(distance, breakdown),
    )


async def _measure_with_stops(
    db: AsyncSession,
    *,
    pickup_location: str,
    drop_location: str,
    stops: list[str],
    origin_city_id: int,
) -> TripDistance:
    """Pickup → stop 1 → … → drop, leg by leg.

    Google is asked per leg so a stop that the direct-distance shortcut would
    ignore is counted. The reported source is the *worst* one used: if any leg
    fell back to a straight-line estimate, the whole figure is approximate and
    the booking must say so.
    """
    from app.modules.admin.cab_route_distance import resolve_route_distance

    points = [pickup_location, *stops, drop_location]
    total = Decimal("0")
    sources: set[str] = set()
    for a, b in zip(points, points[1:]):
        km, source = await resolve_route_distance(db, a, b)
        total += Decimal(str(km))
        sources.add(source)
    source = "GOOGLE" if sources == {"GOOGLE"} else "HAVERSINE"
    return TripDistance(
        km=total.quantize(Decimal("0.1")),
        source=source,
        toll=Decimal("0.00"),
        toll_configured=False,
        pricing_city_id=origin_city_id,
    )


def _notes_for(distance: TripDistance, breakdown: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if distance.source == "HAVERSINE":
        notes.append(_ESTIMATE_NOTE)
    if distance.source == "CLIENT_STATED":
        notes.append(_CLIENT_STATED_NOTE)
    if not distance.toll_configured:
        notes.append(_TOLL_UNCONFIGURED_NOTE)
    # The minimum is the one part of a cab price a customer reliably disputes: a
    # 23 km one-way trip billed at a 30 km minimum needs saying out loud.
    billable = Decimal(str(breakdown["billable_km"]))
    actual = Decimal(str(breakdown["actual_distance_km"]))
    if billable > actual:
        notes.append(
            f"Billed at the {billable} km minimum for this trip type, not the "
            f"{actual} km of the journey."
        )
    if (
        breakdown.get("is_night")
        and Decimal(str(breakdown["night_charge_applied"])) > 0
    ):
        notes.append(
            "Night charge applies to this pickup time " "(22:00–06:00 IST window)."
        )
    return notes


def fare_columns(quote: TripQuote) -> dict[str, Any]:
    """The cab_bookings column values for this quote.

    Kept as one mapping so a booking, an amendment and a re-quote cannot each
    remember a different subset of the lines and drift.
    """
    b = quote.breakdown
    return {
        "pricing_city_id": quote.distance.pricing_city_id,
        "distance_source": quote.distance.source,
        "billing_distance_km": Decimal(str(b["billable_km"])),
        "base_fare_amount": Decimal(str(b["base_fare"])),
        "distance_charge_amount": Decimal(str(b["distance_charge"])),
        "driver_allowance_amount": Decimal(str(b["driver_allowance"])),
        "night_charge_amount": Decimal(str(b["night_charge_applied"])),
        "toll_amount": Decimal(str(b["toll"])),
        "waiting_charge_amount": Decimal(str(b["waiting"])),
        "rate_snapshot": _rate_snapshot(quote),
    }


def _rate_snapshot(quote: TripQuote) -> dict[str, Any]:
    """The rule the price came from, small enough to read on a dispute.

    Stored rather than re-fetched later because rules change. The snapshot says
    what was charged and where it was charged, which is the whole question an
    agreed fare raises afterwards.
    """
    rule = quote.rule
    return {
        "rule_id": rule.get("rule_id"),
        "rule_table": rule.get("rule_table"),
        "city_id": rule.get("city_id"),
        "source": rule.get("source"),
        "trip_type": rule.get("trip_type"),
        "vehicle_category_id": rule.get("vehicle_category_id"),
        "base_fare": str(rule.get("base_fare") or 0),
        "minimum_km": str(rule.get("minimum_km") or 0),
        "per_km_rate": str(rule.get("per_km_rate") or 0),
        "driver_allowance": str(rule.get("driver_allowance") or 0),
        "driver_allowance_type": rule.get("driver_allowance_type"),
        "night_charge": str(rule.get("night_charge") or 0),
        "night_charge_type": rule.get("night_charge_type"),
        "waiting_rate_per_hour": str(rule.get("waiting_rate_per_hour") or 0),
        "free_waiting_minutes": str(rule.get("free_waiting_minutes") or 0),
        "effective_from": (
            rule["effective_from"].isoformat()
            if isinstance(rule.get("effective_from"), date)
            else rule.get("effective_from")
        ),
        "effective_to": (
            rule["effective_to"].isoformat()
            if isinstance(rule.get("effective_to"), date)
            else rule.get("effective_to")
        ),
        # What the trip was measured as, so the lines can be re-added by hand.
        "distance_km": str(quote.distance.km),
        "distance_source": quote.distance.source,
        "route_id": quote.distance.route_id,
        "toll": str(quote.distance.toll) if quote.distance.toll_configured else None,
    }
