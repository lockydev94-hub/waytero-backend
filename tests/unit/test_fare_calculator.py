# ============================================================
# WAY TERO — CAB FARE CALCULATOR TESTS
# File: tests/unit/test_fare_calculator.py
# Doc Ref: BRD Part 3 §35 — Fare engine formula
#
# Covers the centralised calculator. All call sites (admin close-trip,
# partner close-trip, partner pricing preview, admin cab-add) must agree
# with this function.
# ============================================================

from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.modules.booking.services.fare import (
    calculate_fare,
    calculate_fare_breakdown,
    is_night_trip,
    trip_days_from,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _rule(**kw):
    base = dict(
        base_fare=Decimal("100"),
        minimum_km=Decimal("5"),
        per_km_rate=Decimal("20"),
        driver_allowance=Decimal("0"),
        night_charge=Decimal("0"),
    )
    base.update(kw)
    return base


# ── calculate_fare ─────────────────────────────────────────────────────────


def test_short_trip_only_base():
    """Trip under minimum_km — only base_fare + allowance + night."""
    fare = calculate_fare(_rule(), 3.0)
    assert fare == Decimal("100.00")


def test_long_trip_includes_distance():
    """Trip beyond minimum_km adds per_km × extra_km."""
    fare = calculate_fare(_rule(), 8.0)  # 3 extra km × 20
    assert fare == Decimal("160.00")


def test_exactly_minimum_no_distance_charge():
    fare = calculate_fare(_rule(), 5.0)
    assert fare == Decimal("100.00")


def test_driver_allowance_always_applied():
    """Allowance is unconditional — not tied to night or distance."""
    fare = calculate_fare(_rule(driver_allowance=Decimal("300")), 3.0)
    assert fare == Decimal("400.00")


def test_night_charge_day_trip_excluded():
    """Daytime trip → night_charge ignored."""
    start = datetime(2026, 8, 10, 10, 0, tzinfo=IST)
    end = datetime(2026, 8, 10, 11, 0, tzinfo=IST)
    fare = calculate_fare(_rule(night_charge=Decimal("150")), 8.0, start, end)
    assert fare == Decimal("160.00")  # 100 + 3×20 + 0 + 0


def test_night_charge_night_trip_included():
    """Trip fully inside the night window → night_charge added."""
    start = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 1, 0, tzinfo=IST)
    fare = calculate_fare(_rule(night_charge=Decimal("150")), 3.0, start, end)
    # base 100 + 0 distance (under min) + 0 allowance + night 150
    assert fare == Decimal("250.00")


def test_night_charge_trip_crossing_midnight():
    """Trip crossing 00:00 still counts as night for the late-evening half."""
    start = datetime(2026, 8, 10, 21, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 5, 0, tzinfo=IST)
    fare = calculate_fare(_rule(night_charge=Decimal("200")), 10.0, start, end)
    # base 100 + 5×20 (10 − 5 min) + 0 + 200 night
    assert fare == Decimal("400.00")


def test_all_components():
    """Long night trip with allowance — every line item included."""
    start = datetime(2026, 8, 10, 23, 30, tzinfo=IST)
    end = datetime(2026, 8, 11, 2, 0, tzinfo=IST)
    rule = _rule(
        base_fare=Decimal("200"),
        minimum_km=Decimal("10"),
        per_km_rate=Decimal("15"),
        driver_allowance=Decimal("300"),
        night_charge=Decimal("100"),
    )
    fare = calculate_fare(rule, 25.0, start, end)
    # base 200 + 15×15 (25 − 10) + 300 + 100 = 825
    assert fare == Decimal("825.00")


def test_missing_rule_fields_default_to_zero():
    """Rule with only base_fare set — other components zero."""
    fare = calculate_fare({"base_fare": Decimal("100")}, 50.0)
    assert fare == Decimal("100.00")


def test_zero_distance():
    fare = calculate_fare(_rule(), 0.0)
    assert fare == Decimal("100.00")


def test_negative_distance_guarded():
    fare = calculate_fare(_rule(), -5.0)
    assert fare == Decimal("100.00")


def test_rounds_to_two_decimals():
    """Per-km × extra-km may produce long decimals — must round to 2dp."""
    rule = _rule(
        base_fare=Decimal("0"),
        minimum_km=Decimal("0"),
        per_km_rate=Decimal("0.333"),
    )
    fare = calculate_fare(rule, 3.0)
    # 3 × 0.333 = 0.999 → 1.00
    assert fare == Decimal("1.00")


def test_no_timestamps_skips_night():
    """Without timestamps the night check must default to False."""
    fare = calculate_fare(_rule(night_charge=Decimal("500")), 5.0)
    assert fare == Decimal("100.00")


# ── calculate_fare_breakdown ──────────────────────────────────────────────


def test_breakdown_structure_and_total():
    start = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 1, 0, tzinfo=IST)
    bd = calculate_fare_breakdown(
        _rule(
            base_fare=Decimal("100"),
            minimum_km=Decimal("5"),
            per_km_rate=Decimal("20"),
            driver_allowance=Decimal("50"),
            night_charge=Decimal("150"),
        ),
        12.0,
        start,
        end,
    )
    assert bd["base_fare"] == Decimal("100.00")
    assert bd["distance_charge"] == Decimal("140.00")  # (12 − 5) × 20
    assert bd["driver_allowance"] == Decimal("50.00")
    assert bd["night_charge_applied"] == Decimal("150.00")
    assert bd["is_night"] is True
    assert bd["minimum_km"] == 5
    assert bd["extra_km"] == 7.0
    assert bd["total"] == Decimal("440.00")


def test_breakdown_day_trip_zero_night():
    bd = calculate_fare_breakdown(_rule(), 5.0)
    assert bd["night_charge_applied"] == Decimal("0")
    assert bd["is_night"] is False


# ── is_night_trip ─────────────────────────────────────────────────────────


def test_night_trip_window_wraps_midnight():
    start = datetime(2026, 8, 10, 22, 0, tzinfo=IST)
    end = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    assert is_night_trip(start, end) is True


def test_night_trip_early_morning():
    start = datetime(2026, 8, 10, 4, 0, tzinfo=IST)
    end = datetime(2026, 8, 10, 5, 30, tzinfo=IST)
    assert is_night_trip(start, end) is True


def test_day_trip_not_night():
    start = datetime(2026, 8, 10, 9, 0, tzinfo=IST)
    end = datetime(2026, 8, 10, 17, 0, tzinfo=IST)
    assert is_night_trip(start, end) is False


def test_missing_timestamps_returns_false():
    assert is_night_trip(None, datetime.now(IST)) is False
    assert is_night_trip(datetime.now(IST), None) is False


def test_trip_bracketing_night_start():
    """Day trip that ends right at 22:00 — night window is [22:00, 24:00)."""
    start = datetime(2026, 8, 10, 21, 0, tzinfo=IST)
    end = datetime(2026, 8, 10, 21, 59, tzinfo=IST)
    assert is_night_trip(start, end) is False


def test_naive_datetime_treated_as_ist():
    """Naive datetimes are assumed IST (the BRD convention)."""
    start = datetime(2026, 8, 10, 23, 0)
    end = datetime(2026, 8, 10, 23, 30)
    assert is_night_trip(start, end) is True


# ── Billable-minimum distance for OUTSTATION / ONE_WAY / ROUND_TRIP ────────
# Migration 0049: when trip_type is OUTSTATION-family, the per-km rate
# applies to MAX(actual_km, minimum_km). For LOCAL/AIRPORT the old
# "first N km included" logic is unchanged.


def test_outstation_under_minimum_bills_minimum():
    """65 km trip on minimum=100 → billable = 100 km (not 65 × rate × 0)."""
    rule = _rule(
        base_fare=Decimal("0"),
        minimum_km=Decimal("100"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("300"),
        driver_allowance_type="PER_TRIP",
    )
    fare = calculate_fare(rule, 65.0, trip_type="OUTSTATION")
    # base 0 + distance 100 × 14 + allowance 300 = 1700
    assert fare == Decimal("1700.00")


def test_one_way_under_minimum_bills_minimum():
    rule = _rule(
        minimum_km=Decimal("80"),
        per_km_rate=Decimal("12"),
        driver_allowance_type="PER_TRIP",
    )
    fare = calculate_fare(rule, 50.0, trip_type="ONE_WAY")
    # base 100 + (max(50, 80) × 12) + 0 + 0 = 100 + 960 = 1060
    assert fare == Decimal("1060.00")


def test_round_trip_bills_billable_not_actual():
    """Actual below minimum → MINIMUM still wins."""
    rule = _rule(
        base_fare=Decimal("300"),
        minimum_km=Decimal("100"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("0"),
        driver_allowance_type="PER_TRIP",
    )
    fare = calculate_fare(rule, 50.0, trip_type="ROUND_TRIP")
    # 300 + 100 × 14 = 1700
    assert fare == Decimal("1700.00")


def test_outstation_above_minimum_bills_actual():
    """Actual above minimum → ACTUAL wins (no regression)."""
    rule = _rule(
        base_fare=Decimal("0"),
        minimum_km=Decimal("60"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("0"),
        driver_allowance_type="PER_TRIP",
    )
    fare = calculate_fare(rule, 150.0, trip_type="OUTSTATION")
    # 0 + 150 × 14 + 0 = 2100
    assert fare == Decimal("2100.00")


def test_local_under_minimum_no_distance_charge():
    """LOCAL: under-minimum trips never bill distance (package-style)."""
    rule = _rule(
        base_fare=Decimal("800"),
        minimum_km=Decimal("80"),
        per_km_rate=Decimal("12"),
        driver_allowance=Decimal("250"),
        driver_allowance_type="PER_TRIP",
    )
    fare = calculate_fare(rule, 50.0, trip_type="LOCAL")
    # base 800 + 0 distance (under min) + 250 = 1050
    assert fare == Decimal("1050.00")


def test_airport_under_minimum_no_distance_charge():
    """AIRPORT behaves as package-style (same as LOCAL)."""
    rule = _rule(
        base_fare=Decimal("500"),
        minimum_km=Decimal("20"),
        per_km_rate=Decimal("18"),
        driver_allowance=Decimal("0"),
        driver_allowance_type="PER_TRIP",
    )
    fare = calculate_fare(rule, 15.0, trip_type="AIRPORT")
    # base 500 + 0 distance + 0 = 500
    assert fare == Decimal("500.00")


def test_legacy_caller_with_no_trip_type_defaults_to_local():
    """Calling without trip_type keeps legacy package behaviour."""
    rule = _rule(
        base_fare=Decimal("100"),
        minimum_km=Decimal("10"),
        per_km_rate=Decimal("5"),
        driver_allowance=Decimal("50"),
    )
    # trip_type=None → legacy: 5 km trip on min 10 km → bill base + 0 dist + da
    fare = calculate_fare(rule, 5.0)
    assert fare == Decimal("150.00")


# ── Driver allowance: PER_TRIP / PER_DAY / PER_KM / NONE ────────────────────


def test_driver_allowance_per_trip_default():
    """PER_TRIP (legacy) — flat amount, multiplied by 1."""
    rule = _rule(driver_allowance=Decimal("300"), driver_allowance_type="PER_TRIP")
    fare = calculate_fare(rule, 8.0, trip_type="LOCAL")
    # 100 + 3 × 20 + 300 = 460
    assert fare == Decimal("460.00")


def test_driver_allowance_per_day_single_day():
    """PER_DAY with 1 day = flat amount (matches PER_TRIP for single day)."""
    rule = _rule(driver_allowance=Decimal("300"), driver_allowance_type="PER_DAY")
    fare = calculate_fare(rule, 8.0, trip_type="LOCAL", trip_days=1)
    # 100 + 3 × 20 + 300 × 1 = 460
    assert fare == Decimal("460.00")


def test_driver_allowance_per_day_multi_day():
    """PER_DAY scales by trip_days — 2-day trip bills 2 × flat."""
    rule = _rule(driver_allowance=Decimal("300"), driver_allowance_type="PER_DAY")
    fare = calculate_fare(rule, 8.0, trip_type="LOCAL", trip_days=2)
    # 100 + 3 × 20 + 300 × 2 = 760
    assert fare == Decimal("760.00")


def test_driver_allowance_per_day_three_day_round_trip():
    """Bhubaneswar→Puri→Bhubaneswar over 3 days on ROUND_TRIP rule."""
    rule = _rule(
        base_fare=Decimal("300"),
        minimum_km=Decimal("100"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("300"),
        driver_allowance_type="PER_DAY",
    )
    fare = calculate_fare(rule, 100.0, trip_type="ROUND_TRIP", trip_days=3)
    # 300 + 100 × 14 + 300 × 3 = 300 + 1400 + 900 = 2600
    assert fare == Decimal("2600.00")


def test_driver_allowance_per_km():
    """PER_KM scales by billable_km."""
    rule = _rule(
        base_fare=Decimal("0"),
        minimum_km=Decimal("0"),
        per_km_rate=Decimal("12"),
        driver_allowance=Decimal("2"),
        driver_allowance_type="PER_KM",
    )
    fare = calculate_fare(rule, 50.0, trip_type="OUTSTATION")
    # 0 + 50 × 12 + 2 × 50 (billable) = 600 + 100 = 700
    assert fare == Decimal("700.00")


def test_driver_allowance_per_km_uses_billable_for_outstation():
    """PER_KM on OUTSTATION uses billable_km, not actual, under-minimum trip."""
    rule = _rule(
        base_fare=Decimal("0"),
        minimum_km=Decimal("100"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("1"),
        driver_allowance_type="PER_KM",
    )
    # actual 65, min 100 → billable = 100 → distance = 100 × 14 = 1400,
    # allowance = 1 × 100 = 100
    fare = calculate_fare(rule, 65.0, trip_type="OUTSTATION")
    assert fare == Decimal("1500.00")


def test_driver_allowance_none_zeros_line():
    """NONE means the customer doesn't pay any allowance."""
    rule = _rule(
        driver_allowance=Decimal("999"),
        driver_allowance_type="NONE",
    )
    fare = calculate_fare(rule, 8.0, trip_type="LOCAL")
    # 100 + 3 × 20 + 0 = 160
    assert fare == Decimal("160.00")


def test_driver_allowance_unknown_type_falls_back_to_per_trip():
    """Future enum members: unknown values default to PER_TRIP behaviour."""
    rule = _rule(
        driver_allowance=Decimal("300"),
        driver_allowance_type="NOT_A_REAL_VALUE",
    )
    fare = calculate_fare(rule, 8.0, trip_type="LOCAL")
    # Falls back to PER_TRIP: 100 + 3 × 20 + 300 = 460
    assert fare == Decimal("460.00")


def test_driver_allowance_per_day_none_trip_days_defaults_to_one():
    """trip_days=None or 0 → at least 1 day (no zero allowance)."""
    rule = _rule(driver_allowance=Decimal("500"), driver_allowance_type="PER_DAY")
    fare = calculate_fare(rule, 5.0, trip_type="LOCAL", trip_days=None)
    # 100 + 0 + 500 × 1 = 600
    assert fare == Decimal("600.00")


# ── trip_days_from helper ───────────────────────────────────────────────────


def test_trip_days_same_day_is_one():
    pickup = datetime(2026, 8, 13, 9, 0)
    ret = datetime(2026, 8, 13, 21, 0)
    assert trip_days_from(pickup, ret) == 1


def test_trip_days_one_night_is_two():
    pickup = datetime(2026, 8, 13, 22, 0)
    ret = datetime(2026, 8, 14, 6, 0)
    assert trip_days_from(pickup, ret) == 2


def test_trip_days_two_nights_is_three():
    pickup = datetime(2026, 8, 13, 10, 0)
    ret = datetime(2026, 8, 15, 18, 0)
    assert trip_days_from(pickup, ret) == 3


def test_trip_days_missing_inputs_default_to_one():
    assert trip_days_from(None, datetime(2026, 8, 13, 10, 0)) == 1
    assert trip_days_from(datetime(2026, 8, 13, 10, 0), None) == 1
    assert trip_days_from(None, None) == 1


def test_trip_days_return_before_pickup_clamped_to_one():
    """Backwards datetime → at least 1 day, no zero days."""
    pickup = datetime(2026, 8, 13, 10, 0)
    ret = datetime(2026, 8, 13, 9, 0)
    assert trip_days_from(pickup, ret) == 1


# ── Breakdown: new keys + billable/extra distinction ───────────────────────


def test_breakdown_includes_billable_km_for_outstation():
    """Billable_km appears in breakdown for UI to render the OUTSTATION line."""
    rule = _rule(
        base_fare=Decimal("300"),
        minimum_km=Decimal("100"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("0"),
    )
    bd = calculate_fare_breakdown(rule, 65.0, trip_type="OUTSTATION")
    assert bd["billable_km"] == 100.0
    assert bd["actual_distance_km"] == 65.0
    assert (
        bd["extra_km"] == 0.0
    )  # max(0, 65 − 100) = 0; UI uses billable for outstation
    assert bd["distance_charge"] == Decimal("1400.00")
    assert bd["driver_allowance_type"] == "PER_TRIP"
    assert bd["trip_days"] == 1


def test_breakdown_extra_km_for_local():
    """LOCAL breakdown exposes extra_km = actual − minimum for package rows."""
    rule = _rule(
        base_fare=Decimal("100"),
        minimum_km=Decimal("5"),
        per_km_rate=Decimal("20"),
        driver_allowance=Decimal("0"),
    )
    bd = calculate_fare_breakdown(rule, 12.0, trip_type="LOCAL")
    assert bd["billable_km"] == 7.0  # same as extra_km for local
    assert bd["extra_km"] == 7.0
    assert bd["distance_charge"] == Decimal("140.00")


def test_breakdown_driver_allowance_type_echoed():
    rule = _rule(driver_allowance=Decimal("300"), driver_allowance_type="PER_KM")
    bd = calculate_fare_breakdown(rule, 50.0, trip_type="OUTSTATION")
    assert bd["driver_allowance_type"] == "PER_KM"
    # billable_km = max(50, 5) = 50; PER_KM allowance = 300 × 50 = 15000
    assert bd["billable_km"] == 50.0
    assert bd["driver_allowance"] == Decimal("15000.00")


def test_breakdown_trip_days_persisted():
    """trip_days is part of the breakdown dict for UI labels."""
    rule = _rule(driver_allowance=Decimal("300"), driver_allowance_type="PER_DAY")
    bd = calculate_fare_breakdown(rule, 8.0, trip_type="ROUND_TRIP", trip_days=2)
    assert bd["trip_days"] == 2
    # allowance = 300 × 2 = 600
    # billable_km = max(8, 5) = 8; distance_charge = 8 × 20 = 160
    # base 100 + 160 + 600 = 860
    assert bd["driver_allowance"] == Decimal("600.00")
    assert bd["distance_charge"] == Decimal("160.00")
    assert bd["billable_km"] == 8.0
    assert bd["total"] == Decimal("860.00")


# ── toll & waiting charge (migration-adjacent additions) ───────────────────


def test_toll_added_flat():
    """Flat toll amount is added to the fare regardless of distance."""
    fare = calculate_fare(_rule(toll=Decimal("250")), 3.0)
    # base 100 + toll 250 = 350
    assert fare == Decimal("350.00")


def test_toll_with_distance_and_allowance():
    """Toll composes with distance charge and driver allowance."""
    rule = _rule(
        base_fare=Decimal("200"),
        minimum_km=Decimal("10"),
        per_km_rate=Decimal("14"),
        driver_allowance=Decimal("300"),
        toll=Decimal("200"),
    )
    fare = calculate_fare(rule, 65.0, trip_type="OUTSTATION")
    # billable = max(65, 10) = 65; 65×14 = 910
    # 200 + 910 + 300 + 200 = 1610
    assert fare == Decimal("1610.00")


def test_waiting_charge_15_minutes_blocks():
    """Free period consumed; extra waiting billed in 15-min blocks."""
    rule = _rule(
        free_waiting_minutes=Decimal("30"),
        actual_waiting_minutes=Decimal("80"),
        waiting_rate_per_hour=Decimal("100"),
        waiting_granularity="PER_15_MINUTES",
    )
    fare = calculate_fare(rule, 3.0)
    # chargeable 50min → ceil(50/15)=4 blocks × 25 = 100
    # base 100 + waiting 100 = 200
    assert fare == Decimal("200.00")


def test_waiting_within_free_period_no_charge():
    """Actual waiting inside the free window → no waiting line."""
    rule = _rule(
        free_waiting_minutes=Decimal("30"),
        actual_waiting_minutes=Decimal("20"),
        waiting_rate_per_hour=Decimal("100"),
        waiting_granularity="PER_15_MINUTES",
    )
    fare = calculate_fare(rule, 3.0)
    assert fare == Decimal("100.00")


def test_waiting_granularity_per_hour():
    rule = _rule(
        free_waiting_minutes=Decimal("0"),
        actual_waiting_minutes=Decimal("20"),
        waiting_rate_per_hour=Decimal("120"),
        waiting_granularity="PER_HOUR",
    )
    fare = calculate_fare(rule, 3.0)
    # ceil(20/60)=1 hour block × 120 = 120 → 100 + 120
    assert fare == Decimal("220.00")


def test_waiting_charge_shown_in_breakdown():
    rule = _rule(
        free_waiting_minutes=Decimal("30"),
        actual_waiting_minutes=Decimal("50"),
        waiting_rate_per_hour=Decimal("120"),
        waiting_granularity="PER_30_MINUTES",
    )
    bd = calculate_fare_breakdown(rule, 3.0)
    # chargeable 20min → ceil(20/30)=1 block × 60 = 60
    assert bd["waiting"] == Decimal("60.00")
    assert bd["total"] == Decimal("160.00")


def test_toll_shown_in_breakdown():
    bd = calculate_fare_breakdown(_rule(toll=Decimal("75")), 3.0)
    assert bd["toll"] == Decimal("75.00")
    assert bd["total"] == Decimal("175.00")


# ── night charge types (FIXED / PERCENTAGE / PER_KM) ───────────────────────


def test_night_charge_percentage():
    """PERCENTAGE night charge = % of the pre-night subtotal."""
    start = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 1, 0, tzinfo=IST)
    rule = _rule(
        base_fare=Decimal("1000"),
        night_charge=Decimal("10"),
        night_charge_type="PERCENTAGE",
    )
    fare = calculate_fare(rule, 3.0, start, end)
    # subtotal 1000 → night = 10% × 1000 = 100 → total 1100
    assert fare == Decimal("1100.00")


def test_night_charge_percentage_not_applied_by_day():
    start = datetime(2026, 8, 10, 10, 0, tzinfo=IST)
    end = datetime(2026, 8, 10, 12, 0, tzinfo=IST)
    rule = _rule(night_charge=Decimal("10"), night_charge_type="PERCENTAGE")
    fare = calculate_fare(rule, 3.0, start, end)
    assert fare == Decimal("100.00")


def test_night_charge_per_km():
    """PER_KM night charge = rate × billable km on a night trip."""
    start = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 1, 0, tzinfo=IST)
    rule = _rule(
        base_fare=Decimal("0"),
        minimum_km=Decimal("5"),
        per_km_rate=Decimal("20"),
        night_charge=Decimal("5"),
        night_charge_type="PER_KM",
    )
    fare = calculate_fare(rule, 8.0, start, end, trip_type="OUTSTATION")
    # billable = max(8,5) = 8; 8×20=160; night = 5×8 = 40 → 200
    assert fare == Decimal("200.00")


def test_night_charge_type_echoed_in_breakdown():
    start = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 1, 0, tzinfo=IST)
    bd = calculate_fare_breakdown(
        _rule(night_charge=Decimal("10"), night_charge_type="PERCENTAGE"),
        3.0,
        start,
        end,
    )
    assert bd["night_charge_type"] == "PERCENTAGE"
    assert bd["night_charge_applied"] == Decimal("10.00")
    assert bd["total"] == Decimal("110.00")


def test_unknown_night_charge_type_falls_back_fixed():
    start = datetime(2026, 8, 10, 23, 0, tzinfo=IST)
    end = datetime(2026, 8, 11, 1, 0, tzinfo=IST)
    fare = calculate_fare(
        _rule(night_charge=Decimal("200"), night_charge_type="WEEKEND"),
        3.0,
        start,
        end,
    )
    # unknown → FIXED → base 100 + night 200
    assert fare == Decimal("300.00")
