# ============================================================
# WAY TERO — REDIS CACHE CLIENT
# File: app/infrastructure/cache/redis_client.py
# Doc Ref: System Architecture Section 11 — Caching Strategy
# Used for: OTP (5min TTL), Driver Location, Pricing Cache
# ============================================================

import json
from typing import Any, Optional
import redis.asyncio as aioredis
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


class RedisCache:
    """
    WayTero Redis cache helper.
    Wraps common operations used across services.
    """

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    # --- OTP (Doc: 5 Minute TTL)
    async def set_otp(self, phone: str, otp: str) -> bool:
        key = f"otp:{phone}"
        ttl = settings.OTP_EXPIRE_MINUTES * 60
        return await self.redis.setex(key, ttl, otp)

    async def get_otp(self, phone: str) -> Optional[str]:
        return await self.redis.get(f"otp:{phone}")

    async def delete_otp(self, phone: str) -> bool:
        return bool(await self.redis.delete(f"otp:{phone}"))

    # --- Generic cache
    async def set(self, key: str, value: Any, ttl_seconds: int = 300) -> bool:
        return await self.redis.setex(key, ttl_seconds, json.dumps(value))

    async def get(self, key: str) -> Optional[Any]:
        val = await self.redis.get(key)
        if val:
            return json.loads(val)
        return None

    async def delete(self, key: str) -> bool:
        return bool(await self.redis.delete(key))

    async def exists(self, key: str) -> bool:
        return bool(await self.redis.exists(key))

    async def increment(self, key: str, ttl_seconds: int) -> int:
        value = await self.redis.incr(key)
        if int(value) == 1:
            await self.redis.expire(key, ttl_seconds)
        return int(value)

    async def ttl(self, key: str) -> int:
        return int(await self.redis.ttl(key))
