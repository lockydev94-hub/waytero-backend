# ============================================================
# WAY TERO — WEBSOCKET ENDPOINT
# File: app/modules/notification/realtime/ws_endpoint.py
# Doc Ref: BRD Part 7 §155 — Realtime channel
#
# Path: /ws?token=<JWT>
#
# The browser uses the standard WebSocket API which can't set the
# Authorization header, so we accept the JWT via a `token` query param
# and decode it the same way HTTP requests do.
#
# After the handshake the server starts a per-connection coroutine that
# listens for client messages (we only care about a few control types)
# AND pings the client every `WEBSOCKET_PING_INTERVAL` seconds to
# detect dead connections — uvicorn drops the socket on its side after
# the ping times out.
#
# Server -> client message envelope (always):
#     {"event": "<event_type>", "data": { ... }, "ts": "<ISO timestamp>"}
#
# Recognised client -> server messages:
#   {"type": "ping"}              -> replies {"type": "pong"}
#   {"type": "subscribe", ...}    -> reserved for future channel routing
# ============================================================

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.security import decode_access_token
from app.modules.auth.models.user import User
from app.modules.notification.realtime import manager

logger = logging.getLogger("waytero.ws")

router = APIRouter()


async def _resolve_user(db: AsyncSession, user_id: str) -> User | None:
    try:
        uid = UUID(user_id)
    except (ValueError, TypeError):
        return None
    return (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()


def _envelope(event: str, data: dict) -> dict:
    return {
        "event": event,
        "data": data,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    """
    Long-lived realtime connection. Auth = JWT in query string.

    Lifecycle:
      1. Decode JWT — reject the handshake with 1008 if invalid.
      2. Verify the user still exists & is active — same rule.
      3. accept() the WS, register with ConnectionManager.
      4. Loop: await incoming text/JSON messages. Respond to ping with pong.
      5. Server-initiated pings every WEBSOCKET_PING_INTERVAL seconds.
      6. On WebSocketDisconnect, deregister.
    """
    payload = decode_access_token(token)
    if not payload:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user_id_str = payload.get("sub")
    if not user_id_str:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    async with AsyncSessionLocal() as db:
        user = await _resolve_user(db, user_id_str)
        if not user or not user.is_active:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    await manager.connect(user_id_str, websocket)
    await websocket.send_json(_envelope("connected", {"user_id": user_id_str}))

    # Read WEBSOCKET_PING_INTERVAL from DB on connect; default 25s.
    # (We don't re-read on every tick to avoid hammering system_configurations.)
    ping_interval = 25
    async with AsyncSessionLocal() as db:
        from app.modules.admin.models import SystemConfiguration

        row = (
            await db.execute(
                select(SystemConfiguration.config_value).where(
                    SystemConfiguration.config_key == "WEBSOCKET_PING_INTERVAL"
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            try:
                ping_interval = max(5, int(str(row).strip()))
            except (TypeError, ValueError):
                pass

    async def ping_loop() -> None:
        try:
            while True:
                await asyncio.sleep(ping_interval)
                await websocket.send_json({"type": "ping"})
        except (asyncio.CancelledError, WebSocketDisconnect):
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("ws.ping_loop_err user=%s err=%s", user_id_str, exc)
            return

    ping_task = asyncio.create_task(ping_loop())

    try:
        while True:
            # The server doesn't expect client -> server business traffic
            # today; we only care about keepalive. text/json are both
            # handled by .receive() so the client can send plain strings.
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"type": "raw"}

            mtype = msg.get("type") if isinstance(msg, dict) else None
            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
            elif mtype == "pong":
                continue
            elif mtype == "subscribe":
                # Reserved for future per-booking / per-channel subscription.
                # Acknowledge for now so the client gets feedback.
                await websocket.send_json(
                    _envelope("subscribed", {"channel": msg.get("channel")})
                )
            # Any other message types are silently ignored — server
            # publishes everything; clients receive, they don't send.
    except WebSocketDisconnect:
        pass
    finally:
        ping_task.cancel()
        manager.disconnect(user_id_str, websocket)
        logger.info("ws.closed user=%s", user_id_str)
