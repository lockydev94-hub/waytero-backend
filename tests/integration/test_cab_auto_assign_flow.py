"""Round 15 C3 — the fan-out offer chain, proved on a database that keeps nothing.

The claim is the owner's phone-support sequence continued past the desk: a cab
that has no partner yet is asked of the partners who could take it — the ones in
that city, with that category of vehicle, free right now, least work already on
their plate — in small groups, one group at a time, and a booking leaves the
chain only when a partner says yes, when the rounds run out, or when there is
nobody left to ask.

Everything here calls the functions the live endpoints call — `publish_round`,
`close_open_offers`, `advance_after_offers_closed`, `begin_chain`, and the
admin's own `_close_pending_partner_assignment` — inside one transaction on a
private engine that is always rolled back. The city, the partners, their
vehicles and the bookings are created here and thrown away, because offering the
owner's real partners a trip that does not exist, or filing a booking they would
have to clean up, is not what a test is for. The last assertion is that the
tables hold the number of rows they started with.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.modules.booking.models import CabBooking
from app.modules.booking.services import auto_assign
from app.modules.partner.booking_api import _current_offer_row

CATEGORY = 2  # SEDAN — one of the owner's seven vehicle categories
PARTNER_NAMES = ("Alpha", "Bravo", "Charlie")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

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
    "vehicles",
    "system_configurations",
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
    always rolled back — the idiom the amendment-flow test established.

    `autoflush=False` matches the app's own session factory, so the code under
    test sees the same session behaviour it sees in a request, and every write
    below is flushed explicitly the way `get_db`'s commit does for the endpoints.
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
    """Counted once, before the first test in this module writes anything."""
    return {t: _count(t) for t in TRADED_TABLES}


# ── seeds, all of them thrown away ────────────────────────────────────────


async def _free_mobile(session: AsyncSession) -> str:
    """The first number in a reserved test block nobody holds — `users.mobile_number`
    is unique, so a fixed literal would break the day the owner registers it."""
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
    """Its own city, so no partner or booking of the owner's is reachable.

    The chain asks "who in this city has a free cab of this category", and the
    only honest way to answer that in a test is a city whose partners are the
    ones this file creates.
    """
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO cities (state_id, name, is_active) "
                    "VALUES (1, 'Auto-Assign Test City', FALSE) RETURNING id"
                )
            )
        ).scalar_one()
    )


async def _seed_partner(session: AsyncSession, city_id: int, name: str) -> int:
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text(
            "INSERT INTO users (id, first_name, mobile_number, user_type) "
            "VALUES (:uid, :name, :mobile, 'PARTNER')"
        ),
        {"uid": user_id, "name": f"{name} Owner", "mobile": mobile},
    )
    partner_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO partners (user_id, partner_code, partner_type, "
                    "       owner_name, mobile, city_id, status, business_name) "
                    "VALUES (:uid, :code, 'INDIVIDUAL', :name, :mobile, :city, "
                    "        'ACTIVE', :name) RETURNING id"
                ),
                {
                    "uid": user_id,
                    "code": f"IT-AUTO-{str(user_id)[:8]}",
                    "name": f"{name} Cabs",
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
    await session.flush()
    return partner_id


async def _seed_vehicle(
    session: AsyncSession, partner_id: int, city_id: int, tag: str
) -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO vehicles (partner_id, vehicle_category_id, "
                    "       registration_number, city_id, status, vehicle_code) "
                    "VALUES (:pid, :cat, :reg, :city, 'ACTIVE', :code) "
                    "RETURNING id"
                ),
                {
                    "pid": partner_id,
                    "cat": CATEGORY,
                    "reg": f"OT-AUTO-{tag}",
                    "city": city_id,
                    "code": f"IT-AUTO-V-{tag}",
                },
            )
        ).scalar_one()
    )


async def _seed_booking(session: AsyncSession, city_id: int, tag: str) -> int:
    """A cab booking in this city with nobody asked yet."""
    user_id = uuid4()
    mobile = await _free_mobile(session)
    await session.execute(
        text(
            "INSERT INTO users (id, first_name, mobile_number, user_type) "
            "VALUES (:uid, 'Auto Assign Customer', :mobile, 'CUSTOMER')"
        ),
        {"uid": user_id, "mobile": mobile},
    )
    customer_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO customers (user_id, customer_code, first_name) "
                    "VALUES (:uid, :code, 'Auto Assign') RETURNING id"
                ),
                {"uid": user_id, "code": f"IT-AUTO-C-{str(user_id)[:8]}"},
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
                    "VALUES (:uuid, :num, :cust, :city, 'CONFIRMED', 'PENDING', "
                    "        1000.00, :start) RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "num": f"MB-AUTO-{tag}",
                    "cust": customer_id,
                    "city": city_id,
                    "start": datetime(2026, 11, 20).date(),
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
                    "       estimated_distance, estimated_amount, booking_status) "
                    "VALUES (:uuid, :mb, :num, 'ONE_WAY', :cat, "
                    "        'Auto Pickup', 'Auto Drop', :pickup, 40.00, 1000.00, "
                    "        'PENDING_ASSIGNMENT') RETURNING id"
                ),
                {
                    "uuid": uuid4(),
                    "mb": master_id,
                    "num": f"CAB-AUTO-{tag}",
                    "cat": CATEGORY,
                    "pickup": datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc),
                },
            )
        ).scalar_one()
    )
    await session.flush()
    return cab_id


async def _seed_world(
    session: AsyncSession, *, tag: str, round_size: int = 2, max_rounds: int = 2,
    enabled: bool = True,
) -> dict:
    """City, three partners with one cab each, and one booking waiting."""
    city_id = await _seed_city(session)
    partner_ids = [
        await _seed_partner(session, city_id, name) for name in PARTNER_NAMES
    ]
    vehicles = [
        await _seed_vehicle(session, pid, city_id, f"{tag}{i}")
        for i, pid in enumerate(partner_ids)
    ]
    await _set_config(
        session,
        enabled=enabled,
        round_size=round_size,
        max_rounds=max_rounds,
    )
    cab_id = await _seed_booking(session, city_id, tag)
    return {"city_id": city_id, "partner_ids": partner_ids, "cab_id": cab_id,
            "vehicles": vehicles}


async def _set_config(
    session: AsyncSession,
    *,
    enabled: bool,
    round_size: int = 2,
    max_rounds: int = 2,
    timeout_seconds: int = 180,
) -> None:
    """The four switches migration 0086 seeded, set for the test and rolled back.

    Updated rather than inserted: the rows already exist, and reading whatever
    the super admin configured is the behaviour under test.
    """
    for key, value in (
        ("auto_assign_enabled", "true" if enabled else "false"),
        ("auto_assign_round_size", str(round_size)),
        ("auto_assign_max_rounds", str(max_rounds)),
        ("auto_assign_timeout_seconds", str(timeout_seconds)),
    ):
        await session.execute(
            text(
                "UPDATE system_configurations SET config_value = :v "
                "WHERE config_key = :k"
            ),
            {"v": value, "k": key},
        )
    await session.flush()


# ── reads ─────────────────────────────────────────────────────────────────


async def _load_cab(session: AsyncSession, cab_id: int) -> CabBooking:
    """Read the cab the way its own request would: from a session that has never
    seen the writes before it.

    One session runs the whole flow here, and the chain writes with SQL, so an
    object loaded earlier still carries the status and the assignment list it had
    when it was read (`autoflush=False` hides pending rows, raw UPDATEs hide
    committed ones). Expiring first makes the reload honest.
    """
    session.expire_all()
    return (
        await session.execute(
            select(CabBooking)
            .options(selectinload(CabBooking.assignments))
            .where(CabBooking.id == cab_id)
        )
    ).scalar_one()


async def _offer_rows(session: AsyncSession, cab_id: int) -> list[dict]:
    return [
        dict(r)
        for r in (
            await session.execute(
                text(
                    "SELECT partner_id, assignment_type, accepted_at, rejected_at, "
                    "       rejection_reason_code, closed_at, close_reason, "
                    "       acceptance_deadline, available_cabs_at_offer "
                    "FROM cab_booking_assignments WHERE cab_booking_id = :id "
                    "ORDER BY partner_id"
                ),
                {"id": cab_id},
            )
        ).mappings().all()
    ]


async def _cab_state(session: AsyncSession, cab_id: int) -> dict:
    return dict(
        (
            await session.execute(
                text(
                    "SELECT booking_status, pending_partner_id, acceptance_deadline, "
                    "       needs_attention, partner_responded_at "
                    "FROM cab_bookings WHERE id = :id"
                ),
                {"id": cab_id},
            )
        ).mappings().one()
    )


async def _timelines(session: AsyncSession, cab_id: int) -> list[dict]:
    return [
        dict(r)
        for r in (
            await session.execute(
                text(
                    "SELECT t.event_type, t.event_description, t.created_by "
                    "FROM booking_timelines t "
                    "JOIN cab_bookings cb ON cb.master_booking_id = t.master_booking_id "
                    "WHERE cb.id = :id ORDER BY t.id"
                ),
                {"id": cab_id},
            )
        ).mappings().all()
    ]


# ── the round ─────────────────────────────────────────────────────────────


def test_a_round_asks_the_free_partners_and_an_acceptance_ends_the_others() -> None:
    async def flow(session: AsyncSession) -> dict:
        world = await _seed_world(session, tag="R")
        cab_id, partner_ids = world["cab_id"], world["partner_ids"]

        cb = await _load_cab(session, cab_id)
        config = await auto_assign.settings(session)
        offered = await auto_assign.publish_round(
            session,
            cb,
            city_id=world["city_id"],
            cohort_ids=None,
            settings_=config,
        )
        await session.flush()
        rows = await _offer_rows(session, cab_id)
        state = await _cab_state(session, cab_id)

        # Partner R1 accepts, the way the accept endpoint does: the offer row it
        # is still allowed to answer, the booking moved on, and the row still open
        # on R2's screen closed as accepted elsewhere — not as a refusal.
        cb = await _load_cab(session, cab_id)
        accepting = partner_ids[0]
        a_row = next(r for r in cb.assignments if r.partner_id == accepting)
        a_row.accepted_at = NOW
        cb.booking_status = "ASSIGNED"
        cb.partner_responded_at = NOW
        cb.acceptance_deadline = None
        closed = await auto_assign.close_open_offers(
            session,
            cab_id,
            reason="ACCEPTED_BY_ANOTHER",
            now=NOW,
            exclude_partner_id=accepting,
            note=f"Partner {accepting} accepted the trip first.",
        )
        await session.flush()
        # What the accept and reject endpoints read the next time any of these
        # partners calls: the closed rows and the answered one are all finished.
        cab_after = await _load_cab(session, cab_id)
        answerable = {
            pid: _current_offer_row(cab_after, pid) is not None
            for pid in partner_ids
        }
        return {
            "round_size": config.round_size,
            "offered_ids": [int(p["partner_id"]) for p in offered],
            "answerable": answerable,
            "rows": rows,
            "state": state,
            "closed": [int(p) for p in closed],
            "after_accept": await _offer_rows(session, cab_id),
            "state_after_accept": await _cab_state(session, cab_id),
            "offer_count": await auto_assign.published_rounds(session, cab_id),
            "open_offers": await auto_assign.open_offer_partner_ids(session, cab_id),
            "timelines": await _timelines(session, cab_id),
            "partner_ids": partner_ids,
        }

    out = _with_session(flow)

    # Ranking is work already in hand, then id. Nobody here has a trip running,
    # so the round is the first `round_size` partners by id — deterministic, and
    # no invented rating.
    assert out["round_size"] == 2
    assert out["offered_ids"] == out["partner_ids"][:2]

    assert len(out["rows"]) == 2
    for row in out["rows"]:
        assert row["assignment_type"] == "AUTO_OFFER"
        assert row["accepted_at"] is None
        assert row["rejected_at"] is None
        assert row["closed_at"] is None
        assert row["acceptance_deadline"] is not None
        # What the partner had when asked, snapshotted like a manual assign does.
        assert row["available_cabs_at_offer"] == 1

    # The booking is being asked of a group, so the one-column pointer stays empty
    # and the shared deadline is what the countdown reads.
    assert out["state"]["booking_status"] == "PENDING_PARTNER_ACCEPTANCE"
    assert out["state"]["pending_partner_id"] is None
    assert out["state"]["acceptance_deadline"] is not None
    assert out["state"]["needs_attention"] is None

    assert out["closed"] == [out["partner_ids"][1]]
    by_partner = {r["partner_id"]: r for r in out["after_accept"]}
    winner = by_partner[out["partner_ids"][0]]
    loser = by_partner[out["partner_ids"][1]]
    assert winner["accepted_at"] is not None
    assert winner["closed_at"] is None
    assert winner["close_reason"] is None
    assert loser["closed_at"] is not None
    assert loser["closed_at"] == NOW
    assert loser["close_reason"] == "ACCEPTED_BY_ANOTHER"
    assert loser["rejection_reason_code"] == "ACCEPTED_BY_ANOTHER"

    assert out["state_after_accept"]["booking_status"] == "ASSIGNED"
    assert out["state_after_accept"]["acceptance_deadline"] is None
    # Nobody is left holding an unanswered offer.
    assert out["open_offers"] == []
    # Three different reasons, one answer for the endpoints: the winner already
    # answered, the loser was closed as accepted elsewhere, and the third partner
    # was never asked — none of them has an Accept button left.
    assert out["answerable"] == {pid: False for pid in out["partner_ids"]}
    # An accepted trip is not a round the chain may continue: the deadline that
    # defines round one still exists on the closed row, so the count stays true.
    assert out["offer_count"] == 1

    offered_events = [
        t for t in out["timelines"] if t["event_type"] == "AUTO_ASSIGN_OFFERED"
    ]
    assert len(offered_events) == 1
    assert "round 1" in offered_events[0]["event_description"]
    assert "2 partner(s)" in offered_events[0]["event_description"]
    # Nobody signed this: the chain is the system asking, not an admin acting.
    assert offered_events[0]["created_by"] is None


def test_the_chain_offers_the_next_group_then_hands_the_booking_to_a_human() -> None:
    async def flow(session: AsyncSession) -> dict:
        world = await _seed_world(session, tag="S")
        cab_id, partner_ids, city_id = (
            world["cab_id"],
            world["partner_ids"],
            world["city_id"],
        )

        cb = await _load_cab(session, cab_id)
        config = await auto_assign.settings(session)
        first = await auto_assign.publish_round(
            session, cb, city_id=city_id, cohort_ids=None, settings_=config
        )
        await session.flush()

        # Round one expires with nobody answering — the same close the sweeper
        # writes before it calls the advance() this test calls.
        timed_out = await auto_assign.close_open_offers(
            session,
            cab_id,
            reason="TIMEOUT",
            now=NOW,
            note="Auto-rejected by sweeper after deadline.",
        )
        await session.flush()
        outcome, second = await auto_assign.advance_after_offers_closed(
            session,
            cb,
            city_id=city_id,
            now=NOW,
            reason_for_next_round="2 partners did not answer",
        )
        await session.flush()
        state_mid = await _cab_state(session, cab_id)
        rows_mid = await _offer_rows(session, cab_id)

        # Round two expires too, and the configured rounds are spent.
        await auto_assign.close_open_offers(
            session,
            cab_id,
            reason="TIMEOUT",
            now=NOW + timedelta(minutes=3),
            note="Auto-rejected by sweeper after deadline.",
        )
        await session.flush()
        outcome2, _ = await auto_assign.advance_after_offers_closed(
            session,
            cb,
            city_id=city_id,
            now=NOW + timedelta(minutes=3),
            reason_for_next_round="1 partner did not answer",
        )
        await session.flush()
        return {
            "first": sorted(int(p["partner_id"]) for p in first),
            "timed_out": sorted(int(p) for p in timed_out),
            "outcome": outcome,
            "second": [int(p["partner_id"]) for p in second],
            "rows_mid": rows_mid,
            "state_mid": state_mid,
            "outcome2": outcome2,
            "state_end": await _cab_state(session, cab_id),
            "rows_end": await _offer_rows(session, cab_id),
            "rounds": await auto_assign.published_rounds(session, cab_id),
            "timelines": await _timelines(session, cab_id),
            "partner_ids": partner_ids,
        }

    out = _with_session(flow)

    assert out["timed_out"] == out["first"]
    assert out["outcome"] == "NEXT_ROUND"
    # The partner who was not in round one is the only one left to ask, and the
    # already-asked two are never asked twice on the same booking.
    assert out["second"] == [out["partner_ids"][2]]
    assert out["state_mid"]["booking_status"] == "PENDING_PARTNER_ACCEPTANCE"
    assert out["state_mid"]["needs_attention"] is None

    assert out["rounds"] == 2
    # A round is its shared deadline: two partners asked together, one partner
    # asked later, and closing a row leaving the deadline alone so the chain can
    # still count the rounds it has spent.
    grouped: dict = {}
    for row in out["rows_mid"]:
        grouped.setdefault(row["acceptance_deadline"], []).append(row["partner_id"])
    assert sorted(len(ids) for ids in grouped.values()) == [1, 2]
    assert len(grouped) == out["rounds"]

    assert out["outcome2"] == "EXHAUSTED"
    assert out["state_end"]["booking_status"] == "PENDING_ASSIGNMENT"
    assert out["state_end"]["needs_attention"] == "AUTO_ASSIGN_EXHAUSTED"
    assert out["state_end"]["acceptance_deadline"] is None
    assert all(r["closed_at"] is not None for r in out["rows_end"])

    stopped = [t for t in out["timelines"] if t["event_type"] == "AUTO_ASSIGN_EXHAUSTED"]
    assert len(stopped) == 1
    assert "2 round(s)" in stopped[0]["event_description"]
    assert "did not answer" in stopped[0]["event_description"]


def test_a_partner_with_no_free_cab_of_that_category_is_never_asked() -> None:
    async def flow(session: AsyncSession) -> dict:
        world = await _seed_world(session, tag="T")
        partner_ids, vehicles = world["partner_ids"], world["vehicles"]

        # A second booking already has two of the three cabs out on the road:
        # those partners must not be offered a trip they cannot run.
        busy_cab = await _seed_booking(session, world["city_id"], "BUSY")
        for pid, vid in zip(partner_ids[:2], vehicles[:2]):
            await session.execute(
                text(
                    "INSERT INTO cab_booking_assignments (cab_booking_id, "
                    "       partner_id, vehicle_id, assignment_type, accepted_at) "
                    "VALUES (:cb, :pid, :vid, 'MANUAL', NOW())"
                ),
                {"cb": busy_cab, "pid": pid, "vid": vid},
            )
        await session.execute(
            text(
                "UPDATE cab_bookings SET booking_status = 'STARTED' WHERE id = :id"
            ),
            {"id": busy_cab},
        )
        await session.flush()

        free_cab = world["cab_id"]
        config = await auto_assign.settings(session)
        cb = await _load_cab(session, free_cab)
        # Read the ranking before the round exists: being asked is itself a reason
        # not to be asked again, and a candidate list read afterwards would be
        # proving the exclusion rule instead of the availability one.
        candidates = await auto_assign.candidates(
            session,
            cab_booking_id=free_cab,
            city_id=world["city_id"],
            category_id=CATEGORY,
            round_size=3,
        )
        offered = await auto_assign.publish_round(
            session,
            cb,
            city_id=world["city_id"],
            cohort_ids=None,
            settings_=config,
        )
        await session.flush()
        asked_again = await auto_assign.candidates(
            session,
            cab_booking_id=free_cab,
            city_id=world["city_id"],
            category_id=CATEGORY,
            round_size=3,
        )
        return {
            "busy_partners": partner_ids[:2],
            "free_partner": partner_ids[2],
            "candidate_ids": [int(c["partner_id"]) for c in candidates],
            "asked_twice": [int(c["partner_id"]) for c in asked_again],
            "candidate_free_cabs": {
                int(c["partner_id"]): int(c["free_cabs"]) for c in candidates
            },
            "offered": [int(p["partner_id"]) for p in offered],
        }

    out = _with_session(flow)

    # The two busy partners are excluded because the trip would sit on them; the
    # one with a cab standing still is the only answer the ranking can give.
    assert out["candidate_ids"] == [out["free_partner"]]
    assert out["candidate_free_cabs"] == {out["free_partner"]: 1}
    assert out["offered"] == [out["free_partner"]]
    # Asked is asked: the partner who holds the offer is out of the next
    # candidate list, so a chain never offers the same trip twice.
    assert out["asked_twice"] == []


def test_the_chain_does_not_start_when_the_super_admin_has_it_switched_off() -> None:
    async def flow(session: AsyncSession) -> dict:
        world = await _seed_world(session, tag="U", enabled=False)
        cab_id, partner_ids = world["cab_id"], world["partner_ids"]

        cb = await _load_cab(session, cab_id)
        started = await auto_assign.begin_chain(session, cb, city_id=world["city_id"])
        await session.flush()
        rows_before = await _offer_rows(session, cab_id)

        # A hand-picked partner who does not answer: with the chain off this is
        # what the sweeper did before C3 existed — back to the queue, for a human.
        await session.execute(
            text(
                "INSERT INTO cab_booking_assignments (cab_booking_id, partner_id, "
                "       assignment_type, acceptance_deadline) "
                "VALUES (:cb, :pid, 'MANUAL', NOW() + interval '10 minutes')"
            ),
            {"cb": cab_id, "pid": partner_ids[0]},
        )
        await session.execute(
            text(
                "UPDATE cab_bookings SET booking_status = "
                "              'PENDING_PARTNER_ACCEPTANCE' WHERE id = :cb"
            ),
            {"cb": cab_id},
        )
        await session.flush()

        cb = await _load_cab(session, cab_id)
        closed = await auto_assign.close_open_offers(
            session,
            cab_id,
            reason="TIMEOUT",
            now=NOW,
            note="Auto-rejected by sweeper after deadline.",
        )
        await session.flush()
        outcome, _ = await auto_assign.advance_after_offers_closed(
            session,
            cb,
            city_id=world["city_id"],
            now=NOW,
            reason_for_next_round="1 partner did not answer",
        )
        await session.flush()
        return {
            "started": started,
            "rows_before": rows_before,
            "closed": [int(p) for p in closed],
            "outcome": outcome,
            "state": await _cab_state(session, cab_id),
            "rows_after": await _offer_rows(session, cab_id),
            "timelines": await _timelines(session, cab_id),
        }

    out = _with_session(flow)

    assert out["started"] == []
    assert out["rows_before"] == []
    assert out["closed"]  # the unanswered offer was still closed by the sweeper
    assert out["outcome"] == "FOR_HUMAN"
    assert out["state"]["booking_status"] == "PENDING_ASSIGNMENT"
    assert out["state"]["needs_attention"] is None
    assert out["state"]["acceptance_deadline"] is None
    assert out["state"]["pending_partner_id"] is None
    # No round was published, so the refused partner's row is the only one there is.
    assert len(out["rows_after"]) == 1
    assert not [t for t in out["timelines"] if t["event_type"] == "AUTO_ASSIGN_OFFERED"]


def test_a_losing_partner_has_nothing_left_to_answer() -> None:
    """`_current_offer_row` is how the accept and reject endpoints decide who may
    speak — the piece the fan-out breaks, because a round has several live rows
    at the same instant and `pending_partner_id` cannot name them.
    """

    class _Row:
        def __init__(self, partner_id: int, assigned_at: datetime):
            self.id = partner_id
            self.partner_id = partner_id
            self.assigned_at = assigned_at
            self.accepted_at = None
            self.rejected_at = None
            self.rejection_reason_code = None
            self.closed_at = None
            self.close_reason = None

    class _Cab:
        def __init__(self, assignments):
            self.assignments = assignments

    round_rows = [_Row(pid, NOW) for pid in (11, 12, 13)]
    cab = _Cab(round_rows)

    # All three were asked at the same instant — each of them may still answer.
    live = [_current_offer_row(cab, pid) for pid in (11, 12, 13)]
    assert [row.partner_id for row in live] == [11, 12, 13]

    # One accepts and the others' rows are closed as accepted elsewhere. An
    # answered row is not an answerable offer, so the winner has nothing left to
    # submit either — but its row stays open, because an open row is what keeps
    # the trip on that partner's own list.
    round_rows[0].accepted_at = NOW
    for row in round_rows[1:]:
        row.closed_at = NOW
        row.close_reason = "ACCEPTED_BY_ANOTHER"
        row.rejected_at = NOW
        row.rejection_reason_code = "ACCEPTED_BY_ANOTHER"
    assert [row.partner_id for row in round_rows if row.closed_at is None] == [11]
    assert all(_current_offer_row(cab, pid) is None for pid in (11, 12, 13, 99))


def test_admin_reassign_closes_every_offer_in_the_round() -> None:
    """The admin's own close-out used to stop at the first open row. With a round
    of three that left two partners holding an Accept button on a booking that
    had already moved on to somebody else.
    """
    from app.modules.admin.booking_api import _close_pending_partner_assignment

    async def flow(session: AsyncSession) -> dict:
        world = await _seed_world(session, tag="V", round_size=3)
        cab_id, partner_ids, city_id = (
            world["cab_id"],
            world["partner_ids"],
            world["city_id"],
        )

        cb = await _load_cab(session, cab_id)
        config = await auto_assign.settings(session)
        await auto_assign.publish_round(
            session, cb, city_id=city_id, cohort_ids=None, settings_=config
        )
        await session.flush()

        # The queue chip this booking would be showing if the chain had stopped.
        cb = await _load_cab(session, cab_id)
        cb.needs_attention = "AUTO_ASSIGN_EXHAUSTED"
        closed_rows = await _close_pending_partner_assignment(
            session,
            cb,
            reason_code="ADMIN_REASSIGN",
            description_suffix="Admin took over.",
        )
        # …and what the admin's assign path writes immediately afterwards.
        cb.booking_status = "PENDING_PARTNER_ACCEPTANCE"
        cb.pending_partner_id = partner_ids[0]
        cb.needs_attention = None
        await session.flush()

        return {
            "closed": sorted(int(r.partner_id) for r in closed_rows),
            "rows": await _offer_rows(session, cab_id),
            "state": await _cab_state(session, cab_id),
            "partner_ids": partner_ids,
            "timelines": await _timelines(session, cab_id),
        }

    out = _with_session(flow)

    assert out["closed"] == sorted(out["partner_ids"])
    assert len(out["rows"]) == 3
    for row in out["rows"]:
        assert row["closed_at"] is not None
        assert row["close_reason"] == "ADMIN_REASSIGN"
        assert row["rejection_reason_code"] == "ADMIN_REASSIGN"
    assert out["state"]["needs_attention"] is None
    assert out["state"]["pending_partner_id"] == out["partner_ids"][0]
    assert out["timelines"][-1]["event_type"] == "PARTNER_ASSIGNMENT_CLOSED"


def test_nothing_survives_the_transaction(_row_counts_before_any_test: dict) -> None:
    # Counted before this module's first test wrote anything, read now: the rows
    # these flows booked, offered, refused and timed out were never in the
    # database the owner uses.
    for table, before in _row_counts_before_any_test.items():
        assert _count(table) == before, table
