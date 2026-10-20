# ============================================================
# WAYTERO — PHONE-DESK CAB AMENDMENT GATE TESTS
# File: tests/api/test_cab_amendment_api.py
# Doc Ref: Round 15 plan C2c (the desk changes a booking that already exists).
#
# The flow — a trip re-measured, re-priced, written and audited as one number —
# is proved in `tests/integration/test_cab_amendment_flow.py`, where the whole
# thing runs inside a transaction that is rolled back. What is left to prove here
# is the shape of the door, which a rolled-back session cannot show:
#
# 1. The route is *declared* scoped. Undeclared, a branch desk gets
#    `BRANCH_SCOPE_UNDECLARED` on the whole screen and the amendment is dead for
#    the offices that need it most; declared `global`, and it would read another
#    city's bookings.
# 2. A booking this branch cannot see is refused as though it did not exist, and
#    refused *before* anything is measured or written — the desk must not be able
#    to probe another office's fares by id.
# 3. The body the desk sends describes the trip and carries no price. A screen
#    that could post `estimated_amount` would be a screen where the fare is
#    whatever the agent typed.
# ============================================================
from __future__ import annotations

from typing import Any

import pytest

from app.core.branch_scope_registry import DECLARED
from app.core.config import settings
from app.modules.admin.customer_care_api import CabBookingEditByAdmin

API = "/api/v1"
EDIT = f"{API}/admin/customer-care/cab-booking"

# Cuttack's own city id — the branch the desk in these tests belongs to.
Cuttack = 3

ROUTE_KEY = "PUT /admin/customer-care/cab-booking/{cab_booking_id}"


def _dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _fetch(sql: str, *args: Any) -> list[dict]:
    """Read one fact from the database on a throwaway loop — TestClient owns the
    app engine's loop for the duration of a test, the same reason
    `test_customer_care_lookup_api.py` connects out rather than through the app."""
    import asyncio

    import asyncpg

    async def run():
        conn = await asyncpg.connect(_dsn())
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _a_booking_from_another_city() -> dict | None:
    """The first cab booking whose master booking is priced in a city that is not
    Cuttack's. Which one is "not ours" is a fact about the owner's data, so it is
    read rather than assumed; a database with no such booking skips."""
    rows = _fetch(
        """
        SELECT cb.id AS cab_id, cb.estimated_amount, cb.drop_location,
               cb.trip_type, mb.city_id
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        WHERE mb.city_id IS NOT NULL AND mb.city_id <> $1
        ORDER BY cb.id
        LIMIT 1
        """,
        Cuttack,
    )
    return rows[0] if rows else None


def _edit_body(**overrides) -> dict:
    """A complete, valid amendment for a trip nobody is going to be charged for —
    the gates under test all answer before the fare engine is reached."""
    body = {
        "trip_type": "LOCAL",
        "vehicle_category_id": 1,
        "pickup_location": "Cuttack Barabati Stadium",
        "drop_location": "Cuttack Bus Stand",
        "pickup_datetime": "2026-11-04T14:30:00",
    }
    body.update(overrides)
    return body


# ── the route is a branch's route, reviewed as such ─────────────────────────


def test_the_amendment_is_declared_scoped_for_branch_desks() -> None:
    """Neither undeclared nor global, both of which are wrong in opposite ways.

    Undeclared: the gate refuses `client_cuttack_cco` before the handler runs and
    the desk loses the feature. Global: the route would be allowed to read a
    booking no branch is accountable for servicing.
    """
    assert ROUTE_KEY in DECLARED, ROUTE_KEY
    assert DECLARED[ROUTE_KEY] == "scoped", DECLARED[ROUTE_KEY]


# ── another office's booking is not here ───────────────────────────────────


def test_a_branch_desk_cannot_amend_another_citys_trip(client_cuttack_cco) -> None:
    """404 on the row gate, not 403: an out-of-scope id must not be confirmable.

    The important half of the claim is what did not happen. The gate sits in
    front of the distance lookup and the price, so a Cuttack agent with a
    Bhubaneswar booking id learns nothing about that trip's fare — and the row is
    untouched afterwards.
    """
    outsider = _a_booking_from_another_city()
    if outsider is None:
        pytest.skip("this database has no cab booking outside Cuttack")

    response = client_cuttack_cco.put(f"{EDIT}/{outsider['cab_id']}", json=_edit_body())

    assert response.status_code == 404, response.status_code

    still = _fetch(
        """
        SELECT cb.estimated_amount, cb.drop_location, cb.trip_type
        FROM cab_bookings cb WHERE cb.id = $1
        """,
        outsider["cab_id"],
    )[0]
    assert still == {
        "estimated_amount": outsider["estimated_amount"],
        "drop_location": outsider["drop_location"],
        "trip_type": outsider["trip_type"],
    }


def test_an_id_that_is_not_a_booking_answers_the_same_way(client_super_admin) -> None:
    """A miss and an out-of-scope row are one answer, so ids cannot be probed.

    The head office scope passes every row test, so a 404 here can only be the
    missing record — nothing was priced and nothing was written for it.
    """
    gap = _fetch("SELECT COALESCE(MAX(id), 0) + 1000 AS id FROM cab_bookings")[0]["id"]

    response = client_super_admin.put(f"{EDIT}/{gap}", json=_edit_body())

    assert response.status_code == 404, response.status_code


# ── the body is the trip, never the price ──────────────────────────────────


def test_the_amendment_body_names_the_trip_and_no_money() -> None:
    """The fare engine is the only thing that puts a number on a booking.

    Every field here is a fact about the journey (where, when, which vehicle,
    which city) or a note for the audit trail. A distance, an amount or a coupon
    in this schema would be a desk that prices by typing.
    """
    fields = set(CabBookingEditByAdmin.model_fields)

    assert fields <= {
        "city_id",
        "destination_city_id",
        "trip_type",
        "vehicle_category_id",
        "pickup_location",
        "pickup_latitude",
        "pickup_longitude",
        "drop_location",
        "drop_latitude",
        "drop_longitude",
        "pickup_datetime",
        "return_datetime",
        "waiting_minutes",
        "reason",
    }, fields
    for forbidden in ("distance", "amount", "coupon", "customer", "total", "fare"):
        assert not any(forbidden in name for name in fields), forbidden


def test_a_client_that_sends_a_price_cannot_get_it_into_the_body() -> None:
    """The screen is not the only caller, and the handler reads the model.

    Extras are dropped at the boundary, so `estimated_amount` from any client —
    a hacked console included — never reaches the code that writes the row.
    """
    parsed = CabBookingEditByAdmin(
        **_edit_body(estimated_amount=1, distance_km=999, coupon_id=7, customer_id=3)
    ).model_dump()

    assert set(parsed) <= set(CabBookingEditByAdmin.model_fields)
    assert "estimated_amount" not in parsed and "coupon_id" not in parsed
