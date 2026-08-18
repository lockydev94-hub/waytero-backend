# ============================================================
# WAY TERO — REALTIME CONNECTION MANAGER
# File: app/modules/notification/realtime/connection_manager.py
# Doc Ref: BRD Part 7 §155 — WebSocket / Realtime channel
#
# In-memory per-user WebSocket registry. One user may have N connections
# (multiple tabs, mobile + web). Messages dispatched to a user fan out
# to all of their connections.
#
# Designed for a single FastAPI worker process — for production
# horizontally-scaled deployments swap the storage layer for Redis
# pub/sub (the interface here is intentionally narrow so it's a
# 30-line change).
# ============================================================

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Iterable
from uuid import UUID

from fastapi import WebSocket

logger = logging.getLogger("waytero.realtime")


class ConnectionManager:
    """
    Tracks active WebSocket connections keyed by user_id.

    Usage:
        manager.connect(user_id, ws)
        await manager.send_to_user(user_id, {"event": "...", "data": ...})
        manager.disconnect(user_id, ws)
    """

    def __init__(self) -> None:
        # user_id -> set of active WebSocket objects (a user may be in
        # multiple tabs / devices)
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, user_id: UUID | str, ws: WebSocket) -> None:
        """Register a connected websocket for the given user."""
        key = str(user_id)
        async with self._lock:
            self._connections[key].add(ws)
        logger.info(
            "ws.connected user=%s total_for_user=%d", key, len(self._connections[key])
        )

    def disconnect(self, user_id: UUID | str, ws: WebSocket) -> None:
        """Drop a websocket from the registry. Safe to call twice."""
        key = str(user_id)
        conns = self._connections.get(key)
        if conns is None:
            return
        conns.discard(ws)
        if not conns:
            self._connections.pop(key, None)
        logger.info(
            "ws.disconnected user=%s remaining_for_user=%d",
            key,
            len(self._connections.get(key, ())),
        )

    def is_online(self, user_id: UUID | str) -> bool:
        return bool(self._connections.get(str(user_id)))

    def online_user_ids(self) -> Iterable[str]:
        return list(self._connections.keys())

    async def send_to_user(self, user_id: UUID | str, payload: dict[str, Any]) -> int:
        """
        Deliver payload (dict) to every open connection for `user_id`.
        Returns the number of connections the message reached. Dead
        connections are silently pruned.
        """
        key = str(user_id)
        conns = list(self._connections.get(key, ()))
        if not conns:
            return 0

        delivered = 0
        for ws in conns:
            try:
                await ws.send_json(payload)
                delivered += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ws.send_failed user=%s err=%s; pruning", key, exc)
                self.disconnect(key, ws)
        return delivered

    async def broadcast(self, payload: dict[str, Any]) -> int:
        """Deliver payload to every connected user (e.g. admin announcements)."""
        total = 0
        for key in list(self._connections.keys()):
            total += await self.send_to_user(key, payload)
        return total

    def stats(self) -> dict[str, int]:
        """Snapshot for /health and debug panels."""
        return {
            "users_online": len(self._connections),
            "connections_total": sum(len(c) for c in self._connections.values()),
        }


# Singleton — one manager per worker process. Imported by main.py to
# attach to app.state so the WS endpoint can access it without
# re-instantiation, and by the notification service to publish events.
manager = ConnectionManager()
