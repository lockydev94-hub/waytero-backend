"""Round 15 C2c — amending a booked cab trip, proved on a database that keeps nothing.

The claim is the one the phone desk needs: when a customer changes the trip, the
booking becomes the new trip at the new price, everywhere at once. Not the fare
recomputed and the old route left on the row, not the master booking moved while
the cab row stayed behind — the number the customer is told, the number on the
cab booking, the number on the master booking, the number on the service link and
the sum of the persisted lines all becoming the same Decimal, with the old total
kept in the audit trail.

Everything here runs the endpoints themselves — `create_cab_booking_by_admin` and
`edit_cab_booking_by_admin` — inside one transaction on a private engine that is
always rolled back, because the alternative is filing a booking, a customer and a
fare sheet into the owner's live database to read them back once. Nothing outside
this transaction is touched, the last assertion is that the booking table has the
same number of rows it started with, and no distance is ever asked of Google: both
trips are measured on published corridors, one real and one the test publishes and
then throws away.

The refusals at the end are on the same row the flow created — a booking whose trip
has run, one with money collected against it, and one carrying a redemption that no
longer validates — because the gate is worth nothing if it only refuses bookings
that do not exist.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.branch_scope import BranchScope
from app.core.config import settings
from app.modules.admin.customer_care_api import (
    CabBookingCreateByAdmin,
    CabBookingEditByAdmin,
    create_cab_booking_by_admin,
    edit_cab_booking_by_admin,
)

Bhubaneswar = 5  # the city with fare sheets and with a published corridor
Puri = 4
Berhampur = 1

# Fixed future dates, not "now + 3 days": the booking path refuses a pickup
# that has already passed, and a relative date would make the assertions below
# move with the calendar.
PICKUP = datetime(2027, 11, 5, 14, 30)  # afternoon: no night charge anywhere
AMENDED_PICKUP = datetime(2027, 11, 12, 16, 0)
AMENDED_RETURN = datetime(2027, 11, 13, 10, 0)

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

# The sheets this test prices with, stated as numbers rather than read from the
# owner's data: the city, category and trip type are chosen so that nothing else
# is in force for them, which is what lets the assertions below be exact.
ONE_WAY_SHEET = {
    "trip_type": "ONE_WAY",
    "base_fare": "500",
    "minimum_km": "30",
    "per_km_rate": "20",
    "driver_allowance": "0",
}
ROUND_TRIP_SHEET = {
    "trip_type": "ROUND_TRIP",
    "base_fare": "800",
    "minimum_km": "30",
    "per_km_rate": "15",
    "driver_allowance": "500",
}

# 70.90 km is the corridor the owner published between Bhubaneswar and Puri, and
# a 250 km one with a ₹300 toll is the corridor this test invents for the amended
# trip. ONE_WAY bills the greater of the two: 70.90 × 20 + 500. ROUND_TRIP:
# 250 × 15 + 800 + 500 allowance + the corridor's toll.
ORIGINAL_TOTAL = Decimal("1918.00")
AMENDED_TOTAL = Decimal("5350.00")


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
    """Run `work(session)` on a private engine inside a transaction that is always
    rolled back — the idiom `test_cab_pricing_authority_api.py` established.

    `autoflush=False` matches the app's own session factory, so the endpoints under
    test see the same session behaviour they see in a request; each write here is
    followed by an explicit flush, which is what `get_db`'s commit does for them.
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


async def _seed_sheet(session: AsyncSession, category_id: int, sheet: dict) -> None:
    await session.execute(
        text(
            """
            INSERT INTO vehicle_pricing_rules (
                city_id, vehicle_category_id, trip_type, base_fare, minimum_km,
                per_km_rate, driver_allowance, night_charge, toll,
                effective_from, effective_to
            ) VALUES (
                :city, :vcid, :trip_type, :base_fare, :minimum_km,
                :per_km_rate, :driver_allowance, 0, 0,
                NULL, NULL
            )
        """
        ),
        {
            "city": Bhubaneswar,
            "vcid": category_id,
            **{
                k: Decimal(v) if k not in {"trip_type"} else v for k, v in sheet.items()
            },
        },
    )
    await session.flush()


async def _seed_far_corridor(session: AsyncSession) -> None:
    """A published Bhubaneswar→Berhampur corridor with a toll on it.

    `cab_routes` is where the owner keeps both the distance and the toll, so this
    is the only way to prove the amended trip's toll arrives from the corridor
    rather than from the city's flat figure.
    """
    await session.execute(
        text(
            """
            INSERT INTO cab_routes (
                slug, origin_label, destination_label, origin_city_id,
                destination_city_id, pricing_city_id, distance_km,
                distance_source, toll_charge, description, terms_and_conditions,
                is_active, sort_order, created_at, updated_at
            ) VALUES (
                'it-wt-cab-amend-flow', :origin, :destination, :orig_city,
                :dest_city, :pricing, 250.00, 'MANUAL', 300.00,
                'Temporary corridor created inside a rolled-back test transaction.',
                'Test only.', TRUE, 9999, NOW(), NOW()
            )
        """
        ),
        {
            # The labels carry the test's name because `cab_routes` is unique on
            # the (origin, destination) text pair, and the owner has already
            # published a Bhubaneswar→Berhampur corridor with no city on it.
            "origin": "Bhubaneswar (amendment flow test)",
            "destination": "Berhampur (amendment flow test)",
            "orig_city": Bhubaneswar,
            "dest_city": Berhampur,
            "pricing": Bhubaneswar,
        },
    )
    await session.flush()


async def _free_mobile(session: AsyncSession) -> str:
    """The first number in a reserved test block that nobody holds.

    `users.mobile_number` is unique on the string, so a fixed literal would make
    this test fail the day the owner registers a customer with it — and the block
    is searched rather than assumed, because whether `9000000004` belongs to
    somebody is a fact about this database, not about the code.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT '9' || lpad(n::text, 9, '0') AS mobile "
                    "FROM generate_series(0, 999) AS n "
                    "WHERE NOT EXISTS (SELECT 1 FROM users u "
                    "                  WHERE u.mobile_number = '9' || lpad(n::text, 9, '0')) "
                    "ORDER BY n LIMIT 1"
                )
            )
        )
        .mappings()
        .one()
    )
    return str(row["mobile"])


async def _any_branch(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(text("SELECT id FROM branches ORDER BY id LIMIT 1"))
        ).scalar_one()
    )


async def _seed_users(session: AsyncSession) -> tuple[int, str]:
    """A walk-in customer and the desk user acting for them, both thrown away.

    The customer has no email, so the confirmation mail is never attempted — a
    rolled-back transaction must not be able to reach an SMTP server. The admin
    exists because `advance_payments.collected_by_user_id` is a real foreign key
    and the second refusal needs a collector who is somebody, and because the
    acting user on the timeline should be a user too.
    """
    customer_user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, first_name, mobile_number, user_type, email) "
            "VALUES (:uid, 'Amendment Test', :mobile, 'CUSTOMER', NULL)"
        ),
        {"uid": customer_user_id, "mobile": await _free_mobile(session)},
    )
    await session.flush()

    admin_user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, first_name, mobile_number, user_type, branch_id) "
            "VALUES (:uid, 'Amendment Desk', :mobile, 'ADMIN', :branch)"
        ),
        {
            "uid": admin_user_id,
            "mobile": await _free_mobile(session),
            # `ck_users_branch_membership` requires an ADMIN to belong somewhere.
            # The desk here is acting with the head office scope, so which branch
            # the account sits in is a detail of the seed, not of the claim.
            "branch": await _any_branch(session),
        },
    )
    await session.execute(
        text(
            "INSERT INTO customers (uuid, user_id, customer_code) "
            "VALUES (:cu, :uid, :code)"
        ),
        {
            "cu": uuid4(),
            "uid": customer_user_id,
            "code": f"WTTEST{customer_user_id.hex[:6].upper()}",
        },
    )
    await session.flush()
    customer_id = int(
        (
            await session.execute(
                text("SELECT id FROM customers WHERE user_id = :uid"),
                {"uid": customer_user_id},
            )
        ).scalar_one()
    )
    return customer_id, str(admin_user_id)


async def _booking_rows(session: AsyncSession, cab_id: int) -> dict:
    return (
        (
            await session.execute(
                text(
                    "SELECT cb.id, cb.master_booking_id, cb.booking_status, "
                    "       cb.trip_type, cb.vehicle_category_id, "
                    "       cb.pickup_location, cb.drop_location, "
                    "       cb.pickup_datetime, cb.estimated_distance, "
                    "       cb.estimated_amount, cb.distance_source, "
                    "       cb.billing_distance_km, cb.base_fare_amount, "
                    "       cb.distance_charge_amount, cb.driver_allowance_amount, "
                    "       cb.night_charge_amount, cb.toll_amount, "
                    "       cb.waiting_charge_amount, cb.rate_snapshot, "
                    "       cb.pending_partner_id, cb.acceptance_deadline, "
                    "       mb.total_amount AS mb_total, mb.city_id AS mb_city, "
                    "       mb.journey_start_date, "
                    "       bs.service_amount, bs.service_status "
                    "FROM cab_bookings cb "
                    "JOIN master_bookings mb ON mb.id = cb.master_booking_id "
                    "JOIN booking_services bs "
                    "  ON bs.master_booking_id = mb.id AND bs.service_type = 'CAB' "
                    "WHERE cb.id = :id"
                ),
                {"id": cab_id},
            )
        )
        .mappings()
        .one()
    )


async def _modified_note(session: AsyncSession, mb_id: int) -> str:
    return (
        await session.execute(
            text(
                "SELECT event_description FROM booking_timelines "
                "WHERE master_booking_id = :mb AND event_type = 'BOOKING_MODIFIED' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"mb": mb_id},
        )
    ).scalar_one()


def _amended_edit(category_id: int, reason: str) -> CabBookingEditByAdmin:
    """The longer trip: Bhubaneswar → Berhampur, return leg, round-trip sheet."""
    return CabBookingEditByAdmin(
        city_id=Bhubaneswar,
        destination_city_id=Berhampur,
        trip_type="ROUND_TRIP",
        vehicle_category_id=category_id,
        pickup_location="Bhubaneswar Railway Station",
        drop_location="Berhampur Bus Stand",
        pickup_datetime=AMENDED_PICKUP,
        return_datetime=AMENDED_RETURN,
        reason=reason,
    )


def _refusal_edit(category_id: int) -> CabBookingEditByAdmin:
    """The shorter trip every refusal is attempted with."""
    return CabBookingEditByAdmin(
        city_id=Bhubaneswar,
        destination_city_id=Puri,
        trip_type="ONE_WAY",
        vehicle_category_id=category_id,
        pickup_location="Bhubaneswar Railway Station",
        drop_location="Puri Grand Road",
        pickup_datetime=PICKUP,
    )


async def _open_offer(session: AsyncSession, cab_id: int, partner_id: int) -> int:
    """Put the booking back on a partner's screen with one live offer.

    The offer row is what the partner portal acts on, so an amendment has to deal
    with it explicitly — this is the state a partner is standing in when the
    customer phones back and changes the trip.
    """
    await session.execute(
        text(
            "UPDATE cab_bookings SET booking_status = "
            "              'PENDING_PARTNER_ACCEPTANCE', "
            "       pending_partner_id = :p, "
            "       acceptance_deadline = NOW() + INTERVAL '10 minutes' "
            "WHERE id = :id"
        ),
        {"id": cab_id, "p": partner_id},
    )
    offer = int(
        (
            await session.execute(
                text(
                    "INSERT INTO cab_booking_assignments "
                    "       (cab_booking_id, partner_id) "
                    "VALUES (:cb, :p) RETURNING id"
                ),
                {"cb": cab_id, "p": partner_id},
            )
        ).scalar_one()
    )
    await session.flush()
    return offer


async def _offer_row(session: AsyncSession, offer_id: int) -> dict:
    return (
        (
            await session.execute(
                text(
                    "SELECT closed_at, close_reason, accepted_at "
                    "FROM cab_booking_assignments WHERE id = :id"
                ),
                {"id": offer_id},
            )
        )
        .mappings()
        .one()
    )


async def _flow(session: AsyncSession) -> dict:
    # This suite exercises the amendment engine, not the product switch. One Way
    # is disabled in the launch configuration, so opt it back in inside this
    # transaction; the rollback at the end restores the real setting.
    await session.execute(
        text(
            "INSERT INTO system_configurations "
            "(config_key, config_value, description, updated_at) "
            "VALUES ('CAB_BOOKING_ONE_WAY_ENABLED', 'true', "
            "        'Test opt-in for amendment flow', NOW()) "
            "ON CONFLICT (config_key) DO UPDATE "
            "SET config_value = 'true', updated_at = NOW()"
        )
    )
    await session.flush()

    category_id = int(
        (
            await session.execute(
                text(
                    "SELECT vc.id FROM vehicle_categories vc "
                    "WHERE vc.id <> 3 AND COALESCE(vc.is_active, TRUE) "
                    "  AND NOT EXISTS (SELECT 1 FROM vehicle_pricing_rules r "
                    "                  WHERE r.city_id = :city "
                    "                    AND r.vehicle_category_id = vc.id) "
                    "ORDER BY vc.id LIMIT 1"
                ),
                {"city": Bhubaneswar},
            )
        ).scalar_one()
    )
    await _seed_sheet(session, category_id, ONE_WAY_SHEET)
    await _seed_sheet(session, category_id, ROUND_TRIP_SHEET)
    await _seed_far_corridor(session)
    customer_id, actor_id = await _seed_users(session)
    actor = {"sub": actor_id}

    created = await create_cab_booking_by_admin(
        CabBookingCreateByAdmin(
            customer_id=customer_id,
            city_id=Bhubaneswar,
            destination_city_id=Puri,
            trip_type="ONE_WAY",
            vehicle_category_id=category_id,
            pickup_location="Bhubaneswar Railway Station",
            drop_location="Puri Grand Road",
            pickup_datetime=PICKUP,
        ),
        db=session,
        current_user=actor,
        scope=SUPER_SCOPE,
    )
    # The handler leaves the service link and the timeline as pending ORM objects
    # until the request's own commit flushes them; this stands in for that commit
    # without one, so the amendment's UPDATE finds the rows it is meant to move.
    await session.flush()
    after_create = await _booking_rows(session, created.cab_booking_id)

    # The same call a screen would make: no `quote_id`, no distance, no amount —
    # the handler re-derives everything from the new trip and refuses to be told.
    amended = await edit_cab_booking_by_admin(
        created.cab_booking_id,
        _amended_edit(category_id, "Customer extended the trip to Berhampur"),
        db=session,
        current_user=actor,
        scope=SUPER_SCOPE,
    )
    await session.flush()
    after_edit = await _booking_rows(session, created.cab_booking_id)

    # `pickup_datetime` is a timestamptz — the row stores an instant, not a
    # wall-clock string — so the check is the database's own comparison rather
    # than one done in Python, which would depend on the machine's timezone.
    holds_new_time = bool(
        (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM cab_bookings "
                    "                WHERE id = :id AND pickup_datetime = :when)"
                ),
                {"id": created.cab_booking_id, "when": AMENDED_PICKUP},
            )
        ).scalar()
    )

    timeline = (
        (
            await session.execute(
                text(
                    "SELECT event_type, event_description, created_by "
                    "FROM booking_timelines WHERE master_booking_id = :mb "
                    "  AND event_type = 'BOOKING_MODIFIED' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"mb": created.master_booking_id},
            )
        )
        .mappings()
        .one()
    )

    # A second call that changes nothing about the journey, only the agent's note.
    # The audit sentence has to name what moved and nothing else — a trail that
    # claims the pickup time changed on every save is a trail nobody reads.
    re_saved = await edit_cab_booking_by_admin(
        created.cab_booking_id,
        CabBookingEditByAdmin(
            city_id=Bhubaneswar,
            destination_city_id=Berhampur,
            trip_type="ROUND_TRIP",
            vehicle_category_id=category_id,
            pickup_location="Bhubaneswar Railway Station",
            drop_location="Berhampur Bus Stand",
            pickup_datetime=AMENDED_PICKUP,
            return_datetime=AMENDED_RETURN,
            reason="Customer re-read the fare back to us",
        ),
        db=session,
        current_user=actor,
        scope=SUPER_SCOPE,
    )
    await session.flush()
    unchanged_note = await _modified_note(session, created.master_booking_id)
    after_re_save = await _booking_rows(session, created.cab_booking_id)

    # Refusal 1: the trip has run. Money is in play, so the fare is not editable.
    await session.execute(
        text("UPDATE cab_bookings SET booking_status = 'SETTLED' WHERE id = :id"),
        {"id": created.cab_booking_id},
    )
    settled_refusal = None
    try:
        await edit_cab_booking_by_admin(
            created.cab_booking_id,
            _refusal_edit(category_id),
            db=session,
            current_user=actor,
            scope=SUPER_SCOPE,
        )
    except HTTPException as exc:
        settled_refusal = exc

    # Refusal 2: an advance is open against the booking, at the ORIGINAL status —
    # otherwise the first gate would answer before this one is ever reached.
    await session.execute(
        text(
            "UPDATE cab_bookings SET booking_status = 'PENDING_ASSIGNMENT' WHERE id = :id"
        ),
        {"id": created.cab_booking_id},
    )
    await session.execute(
        text(
            "INSERT INTO advance_payments (cab_booking_id, master_booking_id, "
            "       booking_number, receipt_number, amount, payment_mode, "
            "       received_by, status, collected_by_user_id, collected_by_role, "
            "       collected_at) "
            "VALUES (:cb, :mb, :bn, :rn, 500.00, 'CASH', 'ADMIN', 'ACTIVE', :by, "
            "        'ADMIN', NOW())"
        ),
        {
            "cb": created.cab_booking_id,
            "mb": created.master_booking_id,
            "bn": created.cab_booking_number,
            "rn": f"WTTESTADV{uuid4().hex[:8].upper()}",
            "by": actor_id,
        },
    )
    advance_refusal = None
    try:
        await edit_cab_booking_by_admin(
            created.cab_booking_id,
            _refusal_edit(category_id),
            db=session,
            current_user=actor,
            scope=SUPER_SCOPE,
        )
    except HTTPException as exc:
        advance_refusal = exc

    # Refusal 3: a redemption rides on the booking, and the coupon engine gets a
    # vote. A code that no longer validates must stop the edit rather than let the
    # amendment carry the old discount onto a new fare — the usage row and the
    # partner/platform funding split were written for the fare the customer agreed
    # to, and `here not have adjust any commission amount` is the owner's rule.
    await session.execute(
        text("DELETE FROM advance_payments WHERE cab_booking_id = :cb"),
        {"cb": created.cab_booking_id},
    )
    coupon_code = f"WTTEST{uuid4().hex[:8].upper()}"
    coupon_id = (
        await session.execute(
            text(
                "INSERT INTO coupons (coupon_code, title, apply_to, discount_type, "
                "       discount_value, min_booking_amount, max_usage_per_customer, "
                "       valid_from, valid_to, is_active) "
                "VALUES (:code, 'Amendment test coupon', 'CAB', 'FLAT', 150.00, 0, 1, "
                "        CURRENT_DATE - 1, CURRENT_DATE + 30, FALSE) RETURNING id"
            ),
            {"code": coupon_code},
        )
    ).scalar_one()
    await session.execute(
        text(
            "UPDATE cab_bookings SET coupon_id = :cid, coupon_code = :code, "
            "       coupon_discount = 150.00 WHERE id = :id"
        ),
        {"cid": coupon_id, "code": coupon_code, "id": created.cab_booking_id},
    )
    await session.flush()
    coupon_refusal = None
    try:
        await edit_cab_booking_by_admin(
            created.cab_booking_id,
            _refusal_edit(category_id),
            db=session,
            current_user=actor,
            scope=SUPER_SCOPE,
        )
    except HTTPException as exc:
        coupon_refusal = exc
    after_coupon_refusal = await _booking_rows(session, created.cab_booking_id)

    # The partner half. A booking on a partner's screen with an offer they have not
    # answered is a commitment about a particular trip: when the edit changes what
    # the job is, the offer closes and the booking goes back for assignment. When
    # the edit changes nothing, the live offer stays where it is.
    await session.execute(
        text(
            "UPDATE cab_bookings SET coupon_id = NULL, coupon_code = NULL, "
            "       coupon_discount = 0.00 WHERE id = :id"
        ),
        {"id": created.cab_booking_id},
    )
    partner_id = (
        await session.execute(text("SELECT id FROM partners ORDER BY id LIMIT 1"))
    ).scalar()
    offer_closed = offer_kept = changed_offer_row = kept_row = None
    after_close = after_keep = None
    closed_offer_note = kept_offer_note = ""
    offer_closed = offer_kept = None
    if partner_id is not None:
        changed_offer = await _open_offer(
            session, created.cab_booking_id, int(partner_id)
        )
        offer_closed = await edit_cab_booking_by_admin(
            created.cab_booking_id,
            _refusal_edit(category_id),
            db=session,
            current_user=actor,
            scope=SUPER_SCOPE,
        )
        await session.flush()
        changed_offer_row = await _offer_row(session, changed_offer)
        closed_offer_note = await _modified_note(session, created.master_booking_id)
        after_close = await _booking_rows(session, created.cab_booking_id)

        kept_offer = await _open_offer(session, created.cab_booking_id, int(partner_id))
        offer_kept = await edit_cab_booking_by_admin(
            created.cab_booking_id,
            _refusal_edit(category_id),
            db=session,
            current_user=actor,
            scope=SUPER_SCOPE,
        )
        await session.flush()
        kept_row = await _offer_row(session, kept_offer)
        kept_offer_note = await _modified_note(session, created.master_booking_id)
        after_keep = await _booking_rows(session, created.cab_booking_id)

    return {
        "created": created,
        "amended": amended,
        "after_create": after_create,
        "after_edit": after_edit,
        "holds_new_time": holds_new_time,
        "timeline": timeline,
        "re_saved": re_saved,
        "unchanged_note": unchanged_note,
        "after_re_save": after_re_save,
        "actor_id": actor["sub"],
        "settled_refusal": settled_refusal,
        "advance_refusal": advance_refusal,
        "coupon_refusal": coupon_refusal,
        "after_coupon_refusal": after_coupon_refusal,
        "offer_closed": offer_closed,
        "changed_offer_row": changed_offer_row,
        "closed_offer_note": closed_offer_note,
        "offer_kept": offer_kept,
        "kept_row": kept_row,
        "kept_offer_note": kept_offer_note,
        "after_close": after_close,
        "after_keep": after_keep,
        "partner_id": partner_id,
    }


# ── the flow ────────────────────────────────────────────────────────────────

# Every table this file writes into. Both tests check the counts come back to
# where they started: the transaction is the test's sandbox and it is discarded.
_TRADED_TABLES = (
    "cab_bookings",
    "master_bookings",
    "booking_timelines",
    "booking_services",
    "advance_payments",
    "coupons",
    "cab_booking_assignments",
)


def _nothing_survived(before: dict) -> None:
    for table in _TRADED_TABLES:
        assert _count(table) == before[table], table


def test_an_amended_trip_is_measured_priced_and_written_as_one_number() -> None:
    before = {table: _count(table) for table in _TRADED_TABLES}
    out = _with_session(_flow)
    created, after = out["after_create"], out["after_edit"]

    # ── the booking starts as the trip that was quoted
    assert Decimal(str(created["estimated_amount"])) == ORIGINAL_TOTAL
    assert created["distance_source"] == "CONFIGURED_ROUTE"
    assert Decimal(str(created["estimated_distance"])) == Decimal("70.90")

    # ── and becomes the trip the customer asked for
    assert Decimal(str(after["estimated_amount"])) == AMENDED_TOTAL
    assert after["trip_type"] == "ROUND_TRIP"
    assert after["drop_location"] == "Berhampur Bus Stand"
    assert out["holds_new_time"] is True
    assert after["journey_start_date"] == AMENDED_PICKUP.date()
    assert after["booking_status"] == "PENDING_ASSIGNMENT"
    assert Decimal(str(after["estimated_distance"])) == Decimal("250.00")
    assert after["distance_source"] == "CONFIGURED_ROUTE"

    # The toll arrived from the corridor, not from the city's flat figure, which
    # is the owner's "some routes have a toll and it is not in the booking".
    assert Decimal(str(after["toll_amount"])) == Decimal("300.00")
    assert Decimal(str(created["toll_amount"])) == Decimal("0.00")

    # ONE_WAY and ROUND_TRIP bill the greater of the journey and the minimum, so
    # both of these are the whole distance — and the rate is the new sheet's.
    assert Decimal(str(after["billing_distance_km"])) == Decimal("250.00")
    assert Decimal(str(after["distance_charge_amount"])) == Decimal("3750.00")
    assert Decimal(str(after["driver_allowance_amount"])) == Decimal("500.00")

    # Every place money lives, one number. Four values that used to be kept by
    # four different writes and could each be a little out of date.
    assert Decimal(str(after["mb_total"])) == AMENDED_TOTAL
    assert Decimal(str(after["service_amount"])) == AMENDED_TOTAL
    lines = sum(
        (
            Decimal(str(after[k]))
            for k in (
                "base_fare_amount",
                "distance_charge_amount",
                "driver_allowance_amount",
                "night_charge_amount",
                "toll_amount",
                "waiting_charge_amount",
            )
        ),
        Decimal("0.00"),
    )
    assert lines == Decimal(str(after["estimated_amount"]))

    # The rule the new fare came from, snapshotted — so editing a rate sheet next
    # month cannot move this booking's price.
    snapshot = after["rate_snapshot"]
    assert str(snapshot["distance_km"]) == "250.00"
    assert Decimal(str(snapshot["per_km_rate"])) == Decimal("15")
    assert str(snapshot["toll"]) == "300.00"

    # The audit trail carries both fares and the reason the agent gave, so the
    # sentence "we told them 1918 and then it became 5350" has a source.
    note = out["timeline"]["event_description"]
    assert "1918.00" in note and "5350.00" in note
    assert "ONE_WAY → ROUND_TRIP" in note
    assert "Puri Grand Road → Berhampur Bus Stand" in note
    assert "Customer extended the trip to Berhampur" in note
    assert str(out["timeline"]["created_by"]) == out["actor_id"]
    # The time is named as the agent named it, not as the column stores it.
    assert "pickup moved to 12 Nov 2027 16:00" in note

    # A save that changes nothing about the journey claims nothing either, and it
    # does not drift the fare: re-reading the trip back is not a re-price.
    quiet = out["unchanged_note"]
    for claimed in ("pickup moved", "return", "trip type", "vehicle category", "drop"):
        assert claimed not in quiet, claimed
    assert "Customer re-read the fare back to us" in quiet
    assert out["re_saved"].estimated_amount == AMENDED_TOTAL
    assert out["re_saved"].re_offered is False
    assert Decimal(str(out["after_re_save"]["estimated_amount"])) == AMENDED_TOTAL

    # The endpoint's own answer matches the row it wrote, in Decimal form.
    assert out["amended"].old_amount == ORIGINAL_TOTAL
    assert out["amended"].estimated_amount == AMENDED_TOTAL
    assert out["amended"].payable_amount == AMENDED_TOTAL  # no coupon on this one
    assert isinstance(out["amended"].estimated_amount, Decimal)

    # ── the refusals, on a booking that really exists
    settled = out["settled_refusal"]
    assert isinstance(settled, HTTPException), "a settled trip must not be amendable"
    assert settled.status_code == 409
    assert "SETTLED" in settled.detail

    advance = out["advance_refusal"]
    assert isinstance(advance, HTTPException), "money collected must block an edit"
    assert advance.status_code == 409
    assert "advance" in str(advance.detail).lower()

    coupon = out["coupon_refusal"]
    assert isinstance(coupon, HTTPException), "a dead redemption must block an edit"
    assert coupon.status_code == 409
    assert "coupon" in str(coupon.detail).lower()
    # Refused means refused: the fare the customer was last quoted is still on the
    # row, with neither the old trip nor the discount rewritten.
    assert (
        Decimal(str(out["after_coupon_refusal"]["estimated_amount"])) == AMENDED_TOTAL
    )

    # Nothing survived the transaction — the row this flow booked, repriced and
    # refused three times was never in the database the owner uses.
    _nothing_survived(before)


def test_a_changed_trip_closes_the_offer_a_partner_has_not_answered() -> None:
    """A partner is shown a fare for a job. Change the job and that offer dies.

    The failure this prevents is quiet and expensive: the desk amends a booking
    that is sitting on three partners' screens, the trip grows from 71 km to a
    longer one, and a partner accepts the old price they were never re-shown —
    then bills for a job nobody quoted. So the open, unanswered offers close, the
    booking returns to PENDING_ASSIGNMENT, and the desk is told (`re_offered`)
    that it has to be assigned again.

    The second half is what stops this being a gate that only ever destroys: an
    amendment that changes nothing about the job leaves the live offer alone.
    """
    before = {table: _count(table) for table in _TRADED_TABLES}
    out = _with_session(_flow)

    if out["offer_closed"] is None:
        pytest.skip("this database has no partner to be offered the trip")

    closed = out["offer_closed"]
    assert closed.re_offered is True
    assert closed.estimated_amount == ORIGINAL_TOTAL  # the shorter, amended-to trip

    row = out["changed_offer_row"]
    assert row["closed_at"] is not None
    assert row["close_reason"] == "ADMIN_REASSIGN"
    assert row["accepted_at"] is None

    booking = out["after_close"]
    assert booking["booking_status"] == "PENDING_ASSIGNMENT"
    # Nothing is left pointing at a partner who no longer has an offer.
    assert booking["pending_partner_id"] is None
    assert booking["acceptance_deadline"] is None
    # and the audit trail says so, in the sentence the desk reads back.
    assert "outstanding partner offers closed" in out["closed_offer_note"]

    kept = out["offer_kept"]
    assert kept.re_offered is False
    assert "outstanding partner offers closed" not in out["kept_offer_note"]
    assert (
        out["kept_row"]["closed_at"] is None
    ), "a no-change edit must not close an offer"
    still = out["after_keep"]
    assert still["booking_status"] == "PENDING_PARTNER_ACCEPTANCE"
    assert int(still["pending_partner_id"]) == int(out["partner_id"])

    _nothing_survived(before)
