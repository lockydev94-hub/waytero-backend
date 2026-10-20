# ============================================================
# WAYTERO — ROUND 15 C4a: THE CLOSE IS THE MONEY
# File: tests/integration/test_cab_trip_close_money.py
#
# What this proves, in the owner's words: "here not have adjust any commission
# amount" and "here not have guess ideas implements". The trip close is the one
# moment a cab booking's money is decided, and it used to be decided four times
# over — twice in the admin desk, once in its commission endpoint, once on the
# partner's own close — each written by hand, each in floats. This file runs the
# live handlers against a real database and asserts the number that lands on the
# row, because a unit test of the shared function cannot show that a whole
# endpoint stopped reading its own copy.
#
#   1. A city configured with a FLAT rule is charged that flat amount. Both
#      desks. This is the drift: a flat city was charged the platform default
#      10% on the partner's close and the configured rule on the admin's.
#   2. The coupon on the invoice and the balance is this cab's coupon, not the
#      sum of every coupon the master booking carries.
#   3. A refund taken out of the advance is not counted as money still held.
#   4. Cancelling a booked, invoiced trip voids that invoice, and all three
#      download routes refuse to hand out a voided tax document.
#
# Everything is seeded into its own city with its own commission group, and the
# whole run happens inside a transaction that is always rolled back. The owner's
# real commission rules, partners and bookings are never read or touched — the
# last test is that the tables hold the row counts they started with.
# ============================================================
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.branch_scope import BranchScope
from app.core.config import settings
from app.modules.admin.trip_assistance_api import (
    TripCloseRequest,
    admin_close_trip,
    download_invoice_pdf as admin_download_invoice_pdf,
)
from app.modules.booking.services.cancellation_policy import apply_cab_cancellation
from app.modules.booking.services.fare import calculate_fare, trip_days_from
from app.modules.booking.services.settlement_math import money
from app.modules.partner.booking_api import (
    PartnerCollectPaymentRequest,
    PartnerTripCloseRequest,
    partner_close_trip,
    partner_collect_payment,
    partner_download_invoice,
)

CATEGORY = 2  # SEDAN — a real vehicle category, so the fare engine answers
FLAT_COMMISSION = Decimal("900.00")
PICKUP = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
TRIP_START = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
TRIP_END = datetime(2026, 11, 20, 16, 30, tzinfo=timezone.utc)
START_KM = Decimal("10.0")
END_KM = Decimal("60.0")

SUPER_SCOPE = BranchScope(
    actor_id="00000000-0000-0000-0000-000000000000",
    user_type="SUPER_ADMIN",
    is_super=True,
    branch_id=None,
    branch_name=None,
    is_headquarters=False,
    cities=frozenset(),
    include_unassigned=True,
)

TRADED_TABLES = (
    "cities",
    "users",
    "customers",
    "master_bookings",
    "cab_bookings",
    "cab_booking_assignments",
    "booking_timelines",
    "partners",
    "partner_services",
    "commission_groups",
    "commission_rules",
    "coupons",
    "coupon_usages",
    "advance_payments",
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
    """Run `work(session)` on a private engine inside a transaction that is
    always rolled back.

    `join_transaction_mode="create_savepoint"` is what lets the partner's own
    `db.commit()` (collect-payment commits to close its duplicate-payment race)
    run for real here: it commits a savepoint, and the outer rollback still
    takes everything back.
    """

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
    """Its own city, so no rule or fare sheet of the owner's can answer here.

    Commission resolution is city → platform-wide → default, so a test that ran
    in a real city would pass on whatever the super admin happened to configure.
    """
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO cities (state_id, name, is_active) "
                    "VALUES (1, 'Close Money Test City', FALSE) RETURNING id"
                )
            )
        ).scalar_one()
    )


async def _seed_flat_rule(session: AsyncSession, city_id: int) -> int:
    """A FLAT rule on its own commission group — nothing else in force for it."""
    group_id = int(
        (
            await session.execute(
                text("INSERT INTO commission_groups (group_name) "
                     "VALUES (:n) RETURNING id"),
                {"n": f"_wt_close_{uuid4().hex[:12]}"},
            )
        ).scalar_one()
    )
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO commission_rules "
                    "(commission_group_id, service_type, commission_type, "
                    " commission_value, city_id, is_active) "
                    "VALUES (:g, 'CAB', 'FLAT', :v, :c, TRUE) RETURNING id"
                ),
                {"g": group_id, "v": FLAT_COMMISSION, "c": city_id},
            )
        ).scalar_one()
    )


async def _seed_desk_user(session: AsyncSession) -> UUID:
    """The booking was cancelled by a real desk login, so the audit column is a
    user that exists."""
    user_id = uuid4()
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Close Money Desk', :mobile, 'SUPER_ADMIN')"),
        {"uid": user_id, "mobile": await _free_mobile(session)},
    )
    await session.flush()
    return user_id


async def _seed_partner(session: AsyncSession, city_id: int) -> tuple[int, str]:
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Close Money Partner', :mobile, 'PARTNER')"),
        {"uid": user_id, "mobile": mobile},
    )
    partner_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO partners (user_id, partner_code, partner_type, "
                    "       owner_name, mobile, city_id, status, business_name) "
                    "VALUES (:uid, :code, 'INDIVIDUAL', 'Close Money Cabs', :mobile, "
                    "        :city, 'ACTIVE', 'Close Money Cabs') RETURNING id"
                ),
                {
                    "uid": user_id,
                    "code": f"IT-CLOSE-{str(user_id)[:8]}",
                    "mobile": mobile,
                    "city": city_id,
                },
            )
        ).scalar_one()
    )
    await session.execute(
        text("INSERT INTO partner_services (partner_id, service_type) "
             "VALUES (:pid, 'CAB')"),
        {"pid": partner_id},
    )
    return partner_id, str(user_id)


async def _seed_started_booking(
    session: AsyncSession,
    *,
    city_id: int,
    partner_id: int,
    tag: str,
    coupon_discount: Decimal | None = None,
) -> dict:
    """A running trip, assigned to this partner, that nobody has closed."""
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text("INSERT INTO users (id, first_name, mobile_number, user_type) "
             "VALUES (:uid, 'Close Money Customer', :mobile, 'CUSTOMER')"),
        {"uid": user_id, "mobile": mobile},
    )
    customer_id = int(
        (
            await session.execute(
                text("INSERT INTO customers (user_id, customer_code, first_name) "
                     "VALUES (:uid, :code, 'Close Money') RETURNING id"),
                {"uid": user_id, "code": f"IT-CLOSE-C-{str(user_id)[:8]}"},
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
                    "VALUES (:uuid, :num, :cust, :city, 'STARTED', 'PENDING', "
                    "        1000.00, :start) RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "num": f"MB-CLOSE-{tag}",
                    "cust": customer_id,
                    "city": city_id,
                    "start": PICKUP.date(),
                },
            )
        ).scalar_one()
    )
    params = {
        "uuid": uuid4(),
        "mb": master_id,
        "num": f"CAB-CLOSE-{tag}",
        "cat": CATEGORY,
        "pickup": PICKUP,
        "start_km": START_KM,
        "started_at": TRIP_START,
    }
    coupon_clause = ""
    if coupon_discount is not None:
        coupon_clause = ", coupon_discount"
        params["coupon"] = coupon_discount
    cab_id = int(
        (
            await session.execute(
                text(
                    f"INSERT INTO cab_bookings (uuid, master_booking_id, "
                    f"       booking_number, trip_type, vehicle_category_id, "
                    f"       pickup_location, drop_location, pickup_datetime, "
                    f"       estimated_distance, estimated_amount, booking_status, "
                    f"       trip_start_km, trip_started_at{coupon_clause}) "
                    f"VALUES (:uuid, :mb, :num, 'ONE_WAY', :cat, "
                    f"        'Close Pickup', 'Close Drop', :pickup, 50.00, 1000.00, "
                    f"        'STARTED', :start_km, :started_at"
                    + (", :coupon)" if coupon_clause else ")")
                    + " RETURNING id"
                ),
                params,
            )
        ).scalar_one()
    )
    await session.execute(
        text(
            "INSERT INTO cab_booking_assignments (cab_booking_id, partner_id, "
            "       assignment_type, assigned_at, accepted_at) "
            "VALUES (:cb, :pid, 'MANUAL', NOW(), NOW())"
        ),
        {"cb": cab_id, "pid": partner_id},
    )
    await session.flush()
    return {
        "cab_id": cab_id,
        "master_id": master_id,
        "customer_id": customer_id,
        "booking_number": f"CAB-CLOSE-{tag}",
    }


async def _expected_fare(session: AsyncSession, city_id: int) -> Decimal:
    """What the platform's own fare engine says this trip costs.

    The close refuses a final amount more than 50% away from that number, so the
    tests price with the real figure rather than a literal that would break the
    day the super admin edits the default sheet.
    """
    from app.modules.admin.services import DefaultPricingService

    effective = await DefaultPricingService.get_effective_rule(
        session, city_id, CATEGORY, "ONE_WAY"
    )
    assert effective is not None, "the default fare sheet should answer"
    return calculate_fare(
        effective,
        END_KM - START_KM,
        start_dt=TRIP_START,
        end_dt=TRIP_END,
        trip_type="ONE_WAY",
        trip_days=trip_days_from(PICKUP, None),
    )


async def _cab_row(session: AsyncSession, cab_id: int) -> dict:
    # The close writes the money columns through the ORM and the request session
    # commits them at the end of the call; flush so this raw read sees them.
    await session.flush()
    session.expire_all()
    return dict(
        (
            await session.execute(
                text(
                    "SELECT booking_status, final_amount, platform_commission, "
                    "       partner_payout, gst_rate, gst_amount, "
                    "       is_tax_invoice, commission_config_snapshot, "
                    "       invoice_number, invoice_voided_at, invoice_void_reason, "
                    "       payment_mode, coupon_discount, actual_distance "
                    "FROM cab_bookings WHERE id = :id"
                ),
                {"id": cab_id},
            )
        ).mappings().one()
    )


async def _timelines(session: AsyncSession, master_id: int) -> list[dict]:
    return [
        dict(r)
        for r in (
            await session.execute(
                text("SELECT event_type, event_description FROM booking_timelines "
                     "WHERE master_booking_id = :m ORDER BY id"),
                {"m": master_id},
            )
        ).mappings().all()
    ]


async def _gst_off(session: AsyncSession) -> None:
    """A tax-free booking, so the assertions are about commission alone.

    Updated, not inserted: the super admin's own setting is what the close reads,
    and the tests below say which one they assume.
    """
    await session.execute(
        text("UPDATE system_configurations SET config_value = 'false' "
             "WHERE config_key = 'GST_ENABLED'")
    )
    await session.flush()


# ── 1. the flat rule, on both desks ───────────────────────────────────────


def test_the_desk_charges_the_flat_rule_configured_for_that_city() -> None:
    async def work(session: AsyncSession) -> dict:
        await _gst_off(session)
        city_id = await _seed_city(session)
        rule_id = await _seed_flat_rule(session, city_id)
        partner_id, _ = await _seed_partner(session, city_id)
        seed = await _seed_started_booking(
            session, city_id=city_id, partner_id=partner_id, tag="ADM1"
        )
        fare = await _expected_fare(session, city_id)

        result = await admin_close_trip(
            TripCloseRequest(
                booking_number=seed["booking_number"],
                end_km=END_KM,
                end_datetime=TRIP_END,
                final_amount=fare,
            ),
            db=session,
            scope=SUPER_SCOPE,
        )
        row = await _cab_row(session, seed["cab_id"])
        events = await _timelines(session, seed["master_id"])
        return {
            "result": result,
            "row": row,
            "fare": fare,
            "rule_id": rule_id,
            "events": events,
        }

    out = _with_session(work)
    row, result = out["row"], out["result"]

    assert result["commission_type"] == "FLAT"
    assert money(row["platform_commission"]) == FLAT_COMMISSION
    # The invariant the owner stated: the split is the fare, with nothing
    # adjusted. A 10% charge on a fare over ₹900 would fail both halves.
    assert money(row["platform_commission"]) + money(row["partner_payout"]) == money(
        row["final_amount"]
    )
    assert money(row["final_amount"]) == out["fare"]

    snap = row["commission_config_snapshot"]
    assert snap["commission_type"] == "FLAT"
    assert Decimal(str(snap["commission_value"])) == FLAT_COMMISSION
    assert snap["commission_rule_id"] == out["rule_id"]
    assert snap["commission_source"] == "city"

    close_event = next(
        e for e in out["events"] if e["event_type"] == "TRIP_CLOSED_BY_ADMIN"
    )
    assert "FLAT 900.00" in close_event["event_description"]


def test_the_partner_closing_the_same_trip_gets_the_same_numbers() -> None:
    """The drift this round exists to kill.

    Two bookings, one city, one flat rule, identical trips: one closed by the
    admin desk, one by the partner's own portal. Before the shared close they
    disagreed — the partner side read only PERCENTAGE rules and charged the
    platform default 10%, so a flat-rate city paid out a different amount
    depending on who typed the odometer reading.
    """

    async def work(session: AsyncSession) -> dict:
        await _gst_off(session)
        city_id = await _seed_city(session)
        await _seed_flat_rule(session, city_id)
        partner_id, partner_user = await _seed_partner(session, city_id)
        admin_trip = await _seed_started_booking(
            session, city_id=city_id, partner_id=partner_id, tag="ADM2"
        )
        partner_trip = await _seed_started_booking(
            session, city_id=city_id, partner_id=partner_id, tag="PRT2"
        )
        fare = await _expected_fare(session, city_id)

        await admin_close_trip(
            TripCloseRequest(
                booking_number=admin_trip["booking_number"],
                end_km=END_KM,
                end_datetime=TRIP_END,
                final_amount=fare,
            ),
            db=session,
            scope=SUPER_SCOPE,
        )
        result = await partner_close_trip(
            partner_trip["booking_number"],
            PartnerTripCloseRequest(
                end_km=END_KM, end_datetime=TRIP_END, final_amount=fare
            ),
            db=session,
            current_user={"sub": partner_user},
        )
        return {
            "admin": await _cab_row(session, admin_trip["cab_id"]),
            "partner": await _cab_row(session, partner_trip["cab_id"]),
            "result": result,
        }

    out = _with_session(work)
    admin, partner = out["admin"], out["partner"]

    assert money(partner["platform_commission"]) == FLAT_COMMISSION
    assert money(partner["platform_commission"]) == money(admin["platform_commission"])
    assert money(partner["partner_payout"]) == money(admin["partner_payout"])
    assert partner["commission_config_snapshot"]["commission_rule_id"] == (
        admin["commission_config_snapshot"]["commission_rule_id"]
    )
    assert out["result"]["commission_type"] == "FLAT"
    # commission + payout is still the whole fare on the partner's side too.
    assert money(partner["platform_commission"]) + money(partner["partner_payout"]) == (
        money(partner["final_amount"])
    )


# ── 2. the coupon is this cab's, not the master's ─────────────────────────


def test_another_service_s_coupon_does_not_discount_this_cab() -> None:
    """A ₹500 hotel redemption hanging off the same master booking must not
    take ₹500 off a cab fare the customer never got a coupon on. The close, the
    desk detail and the invoice all used to read SUM(coupon_usages by master)
    instead of the cab's own column — the figure settlement validates against the
    immutable usage row."""

    async def work(session: AsyncSession) -> dict:
        await _gst_off(session)
        city_id = await _seed_city(session)
        await _seed_flat_rule(session, city_id)
        partner_id, partner_user = await _seed_partner(session, city_id)
        seed = await _seed_started_booking(
            session,
            city_id=city_id,
            partner_id=partner_id,
            tag="CPN1",
            coupon_discount=Decimal("50.00"),
        )
        fare = await _expected_fare(session, city_id)

        coupon_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO coupons (coupon_code, title, apply_to, "
                        "       discount_type, discount_value, valid_from, valid_to, "
                        "       coupon_type) "
                        "VALUES (:code, 'Hotel-only coupon test', 'HOTEL', 'FLAT', "
                        "        500.00, :vf, :vt, 'PLATFORM') RETURNING id"
                    ),
                    {
                        "code": f"ITCLOSE{uuid4().hex[:8].upper()}",
                        "vf": (TRIP_END - timedelta(days=30)).date(),
                        "vt": (TRIP_END + timedelta(days=30)).date(),
                    },
                )
            ).scalar_one()
        )
        await session.execute(
            text(
                "INSERT INTO coupon_usages (coupon_id, customer_id, "
                "       master_booking_id, discount_applied, coupon_type, "
                "       platform_share, partner_share, eligible_partner_ids) "
                "VALUES (:c, :cust, :mb, 500.00, 'PLATFORM', 500.00, 0.00, '[]'::jsonb)"
            ),
            {
                "c": coupon_id,
                "cust": seed["customer_id"],
                "mb": seed["master_id"],
            },
        )
        await session.flush()

        result = await partner_close_trip(
            seed["booking_number"],
            PartnerTripCloseRequest(
                end_km=END_KM, end_datetime=TRIP_END, final_amount=fare
            ),
            db=session,
            current_user={"sub": partner_user},
        )
        return {"result": result, "fare": fare}

    out = _with_session(work)
    result = out["result"]

    assert result["coupon_discount"] == 50.0
    # Balance is the fare this booking was closed at, less this booking's coupon,
    # less the advance nobody took. A ₹550 total discount would fail this.
    assert Decimal(str(result["balance_due"])) == (
        money(out["fare"]) - Decimal("50.00")
    )


# ── 3. a refunded part of the advance is not still held ───────────────────


def test_a_refunded_advance_is_netted_out_of_the_balance() -> None:
    """A partially refunded advance keeps its ACTIVE row and accumulates
    `refunded_amount` (admin/payment_api.refund_advance). Money the customer got
    back is not money still sitting against the fare — the customer's own invoice
    already netted it, which is how the two portals came to show different
    amounts due on one booking."""

    async def work(session: AsyncSession) -> dict:
        await _gst_off(session)
        city_id = await _seed_city(session)
        await _seed_flat_rule(session, city_id)
        partner_id, partner_user = await _seed_partner(session, city_id)
        seed = await _seed_started_booking(
            session, city_id=city_id, partner_id=partner_id, tag="ADV1"
        )
        desk_user = await _seed_desk_user(session)
        fare = await _expected_fare(session, city_id)

        await session.execute(
            text(
                "INSERT INTO advance_payments (cab_booking_id, "
                "       master_booking_id, booking_number, receipt_number, amount, "
                "       refunded_amount, payment_mode, received_by, partner_id, "
                "       status, collected_by_user_id, collected_by_role, "
                "       collected_at) "
                "VALUES (:cb, :mb, :num, :rec, 300.00, 100.00, 'CASH', "
                "        'PLATFORM', :pid, 'ACTIVE', :cby, 'SUPER_ADMIN', NOW())"
            ),
            {
                "cb": seed["cab_id"],
                "mb": seed["master_id"],
                "num": seed["booking_number"],
                "rec": f"IT-ADV-{uuid4().hex[:10].upper()}",
                "pid": partner_id,
                "cby": desk_user,
            },
        )
        await session.flush()

        result = await partner_close_trip(
            seed["booking_number"],
            PartnerTripCloseRequest(
                end_km=END_KM, end_datetime=TRIP_END, final_amount=fare
            ),
            db=session,
            current_user={"sub": partner_user},
        )
        return {"result": result, "fare": fare}

    out = _with_session(work)
    result = out["result"]

    assert result["advance_paid"] == 200.0
    assert Decimal(str(result["balance_due"])) == (
        money(out["fare"]) - Decimal("200.00")
    )


# ── 4. a cancelled trip voids its invoice ─────────────────────────────────


def test_cancelling_after_the_invoice_voids_it_and_every_download_refuses() -> None:
    """Close → collect → invoice → cancel, the way a real no-show dispute ends.

    The tax document dies with the booking: `invoice_voided_at` and a reason are
    written once (migration 0086 forbids one without the other), the number
    itself is kept so finance can name it, and all three download routes refuse
    it. A second cancellation cannot re-write the reason.
    """

    async def work(session: AsyncSession) -> dict:
        await _gst_off(session)
        city_id = await _seed_city(session)
        await _seed_flat_rule(session, city_id)
        partner_id, partner_user = await _seed_partner(session, city_id)
        seed = await _seed_started_booking(
            session, city_id=city_id, partner_id=partner_id, tag="VOI1"
        )
        desk_user = await _seed_desk_user(session)
        fare = await _expected_fare(session, city_id)

        await partner_close_trip(
            seed["booking_number"],
            PartnerTripCloseRequest(
                end_km=END_KM, end_datetime=TRIP_END, final_amount=fare
            ),
            db=session,
            current_user={"sub": partner_user},
        )
        collected = await partner_collect_payment(
            seed["booking_number"],
            PartnerCollectPaymentRequest(
                payment_mode="CASH", payment_collected_by="DRIVER"
            ),
            db=session,
            current_user={"sub": partner_user},
        )
        invoice_number = collected["invoice_number"]
        assert invoice_number, "payment collection issues the invoice"

        cancelled = await apply_cab_cancellation(
            session,
            cab_booking_id=seed["cab_id"],
            master_booking_id=seed["master_id"],
            cancellation_reason="Customer did not show; fare already invoiced",
            cancelled_by_user_id=desk_user,
            cancelled_by_role="ADMIN",
            cancelled_source="ADMIN_CANCEL",
        )
        row_after_first = await _cab_row(session, seed["cab_id"])

        # The same booking cancelled again — the reason must not be rewritten.
        await apply_cab_cancellation(
            session,
            cab_booking_id=seed["cab_id"],
            master_booking_id=seed["master_id"],
            cancellation_reason="Second cancellation attempt",
            cancelled_by_user_id=desk_user,
            cancelled_by_role="ADMIN",
            cancelled_source="ADMIN_CANCEL",
        )
        row_after_second = await _cab_row(session, seed["cab_id"])

        refused: dict[str, str] = {}
        for label, coro in (
            (
                "partner",
                partner_download_invoice(
                    seed["booking_number"],
                    db=session,
                    current_user={"sub": partner_user},
                ),
            ),
            (
                "admin",
                admin_download_invoice_pdf(
                    booking_number=seed["booking_number"],
                    db=session,
                    scope=SUPER_SCOPE,
                ),
            ),
        ):
            try:
                await coro
                refused[label] = "returned a PDF"
            except HTTPException as exc:
                refused[label] = f"{exc.status_code}: {exc.detail}"

        events = await _timelines(session, seed["master_id"])
        return {
            "invoice_number": invoice_number,
            "first": row_after_first,
            "second": row_after_second,
            "refused": refused,
            "cancelled": cancelled,
            "events": events,
        }

    out = _with_session(work)
    first, second = out["first"], out["second"]

    assert out["cancelled"]["invoice_voided"] is True
    assert out["cancelled"]["invoice_number"] == out["invoice_number"]
    assert first["invoice_voided_at"] is not None
    assert first["invoice_number"] == out["invoice_number"], (
        "a voided invoice keeps its number"
    )
    assert "Customer did not show" in first["invoice_void_reason"]

    assert second["invoice_voided_at"] == first["invoice_voided_at"]
    assert second["invoice_void_reason"] == first["invoice_void_reason"]

    for label, message in out["refused"].items():
        assert message.startswith("400:"), f"{label} served {message}"
        assert "voided" in message, f"{label} did not say why: {message}"

    assert any(
        e["event_type"] == "CAB_CANCELLED" and "voided" in e["event_description"]
        for e in out["events"]
    )


def test_nothing_survives_the_transaction(_row_counts_before_any_test: dict) -> None:
    after = {t: _count(t) for t in TRADED_TABLES}
    changed = {
        t: (before, after[t])
        for t, before in _row_counts_before_any_test.items()
        if before != after[t]
    }
    assert not changed, f"rows outlived the rollback: {changed}"
