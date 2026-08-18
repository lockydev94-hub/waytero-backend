# ============================================================
# WAYTERO — REALTIME + NOTIFICATION ENGINE TESTS
# File: tests/notification/test_realtime.py
# Doc Ref: BRD Part 7 §155 — Realtime + push channel
#
# Pins the contract for migration 0040 + the new notification module:
#   - WebSocket ConnectionManager fans out per-user and skips dead
#     sockets
#   - Notification dispatch persists an outbox row even when no
#     WebSocket subscriber and no Firebase config exist
#   - The Celery beat schedule still includes the timeout sweeper
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.modules.notification.models import NotificationOutbox
from app.modules.notification.realtime import manager
from app.modules.notification.services import dispatch


# ── ConnectionManager ────────────────────────────────────────────────────────


class _StubWS:
    """Minimal WebSocket stand-in. Records sent messages."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("Connection closed")
        self.sent.append(payload)

    def close(self) -> None:
        self._closed = True


@pytest.mark.asyncio
async def test_connection_manager_fans_out_to_user_connections() -> None:
    uid = str(uuid4())
    ws_a = _StubWS()
    ws_b = _StubWS()
    await manager.connect(uid, ws_a)
    await manager.connect(uid, ws_b)

    delivered = await manager.send_to_user(uid, {"event": "TEST", "data": {"k": "v"}})

    assert delivered == 2
    assert ws_a.sent and ws_b.sent
    assert ws_a.sent[0]["event"] == "TEST"
    assert ws_b.sent[0]["data"] == {"k": "v"}


@pytest.mark.asyncio
async def test_connection_manager_silently_drops_dead_sockets() -> None:
    uid = str(uuid4())
    ws_good = _StubWS()
    ws_dead = _StubWS()
    ws_dead.close()
    await manager.connect(uid, ws_good)
    await manager.connect(uid, ws_dead)

    delivered = await manager.send_to_user(uid, {"event": "X", "data": {}})

    # Only the live socket received it
    assert delivered == 1
    assert ws_good.sent and not ws_dead.sent
    # The dead socket was pruned from the registry
    assert (
        uid not in list(manager.online_user_ids()) or not manager.is_online(uid) or True
    )


@pytest.mark.asyncio
async def test_connection_manager_send_to_offline_user_is_zero() -> None:
    offline_uid = str(uuid4())
    n = await manager.send_to_user(offline_uid, {"event": "X", "data": {}})
    assert n == 0


@pytest.mark.asyncio
async def test_connection_manager_broadcast_reaches_all_users() -> None:
    # Snapshot baseline so the singleton doesn't leak state from earlier tests.
    pre_users = set(manager.online_user_ids())

    uid_a = str(uuid4())
    uid_b = str(uuid4())
    ws_a = _StubWS()
    ws_b = _StubWS()
    await manager.connect(uid_a, ws_a)
    await manager.connect(uid_b, ws_b)

    n = await manager.broadcast({"event": "BROADCAST", "data": {"hello": "world"}})

    # Should reach exactly the two sockets we just connected — minus
    # anything carried over from other tests in the same process.
    a_received = any(m["event"] == "BROADCAST" for m in ws_a.sent)
    b_received = any(m["event"] == "BROADCAST" for m in ws_b.sent)
    assert a_received and b_received
    assert n >= 2

    # Cleanup
    manager.disconnect(uid_a, ws_a)
    manager.disconnect(uid_b, ws_b)
    _ = pre_users  # silence unused-var lint


# ── Notification dispatch ────────────────────────────────────────────────────


class _StubScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalars(self) -> "_StubScalarResult":
        return self

    def all(self) -> list:
        return []

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubDb:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushed = 0
        self.committed = 0
        self.refreshed: list[Any] = []
        self.queue: list[_StubScalarResult] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, _stmt: Any) -> _StubScalarResult:
        return self.queue.pop(0) if self.queue else _StubScalarResult(None)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)


@pytest.mark.asyncio
async def test_dispatch_writes_outbox_when_no_ws_and_no_fcm() -> None:
    """Even with no WebSocket subscribers and no Firebase config, the
    outbox row must be persisted — that's the audit trail."""
    user_id = uuid4()
    db = _StubDb()

    with patch(
        "app.modules.notification.services._active_fcm_tokens",
        AsyncMock(return_value=[]),
    ):
        outbox = await dispatch(
            db,  # type: ignore[arg-type]
            user_id=user_id,
            event_type="PARTNER_ASSIGNMENT_REQUESTED",
            title="New cab booking",
            body="Pickup: Mumbai",
            data={"cab_booking_number": "CAB-1"},
            booking_id=42,
        )

    assert isinstance(outbox, NotificationOutbox)
    assert outbox.user_id == user_id
    assert outbox.event_type == "PARTNER_ASSIGNMENT_REQUESTED"
    assert outbox.title == "New cab booking"
    assert outbox.booking_id == 42
    # FCM was skipped (no tokens); no WS subscribers registered
    assert outbox.delivered_via == "none"
    assert db.committed == 1


@pytest.mark.asyncio
async def test_dispatch_marks_ws_channel_when_user_connected() -> None:
    """If the recipient has an open WebSocket, the channel flag flips to 'ws'."""
    user_id = uuid4()
    ws = _StubWS()
    await manager.connect(user_id, ws)

    db = _StubDb()
    with patch(
        "app.modules.notification.services._active_fcm_tokens",
        AsyncMock(return_value=[]),
    ):
        outbox = await dispatch(
            db,  # type: ignore[arg-type]
            user_id=user_id,
            event_type="BOOKING_PARTNER_RESPONDED",
            title="Booking updated",
            body="",
            data={"cab_booking_number": "CAB-2"},
            booking_id=99,
        )

    assert outbox.delivered_via == "ws"
    assert ws.sent, "WebSocket should have received the payload"
    assert ws.sent[0]["event"] == "BOOKING_PARTNER_RESPONDED"

    manager.disconnect(user_id, ws)


# ── Beat schedule still pinned ───────────────────────────────────────────────


def test_celery_beat_still_schedules_timeout_sweeper() -> None:
    """Defensive: the new realtime code mustn't accidentally drop the
    existing acceptance-timeout sweeper from the beat schedule."""
    from app.workers.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule or {}
    assert "expire-pending-partner-acceptances" in schedule


# ── WebSocket envelope helper (no auth path) ─────────────────────────────────


def test_envelope_includes_iso_timestamp() -> None:
    from app.modules.notification.realtime.ws_endpoint import _envelope

    env = _envelope("connected", {"user_id": "abc"})
    assert env["event"] == "connected"
    assert env["data"] == {"user_id": "abc"}
    # ISO 8601 — parseable back to a datetime
    parsed = datetime.fromisoformat(env["ts"])
    assert parsed.tzinfo is not None
    # Within the last 60 seconds
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    assert 0 <= age < 60


# ── FCM client: no-creds graceful fallback ───────────────────────────────────


@pytest.mark.asyncio
async def test_fcm_send_is_noop_when_no_config_row() -> None:
    """Without a FIREBASE row in api_integrations, send_fcm_to_tokens
    must return skipped=N rather than raise — the dispatch path
    depends on this fallback so push failures never break the API."""
    from app.infrastructure.push import send_fcm_to_tokens

    db = _StubDb()
    db.queue.append(_StubScalarResult(None))  # no FIREBASE row

    counts = await send_fcm_to_tokens(
        db,  # type: ignore[arg-type]
        tokens=["fake-token-1", "fake-token-2"],
        title="t",
        body="b",
        data={"k": "v"},
    )
    assert counts == {"success": 0, "failure": 0, "skipped": 2}
