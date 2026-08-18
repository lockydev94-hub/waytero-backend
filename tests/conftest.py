# ruff: noqa: E402

from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.dependencies import get_current_user
from app.core.database import engine as app_engine
from app.main import create_application
from app.modules.auth.api import get_auth_service, get_otp_service
from app.modules.auth.schemas import (
    AuthUserResponse,
    LoginResponse,
    OTPSentResponse,
    RefreshResponse,
)


class StubOTPService:
    async def send_otp(self, mobile: str, purpose: str = "LOGIN") -> OTPSentResponse:
        return OTPSentResponse(mobile_number=mobile, expires_in_seconds=300)


class StubAuthService:
    def __init__(self) -> None:
        self.user_id = uuid4()

    def _login_response(
        self, user_type: str, session_id: UUID | None = None
    ) -> LoginResponse:
        return LoginResponse(
            user=AuthUserResponse(
                id=self.user_id,
                mobile="9876543210",
                email="ops@waytero.test",
                full_name=f"{user_type.title()} User",
                user_type=user_type,
                status="ACTIVE",
                roles=[user_type],
                permissions=["auth.self.read", "auth.sessions.read"],
                is_mobile_verified=True,
                is_email_verified=True,
            ),
            tokens={
                "access_token": f"{user_type.lower()}-access-token",
                "refresh_token": f"{user_type.lower()}-refresh-token",
                "expires_in": 900,
            },
            session_id=session_id or uuid4(),
        )

    async def customer_otp_login(self, **_: object) -> LoginResponse:
        return self._login_response("CUSTOMER")

    async def driver_otp_login(self, **_: object) -> LoginResponse:
        return self._login_response("DRIVER")

    async def partner_password_login(self, **_: object) -> LoginResponse:
        return self._login_response("PARTNER")

    async def partner_otp_login(self, **_: object) -> LoginResponse:
        return self._login_response("PARTNER")

    async def admin_password_login(self, **_: object) -> LoginResponse:
        return self._login_response("ADMIN")

    async def admin_otp_login(self, **_: object) -> LoginResponse:
        return self._login_response("ADMIN")

    async def refresh_access_token(self, refresh_token: str) -> RefreshResponse:
        return RefreshResponse(
            access_token=f"refreshed::{refresh_token}",
            expires_in=900,
            permissions=["auth.self.read"],
            roles=["PARTNER"],
        )

    async def logout(self, **_: object) -> None:
        return None

    async def list_sessions(self, user_id: UUID) -> list[SimpleNamespace]:
        now = datetime.now(timezone.utc)
        return [
            SimpleNamespace(
                id=uuid4(),
                device_name="Chrome",
                device_os="Windows",
                ip_address="127.0.0.1",
                user_agent="pytest",
                is_active=True,
                created_at=now,
                last_used_at=now,
                expires_at=now + timedelta(days=30),
            )
        ]

    async def revoke_session(self, **_: object) -> None:
        return None

    async def change_password(self, **_: object) -> None:
        return None

    async def initiate_password_reset(self, identifier: str) -> dict:
        return {"message": "OTP sent", "channel": "email", "reset_token": "reset-token"}

    async def reset_password(self, **_: object) -> None:
        return None

    async def get_profile(self, user_id: UUID) -> AuthUserResponse:
        return self._login_response("PARTNER", session_id=uuid4()).user


@pytest.fixture(autouse=True)
def _reset_engine_pool():
    """
    Drop pooled connections between tests without closing them.

    Each API test boots its own TestClient (and thus its own event loop),
    but the SQLAlchemy async engine is a module-level singleton. asyncpg
    connections checked out on a previous test's loop can't be reused on
    Windows (ProactorEventLoop) — the pool then fails with
    ``'NoneType' object has no attribute 'send'`` on the next connect.
    ``dispose(close=False)`` discards those stale pooled connections so the
    next test starts with a fresh pool bound to its own loop.
    """
    yield
    import asyncio

    # TestClient runs its own event loop, so by teardown no loop is active —
    # spin a throwaway one to dispose the stale pooled connections.
    asyncio.run(app_engine.dispose(close=False))


@pytest.fixture
def client() -> TestClient:
    app = create_application()
    auth_service = StubAuthService()

    async def override_auth_service() -> StubAuthService:
        return auth_service

    async def override_otp_service() -> StubOTPService:
        return StubOTPService()

    async def override_current_user() -> dict[str, object]:
        return {
            "sub": str(auth_service.user_id),
            "role": "PARTNER",
            "roles": ["PARTNER"],
            "permissions": [
                "auth.self.read",
                "auth.sessions.read",
                "auth.sessions.revoke",
            ],
        }

    app.dependency_overrides[get_auth_service] = override_auth_service
    app.dependency_overrides[get_otp_service] = override_otp_service
    app.dependency_overrides[get_current_user] = override_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
