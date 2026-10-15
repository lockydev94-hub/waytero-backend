# ============================================================
# Coupon cost-bearer math + guard regressions (migration 0075/0076).
# Pure helpers only — no DB session needed.
# ============================================================
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationException
from app.modules.admin.coupon_api import _is_proven_refusal, _split_shares
from app.modules.booking.services.coupon_availability import available_cab_counts

UTC = timezone.utc


# ── _split_shares ─────────────────────────────────────────────────


def test_split_platform_coupon_borne_entirely_by_platform() -> None:
    platform, partner = _split_shares("PLATFORM", Decimal("100.00"), None)
    assert (platform, partner) == (Decimal("100.00"), Decimal("0.00"))


def test_split_partner_only_coupon_borne_entirely_by_partner() -> None:
    platform, partner = _split_shares("PARTNER_ONLY", Decimal("100.00"), None)
    assert (platform, partner) == (Decimal("0.00"), Decimal("100.00"))


def test_split_shares_are_exact_and_sum_to_discount() -> None:
    # 30% platform of ₹100.01 → ₹30.00 (HALF_UP on 30.003); partner absorbs
    # the remainder so platform_share + partner_share == discount exactly.
    platform, partner = _split_shares("PLATFORM_PARTNER", Decimal("100.01"), 30)
    assert platform == Decimal("30.00")
    assert platform + partner == Decimal("100.01")


def test_split_requires_platform_share_percent() -> None:
    with pytest.raises(ValidationException):
        _split_shares("PLATFORM_PARTNER", Decimal("100.00"), None)


# ── available_cab_counts input guards ─────────────────────────────


@pytest.mark.asyncio
async def test_naive_pickup_datetime_is_rejected() -> None:
    """Partner-funded availability compares TIMESTAMPTZ windows — a naive
    pickup would silently shift the window by the UTC offset."""
    with pytest.raises(ValidationException):
        await available_cab_counts(
            None,  # never reached — the guard fires first
            [2],
            city_id=5,
            vehicle_category_id=3,
            pickup_datetime=datetime(2026, 10, 16, 9, 0),
        )


@pytest.mark.asyncio
async def test_return_before_pickup_is_rejected() -> None:
    aware = datetime.now(UTC)
    with pytest.raises(ValidationException):
        await available_cab_counts(
            None,
            [2],
            city_id=5,
            vehicle_category_id=3,
            pickup_datetime=aware,
            return_datetime=aware - timedelta(hours=1),
        )


# ── _is_proven_refusal (admin removal evidence) ───────────────────


def _row(**over):
    base = {
        "rejected_at": datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        "rejection_reason_code": "VEHICLE_UNAVAILABLE",
        "available_cabs_at_decline": 2,
    }
    base.update(over)
    return base


def test_refusal_with_cabs_is_proven() -> None:
    assert _is_proven_refusal(_row()) is True


def test_timeout_is_never_proven_refusal() -> None:
    assert _is_proven_refusal(_row(rejection_reason_code="TIMEOUT")) is False


def test_admin_reassign_is_never_proven_refusal() -> None:
    assert _is_proven_refusal(_row(rejection_reason_code="ADMIN_REASSIGN")) is False


def test_decline_without_available_cabs_is_not_proven() -> None:
    assert _is_proven_refusal(_row(available_cabs_at_decline=0)) is False
    assert _is_proven_refusal(_row(available_cabs_at_decline=None)) is False


def test_open_offer_is_not_a_refusal() -> None:
    assert _is_proven_refusal(_row(rejected_at=None)) is False
