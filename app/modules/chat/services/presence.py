# ============================================================
# WAY TERO — LIVE CHAT: SUPPORT PRESENCE (SMART ROUTING)
# File: app/modules/chat/services/presence.py
# Doc Ref: Website Chat §3 — Smart routing
#
# Who is "support online" is the answer to a single fast cache read.
# The admin portal heartbeats its open/away status here (POST
# /admin/chat/presence every ~30s + on tab close). A Redis hash maps
# admin user_id -> last heartbeat epoch; an admin counts as online if
# their last heartbeat is within PRESENCE_TTL.
#
# The customer-facing widget calls is_support_online() before offering
# a live chat, and conversation-start uses online_admin_ids() to decide
# OPEN vs WAITING and to pick an assigned agent.
#
# All functions degrade gracefully to "nobody online" if Redis is
# unreachable — chat still works, it just starts in offline mode.
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Optional

from app.infrastructure.cache.redis_client import get_redis

logger = logging.getLogger("waytero.chat.presence")

_PRESENCE_KEY = "chat:admins_presence"
PRESENCE_TTL_SECONDS = 90  # must be > the portal's heartbeat interval (30s)


async def mark_presence(admin_user_id: str, status: str) -> None:
    """Record (or clear) an admin's online status."""
    try:
        redis = await get_redis()
        if status == "offline":
            await redis.hdel(_PRESENCE_KEY, str(admin_user_id))
        else:
            await redis.hset(_PRESENCE_KEY, str(admin_user_id), str(time.time()))
    except Exception as exc:  # pragma: no cover - Redis down
        logger.warning("chat.presence_unavailable err=%s", exc)


async def online_admin_ids() -> list[str]:
    """User-ids of admins whose heartbeat is fresh (within TTL)."""
    try:
        redis = await get_redis()
        raw = await redis.hgetall(_PRESENCE_KEY)
    except Exception as exc:  # pragma: no cover - Redis down
        logger.warning("chat.presence_unavailable err=%s", exc)
        return []

    cutoff = time.time() - PRESENCE_TTL_SECONDS
    return [uid for uid, ts in raw.items() if _ts_fresh(ts, cutoff)]


def _ts_fresh(value: str, cutoff: float) -> bool:
    try:
        return float(value) >= cutoff
    except (TypeError, ValueError):
        return False


async def is_support_online() -> bool:
    return bool(await online_admin_ids())


async def pick_agent() -> Optional[str]:
    """Least-fresh agent is a poor load-balancer; we simply pick the
    first online admin deterministically so the same admin owns threads
    started around the same time. Returns None when nobody is online."""
    online = await online_admin_ids()
    return sorted(online)[0] if online else None
