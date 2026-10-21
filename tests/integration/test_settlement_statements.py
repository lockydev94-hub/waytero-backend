# ============================================================
# WAYTERO — ROUND 15 C5: PERIOD SETTLEMENT STATEMENTS
# File: tests/integration/test_settlement_statements.py
#
# What this proves:
#   1. A statement aggregates exactly the partner's cab bookings that reached
#      SETTLED inside the period and are not already on a statement — and sums
#      each booking's own frozen close figures:
#          gross = final + gst (tax invoice only)
#          commission = platform_commission
#          net payable = partner_payout (= gross − commission − gst)
#   2. A second statement for the same partner and period is refused by the
#      database's uq_settlements_partner_period, and a later period picks up
#      only the bookings that settled since (no money counted twice).
#   3. The status lifecycle follows the documented flow and refuses a jump:
#      GENERATED→APPROVED→PROCESSING→PAID→CLOSED, REJECTED out of review.
#   4. The broken writer's replacement stamps `settled_at` on PAID and never
#      writes the nonexistent `status`/`paid_at`/`updated_at`.
#   5. Every /admin/settlements/* route is declared super_only in the branch
#      registry — settlement crosses branches and no query filters by branch.
#
# The whole run is inside a transaction that is always rolled back.
# ============================================================
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.branch_scope_registry import classify
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.admin.services import settlement_statements as statements

PICKUP = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
PERIOD_START = date(2026, 11, 1)
PERIOD_END = date(2026, 11, 30)

TRADED_TABLES = (
    "cities",
    "users",
    "customers",
    "master_bookings",
    "cab_bookings",
    "cab_booking_assignments",
    "booking_timelines",
    "partners",
    "settlements",
    "settlement_items",
)


def _dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _fetch(sql: str, *args) -> list[dict]:
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_dsn())
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _count(table: str) -> int:
    return int(_fetch(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])  # noqa: S608


def _with_session(work):
    async def run():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                trans = await conn.begin()
                session = AsyncSession(
                    bind=conn,
                    expire_on_commit=False,
                    autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    return await work(session)
                finally:
                    await session.close()
                    await trans.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module", autouse=True)
def _row_counts_before_any_test() -> dict:
    return {t: _count(t) for t in TRADED_TABLES}


# ── seeds ─────────────────────────────────────────────────────────────────


async def _free_mobile(session: AsyncSession) -> str:
    row = (
        await session.execute(
            text(
                "SELECT '9' || lpad(n::text, 9, '0') AS mobile "
                "FROM generate_series(0, 999) AS n "
                "WHERE NOT EXISTS (SELECT 1 FROM users u "
                "                  WHERE u.mobile_number = '9' || lpad(n::text, 9, '0')) "
                "ORDER BY n LIMIT 1"
            )
        )
    ).mappings().one()
    return str(row["mobile"])


async def _seed_city(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                text("INSERT INTO cities (state_id, name, is_active) "
                     "VALUES (1, 'Statement Test City', FALSE) RETURNING id")
            )
        ).scalar_one()
    )


async def _seed_partner(session: AsyncSession, city_id: int) -> int:
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Statement Partner', :mobile, 'PARTNER')"),
        {"uid": user_id, "mobile": mobile},
    )
    partner_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO partners (user_id, partner_code, partner_type, "
                    "       owner_name, mobile, city_id, status, business_name) "
                    "VALUES (:uid, :code, 'INDIVIDUAL', 'Statement Cabs', :mobile, "
                    "        :city, 'ACTIVE', 'Statement Cabs') RETURNING id"
                ),
                {
                    "uid": user_id,
                    "code": f"IT-STMT-{str(user_id)[:8]}",
                    "mobile": mobile,
                    "city": city_id,
                },
            )
        ).scalar_one()
    )
    return partner_id


async def _seed_settled_booking(
    session: AsyncSession,
    *,
    partner_id: int,
    city_id: int,
    tag: str,
    final: Decimal,
    commission: Decimal,
    gst: Decimal = Decimal("0.00"),
    is_tax_invoice: bool = False,
    settled_on: date = date(2026, 11, 20),
    snapshot: str | None = '{"commission_type": "PERCENTAGE"}',
) -> int:
    """A cab booking that the per-booking settle already marked SETTLED, with the
    immutable BOOKING_SETTLED_BY_ADMIN event that dates it."""
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Statement Customer', :mobile, 'CUSTOMER')"),
        {"uid": user_id, "mobile": mobile},
    )
    customer_id = int(
        (
            await session.execute(
                text("INSERT INTO customers (user_id, customer_code, first_name) "
                     "VALUES (:uid, :code, 'Statement') RETURNING id"),
                {"uid": user_id, "code": f"IT-STMT-C-{str(user_id)[:8]}"},
            )
        ).scalar_one()
    )
    master_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO master_bookings (uuid, booking_number, customer_id, "
                    "       city_id, booking_status, payment_status, total_amount, "
                    "       journey_start_date) "
                    "VALUES (:uuid, :num, :cust, :city, 'CLOSED', 'PAID', :total, "
                    "        :start) RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "num": f"MB-STMT-{tag}",
                    "cust": customer_id,
                    "city": city_id,
                    "total": final,
                    "start": PICKUP.date(),
                },
            )
        ).scalar_one()
    )
    booking_number = f"CAB-STMT-{tag}"
    cab_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO cab_bookings (uuid, master_booking_id, "
                    "       booking_number, trip_type, vehicle_category_id, "
                    "       pickup_location, drop_location, pickup_datetime, "
                    "       estimated_distance, estimated_amount, booking_status, "
                    "       final_amount, platform_commission, partner_payout, "
                    "       gst_amount, is_tax_invoice, coupon_discount, "
                    "       actual_distance, commission_config_snapshot) "
                    "VALUES (:uuid, :mb, :num, 'ONE_WAY', 2, 'Statement Pickup', "
                    "        'Statement Drop', :pickup, 50.00, :final, 'SETTLED', "
                    "        :final, :commission, :payout, :gst, :taxi, 0.00, 42.0, "
                    "        CAST(:snapshot AS jsonb)) RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "mb": master_id,
                    "num": booking_number,
                    "pickup": PICKUP,
                    "final": final,
                    "commission": commission,
                    "payout": final - commission,
                    "gst": gst,
                    "taxi": is_tax_invoice,
                    "snapshot": snapshot,
                },
            )
        ).scalar_one()
    )
    await session.execute(
        text(
            "INSERT INTO cab_booking_assignments (cab_booking_id, partner_id, "
            "       assignment_type, assigned_at, accepted_at, closed_at) "
            "VALUES (:cb, :pid, 'MANUAL', NOW(), NOW(), NOW())"
        ),
        {"cb": cab_id, "pid": partner_id},
    )
    settled_at = datetime(
        settled_on.year, settled_on.month, settled_on.day, 10, 0, tzinfo=timezone.utc
    )
    await session.execute(
        text(
            "INSERT INTO booking_timelines (master_booking_id, event_type, "
            "       event_description, event_timestamp) "
            "VALUES (:m, 'BOOKING_SETTLED_BY_ADMIN', :desc, :at)"
        ),
        {
            "m": master_id,
            "desc": f"Admin settled {booking_number} via Settlements page.",
            "at": settled_at,
        },
    )
    await session.flush()
    return cab_id


async def _statement_row(session: AsyncSession, settlement_id: int) -> dict:
    await session.flush()
    session.expire_all()
    return dict(
        (
            await session.execute(
                text("SELECT settlement_number, settlement_status, gross_amount, "
                     "       commission_amount, gst_amount, net_payable_amount, "
                     "       start_date, end_date, settled_at "
                     "FROM settlements WHERE id = :id"),
                {"id": settlement_id},
            )
        ).mappings().one()
    )


def _expect_business_error(coro):
    async def run():
        try:
            await coro
        except BusinessException as exc:
            return str(exc)
        return None

    return run()


# ── 1. generation sums the frozen close figures ───────────────────────────


def test_a_statement_sums_the_partners_settled_bookings_for_the_period() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        partner_id = await _seed_partner(session, city_id)
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="A",
            final=Decimal("1000.00"), commission=Decimal("100.00"),
        )
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="B",
            final=Decimal("2000.00"), commission=Decimal("200.00"),
            gst=Decimal("100.00"), is_tax_invoice=True,
            snapshot=None,
        )
        # Outside the period — must not be counted.
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="C",
            final=Decimal("5000.00"), commission=Decimal("500.00"),
            settled_on=date(2026, 12, 5),
        )
        result = await statements.generate_statement(
            session, partner_id=partner_id,
            start_date=PERIOD_START, end_date=PERIOD_END,
        )
        row = await _statement_row(session, result["settlement_id"])
        items = (
            await session.execute(
                text("SELECT booking_id, gross_amount, commission_amount, net_amount "
                     "FROM settlement_items WHERE settlement_id = :s ORDER BY id"),
                {"s": result["settlement_id"]},
            )
        ).mappings().all()
        return {"result": result, "row": row, "items": [dict(i) for i in items]}

    out = _with_session(work)
    row = out["row"]

    assert row["settlement_status"] == "GENERATED"
    assert row["start_date"] == PERIOD_START
    assert row["end_date"] == PERIOD_END
    # Booking A: gross 1000, commission 100. Booking B: gross 2100, comm 200, gst 100.
    assert Decimal(str(row["gross_amount"])) == Decimal("3100.00")
    assert Decimal(str(row["commission_amount"])) == Decimal("300.00")
    assert Decimal(str(row["gst_amount"])) == Decimal("100.00")
    assert Decimal(str(row["net_payable_amount"])) == Decimal("2700.00")
    # The identity the close guarantees, per booking and in total.
    assert (
        Decimal(str(row["gross_amount"]))
        - Decimal(str(row["commission_amount"]))
        - Decimal(str(row["gst_amount"]))
        == Decimal(str(row["net_payable_amount"]))
    )
    assert len(out["items"]) == 2, "the December booking is outside the period"
    assert {Decimal(str(i["net_amount"])) for i in out["items"]} == {
        Decimal("900.00"),
        Decimal("1800.00"),
    }
    assert row["settlement_number"].startswith("WT-SET-202611-")


# ── 2. no double counting ─────────────────────────────────────────────────


def test_a_second_statement_for_the_same_period_is_refused() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        partner_id = await _seed_partner(session, city_id)
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="DUP",
            final=Decimal("1000.00"), commission=Decimal("100.00"),
        )
        await statements.generate_statement(
            session, partner_id=partner_id,
            start_date=PERIOD_START, end_date=PERIOD_END,
        )
        message = await _expect_business_error(
            statements.generate_statement(
                session, partner_id=partner_id,
                start_date=PERIOD_START, end_date=PERIOD_END,
            )
        )
        return {"message": message}

    out = _with_session(work)
    assert out["message"], "a duplicate period must be refused, not written twice"
    assert "already exists" in out["message"]


def test_a_later_period_picks_up_only_the_newly_settled_bookings() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        partner_id = await _seed_partner(session, city_id)
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="OLD",
            final=Decimal("1000.00"), commission=Decimal("100.00"),
        )
        first = await statements.generate_statement(
            session, partner_id=partner_id,
            start_date=PERIOD_START, end_date=PERIOD_END,
        )
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="NEW",
            final=Decimal("700.00"), commission=Decimal("70.00"),
            settled_on=date(2026, 12, 5),
        )
        second = await statements.generate_statement(
            session, partner_id=partner_id,
            start_date=date(2026, 12, 1), end_date=date(2026, 12, 31),
        )
        return {"first": first, "second": second}

    out = _with_session(work)
    assert out["first"]["booking_count"] == 1
    assert out["second"]["booking_count"] == 1
    assert Decimal(str(out["second"]["gross_amount"])) == Decimal("700.00")


# ── 3. the lifecycle ──────────────────────────────────────────────────────


def test_the_lifecycle_follows_the_documented_flow_and_stamps_settled_at() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        partner_id = await _seed_partner(session, city_id)
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="LIFE",
            final=Decimal("1000.00"), commission=Decimal("100.00"),
        )
        gen = await statements.generate_statement(
            session, partner_id=partner_id,
            start_date=PERIOD_START, end_date=PERIOD_END,
        )
        sid = gen["settlement_id"]

        # A jump straight to PAID from GENERATED is refused.
        jump = await _expect_business_error(
            statements.transition_statement(
                session, settlement_id=sid, to_status="PAID"
            )
        )
        steps = []
        for target in ("APPROVED", "PROCESSING", "PAID"):
            steps.append(
                await statements.transition_statement(
                    session, settlement_id=sid, to_status=target
                )
            )
        after_paid = await _statement_row(session, sid)
        closed = await statements.transition_statement(
            session, settlement_id=sid, to_status="CLOSED"
        )
        after_closed = await _statement_row(session, sid)
        terminal = await _expect_business_error(
            statements.transition_statement(
                session, settlement_id=sid, to_status="PROCESSING"
            )
        )
        return {
            "jump": jump, "steps": steps, "after_paid": after_paid,
            "closed": closed, "after_closed": after_closed, "terminal": terminal,
        }

    out = _with_session(work)
    assert out["jump"] and "cannot move" in out["jump"]
    assert [s["status"] for s in out["steps"]] == ["APPROVED", "PROCESSING", "PAID"]
    assert out["after_paid"]["settled_at"] is not None, "PAID stamps settled_at"
    assert out["closed"]["status"] == "CLOSED"
    assert out["after_closed"]["settled_at"] is not None
    assert out["terminal"] and "final" in out["terminal"]


def test_rejection_is_allowed_out_of_review_and_is_final() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        partner_id = await _seed_partner(session, city_id)
        await _seed_settled_booking(
            session, partner_id=partner_id, city_id=city_id, tag="REJ",
            final=Decimal("1000.00"), commission=Decimal("100.00"),
        )
        gen = await statements.generate_statement(
            session, partner_id=partner_id,
            start_date=PERIOD_START, end_date=PERIOD_END,
        )
        rejected = await statements.transition_statement(
            session, settlement_id=gen["settlement_id"], to_status="REJECTED",
            reason="Wrong period",
        )
        after = await _statement_row(session, gen["settlement_id"])
        refused = await _expect_business_error(
            statements.transition_statement(
                session, settlement_id=gen["settlement_id"], to_status="APPROVED"
            )
        )
        return {"rejected": rejected, "after": after, "refused": refused}

    out = _with_session(work)
    assert out["rejected"]["status"] == "REJECTED"
    assert out["after"]["settlement_status"] == "REJECTED"
    assert out["refused"] and "final" in out["refused"]


# ── 4. the branch gate ────────────────────────────────────────────────────


def test_every_settlement_route_is_declared_master_admin_only() -> None:
    for method, path in (
        ("GET", "/api/v1/admin/settlements"),
        ("GET", "/api/v1/admin/settlements/list"),
        ("POST", "/api/v1/admin/settlements/settle"),
        ("POST", "/api/v1/admin/settlements/hotel/settle"),
        ("POST", "/api/v1/admin/settlements/tour/settle"),
        ("POST", "/api/v1/admin/settlements/statements/generate"),
        ("GET", "/api/v1/admin/settlements/statements"),
        ("PATCH", "/api/v1/admin/settlements/statements/7/status"),
        ("PATCH", "/api/v1/admin/settlements/7/mark-paid"),
        ("PATCH", "/api/v1/admin/settlements/7/mark-processing"),
        ("GET", "/api/v1/admin/settlements/coupon-disbursements"),
        ("POST", "/api/v1/admin/settlements/disburse-coupon"),
        ("GET", "/api/v1/admin/settlements/handover-reconciliation"),
    ):
        # Template keys are what the registry is keyed on; 7 stands for {id}
        # here only where the route declares a path parameter.
        template = path.replace("/7/", "/{settlement_id}/")
        assert classify(method, template) == "super_only", f"{method} {template}"


def test_nothing_survives_the_transaction(_row_counts_before_any_test: dict) -> None:
    after = {t: _count(t) for t in TRADED_TABLES}
    changed = {
        t: (before, after[t])
        for t, before in _row_counts_before_any_test.items()
        if before != after[t]
    }
    assert not changed, f"rows outlived the rollback: {changed}"
