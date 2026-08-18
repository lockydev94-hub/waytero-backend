# ============================================================
# WAY TERO — HOTEL CHECK-IN DATE MISMATCH TESTS
# File: tests/unit/test_hotel_checkin_mismatch.py
# Doc Ref: BRD Part 4 §57-92 — Hotel Booking Lifecycle
#
# The check-in date guard closes the gap between "booked for 10 Aug" and
# "guest actually arrived 12 Aug". Both the partner and admin check-in
# endpoints share the same decision helper (`check_in_date_mismatch`), so the
# unit coverage here pins:
#
#   1. The pure decision logic — when a stamp trips the guard, the descriptor
#      payload, and the >1-day rule.
#   2. The endpoint wiring — a request without `confirm_date_mismatch` gets a
#      409 with `code=CHECKIN_DATE_MISMATCH` BEFORE the row is mutated; with
#      the flag set it succeeds and writes the `checkin_date_mismatch` note
#      onto the HotelCheckin.remarks timeline entry.
#
# The handlers are invoked directly (not through HTTP) with a routing fake
# session — the same style the rest of the unit suite uses.
# ============================================================

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.timezone import invalidate_cache
from app.modules.admin.booking_api import HotelCheckInRequest, hotel_check_in
from app.modules.hotel.services.hotel_helpers import check_in_date_mismatch
from app.modules.partner.hotel_booking_api import (
    PartnerCheckInRequest,
    partner_hotel_check_in,
)


@pytest.fixture(autouse=True)
def _reset_tz_cache():
    invalidate_cache()
    yield
    invalidate_cache()


# ── Routing fake session ──────────────────────────────────────────────────
# The check-in handlers issue a handful of SELECTs before/after the guard.
# The fake routes purely on the compiled SQL's FROM clause and hands back
# whatever row the caller asked for; anything else returns None, which makes
# the best-effort WS/notification fan-out degrade silently (it is wrapped in
# try/except in the handlers).
class _FakeScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDb:
    def __init__(self, hr, *, partner=None, hotel=None, mb=None):
        self.hr = hr
        self.partner = partner
        self.hotel = hotel
        self.mb = mb
        self.added: list = []

    async def execute(self, stmt, _params=None):
        sql = str(stmt).lower()
        if "from partners" in sql:
            return _FakeScalarResult(self.partner)
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


def _reservation(status="CONFIRMED", *, booked=date(2026, 8, 10)):
    return SimpleNamespace(
        id=7,
        master_booking_id=10,
        hotel_id=5,
        rooms_count=1,
        room_category_id=None,
        reservation_number="HR-TEST-001",
        reservation_status=status,
        check_in_date=booked,
        check_in_id_proof=None,
        check_in_id_number=None,
        actual_check_in_at=None,
    )


def _partner_db(hr):
    partner = SimpleNamespace(id=99)
    hotel = SimpleNamespace(id=5, partner_id=99)
    return _FakeDb(hr, partner=partner, hotel=hotel)


# ── Pure decision helper ──────────────────────────────────────────────────


def test_mismatch_none_when_same_date():
    hr = _reservation(booked=date(2026, 8, 10))
    assert check_in_date_mismatch(hr, datetime(2026, 8, 10, 23, 30)) is None


def test_mismatch_none_when_hr_or_stamp_missing():
    assert check_in_date_mismatch(None, datetime(2026, 8, 12)) is None
    hr = _reservation(booked=date(2026, 8, 10))
    assert check_in_date_mismatch(hr, None) is None
    no_booked = _reservation()
    no_booked.check_in_date = None
    assert check_in_date_mismatch(no_booked, datetime(2026, 8, 12)) is None


def test_mismatch_trips_at_one_day_late():
    # Plan assumption: the warning fires at a >= 1 day delta (a guest arriving
    # the next calendar day is still worth a confirmation prompt).
    hr = _reservation(booked=date(2026, 8, 10))
    desc = check_in_date_mismatch(hr, datetime(2026, 8, 11, 14, 0))
    assert desc is not None
    assert desc["code"] == "CHECKIN_DATE_MISMATCH"
    assert desc["delta_days"] == 1
    assert desc["booking_date"] == "2026-08-10"
    assert desc["actual_date"] == "2026-08-11"


def test_mismatch_two_days_late():
    hr = _reservation(booked=date(2026, 8, 10))
    desc = check_in_date_mismatch(hr, datetime(2026, 8, 12, 14, 0))
    assert desc is not None
    assert desc["delta_days"] == 2
    assert "after" in desc["message"]


def test_mismatch_early_arrival():
    hr = _reservation(booked=date(2026, 8, 10))
    desc = check_in_date_mismatch(hr, datetime(2026, 8, 8, 14, 0))
    assert desc is not None
    assert desc["delta_days"] == -2
    assert "before" in desc["message"]


def test_mismatch_aware_stamp_converted_to_platform_zone():
    # 2026-08-12 09:00 UTC == 14:30 Asia/Kolkata on the same calendar day —
    # no date drift, so no mismatch, even though the raw stamp "reads" as the
    # 12th in both zones.
    hr = _reservation(booked=date(2026, 8, 12))
    desc = check_in_date_mismatch(hr, datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc))
    assert desc is None


# ── Partner endpoint wiring ────────────────────────────────────────────────


async def test_partner_check_in_409_on_date_mismatch():
    hr = _reservation(booked=date(2026, 8, 10))
    db = _partner_db(hr)
    payload = PartnerCheckInRequest(
        id_proof="AADHAAR",
        id_number="XYZ123",
        actual_check_in_at=datetime(2026, 8, 12, 14, 0),
    )
    with pytest.raises(HTTPException) as exc_info:
        await partner_hotel_check_in(7, payload, {"sub": str(uuid4())}, db)

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.detail["code"] == "CHECKIN_DATE_MISMATCH"
    assert exc.detail["delta_days"] == 2
    # The guard fires BEFORE the row is mutated.
    assert hr.reservation_status == "CONFIRMED"
    assert hr.actual_check_in_at is None
    assert db.added == []


async def test_partner_check_in_confirms_and_records_remarks():
    hr = _reservation(booked=date(2026, 8, 10))
    db = _partner_db(hr)
    payload = PartnerCheckInRequest(
        id_proof="AADHAAR",
        id_number="XYZ123",
        actual_check_in_at=datetime(2026, 8, 12, 14, 0),
        confirm_date_mismatch=True,
    )
    result = await partner_hotel_check_in(7, payload, {"sub": str(uuid4())}, db)

    assert result["success"] is True
    assert hr.reservation_status == "CHECKED_IN"
    # Stored stamp is normalised to UTC.
    assert hr.actual_check_in_at.tzinfo == timezone.utc

    checkin = next(o for o in db.added if type(o).__name__ == "HotelCheckin")
    assert checkin.remarks is not None
    assert "checkin_date_mismatch" in checkin.remarks
    assert "booked=2026-08-10" in checkin.remarks
    assert "actual=2026-08-12" in checkin.remarks
    assert "delta_days=2" in checkin.remarks


async def test_partner_check_in_same_date_no_remarks_note():
    hr = _reservation(booked=date(2026, 8, 10))
    db = _partner_db(hr)
    payload = PartnerCheckInRequest(
        id_proof="PASSPORT",
        id_number="PP0001",
        actual_check_in_at=datetime(2026, 8, 10, 14, 0),
    )
    await partner_hotel_check_in(7, payload, {"sub": str(uuid4())}, db)

    checkin = next(o for o in db.added if type(o).__name__ == "HotelCheckin")
    assert checkin.remarks is None


# ── Admin endpoint wiring (mirrors partner) ───────────────────────────────


async def test_admin_check_in_409_on_date_mismatch():
    hr = _reservation(booked=date(2026, 8, 10))
    mb = SimpleNamespace(
        id=10,
        total_amount=0,
        total_paid_amount=0,
        payment_status="PENDING",
        booking_status="CONFIRMED",
        services=[],
        cab_bookings=[],
        timelines=[],
        notes=[],
    )
    db = _FakeDb(hr, mb=mb)
    payload = HotelCheckInRequest(
        id_proof="AADHAAR",
        id_number="XYZ123",
        actual_check_in_at=datetime(2026, 8, 12, 14, 0),
    )
    with pytest.raises(HTTPException) as exc_info:
        await hotel_check_in(10, 5, payload, db)

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.detail["code"] == "CHECKIN_DATE_MISMATCH"
    assert exc.detail["delta_days"] == 2
    assert hr.reservation_status == "CONFIRMED"
    assert db.added == []
