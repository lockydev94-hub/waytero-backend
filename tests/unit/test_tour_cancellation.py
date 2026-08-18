# ============================================================
# WAY TERO — TOUR CANCELLATION POLICY ENGINE TESTS
# File: tests/unit/test_tour_cancellation.py
# Doc Ref: BRD Part 5 §119 — Tour cancellation policy
#
# Pins the tour cancellation ladder:
#   * global fallback (system_configurations, seeded by migration 0060)
#   * per-package override (tour_package_policies, migration 0062)
#   * refund capped at the live advance; charge = total - refund
#   * apply_tour_cancellation writes the full audit trail
#     (booking status, wallet credit, advance refund, cancellation row,
#     master totals, timeline) and returns the policy numbers.
#
# The session is a recording fake — no Postgres involved. The contract
# under test is the policy math + the SQL statements the orchestrator
# issues, not the DB itself.
# ============================================================

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.modules.booking.services.cancellation_policy import (
    CANCEL_SOURCE_CUSTOMER,
    apply_tour_cancellation,
    get_tour_policy_effective,
    quote_tour_cancellation,
)

NOW = datetime.now(timezone.utc)

CONFIG_VALUES = {
    "TOUR_CANCELLATION_FREE_DAYS": "30",
    "TOUR_CANCELLATION_TIER_1_DAYS": "15",
    "TOUR_CANCELLATION_TIER_2_DAYS": "7",
    "TOUR_CANCELLATION_TIER_1_PERCENT": "100",
    "TOUR_CANCELLATION_TIER_2_PERCENT": "75",
    "TOUR_CANCELLATION_TIER_3_PERCENT": "50",
    "TOUR_CANCELLATION_LAST_MINUTE_PERCENT": "0",
}


class _Rows:
    """Result-like returned by the fake session's execute()."""

    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows and self._rows[0] else None

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Async session recording every statement + params, with canned
    responses keyed by SQL substrings. Unmatched statements return empty
    results but are still recorded so tests can assert what was written."""

    def __init__(self, responses):
        self.responses = responses  # dict[substring] -> FakeResult | callable
        self.records = []  # list[(sql, params)]

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        self.records.append((sql, params))
        for key, res in self.responses.items():
            if key in sql:
                return res(sql, params) if callable(res) else res
        return _Rows([])


def _quote_row(*, package_policy: bool, days_to_travel: int | None, total="100000"):
    """Mapping row for quote_tour_cancellation's booking join query."""
    # travel_start_date is a Postgres DATE — the engine does date - date.
    travel = (
        NOW.date() + timedelta(days=days_to_travel)
        if days_to_travel is not None
        else None
    )
    base = {
        "travel_start_date": travel,
        "booking_status": "CONFIRMED",
        "package_id": 42,
        "cancellation_free_days": None,
        "cancellation_tier_1_days": None,
        "refund_percent_tier_1": None,
        "cancellation_tier_2_days": None,
        "refund_percent_tier_2": None,
        "refund_percent_tier_3": None,
        "refund_percent_last_minute": None,
        "cancellation_policy_text": None,
    }
    if package_policy:
        base.update(
            {
                "cancellation_free_days": 45,
                "cancellation_tier_1_days": 25,
                "refund_percent_tier_1": Decimal("100"),
                "cancellation_tier_2_days": 10,
                "refund_percent_tier_2": Decimal("80"),
                "refund_percent_tier_3": Decimal("40"),
                "refund_percent_last_minute": Decimal("10"),
                "cancellation_policy_text": "Package-specific terms",
            }
        )
    return base


def _quote_session(*, package_policy=False, days_to_travel=20, advance="25000"):
    return FakeSession(
        {
            "tp.id AS package_id": _Rows(
                [
                    _quote_row(
                        package_policy=package_policy,
                        days_to_travel=days_to_travel,
                    )
                ]
            ),
            "FROM system_configurations WHERE config_key = :k": (
                lambda sql, p: _Rows([(CONFIG_VALUES.get(p.get("k"), "0"),)])
            ),
        }
    )


async def _quote(**kw):
    db = _quote_session(**kw)
    return await quote_tour_cancellation(
        db,
        tour_booking_id=101,
        tour_total_amount=Decimal(kw.get("total", "100000")),
        advance_paid_total=Decimal(kw.get("advance", "25000")),
        now=NOW,
    )


# ── Global ladder tiers ──────────────────────────────────────────


async def test_quote_global_free_window():
    q = await _quote(days_to_travel=40)
    assert q.tier_label == "free_window"
    assert q.refund_percent == Decimal("100")
    assert q.refund_amount == Decimal("25000")  # capped at advance
    assert q.charge == Decimal("75000")
    assert q.policy_snapshot["source"] == "global_tour_ladder"


async def test_quote_global_tier_1():
    q = await _quote(days_to_travel=20)
    assert q.tier_label == "tier_1"
    assert q.refund_percent == Decimal("75")
    # 75% of total = 75000, but only 25000 was paid → refund 25000.
    assert q.refund_amount == Decimal("25000")
    assert q.charge == Decimal("75000")


async def test_quote_global_tier_2():
    q = await _quote(days_to_travel=9)
    assert q.tier_label == "tier_2"
    assert q.refund_percent == Decimal("50")


async def test_quote_global_last_minute():
    q = await _quote(days_to_travel=2)
    assert q.tier_label == "last_minute"
    assert q.refund_percent == Decimal("0")
    assert q.refund_amount == Decimal("0")
    assert q.charge == Decimal("100000")


async def test_quote_global_no_travel_date():
    q = await _quote(days_to_travel=None)
    assert q.tier_label == "no_travel_date"
    assert q.refund_amount == Decimal("0")


# ── Per-package policy ───────────────────────────────────────────


async def test_quote_package_policy_overrides_global():
    q = await _quote(package_policy=True, days_to_travel=50)
    assert q.policy_snapshot["source"] == "tour_package_policy"
    assert q.policy_snapshot["package_id"] == 42
    # Global ladder would say free_window too here; package free window is
    # 45d, so 50 days is inside it.
    assert q.tier_label == "free_window"
    assert q.refund_percent == Decimal("100")


async def test_quote_package_policy_tier_math():
    q = await _quote(package_policy=True, days_to_travel=12)
    # Package: free 45 / t1 25 / t2 10 → 12 days is tier_2 at 40%.
    assert q.tier_label == "tier_2"
    assert q.refund_percent == Decimal("40")


async def test_quote_package_policy_last_minute():
    q = await _quote(package_policy=True, days_to_travel=3)
    # 3 days < t2 (10) → last-minute at package's 10%.
    assert q.tier_label == "last_minute"
    assert q.refund_percent == Decimal("10")


# ── Money invariants ─────────────────────────────────────────────


async def test_quote_refund_capped_at_advance():
    q = await _quote(days_to_travel=40, advance="5000")
    # 100% of 100000 = 100000 but only 5000 paid.
    assert q.refund_amount == Decimal("5000")
    assert q.charge == Decimal("95000")


async def test_quote_full_payment_charge_plus_refund_equals_total():
    q = await _quote(days_to_travel=20, advance="100000")
    assert q.refund_amount == Decimal("75000")
    assert q.charge == Decimal("25000")
    assert q.refund_amount + q.charge == Decimal("100000")


# ── apply_tour_cancellation ──────────────────────────────────────


def _apply_session(*, advance="25000", existing_cxl=False, package_policy=False):
    return FakeSession(
        {
            # 1. tour booking row (plain first())
            "SELECT id, master_booking_id, booking_status, total_amount,\n                       travel_start_date\n                FROM tour_bookings": _Rows(
                [
                    (
                        101,
                        55,
                        "CONFIRMED",
                        Decimal("100000"),
                        (NOW.date() + timedelta(days=20)).isoformat(),
                    )
                ]
            ),
            # 2. customer via master
            "SELECT customer_id FROM master_bookings": _Rows([(7,)]),
            # 3. advance sum
            # advance sum — note the SQL has a newline before WHERE.
            "tour_booking_id = :id AND status = 'ACTIVE'": _Rows([(Decimal(advance),)]),
            # 4. quote join row
            "tp.id AS package_id": _Rows(
                [_quote_row(package_policy=package_policy, days_to_travel=20)]
            ),
            # 5. global config reads
            "FROM system_configurations WHERE config_key = :k": (
                lambda sql, p: _Rows([(CONFIG_VALUES.get(p.get("k"), "0"),)])
            ),
            # 6. wallet
            "FROM customer_wallets WHERE customer_id": _Rows([(900, Decimal("5000"))]),
            # 7. partner recovery — no partner-held advances in these tests
            "GROUP BY tp.partner_id": _Rows([]),
            # 8. existing cancellation row?
            "FROM booking_cancellations": (
                _Rows([(444,)]) if existing_cxl else _Rows([])
            ),
        }
    )


async def test_apply_tour_cancellation_full_flow():
    db = _apply_session()
    result = await apply_tour_cancellation(
        db,
        tour_booking_id=101,
        master_booking_id=55,
        cancellation_reason="Customer changed plans",
        cancelled_by_user_id=uuid4(),
        cancelled_by_role="CUSTOMER",
        cancelled_source=CANCEL_SOURCE_CUSTOMER,
        now=NOW,
    )

    # Engine numbers.
    assert result["tour_booking_id"] == 101
    assert result["tier_label"] == "tier_1"
    assert result["refund_amount"] == Decimal("25000")
    assert result["advance_refunded_total"] == Decimal("25000")
    assert result["charge"] == Decimal("75000")
    assert result["new_wallet_balance"] == Decimal("30000")  # 5000 + 25000

    sqls = [sql for sql, _ in db.records]

    # Tour flipped to CANCELLED.
    tour_upd = next(s for s in sqls if "UPDATE tour_bookings" in s)
    assert "booking_status = 'CANCELLED'" in tour_upd

    # Wallet credited.
    assert any("UPDATE customer_wallets" in s for s in sqls)
    assert any("INSERT INTO customer_wallet_ledger" in s for s in sqls)

    # Advance marked refunded.
    adv_upd = next(s for s in sqls if "UPDATE tour_advance_payments" in s)
    assert "status          = CASE" in adv_upd

    # Master totals bumped.
    assert any(
        "UPDATE master_bookings" in s and "total_refund_amount" in s for s in sqls
    )

    # Cancellation row inserted + timeline.
    assert any("INSERT INTO booking_cancellations" in s for s in sqls)
    assert any(
        "INSERT INTO booking_timelines" in s and "TOUR_CANCELLED" in s for s in sqls
    )


async def test_apply_tour_cancellation_zero_refund_no_wallet_write():
    # Last-minute tier → refund 0 → wallet untouched.
    db = FakeSession(
        {
            "SELECT id, master_booking_id, booking_status, total_amount,\n                       travel_start_date\n                FROM tour_bookings": _Rows(
                [
                    (
                        102,
                        56,
                        "CONFIRMED",
                        Decimal("100000"),
                        (NOW.date() + timedelta(days=2)).isoformat(),
                    )
                ]
            ),
            "SELECT customer_id FROM master_bookings": _Rows([(7,)]),
            "FROM tour_advance_payments WHERE tour_booking_id": _Rows(
                [(Decimal("25000"),)]
            ),
            "tp.id AS package_id": _Rows(
                [_quote_row(package_policy=False, days_to_travel=2)]
            ),
            "FROM system_configurations WHERE config_key = :k": (
                lambda sql, p: _Rows([(CONFIG_VALUES.get(p.get("k"), "0"),)])
            ),
            "GROUP BY tp.partner_id": _Rows([]),
            "FROM booking_cancellations": _Rows([]),
        }
    )
    result = await apply_tour_cancellation(
        db,
        tour_booking_id=102,
        master_booking_id=56,
        cancellation_reason="Last minute",
        cancelled_by_user_id=uuid4(),
        cancelled_by_role="CUSTOMER",
        cancelled_source=CANCEL_SOURCE_CUSTOMER,
        now=NOW,
    )
    assert result["refund_amount"] == Decimal("0")
    sqls = [sql for sql, _ in db.records]
    assert not any("customer_wallet_ledger" in s for s in sqls)
    assert not any("UPDATE customer_wallets" in s for s in sqls)
    assert any("INSERT INTO booking_cancellations" in s for s in sqls)


async def test_apply_tour_cancellation_second_call_updates_row():
    # Two tours under one master → second call updates the cancellation row.
    db = _apply_session(existing_cxl=True)
    await apply_tour_cancellation(
        db,
        tour_booking_id=103,
        master_booking_id=55,
        cancellation_reason="Second service cancelled",
        cancelled_by_user_id=uuid4(),
        cancelled_by_role="CUSTOMER",
        cancelled_source=CANCEL_SOURCE_CUSTOMER,
        now=NOW,
    )
    sqls = [sql for sql, _ in db.records]
    assert not any("INSERT INTO booking_cancellations" in s for s in sqls)
    upd = next(s for s in sqls if "UPDATE booking_cancellations" in s)
    assert "refund_amount          = :refund" in upd


# ── Effective-policy read ────────────────────────────────────────


async def test_get_tour_policy_effective():
    rows = [
        {"config_key": k, "config_value": v, "description": f"desc-{k}"}
        for k, v in CONFIG_VALUES.items()
    ]

    class _M:
        def __init__(self, rows_):
            self._rows = rows_

        def mappings(self):
            return self

        def all(self):
            return self._rows

    db = FakeSession({"TOUR_CANCELLATION_%": _M(rows)})
    out = await get_tour_policy_effective(db)
    assert out["TOUR_CANCELLATION_FREE_DAYS"]["value"] == "30"
    assert out["TOUR_CANCELLATION_LAST_MINUTE_PERCENT"]["value"] == "0"
    assert len(out) == 7
