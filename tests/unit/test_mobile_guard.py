# ============================================================
# WAYTERO — MOBILE-REQUIRED BOOKING GUARD TESTS
# File: tests/unit/test_mobile_guard.py
#
# Pins the platform rule: a self-service customer cannot create a cab, hotel
# or tour booking until a usable mobile is on file. Verified AND unverified
# both count — the SMS provider is an admin toggle, so an unverified number is
# the normal state when SMS is off.
#
# Uses an in-memory stub DB (the convention in tests/booking and tests/unit)
# so no live PostgreSQL connection is needed.
# ============================================================

from __future__ import annotations

import pytest

from app.core.exceptions import BusinessException
from app.shared.utilities.mobile import has_real_mobile, require_real_mobile


class _StubDB:
    """Stands in for AsyncSession: db.scalar(...) -> the stored value."""

    def __init__(self, mobile) -> None:
        self._mobile = mobile
        self.calls: list[str] = []

    async def scalar(self, stmt, params=None):  # noqa: ANN001, ARG002
        self.calls.append(str(getattr(stmt, "text", stmt)))
        return self._mobile


# ── has_real_mobile ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "9876543210",
        "6000000000",
        "9999999999",
    ],
)
def test_accepts_real_indian_mobile(value: str) -> None:
    assert has_real_mobile(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "fb_b51116e43a",  # the Firebase placeholder a Google-only account gets
        "1234567890",  # 10 digits but not a valid Indian prefix
        "0987654321",  # leading zero
        "987654321",  # too short
        "98765432101",  # too long
        "+919876543210",  # not normalised
        "abcdefghij",
        "9876 54321",  # not a contiguous run of digits
    ],
)
def test_rejects_everything_that_is_not_a_mobile(value) -> None:  # noqa: ANN001
    assert has_real_mobile(value) is False


# ── require_real_mobile ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_placeholder_mobile_blocks_the_booking() -> None:
    db = _StubDB("fb_b51116e43a")
    with pytest.raises(BusinessException) as exc:
        await require_real_mobile(db, "11111111-1111-1111-1111-111111111111")
    assert exc.value.code == "MOBILE_REQUIRED"
    assert exc.value.status_code == 422
    assert "mobile" in exc.value.message.lower()


@pytest.mark.asyncio
async def test_empty_mobile_blocks_the_booking() -> None:
    with pytest.raises(BusinessException) as exc:
        await require_real_mobile(_StubDB(""), "11111111-1111-1111-1111-111111111111")
    assert exc.value.code == "MOBILE_REQUIRED"


@pytest.mark.asyncio
async def test_unverified_real_mobile_is_allowed_through() -> None:
    """The guard reads the number only — is_mobile_verified is NOT consulted."""
    db = _StubDB("9876543210")
    await require_real_mobile(db, "11111111-1111-1111-1111-111111111111")
    assert len(db.calls) == 1


@pytest.mark.asyncio
async def test_missing_user_row_does_not_claim_mobile_required() -> None:
    """A deleted account has no users row: mobile_number is NOT NULL, so None
    can only mean the row is gone. The caller's own customer lookup reports
    that, rather than this guard mislabelling it as a missing phone number."""
    await require_real_mobile(_StubDB(None), "11111111-1111-1111-1111-111111111111")
