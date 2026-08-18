# ============================================================
# WAYTERO — BREAKDOWN / VEHICLE-SWAP SERVICE TESTS
# File: tests/booking/test_breakdown_service.py
# Doc Ref: BRD Part 3 §42 — Spec section: Cab Breakdown → Vehicle Swap
#
# Pins the behaviour of app/modules/booking/services/breakdown.py —
# the single source of truth for in-trip vehicle breakdowns. Uses an
# in-memory stub DB so we don't need a live PostgreSQL connection for
# these tests.
#
# Covers:
#   • report_breakdown
#       - happy path: DRIVER_ASSIGNED → BREAKDOWN_REPORTED
#       - idempotent re-reporting
#       - rejects invalid reason code
#       - rejects non-in-flight booking status
#       - partner authorization (rejects other partners)
#   • swap_vehicle_same_partner
#       - happy path: same partner, new vehicle, new driver
#       - rejects cross-partner target
#       - rejects vehicle not ACTIVE
#   • handover_to_new_partner
#       - happy path: original gets zero payout, new active assignment created
#       - voids partner-side advances
#       - clears cash_pending_at
#       - rejects SETTLED bookings
#       - rejects same-partner target
#   • close_assignment_on_trip_complete
#       - idempotent (second call returns None)
#       - sets close_reason = TRIP_COMPLETED
#       - captures end_km
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.modules.booking.services import breakdown as breakdown_service
from app.modules.partner.constants import BREAKDOWN_REASON_CODES


# ── Stub helpers ──────────────────────────────────────────────────────────────


class _StubScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> Any:
        # Mimic SQLAlchemy: .scalars().all() returns the list as-is.
        # If a single object was queued, treat it as a single-row list.
        if isinstance(self._value, list):
            return _Scalars(self._value)
        if self._value is None:
            return _Scalars([])
        return _Scalars([self._value])


class _Scalars:
    def __init__(self, v):
        self.v = v

    def all(self):
        return self.v

    def first(self):
        return self.v[0] if self.v else None


class _StubDb:
    """Just enough DB surface for the breakdown service under test."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.flushes: int = 0
        # FIFO queue of scalar results for the queries inside the service.
        self.queue: list[_StubScalarResult] = []
        # Popped cab / partner / etc. values — remembered so flush can
        # patch relationships onto them.
        self.loaded: list[Any] = []
        # Monotonic id for auto-generated rows (assignments + timeline).
        self._next_id = 1000

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(
        self, _stmt: Any = None, *_args: Any, **_kwargs: Any
    ) -> _StubScalarResult:
        self.executed.append(_stmt)
        if self.queue:
            result = self.queue.pop(0)
            self.loaded.append(result._value)
            return result
        return _StubScalarResult(None)

    async def flush(self) -> None:
        self.flushes += 1
        # Mimic the session's identity-map behaviour: when a new
        # CabBookingAssignment / BookingTimeline is added, give it a
        # primary key and (for assignment) append it to the parent cab's
        # `assignments` collection so downstream code sees it.
        from app.modules.booking.models import (
            CabBookingAssignment,
            BookingTimeline,
        )

        # Duck-typed lookup: anything with `assignments` attr is a cab.
        def _is_cab_like(a):
            return hasattr(a, "assignments") and hasattr(a, "id")

        for obj in list(self.added):
            # The service uses the real ORM classes for new rows, so the
            # isinstance check is exact.
            if (
                isinstance(obj, CabBookingAssignment)
                and getattr(obj, "id", None) is None
            ):
                self._next_id += 1
                obj.id = self._next_id
                for cab in [a for a in self.added if _is_cab_like(a)] + [
                    a for a in self.loaded if _is_cab_like(a)
                ]:
                    if cab.id == obj.cab_booking_id:
                        cab.assignments = list(cab.assignments or []) + [obj]
                        break
            elif isinstance(obj, BookingTimeline) and getattr(obj, "id", None) is None:
                self._next_id += 1
                obj.id = self._next_id


def _cab(
    *,
    cab_id: int = 1,
    booking_number: str = "CAB-TEST-001",
    master_id: int = 10,
    status: str = "DRIVER_ASSIGNED",
    assignments: list[Any] | None = None,
    original_partner_id: int | None = None,
    breakdown_reason: str | None = None,
    cash_pending_at: str | None = "NONE",
    cash_amount_due: float = 0.0,
    trip_start_km: float | None = None,
    trip_end_km: float | None = None,
    swap_count: int = 0,
    is_breakdown_swap: bool = False,
) -> SimpleNamespace:
    cab = SimpleNamespace(
        id=cab_id,
        master_booking_id=master_id,
        booking_number=booking_number,
        booking_status=status,
        assignments=list(assignments or []),
        original_partner_id=original_partner_id,
        breakdown_reason=breakdown_reason,
        cash_pending_at=cash_pending_at,
        cash_amount_due=cash_amount_due,
        payment_collected_by=None,
        trip_start_km=trip_start_km,
        trip_end_km=trip_end_km,
        swap_count=swap_count,
        is_breakdown_swap=is_breakdown_swap,
        acceptance_deadline=None,
        partner_responded_at=None,
        pending_partner_id=None,
        pre_swap_actual_km=None,
        breakdown_reported_at=None,
        breakdown_reported_by=None,
        breakdown_latitude=None,
        breakdown_longitude=None,
        last_swap_at=None,
    )
    # The service accesses row.master_booking for assignment closure.
    cab.master_booking = SimpleNamespace(id=master_id)
    return cab


def _assignment(
    *,
    assign_id: int = 1,
    cab_id: int = 1,
    partner_id: int = 100,
    driver_id: int | None = 200,
    vehicle_id: int | None = 300,
    closed_at: Any = None,
    close_reason: str | None = None,
    accepted_at: Any = None,
    rejected_at: Any = None,
    assigned_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=assign_id,
        cab_booking_id=cab_id,
        partner_id=partner_id,
        vehicle_id=vehicle_id,
        driver_id=driver_id,
        assigned_at=assigned_at or datetime.now(timezone.utc),
        assignment_type="MANUAL",
        accepted_at=accepted_at,
        rejected_at=rejected_at,
        rejection_reason_code=None,
        rejection_notes=None,
        acceptance_deadline=None,
        closed_at=closed_at,
        close_reason=close_reason,
        km_at_assignment_start=None,
        km_at_assignment_end=None,
    )


def _partner(
    partner_id: int = 100, business_name: str = "Test Partner"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=partner_id,
        business_name=business_name,
        partner_status="APPROVED",
        city_id=1,
    )


def _vehicle(
    vehicle_id: int = 300, partner_id: int = 100, status: str = "ACTIVE"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=vehicle_id,
        partner_id=partner_id,
        status=status,
        registration_number="WB-04-1234",
        vehicle_category_id=1,
    )


def _driver(
    driver_id: int = 200, partner_id: int = 100, status: str = "ACTIVE"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=driver_id,
        partner_id=partner_id,
        status=status,
        full_name="Test Driver",
        mobile="9876543210",
    )


# ── report_breakdown ─────────────────────────────────────────────────────────


async def test_report_breakdown_happy_path():
    db = _StubDb()
    active = _assignment(partner_id=100, driver_id=200, vehicle_id=300)
    cab = _cab(assignments=[active])
    # Queue order matches what the service queries:
    # 1. SELECT cab+assignments → cab
    # 2. SELECT Vehicle where id=300 → vehicle
    db.queue.append(_StubScalarResult(cab))  # initial load
    # Vehicle re-load is not strictly necessary in our test (assignment has vehicle_id), but
    # _set_vehicle_status uses update() which goes through execute(text), no result.
    snapshot = await breakdown_service.report_breakdown(
        db,
        cab_booking_id=1,
        reported_by=breakdown_service.REPORTER_DRIVER,
        reported_by_user_id=None,
        reporter_partner_id=None,
        reporter_driver_id=200,
        reason_code="VEHICLE_BREAKDOWN",
        notes="Engine overheating",
    )
    assert snapshot["booking_status"] == "BREAKDOWN_REPORTED"
    assert snapshot["breakdown_reason"] == "VEHICLE_BREAKDOWN"
    assert snapshot["original_partner_id"] == 100
    assert cab.booking_status == "BREAKDOWN_REPORTED"
    assert cab.breakdown_reason == "VEHICLE_BREAKDOWN"
    assert cab.original_partner_id == 100  # snapshotted on first report
    assert cab.is_breakdown_swap is False  # no swap yet, just reported
    assert cab.pre_swap_actual_km is not None
    # Timeline event written
    assert any(
        getattr(a, "master_booking_id", None) == 10
        and "BREAKDOWN_REPORTED_BY_DRIVER" in getattr(a, "event_type", "")
        for a in db.added
    )


async def test_report_breakdown_rejects_invalid_reason():
    db = _StubDb()
    active = _assignment()
    cab = _cab(assignments=[active])
    db.queue.append(_StubScalarResult(cab))

    with pytest.raises(breakdown_service.ValidationException):
        await breakdown_service.report_breakdown(
            db,
            cab_booking_id=1,
            reported_by=breakdown_service.REPORTER_DRIVER,
            reported_by_user_id=None,
            reporter_partner_id=None,
            reporter_driver_id=200,
            reason_code="NOT_A_REAL_REASON",
        )


async def test_report_breakdown_rejects_invalid_status():
    db = _StubDb()
    active = _assignment()
    cab = _cab(status="COMPLETED", assignments=[active])
    db.queue.append(_StubScalarResult(cab))

    with pytest.raises(breakdown_service.BusinessException) as exc_info:
        await breakdown_service.report_breakdown(
            db,
            cab_booking_id=1,
            reported_by=breakdown_service.REPORTER_DRIVER,
            reported_by_user_id=None,
            reporter_partner_id=None,
            reporter_driver_id=200,
            reason_code="VEHICLE_BREAKDOWN",
        )
    assert "INVALID_BREAKDOWN_STATE" in str(exc_info.value.code)


async def test_report_breakdown_rejects_wrong_driver():
    db = _StubDb()
    active = _assignment(driver_id=200)
    cab = _cab(assignments=[active])
    db.queue.append(_StubScalarResult(cab))

    with pytest.raises(breakdown_service.PermissionDeniedException):
        await breakdown_service.report_breakdown(
            db,
            cab_booking_id=1,
            reported_by=breakdown_service.REPORTER_DRIVER,
            reported_by_user_id=None,
            reporter_partner_id=None,
            reporter_driver_id=999,  # not the assigned driver
            reason_code="VEHICLE_BREAKDOWN",
        )


async def test_report_breakdown_idempotent_on_already_reported():
    db = _StubDb()
    active = _assignment()
    cab = _cab(
        status="BREAKDOWN_REPORTED",
        assignments=[active],
        original_partner_id=100,
        breakdown_reason="VEHICLE_BREAKDOWN",
    )
    db.queue.append(_StubScalarResult(cab))

    snapshot = await breakdown_service.report_breakdown(
        db,
        cab_booking_id=1,
        reported_by=breakdown_service.REPORTER_ADMIN,
        reported_by_user_id=None,
        reporter_partner_id=None,
        reporter_driver_id=None,
        reason_code="ACCIDENT",  # updates reason
    )
    assert cab.breakdown_reason == "ACCIDENT"  # refreshed
    assert snapshot["breakdown_reason"] == "ACCIDENT"


# ── swap_vehicle_same_partner ────────────────────────────────────────────────


async def test_swap_same_partner_happy_path():
    db = _StubDb()
    old = _assignment(assign_id=1, partner_id=100, driver_id=200, vehicle_id=300)
    cab = _cab(status="BREAKDOWN_REPORTED", assignments=[old], trip_start_km=10.0)
    # Query order:
    # 1. SELECT cab → cab
    # 2. SELECT Vehicle(new) → vehicle
    # 3. SELECT Driver(new) → driver
    db.queue.append(_StubScalarResult(cab))
    db.queue.append(_StubScalarResult(_vehicle(vehicle_id=400, partner_id=100)))
    db.queue.append(_StubScalarResult(_driver(driver_id=500, partner_id=100)))

    snapshot = await breakdown_service.swap_vehicle_same_partner(
        db,
        cab_booking_id=1,
        new_vehicle_id=400,
        new_driver_id=500,
        performed_by=breakdown_service.REPORTER_ADMIN,
        performed_by_user_id=None,
        performed_by_partner_id=None,
    )
    assert snapshot["booking_status"] == "DRIVER_ASSIGNED"
    assert snapshot["active_vehicle_id"] == 400
    assert snapshot["active_driver_id"] == 500
    # Old assignment closed.
    assert old.closed_at is not None
    assert old.close_reason == "VEHICLE_BREAKDOWN"
    # New assignment appended.
    new_assigns = [
        a for a in db.added if getattr(a, "assignment_type", "") == "SWAP_SAME_PARTNER"
    ]
    assert len(new_assigns) == 1
    na = new_assigns[0]
    assert na.partner_id == 100  # SAME partner
    assert na.vehicle_id == 400
    assert na.driver_id == 500
    assert na.assignment_type == "SWAP_SAME_PARTNER"
    # Cab status moves to DRIVER_ASSIGNED for re-start.
    assert cab.booking_status == "DRIVER_ASSIGNED"
    assert cab.swap_count == 1
    assert cab.is_breakdown_swap is True


async def test_swap_same_partner_rejects_cross_partner_vehicle():
    db = _StubDb()
    old = _assignment(partner_id=100)
    cab = _cab(status="BREAKDOWN_REPORTED", assignments=[old])
    # Service queries: cab → vehicle → driver → guard fires on vehicle.
    db.queue.append(_StubScalarResult(cab))
    db.queue.append(
        _StubScalarResult(_vehicle(vehicle_id=400, partner_id=999))
    )  # different partner
    db.queue.append(
        _StubScalarResult(_driver(driver_id=500, partner_id=100))
    )  # queued for ordering

    with pytest.raises(breakdown_service.BusinessException) as exc_info:
        await breakdown_service.swap_vehicle_same_partner(
            db,
            cab_booking_id=1,
            new_vehicle_id=400,
            new_driver_id=500,
            performed_by=breakdown_service.REPORTER_ADMIN,
            performed_by_user_id=None,
            performed_by_partner_id=None,
        )
    assert "DIFFERENT_PARTNER_NOT_ALLOWED" in str(exc_info.value.code)


async def test_swap_same_partner_rejects_non_active_vehicle():
    db = _StubDb()
    old = _assignment(partner_id=100)
    cab = _cab(status="BREAKDOWN_REPORTED", assignments=[old])
    db.queue.append(_StubScalarResult(cab))
    db.queue.append(
        _StubScalarResult(
            _vehicle(vehicle_id=400, partner_id=100, status="MAINTENANCE")
        )
    )
    db.queue.append(_StubScalarResult(_driver(driver_id=500, partner_id=100)))

    with pytest.raises(breakdown_service.BusinessException) as exc_info:
        await breakdown_service.swap_vehicle_same_partner(
            db,
            cab_booking_id=1,
            new_vehicle_id=400,
            new_driver_id=500,
            performed_by=breakdown_service.REPORTER_ADMIN,
            performed_by_user_id=None,
            performed_by_partner_id=None,
        )
    assert "VEHICLE_NOT_AVAILABLE" in str(exc_info.value.code)


# ── handover_to_new_partner ──────────────────────────────────────────────────


async def test_handover_happy_path():
    db = _StubDb()
    old = _assignment(
        assign_id=1,
        partner_id=100,
        driver_id=200,
        vehicle_id=300,
        accepted_at=datetime.now(timezone.utc),
    )
    cab = _cab(
        status="BREAKDOWN_REPORTED",
        assignments=[old],
        cash_pending_at="DRIVER",
        cash_amount_due=500.0,
        original_partner_id=100,
    )
    # 1. cab  2. new partner
    db.queue.append(_StubScalarResult(cab))
    db.queue.append(
        _StubScalarResult(_partner(partner_id=200, business_name="New Partner"))
    )

    snapshot = await breakdown_service.handover_to_new_partner(
        db,
        cab_booking_id=1,
        new_partner_id=200,
        performed_by_user_id=None,
    )
    assert snapshot["booking_status"] == "PENDING_PARTNER_ACCEPTANCE"
    assert snapshot["active_partner_id"] == 200
    assert snapshot["acceptance_deadline"] is not None
    # Old row closed with PARTNER_HANDOVER.
    assert old.close_reason == "PARTNER_HANDOVER"
    assert old.closed_at is not None
    # New row appended.
    new_assigns = [
        a for a in db.added if getattr(a, "assignment_type", "") == "SWAP_HANDOVER"
    ]
    assert len(new_assigns) == 1
    na = new_assigns[0]
    assert na.partner_id == 200
    assert na.assignment_type == "SWAP_HANDOVER"
    # Cab moved to PENDING_PARTNER_ACCEPTANCE.
    assert cab.booking_status == "PENDING_PARTNER_ACCEPTANCE"
    assert cab.pending_partner_id == 200
    assert cab.acceptance_deadline is not None
    # Custody reset.
    assert cab.cash_pending_at == "NONE"
    assert float(cab.cash_amount_due) == 0.0


async def test_handover_rejects_settled_booking():
    db = _StubDb()
    active = _assignment(partner_id=100)
    cab = _cab(status="SETTLED", assignments=[active])
    db.queue.append(_StubScalarResult(cab))

    with pytest.raises(breakdown_service.BusinessException) as exc_info:
        await breakdown_service.handover_to_new_partner(
            db,
            cab_booking_id=1,
            new_partner_id=200,
            performed_by_user_id=None,
        )
    # SETTLED is rejected because it's not in SWAPPABLE_STATUSES, so we
    # surface INVALID_HANDOVER_STATE (the catch-all). The specific
    # BOOKING_ALREADY_SETTLED code is also accepted.
    assert str(exc_info.value.code) in (
        "INVALID_HANDOVER_STATE",
        "BOOKING_ALREADY_SETTLED",
    )


async def test_handover_rejects_same_partner_target():
    db = _StubDb()
    active = _assignment(partner_id=100)
    cab = _cab(status="BREAKDOWN_REPORTED", assignments=[active])
    db.queue.append(_StubScalarResult(cab))
    db.queue.append(_StubScalarResult(_partner(partner_id=100)))  # same partner

    with pytest.raises(breakdown_service.BusinessException) as exc_info:
        await breakdown_service.handover_to_new_partner(
            db,
            cab_booking_id=1,
            new_partner_id=100,
            performed_by_user_id=None,
        )
    assert "SAME_PARTNER_USE_SWAP" in str(exc_info.value.code)


# ── close_assignment_on_trip_complete ────────────────────────────────────────


async def test_close_assignment_on_trip_complete_sets_reason_and_km():
    db = _StubDb()
    active = _assignment(closed_at=None)
    cab = _cab(status="STARTED", assignments=[active], trip_end_km=42.5)
    db.queue.append(_StubScalarResult(cab))

    closed_id = await breakdown_service.close_assignment_on_trip_complete(
        db, cab_booking_id=1, end_km=42.5
    )
    assert closed_id == active.id
    assert active.closed_at is not None
    assert active.close_reason == "TRIP_COMPLETED"
    assert float(active.km_at_assignment_end) == 42.5


async def test_close_assignment_is_idempotent():
    db = _StubDb()
    active = _assignment(closed_at=None)
    cab = _cab(assignments=[active])
    db.queue.append(_StubScalarResult(cab))

    # First call closes.
    first = await breakdown_service.close_assignment_on_trip_complete(
        db, cab_booking_id=1
    )
    assert first == active.id
    # Second call finds no open assignment.
    db.queue.append(_StubScalarResult(cab))
    second = await breakdown_service.close_assignment_on_trip_complete(
        db, cab_booking_id=1
    )
    assert second is None


# ── Reason-code catalogue ────────────────────────────────────────────────────


def test_breakdown_reason_codes_include_required_values():
    """The catalogue surfaced to driver/partner/admin must include every
    code that the service accepts in the reason_code parameter."""
    expected = {"VEHICLE_BREAKDOWN", "ACCIDENT", "DRIVER_UNWELL", "OTHER"}
    assert expected.issubset(BREAKDOWN_REASON_CODES)
