# ============================================================
# WAY TERO — HOTEL BILLING: OVERTIME & NIGHT COUNT TESTS
# File: tests/unit/test_hotel_billing_overtime.py
# Doc Ref: BRD Part 4 §70, SRS Part 5 §163-164
#
# Guards the real-world check-out rules:
#   - checkout time is India-local (Asia/Kolkata), so naive stamps (partner
#     portal) and UTC stamps (admin portal) must both land on the same delta
#   - an overstay's extra nights are billed as room charge; overtime only
#     covers the partial time past the check-out hour on the actual departure
#     day (no double-billing)
#   - nights actually stayed are counted on India-local dates
# ============================================================

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.modules.hotel.services.billing import (
    IST,
    as_utc,
    actual_nights_of,
    compute_overtime,
)

CHECKOUT = time(hour=11, minute=0)
RATE = Decimal("4000.00")
SLAB_ARGS = dict(
    grace_minutes=60,
    mode="SLAB",
    hourly_percent=Decimal("10"),
    halfday_percent=Decimal("50"),
    fullday_percent=Decimal("100"),
    halfday_until_hours=6,
)


def _compute(actual, check_out_date=date(2026, 8, 16), **kw):
    kwargs = dict(
        check_out_date=check_out_date,
        actual_check_out_at=actual,
        nightly_rate=RATE,
        rooms_count=1,
        checkout_time=CHECKOUT,
    )
    kwargs.update(SLAB_ARGS)
    kwargs.update(kw)
    return compute_overtime(**kwargs)


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestCheckoutTimeZone:
    """Naive IST (partner) and UTC (admin) stamps must agree on the delta."""

    def test_on_time_in_ist_is_not_overtime(self):
        # 11:00 IST on the booked checkout date, sent as naive wall-clock.
        naive = datetime(2026, 8, 16, 11, 0)
        res = _compute(naive)
        assert not res.is_overtime
        assert res.overtime_hours == Decimal("0.00")

    def test_on_time_in_ist_sent_as_utc_is_not_overtime(self):
        # Same instant sent as UTC by the admin portal.
        res = _compute(as_utc(datetime(2026, 8, 16, 11, 0)))
        assert not res.is_overtime
        assert res.overtime_hours == Decimal("0.00")

    def test_late_afternoon_checkout_bills_overtime(self):
        # 16:00 IST = 3h late -> half-day slab. Old code treated 11:00 as UTC
        # and compared against 16:30 IST, so this used to be "on time".
        naive = datetime(2026, 8, 16, 16, 0)
        res = _compute(naive)
        assert res.is_overtime
        assert res.slab == "HALF_DAY"
        assert res.charge == Decimal("2000.00")  # 50% of one night

    def test_same_instant_via_utc_gives_same_charge(self):
        naive = datetime(2026, 8, 16, 16, 0)
        aware = as_utc(naive)
        a = _compute(naive)
        b = _compute(aware)
        assert a.charge == b.charge
        assert a.raw_hours == b.raw_hours

    def test_within_grace_window_is_free(self):
        # 11:30 IST, 30 min late — inside the 60-minute grace.
        res = _compute(datetime(2026, 8, 16, 11, 30))
        assert not res.is_overtime
        assert "grace" in res.reason.lower()


class TestOverstay:
    """Extra nights are room charge; overtime is only the partial final day."""

    def test_overnight_overstay_charges_only_late_portion(self):
        # Booked out 16 Aug, guest leaves 17 Aug 13:00 IST. Room charge (built
        # from actual_nights) already bills the extra night; overtime should be
        # the 2h past 11:00 on the 17th -> half-day, NOT a full day measured
        # from the 16th.
        res = _compute(datetime(2026, 8, 17, 13, 0))
        assert res.is_overtime
        assert res.slab == "HALF_DAY"
        assert res.charge == Decimal("2000.00")

    def test_overstay_before_checkout_hour_next_day_is_free(self):
        # Guest stays an extra night but leaves at 10:00 — no late fee.
        res = _compute(datetime(2026, 8, 17, 10, 0))
        assert not res.is_overtime

    def test_two_night_overstay_bills_one_full_day(self):
        # Leaves 18 Aug at 14:00 IST (3h past 11:00 on the 18th) -> half-day.
        res = _compute(datetime(2026, 8, 18, 14, 0))
        assert res.slab == "HALF_DAY"
        assert res.charge == Decimal("2000.00")

    def test_early_checkout_owes_nothing(self):
        # Guest checks out a day early — releases the room, no late fee.
        res = _compute(datetime(2026, 8, 15, 14, 0))
        assert not res.is_overtime

    def test_long_overrun_bills_full_day(self):
        # 8h past the check-out hour on the actual departure day -> FULL_DAY.
        res = _compute(datetime(2026, 8, 17, 19, 0))
        assert res.slab == "FULL_DAY"
        assert res.charge == Decimal("4000.00")


class TestHourlyMode:
    def test_hourly_rounds_up(self):
        res = _compute(
            datetime(2026, 8, 16, 13, 10),
            mode="HOURLY",
            hourly_percent=Decimal("10"),
        )
        assert res.is_overtime
        assert res.charge == Decimal("800.00")  # 2h x 10% of one night


class TestActualNights:
    def _hr(self, cin, cout):
        return SimpleNamespace(
            actual_check_in_at=cin,
            actual_check_out_at=cout,
        )

    def test_two_ist_nights_across_midnight(self):
        # Check in 14 Aug 23:30 IST, out 16 Aug 01:30 IST = 2 nights. On UTC
        # dates this would read 1 night (14th -> 15th).
        cin = as_utc(datetime(2026, 8, 14, 23, 30))
        cout = as_utc(datetime(2026, 8, 16, 1, 30))
        assert actual_nights_of(self._hr(cin, cout)) == 2

    def test_single_night(self):
        cin = as_utc(datetime(2026, 8, 14, 12, 0))
        cout = as_utc(datetime(2026, 8, 15, 10, 0))
        assert actual_nights_of(self._hr(cin, cout)) == 1

    def test_day_use_is_one_night(self):
        cin = as_utc(datetime(2026, 8, 14, 10, 0))
        cout = as_utc(datetime(2026, 8, 14, 18, 0))
        assert actual_nights_of(self._hr(cin, cout)) == 1

    def test_missing_stamp_returns_none(self):
        hr = SimpleNamespace(actual_check_in_at=None, actual_check_out_at=None)
        assert actual_nights_of(hr) is None

    def test_overstay_night_count(self):
        cin = as_utc(datetime(2026, 8, 14, 12, 0))
        cout = as_utc(datetime(2026, 8, 17, 13, 0))
        assert actual_nights_of(self._hr(cin, cout)) == 3


class TestTzHelpers:
    def test_ist_offset(self):
        assert IST.utcoffset(datetime(2026, 1, 1)) == timedelta(hours=5, minutes=30)

    def test_as_utc_treats_naive_as_ist(self):
        naive = datetime(2026, 8, 16, 14, 0)
        utc = as_utc(naive)
        assert utc.tzinfo is timezone.utc
        assert utc.hour == 8 and utc.minute == 30

    def test_as_utc_converts_aware_utc(self):
        aware = _utc(2026, 8, 16, 8, 30)
        assert as_utc(aware) == aware
