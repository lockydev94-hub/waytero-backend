"""Round 15 C2b — the desk's front door: one customer, however they are typed.

Two claims, both about what happens before a booking exists:

1. A support agent types the number the caller said. `+91 98765 43210`,
   `09876543210` and `9876543210` are one person, and `/lookup` compared them
   as strings — so a customer who books every week read as a stranger, and the
   desk's next move after "not found" is to register a second account for them.
2. A number that cannot be a mobile is refused rather than answered with "no
   such customer". Those are different conclusions, and only one of them tells
   the agent to check what they typed.

The branch gate on the same door gets proved here too, because it is the same
request shape: a booking for a customer this branch cannot see is refused before
anything is measured, priced or written — which is why none of these tests leave
a row behind.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import settings

API = "/api/v1"
LOOKUP = f"{API}/admin/customer-care/lookup"
CREATE = f"{API}/admin/customer-care/cab-booking"


def _dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _fetch(sql: str, *args: Any) -> list[dict]:
    """Read one fact from the database on a throwaway loop — TestClient owns the
    app engine's loop for the duration of a test, the same reason
    `test_branch_scope_api.py` connects out rather than through the app."""
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_dsn())
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _a_customer_with_a_plain_number() -> dict:
    rows = _fetch(
        """
        SELECT c.id AS customer_id, u.mobile_number
        FROM customers c
        JOIN users u ON u.id = c.user_id
        WHERE u.user_type = 'CUSTOMER'
          AND u.mobile_number ~ '^[6-9][0-9]{9}$'
        ORDER BY c.id
        LIMIT 1
        """
    )
    if not rows:
        pytest.skip("this database has no customer with a bare 10-digit mobile")
    return rows[0]


def _grouped(mobile: str) -> str:
    """The same number as a person says it: 98765 43210."""
    return f"{mobile[:5]} {mobile[5:]}"


def _desk_shows_customer(client, customer_id: int) -> bool:
    """Does this account's desk see that profile at all — the test `/lookup`
    applies, asked through the by-id route so no phone number is involved."""
    return (
        client.get(f"{API}/admin/customer-care/customer/{customer_id}").status_code
        == 200
    )


def _a_customer_the_desk_can_or_cannot_see(client, *, want_visible: bool):
    """The first profile in this database that answers the way the test needs.

    Which customer is "not ours" is a fact about the owner's data, not about the
    code, so candidates are read rather than assumed — and a database where every
    customer is visible to every branch skips instead of failing.
    """
    for row in _fetch(
        """
        SELECT c.id AS customer_id
        FROM customers c
        JOIN users u ON u.id = c.user_id
        WHERE u.user_type = 'CUSTOMER'
        ORDER BY c.id
        LIMIT 25
        """
    ):
        if _desk_shows_customer(client, row["customer_id"]) is want_visible:
            return row["customer_id"]
    return None


# ── the same customer, however the agent types them ─────────────────────────


def test_a_country_coded_search_finds_the_customer_the_bare_digits_find(
    client_super_admin,
) -> None:
    """`+91 …`, `0…` and the bare digits are one customer, and one profile.

    The assertion messages carry the status code rather than the response body:
    a successful lookup body is that customer's mobile number and email address,
    which have no business in a test log.
    """
    customer = _a_customer_with_a_plain_number()
    digits = customer["mobile_number"]

    plain = client_super_admin.get(LOOKUP, params={"mobile": digits})
    prefixed = client_super_admin.get(LOOKUP, params={"mobile": f"+91 {digits}"})
    spaced = client_super_admin.get(LOOKUP, params={"mobile": f"+91 {_grouped(digits)}"})
    trunk = client_super_admin.get(LOOKUP, params={"mobile": f"0{digits}"})

    assert plain.status_code == 200, plain.status_code
    assert plain.json()["found"] is True
    for response in (prefixed, spaced, trunk):
        # Every one of them has to reach the same profile. Answering "not found"
        # is not a neutral miss: it sends the agent to register a duplicate.
        assert response.status_code == 200, response.status_code
        data = response.json()
        assert data["found"] is True
        assert data["customer_id"] == plain.json()["customer_id"]


def test_a_number_that_is_not_a_mobile_is_refused_not_answered_as_absent(
    client_super_admin,
) -> None:
    """`found: false` means "nobody here has that number".

    For a string that is not a number at all, that claim is unsupported — the
    honest answer is that the input cannot be searched, so the agent checks it
    instead of booking a walk-in as a new customer.
    """
    response = client_super_admin.get(LOOKUP, params={"mobile": "1234567890"})

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert "10-digit" in body["message"]


def test_a_number_truncated_after_the_country_code_is_refused(
    client_super_admin,
) -> None:
    """`+91 98765 432` is nine digits with a country code on it.

    Drop the code and the remaining characters are ten digits starting with 9 —
    the shape of somebody else's real mobile. A `+` commits the caller to the
    full length, so this is refused instead of searching for a stranger.
    """
    response = client_super_admin.get(LOOKUP, params={"mobile": "+91 98765 432"})

    assert response.status_code == 400


# ── the door is still a branch's door ───────────────────────────────────────


def test_the_desk_books_for_its_own_customers_only(client_cuttack_cco) -> None:
    """The booking used to select `customers` by id with no branch test at all.

    A Cuttack agent who had a Bhubaneswar customer's id could file a booking that
    `/lookup` would refuse to show them — invisible to the branch that has to
    service it. The refusal is 404, not 403: an out-of-scope profile must not be
    confirmable by id.
    """
    outsider = _a_customer_the_desk_can_or_cannot_see(
        client_cuttack_cco, want_visible=False
    )
    if outsider is None:
        pytest.skip("every customer in this database is in Cuttack's book")

    response = client_cuttack_cco.post(
        CREATE,
        json={
            "customer_id": outsider,
            "vehicle_category_id": 1,
            "trip_type": "LOCAL",
            "pickup_location": "Cuttack Bus Stand",
            "drop_location": "Cuttack Barabati Stadium",
            "pickup_datetime": "2026-11-04T14:30:00",
        },
    )

    assert response.status_code == 404, response.text
    # Refusing here means nothing downstream ran: no distance measured, no fare
    # sheet read, no booking number consumed.


def test_the_gate_does_not_refuse_the_branchs_own_customer(client_cuttack_cco) -> None:
    """A gate that only ever says no is indistinguishable from a broken gate.

    The in-scope profile has to reach the pricing stage, which is where this
    request stops on purpose: no cab category in the world is 999999, so the fare
    engine refuses and no booking is written either way.
    """
    mine = _a_customer_the_desk_can_or_cannot_see(
        client_cuttack_cco, want_visible=True
    )
    if mine is None:
        pytest.skip("Cuttack's book has no customer in this database")

    response = client_cuttack_cco.post(
        CREATE,
        json={
            "customer_id": mine,
            "vehicle_category_id": 999999,
            "trip_type": "LOCAL",
            "pickup_location": "Cuttack Bus Stand",
            "drop_location": "Cuttack Barabati Stadium",
            "pickup_datetime": "2026-11-04T14:30:00",
        },
    )

    assert response.status_code != 404, response.text
    body = response.json()
    assert body["success"] is False
