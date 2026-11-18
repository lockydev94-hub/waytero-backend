"""
One login must produce one audit row and one last-login update.

`_create_session_and_tokens` used to run its token-mint + `update_last_login`
+ `AuditLogger` block twice per login, which wrote two LOGIN_SUCCESS rows and
would double any per-login bookkeeping layered on afterwards.
"""
from types import SimpleNamespace
from uuid import uuid4

from app.core.security import decode_access_token
from app.modules.auth.constants import AUTH_EVENT_LOGIN_SUCCESS
from app.modules.auth.models.user import User
from app.modules.auth.services import AuthService, _hash_token
from app.shared.enums.user_types import UserType, UserStatus


class DbStub:
    def __init__(self) -> None:
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1


class SessionRepoStub:
    def __init__(self) -> None:
        self.created = []

    async def count_active_sessions(self, user_id):
        return 0

    async def deactivate_oldest_session(self, user_id):
        return None

    async def create(self, **kwargs):
        session = SimpleNamespace(id=uuid4(), **kwargs)
        self.created.append(session)
        return session


class UserRepoStub:
    def __init__(self) -> None:
        self.last_login_calls = 0

    async def update_last_login(self, user_id):
        self.last_login_calls += 1


class WorkRepoStub:
    def __init__(self) -> None:
        self.opened = []

    async def open_day(self, *, user_id, work_date):
        self.opened.append((user_id, work_date))


def _make_user(user_type: UserType = UserType.ADMIN) -> User:
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
        # server_default only fires on INSERT, and an in-memory row has None
        force_password_change=False,
    )


def _service(monkeypatch, partner_int_id=None):
    audit_calls = []

    class FakeAuditLogger:
        @staticmethod
        async def log_auth_event(db, *, action_type=None, **_kwargs):
            audit_calls.append(action_type)
            return 1

    monkeypatch.setattr("app.modules.auth.services.AuditLogger", FakeAuditLogger)

    service = AuthService(DbStub(), None)
    service.user_repo = UserRepoStub()
    service.session_repo = SessionRepoStub()
    service.work_repo = WorkRepoStub()

    async def fake_access_context(user_id):
        return (["ADMIN"], ["auth.self.read"])

    async def fake_partner_access(user):
        return partner_int_id, None, []

    service._get_access_context = fake_access_context
    service._resolve_partner_access = fake_partner_access
    return service, audit_calls


async def test_login_writes_one_audit_row_one_last_login_and_one_work_day(
    monkeypatch,
):
    service, audit_calls = _service(monkeypatch)

    await service._create_session_and_tokens(
        _make_user(), "Chrome", "Windows", "127.0.0.1"
    )

    assert audit_calls == [AUTH_EVENT_LOGIN_SUCCESS]
    assert service.user_repo.last_login_calls == 1
    # Attendance counts a login, so the deleted duplicate block would have
    # silently doubled `session_count` on every staff day row.
    assert len(service.work_repo.opened) == 1


async def test_stored_refresh_hash_matches_the_returned_refresh_token(monkeypatch):
    service, _ = _service(monkeypatch)
    session_repo = service.session_repo

    response = await service._create_session_and_tokens(
        _make_user(), "Chrome", "Windows", "127.0.0.1"
    )

    # The first block minted tokens that were thrown away, so the hash written
    # by it belonged to no token the client ever received — refresh would 401.
    assert session_repo.created[0].refresh_token_hash == _hash_token(
        response.tokens.refresh_token
    )


async def test_partner_claim_survives_in_the_returned_access_token(monkeypatch):
    service, _ = _service(monkeypatch, partner_int_id=42)

    response = await service._create_session_and_tokens(
        _make_user(UserType.PARTNER), "Chrome", "Windows", "127.0.0.1"
    )

    payload = decode_access_token(response.tokens.access_token)
    assert payload["partner_id"] == 42
    # Attendance is a dashboard concern: a partner or customer login must not
    # create a staff day row.
    assert service.work_repo.opened == []


async def test_customer_login_writes_no_work_day(monkeypatch):
    service, _ = _service(monkeypatch)

    await service._create_session_and_tokens(
        _make_user(UserType.CUSTOMER), "Chrome", "Windows", "127.0.0.1"
    )

    assert service.work_repo.opened == []
