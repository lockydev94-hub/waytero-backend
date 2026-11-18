# ============================================================
# WAYTERO — PACKAGE CAB BOOKING (admin-side quote)
# File: app/modules/booking/services/rental_booking.py
# Doc Ref: Docs/01_Business_Requirements/SUPPLIER_CAB_PRICE_MASTER_V1.md
#          BRD Part 3 §35 (fare engine), §36 (city-based pricing); migration 0098
#
# The desk's package quote: pick a rate class, a rental type and the hours/km the
# customer described, and this returns the price the rate card produces *and* the
# price this customer has been promised if an override exists. Both sides are
# kept, because an admin approving a discount has to be able to see what it costs
# against the card — a single number hides whether ₹9,000 was a good deal.
#
# Deliberate choices:
#
#   • The standard quote is computed first and never modified. The override is
#     applied to a *copy* of the selected package row, so `standard` stays the
#     card's answer and `difference` is arithmetic nobody can disagree with.
#   • An override is keyed to one package (``included_hours``), not a class. A
#     negotiated "₹2,000 for the 8h/80km Sedan" must not become the price of the
#     12h/120km trip the same customer books next week.
#   • A city row for the class wins over the platform default as a whole; the
#     override then wins over either. Three levels, never a blend.
#   • Validations are data, not exceptions: the quote endpoint returns them so
#     the UI can show every reason at once, and the create path turns the ERROR
#     ones into a refusal. One validator, two audiences.
# ============================================================

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.modules.booking.services.rental_rates import (
    RENTAL_TYPES,
    RentalQuote,
    calculate_rental_fare,
)

TWO_PLACES = Decimal("0.01")

# Ordering for the UI: the three kinds the card sells, cheapest commitment first.
RENTAL_TYPE_ORDER = ("LOCAL", "OUTSTATION", "DAILY")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWO_PLACES)


def _field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _dict(row: Any) -> dict:
    return dict(row) if isinstance(row, Mapping) else dict(row.__dict__)


@dataclass(frozen=True)
class ValidationIssue:
    """One reason the desk's screen should say something.

    ``severity`` is the UI contract: ERROR cannot be booked, WARNING is allowed
    but the agent must know, INFO is an explanation of the number on screen.
    """

    code: str
    severity: str
    message: str
    field: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "field": self.field,
        }


@dataclass(frozen=True)
class CustomQuote:
    """A customer's negotiated price for one package."""

    id: int
    customer_id: int
    city_id: Optional[int]
    vehicle_class: str
    rental_type: str
    included_hours: int
    package_price: Optional[Decimal]
    extra_hour_rate: Optional[Decimal]
    extra_km_rate: Optional[Decimal]
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    reason: Optional[str] = None
    is_active: bool = True

    def as_dict(self) -> dict:
        # JSON-safe by construction: this mapping is stored inside the booking's
        # ``rate_snapshot`` jsonb column, and a Decimal there fails at INSERT
        # long after the customer was quoted a price.
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "city_id": self.city_id,
            "vehicle_class": self.vehicle_class,
            "rental_type": self.rental_type,
            "included_hours": self.included_hours,
            "package_price": (
                str(_money(self.package_price))
                if self.package_price is not None
                else None
            ),
            "extra_hour_rate": (
                str(_money(self.extra_hour_rate))
                if self.extra_hour_rate is not None
                else None
            ),
            "extra_km_rate": (
                str(_money(self.extra_km_rate))
                if self.extra_km_rate is not None
                else None
            ),
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "reason": self.reason,
            "is_active": self.is_active,
        }

    def covers(self, on_date: Optional[date]) -> bool:
        if on_date is None:
            return True
        if self.valid_from and on_date < self.valid_from:
            return False
        if self.valid_to and on_date > self.valid_to:
            return False
        return True

    def diff_fields(self) -> list[str]:
        changes = []
        if self.package_price is not None:
            changes.append("package price")
        if self.extra_hour_rate is not None:
            changes.append("extra hour rate")
        if self.extra_km_rate is not None:
            changes.append("extra km rate")
        return changes


@dataclass
class RentalPackageQuote:
    """What the desk is shown for one package trip."""

    city_id: int
    rate_source: str
    vehicle_class: str
    vehicle_label: Optional[str]
    vehicle_category_id: Optional[int]
    seating_capacity: Optional[int]
    rental_type: str
    standard: RentalQuote
    applied: RentalQuote
    custom_quote: Optional[CustomQuote]
    # True only when the override was inside its window and actually priced the
    # trip. A row that exists but has expired must not be reported as applied.
    custom_applied: bool
    difference: Decimal
    hours: Decimal
    km: Decimal
    days: int
    returned_after_midnight: bool
    # Every package the class sells for this rental type, so the UI can offer
    # the one above and below the automatic pick without a second request.
    package_options: list[dict] = field(default_factory=list)
    validations: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [v for v in self.validations if v.severity == "ERROR"]

    @property
    def total(self) -> Decimal:
        return self.applied.total

    @property
    def trip_type(self) -> str:
        return self.rental_type

    def notes(self) -> list[str]:
        return [v.message for v in self.validations]


# ── Loading ──────────────────────────────────────────────────


async def load_rate_card(
    db: AsyncSession, city_id: Optional[int], on_date: Optional[date] = None
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Every active rate class for a city, with the card it was priced from.

    Returns ``(rows_by_class, source_by_class)`` where source is ``CITY`` when
    that class is priced by the city's own sheet and ``PLATFORM`` when it falls
    back to the platform default. The fallback is per class, so a city that has
    priced only its Sedans still sells Travellers at the default card.
    """
    result = await db.execute(
        text(
            """
            SELECT * FROM cab_rental_rates
            WHERE is_active = TRUE
              AND (city_id = :city_id OR city_id IS NULL)
            ORDER BY vehicle_class, rental_type, included_hours
            """
        ),
        {"city_id": city_id},
    )
    rows = [dict(r) for r in result.mappings().all()]

    by_class: dict[str, list[dict]] = {}
    source: dict[str, str] = {}
    for row in rows:
        cls = str(row["vehicle_class"])
        row_city = row.get("city_id")
        if row_city is None:
            # A platform row adds nothing once the city has priced this class.
            if source.get(cls) == "CITY":
                continue
            source[cls] = "PLATFORM"
        else:
            if source.get(cls) == "PLATFORM":
                # First city row for this class: the city's sheet replaces the
                # platform's for the class as a whole, never row by row.
                by_class[cls] = []
            source[cls] = "CITY"
        by_class.setdefault(cls, []).append(row)

    return by_class, source


async def load_custom_quote(
    db: AsyncSession,
    *,
    customer_id: int,
    city_id: Optional[int],
    vehicle_class: str,
    rental_type: str,
    included_hours: int,
    on_date: Optional[date] = None,
) -> Optional[CustomQuote]:
    """This customer's override for exactly this package, city first.

    Rows outside their validity window are still returned; ``covers()`` decides
    whether they apply, so the caller can tell the agent "there is an override
    but it expired" instead of silently pricing the card.
    """
    result = await db.execute(
        text(
            """
            SELECT id, customer_id, city_id, vehicle_class, rental_type,
                   included_hours, package_price, extra_hour_rate, extra_km_rate,
                   valid_from, valid_to, reason, is_active
            FROM cab_custom_quotes
            WHERE customer_id = :customer_id
              AND vehicle_class = :vehicle_class
              AND rental_type = :rental_type
              AND included_hours = :included_hours
              AND is_active = TRUE
              AND (city_id = :city_id OR city_id IS NULL)
            ORDER BY city_id NULLS LAST
            LIMIT 1
            """
        ),
        {
            "customer_id": customer_id,
            "city_id": city_id,
            "vehicle_class": vehicle_class,
            "rental_type": rental_type,
            "included_hours": included_hours,
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    data = dict(row)
    for key in ("package_price", "extra_hour_rate", "extra_km_rate"):
        if data.get(key) is not None:
            data[key] = Decimal(str(data[key]))
    return CustomQuote(**data)


# ── Quoting ──────────────────────────────────────────────────


def _package_options(rows: Iterable[dict], rental_type: str) -> list[dict]:
    options = [
        {
            "rate_id": row.get("id"),
            "rental_type": rental_type,
            "included_hours": int(row.get("included_hours") or 0),
            "included_km": int(row.get("included_km") or 0),
            "price": _money(row.get("price")),
            "extra_hour_rate": _money(row.get("extra_hour_rate")),
            "extra_km_rate": _money(row.get("extra_km_rate")),
        }
        for row in rows
        if str(row.get("rental_type") or "").upper() == rental_type
    ]
    return sorted(options, key=lambda o: (o["included_hours"], o["included_km"]))


def _overridden_rows(
    rows: list[dict], custom: CustomQuote, chosen_hours: int
) -> list[dict]:
    """The class's rows with this customer's agreed lines substituted in.

    Only the quoted package's row changes: the other packages stay at the card
    price, because a negotiated 8h rate says nothing about what the 12h one
    should cost.
    """
    out = []
    for row in rows:
        clone = dict(row)
        if (
            str(clone.get("rental_type") or "").upper() == custom.rental_type
            and int(clone.get("included_hours") or 0) == chosen_hours
        ):
            if custom.package_price is not None:
                clone["price"] = custom.package_price
            if custom.extra_hour_rate is not None:
                clone["extra_hour_rate"] = custom.extra_hour_rate
            if custom.extra_km_rate is not None:
                clone["extra_km_rate"] = custom.extra_km_rate
        out.append(clone)
    return out


async def quote_rental_package(
    db: AsyncSession,
    *,
    city_id: int,
    vehicle_class: str,
    rental_type: str,
    hours: float = 0,
    km: float = 0,
    days: int = 1,
    returned_after_midnight: bool = False,
    customer_id: Optional[int] = None,
    pickup_datetime: Optional[datetime] = None,
) -> RentalPackageQuote:
    """Price one package for one customer, with every reason the desk needs.

    Raises ValidationException only when there is nothing to price at all — an
    unknown class or a class with no package of this rental type. Everything an
    agent can act on (past pickup, overflow, expired override, unmapped class)
    comes back in ``validations`` so the screen can show it beside the number.
    """
    kind = (rental_type or "").upper()
    if kind not in RENTAL_TYPES:
        raise ValidationException(
            f"Choose a rental type — {', '.join(RENTAL_TYPE_ORDER)}."
        )

    by_class, source = await load_rate_card(db, city_id)
    rows = by_class.get(vehicle_class) or []
    if not rows:
        raise ValidationException(
            "That vehicle class has no rate card in this city or on the platform "
            "default. Set its prices before booking it."
        )
    class_rows = [r for r in rows if str(r.get("rental_type") or "").upper() == kind]
    if not class_rows:
        available = sorted({str(r.get("rental_type") or "").upper() for r in rows})
        raise ValidationException(
            f"{vehicle_class} has no {kind} package. It is sold as "
            f"{', '.join(available)}."
        )

    trip_day = pickup_datetime.date() if pickup_datetime else None

    standard = calculate_rental_fare(
        rows,
        rental_type=kind,
        hours=hours,
        km=km,
        days=days,
        returned_after_midnight=returned_after_midnight,
    )

    custom = None
    applied = standard
    custom_applied = False
    if customer_id:
        custom = await load_custom_quote(
            db,
            customer_id=customer_id,
            city_id=city_id,
            vehicle_class=vehicle_class,
            rental_type=kind,
            included_hours=standard.package_hours,
            on_date=trip_day,
        )
        if custom is not None and custom.covers(trip_day):
            custom_applied = True
            applied = calculate_rental_fare(
                _overridden_rows(rows, custom, standard.package_hours),
                rental_type=kind,
                hours=hours,
                km=km,
                days=days,
                returned_after_midnight=returned_after_midnight,
            )
        elif custom is not None:
            # Present but not applicable — priced from the card, and said out
            # loud by the CUSTOM_QUOTE_EXPIRED validation below.
            pass

    sample = class_rows[0]
    validations = _validations(
        standard=standard,
        applied=applied,
        custom=custom,
        custom_applied=custom_applied,
        rate_source=source.get(vehicle_class, "PLATFORM"),
        city_id=city_id,
        pickup_datetime=pickup_datetime,
        hours=Decimal(str(hours or 0)),
        km=Decimal(str(km or 0)),
        days=days,
        class_rows=class_rows,
        vehicle_category_id=_field(sample, "vehicle_category_id"),
        effective_from=_field(sample, "effective_from"),
    )

    return RentalPackageQuote(
        city_id=city_id,
        rate_source=source.get(vehicle_class, "PLATFORM"),
        vehicle_class=vehicle_class,
        vehicle_label=_field(sample, "vehicle_label"),
        vehicle_category_id=_field(sample, "vehicle_category_id"),
        seating_capacity=_field(sample, "seating_capacity"),
        rental_type=kind,
        standard=standard,
        applied=applied,
        custom_quote=custom,
        custom_applied=custom_applied,
        difference=applied.total - standard.total,
        hours=Decimal(str(hours or 0)),
        km=Decimal(str(km or 0)),
        days=max(1, int(days or 1)),
        returned_after_midnight=returned_after_midnight,
        package_options=_package_options(class_rows, kind),
        validations=validations,
    )


def _validations(
    *,
    standard: RentalQuote,
    applied: RentalQuote,
    custom: Optional[CustomQuote],
    custom_applied: bool,
    rate_source: str,
    city_id: int,
    pickup_datetime: Optional[datetime],
    hours: Decimal,
    km: Decimal,
    days: int,
    class_rows: list[dict],
    vehicle_category_id: Optional[int],
    effective_from: Optional[date],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if pickup_datetime is not None:
        pickup = pickup_datetime
        if pickup.tzinfo is None:
            pickup = pickup.replace(tzinfo=timezone.utc)
        if pickup < datetime.now(timezone.utc):
            issues.append(
                ValidationIssue(
                    code="PICKUP_IN_PAST",
                    severity="ERROR",
                    field="pickup_datetime",
                    message=(
                        "The pickup time is in the past. Choose a time that has "
                        "not happened yet, or create the booking as a past trip "
                        "through Trip Assistance."
                    ),
                )
            )

    if standard.rental_type == "DAILY" and days < 1:
        issues.append(
            ValidationIssue(
                code="DAILY_DAYS_INVALID",
                severity="ERROR",
                field="days",
                message="A daily package needs at least one day.",
            )
        )

    if vehicle_category_id is None:
        issues.append(
            ValidationIssue(
                code="CLASS_NOT_MAPPED",
                severity="ERROR",
                field="vehicle_class",
                message=(
                    f"{standard.vehicle_class} is not linked to a bookable vehicle "
                    "category, so no partner fleet can take the job. Map it on the "
                    "rate card first."
                ),
            )
        )

    if effective_from and pickup_datetime:
        if effective_from > pickup_datetime.date():
            issues.append(
                ValidationIssue(
                    code="RATE_NOT_YET_EFFECTIVE",
                    severity="WARNING",
                    field="pickup_datetime",
                    message=(
                        f"These are the prices in force now; this class's card is "
                        f"dated {effective_from:%d %b %Y}, which is after the trip. "
                        "Confirm the fare before telling the customer."
                    ),
                )
            )

    if rate_source == "PLATFORM":
        issues.append(
            ValidationIssue(
                code="CITY_FALLBACK",
                severity="INFO",
                message=(
                    "This city has no rate card for the class, so platform default "
                    "prices apply."
                ),
            )
        )

    if hours > standard.package_hours and not any(
        int(r.get("included_hours") or 0) > standard.package_hours
        and int(r.get("included_km") or 0) >= km
        for r in class_rows
    ):
        issues.append(
            ValidationIssue(
                code="NO_LARGER_PACKAGE",
                severity="INFO",
                field="hours",
                message=(
                    f"{hours:g}h is beyond the largest package; the extra "
                    f"{standard.extra_hours:g}h is billed at "
                    f"₹{standard.extra_hour_rate}/hour."
                ),
            )
        )

    if standard.extra_hours > 0:
        issues.append(
            ValidationIssue(
                code="EXTRA_HOURS",
                severity="INFO",
                field="hours",
                message=(
                    f"{standard.extra_hours:g}h over the {standard.package_hours}h "
                    f"package × ₹{standard.extra_hour_rate} = "
                    f"₹{standard.extra_hour_charge}."
                ),
            )
        )
    if standard.extra_km > 0:
        issues.append(
            ValidationIssue(
                code="EXTRA_KM",
                severity="INFO",
                field="km",
                message=(
                    f"{standard.extra_km:g}km over the {standard.package_km}km "
                    f"package × ₹{standard.extra_km_rate} = "
                    f"₹{standard.extra_km_charge}."
                ),
            )
        )

    if standard.midnight_charge > 0:
        issues.append(
            ValidationIssue(
                code="MIDNIGHT_RETURN",
                severity="INFO",
                field="returned_after_midnight",
                message=(
                    f"Midnight return charge ₹{standard.midnight_charge} is added, "
                    "per the rate card."
                ),
            )
        )

    if standard.rental_type == "DAILY":
        issues.append(
            ValidationIssue(
                code="DAILY_INCLUSIONS",
                severity="INFO",
                message=(
                    f"Daily price covers {standard.package_hours}h and "
                    f"{standard.package_km}km per day, with driver allowance, "
                    "toll, parking and night halt included."
                ),
            )
        )
    elif not standard.toll_included or not standard.parking_included:
        issues.append(
            ValidationIssue(
                code="TOLL_PARKING_EXTRA",
                severity="INFO",
                message=(
                    "Toll and parking are not in the package price — the operator "
                    "bills the actual, so tell the customer before they agree."
                ),
            )
        )

    if custom is not None and custom_applied:
        changes = " and ".join(custom.diff_fields()) or "the agreed lines"
        delta = applied.total - standard.total
        if delta < 0:
            against = f"₹{abs(_money(delta))} below the rate card"
        elif delta > 0:
            against = f"₹{_money(delta)} above the rate card"
        else:
            against = "the same as the rate card"
        issues.append(
            ValidationIssue(
                code="CUSTOM_QUOTE_APPLIED",
                severity="INFO",
                message=f"Customer price applied: {changes} agreed, {against}.",
            )
        )
    elif custom is not None:
        window = []
        if custom.valid_from:
            window.append(f"from {custom.valid_from:%d %b %Y}")
        if custom.valid_to:
            window.append(f"until {custom.valid_to:%d %b %Y}")
        issues.append(
            ValidationIssue(
                code="CUSTOM_QUOTE_EXPIRED",
                severity="WARNING",
                message=(
                    "This customer has an agreed price for this package but it is "
                    f"not valid for this trip ({' '.join(window) or 'no window'}). "
                    "The rate card price is used."
                ),
            )
        )

    return issues


# ── Storing the quote on the booking ─────────────────────────


def rental_fare_columns(quote: RentalPackageQuote) -> dict[str, Any]:
    """The cab_bookings money columns for a package trip.

    The package price lands on ``base_fare_amount`` and the overflow on the
    distance/hour lines, so the invoice reads like every other cab invoice while
    the snapshot below says the price came from a package and not from
    kilometres. ``estimated_distance`` is the duty kilometres the package was
    quoted on — the basis of the extra-km line, not a measurement of a route.
    """
    q = quote.applied
    return {
        "pricing_city_id": quote.city_id,
        "distance_source": "PACKAGE",
        "billing_distance_km": _money(quote.km),
        "base_fare_amount": _money(q.package_price),
        "distance_charge_amount": _money(q.extra_km_charge),
        "driver_allowance_amount": _money(q.driver_allowance),
        # Limiting the column name to the line it stores: the midnight return
        # surcharge is a time-of-day charge, the same bucket as the per-km
        # engine's night charge, and the snapshot says which one it was.
        "night_charge_amount": _money(q.midnight_charge),
        "toll_amount": Decimal("0.00"),
        "waiting_charge_amount": _money(q.extra_hour_charge),
        "rate_snapshot": rental_rate_snapshot(quote),
    }


def rental_rate_snapshot(quote: RentalPackageQuote) -> dict[str, Any]:
    """What priced this trip, small enough to read on a dispute.

    Holds both sides of a negotiated price: the card's total and the customer's.
    Months later the question is "what did we charge and what should we have
    charged", and this answers it without a rate card that may since have moved.
    """
    std, app = quote.standard, quote.applied
    return {
        "mode": "PACKAGE",
        "rental_type": quote.rental_type,
        "vehicle_class": quote.vehicle_class,
        "vehicle_label": quote.vehicle_label,
        "vehicle_category_id": quote.vehicle_category_id,
        "rate_source": quote.rate_source,
        "city_id": quote.city_id,
        "package_hours": app.package_hours,
        "package_km": app.package_km,
        "days": app.days,
        "requested_hours": str(quote.hours),
        "requested_km": str(quote.km),
        "returned_after_midnight": quote.returned_after_midnight,
        "toll_included": app.toll_included,
        "parking_included": app.parking_included,
        "night_halt_included": app.night_halt_included,
        "standard": _quote_side(std),
        "applied": _quote_side(app),
        "difference": str(_money(quote.difference)),
        "custom_quote": quote.custom_quote.as_dict() if quote.custom_quote else None,
        "custom_quote_applied": quote.custom_applied,
        "validations": [v.as_dict() for v in quote.validations],
    }


def _quote_side(q: RentalQuote) -> dict[str, str]:
    return {
        "package_price": str(_money(q.package_price)),
        "extra_hours": str(q.extra_hours),
        "extra_hour_rate": str(_money(q.extra_hour_rate)),
        "extra_hour_charge": str(_money(q.extra_hour_charge)),
        "extra_km": str(q.extra_km),
        "extra_km_rate": str(_money(q.extra_km_rate)),
        "extra_km_charge": str(_money(q.extra_km_charge)),
        "driver_allowance": str(_money(q.driver_allowance)),
        "midnight_charge": str(_money(q.midnight_charge)),
        "total": str(_money(q.total)),
    }
