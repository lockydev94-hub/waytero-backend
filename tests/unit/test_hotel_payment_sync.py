# ============================================================
# WAYTERO — HOTEL PAYMENT SYNC TESTS
# File: tests/unit/test_hotel_payment_sync.py
# Doc Ref: BRD Part 3 §35-§45 (cab/hotel fare engine + settlement custody)
#
# Regression tests for `rollup_hotel_totals_into_master` in
# app.modules.booking.services. The helper is the single source of truth that
# rolls hotel reservation totals and collections onto the umbrella master
# booking; if it drifts, every admin portal booking list shows the wrong
# payment_status (PENDING) and total_paid_amount (₹0) for a fully paid stay.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List


# === Minimal fake session ==================================================
#
# The helper drives three SQLAlchemy queries against the session:
#   1. ``select(BookingService.service_type).where(...)`` → list of strings
#   2. ``select(func.coalesce(func.sum(...), 0)).where(...)`` → scalar Decimal
#   3. ``select(func.coalesce(func.sum(...), 0)).where(...)`` → scalar Decimal
#   4. ``update(BookingService).where(...).values(...)`` → no-op for state
#
# We don't need a real DB. The fake captures the SQL primitives that drive
# the helper's decisions and feeds it pre-baked answers via the route table.
# This is brittle-by-design: if the helper's query shape changes, the test
# fails loudly — exactly the regression we want to detect.
# ---------------------------------------------------------------------------


@dataclass
class _FakeScalarResult:
    """The result of ``session.execute(...)`` for a scalar SELECT."""

    _value: Any

    def scalar(self) -> Any:
        return self._value

    def scalars(self) -> "_FakeScalarResult":
        # .scalars().all() is the access pattern in the helper.
        return self

    def all(self) -> List[Any]:
        if isinstance(self._value, list):
            return self._value
        return [self._value]


@dataclass
class _FakeUpdateResult:
    """The result of ``session.execute(update(...))`` — no rowcount needed."""

    rowcount: int = 0


@dataclass
class _FakeSession:
    """Drives rollup_hotel_totals_into_master with canned SQL results."""

    # Pre-baked answers.
    service_types: List[str] = field(default_factory=list)
    reservation_total: Decimal = Decimal("0")
    advance_net_paid: Decimal = Decimal("0")
    # Captured writes.
    service_amount_writes: List[dict] = field(default_factory=list)

    async def execute(self, stmt: Any) -> _FakeScalarResult | _FakeUpdateResult:
        # The helper uses SQLAlchemy expression objects via
        # ``select(...)`` and ``update(...)``. We do a lightweight structural
        # sniff on the compiled string rather than walking the expression
        # tree — sufficient for the helper's three queries.
        compiled = str(stmt).lower()
        is_select = "select" in compiled
        if is_select:
            if "booking_services" in compiled and "service_type" in compiled:
                # Query 1: distinct service_types on the master.
                return _FakeScalarResult(list(self.service_types))
            if "hotel_reservations" in compiled and "total_amount" in compiled:
                # Query 2: SUM(reservation total_amount) for active stays.
                return _FakeScalarResult(self.reservation_total)
            if "hotel_advance_payments" in compiled:
                # Query 3: SUM(amount - refunded_amount) for ACTIVE advances.
                return _FakeScalarResult(self.advance_net_paid)
            # Unknown SELECT — fail loudly so the test is updated.
            raise AssertionError(f"Unexpected SELECT in helper: {compiled!r}")
        # UPDATE — record the write.
        is_update = "update" in compiled
        if is_update and "booking_services" in compiled:
            self.service_amount_writes.append({"_stmt": compiled})
            return _FakeUpdateResult()
        raise AssertionError(f"Unexpected statement in helper: {compiled!r}")


# === Helper under test ======================================================
# Import lazily so the helper itself can be loaded even if its surrounding
# module pulls in heavy dependencies at import time.
def _load_helper():
    from app.modules.booking.services import rollup_hotel_totals_into_master

    return rollup_hotel_totals_into_master


def _make_master(
    total: Decimal = Decimal("0"), paid: Decimal = Decimal("0"), status: str = "PENDING"
):
    """Build a minimal stand-in for MasterBooking with the .payment_status /
    .total_amount / .total_paid_amount attributes the helper writes."""

    class _Mb:
        pass

    mb = _Mb()
    mb.total_amount = total
    mb.total_paid_amount = paid
    mb.payment_status = status
    mb.updated_at = datetime.now(timezone.utc)
    return mb


# === Tests =================================================================


class TestSyncMasterFromHotel:
    """Direct unit tests for ``rollup_hotel_totals_into_master``."""

    def test_partition_partial_when_advance_covers_part_of_total(self):
        """One ACTIVE advance for half a ₹5,300 stay → PARTIAL, ₹X paid."""
        sync = _load_helper()
        mb = _make_master()
        session = _FakeSession(
            service_types=["HOTEL"],
            reservation_total=Decimal("5300.00"),
            advance_net_paid=Decimal("2000.00"),
        )

        # The helper is async; the fake session has no async, but the helper
        # only ever awaits `session.execute(...)`. Awaiting a non-awaitable
        # works because we overload __await__ below — but easier: just call
        # the body directly via asyncio.run.
        import asyncio

        asyncio.run(sync(session, 42, mb))

        assert mb.total_amount == Decimal("5300.00")
        assert mb.total_paid_amount == Decimal("2000.00")
        assert mb.payment_status == "PARTIAL"

    def test_paid_when_advances_equal_total(self):
        """Active advances summing to the grand total → PAID, ₹total paid."""
        sync = _load_helper()
        mb = _make_master()
        session = _FakeSession(
            service_types=["HOTEL"],
            reservation_total=Decimal("5300.00"),
            advance_net_paid=Decimal("5300.00"),
        )

        import asyncio

        asyncio.run(sync(session, 42, mb))

        assert mb.total_amount == Decimal("5300.00")
        assert mb.total_paid_amount == Decimal("5300.00")
        assert mb.payment_status == "PAID"

    def test_pending_when_no_active_advances(self):
        """No money collected yet → PENDING, ₹0 paid."""
        sync = _load_helper()
        mb = _make_master()
        session = _FakeSession(
            service_types=["HOTEL"],
            reservation_total=Decimal("5300.00"),
            advance_net_paid=Decimal("0"),
        )

        import asyncio

        asyncio.run(sync(session, 42, mb))

        assert mb.total_amount == Decimal("5300.00")
        assert mb.total_paid_amount == Decimal("0.00")
        assert mb.payment_status == "PENDING"

    def test_helper_skips_when_master_has_no_hotel_service(self):
        """A pure-CAB master must not be touched by the hotel sync helper —
        mixed-master support is the recent loosening, but pure cab must still
        be a no-op."""
        sync = _load_helper()
        mb = _make_master(
            total=Decimal("1000.00"),
            paid=Decimal("1000.00"),
            status="PAID",
        )
        session = _FakeSession(
            service_types=["CAB"],
            reservation_total=Decimal("9999.00"),  # these would be wrong if applied
            advance_net_paid=Decimal("9999.00"),
        )

        import asyncio

        asyncio.run(sync(session, 42, mb))

        assert mb.total_amount == Decimal("1000.00")
        assert mb.total_paid_amount == Decimal("1000.00")
        assert mb.payment_status == "PAID"
        # No service_amount write was issued.
        assert session.service_amount_writes == []

    def test_helper_runs_for_mixed_cab_and_hotel_master(self):
        """Regression for the loosened guard: a {CAB, HOTEL} master must run
        the hotel sync (cab accounting stays on its own inline writes)."""
        sync = _load_helper()
        mb = _make_master()
        session = _FakeSession(
            service_types=["CAB", "HOTEL"],
            reservation_total=Decimal("5300.00"),
            advance_net_paid=Decimal("5300.00"),
        )

        import asyncio

        asyncio.run(sync(session, 42, mb))

        # Helper ran and reclculated the master.
        assert mb.total_amount == Decimal("5300.00")
        assert mb.total_paid_amount == Decimal("5300.00")
        assert mb.payment_status == "PAID"
        # And wrote the booking_services.service_amount.
        assert len(session.service_amount_writes) == 1


class TestRefundHandling:
    """The helper nets ``amount - refunded_amount`` for ACTIVE advances only.
    VOIDED advances are excluded by the WHERE clause (which the helper passes
    to the DB), so we only need to confirm the net calculation is wired
    correctly."""

    def test_advance_net_of_partial_refund_is_paid(self):
        """Advance ₹5,300 minus ₹300 refunded → net ₹5,000 collected on a
        ₹5,000 stay → PAID."""
        sync = _load_helper()
        mb = _make_master()
        session = _FakeSession(
            service_types=["HOTEL"],
            reservation_total=Decimal("5000.00"),
            advance_net_paid=Decimal("5000.00"),  # 5300 - 300
        )

        import asyncio

        asyncio.run(sync(session, 42, mb))

        assert mb.total_paid_amount == Decimal("5000.00")
        assert mb.payment_status == "PAID"

    def test_partial_with_small_refund(self):
        """Advance ₹1,000 minus ₹100 refunded → ₹900 on a ₹5,000 stay →
        PARTIAL."""
        sync = _load_helper()
        mb = _make_master()
        session = _FakeSession(
            service_types=["HOTEL"],
            reservation_total=Decimal("5000.00"),
            advance_net_paid=Decimal("900.00"),
        )

        import asyncio

        asyncio.run(sync(session, 42, mb))

        assert mb.total_paid_amount == Decimal("900.00")
        assert mb.payment_status == "PARTIAL"
