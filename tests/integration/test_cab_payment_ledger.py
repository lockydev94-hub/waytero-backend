# ============================================================
# WAYTERO — ROUND 15 C4b: THE PAYMENTS LEDGER AND THE DERIVED STATE
# File: tests/integration/test_cab_payment_ledger.py
#
# What this proves: money moves, a `payments` row appears, and the master's
# payment state is arithmetic over those rows rather than typed in. `payments`
# was migrated by the finance schema and never written to; `payment_status` was
# hard-coded to 'PAID' by whichever endpoint recorded a balance, so a booking the
# customer had only half paid read 'PAID' to finance, to the desk and to the
# customer's own history.
#
#   1. An advance taken at the desk writes exactly one live ADVANCE ledger line,
#      and the master goes PARTIAL — not because anyone set it, but because the
#      money held is less than the fare.
#   2. The balance the customer hands over at close is a BALANCE_PAYMENT when an
#      advance is already held, a FULL_PAYMENT when nothing was — the distinction
#      DB Schema Part 7 §4 names — and a zero balance records nothing at all.
#   3. The master walks PENDING → PARTIAL → PAID purely as a function of the
#      ledger, against the closed fare (GST on a tax invoice, this cab's coupon
#      off it), and a cancelled line owes nothing.
#   4. A refund is pulled out of the ledger line (status follows the amount, a
#      `refunds` document points at it); a voided receipt leaves the books as if
#      the advance never counted.
#   5. A master that also carries a hotel line is left for its own rollup — the
#      derive does not touch mixed masters.
#
# Everything is seeded into its own city and thrown away: the whole run happens
# inside a transaction that is always rolled back, and the last test asserts the
# tables still hold the row counts they started with.
# ============================================================
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.modules.admin.payment_api import InitiateRefundIn, initiate_refund
from app.modules.booking.services import create_advance, void_advance
from app.modules.booking.services import payments as payments_service

CATEGORY = 2  # SEDAN
PICKUP = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
TRIP_START = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

TRADED_TABLES = (
    "cities",
    "users",
    "customers",
    "master_bookings",
    "cab_bookings",
    "cab_booking_assignments",
    "booking_services",
    "advance_payments",
    "payments",
    "refunds",
    "invoice_sequences",
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


# ── seeds, all of them thrown away ────────────────────────────────────────


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
                     "VALUES (1, 'Ledger Test City', FALSE) RETURNING id")
            )
        ).scalar_one()
    )


async def _seed_desk_user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Ledger Desk', :mobile, 'SUPER_ADMIN')"),
        {"uid": user_id, "mobile": await _free_mobile(session)},
    )
    await session.flush()
    return user_id


async def _seed_booking(
    session: AsyncSession,
    *,
    city_id: int,
    tag: str,
    estimated_amount: Decimal = Decimal("1000.00"),
) -> dict:
    """A running cab booking on its own master, no services of the owner's
    involved."""
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Ledger Customer', :mobile, 'CUSTOMER')"),
        {"uid": user_id, "mobile": mobile},
    )
    customer_id = int(
        (
            await session.execute(
                text("INSERT INTO customers (user_id, customer_code, first_name) "
                     "VALUES (:uid, :code, 'Ledger') RETURNING id"),
                {"uid": user_id, "code": f"IT-LEDG-C-{str(user_id)[:8]}"},
            )
        ).scalar_one()
    )
    master_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO master_bookings (uuid, booking_number, customer_id, "
                    "       city_id, booking_status, payment_status, total_amount, "
                    "       total_paid_amount, journey_start_date) "
                    "VALUES (:uuid, :num, :cust, :city, 'STARTED', 'PENDING', "
                    "        :total, 0.00, :start) RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "num": f"MB-LEDG-{tag}",
                    "cust": customer_id,
                    "city": city_id,
                    "total": estimated_amount,
                    "start": PICKUP.date(),
                },
            )
        ).scalar_one()
    )
    cab_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO cab_bookings (uuid, master_booking_id, "
                    "       booking_number, trip_type, vehicle_category_id, "
                    "       pickup_location, drop_location, pickup_datetime, "
                    "       estimated_distance, estimated_amount, booking_status, "
                    "       trip_start_km, trip_started_at, is_tax_invoice) "
                    "VALUES (:uuid, :mb, :num, 'ONE_WAY', :cat, 'Ledger Pickup', "
                    "        'Ledger Drop', :pickup, 50.00, :est, 'STARTED', 10.0, "
                    "        :started_at, FALSE) RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "mb": master_id,
                    "num": f"CAB-LEDG-{tag}",
                    "cat": CATEGORY,
                    "pickup": PICKUP,
                    "est": estimated_amount,
                    "started_at": TRIP_START,
                },
            )
        ).scalar_one()
    )
    await session.flush()
    return {
        "cab_id": cab_id,
        "master_id": master_id,
        "customer_id": customer_id,
        "booking_number": f"CAB-LEDG-{tag}",
    }


async def _close_fare(
    session: AsyncSession,
    cab_id: int,
    *,
    final_amount: Decimal,
    coupon_discount: Decimal = Decimal("0.00"),
    gst_amount: Decimal = Decimal("0.00"),
    is_tax_invoice: bool = False,
) -> None:
    """Set the closed fare the way a close leaves it, so the derive prices the
    master against `final_amount` rather than the quote."""
    await session.execute(
        text(
            "UPDATE cab_bookings SET booking_status = 'COMPLETED', "
            "       final_amount = :final, coupon_discount = :coupon, "
            "       gst_amount = :gst, is_tax_invoice = :taxi "
            " WHERE id = :id"
        ),
        {
            "final": final_amount,
            "coupon": coupon_discount,
            "gst": gst_amount,
            "taxi": is_tax_invoice,
            "id": cab_id,
        },
    )
    await session.flush()


async def _master_row(session: AsyncSession, master_id: int) -> dict:
    await session.flush()
    session.expire_all()
    return dict(
        (
            await session.execute(
                text("SELECT payment_status, total_paid_amount, total_amount "
                     "FROM master_bookings WHERE id = :m"),
                {"m": master_id},
            )
        ).mappings().one()
    )


async def _live_payments(session: AsyncSession, master_id: int) -> list[dict]:
    await session.flush()
    session.expire_all()
    return [
        dict(r)
        for r in (
            await session.execute(
                text("SELECT payment_type, payment_number, amount, refunded_amount, "
                     "       payment_status, voided_at, advance_payment_id, "
                     "       payment_method, received_by "
                     "FROM payments WHERE master_booking_id = :m ORDER BY id"),
                {"m": master_id},
            )
        ).mappings().all()
    ]


async def _take_advance(
    session: AsyncSession, seed: dict, *, amount: float, desk_user: UUID
) -> dict:
    return await create_advance(
        session,
        cab_booking_id=seed["cab_id"],
        amount=amount,
        payment_mode="CASH",
        received_by="ADMIN",
        user_id=str(desk_user),
        source_role="SUPER_ADMIN",
    )


# ── 1. an advance writes a ledger line and the master goes PARTIAL ─────────


def test_an_advance_taken_at_the_desk_lands_on_the_ledger() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="ADV1")
        await _take_advance(session, seed, amount=300.0, desk_user=desk_user)
        return {"payments": await _live_payments(session, seed["master_id"]),
                "master": await _master_row(session, seed["master_id"])}

    out = _with_session(work)
    payments, master = out["payments"], out["master"]

    assert len(payments) == 1, "one advance → one ledger line"
    row = payments[0]
    assert row["payment_type"] == "ADVANCE"
    assert row["payment_status"] == "SUCCESS"
    assert Decimal(str(row["amount"])) == Decimal("300.00")
    assert Decimal(str(row["refunded_amount"])) == Decimal("0.00")
    assert row["advance_payment_id"] is not None, "the line names its receipt"
    assert row["payment_number"].startswith("WT-PAY-")
    assert row["voided_at"] is None

    # Held ₹300 against a ₹1000 fare — PARTIAL because the money says so, not
    # because an endpoint assumed it.
    assert master["payment_status"] == "PARTIAL"
    assert Decimal(str(master["total_paid_amount"])) == Decimal("300.00")


# ── 2. the balance row picks its own type ──────────────────────────────────


def test_the_balance_is_a_full_payment_when_nothing_was_paid_in_front() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="FULL1")
        await _close_fare(session, seed["cab_id"], final_amount=Decimal("1000.00"))
        await payments_service.record_final_payment(
            session,
            master_booking_id=seed["master_id"],
            cab_booking_id=seed["cab_id"],
            amount=Decimal("1000.00"),
            payment_mode="CASH",
            received_by="PLATFORM",
            collected_by_user_id=str(desk_user),
            collected_by_role="ADMIN",
        )
        return await _live_payments(session, seed["master_id"])

    out = _with_session(work)
    assert len(out) == 1
    assert out[0]["payment_type"] == "FULL_PAYMENT"


def test_the_balance_is_a_balance_payment_once_an_advance_is_held() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="BAL1")
        await _take_advance(session, seed, amount=300.0, desk_user=desk_user)
        await _close_fare(session, seed["cab_id"], final_amount=Decimal("1000.00"))
        await payments_service.record_final_payment(
            session,
            master_booking_id=seed["master_id"],
            cab_booking_id=seed["cab_id"],
            amount=Decimal("700.00"),
            payment_mode="CASH",
            received_by="PLATFORM",
            collected_by_user_id=str(desk_user),
            collected_by_role="ADMIN",
        )
        return {"payments": await _live_payments(session, seed["master_id"]),
                "master": await _master_row(session, seed["master_id"])}

    out = _with_session(work)
    types = sorted(p["payment_type"] for p in out["payments"])
    assert types == ["ADVANCE", "BALANCE_PAYMENT"]
    # ₹300 advance + ₹700 balance covers the ₹1000 fare exactly.
    assert out["master"]["payment_status"] == "PAID"
    assert Decimal(str(out["master"]["total_paid_amount"])) == Decimal("1000.00")


def test_an_advance_covered_fare_records_no_balance_line() -> None:
    """A coupon-and-advance-paid-in-full trip moves no money at close, so there
    is no rupee to write: `ck_payments_amount_positive` (and common sense) refuse
    a ₹0.00 row. The state is still derived, and it reads PAID."""

    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="ZERO1")
        await _take_advance(session, seed, amount=400.0, desk_user=desk_user)
        await _close_fare(
            session,
            seed["cab_id"],
            final_amount=Decimal("500.00"),
            coupon_discount=Decimal("100.00"),
        )
        result = await payments_service.record_final_payment(
            session,
            master_booking_id=seed["master_id"],
            cab_booking_id=seed["cab_id"],
            amount=Decimal("0.00"),
            payment_mode="CASH",
            received_by="PLATFORM",
            collected_by_user_id=str(desk_user),
            collected_by_role="ADMIN",
        )
        await payments_service.derive_master_payment_status(session, seed["master_id"])
        return {"result": result,
                "payments": await _live_payments(session, seed["master_id"]),
                "master": await _master_row(session, seed["master_id"])}

    out = _with_session(work)
    assert out["result"] is None, "nothing moved, so nothing recorded"
    assert [p["payment_type"] for p in out["payments"]] == ["ADVANCE"]
    # ₹400 held against ₹500 − ₹100 coupon = ₹400 owed.
    assert out["master"]["payment_status"] == "PAID"


# ── 3. the derived state walks PENDING → PARTIAL → PAID ────────────────────


def test_the_master_state_is_arithmetic_over_the_ledger() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="WALK1")

        steps: dict[str, dict] = {}
        steps["none"] = await payments_service.derive_master_payment_status(
            session, seed["master_id"]
        )
        await _take_advance(session, seed, amount=400.0, desk_user=desk_user)
        steps["advance"] = await payments_service.derive_master_payment_status(
            session, seed["master_id"]
        )
        await _close_fare(session, seed["cab_id"], final_amount=Decimal("1000.00"))
        await payments_service.record_final_payment(
            session,
            master_booking_id=seed["master_id"],
            cab_booking_id=seed["cab_id"],
            amount=Decimal("600.00"),
            payment_mode="CASH",
            received_by="PLATFORM",
            collected_by_user_id=str(desk_user),
            collected_by_role="ADMIN",
        )
        steps["paid"] = await payments_service.derive_master_payment_status(
            session, seed["master_id"]
        )
        steps["master"] = await _master_row(session, seed["master_id"])
        return steps

    out = _with_session(work)
    assert out["none"]["payment_status"] == "PENDING"
    assert out["advance"]["payment_status"] == "PARTIAL"
    assert out["paid"]["payment_status"] == "PAID"
    assert out["master"]["payment_status"] == "PAID"
    assert Decimal(str(out["master"]["total_paid_amount"])) == Decimal("1000.00")


def test_a_tax_invoice_adds_gst_and_a_coupon_takes_the_discount_off() -> None:
    """The derive prices the master exactly the way the invoice PDF does: gross
    is the closed fare plus GST when it is a tax invoice, less this cab's coupon.
    ₹1000 + ₹100 − ₹50 = ₹1050 owed; ₹1040 held leaves it PARTIAL by ₹10."""

    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="GST1")
        await _close_fare(
            session,
            seed["cab_id"],
            final_amount=Decimal("1000.00"),
            coupon_discount=Decimal("50.00"),
            gst_amount=Decimal("100.00"),
            is_tax_invoice=True,
        )
        await payments_service.record_final_payment(
            session,
            master_booking_id=seed["master_id"],
            cab_booking_id=seed["cab_id"],
            amount=Decimal("1040.00"),
            payment_mode="CASH",
            received_by="PLATFORM",
            collected_by_user_id=str(desk_user),
            collected_by_role="ADMIN",
        )
        derived = await payments_service.derive_master_payment_status(
            session, seed["master_id"]
        )
        return derived

    out = _with_session(work)
    assert Decimal(str(out["payable"])) == Decimal("1050.00")
    assert out["payment_status"] == "PARTIAL"


# ── 4. refunds and voids are pulled out of the ledger ──────────────────────


def test_refunding_an_advance_marks_the_ledger_line_and_documents_the_money() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="RFD1")
        adv = await _take_advance(session, seed, amount=300.0, desk_user=desk_user)

        # Partial first: ₹100 back.
        await initiate_refund(
            InitiateRefundIn(
                advance_payment_id=adv["id"],
                reason="Customer asked for part back",
                refund_amount=100.0,
            ),
            db=session,
            current_user={"sub": str(desk_user)},
        )
        after_part = await _live_payments(session, seed["master_id"])

        # Then the rest: ₹200 back — the advance is now fully refunded.
        await initiate_refund(
            InitiateRefundIn(
                advance_payment_id=adv["id"],
                reason="Customer asked for the rest back",
                refund_amount=200.0,
            ),
            db=session,
            current_user={"sub": str(desk_user)},
        )
        after_full = await _live_payments(session, seed["master_id"])
        payment_id = (
            await session.execute(
                text("SELECT id FROM payments WHERE advance_payment_id = :a"),
                {"a": adv["id"]},
            )
        ).scalar_one()
        refunds = [
            dict(r)
            for r in (
                await session.execute(
                    text("SELECT refund_number, refund_amount, refund_status "
                         "FROM refunds WHERE payment_id = :p ORDER BY id"),
                    {"p": payment_id},
                )
            ).mappings().all()
        ]
        master = await _master_row(session, seed["master_id"])
        return {"part": after_part[0], "full": after_full[0],
                "refunds": refunds, "master": master}

    out = _with_session(work)
    part, full = out["part"], out["full"]

    assert Decimal(str(part["refunded_amount"])) == Decimal("100.00")
    assert part["payment_status"] == "PARTIAL_REFUNDED"
    assert Decimal(str(full["refunded_amount"])) == Decimal("300.00")
    assert full["payment_status"] == "REFUNDED"

    assert len(out["refunds"]) == 2
    assert sum(Decimal(str(r["refund_amount"])) for r in out["refunds"]) == (
        Decimal("300.00")
    )
    assert all(r["refund_number"].startswith("RF-WT-PAY-") for r in out["refunds"])

    # Every rupee came back — the ledger holds nothing, so the master falls to
    # PENDING rather than staying frozen at PARTIAL.
    assert Decimal(str(out["master"]["total_paid_amount"])) == Decimal("0.00")
    assert out["master"]["payment_status"] == "PENDING"


def test_a_voided_receipt_leaves_the_books_as_if_it_never_counted() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        desk_user = await _seed_desk_user(session)
        seed = await _seed_booking(session, city_id=city_id, tag="VOID1")
        await _take_advance(session, seed, amount=300.0, desk_user=desk_user)
        await void_advance(
            session,
            cab_booking_id=seed["cab_id"],
            user_id=str(desk_user),
            reason="Recorded against the wrong booking",
        )
        return {"payments": await _live_payments(session, seed["master_id"]),
                "master": await _master_row(session, seed["master_id"])}

    out = _with_session(work)
    payments = out["payments"]
    assert len(payments) == 1
    assert payments[0]["voided_at"] is not None, "voided, not deleted"
    # The derive drops voided rows: held 0 → PENDING, paid 0.
    assert out["master"]["payment_status"] == "PENDING"
    assert Decimal(str(out["master"]["total_paid_amount"])) == Decimal("0.00")


# ── 5. a mixed master is left to its own rollup ────────────────────────────


def test_the_derive_never_touches_a_master_that_carries_a_hotel_line() -> None:
    async def work(session: AsyncSession) -> dict:
        city_id = await _seed_city(session)
        seed = await _seed_booking(session, city_id=city_id, tag="MIX1")
        await session.execute(
            text("INSERT INTO booking_services (master_booking_id, service_type, "
                 "       service_reference_id, service_amount, created_at) "
                 "VALUES (:m, 'HOTEL', 1, 5000.00, NOW())"),
            {"m": seed["master_id"]},
        )
        await session.flush()
        before = await _master_row(session, seed["master_id"])
        derived = await payments_service.derive_master_payment_status(
            session, seed["master_id"]
        )
        after = await _master_row(session, seed["master_id"])
        return {"before": before, "derived": derived, "after": after}

    out = _with_session(work)
    assert out["derived"] is None, "a hotel line owns this master's payment state"
    assert out["before"] == out["after"], "the columns are not rewritten"


# ── 6. nothing survives the transaction ────────────────────────────────────


def test_nothing_survives_the_transaction(_row_counts_before_any_test: dict) -> None:
    after = {t: _count(t) for t in TRADED_TABLES}
    changed = {
        t: (before, after[t])
        for t, before in _row_counts_before_any_test.items()
        if before != after[t]
    }
    assert not changed, f"rows outlived the rollback: {changed}"
