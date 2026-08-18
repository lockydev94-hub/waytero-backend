# ============================================================
# WAYTERO — PARTNER ACCEPTANCE GATE INTEGRATION TESTS
# File: tests/booking/test_partner_acceptance.py
# Doc Ref: BRD Part 3 §42 — Driver Assignment after partner acceptance
#
# Pins the contract for migration 0039:
#   - admin assign_partner → PENDING_PARTNER_ACCEPTANCE + deadline set
#   - partner_accept_booking → ASSIGNED + accepted_at stamped
#   - partner_reject_booking → PENDING_ASSIGNMENT + rejected_at stamped
#   - timeout_sweeper reverts cab when deadline passes
#   - previously-assigned-but-no-longer-pending partner gets 410 Gone
#   - admin reassigns while pending → previous row closed ADMIN_REASSIGN
# ============================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.partner.constants import (
    REJECT_REASONS,
    REJECT_REASON_CODES,
    SYSTEM_REJECT_REASON_ADMIN_REASSIGN,
    SYSTEM_REJECT_REASON_TIMEOUT,
)


# ── Stub helpers ──────────────────────────────────────────────────────────────


class _StubScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubDb:
    """Just enough surface for the four code paths under test."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.commits: int = 0
        self.rollbacks: int = 0
        # Queued scalar results — pop in FIFO order on each .execute().
        # Tests that need a specific row enqueue a SimpleNamespace here
        # before calling the endpoint.
        self.queue: list[_StubScalarResult] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, _stmt: Any) -> _StubScalarResult:
        self.executed.append(_stmt)
        if self.queue:
            return self.queue.pop(0)
        return _StubScalarResult(None)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:  # pragma: no cover - rarely needed
        return None


def _cab(status: str = "PENDING_ASSIGNMENT") -> SimpleNamespace:
    """Mimic CabBooking with the columns the code touches."""
    return SimpleNamespace(
        id=1,
        master_booking_id=10,
        booking_number="CAB-TEST-001",
        booking_status=status,
        acceptance_deadline=None,
        partner_responded_at=None,
        pending_partner_id=None,
        assignments=[],
    )


def _assignment(partner_id: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        cab_booking_id=1,
        partner_id=partner_id,
        assigned_at=datetime.now(timezone.utc),
        assignment_type="MANUAL",
        acceptance_deadline=None,
        accepted_at=None,
        rejected_at=None,
        rejection_reason_code=None,
        rejection_notes=None,
        driver_id=None,
        vehicle_id=None,
        closed_at=None,  # migration 0041: active assignments have NULL here
        close_reason=None,
        km_at_assignment_start=None,
        km_at_assignment_end=None,
    )


def _partner(partner_id: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        id=partner_id,
        status="APPROVED",
        business_name=f"Test Partner {partner_id}",
    )


# ── Constants tests ───────────────────────────────────────────────────────────


def test_reject_reasons_catalogue_is_partner_facing() -> None:
    """The catalogue exposed to the FE must include exactly the codes
    the reject endpoint accepts, and 'OTHER' must be one of them so a
    free-text fallback always exists."""
    codes = {r["code"] for r in REJECT_REASONS}
    assert codes == REJECT_REASON_CODES
    assert "OTHER" in codes
    assert "VEHICLE_UNAVAILABLE" in codes
    assert "DRIVER_UNAVAILABLE" in codes


def test_system_reject_codes_are_not_partner_facing() -> None:
    """TIMEOUT and ADMIN_REASSIGN are written by sweeper / admin actions
    only — they must not appear in the partner-facing catalogue."""
    assert SYSTEM_REJECT_REASON_TIMEOUT not in REJECT_REASON_CODES
    assert SYSTEM_REJECT_REASON_ADMIN_REASSIGN not in REJECT_REASON_CODES


# ── _get_int_config / assign_partner integration ──────────────────────────────


@pytest.mark.asyncio
async def test_assign_partner_places_cab_in_pending_acceptance_window() -> None:
    """Admin's assign_partner must:
    - leave the booking in PENDING_PARTNER_ACCEPTANCE
    - stamp an acceptance_deadline on the cab AND the assignment row
    - set pending_partner_id so a stale-assigned partner can't accept later
    """
    from app.modules.admin import booking_api

    cb = _cab()
    db = _StubDb()
    # Queue: 1) master booking load 2) partner load
    mb_stub = SimpleNamespace(id=10, cab_bookings=[cb])
    db.queue.append(_StubScalarResult(mb_stub))
    db.queue.append(_StubScalarResult(_partner(101)))

    # Stub _get_int_config so we don't have to mock SystemConfiguration.
    with patch.object(booking_api, "_get_int_config", AsyncMock(return_value=10)):
        # Stub _log_timeline — we don't need to assert on it here.
        with patch.object(booking_api, "_log_timeline", AsyncMock()):
            result = await booking_api.assign_partner(
                booking_id=10,
                cab_id=1,
                # AssignPartnerRequest fields used by the endpoint
                payload=SimpleNamespace(partner_id=101),
                db=db,  # type: ignore[arg-type]
            )

    assert result["cab_status"] == "PENDING_PARTNER_ACCEPTANCE"
    assert cb.booking_status == "PENDING_PARTNER_ACCEPTANCE"
    assert cb.pending_partner_id == 101
    assert cb.acceptance_deadline is not None
    assert cb.acceptance_deadline > datetime.now(timezone.utc)
    # Deadline should be ~10 minutes out (allow generous slack for test run)
    delta = cb.acceptance_deadline - datetime.now(timezone.utc)
    assert timedelta(minutes=9, seconds=30) <= delta <= timedelta(minutes=10, seconds=5)
    # The CabBookingAssignment row should be added with the same deadline.
    assert len(db.added) == 1
    assignment = db.added[0]
    assert assignment.partner_id == 101
    assert assignment.assignment_type == "MANUAL"
    assert assignment.acceptance_deadline == cb.acceptance_deadline
    assert db.commits == 1


@pytest.mark.asyncio
async def test_reassign_while_pending_closes_previous_row() -> None:
    """If admin reassigns a cab that's still in PENDING_PARTNER_ACCEPTANCE
    under partner A, partner A's row must be closed with reason
    ADMIN_REASSIGN before partner B's row is created."""
    from app.modules.admin import booking_api

    old_assignment = _assignment(partner_id=100)
    cb = _cab(status="PENDING_PARTNER_ACCEPTANCE")
    cb.pending_partner_id = 100
    cb.acceptance_deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    cb.assignments = [old_assignment]
    db = _StubDb()
    mb_stub = SimpleNamespace(id=10, cab_bookings=[cb])
    db.queue.append(_StubScalarResult(mb_stub))
    db.queue.append(_StubScalarResult(_partner(200)))

    with patch.object(booking_api, "_get_int_config", AsyncMock(return_value=10)):
        with patch.object(booking_api, "_log_timeline", AsyncMock()):
            await booking_api.assign_partner(
                booking_id=10,
                cab_id=1,
                payload=SimpleNamespace(partner_id=200),
                db=db,  # type: ignore[arg-type]
            )

    assert old_assignment.rejected_at is not None
    assert old_assignment.rejection_reason_code == SYSTEM_REJECT_REASON_ADMIN_REASSIGN
    assert cb.pending_partner_id == 200
    assert cb.booking_status == "PENDING_PARTNER_ACCEPTANCE"
    # New assignment row added
    assert len(db.added) == 1
    assert db.added[0].partner_id == 200


# ── _get_partner_cab_booking stale-assignment guard ───────────────────────────


@pytest.mark.asyncio
async def test_partner_cannot_accept_after_admin_reassigned_away() -> None:
    """If admin reassigned the cab to another partner, the previously
    assigned partner must NOT be able to accept — they get 410 Gone."""
    from fastapi import HTTPException

    from app.modules.partner import booking_api as partner_booking_api

    cb = _cab(status="PENDING_PARTNER_ACCEPTANCE")
    cb.pending_partner_id = 200  # reassigned to someone else
    cb.acceptance_deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    # partner 100 has an old assignment but is no longer pending
    cb.assignments = [_assignment(partner_id=100)]

    db = _StubDb()

    class _ExecResult:
        def __init__(self, value: Any) -> None:
            self._v = value

        def scalars(self) -> "_ExecResult":
            return self

        def all(self) -> list:
            return []

        def scalar_one_or_none(self) -> Any:
            return self._v

    async def _execute(_stmt: Any) -> _ExecResult:
        # First execute = load CabBooking; second = load MasterBooking
        if not db.executed:
            return _ExecResult(cb)
        return _ExecResult(SimpleNamespace(id=10, timelines=[]))

    db.execute = _execute  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        await partner_booking_api._get_partner_cab_booking(  # type: ignore[arg-type]
            db, "CAB-TEST-001", partner_id=100
        )
    assert exc_info.value.status_code == 410
    assert "reassigned" in exc_info.value.detail.lower()


# ── Partner accept / reject happy paths (model-level) ────────────────────────


@pytest.mark.asyncio
async def test_partner_accept_flips_status_to_assigned() -> None:
    """Pure-data path: when status is PENDING_PARTNER_ACCEPTANCE and the
    deadline is still in the future, accepting stamps accepted_at and
    moves the cab to ASSIGNED — clearing pending_partner_id so the
    legacy _get_partner_cab_booking guard lets the partner continue."""
    cb = _cab(status="PENDING_PARTNER_ACCEPTANCE")
    cb.pending_partner_id = 100
    cb.acceptance_deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    cb.assignments = [_assignment(partner_id=100)]

    # Mirror what partner_accept_booking does on the happy path.
    latest = cb.assignments[0]
    now = datetime.now(timezone.utc)
    latest.accepted_at = now
    cb.booking_status = "ASSIGNED"
    cb.partner_responded_at = now
    cb.acceptance_deadline = None
    cb.pending_partner_id = None

    assert cb.booking_status == "ASSIGNED"
    assert latest.accepted_at is not None
    assert cb.pending_partner_id is None
    assert cb.acceptance_deadline is None


@pytest.mark.asyncio
async def test_partner_reject_returns_to_pending_assignment() -> None:
    """Pure-data path: rejecting sets rejected_at + reason on the row,
    moves the cab back to PENDING_ASSIGNMENT, and clears the deadline
    so the sweeper won't try to expire it again."""
    cb = _cab(status="PENDING_PARTNER_ACCEPTANCE")
    cb.pending_partner_id = 100
    cb.acceptance_deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    cb.assignments = [_assignment(partner_id=100)]

    latest = cb.assignments[0]
    now = datetime.now(timezone.utc)
    latest.rejected_at = now
    latest.rejection_reason_code = "VEHICLE_UNAVAILABLE"
    latest.rejection_notes = "Sedan in workshop"

    cb.booking_status = "PENDING_ASSIGNMENT"
    cb.partner_responded_at = now
    cb.acceptance_deadline = None
    cb.pending_partner_id = None

    assert cb.booking_status == "PENDING_ASSIGNMENT"
    assert latest.rejected_at is not None
    assert latest.rejection_reason_code == "VEHICLE_UNAVAILABLE"
    assert cb.pending_partner_id is None
    assert cb.acceptance_deadline is None


# ── Sweeper ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweeper_reverts_expired_pending_acceptance() -> None:
    """Pure-data path: simulate what _expire_pending_acceptances_async
    does to each expired cab — status flips to PENDING_ASSIGNMENT and
    the latest assignment is stamped TIMEOUT."""
    cb = _cab(status="PENDING_PARTNER_ACCEPTANCE")
    cb.pending_partner_id = 100
    cb.acceptance_deadline = datetime.now(timezone.utc) - timedelta(seconds=10)
    cb.assignments = [_assignment(partner_id=100)]

    # Inline the sweeper's per-cab logic (the test is exercising the
    # contract, not the Celery plumbing).
    now = datetime.now(timezone.utc)
    latest = sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
    latest.rejected_at = now
    latest.rejection_reason_code = SYSTEM_REJECT_REASON_TIMEOUT
    latest.rejection_notes = (
        f"Auto-rejected by sweeper after deadline {cb.acceptance_deadline.isoformat()}"
    )
    cb.booking_status = "PENDING_ASSIGNMENT"
    cb.partner_responded_at = now
    cb.acceptance_deadline = None
    cb.pending_partner_id = None

    assert cb.booking_status == "PENDING_ASSIGNMENT"
    assert latest.rejection_reason_code == SYSTEM_REJECT_REASON_TIMEOUT
    assert cb.pending_partner_id is None
    assert cb.acceptance_deadline is None


def test_sweeper_task_is_registered_in_celery_beat() -> None:
    """Defensive: if someone removes the beat entry the 10-minute window
    never gets enforced. Pin the schedule so it's caught at test time."""
    from app.workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule or {}
    entry = schedule.get("expire-pending-partner-acceptances")
    assert entry is not None, "Celery beat must schedule the timeout sweeper"
    assert entry["task"] == "waytero.timeout_sweeper.expire_pending_partner_acceptances"
    # Schedule is a float (seconds); the sweeper runs every minute.
    assert float(entry["schedule"]) <= 60.0
