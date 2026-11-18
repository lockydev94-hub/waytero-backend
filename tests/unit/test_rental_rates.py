"""Unit tests for the supplier rental rate-card calculator.

The fixtures are the real seeded sheet: the rows come from migration 0097's
``_rows_for`` helper, so these tests fail if the seed and the master document
drift apart.
"""

import importlib.util
from pathlib import Path

import pytest

from app.modules.booking.services.rental_rates import calculate_rental_fare

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0097_cab_rental_rate_master.py"
)


def _load_sheet():
    spec = importlib.util.spec_from_file_location("cab_price_master_0097", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_SHEET = _load_sheet()


def rates_for(vehicle_class: str):
    return [r for r in _SHEET._rows_for(None) if r["vehicle_class"] == vehicle_class]


def test_local_package_exact_match_uses_that_package() -> None:
    # Sedan 8h/80km is quoted at ₹2,200 — no extras.
    quote = calculate_rental_fare(
        rates_for("SEDAN"), rental_type="LOCAL", hours=8, km=80
    )

    assert quote.package_price == 2200
    assert quote.extra_hour_charge == 0
    assert quote.extra_km_charge == 0
    assert quote.total == 2200


def test_local_package_smallest_covering_band_is_picked() -> None:
    # 6 hours / 60 km does not fit 5h/50km, so it steps up to 8h/80km (₹2,200)
    # rather than billing the 5h package plus overflow.
    quote = calculate_rental_fare(
        rates_for("SEDAN"), rental_type="LOCAL", hours=6, km=60
    )

    assert (quote.package_hours, quote.package_km) == (8, 80)
    assert quote.total == 2200


def test_local_overflow_bills_both_extra_hours_and_extra_km() -> None:
    # 13h / 140km on the 12h/120km Sedan package: ₹3,000 + 1×₹120 + 20×₹12.
    quote = calculate_rental_fare(
        rates_for("SEDAN"), rental_type="LOCAL", hours=13, km=140
    )

    assert quote.package_price == 3000
    assert quote.extra_hours == 1
    assert quote.extra_hour_charge == 120
    assert quote.extra_km == 20
    assert quote.extra_km_charge == 240
    assert quote.total == 3360


def test_outstation_adds_driver_allowance_and_extra_km() -> None:
    # Sedan outstation 12h/250km = ₹4,000 + 50 km × ₹12 + ₹150 driver.
    quote = calculate_rental_fare(
        rates_for("SEDAN"), rental_type="OUTSTATION", hours=12, km=300
    )

    assert quote.package_price == 4000
    assert quote.extra_km_charge == 600
    assert quote.driver_allowance == 150
    assert quote.midnight_charge == 0
    assert quote.total == 4750


def test_outstation_midnight_return_is_charged_separately() -> None:
    quote = calculate_rental_fare(
        rates_for("SEDAN"),
        rental_type="OUTSTATION",
        hours=14,
        km=260,
        returned_after_midnight=True,
    )

    assert quote.extra_hour_charge == 300  # 2 h × ₹150
    assert quote.extra_km_charge == 120  # 10 km × ₹12
    assert quote.midnight_charge == 300
    assert quote.total == 4000 + 300 + 120 + 150 + 300


def test_daily_package_multiplies_days_and_bills_overflow_per_day_allowance() -> None:
    # 2 days = 24h / 200km included at ₹3,500/day.
    quote = calculate_rental_fare(
        rates_for("SEDAN"), rental_type="DAILY", days=2, hours=26, km=230
    )

    assert quote.package_price == 7000
    assert quote.extra_km_charge == 360  # 30 km × ₹12
    assert quote.extra_hour_charge == 300  # 2 h × ₹150
    assert quote.driver_allowance == 0  # included in the daily price
    assert quote.toll_included is True
    assert quote.night_halt_included is True
    assert quote.total == 7660


def test_daily_package_never_bills_fewer_than_one_day() -> None:
    quote = calculate_rental_fare(
        rates_for("SEDAN"), rental_type="DAILY", days=0, hours=6, km=60
    )

    assert quote.days == 1
    assert quote.total == 3500


def test_bus_local_package_and_extra_km_rate_come_from_its_own_row() -> None:
    # 50+D non-pushback has a single 12h/120km band at ₹15,000, ₹60/km beyond.
    quote = calculate_rental_fare(
        rates_for("AC_BUS_50D"), rental_type="LOCAL", hours=10, km=130
    )

    assert (quote.package_hours, quote.package_km) == (12, 120)
    assert quote.package_price == 15000
    assert quote.extra_km_charge == 600  # 10 km × ₹60
    assert quote.total == 15600


def test_urbania_and_traveller_13_seaters_are_priced_apart() -> None:
    # Same seating, different supplier price — the reason vehicle_class is text.
    urbania = calculate_rental_fare(
        rates_for("URBANIA_13D"), rental_type="LOCAL", hours=10, km=100
    )
    traveller = calculate_rental_fare(
        rates_for("TRAVELLER_13D"), rental_type="LOCAL", hours=10, km=100
    )

    assert urbania.total == 8000
    assert traveller.total == 5000


def test_orm_style_rows_are_accepted() -> None:
    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    rows = [
        Row(
            rental_type="LOCAL",
            vehicle_class="SEDAN",
            included_hours=8,
            included_km=80,
            price="2200.00",
            extra_hour_rate="120",
            extra_km_rate="12",
            driver_allowance=None,
            midnight_return_charge=None,
            toll_included=False,
            parking_included=False,
            night_halt_included=False,
            is_active=True,
        )
    ]

    quote = calculate_rental_fare(rows, rental_type="LOCAL", hours=8, km=80)

    assert quote.total == 2200


def test_unknown_rental_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        calculate_rental_fare(rates_for("SEDAN"), rental_type="HOURLY", hours=2)


def test_missing_rate_for_the_requested_type_is_rejected() -> None:
    air_only = [{"rental_type": "LOCAL", "is_active": False}]
    with pytest.raises(ValueError):
        calculate_rental_fare(air_only, rental_type="LOCAL", hours=2, km=10)
