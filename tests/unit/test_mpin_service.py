"""
The dashboard unlock PIN: who may set it, and what a wrong guess costs.

Doc Ref: Admin Portal §39 Security; migration 0084.

What these tests pin down is the part that is easy to get wrong and expensive
to break in production:
  * MPIN failures must never touch `locked_until` — that field is what
    `User.is_locked` reads, so sharing it would lock the password login and
    leave the admin with no way back in.
  * Installing the gate requires the password, otherwise a hijacked live
    session token could set the PIN that locks out the real admin.
  * The policy comes from configuration and is clamped, never trusted.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
    BusinessException,
    PermissionDeniedException,
    ValidationException,
)
from app.core.security import hash_password, verify_password
from app.modules.auth.models.user import User
from app.modules.auth.services import AuthService
from app.shared.enums.user_types import UserType, UserStatus

DEFAULT_CONFIG = {
    "ADMIN_IDLE_DEFAULT_MINUTES": "5",
    "ADMIN_DAILY_LOGOUT_HOUR": "23",
    "ADMIN_MPIN_MIN_LENGTH": "6",
    "ADMIN_MPIN_MAX_ATTEMPTS": "5",
    "ADMIN_MPIN_LOCKOUT_MINUTES": "15",
}


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class DbStub:
    """Stands in for AsyncSession: the policy read plus flush counting."""

    def __init__(self, config: dict) -> None:
        self._rows = list(config.items())
        self.flushes = 0

    async def execute(self, *_args, **_kwargs):
        return _Result(self._rows)

    async def flush(self) -> None:
        self.flushes += 1


class UserRepoStub:
    def __init__(self, user: User) -> None:
        self._user = user

    async def get_by_id(self, user_id):
        return self._user


def _make_user(
    *,
    user_type: UserType = UserType.ADMIN,
    password: str | None = "Str0ng!Passw0rd",
    mpin: str | None = None,
) -> User:
    return User(
        id=uuid4(),
        user_code="ADM-000001",
        first_name="Asha",
        last_name="Rout",
        mobile_number="9000000000",
        email="asha.rout@example.test",
        user_type=user_type,
        status=UserStatus.ACTIVE,
        is_active=True,
        is_mobile_verified=True,
        is_email_verified=True,
        force_password_change=False,
        password_hash=hash_password(password) if password else None,
        mpin_hash=hash_password(mpin) if mpin else None,
        mpin_failed_attempts=0,
        mpin_locked_until=None,
        idle_lock_minutes=None,
        # defaults are server-side; an in-memory row has None
        failed_login_attempts=0,
        locked_until=None,
    )


def _service(user: User, config: dict | None = None, monkeypatch=None):
    audits = []

    class FakeAuditLogger:
        @staticmethod
        async def log_auth_event(db, *, action_type=None, **_kwargs):
            audits.append(action_type)
            return 1

    monkeypatch.setattr("app.modules.auth.services.AuditLogger", FakeAuditLogger)

    service = AuthService(DbStub(config or DEFAULT_CONFIG), None)
    service.user_repo = UserRepoStub(user)
    return service, audits


async def test_set_mpin_stores_a_verifiable_pin_and_audits_it(monkeypatch):
    user = _make_user()
    service, audits = _service(user, monkeypatch=monkeypatch)

    await service.set_mpin(user.id, "248157", "Str0ng!Passw0rd")

    assert verify_password("248157", user.mpin_hash)
    assert user.mpin_failed_attempts == 0
    assert "MPIN_SET" in audits


@pytest.mark.parametrize("current_password", [None, "", "Wrong!Password1"])
async def test_set_mpin_requires_the_password(current_password, monkeypatch):
    user = _make_user()
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(AuthenticationException):
        await service.set_mpin(user.id, "248157", current_password)

    assert user.mpin_hash is None


async def test_set_mpin_refuses_an_otp_only_account(monkeypatch):
    # No password to re-enter means no way to prove the token holder is the
    # admin — the caller must go through the OTP path instead.
    user = _make_user(password=None)
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(BusinessException):
        await service.set_mpin(user.id, "248157", None)


async def test_set_mpin_twice_is_refused_and_keeps_the_original(monkeypatch):
    original = hash_password("135790")
    user = _make_user(mpin="135790")
    user.mpin_hash = original
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(BusinessException) as exc:
        await service.set_mpin(user.id, "248157", "Str0ng!Passw0rd")

    assert exc.value.code == "MPIN_ALREADY_SET"
    assert user.mpin_hash is original


async def test_minimum_length_is_read_from_configuration(monkeypatch):
    user = _make_user()
    service, _ = _service(
        user, {**DEFAULT_CONFIG, "ADMIN_MPIN_MIN_LENGTH": "8"}, monkeypatch=monkeypatch
    )

    with pytest.raises(ValidationException) as exc:
        await service.set_mpin(user.id, "248157", "Str0ng!Passw0rd")

    assert exc.value.code == "MPIN_TOO_SHORT"
    assert user.mpin_hash is None


async def test_a_corrupt_config_row_falls_back_to_the_seed_value(monkeypatch):
    # 'abc' is what a hand-edited row looks like. Falling back to 6 keeps the
    # policy readable rather than crashing the lock screen.
    user = _make_user()
    service, _ = _service(
        user, {**DEFAULT_CONFIG, "ADMIN_MPIN_MIN_LENGTH": "abc"}, monkeypatch=monkeypatch
    )
    policy = await service._admin_policy()

    assert policy["mpin_min_length"] == 6


async def test_config_is_clamped_into_its_safe_range(monkeypatch):
    user = _make_user()
    service, _ = _service(
        user,
        {**DEFAULT_CONFIG, "ADMIN_IDLE_DEFAULT_MINUTES": "99999"},
        monkeypatch=monkeypatch,
    )

    policy = await service._admin_policy()

    # A row can make the lock stricter, never switch it off.
    assert policy["idle_default_minutes"] == 240


async def test_five_wrong_pins_lock_the_unlock_and_never_touch_the_password_lock(
    monkeypatch,
):
    user = _make_user(mpin="248157")
    service, audits = _service(user, monkeypatch=monkeypatch)

    remaining = []
    for _ in range(4):
        with pytest.raises(BusinessException) as exc:
            await service.verify_mpin(user.id, "999999")
        assert exc.value.code == "MPIN_INVALID"
        remaining.append(exc.value.details["attempts_remaining"])

    assert remaining == [4, 3, 2, 1]
    assert user.locked_until is None  # the password login is untouched

    with pytest.raises(BusinessException) as exc:
        await service.verify_mpin(user.id, "999999")

    assert exc.value.code == "MPIN_LOCKED"
    assert user.mpin_locked_until > datetime.now(timezone.utc)
    assert "MPIN_LOCKED" in audits


async def test_a_correct_pin_clears_the_failure_count(monkeypatch):
    user = _make_user(mpin="248157")
    user.mpin_failed_attempts = 3
    service, _ = _service(user, monkeypatch=monkeypatch)

    await service.verify_mpin(user.id, "248157")

    assert user.mpin_failed_attempts == 0


async def test_an_expired_lockout_clears_instead_of_refusing_forever(monkeypatch):
    user = _make_user(mpin="248157")
    user.mpin_failed_attempts = 5
    user.mpin_locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    service, _ = _service(user, monkeypatch=monkeypatch)

    await service.verify_mpin(user.id, "248157")

    assert user.mpin_locked_until is None
    assert user.mpin_failed_attempts == 0


async def test_verify_without_a_pin_says_so_rather_than_failing_like_a_wrong_one(
    monkeypatch,
):
    user = _make_user()
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(BusinessException) as exc:
        await service.verify_mpin(user.id, "248157")

    assert exc.value.code == "MPIN_NOT_SET"


async def test_password_reentry_clears_an_mpin_lockout(monkeypatch):
    user = _make_user(mpin="248157")
    user.mpin_failed_attempts = 5
    user.mpin_locked_until = datetime.now(timezone.utc) + timedelta(minutes=14)
    service, _ = _service(user, monkeypatch=monkeypatch)

    await service.remove_mpin(user.id, "Str0ng!Passw0rd")

    assert user.mpin_hash is None
    assert user.mpin_locked_until is None
    assert user.mpin_failed_attempts == 0


async def test_otp_only_admin_removes_their_pin_with_the_pin(monkeypatch):
    user = _make_user(password=None, mpin="248157")
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(BusinessException):
        await service.remove_mpin(user.id, None, "999999")
    assert user.mpin_hash is not None

    await service.remove_mpin(user.id, None, "248157")
    assert user.mpin_hash is None


async def test_change_pin_rejects_reusing_the_current_one(monkeypatch):
    user = _make_user(mpin="248157")
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(ValidationException) as exc:
        await service.change_mpin(user.id, "248157", "248157")

    assert exc.value.code == "MPIN_UNCHANGED"
    assert verify_password("248157", user.mpin_hash)


async def test_change_pin_installs_the_new_one(monkeypatch):
    user = _make_user(mpin="248157")
    service, audits = _service(user, monkeypatch=monkeypatch)

    await service.change_mpin(user.id, "248157", "807060")

    assert verify_password("807060", user.mpin_hash)
    assert "MPIN_CHANGED" in audits


async def test_idle_lock_is_reduce_only(monkeypatch):
    user = _make_user()
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(ValidationException) as exc:
        await service.set_idle_lock_minutes(user.id, 10)

    assert exc.value.code == "IDLE_LOCK_TOO_LONG"
    assert user.idle_lock_minutes is None

    assert await service.set_idle_lock_minutes(user.id, 2) == 2
    assert await service.set_idle_lock_minutes(user.id, None) == 5


async def test_session_config_reports_the_effective_lock_time(monkeypatch):
    user = _make_user(mpin="248157")
    user.idle_lock_minutes = 3
    service, _ = _service(user, monkeypatch=monkeypatch)

    config = await service.get_session_config(user.id)

    assert config.has_mpin is True
    assert config.idle_minutes == 3
    assert config.idle_default_minutes == 5
    assert config.daily_logout_hour == 23
    assert config.mpin_attempts_remaining == 5
    # Seconds, not minutes — the shell schedules its own pre-expiry refresh.
    assert config.access_token_expires_in == settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60


async def test_customer_tokens_cannot_reach_the_pin_endpoints(monkeypatch):
    # The columns live on the shared `users` row, so /auth/admin/* has to check
    # the type itself — the URL prefix is not a gate.
    user = _make_user(user_type=UserType.CUSTOMER, mpin="248157")
    service, _ = _service(user, monkeypatch=monkeypatch)

    with pytest.raises(PermissionDeniedException):
        await service.verify_mpin(user.id, "248157")
