# ============================================================
# WAYTERO — CAB RENTAL RATE-CARD CALCULATOR
# File: app/modules/booking/services/rental_rates.py
# Doc Ref: Docs/01_Business_Requirements/SUPPLIER_CAB_PRICE_MASTER_V1.md
#          BRD Part 3 §35 (fare engine), §36 (city-based pricing)
#
# The supplier sheet (migration 0097, table `cab_rental_rates`) sells three
# rental types and this module is the one place that turns a package row into a
# price. It is deliberately separate from `fare.py`: that engine prices by
# kilometre (base + included km + per-km), while the supplier sheet quotes
# fixed hour/km packages — 5h/50km, 8h/80km, 10h/100km, 12h/120km for local,
# 12h/250km for outstation, 12h/100km per day for daily. Forcing packages
# through the per-km formula is what would silently drop the duration.
#
# Rules implemented (master §1):
#
#   LOCAL       take the smallest package that covers the requested hours AND
#               kilometres; anything beyond it is charged at the vehicle's
#               local extra-hour / extra-km rate. Both extra lines can apply.
#               A request larger than the biggest package stays on the biggest
#               package and pays the overflow as extras — never "no rate".
#
#   OUTSTATION  one package (12h/250km). Extra hours and kilometres beyond it
#               at the outstation rates, driver allowance per trip, and the
#               midnight-return charge when the same-day return happens after
#               00:00.
#
#   DAILY       price × days, with 12h and 100km allowed per day. Overflow is
#               charged at the outstation extra rates (the sheet quotes no
#               package-specific overtime), per master §1.
#
# Toll and parking are NOT added: where the sheet says "extra", the operator
# bills the actual, and inventing an estimate here would be a made-up number.
# Where the sheet says "included" (the daily package), the flags are on the row
# so a caller can say so instead of re-deriving it.
#
# Pure functions — no DB, no side effects. Callers fetch the rows
# (`cab_rental_rates` for the city, falling back to `city_id IS NULL`).
# ============================================================

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping, Optional

TWO_PLACES = Decimal("0.01")

RENTAL_TYPES = frozenset({"LOCAL", "OUTSTATION", "DAILY"})


def _field(row: Any, name: str) -> Any:
    """Read a column from a row mapping or an ORM object."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RentalQuote:
    """One priced rental package, with the lines that produced the total."""

    vehicle_class: str
    rental_type: str
    package_hours: int
    package_km: int
    package_price: Decimal
    extra_hours: Decimal
    extra_hour_rate: Decimal
    extra_hour_charge: Decimal
    extra_km: Decimal
    extra_km_rate: Decimal
    extra_km_charge: Decimal
    driver_allowance: Decimal
    midnight_charge: Decimal
    total: Decimal
    days: int = 1
    toll_included: bool = False
    parking_included: bool = False
    night_halt_included: bool = False


def _rates_of(rows: Iterable[Any], rental_type: str) -> list:
    found = [
        row
        for row in rows
        if (_field(row, "rental_type") or "").upper() == rental_type
        and _field(row, "is_active") is not False
    ]
    if not found:
        raise ValueError(f"No active {rental_type} rental rate was supplied")
    return found


def _pick_local_package(packages: list, hours: Decimal, km: Decimal):
    """Smallest package covering both limits, else the largest one."""
    ordered = sorted(
        packages,
        key=lambda r: (
            _decimal(_field(r, "included_hours")),
            _decimal(_field(r, "included_km")),
        ),
    )
    for package in ordered:
        if (
            _decimal(_field(package, "included_hours")) >= hours
            and _decimal(_field(package, "included_km")) >= km
        ):
            return package
    return ordered[-1]


def calculate_rental_fare(
    rates: Iterable[Any],
    *,
    rental_type: str,
    hours: float = 0,
    km: float = 0,
    days: int = 1,
    returned_after_midnight: bool = False,
) -> RentalQuote:
    """Price one supplier rental package.

    ``rates`` is every active row for one vehicle class (all rental types is
    fine — the right ones are selected here). ``hours``/``km`` are the trip's
    actual duty hours and distance; ``days`` is only read for DAILY.
    """
    kind = (rental_type or "").upper()
    if kind not in RENTAL_TYPES:
        raise ValueError(
            f"Unknown rental type {rental_type!r}; expected one of "
            f"{sorted(RENTAL_TYPES)}"
        )

    asked_hours = _decimal(hours)
    asked_km = _decimal(km)
    if asked_hours < 0 or asked_km < 0:
        raise ValueError("hours and km cannot be negative")

    rows = _rates_of(rates, kind)
    vehicle_class = _field(rows[0], "vehicle_class") or ""

    if kind == "LOCAL":
        package = _pick_local_package(rows, asked_hours, asked_km)
        package_days = 1
        driver_allowance = Decimal("0")
        midnight = Decimal("0")
    elif kind == "OUTSTATION":
        package = rows[0]
        package_days = 1
        driver_allowance = _decimal(_field(package, "driver_allowance"))
        midnight = (
            _decimal(_field(package, "midnight_return_charge"))
            if returned_after_midnight
            else Decimal("0")
        )
    else:  # DAILY
        package = rows[0]
        package_days = max(1, int(days or 1))
        # Driver allowance and night halt are inside the daily price.
        driver_allowance = Decimal("0")
        midnight = Decimal("0")

    package_hours = int(_decimal(_field(package, "included_hours")))
    package_km = int(_decimal(_field(package, "included_km")))
    package_price = _decimal(_field(package, "price"))

    allowed_hours = Decimal(package_hours) * package_days
    allowed_km = Decimal(package_km) * package_days

    extra_hours = max(Decimal("0"), asked_hours - allowed_hours)
    extra_km = max(Decimal("0"), asked_km - allowed_km)

    extra_hour_rate = _decimal(_field(package, "extra_hour_rate"))
    extra_km_rate = _decimal(_field(package, "extra_km_rate"))

    extra_hour_charge = _money(extra_hours * extra_hour_rate)
    extra_km_charge = _money(extra_km * extra_km_rate)
    package_total = _money(package_price * package_days)

    total = _money(
        package_total
        + extra_hour_charge
        + extra_km_charge
        + driver_allowance
        + midnight
    )

    return RentalQuote(
        vehicle_class=vehicle_class,
        rental_type=kind,
        package_hours=package_hours,
        package_km=package_km,
        package_price=package_total,
        extra_hours=extra_hours,
        extra_hour_rate=extra_hour_rate,
        extra_hour_charge=extra_hour_charge,
        extra_km=extra_km,
        extra_km_rate=extra_km_rate,
        extra_km_charge=extra_km_charge,
        driver_allowance=_money(driver_allowance),
        midnight_charge=_money(midnight),
        total=total,
        days=package_days,
        toll_included=bool(_field(package, "toll_included")),
        parking_included=bool(_field(package, "parking_included")),
        night_halt_included=bool(_field(package, "night_halt_included")),
    )


async def load_rental_rates(db: Any, vehicle_class: str, city_id: Optional[int]):
    """Fetch one vehicle class's sheet: the city's rows, else the platform's.

    The city sheet wins as a whole — a city that prices its Sedan does not
    inherit the platform's Sedan packages, because mixing two sheets in one
    quote is how a trip gets half a Bhubaneswar rate and half a default rate.
    """
    from sqlalchemy import text

    query = text(
        """
        SELECT * FROM cab_rental_rates
        WHERE vehicle_class = :vehicle_class
          AND is_active = TRUE
          AND city_id IS NOT DISTINCT FROM :city_id
        ORDER BY rental_type, included_hours
        """
    )
    if city_id is not None:
        city_rows = (
            (
                await db.execute(
                    query, {"vehicle_class": vehicle_class, "city_id": city_id}
                )
            )
            .mappings()
            .all()
        )
        if city_rows:
            return city_rows
    return (
        (await db.execute(query, {"vehicle_class": vehicle_class, "city_id": None}))
        .mappings()
        .all()
    )
