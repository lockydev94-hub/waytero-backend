"""Round 15 C2 — the kilometres and the fare are the server's, not the caller's.

Two claims get proved here, and neither is a UI claim:

1. Nothing a caller sends can move a cab total. The desk's payload no longer has
   a distance or an amount field at all, the city that prices the trip is the
   branch's own rather than one the agent names, and the corridor trips the
   website books are measured by the server (published `cab_routes` distance, then
   Google), so the number that used to come from the browser is now only a fallback
   for a hire with no destination to measure — and that fallback is labelled on the
   row.
2. A fare sheet is read as of the day the trip happens. `effective_from` /
   `effective_to` have existed since the earliest migration and were never
   consulted, so a superseded rate kept pricing bookings and a next-month trip was
   quoted at today's number.

The API half runs over the app's own routes with one client fixture per test, the
way the rest of `tests/api` does. The service half runs on a private engine inside
a transaction that is always rolled back, because proving the date filter needs
fare sheets with dates on them, and the owner's database must not keep a test's
master data. The one thing that never happens here is a Google call: every trip in
this file is either matched to a configured corridor or refused before measuring.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.branch_scope import BranchScope
from app.core.config import settings
from app.core.exceptions import BusinessException, ValidationException
from app.modules.admin.customer_care_api import (
    CabBookingCreateByAdmin,
    CabBookingQuoteRequest,
    _desk_pricing_city,
)
from app.modules.admin.services import DefaultPricingService
from app.modules.booking.services.trip_pricing import fare_columns, quote_cab_trip

API = "/api/v1"
QUOTE_URL = f"{API}/admin/customer-care/cab-booking/quote"

# Every persisted money line, so a test can add them up rather than trust one.
LINE_KEYS = (
    "base_fare",
    "distance_charge",
    "driver_allowance",
    "night_charge",
    "toll",
    "waiting",
)


def _dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _fetch(sql: str, *args: Any) -> list[dict]:
    """Read one fact from the database, on a throwaway loop and a pooled-free
    connection — the same reason `test_branch_scope_api.py` does it this way:
    TestClient owns the app engine's loop for the duration of a test."""
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_dsn())
        try:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    return asyncio.run(run())


def _corridor() -> dict | None:
    """A published corridor the distance authority can match — the pair with a
    city on each end and a distance the admin already set."""
    rows = _fetch(
        """
        SELECT id, origin_city_id, destination_city_id, distance_km, pricing_city_id
        FROM cab_routes
        WHERE is_active IS TRUE
          AND origin_city_id IS NOT NULL
          AND destination_city_id IS NOT NULL
          AND origin_city_id <> destination_city_id
          AND distance_km IS NOT NULL
        ORDER BY id
        LIMIT 1
        """
    )
    return rows[0] if rows else None


def _sheet_for(city_id: int) -> dict:
    """A cab category and trip type this city actually has a fare sheet for."""
    today = date.today()
    rows = _fetch(
        """
        SELECT vehicle_category_id, trip_type
        FROM vehicle_pricing_rules
        WHERE city_id = $1
          AND (effective_from IS NULL OR effective_from <= $2)
          AND (effective_to IS NULL OR effective_to >= $2)
        ORDER BY id
        LIMIT 1
        """,
        city_id,
        today,
    )
    if not rows:
        pytest.skip(f"city {city_id} has no fare sheet in force today")
    return rows[0]


def _with_session(work: Callable[[AsyncSession], Any]):
    """Run `work(session)` on a private engine inside a transaction that is always
    rolled back. A fare sheet with dates on it is master data; the owner's database
    must not keep one because a test wanted to read it."""

    async def run():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                trans = await conn.begin()
                session = AsyncSession(
                    bind=conn,
                    expire_on_commit=False,
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


async def _insert_sheet(
    session: AsyncSession,
    city_id: int,
    vehicle_category_id: int,
    trip_type: str,
    base_fare: str,
    effective_from: date | None,
    effective_to: date | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO vehicle_pricing_rules (
                city_id, vehicle_category_id, trip_type, base_fare, minimum_km,
                per_km_rate, driver_allowance, night_charge,
                effective_from, effective_to
            ) VALUES (
                :city_id, :vcid, :trip_type, :base_fare, 5,
                10, 0, 0,
                :effective_from, :effective_to
            )
        """
        ),
        {
            "city_id": city_id,
            "vcid": vehicle_category_id,
            "trip_type": trip_type,
            "base_fare": Decimal(base_fare),
            "effective_from": effective_from,
            "effective_to": effective_to,
        },
    )
    await session.flush()


# ── 1. The desk's payload cannot carry a number ───────────────────────────


def test_the_desk_payload_has_no_distance_and_no_amount():
    """The tamper surface is gone at the schema level, not by convention.

    `estimated_distance` used to be a field the agent typed and the fare engine
    then faithfully priced — a booking whose total was arithmetically correct and
    factually wrong. There is nothing left to forge.
    """
    for model in (CabBookingCreateByAdmin, CabBookingQuoteRequest):
        assert "estimated_distance" not in model.model_fields, model.__name__
        assert "estimated_amount" not in model.model_fields, model.__name__


def test_the_quote_prices_the_corridor_whatever_the_caller_sends(client_super_admin):
    corridor = _corridor()
    if corridor is None:
        pytest.skip("no active corridor with a city pair and a distance to match")
    sheet = _sheet_for(corridor["pricing_city_id"])
    body = {
        "city_id": corridor["origin_city_id"],
        "destination_city_id": corridor["destination_city_id"],
        "vehicle_category_id": sheet["vehicle_category_id"],
        "trip_type": sheet["trip_type"],
        "pickup_location": "Pickup place",
        "drop_location": "Drop place",
        "pickup_datetime": "2026-11-05T10:00:00+05:30",
    }

    honest = client_super_admin.post(QUOTE_URL, json=body)
    assert honest.status_code == 200, honest.text
    quoted = honest.json()

    forged = client_super_admin.post(
        QUOTE_URL,
        json={**body, "estimated_distance_km": 1, "billable_km": 0, "total": 1},
    )
    assert forged.status_code == 200, forged.text
    forged_json = forged.json()

    assert quoted["distance_source"] == "CONFIGURED_ROUTE"
    assert Decimal(quoted["distance_km"]) == Decimal(str(corridor["distance_km"]))
    # A number sent alongside a schema that has no field for it changes nothing.
    assert Decimal(forged_json["total"]) == Decimal(quoted["total"])
    assert forged_json["distance_km"] == quoted["distance_km"]


def test_the_quote_lines_add_up_to_the_quote_total(client_super_admin):
    """The agent reads these lines to a customer on the phone, so they have to be
    the total, not a decoration next to it."""
    corridor = _corridor()
    if corridor is None:
        pytest.skip("no active corridor with a city pair and a distance to match")
    sheet = _sheet_for(corridor["pricing_city_id"])
    r = client_super_admin.post(
        QUOTE_URL,
        json={
            "city_id": corridor["origin_city_id"],
            "destination_city_id": corridor["destination_city_id"],
            "vehicle_category_id": sheet["vehicle_category_id"],
            "trip_type": sheet["trip_type"],
            "pickup_location": "Pickup place",
            "drop_location": "Drop place",
            "pickup_datetime": "2026-11-05T10:00:00+05:30",
        },
    )
    assert r.status_code == 200, r.text
    q = r.json()
    added_up = sum((Decimal(q[k]) for k in LINE_KEYS), Decimal("0"))
    assert added_up == Decimal(q["total"]), (added_up, q)
    # Money leaves as Decimal-strings, so a JSON float cannot round a fare.
    assert isinstance(q["total"], str)


def _desk_body(city_id: int | None) -> CabBookingQuoteRequest:
    return CabBookingQuoteRequest(
        city_id=city_id,
        vehicle_category_id=1,
        trip_type="OUTSTATION",
        pickup_location="Cuttack Barabati Stadium",
        drop_location="Puri Grand Road",
        pickup_datetime=datetime(2026, 11, 5, 10, 0),
    )


class _NoQueryDb:
    """A session that fails the test if the code looks anything up."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("nothing should be queried to answer this")


def _branch_scope(city_id: int) -> BranchScope:
    return BranchScope(
        actor_id="00000000-0000-0000-0000-000000000000",
        user_type="CCO",
        is_super=False,
        branch_id=2,
        branch_name="Cuttack",
        is_headquarters=False,
        cities=frozenset({city_id}),
        include_unassigned=False,
    )


def test_a_branch_desk_prices_on_its_own_city_however_it_names_one():
    """`city_id` is the other forgeable field on this payload.

    A fare sheet belongs to a city, so a Cuttack agent who could name
    Bhubaneswar would price a Cuttack trip on another office's rates — the same
    defect as a hand-typed distance, one field over. The branch's city comes from
    its own scope, and answering it queries nothing: a city the branch has no
    standing to book in is not even looked up to see whether it exists.
    """
    cuttack = _branch_scope(3)
    for named in (5, 999999, None):
        assert (
            asyncio.run(_desk_pricing_city(_desk_body(named), _NoQueryDb(), cuttack))
            == 3
        ), named


def test_the_head_office_has_to_name_a_city_it_can_price_in():
    """The same field is a real choice for a super admin, and so has to be checked.

    Refusing before the fare sheet is read is what keeps "no price" from arriving
    as the misleading *no fare is configured for that cab category* — an empty
    city field is the agent's mistake, not a missing rate.
    """
    hq = BranchScope(
        actor_id="00000000-0000-0000-0000-000000000000",
        user_type="SUPER_ADMIN",
        is_super=True,
        branch_id=None,
        branch_name=None,
        is_headquarters=False,
        cities=frozenset(),
        include_unassigned=True,
    )
    with pytest.raises(ValidationException, match="Choose the city"):
        asyncio.run(_desk_pricing_city(_desk_body(None), _NoQueryDb(), hq))

    # A city that is not there fails against the database, so this one needs a
    # session — the rolled-back transaction adds no master data.
    with pytest.raises(ValidationException, match="not an active city"):
        _with_session(
            lambda session: _desk_pricing_city(_desk_body(999999), session, hq)
        )


# ── 2. Distance authority, at the service boundary ────────────────────────


def test_a_matched_corridor_ignores_a_handed_over_distance():
    corridor = _corridor()
    if corridor is None:
        pytest.skip("no active corridor with a city pair and a distance to match")
    sheet = _sheet_for(corridor["pricing_city_id"])

    async def work(session: AsyncSession):
        common = dict(
            pickup_location="Pickup place",
            drop_location="Drop place",
            origin_city_id=corridor["origin_city_id"],
            destination_city_id=corridor["destination_city_id"],
            vehicle_category_id=sheet["vehicle_category_id"],
            trip_type=sheet["trip_type"],
        )
        measured = await quote_cab_trip(session, **common)
        with_claim = await quote_cab_trip(
            session, **common, fallback_distance_km=Decimal("9999")
        )
        return measured, with_claim

    measured, with_claim = _with_session(work)
    assert measured.distance.km == with_claim.distance.km
    assert measured.total == with_claim.total
    assert measured.distance.source == "CONFIGURED_ROUTE"


def test_an_unmeasurable_trip_is_refused_rather_than_priced_at_zero():
    """The old desk path inserted `estimated_amount = 0.00` when the distance
    field was left empty. A ₹0.00 booking is not a free ride, it is a booking no
    partner will take and a customer who was never quoted."""
    corridor = _corridor()
    if corridor is None:
        pytest.skip("no active corridor to take the pricing city from")
    sheet = _sheet_for(corridor["pricing_city_id"])

    async def work(session: AsyncSession):
        with pytest.raises(ValidationException):
            await quote_cab_trip(
                session,
                pickup_location="Only a pickup, nowhere stated to go",
                drop_location="",
                origin_city_id=corridor["origin_city_id"],
                vehicle_category_id=sheet["vehicle_category_id"],
                trip_type="LOCAL",
            )

    _with_session(work)


def test_a_handed_over_distance_is_labelled_as_handed_over():
    """The website's one honest exception: a local hire with no destination has
    nothing to measure, so the customer's own figure is used — and the row says
    whose word it is instead of passing it off as a measurement."""
    corridor = _corridor()
    if corridor is None:
        pytest.skip("no active corridor to take the pricing city from")
    sheet = _sheet_for(corridor["pricing_city_id"])

    async def work(session: AsyncSession):
        return await quote_cab_trip(
            session,
            pickup_location="In-city pickup",
            drop_location="",
            origin_city_id=corridor["origin_city_id"],
            vehicle_category_id=sheet["vehicle_category_id"],
            trip_type="LOCAL",
            fallback_distance_km=Decimal("12.5"),
        )

    quote = _with_session(work)
    assert quote.distance.source == "CLIENT_STATED"
    assert quote.distance.km == Decimal("12.5")
    assert any("booking form" in n for n in quote.notes)


def test_the_persisted_lines_are_the_same_numbers_as_the_breakdown():
    """`fare_columns` is what a booking row stores. If it ever disagreed with the
    breakdown the invoice was built from, the invoice and the settlement line
    would be reconciling two different trips."""
    corridor = _corridor()
    if corridor is None:
        pytest.skip("no active corridor with a city pair and a distance to match")
    sheet = _sheet_for(corridor["pricing_city_id"])

    async def work(session: AsyncSession):
        return await quote_cab_trip(
            session,
            pickup_location="Pickup place",
            drop_location="Drop place",
            origin_city_id=corridor["origin_city_id"],
            destination_city_id=corridor["destination_city_id"],
            vehicle_category_id=sheet["vehicle_category_id"],
            trip_type=sheet["trip_type"],
        )

    quote = _with_session(work)
    cols = fare_columns(quote)
    b = quote.breakdown
    assert cols["base_fare_amount"] == Decimal(str(b["base_fare"]))
    assert cols["distance_charge_amount"] == Decimal(str(b["distance_charge"]))
    assert cols["night_charge_amount"] == Decimal(str(b["night_charge_applied"]))
    assert cols["toll_amount"] == Decimal(str(b["toll"]))
    assert cols["waiting_charge_amount"] == Decimal(str(b["waiting"]))
    assert cols["billing_distance_km"] == Decimal(str(b["billable_km"]))
    assert cols["distance_source"] == quote.distance.source
    stored_total = sum(
        (
            cols[k]
            for k in (
                "base_fare_amount",
                "distance_charge_amount",
                "driver_allowance_amount",
                "night_charge_amount",
                "toll_amount",
                "waiting_charge_amount",
            )
        ),
        Decimal("0"),
    )
    assert stored_total == quote.total
    # The snapshot must be the sheet that answered, or a dispute re-reads a rate
    # that was never applied.
    assert cols["rate_snapshot"]["rule_id"] == quote.rule["rule_id"]
    assert cols["rate_snapshot"]["distance_km"] == str(quote.distance.km)


# ── 3. The fare sheet is read as of the trip's day ────────────────────────


def test_an_expired_sheet_stops_pricing_the_day_after_it_expires():
    vcat = _fetch(
        "SELECT id FROM vehicle_categories WHERE is_active = TRUE ORDER BY id LIMIT 1"
    )[0]["id"]
    today = date.today()

    async def work(session: AsyncSession):
        city = (
            await session.execute(
                text(
                    "INSERT INTO cities (state_id, name, city_code, is_active) "
                    "VALUES ((SELECT state_id FROM cities WHERE id = 5), "
                    "'R15 Expired Sheet City', 'R15EXP', TRUE) RETURNING id"
                )
            )
        ).scalar_one()
        # The current sheet runs today only; the one before it ran up to yesterday.
        await _insert_sheet(
            session,
            city,
            vcat,
            "LOCAL",
            "1111.00",
            today,
            today + timedelta(days=30),
        )
        await _insert_sheet(
            session,
            city,
            vcat,
            "LOCAL",
            "9999.00",
            today - timedelta(days=60),
            today - timedelta(days=1),
        )

        on_the_day = await DefaultPricingService.get_effective_rule(
            session, city, vcat, "LOCAL", for_date=today
        )
        last_month = await DefaultPricingService.get_effective_rule(
            session,
            city,
            vcat,
            "LOCAL",
            for_date=today - timedelta(days=30),
        )
        after_both_expired = await DefaultPricingService.get_effective_rule(
            session,
            city,
            vcat,
            "LOCAL",
            for_date=today + timedelta(days=45),
        )
        return on_the_day, last_month, after_both_expired

    on_the_day, last_month, after_both_expired = _with_session(work)
    # The sheet that expired yesterday is not today's price…
    assert Decimal(str(on_the_day["base_fare"])) == Decimal("1111.00")
    assert on_the_day["rule_table"] == "vehicle_pricing_rules"
    # …but it was correctly the price on a day it covered, which is what a
    # back-dated booking or a reopened dispute needs the engine to still say.
    assert Decimal(str(last_month["base_fare"])) == Decimal("9999.00")
    # Past every city sheet the platform default takes over — the documented
    # fallback, and a different claim from "the last rate lingers for ever".
    assert after_both_expired["source"] == "default"


def test_a_future_sheet_prices_a_future_trip_and_nothing_today():
    vcat = _fetch(
        "SELECT id FROM vehicle_categories WHERE is_active = TRUE ORDER BY id LIMIT 1"
    )[0]["id"]
    today = date.today()

    async def work(session: AsyncSession):
        city = (
            await session.execute(
                text(
                    "INSERT INTO cities (state_id, name, city_code, is_active) "
                    "VALUES ((SELECT state_id FROM cities WHERE id = 5), "
                    "'R15 Future Sheet City', 'R15FUT', TRUE) RETURNING id"
                )
            )
        ).scalar_one()
        await _insert_sheet(
            session,
            city,
            vcat,
            "LOCAL",
            "2222.00",
            today + timedelta(days=10),
            None,
        )
        today_rule = await DefaultPricingService.get_effective_rule(
            session, city, vcat, "LOCAL", for_date=today
        )
        announced = await DefaultPricingService.get_effective_rule(
            session,
            city,
            vcat,
            "LOCAL",
            for_date=today + timedelta(days=12),
        )
        return today_rule, announced

    today_rule, announced = _with_session(work)
    # A rate announced for next month must not be applied to today's booking, and
    # a today booking that has no city sheet at all takes the platform default.
    assert today_rule["source"] == "default"
    assert Decimal(str(announced["base_fare"])) == Decimal("2222.00")


def test_two_sheets_in_force_for_one_trip_are_a_conflict_not_a_coin_flip():
    """`LIMIT 1` used to pick whichever row the index happened to reach first, so
    a duplicated fare sheet priced real bookings at an unprovable rate. The
    platform now refuses and names the day."""
    vcat = _fetch(
        "SELECT id FROM vehicle_categories WHERE is_active = TRUE ORDER BY id LIMIT 1"
    )[0]["id"]
    today = date.today()

    async def work(session: AsyncSession):
        city = (
            await session.execute(
                text(
                    "INSERT INTO cities (state_id, name, city_code, is_active) "
                    "VALUES ((SELECT state_id FROM cities WHERE id = 5), "
                    "'R15 Doubled Sheet City', 'R15DUP', TRUE) RETURNING id"
                )
            )
        ).scalar_one()
        await _insert_sheet(session, city, vcat, "ONE_WAY", "300.00", None, None)
        await _insert_sheet(session, city, vcat, "ONE_WAY", "400.00", None, None)
        with pytest.raises(BusinessException):
            await DefaultPricingService.get_effective_rule(
                session, city, vcat, "ONE_WAY", for_date=today
            )

    _with_session(work)
