# ============================================================
# WAY TERO — HOTEL RATE RESOLUTION TESTS
# File: tests/unit/test_hotel_pricing.py
# Doc Ref: BRD Part 4 §70, SRS Part 5 §163-164
# ============================================================

from datetime import date
from decimal import Decimal

from app.modules.hotel.services.pricing import (
    RatePlanInput,
    resolve_nightly_rate,
    resolve_stay_rates,
)

BASE = Decimal("4000.00")


def _plan(**kw) -> RatePlanInput:
    defaults = dict(
        id=1,
        plan_name="Plan",
        plan_type="SEASONAL",
        priority=0,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 12, 31),
        day_of_week_mask=None,
        rate_mode="ABSOLUTE",
        rate_value=Decimal("5000.00"),
        min_nights=1,
        is_active=True,
    )
    defaults.update(kw)
    return RatePlanInput(**defaults)


class TestNightlyRate:
    def test_falls_back_to_base_price_with_no_plans(self):
        result = resolve_nightly_rate(date(2026, 3, 10), BASE, [])
        assert result.rate == BASE
        assert result.source == "BASE_PRICE"
        assert result.plan_id is None

    def test_applicable_plan_wins_over_base(self):
        result = resolve_nightly_rate(date(2026, 3, 10), BASE, [_plan()])
        assert result.rate == Decimal("5000.00")
        assert result.source == "RATE_PLAN"
        assert result.plan_name == "Plan"

    def test_plan_outside_date_range_is_ignored(self):
        plan = _plan(date_from=date(2026, 6, 1), date_to=date(2026, 6, 30))
        result = resolve_nightly_rate(date(2026, 3, 10), BASE, [plan])
        assert result.source == "BASE_PRICE"

    def test_inactive_plan_is_ignored(self):
        result = resolve_nightly_rate(date(2026, 3, 10), BASE, [_plan(is_active=False)])
        assert result.source == "BASE_PRICE"

    def test_inventory_override_beats_every_plan(self):
        """An admin who typed a number onto a specific date meant that number."""
        result = resolve_nightly_rate(
            date(2026, 3, 10),
            BASE,
            [_plan(plan_type="FESTIVAL", rate_value=Decimal("9000"))],
            inventory_override=Decimal("3300.00"),
        )
        assert result.rate == Decimal("3300.00")
        assert result.source == "INVENTORY_OVERRIDE"


class TestPrecedence:
    def test_festival_beats_seasonal_on_an_overlapping_date(self):
        """Diwali sits inside the winter season — both apply, festival wins."""
        seasonal = _plan(id=1, plan_type="SEASONAL", rate_value=Decimal("5000"))
        festival = _plan(id=2, plan_type="FESTIVAL", rate_value=Decimal("8000"))
        result = resolve_nightly_rate(date(2026, 11, 8), BASE, [seasonal, festival])
        assert result.rate == Decimal("8000.00")
        assert result.plan_id == 2

    def test_seasonal_beats_weekend(self):
        weekend = _plan(id=1, plan_type="WEEKEND", rate_value=Decimal("6000"))
        seasonal = _plan(id=2, plan_type="SEASONAL", rate_value=Decimal("5000"))
        result = resolve_nightly_rate(date(2026, 3, 14), BASE, [weekend, seasonal])
        assert result.plan_id == 2

    def test_weekend_beats_promotional(self):
        promo = _plan(id=1, plan_type="PROMOTIONAL", rate_value=Decimal("3000"))
        weekend = _plan(id=2, plan_type="WEEKEND", rate_value=Decimal("6000"))
        result = resolve_nightly_rate(date(2026, 3, 14), BASE, [promo, weekend])
        assert result.plan_id == 2

    def test_priority_breaks_a_same_type_tie(self):
        low = _plan(id=1, plan_type="SEASONAL", priority=1, rate_value=Decimal("5000"))
        high = _plan(id=2, plan_type="SEASONAL", priority=9, rate_value=Decimal("7000"))
        result = resolve_nightly_rate(date(2026, 3, 10), BASE, [low, high])
        assert result.plan_id == 2

    def test_newest_id_breaks_a_same_priority_tie(self):
        older = _plan(id=1, priority=5, rate_value=Decimal("5000"))
        newer = _plan(id=2, priority=5, rate_value=Decimal("5500"))
        result = resolve_nightly_rate(date(2026, 3, 10), BASE, [older, newer])
        assert result.plan_id == 2

    def test_resolution_is_order_independent(self):
        a = _plan(id=1, plan_type="SEASONAL", rate_value=Decimal("5000"))
        b = _plan(id=2, plan_type="FESTIVAL", rate_value=Decimal("8000"))
        forward = resolve_nightly_rate(date(2026, 3, 10), BASE, [a, b])
        reverse = resolve_nightly_rate(date(2026, 3, 10), BASE, [b, a])
        assert forward.plan_id == reverse.plan_id == 2


class TestDayOfWeekMask:
    def test_mask_restricts_a_plan_to_its_days(self):
        # Fri+Sat only. 2026-03-14 is a Saturday, 2026-03-11 a Wednesday.
        weekend = _plan(plan_type="WEEKEND", day_of_week_mask="0000110")
        assert (
            resolve_nightly_rate(date(2026, 3, 14), BASE, [weekend]).source
            == "RATE_PLAN"
        )
        assert (
            resolve_nightly_rate(date(2026, 3, 11), BASE, [weekend]).source
            == "BASE_PRICE"
        )

    def test_monday_is_the_first_mask_position(self):
        monday_only = _plan(day_of_week_mask="1000000")
        assert date(2026, 3, 9).weekday() == 0
        assert (
            resolve_nightly_rate(date(2026, 3, 9), BASE, [monday_only]).source
            == "RATE_PLAN"
        )
        assert (
            resolve_nightly_rate(date(2026, 3, 10), BASE, [monday_only]).source
            == "BASE_PRICE"
        )


class TestRateModes:
    def test_absolute_uses_the_value_directly(self):
        plan = _plan(rate_mode="ABSOLUTE", rate_value=Decimal("5500"))
        assert resolve_nightly_rate(date(2026, 3, 10), BASE, [plan]).rate == Decimal(
            "5500.00"
        )

    def test_percent_applies_a_delta_to_base(self):
        plan = _plan(rate_mode="PERCENT", rate_value=Decimal("25"))
        assert resolve_nightly_rate(date(2026, 3, 10), BASE, [plan]).rate == Decimal(
            "5000.00"
        )

    def test_negative_percent_discounts(self):
        plan = _plan(rate_mode="PERCENT", rate_value=Decimal("-10"))
        assert resolve_nightly_rate(date(2026, 3, 10), BASE, [plan]).rate == Decimal(
            "3600.00"
        )

    def test_delta_adds_a_flat_amount(self):
        plan = _plan(rate_mode="DELTA", rate_value=Decimal("750"))
        assert resolve_nightly_rate(date(2026, 3, 10), BASE, [plan]).rate == Decimal(
            "4750.00"
        )


class TestMinNights:
    def test_plan_is_skipped_below_its_minimum_stay(self):
        plan = _plan(min_nights=3, rate_value=Decimal("3200"))
        assert (
            resolve_nightly_rate(date(2026, 3, 10), BASE, [plan], nights=2).source
            == "BASE_PRICE"
        )
        assert (
            resolve_nightly_rate(date(2026, 3, 10), BASE, [plan], nights=3).source
            == "RATE_PLAN"
        )


class TestStayRates:
    def test_checkout_night_is_not_charged(self):
        quote = resolve_stay_rates(date(2026, 3, 10), date(2026, 3, 13), BASE, [])
        assert len(quote.nights) == 3
        assert quote.total == Decimal("12000.00")

    def test_same_day_stay_produces_no_nights(self):
        quote = resolve_stay_rates(date(2026, 3, 10), date(2026, 3, 10), BASE, [])
        assert quote.nights == []
        assert quote.total == Decimal("0.00")
        assert quote.average_nightly_rate == Decimal("0.00")

    def test_mixed_rates_across_a_stay(self):
        festival = _plan(
            plan_type="FESTIVAL",
            date_from=date(2026, 3, 11),
            date_to=date(2026, 3, 11),
            rate_value=Decimal("9000"),
        )
        quote = resolve_stay_rates(
            date(2026, 3, 10), date(2026, 3, 13), BASE, [festival]
        )
        assert [n.rate for n in quote.nights] == [
            Decimal("4000.00"),
            Decimal("9000.00"),
            Decimal("4000.00"),
        ]
        assert quote.total == Decimal("17000.00")

    def test_per_date_overrides_apply_to_the_right_nights(self):
        quote = resolve_stay_rates(
            date(2026, 3, 10),
            date(2026, 3, 12),
            BASE,
            [],
            inventory_overrides={date(2026, 3, 11): Decimal("2500")},
        )
        assert quote.nights[0].rate == Decimal("4000.00")
        assert quote.nights[1].rate == Decimal("2500.00")
        assert quote.total == Decimal("6500.00")

    def test_average_nightly_rate(self):
        quote = resolve_stay_rates(
            date(2026, 3, 10),
            date(2026, 3, 12),
            BASE,
            [],
            inventory_overrides={date(2026, 3, 11): Decimal("6000")},
        )
        assert quote.average_nightly_rate == Decimal("5000.00")
