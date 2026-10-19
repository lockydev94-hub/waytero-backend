"""
The sidebar badge endpoint (Admin Portal §5 — Operations).

`GET /admin/action-counts` is one call for eleven queues. What is worth pinning
down is the shape the client indexes by, and the two queues that are
deliberately missing: a badge has to mean "items waiting on somebody", so
hotel switches (an append-only audit, nothing ever waits) and settlements (the
writer still writes `status` while every reader filters `settlement_status`)
stay uncounted until those facts change. An invented number is worse than no
number.
"""
import asyncio

import redis.asyncio as aioredis

from app.core.config import settings
from app.modules.admin.services.dashboard import ACTION_COUNTS_CACHE_KEY

COUNTS = "/api/v1/admin/action-counts"

NAV_KEYS = {
    "bookings",
    "cab-ops",
    "trip-assistance",
    "customer-care",
    "chat",
    "callback-requests",
    "contact-messages",
    "enquiries",
    "partner-applications",
    "guest-feedback",
    "account-deletion",
}


def _drop_cached_counts() -> None:
    """Force the query itself to run — a warm cache would hide a bad fragment."""

    async def drop() -> None:
        client = await aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
        await client.delete(ACTION_COUNTS_CACHE_KEY)
        await client.aclose()

    asyncio.run(drop())


def test_action_counts_returns_every_nav_key_as_a_number(client_staff) -> None:
    response = client_staff.get(COUNTS)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == NAV_KEYS
    assert all(isinstance(value, int) and value >= 0 for value in body.values())


def test_action_counts_omits_queues_without_a_pending_state(client_staff) -> None:
    body = client_staff.get(COUNTS).json()

    assert "hotel-switches" not in body
    assert "settlements" not in body


def test_every_fragment_runs_against_the_real_schema(client_staff) -> None:
    _drop_cached_counts()

    assert set(client_staff.get(COUNTS).json()) == NAV_KEYS


def test_action_counts_is_not_for_partners(client) -> None:
    assert client.get(COUNTS).status_code == 403
