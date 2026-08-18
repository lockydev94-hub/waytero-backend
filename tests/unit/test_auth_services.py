from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.exceptions import AuthenticationException
from app.core.security import hash_password, verify_password
from app.modules.auth.models.user import User
from app.modules.auth.services import AuthService, OTPService
from app.shared.enums.user_types import UserStatus, UserType


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def setex(self, key: str, ttl: int, value: object) -> bool:
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def incr(self, key: str) -> int:
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = value
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        return True


class FakeCache:
    def __init__(self) -> None:
        self.redis = FakeRedis()
        self.data: dict[str, object] = {}

    async def set_otp(self, phone: str, otp: str) -> bool:
        self.data[f"otp:{phone}"] = otp
        return True

    async def get_otp(self, phone: str):
        return self.data.get(f"otp:{phone}")

    async def delete_otp(self, phone: str) -> bool:
        return self.data.pop(f"otp:{phone}", None) is not None

    async def set(self, key: str, value, ttl_seconds: int = 300) -> bool:
        self.data[key] = value
        return True

    async def get(self, key: str):
        return self.data.get(key)

    async def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        return key in self.data or key in self.redis.store

    async def increment(self, key: str, ttl_seconds: int) -> int:
        return await self.redis.incr(key)

    async def ttl(self, key: str) -> int:
        return 30


class FakeResult:
    """Minimal stand-in for a SQLAlchemy Result — the auth service only reads
    `.first()` / `.scalar_one_or_none()` off its customer-profile lookups."""

    def first(self):
        return None

    def scalar_one_or_none(self):
        return None

    def scalar(self):
        return None


class FakeDB:
    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, *args, **kwargs):
        return FakeResult()


class FakeUserRepo:
    def __init__(self) -> None:
        self.users: dict[tuple[str, str], User] = {}
        self.by_email: dict[str, User] = {}

    async def get_by_mobile_and_type(self, mobile: str, user_type: UserType):
        return self.users.get((mobile, user_type.value))

    async def create(self, **kwargs) -> User:
        kwargs.setdefault("id", uuid4())
        kwargs.setdefault("is_mobile_verified", False)
        kwargs.setdefault("is_email_verified", False)
        kwargs.setdefault("failed_login_attempts", 0)
        kwargs.setdefault("is_active", True)
        kwargs.setdefault("force_password_change", False)
        user = User(**kwargs)
        self.users[(user.mobile_number, user.user_type.value)] = user
        if user.email:
            self.by_email[user.email] = user
        return user

    async def mark_mobile_verified(self, user_id):
        return None

    async def get_by_id(self, user_id):
        for user in self.users.values():
            if user.id == user_id:
                return user
        return None

    async def increment_failed_attempts(self, user_id):
        user = await self.get_by_id(user_id)
        if user is None:
            return 0
        user.failed_login_attempts += 1
        return user.failed_login_attempts

    async def lock_account(self, user_id, locked_until):
        user = await self.get_by_id(user_id)
        if user:
            user.locked_until = locked_until

    async def update_password(self, user_id, hashed_password):
        user = await self.get_by_id(user_id)
        if user:
            user.password_hash = hashed_password

    async def update_last_login(self, user_id):
        user = await self.get_by_id(user_id)
        if user:
            user.last_login_at = datetime.now(timezone.utc)

    async def get_by_email(self, email: str):
        return self.by_email.get(email)


class FakeSessionRecord(SimpleNamespace):
    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at


class FakeSessionRepo:
    def __init__(self) -> None:
        self.sessions: list[FakeSessionRecord] = []
        self.revoked_all = False

    async def count_active_sessions(self, user_id):
        return len([session for session in self.sessions if session.is_active])

    async def deactivate_oldest_session(self, user_id):
        if self.sessions:
            self.sessions[0].is_active = False

    async def create(self, **kwargs):
        session = FakeSessionRecord(
            id=uuid4(),
            is_active=True,
            created_at=datetime.now(timezone.utc),
            last_used_at=datetime.now(timezone.utc),
            user_agent=None,
            **kwargs,
        )
        self.sessions.append(session)
        return session

    async def get_by_refresh_token_hash(self, token_hash):
        for session in self.sessions:
            if session.refresh_token_hash == token_hash:
                return session
        return None

    async def update_last_used(self, session_id):
        for session in self.sessions:
            if session.id == session_id:
                session.last_used_at = datetime.now(timezone.utc)

    async def deactivate_all_user_sessions(self, user_id):
        self.revoked_all = True
        for session in self.sessions:
            session.is_active = False

    async def deactivate_session(self, session_id):
        for session in self.sessions:
            if session.id == session_id:
                session.is_active = False

    async def get_active_sessions(self, user_id):
        return [session for session in self.sessions if session.is_active]

    async def get_by_id(self, session_id):
        for session in self.sessions:
            if session.id == session_id:
                return session
        return None


class FakeRoleRepo:
    def __init__(self) -> None:
        self.roles: dict[str, list[str]] = {}
        self.permissions: dict[str, list[str]] = {}

    async def ensure_default_role_for_user(self, user: User) -> None:
        self.roles.setdefault(str(user.id), [user.user_type.value])
        self.permissions.setdefault(
            str(user.id),
            ["auth.self.read", "auth.sessions.read"],
        )

    async def list_roles_for_user(self, user_id):
        return self.roles.get(str(user_id), [])

    async def list_permissions_for_user(self, user_id):
        return self.permissions.get(str(user_id), [])


def build_service() -> (
    tuple[AuthService, FakeUserRepo, FakeSessionRepo, FakeRoleRepo, FakeCache]
):
    cache = FakeCache()
    service = AuthService(FakeDB(), cache)
    user_repo = FakeUserRepo()
    session_repo = FakeSessionRepo()
    role_repo = FakeRoleRepo()
    service.user_repo = user_repo
    service.session_repo = session_repo
    service.role_repo = role_repo

    async def verify_otp(*args, **kwargs):
        return True

    service.otp_service = SimpleNamespace(verify_otp=verify_otp)
    return service, user_repo, session_repo, role_repo, cache


@pytest.mark.asyncio
async def test_otp_service_send_and_verify_round_trip() -> None:
    cache = FakeCache()
    service = OTPService(cache)
    fixed_otp = "123456"

    from app.modules.auth import services as auth_services_module

    original_generate_otp = auth_services_module.generate_otp
    auth_services_module.generate_otp = lambda length=None: fixed_otp
    try:
        response = await service.send_otp("9876543210")
        assert response.mobile_number == "9876543210"
        assert await service.verify_otp("9876543210", fixed_otp) is True
    finally:
        auth_services_module.generate_otp = original_generate_otp


@pytest.mark.asyncio
async def test_otp_service_rejects_incorrect_otp() -> None:
    cache = FakeCache()
    service = OTPService(cache)
    from app.modules.auth import services as auth_services_module

    original_generate_otp = auth_services_module.generate_otp
    auth_services_module.generate_otp = lambda length=None: "123456"
    try:
        await service.send_otp("9876543210")
        with pytest.raises(AuthenticationException):
            await service.verify_otp("9876543210", "000000")
    finally:
        auth_services_module.generate_otp = original_generate_otp


@pytest.mark.asyncio
async def test_customer_otp_login_creates_customer_user() -> None:
    service, user_repo, _, _, _ = build_service()

    async def verify_otp(*args, **kwargs):
        return True

    service.otp_service = SimpleNamespace(verify_otp=verify_otp)
    response = await service.customer_otp_login("9876543210", "123456")

    assert response.user.user_type == "CUSTOMER"
    assert ("9876543210", "CUSTOMER") in user_repo.users


@pytest.mark.asyncio
async def test_partner_password_login_succeeds() -> None:
    service, user_repo, _, role_repo, _ = build_service()
    user = User(
        id=uuid4(),
        mobile_number="9876543210",
        user_type=UserType.PARTNER,
        status=UserStatus.ACTIVE,
        password_hash=hash_password("WayTero@2026"),
        failed_login_attempts=0,
        is_active=True,
        force_password_change=False,
        is_mobile_verified=True,
        is_email_verified=False,
    )
    user_repo.users[(user.mobile_number, user.user_type.value)] = user
    await role_repo.ensure_default_role_for_user(user)

    response = await service.partner_password_login(
        password="WayTero@2026", mobile="9876543210"
    )

    assert response.user.user_type == "PARTNER"
    assert response.tokens.access_token


@pytest.mark.asyncio
async def test_partner_password_login_rejects_wrong_password() -> None:
    service, user_repo, _, role_repo, _ = build_service()
    user = User(
        id=uuid4(),
        mobile_number="9876543210",
        user_type=UserType.PARTNER,
        status=UserStatus.ACTIVE,
        password_hash=hash_password("WayTero@2026"),
        failed_login_attempts=0,
        is_active=True,
        is_mobile_verified=True,
        is_email_verified=False,
    )
    user_repo.users[(user.mobile_number, user.user_type.value)] = user
    await role_repo.ensure_default_role_for_user(user)

    with pytest.raises(AuthenticationException):
        await service.partner_password_login(
            password="WrongPassword@2026", mobile="9876543210"
        )


@pytest.mark.asyncio
async def test_refresh_access_token_returns_new_access_token(monkeypatch) -> None:
    service, user_repo, session_repo, role_repo, _ = build_service()
    user = User(
        id=uuid4(),
        mobile_number="9876543210",
        user_type=UserType.PARTNER,
        status=UserStatus.ACTIVE,
        password_hash=hash_password("WayTero@2026"),
        failed_login_attempts=0,
        is_active=True,
        is_mobile_verified=True,
        is_email_verified=False,
    )
    user_repo.users[(user.mobile_number, user.user_type.value)] = user
    await role_repo.ensure_default_role_for_user(user)

    session = await session_repo.create(
        user_id=user.id,
        refresh_token_hash="hashed-refresh",
        device_name="Chrome",
        device_os="Windows",
        ip_address="127.0.0.1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    monkeypatch.setattr(
        "app.modules.auth.services.decode_refresh_token",
        lambda token: {
            "sub": str(user.id),
            "user_type": user.user_type.value,
            "session_id": str(session.id),
        },
    )
    monkeypatch.setattr(
        "app.modules.auth.services._hash_token",
        lambda token: "hashed-refresh",
    )

    response = await service.refresh_access_token("refresh-token")

    assert response.access_token
    assert response.roles == ["PARTNER"]


@pytest.mark.asyncio
async def test_change_password_revokes_sessions() -> None:
    service, user_repo, session_repo, role_repo, _ = build_service()
    user = User(
        id=uuid4(),
        mobile_number="9876543210",
        user_type=UserType.ADMIN,
        status=UserStatus.ACTIVE,
        password_hash=hash_password("OldPassword@2026"),
        failed_login_attempts=0,
        is_mobile_verified=True,
        is_email_verified=True,
    )
    user_repo.users[(user.mobile_number, user.user_type.value)] = user
    await role_repo.ensure_default_role_for_user(user)

    await service.change_password(user.id, "OldPassword@2026", "WayTero@2026")

    assert verify_password("WayTero@2026", user.password_hash) is True
    assert session_repo.revoked_all is True


@pytest.mark.asyncio
async def test_password_reset_updates_password(monkeypatch) -> None:
    service, user_repo, session_repo, role_repo, cache = build_service()
    user = User(
        id=uuid4(),
        mobile_number="9876543210",
        email="ops@waytero.com",
        user_type=UserType.ADMIN,
        status=UserStatus.ACTIVE,
        password_hash=hash_password("OldPassword@2026"),
        failed_login_attempts=0,
        is_mobile_verified=True,
        is_email_verified=True,
    )
    user_repo.users[(user.mobile_number, user.user_type.value)] = user
    user_repo.by_email[user.email] = user
    await role_repo.ensure_default_role_for_user(user)
    await session_repo.create(
        user_id=user.id,
        refresh_token_hash="session-hash",
        device_name="Chrome",
        device_os="Windows",
        ip_address="127.0.0.1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    monkeypatch.setattr(
        "app.modules.auth.services.generate_secure_token",
        lambda: "fixed-reset-token",
    )
    monkeypatch.setattr(
        "app.modules.auth.services.generate_otp",
        lambda length=None: "123456",
    )

    # Mock _is_smtp_configured to return True so the OTP can be
    # delivered via email.  _send_password_reset_otp_email is mocked
    # to no-op (no real SMTP in unit tests).
    async def _smtp_on():
        return True

    async def _sms_off():
        return False

    async def _noop_email(*args, **kwargs):
        return None

    service._is_smtp_configured = _smtp_on
    service._is_sms_configured = _sms_off
    service._send_password_reset_otp_email = _noop_email
    result = await service.initiate_password_reset("ops@waytero.com")
    assert result["channel"] == "email"
    assert result["reset_token"]
    await service.reset_password(result["reset_token"], "123456", "WayTero@2026")

    assert verify_password("WayTero@2026", user.password_hash) is True
    assert session_repo.revoked_all is True


@pytest.mark.asyncio
async def test_logout_all_devices_deactivates_sessions() -> None:
    service, user_repo, session_repo, role_repo, _ = build_service()
    user = User(
        id=uuid4(),
        mobile_number="9876543210",
        user_type=UserType.PARTNER,
        status=UserStatus.ACTIVE,
        password_hash=hash_password("WayTero@2026"),
        failed_login_attempts=0,
        is_mobile_verified=True,
        is_email_verified=True,
    )
    user_repo.users[(user.mobile_number, user.user_type.value)] = user
    await role_repo.ensure_default_role_for_user(user)
    await session_repo.create(
        user_id=user.id,
        refresh_token_hash="session-hash",
        device_name="Chrome",
        device_os="Windows",
        ip_address="127.0.0.1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    await service.logout(user.id, refresh_token=None, logout_all=True)

    assert session_repo.revoked_all is True
