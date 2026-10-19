# ruff: noqa: E402

from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.branch_scope import STAFF_BRANCH_TYPES
from app.core.dependencies import get_current_user
from app.core.database import engine as app_engine
from app.core.security import create_access_token
from app.main import create_application
from app.modules.auth.api import get_auth_service, get_otp_service
from app.modules.auth.constants import HEARTBEAT_MAX_SECONDS
from app.modules.auth.schemas import (
    AuthUserResponse,
    BranchSummary,
    LoginResponse,
    OTPSentResponse,
    RefreshResponse,
    SessionConfigResponse,
)


class StubOTPService:
    async def send_otp(self, mobile: str, purpose: str = "LOGIN") -> OTPSentResponse:
        return OTPSentResponse(mobile_number=mobile, expires_in_seconds=300)


class StubAuthService:
    def __init__(self) -> None:
        self.user_id = uuid4()
        # Session-policy state the /auth/admin/* routes mutate, so the API tests
        # can assert a flow instead of a canned reply.
        self.mpin_set = False
        self.idle_lock_minutes: int | None = None
        self.heartbeats: list[int] = []
        self.work_hours_days: int | None = None
        self.mpin_otp_sends = 0
        # Branch facts, set by _make_client from the same payload the overridden
        # get_current_user carries, so a login/me test cannot disagree with the
        # scope the caller is being enforced against.
        self.branch_payload: dict | None = None
        self.is_super = False

    def _branch(self) -> BranchSummary | None:
        if self.branch_payload is None:
            return None
        return BranchSummary(
            id=self.branch_payload["id"],
            name=self.branch_payload["name"],
            code=self.branch_payload["code"],
            city_id=self.branch_payload["city_id"],
            city_name=self.branch_payload["name"],
            is_headquarters=self.branch_payload["is_headquarters"],
        )

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
                user_code=f"{user_type[:3].upper()}-000001",
                last_login_at=datetime.now(timezone.utc),
                branch=self._branch(),
                is_super=self.is_super,
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

    # ── Admin session policy (Admin Portal §39, migration 0084) ──
    # Kept in step with AuthService so the /auth/admin/* routes stay testable.

    async def get_session_config(self, user_id: UUID) -> SessionConfigResponse:
        return SessionConfigResponse(
            has_mpin=self.mpin_set,
            has_password=True,
            idle_minutes=self.idle_lock_minutes or 5,
            idle_lock_minutes=self.idle_lock_minutes,
            idle_default_minutes=5,
            daily_logout_hour=23,
            timezone="Asia/Kolkata",
            server_now=datetime.now(timezone.utc),
            mpin_locked_until=None,
            mpin_attempts_remaining=5,
            mpin_min_length=6,
            access_token_expires_in=900,
            branch=self._branch(),
            is_super=self.is_super,
        )

    async def set_mpin(self, **_: object) -> None:
        self.mpin_set = True

    async def send_mpin_otp(self, user_id: UUID) -> OTPSentResponse:
        # The route must send to the session subject's own number; the stub
        # records that it was called at all.
        self.mpin_otp_sends += 1
        return OTPSentResponse(mobile_number="9876543210")

    async def change_mpin(self, **_: object) -> None:
        self.mpin_set = True

    async def verify_mpin(self, *, user_id: UUID, mpin: str) -> None:
        return None

    async def remove_mpin(self, **_: object) -> None:
        self.mpin_set = False

    async def set_idle_lock_minutes(
        self, user_id: UUID, idle_lock_minutes: int | None
    ) -> int:
        self.idle_lock_minutes = idle_lock_minutes
        return idle_lock_minutes or 5

    async def record_heartbeat(self, *, user_id: UUID, delta_seconds: int) -> int:
        # Same clamp the service applies, so the API test proves the route
        # passes the value through rather than trusting the client.
        self.heartbeats.append(delta_seconds)
        return max(1, min(HEARTBEAT_MAX_SECONDS, delta_seconds))

    async def get_my_work_hours(self, *, user_id: UUID, days: int = 30) -> dict:
        today = datetime.now(timezone.utc).date()
        self.work_hours_days = days
        return {
            "items": [],
            "period_totals": {
                "days": 0,
                "staff_count": 1,
                "active_seconds": 0,
                "active_minutes": 0,
            },
            "date_from": (today - timedelta(days=days - 1)).isoformat(),
            "date_to": today.isoformat(),
        }


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


def _branch_payload(
    *,
    branch_id: int,
    city_id: int,
    name: str,
    code: str,
    is_headquarters: bool,
    is_active: bool = True,
) -> dict:
    """The branch dict get_current_user attaches to a staff payload (0085).

    The scope layer reads this shape rather than querying, so a test controls
    the caller's branch by building a payload — which is also what makes the
    fail-closed path testable (omit the key entirely).
    """
    return {
        "id": branch_id,
        "city_id": city_id,
        "name": name,
        "code": code,
        "is_headquarters": is_headquarters,
        "is_active": is_active,
    }


HQ_BRANCH = _branch_payload(
    branch_id=1, city_id=1, name="Bhubaneswar", code="BBS", is_headquarters=True
)
CUTTACK_BRANCH = _branch_payload(
    branch_id=2, city_id=2, name="Cuttack", code="CTK", is_headquarters=False
)


def _make_client(
    roles: list[str],
    *,
    branch: dict | None | object = ...,
) -> Iterator[TestClient]:
    """TestClient whose caller carries `roles` and the stub auth service.

    The role list belongs to the caller, not the stub: /auth/* routes only need
    any signed-in identity, while every /admin/* gate reads the JWT roles — so
    an admin-path test has to present as an admin.

    `branch` is the caller's branch facts, which get_current_user normally reads
    off the user row. Defaults to the head office for a branch-scoped staff role
    (a staff account with no branch cannot exist under ck_users_branch_membership)
    and to nothing for everyone else; pass `None` explicitly to test the
    fail-closed path.

    The Authorization header is a real signed token because
    AuthenticationGuardMiddleware default-denies every /api/v1 path outside the
    public allowlist; `get_current_user` is still overridden, so the guard is
    satisfied while the roles under test come from the fixture.
    """
    app = create_application()
    auth_service = StubAuthService()
    access_token = create_access_token(
        {
            "sub": str(auth_service.user_id),
            "role": roles[0],
            "roles": roles,
            "user_type": roles[0],
        },
        expires_delta=timedelta(hours=1),
    )

    if branch is ...:
        branch = HQ_BRANCH if roles[0] in STAFF_BRANCH_TYPES else None

    auth_service.branch_payload = branch
    auth_service.is_super = roles[0] == "SUPER_ADMIN"

    async def override_auth_service() -> StubAuthService:
        return auth_service

    async def override_otp_service() -> StubOTPService:
        return StubOTPService()

    async def override_current_user() -> dict[str, object]:
        payload: dict[str, object] = {
            "sub": str(auth_service.user_id),
            "role": roles[0],
            "user_type": roles[0],
            "roles": roles,
            "permissions": [
                "auth.self.read",
                "auth.sessions.read",
                "auth.sessions.revoke",
            ],
        }
        if branch is not None:
            payload["branch"] = branch
        return payload

    app.dependency_overrides[get_auth_service] = override_auth_service
    app.dependency_overrides[get_otp_service] = override_otp_service
    app.dependency_overrides[get_current_user] = override_current_user

    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {access_token}"
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Signed in as a PARTNER — enough for /auth/*, refused by /admin/*."""
    yield from _make_client(["PARTNER"])


@pytest.fixture
def client_staff() -> Iterator[TestClient]:
    """Signed in as an ADMIN: passes the ALL_STAFF gates."""
    yield from _make_client(["ADMIN"])


@pytest.fixture
def client_super_admin() -> Iterator[TestClient]:
    """Signed in as SUPER_ADMIN — the working-hours report is gated on this."""
    yield from _make_client(["SUPER_ADMIN"])


@pytest.fixture
def client_cco() -> Iterator[TestClient]:
    """CCO — support work, and nothing that manages staff or platform config."""
    yield from _make_client(["CCO"])


@pytest.fixture
def client_finance_manager() -> Iterator[TestClient]:
    """FINANCE_MANAGER — settlements and reports."""
    yield from _make_client(["FINANCE_MANAGER"])


@pytest.fixture
def client_verification_officer() -> Iterator[TestClient]:
    """VERIFICATION_OFFICER — partner and vehicle verification."""
    yield from _make_client(["VERIFICATION_OFFICER"])


@pytest.fixture
def client_hq_admin() -> Iterator[TestClient]:
    """ADMIN of the head office — its own city plus everything unassigned."""
    yield from _make_client(["ADMIN"], branch=HQ_BRANCH)


@pytest.fixture
def client_cuttack_cco() -> Iterator[TestClient]:
    """CCO of the Cuttack sub-branch — Cuttack rows only."""
    yield from _make_client(["CCO"], branch=CUTTACK_BRANCH)


@pytest.fixture
def client_cuttack_verification_officer() -> Iterator[TestClient]:
    """VERIFICATION_OFFICER of the Cuttack sub-branch."""
    yield from _make_client(
        ["VERIFICATION_OFFICER"], branch=CUTTACK_BRANCH
    )
