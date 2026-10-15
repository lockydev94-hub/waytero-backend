# ============================================================
# WAYTERO — OFFER SERVICE TESTS (migration 0077)
# File: tests/test_offers.py
#
# Pins the offer validation core with a scripted stub session —
# the same convention as tests/booking/test_partner_acceptance.py.
# Covers: badge derivation, window containment, display gate,
# eligibility matrix (cohort/city/service rules), booking-time
# scope enforcement (right vs wrong room type), attribution
# resolution (match/mismatch/none) and publish guards.
# ============================================================
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import ValidationException
from app.modules.admin import offer_service

TODAY = date.today()


# ── Stub plumbing ─────────────────────────────────────────────────


class _Result:
    """Just enough Result surface for the queries under test. Every accessor
    is served from the spec so a mis-ordered stub fails loudly, not silently."""

    def __init__(self, spec: Any) -> None:
        self._spec = spec

    def scalar_one_or_none(self) -> Any:
        return self._spec

    def scalar(self) -> Any:
        return self._spec

    def first(self) -> Any:
        if isinstance(self._spec, list):
            return self._spec[0] if self._spec else None
        return self._spec

    def mappings(self) -> "_Result":
        return self

    def scalars(self) -> "_Result":
        return self

    def all(self) -> Any:
        return self._spec if isinstance(self._spec, list) else [self._spec]


class _StubDb:
    """Async session that replays queued results in execute() order."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.executed.append((stmt, params))
        assert self._results, "stub db ran out of scripted results"
        return _Result(self._results.pop(0))


def _coupon(**over: Any) -> SimpleNamespace:
    base = dict(
        id=7,
        is_active=True,
        coupon_type="PLATFORM",
        apply_to="ALL",
        is_city_specific=False,
        valid_from=TODAY - timedelta(days=5),
        valid_to=TODAY + timedelta(days=30),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _offer(**over: Any) -> SimpleNamespace:
    base = dict(
        id=1,
        is_published=True,
        valid_from=TODAY - timedelta(days=1),
        valid_to=TODAY + timedelta(days=10),
        service_type="HOTEL",
        terms_and_conditions="T&C text",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _listing(**over: Any) -> dict:
    base = dict(partner_id=2, city_id=5, status="ACTIVE", granted=True)
    base.update(over)
    return base


# ── derive_badge ──────────────────────────────────────────────────


def test_badge_flat_integral_value() -> None:
    assert (
        offer_service.derive_badge("FLAT", Decimal("100.00"), None, None)
        == "FLAT ₹100 OFF"
    )


def test_badge_flat_keeps_fraction_when_not_integral() -> None:
    assert "99.5" in offer_service.derive_badge("FLAT", Decimal("99.50"), None, None)


def test_badge_percent_with_cap() -> None:
    assert (
        offer_service.derive_badge("PERCENT", Decimal("30"), Decimal("500.00"), None)
        == "UPTO 30% OFF (₹500 max)"
    )


def test_badge_percent_without_cap() -> None:
    assert (
        offer_service.derive_badge("PERCENT", Decimal("15"), None, None)
        == "UPTO 15% OFF"
    )


def test_badge_override_wins() -> None:
    assert (
        offer_service.derive_badge("FLAT", Decimal("100"), None, "MEGA DEAL")
        == "MEGA DEAL"
    )


# ── window_contained ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_window_inside_coupon_window_passes() -> None:
    await offer_service.window_contained(_coupon(), TODAY, TODAY + timedelta(days=5))


@pytest.mark.asyncio
async def test_window_beyond_coupon_window_rejected() -> None:
    with pytest.raises(ValidationException):
        await offer_service.window_contained(
            _coupon(), TODAY, TODAY + timedelta(days=90)
        )


@pytest.mark.asyncio
async def test_window_starting_before_coupon_window_rejected() -> None:
    with pytest.raises(ValidationException):
        await offer_service.window_contained(
            _coupon(), TODAY - timedelta(days=90), TODAY
        )


# ── offer_is_live (display gate) ──────────────────────────────────


@pytest.mark.asyncio
async def test_live_offer_passes_gate() -> None:
    assert await offer_service.offer_is_live(None, _offer(), _coupon()) is True


@pytest.mark.asyncio
async def test_unpublished_offer_not_live() -> None:
    assert (
        await offer_service.offer_is_live(None, _offer(is_published=False), _coupon())
        is False
    )


@pytest.mark.asyncio
async def test_offer_outside_own_window_not_live() -> None:
    past = _offer(valid_to=TODAY - timedelta(days=1))
    assert await offer_service.offer_is_live(None, past, _coupon()) is False


@pytest.mark.asyncio
async def test_coupon_flip_inactive_drops_offer_from_display() -> None:
    assert (
        await offer_service.offer_is_live(None, _offer(), _coupon(is_active=False))
        is False
    )


@pytest.mark.asyncio
async def test_coupon_window_lapse_drops_offer_from_display() -> None:
    expired_coupon = _coupon(valid_to=TODAY - timedelta(days=1))
    assert await offer_service.offer_is_live(None, _offer(), expired_coupon) is False


@pytest.mark.asyncio
async def test_missing_coupon_not_live() -> None:
    assert await offer_service.offer_is_live(None, _offer(), None) is False


# ── coupon_service_rules_cover ────────────────────────────────────


@pytest.mark.asyncio
async def test_no_service_rules_cover_everything() -> None:
    db = _StubDb([[]])
    assert await offer_service.coupon_service_rules_cover(db, 7, "CAB", 3) is True


@pytest.mark.asyncio
async def test_cab_rule_for_other_category_blocks() -> None:
    db = _StubDb([[{"service_type": "CAB", "vehicle_category_id": 9}]])
    assert await offer_service.coupon_service_rules_cover(db, 7, "CAB", 3) is False


@pytest.mark.asyncio
async def test_cab_rule_with_null_category_covers_all() -> None:
    db = _StubDb([[{"service_type": "CAB", "vehicle_category_id": None}]])
    assert await offer_service.coupon_service_rules_cover(db, 7, "CAB", 3) is True


@pytest.mark.asyncio
async def test_hotel_rule_ignores_vehicle_category() -> None:
    db = _StubDb([[{"service_type": "HOTEL", "vehicle_category_id": 9}]])
    assert await offer_service.coupon_service_rules_cover(db, 7, "HOTEL", None) is True


# ── evaluate_offer_eligibility (save-time matrix) ─────────────────


@pytest.mark.asyncio
async def test_hotel_target_eligible_platform_coupon() -> None:
    db = _StubDb([_listing()])
    ok, reasons, partner = await offer_service.evaluate_offer_eligibility(
        db, _coupon(), "HOTEL", hotel_id=1
    )
    assert ok, reasons
    assert partner == {"partner_id": 2, "granted": True, "in_cohort": True}


@pytest.mark.asyncio
async def test_apply_to_mismatch_blocks() -> None:
    db = _StubDb([_listing()])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(apply_to="CAB"), "HOTEL", hotel_id=1
    )
    assert not ok and "Coupon does not apply to this service" in reasons


@pytest.mark.asyncio
async def test_inactive_coupon_blocks() -> None:
    db = _StubDb([_listing()])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(is_active=False), "HOTEL", hotel_id=1
    )
    assert not ok and "Coupon is inactive" in reasons


@pytest.mark.asyncio
async def test_cab_without_category_blocked() -> None:
    db = _StubDb([])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(), "CAB", vehicle_category_id=None
    )
    assert not ok and "Select a cab category" in reasons


@pytest.mark.asyncio
async def test_cab_category_not_in_service_rules_blocked() -> None:
    db = _StubDb([[{"service_type": "CAB", "vehicle_category_id": 9}]])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(apply_to="CAB"), "CAB", vehicle_category_id=3
    )
    assert not ok and any("cab category" in r for r in reasons)


@pytest.mark.asyncio
async def test_missing_listing_blocked() -> None:
    db = _StubDb([None])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(), "HOTEL", hotel_id=999
    )
    assert not ok and "That listing no longer exists" in reasons


@pytest.mark.asyncio
async def test_inactive_listing_blocked() -> None:
    db = _StubDb([_listing(status="INACTIVE")])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(), "HOTEL", hotel_id=1
    )
    assert not ok and "That listing is not currently active" in reasons


@pytest.mark.asyncio
async def test_partner_not_granted_for_service_blocked() -> None:
    db = _StubDb([_listing(granted=False)])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(), "HOTEL", hotel_id=1
    )
    assert not ok and any("not approved" in r for r in reasons)


@pytest.mark.asyncio
async def test_partner_funded_coupon_requires_cohort_membership() -> None:
    # listing lookup, then cohort probe returns no row
    db = _StubDb([_listing(), None])
    ok, reasons, partner = await offer_service.evaluate_offer_eligibility(
        db, _coupon(coupon_type="PARTNER_ONLY"), "HOTEL", hotel_id=1
    )
    assert not ok and any("has not joined this coupon" in r for r in reasons)
    assert partner["in_cohort"] is False


@pytest.mark.asyncio
async def test_partner_funded_coupon_with_cohort_passes() -> None:
    db = _StubDb([_listing(), [1]])  # cohort row found
    ok, reasons, partner = await offer_service.evaluate_offer_eligibility(
        db, _coupon(coupon_type="PLATFORM_PARTNER"), "HOTEL", hotel_id=1
    )
    assert ok, reasons
    assert partner["in_cohort"] is True


@pytest.mark.asyncio
async def test_city_specific_coupon_blocks_outside_city() -> None:
    db = _StubDb([_listing(city_id=8), None])  # no coupon_city_rules row
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(is_city_specific=True), "HOTEL", hotel_id=1
    )
    assert not ok and any("eligible cities" in r for r in reasons)


@pytest.mark.asyncio
async def test_city_specific_coupon_allows_listed_city() -> None:
    db = _StubDb([_listing(city_id=8), [1]])
    ok, reasons, _ = await offer_service.evaluate_offer_eligibility(
        db, _coupon(is_city_specific=True), "HOTEL", hotel_id=1
    )
    assert ok, reasons


# ── booking_scope_block (manual-code-entry bypass guard) ─────────


@pytest.mark.asyncio
async def test_scope_no_live_offers_passes() -> None:
    db = _StubDb([[]])
    msg = await offer_service.booking_scope_block(
        db, 7, "HOTEL", hotel_id=1, room_category_id=2
    )
    assert msg is None


@pytest.mark.asyncio
async def test_scope_right_room_type_passes() -> None:
    db = _StubDb([[5], [1]])  # live offer ids, then coverage row found
    msg = await offer_service.booking_scope_block(
        db, 7, "HOTEL", hotel_id=1, room_category_id=2
    )
    assert msg is None


@pytest.mark.asyncio
async def test_scope_wrong_room_type_blocked() -> None:
    db = _StubDb([[5], None])
    msg = await offer_service.booking_scope_block(
        db, 7, "HOTEL", hotel_id=1, room_category_id=99
    )
    assert msg is not None and "only for the listings/rooms" in msg


@pytest.mark.asyncio
async def test_scope_tour_package_not_covered_blocked() -> None:
    db = _StubDb([[5], None])
    msg = await offer_service.booking_scope_block(db, 7, "TOUR", tour_package_id=3)
    assert msg is not None


@pytest.mark.asyncio
async def test_scope_cab_category_mismatch_blocked() -> None:
    db = _StubDb([[5], None])
    msg = await offer_service.booking_scope_block(db, 7, "CAB", vehicle_category_id=3)
    assert msg is not None


# ── resolve_offer_for_booking (attribution never breaks booking) ──


@pytest.mark.asyncio
async def test_attribution_without_coupon_is_none() -> None:
    db = _StubDb([])
    assert (
        await offer_service.resolve_offer_for_booking(
            db,
            coupon_id=None,
            service_type="HOTEL",
            vehicle_category_id=None,
            hotel_id=1,
            room_category_id=2,
            tour_package_id=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_attribution_no_live_offers_is_none() -> None:
    db = _StubDb([[]])
    got = await offer_service.resolve_offer_for_booking(
        db,
        coupon_id=7,
        service_type="HOTEL",
        vehicle_category_id=None,
        hotel_id=1,
        room_category_id=2,
        tour_package_id=None,
    )
    assert got is None


@pytest.mark.asyncio
async def test_attribution_hotel_match_returns_offer_id() -> None:
    db = _StubDb([[{"id": 5, "vehicle_category_id": None}], 5])
    got = await offer_service.resolve_offer_for_booking(
        db,
        coupon_id=7,
        service_type="HOTEL",
        vehicle_category_id=None,
        hotel_id=1,
        room_category_id=2,
        tour_package_id=None,
    )
    assert got == 5


@pytest.mark.asyncio
async def test_attribution_cab_forge_mismatch_returns_none() -> None:
    # client claims an offer slug but booked a different vehicle category
    db = _StubDb([[{"id": 5, "vehicle_category_id": 9}]])
    got = await offer_service.resolve_offer_for_booking(
        db,
        coupon_id=7,
        service_type="CAB",
        vehicle_category_id=3,
        hotel_id=None,
        room_category_id=None,
        tour_package_id=None,
    )
    assert got is None


@pytest.mark.asyncio
async def test_attribution_tour_match_returns_offer_id() -> None:
    db = _StubDb([[{"id": 6, "vehicle_category_id": None}], 6])
    got = await offer_service.resolve_offer_for_booking(
        db,
        coupon_id=7,
        service_type="TOUR",
        vehicle_category_id=None,
        hotel_id=None,
        room_category_id=None,
        tour_package_id=3,
    )
    assert got == 6


# ── assert_publishable ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_without_terms_rejected() -> None:
    db = _StubDb([])
    with pytest.raises(ValidationException):
        await offer_service.assert_publishable(db, _offer(terms_and_conditions="   "))


@pytest.mark.asyncio
async def test_publish_hotel_without_active_link_rejected() -> None:
    db = _StubDb([None])
    with pytest.raises(ValidationException):
        await offer_service.assert_publishable(db, _offer(service_type="HOTEL"))


@pytest.mark.asyncio
async def test_publish_hotel_with_active_link_passes() -> None:
    db = _StubDb([[11]])
    await offer_service.assert_publishable(db, _offer(service_type="HOTEL"))


@pytest.mark.asyncio
async def test_publish_tour_without_active_link_rejected() -> None:
    db = _StubDb([None])
    with pytest.raises(ValidationException):
        await offer_service.assert_publishable(db, _offer(service_type="TOUR"))


@pytest.mark.asyncio
async def test_publish_cab_needs_no_link_rows() -> None:
    db = _StubDb([])
    await offer_service.assert_publishable(db, _offer(service_type="CAB"))
