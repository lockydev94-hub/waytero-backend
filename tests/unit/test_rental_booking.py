# ============================================================
# WAYTERO — PACKAGE BOOKING QUOTE TESTS
# File: tests/unit/test_rental_booking.py
# Doc Ref: Docs/01_Business_Requirements/SUPPLIER_CAB_PRICE_MASTER_V1.md
#
# The money rules an admin's negotiated price has to obey:
#   • the card price is never modified by an override, both are always returned
#   • an override only touches the package it was agreed for
#   • an override outside its window does not price the trip, but is reported
#   • blocking validations (past pickup, unmapped class) stop a booking
#   • the snapshot stored on the booking is JSON-safe
#
# Fixtures come from the 0097 migration's own sheet data, so a change to the
# card breaks these tests rather than passing against stale numbers.
# ============================================================

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.modules.booking.services.rental_booking import (
    CustomQuote,
    RentalPackageQuote,
    _overridden_rows,
    _validations,
    rental_rate_snapshot,
)
from app.modules.booking.services.rental_rates import calculate_rental_fare

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0097_cab_rental_rate_master.py"
)

_spec = importlib.util.spec_from_file_location("cab_rates_0097", _MIGRATION)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

SEDAN_ROWS = [row for row in _module._rows_for(None) if row["vehicle_class"] == "SEDAN"]


def _rows(**overrides):
    """The Sedan card with one field changed on every row, for card tests."""
    return [{**row, **overrides} for row in SEDAN_ROWS]


def _quote(rows=None, hours=8, km=80, rental_type="LOCAL", days=1, midnight=False):
    rows = rows or SEDAN_ROWS
    return calculate_rental_fare(
        rows,
        rental_type=rental_type,
        hours=hours,
        km=km,
        days=days,
        returned_after_midnight=midnight,
    )


def _custom(**overrides):
    base = dict(
        id=1,
        customer_id=7,
        city_id=5,
        vehicle_class="SEDAN",
        rental_type="LOCAL",
        included_hours=8,
        package_price=Decimal("2000"),
        extra_hour_rate=None,
        extra_km_rate=None,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
        reason="annual contract",
        is_active=True,
    )
    base.update(overrides)
    return CustomQuote(**base)


def _issue_codes(quote):
    return [v.code for v in quote.validations]


def _package_quote(standard, applied, custom=None, applied_custom=False, **kwargs):
    validations = _validations(
        standard=standard,
        applied=applied,
        custom=custom,
        custom_applied=applied_custom,
        rate_source=kwargs.get("rate_source", "CITY"),
        city_id=5,
        pickup_datetime=kwargs.get("pickup_datetime"),
        hours=kwargs.get("hours", Decimal("8")),
        km=kwargs.get("km", Decimal("80")),
        days=kwargs.get("days", 1),
        class_rows=kwargs.get("class_rows", SEDAN_ROWS),
        vehicle_category_id=kwargs.get("vehicle_category_id", 2),
        effective_from=kwargs.get("effective_from"),
    )
    return RentalPackageQuote(
        city_id=5,
        rate_source=kwargs.get("rate_source", "CITY"),
        vehicle_class="SEDAN",
        vehicle_label="Sedan",
        vehicle_category_id=kwargs.get("vehicle_category_id", 2),
        seating_capacity=4,
        rental_type=standard.rental_type,
        standard=standard,
        applied=applied,
        custom_quote=custom,
        custom_applied=applied_custom,
        difference=applied.total - standard.total,
        hours=kwargs.get("hours", Decimal("8")),
        km=kwargs.get("km", Decimal("80")),
        days=kwargs.get("days", 1),
        returned_after_midnight=False,
        validations=validations,
    )


# ── Overrides ────────────────────────────────────────────────


def test_override_changes_only_the_agreed_package():
    custom = _custom()
    standard = _quote(hours=8, km=80)
    rows = _overridden_rows(SEDAN_ROWS, custom, standard.package_hours)
    applied = _quote(rows, hours=8, km=80)

    assert standard.package_hours == 8
    assert standard.total == Decimal("2200.00")
    assert applied.total == Decimal("2000.00")
    # The 12h/120km package is untouched: the agreement was about 8h/80km.
    twelve_hour = next(
        r for r in rows if r["included_hours"] == 12 and r["rental_type"] == "LOCAL"
    )
    assert twelve_hour["price"] == Decimal("3000")


def test_override_rates_flow_into_overflow():
    custom = _custom(package_price=None, extra_hour_rate=Decimal("100"))
    standard = _quote(hours=10, km=120)
    rows = _overridden_rows(SEDAN_ROWS, custom, standard.package_hours)
    applied = _quote(rows, hours=10, km=120)

    # 10h/120km is card-priced as 12h/120km ₹3,000 at the card; the agreed
    # extra-hour rate is not reached, so the package price is what decides.
    assert applied.package_hours == 12
    assert applied.total == Decimal("3000.00")


def test_override_not_applied_outside_window():
    custom = _custom(valid_to=date(2026, 1, 31))

    assert custom.covers(date(2026, 1, 15)) is True
    assert custom.covers(date(2026, 6, 1)) is False
    assert custom.covers(None) is True


def test_custom_quote_serialises_for_the_booking_snapshot():
    payload = _custom(package_price=Decimal("2000")).as_dict()

    # jsonb cannot take a Decimal; this is the regression that failed at INSERT.
    json.dumps(payload)
    assert payload["package_price"] == "2000.00"
    assert payload["valid_from"] == "2026-01-01"


# ── Validations ──────────────────────────────────────────────


def test_past_pickup_blocks_the_booking():
    standard = _quote()
    quote = _package_quote(
        standard,
        standard,
        pickup_datetime=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    assert "PICKUP_IN_PAST" in _issue_codes(quote)
    assert quote.errors, "a past pickup must be an error, not a note"


def test_unmapped_class_blocks_the_booking():
    standard = _quote()
    quote = _package_quote(standard, standard, vehicle_category_id=None)

    assert [v.code for v in quote.errors] == ["CLASS_NOT_MAPPED"]


def test_extras_and_midnight_are_explained():
    # Outstation is 12h/250km, so 14h and 300km both overflow it.
    standard = _quote(hours=14, km=300, rental_type="OUTSTATION", midnight=True)
    quote = _package_quote(
        standard,
        standard,
        hours=Decimal("14"),
        km=Decimal("300"),
    )
    codes = _issue_codes(quote)

    assert "EXTRA_HOURS" in codes
    assert "EXTRA_KM" in codes
    assert "MIDNIGHT_RETURN" in codes
    assert not quote.errors


def test_platform_fallback_is_said_out_loud():
    standard = _quote()
    quote = _package_quote(standard, standard, rate_source="PLATFORM")

    assert "CITY_FALLBACK" in _issue_codes(quote)


def test_custom_quote_validation_reports_the_difference():
    standard = _quote()
    applied = calculate_rental_fare(
        _overridden_rows(SEDAN_ROWS, _custom(), 8),
        rental_type="LOCAL",
        hours=8,
        km=80,
    )
    quote = _package_quote(standard, applied, custom=_custom(), applied_custom=True)

    assert quote.difference == Decimal("-200.00")
    message = next(
        v.message for v in quote.validations if v.code == "CUSTOM_QUOTE_APPLIED"
    )
    assert "200.00 below the rate card" in message


def test_lapsed_custom_quote_warns_and_prices_the_card():
    standard = _quote()
    quote = _package_quote(
        standard,
        standard,
        custom=_custom(valid_from=date(2026, 1, 1), valid_to=date(2026, 2, 1)),
        applied_custom=False,
    )
    codes = _issue_codes(quote)

    assert quote.total == Decimal("2200.00")
    assert "CUSTOM_QUOTE_EXPIRED" in codes
    assert "CUSTOM_QUOTE_APPLIED" not in codes


# ── Snapshot ─────────────────────────────────────────────────


def test_snapshot_keeps_both_sides_and_is_json_safe():
    standard = _quote()
    applied = calculate_rental_fare(
        _overridden_rows(SEDAN_ROWS, _custom(), 8),
        rental_type="LOCAL",
        hours=8,
        km=80,
    )
    quote = _package_quote(standard, applied, custom=_custom(), applied_custom=True)

    snapshot = rental_rate_snapshot(quote)
    json.dumps(snapshot)

    assert snapshot["mode"] == "PACKAGE"
    assert snapshot["standard"]["total"] == "2200.00"
    assert snapshot["applied"]["total"] == "2000.00"
    assert snapshot["difference"] == "-200.00"
    assert snapshot["custom_quote_applied"] is True


@pytest.mark.parametrize("rental_type", ["LOCAL", "OUTSTATION", "DAILY"])
def test_every_rental_type_is_json_safe(rental_type):
    rows = [
        {**row, "vehicle_category_id": 2}
        for row in _module._rows_for(None)
        if row["vehicle_class"] == "SEDAN"
    ]
    standard = _quote(rows, hours=30, km=300, rental_type=rental_type, days=2)
    quote = _package_quote(
        standard,
        standard,
        hours=Decimal("30"),
        km=Decimal("300"),
        days=2,
        class_rows=rows,
    )

    json.dumps(rental_rate_snapshot(quote))
