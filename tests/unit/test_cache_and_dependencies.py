from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core.dependencies import get_current_user
from app.core.security import create_access_token
from app.infrastructure.cache.redis_client import RedisCache
from app.modules.auth.models.user import User
from app.shared.enums.user_types import UserStatus, UserType


class StubRedis:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def setex(self, key: str, ttl: int, value: object) -> bool:
        self.store[key] = value
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    async def incr(self, key: str) -> int:
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = value
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return 30


@pytest.mark.asyncio
async def test_redis_cache_helpers_round_trip() -> None:
    cache = RedisCache(StubRedis())

    await cache.set("profile", {"id": 1}, ttl_seconds=60)
    await cache.set_otp("9876543210", "hashed-otp")

    assert await cache.get("profile") == {"id": 1}
    assert await cache.get_otp("9876543210") == "hashed-otp"
    assert await cache.increment("rate:login", ttl_seconds=60) == 1
    assert await cache.ttl("rate:login") == 30
    assert await cache.delete("profile") is True


@pytest.mark.asyncio
async def test_get_current_user_returns_payload_for_active_user(monkeypatch) -> None:
    user_id = uuid4()
    token = create_access_token(
        {
            "sub": str(user_id),
            "role": "PARTNER",
            "roles": ["PARTNER"],
            "permissions": ["auth.self.read"],
            "session_id": str(uuid4()),
        }
    )

    class RepoStub:
        async def get_by_id(self, identifier):
            return User(
                id=user_id,
                mobile_number="9876543210",
                user_type=UserType.PARTNER,
                status=UserStatus.ACTIVE,
                failed_login_attempts=0,
                is_active=True,
                is_mobile_verified=True,
                is_email_verified=False,
            )

    monkeypatch.setattr(
        "app.modules.auth.repositories.UserRepository", lambda db: RepoStub()
    )

    payload = await get_current_user(
        request=SimpleNamespace(state=SimpleNamespace(user_payload=None)),
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db=SimpleNamespace(),
    )

    assert payload["sub"] == str(user_id)
    assert payload["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_get_current_user_rejects_inactive_user(monkeypatch) -> None:
    user_id = uuid4()
    token = create_access_token(
        {
            "sub": str(user_id),
            "role": "PARTNER",
            "roles": ["PARTNER"],
            "permissions": ["auth.self.read"],
            "session_id": str(uuid4()),
        }
    )

    class RepoStub:
        async def get_by_id(self, identifier):
            return User(
                id=user_id,
                mobile_number="9876543210",
                user_type=UserType.PARTNER,
                status=UserStatus.SUSPENDED,
                failed_login_attempts=0,
                is_active=False,
                is_mobile_verified=True,
                is_email_verified=False,
            )

    monkeypatch.setattr(
        "app.modules.auth.repositories.UserRepository", lambda db: RepoStub()
    )

    with pytest.raises(HTTPException):
        await get_current_user(
            request=SimpleNamespace(state=SimpleNamespace(user_payload=None)),
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token
            ),
            db=SimpleNamespace(),
        )
