"""
How a working day is measured (Admin Portal §39, migration 0084).

Doc Ref: app/modules/auth/services/__init__.py — `_platform_today`,
`record_heartbeat`, `get_my_work_hours`.

The two things that would silently corrupt the attendance report:
  * the day bucket. It has to be the platform zone, because that is the zone
    the daily forced-logout hour is compared in — a browser or server clock in
    another zone would otherwise file Tuesday's minutes against Wednesday.
  * the number of seconds a client is allowed to claim. Anything holding an
    access token can POST a heartbeat, so the server clamps the value instead
    of trusting it.
"""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import PermissionDeniedException, ResourceNotFoundException
from app.modules.auth.models.user import User
from app.modules.auth.services import AuthService
from app.shared.enums.user_types import UserType, UserStatus

# 19 Oct 21:30 UTC is 20 Oct 03:00 in Asia/Kolkata — the two dates differ, so
# a bucket taken from the wrong clock is visible rather than accidental.
REAL_NOW = datetime(2026, 10, 19, 21, 30, tzinfo=timezone.utc)
PLATFORM_ZONE = ZoneInfo("Asia/Kolkata")


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class DbStub:
    async def execute(self, *_args, **_kwargs):
        return _Result([("PLATFORM_TIMEZONE", "Asia/Kolkata")])

    async def flush(self) -> None:
        return None


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return REAL_NOW if tz is None else REAL_NOW.astimezone(tz)


class UserRepoStub:
    def __init__(self, user: User) -> None:
        self._user = user

    async def get_by_id(self, user_id):
        return self._user if user_id == self._user.id else None


class WorkRepoStub:
    def __init__(self) -> None:
        self.activity: list[tuple] = []
        self.listed: list[tuple] = []
        self.summarized: list[tuple] = []

    async def record_activity(self, *, user_id, work_date, active_seconds):
        self.activity.append((work_date, active_seconds))

    async def list_days(self, *, tz_name, date_from, date_to, user_id=None, **_kw):
        self.listed.append((tz_name, date_from, date_to, user_id))
        return []

    async def summarize_days(self, *, date_from, date_to, user_id=None):
        self.summarized.append((date_from, date_to, user_id))
        return {"days": 0, "staff_count": 0, "active_seconds": 0}


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
        force_password_change=False,
    )


def _service(user: User, monkeypatch) -> tuple[AuthService, WorkRepoStub]:
    service = AuthService(DbStub(), None)
    service.user_repo = UserRepoStub(user)
    service.work_repo = WorkRepoStub()
    monkeypatch.setattr("app.modules.auth.services.get_platform_tz", _platform_tz)
    monkeypatch.setattr("app.modules.auth.services.datetime", FrozenDatetime)
    return service, service.work_repo


async def _platform_tz(_db) -> ZoneInfo:
    return PLATFORM_ZONE


@pytest.mark.asyncio
async def test_heartbeat_is_capped_at_a_minute(monkeypatch) -> None:
    user = _make_user()
    service, work_repo = _service(user, monkeypatch)

    recorded = await service.record_heartbeat(user.id, 3600)

    assert recorded == 60
    assert work_repo.activity == [(date(2026, 10, 20), 60)]


@pytest.mark.asyncio
async def test_heartbeat_keeps_an_honest_short_gap(monkeypatch) -> None:
    user = _make_user()
    service, work_repo = _service(user, monkeypatch)

    assert await service.record_heartbeat(user.id, 42) == 42
    assert work_repo.activity[-1] == (date(2026, 10, 20), 42)


@pytest.mark.asyncio
async def test_work_day_is_bucketed_in_the_platform_zone(monkeypatch) -> None:
    user = _make_user()
    service, work_repo = _service(user, monkeypatch)

    await service.record_heartbeat(user.id, 30)

    # UTC would have said 19 Oct; the report and the 23:00 rule both live in
    # the platform zone, so both answers must be 20 Oct.
    assert work_repo.activity[0][0] == REAL_NOW.astimezone(PLATFORM_ZONE).date()
    assert work_repo.activity[0][0] != REAL_NOW.date()


@pytest.mark.asyncio
async def test_non_dashboard_account_records_nothing(monkeypatch) -> None:
    user = _make_user(UserType.CUSTOMER)
    service, work_repo = _service(user, monkeypatch)

    with pytest.raises(PermissionDeniedException):
        await service.record_heartbeat(user.id, 30)

    assert work_repo.activity == []


@pytest.mark.asyncio
async def test_unknown_account_records_nothing(monkeypatch) -> None:
    service, work_repo = _service(_make_user(), monkeypatch)

    with pytest.raises(ResourceNotFoundException):
        await service.record_heartbeat(uuid4(), 30)

    assert work_repo.activity == []


@pytest.mark.asyncio
async def test_my_hours_window_covers_the_requested_days(monkeypatch) -> None:
    user = _make_user()
    service, work_repo = _service(user, monkeypatch)

    data = await service.get_my_work_hours(user.id, days=30)

    today = REAL_NOW.astimezone(PLATFORM_ZONE).date()
    assert data["date_to"] == today.isoformat()
    assert date.fromisoformat(data["date_from"]) == today - timedelta(days=29)
    assert work_repo.listed[0][0] == "Asia/Kolkata"
    assert work_repo.listed[0][3] == user.id


@pytest.mark.asyncio
async def test_my_hours_window_is_clamped(monkeypatch) -> None:
    user = _make_user()
    service, _ = _service(user, monkeypatch)

    data = await service.get_my_work_hours(user.id, days=5000)

    today = REAL_NOW.astimezone(PLATFORM_ZONE).date()
    assert date.fromisoformat(data["date_from"]) == today - timedelta(days=365)
