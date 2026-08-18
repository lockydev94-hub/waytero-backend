# ============================================================
# WAY TERO — HOTEL COMPLETE / SETTLE IDEMPOTENCY TESTS
# File: tests/unit/test_hotel_settle_idempotent.py
# Doc Ref: BRD Part 4 §75-92 — Check-out, completion & settlement custody
#
# `hotel_complete` and `hotel_settle` are guarded by `reservation_status`:
# once the status has moved past the pre-condition the action is rejected
# with a 400 that names the CURRENT status, so the admin UI can read it and
# hide the button. These tests pin:
#
#   1. A second `complete` (status already COMPLETED) → 400 naming COMPLETED.
#   2. A second `settle` (status already SETTLED) → 400 naming SETTLED.
#   3. `_settle_hotel_reservation` guard ordering — status, then invoice,
#      then full collection — so a partially-paid stay can never settle.
#
# The endpoint handlers are invoked directly with a routing fake session,
# exactly like the other hotel unit tests.
# ============================================================

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.admin.booking_api import hotel_complete, hotel_settle
from app.modules.admin.settlement_api import _settle_hotel_reservation


class _FakeScalarResult:
    def __init__(self, row, *, list_value=None, scalar_value=None):
        self._row = row
        self._list = list_value
        self._scalar = scalar_value

    def scalar_one_or_none(self):
        return self._row

    def scalar(self):
        if self._scalar is not None:
            return self._scalar
        return self._row if isinstance(self._row, (int, float)) else 0

    def scalars(self):
        return self

    def all(self):
        if self._list is not None:
            return self._list
        return [] if self._row is None else [self._row]


class _FakeDb:
    def __init__(self, hr, *, hotel=None, mb=None):
        self.hr = hr
        self.hotel = hotel
        self.mb = mb
        self.added: list = []

    async def execute(self, stmt, _params=None):
        sql = str(stmt).lower()
        if "sum(" in sql:
            return _FakeScalarResult(None, scalar_value=0)
        if "from booking_services" in sql:
            return _FakeScalarResult(None, list_value=["HOTEL"])
        if "from hotels" in sql:
            return _FakeScalarResult(self.hotel)
        if "from hotel_reservations" in sql:
            return _FakeScalarResult(self.hr)
        if "from master_bookings" in sql:
            return _FakeScalarResult(self.mb)
        return _FakeScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None


def _reservation(status, *, invoice_number=None, payment_collected_status="PAID"):
    return SimpleNamespace(
        id=7,
        master_booking_id=10,
        hotel_id=5,
        reservation_number="HR-TEST-001",
        reservation_status=status,
        invoice_number=invoice_number,
        payment_collected_status=payment_collected_status,
        total_amount=0,
        gst_amount=0,
        coupon_discount=0,
    )


def _master_booking():
    return SimpleNamespace(
        id=10,
        total_amount=0,
        total_paid_amount=0,
        payment_status="PENDING",
        booking_status="COMPLETED",
        services=[],
        cab_bookings=[],
        timelines=[],
        notes=[],
    )


def _hotel():
    return SimpleNamespace(id=5, partner_id=99)


def _assert_status_message(exc: HTTPException, fragment: str) -> None:
    assert isinstance(exc.detail, str)
    assert fragment in exc.detail


# ── hotel_complete idempotency ────────────────────────────────────────────


async def test_second_complete_returns_400_naming_completed():
    hr = _reservation("COMPLETED")
    db = _FakeDb(hr, mb=_master_booking())

    with pytest.raises(HTTPException) as exc_info:
        await hotel_complete(10, 5, db)

    exc = exc_info.value
    assert exc.status_code == 400
    _assert_status_message(exc, "Cannot complete: status is 'COMPLETED'")


async def test_complete_from_checked_out_succeeds():
    hr = _reservation("CHECKED_OUT")
    db = _FakeDb(hr, mb=_master_booking())

    result = await hotel_complete(10, 5, db)

    assert result["success"] is True
    assert hr.reservation_status == "COMPLETED"


# ── hotel_settle idempotency ──────────────────────────────────────────────


async def test_second_settle_returns_400_naming_settled():
    hr = _reservation("SETTLED", invoice_number="INV-1")
    db = _FakeDb(hr, hotel=_hotel(), mb=_master_booking())

    with pytest.raises(HTTPException) as exc_info:
        await hotel_settle(10, 5, db)

    exc = exc_info.value
    assert exc.status_code == 400
    _assert_status_message(exc, "Cannot settle: hotel status is 'SETTLED'")


# ── _settle_hotel_reservation guard ordering ──────────────────────────────


async def test_settle_guard_requires_completed_status():
    hr = _reservation("CHECKED_OUT", invoice_number="INV-1")
    with pytest.raises(HTTPException) as exc_info:
        await _settle_hotel_reservation(_FakeDb(hr), _master_booking(), hr, _hotel())

    exc = exc_info.value
    assert exc.status_code == 400
    _assert_status_message(exc, "Only COMPLETED reservations can be settled")


async def test_settle_guard_requires_invoice():
    hr = _reservation("COMPLETED", invoice_number=None)
    with pytest.raises(HTTPException) as exc_info:
        await _settle_hotel_reservation(_FakeDb(hr), _master_booking(), hr, _hotel())

    exc = exc_info.value
    assert exc.status_code == 400
    _assert_status_message(exc, "Generate the invoice before settling")


async def test_settle_guard_requires_full_collection():
    hr = _reservation(
        "COMPLETED", invoice_number="INV-1", payment_collected_status="PENDING"
    )
    with pytest.raises(HTTPException) as exc_info:
        await _settle_hotel_reservation(_FakeDb(hr), _master_booking(), hr, _hotel())

    exc = exc_info.value
    assert exc.status_code == 400
    _assert_status_message(exc, "Collect the full balance before settling this stay")


async def test_settle_guard_requires_partner_assigned():
    hr = _reservation(
        "COMPLETED", invoice_number="INV-1", payment_collected_status="PAID"
    )
    no_partner = SimpleNamespace(id=5, partner_id=None)
    with pytest.raises(HTTPException) as exc_info:
        await _settle_hotel_reservation(_FakeDb(hr), _master_booking(), hr, no_partner)

    exc = exc_info.value
    assert exc.status_code == 400
    _assert_status_message(exc, "no partner assigned")
