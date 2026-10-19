# ============================================================
# WAYTERO — STAFF × BRANCH RULES (UNIT)
# File: tests/unit/test_staff_branch_rules.py
# Doc Ref: migration 0085 (branches, users.branch_id, ck_users_branch_membership)
#
# Who may hire whom, into which office, and who may not be removed. These are
# the owner's rulings turned into code, so they are tested at the decision —
# not through 300 route calls. Which route reaches which decision is
# tests/api/test_staff_rbac_api.py and the scope API suite.
#
# The database is faked. A wrong branch here is a wrong number in the INSERT
# parameters, and that is exactly what a fake lets you read.
# ============================================================

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.branch_scope import BranchScope
from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.admin.schemas.dashboard import StaffUserCreate, StaffUserUpdate
from app.modules.admin.services.dashboard import AdminDashboardService

HQ = {"id": 1, "city_id": 10, "name": "Bhubaneswar", "code": "BBS",
      "is_headquarters": True, "is_active": True}
CUTTACK = {"id": 2, "city_id": 20, "name": "Cuttack", "code": "CTK",
           "is_headquarters": False, "is_active": True}


def _scope(branch, *, is_super=False, user_type="ADMIN", actor_id=None):
    return BranchScope(
        actor_id=actor_id or str(uuid4()),
        user_type="SUPER_ADMIN" if is_super else user_type,
        is_super=is_super,
        branch_id=None if is_super else branch["id"],
        branch_name=None if is_super else branch["name"],
        is_headquarters=False if is_super else branch["is_headquarters"],
        cities=frozenset() if is_super else frozenset({branch["city_id"]}),
        include_unassigned=True if is_super else branch["is_headquarters"],
    )


class _Answer:
    """One query result, shaped however the caller asks for it."""

    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def mappings(self):
        return self

    def one_or_none(self):
        return self._value

    def first(self):
        return self._value


class _Db:
    """Answers from a queue and remembers the SQL it was asked to run."""

    def __init__(self, *answers, branch=None):
        self.answers = list(answers)
        self.branch = branch
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, clause, params=None):
        self.calls.append((str(clause), dict(params or {})))
        value = self.answers.pop(0) if self.answers else None
        return _Answer(value)

    async def get(self, _model, pk):
        self.calls.append((f"GET {getattr(_model, '__tablename__', '?')}:{pk}", {}))
        return self.branch

    async def commit(self):
        return None

    async def flush(self):
        return None

    def sql_containing(self, needle: str) -> list[dict]:
        return [params for sql, params in self.calls if needle in sql]


def _create_params(**overrides) -> dict:
    base = {
        "first_name": "Sneha",
        "email": "sneha@waytero.in",
        "mobile_number": "9876500001",
        "password": "BranchTest!2345",
        "user_type": "CCO",
    }
    base.update(overrides)
    return base


def _hire(db, payload, scope):
    return AdminDashboardService.create_staff_user(
        db, payload, created_by=uuid4(), scope=scope
    )


@pytest.fixture
def no_profile_write(monkeypatch):
    """The staff_profiles half of hiring is another function's business."""

    async def skip(*_a, **_k):
        return None

    monkeypatch.setattr(AdminDashboardService, "_upsert_staff_profile", skip)
    monkeypatch.setattr(
        AdminDashboardService, "get_staff_user", skip
    )


# ── Hiring ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_manager_cannot_hire_a_branch_manager(no_profile_write) -> None:
    """Owner ruling: creating an ADMIN is a super admin act. The refusal has to
    come before the INSERT — a staff row with the wrong role is worse than none."""
    db = _Db()
    payload = StaffUserCreate(**_create_params(user_type="ADMIN"))

    with pytest.raises(PermissionDeniedException) as exc:
        await _hire(db, payload, _scope(CUTTACK))

    assert exc.value.code == "STAFF_ADMIN_CREATION_SUPER_ONLY"
    assert not any("INSERT" in sql for sql, _ in db.calls)


@pytest.mark.asyncio
async def test_a_manager_hires_into_their_own_branch(no_profile_write) -> None:
    """The body never decides the office for a manager-hired account: the caller's
    branch does. Reading the INSERT parameters is the only way to prove that."""
    db = _Db(None, 0)  # no duplicate, zero of this role so far
    payload = StaffUserCreate(**_create_params())

    await _hire(db, payload, _scope(CUTTACK))

    inserted = db.sql_containing("INSERT INTO users")
    assert len(inserted) == 1
    assert inserted[0]["branch_id"] == CUTTACK["id"]
    assert inserted[0]["user_type"] == "CCO"


@pytest.mark.asyncio
async def test_a_manager_cannot_hire_into_another_branch(no_profile_write) -> None:
    db = _Db()
    payload = StaffUserCreate(**_create_params(branch_id=HQ["id"]))

    with pytest.raises(PermissionDeniedException) as exc:
        await _hire(db, payload, _scope(CUTTACK))

    assert exc.value.code == "STAFF_BRANCH_OUT_OF_SCOPE"


@pytest.mark.asyncio
async def test_a_super_admin_has_to_say_which_branch(no_profile_write) -> None:
    """An unassigned staff row is not "global", it is invisible to every branch —
    so hiring without naming an office has to be an error, not a default."""
    db = _Db()
    payload = StaffUserCreate(**_create_params())

    with pytest.raises(ValidationException):
        await _hire(db, payload, _scope(HQ, is_super=True))


@pytest.mark.asyncio
async def test_a_super_admins_branch_has_to_be_open(no_profile_write) -> None:
    closed = SimpleNamespace(
        id=3, name="Puri", code="URI", city_id=30,
        is_headquarters=False, is_active=False,
    )
    db = _Db(None, branch=closed)
    payload = StaffUserCreate(**_create_params(branch_id=3))

    with pytest.raises(PermissionDeniedException) as exc:
        await _hire(db, payload, _scope(HQ, is_super=True))

    assert exc.value.code == "BRANCH_INACTIVE"


@pytest.mark.asyncio
async def test_no_route_here_creates_a_master_admin(no_profile_write) -> None:
    """StaffUserCreate's validator already refuses SUPER_ADMIN; model_construct
    skips validation, which is how a caller could reach this — so the service
    refuses too."""
    db = _Db()
    payload = StaffUserCreate.model_construct(**_create_params(user_type="SUPER_ADMIN"))

    with pytest.raises(PermissionDeniedException):
        await _hire(db, payload, _scope(HQ, is_super=True))


# ── Editing ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_another_branchs_staff_reads_as_not_found() -> None:
    """404, not 403: a manager is not told that the id exists and is someone
    else's. Same answer as a row that was never created."""
    db = _Db({"user_type": "CCO", "branch_id": HQ["id"]})

    with pytest.raises(ResourceNotFoundException):
        await AdminDashboardService.assert_staff_row(db, _scope(CUTTACK), uuid4())


@pytest.mark.asyncio
async def test_a_manager_cannot_edit_a_peer_manager() -> None:
    other = uuid4()
    db = _Db({"user_type": "ADMIN", "branch_id": CUTTACK["id"]})

    with pytest.raises(PermissionDeniedException) as exc:
        await AdminDashboardService.assert_staff_row(
            db, _scope(CUTTACK), other, for_write=True
        )

    assert exc.value.code == "STAFF_ADMIN_ROW_SUPER_ONLY"


@pytest.mark.asyncio
async def test_a_manager_can_edit_their_own_row() -> None:
    me = uuid4()
    db = _Db({"user_type": "ADMIN", "branch_id": CUTTACK["id"]})

    row = await AdminDashboardService.assert_staff_row(
        db, _scope(CUTTACK, actor_id=str(me)), me, for_write=True
    )

    assert row["user_type"] == "ADMIN"


@pytest.mark.asyncio
async def test_reading_a_peer_manager_is_allowed() -> None:
    """The branch's own list already shows its managers, so a read cannot be the
    thing that leaks it. Only the write is refused."""
    db = _Db({"user_type": "ADMIN", "branch_id": CUTTACK["id"]})

    row = await AdminDashboardService.assert_staff_row(
        db, _scope(CUTTACK), uuid4(), for_write=False
    )

    assert row["branch_id"] == CUTTACK["id"]


@pytest.mark.asyncio
async def test_a_manager_cannot_move_anyone_to_another_branch(no_profile_write) -> None:
    """Silently dropping the field would leave the UI reporting a save that did
    not happen, so this answers 403 and says why."""
    target = uuid4()
    db = _Db({"user_type": "CCO", "branch_id": CUTTACK["id"]})
    payload = StaffUserUpdate.model_construct(
        first_name=None, last_name=None, email=None, mobile_number=None,
        user_type=None, profile_image_url=None, branch_id=HQ["id"], profile=None,
    )

    with pytest.raises(PermissionDeniedException) as exc:
        await AdminDashboardService.update_staff_user(db, target, payload, _scope(CUTTACK))

    assert exc.value.code == "STAFF_BRANCH_MOVE_SUPER_ONLY"


@pytest.mark.asyncio
async def test_moving_someones_branch_signs_them_out(no_profile_write) -> None:
    """Not what makes the move take effect — enforcement re-reads the row. This
    is so the old branch's screens are not still open when they come back."""
    target = uuid4()
    reopened = SimpleNamespace(
        id=HQ["id"], name="Bhubaneswar", code="BBS", city_id=10,
        is_headquarters=True, is_active=True,
    )
    db = _Db({"user_type": "CCO", "branch_id": CUTTACK["id"]}, None, branch=reopened)
    payload = StaffUserUpdate.model_construct(
        first_name=None, last_name=None, email=None, mobile_number=None,
        user_type=None, profile_image_url=None, branch_id=HQ["id"], profile=None,
    )

    ended: list = []

    class _Sessions:
        def __init__(self, _db):
            pass

        async def deactivate_all_user_sessions(self, user_id):
            ended.append(user_id)

    import app.modules.auth.repositories as repos

    monkeypatch_attr = repos.SessionRepository
    repos.SessionRepository = _Sessions
    try:
        await AdminDashboardService.update_staff_user(
            db, target, payload, _scope(HQ, is_super=True)
        )
    finally:
        repos.SessionRepository = monkeypatch_attr

    assert ended == [target]


# ── The last master admin ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_last_master_admin_cannot_be_demoted_or_suspended() -> None:
    """Every branch, every hire, every move is behind this account. Losing it is
    not an inconvenience, it is the end of the platform's admin story."""
    db = _Db(1)
    row = {"user_type": "SUPER_ADMIN", "branch_id": None}

    with pytest.raises(BusinessException) as exc:
        await AdminDashboardService.refuse_losing_last_super_admin(
            db, row, would_end_mastership=True
        )

    assert exc.value.code == "LAST_SUPER_ADMIN"


@pytest.mark.asyncio
async def test_a_second_master_admin_unlocks_the_change() -> None:
    await AdminDashboardService.refuse_losing_last_super_admin(
        _Db(2), {"user_type": "SUPER_ADMIN", "branch_id": None},
        would_end_mastership=True,
    )


@pytest.mark.asyncio
async def test_an_ordinary_edit_to_a_master_admin_is_not_a_demotion() -> None:
    """Renaming a master admin must not trip the guard — only a change that ends
    the role does, and the caller says so explicitly."""
    await AdminDashboardService.refuse_losing_last_super_admin(
        _Db(), {"user_type": "SUPER_ADMIN", "branch_id": None},
        would_end_mastership=False,
    )


@pytest.mark.asyncio
async def test_a_manager_row_never_reaches_the_master_admin_guard() -> None:
    await AdminDashboardService.refuse_losing_last_super_admin(
        _Db(), {"user_type": "CCO", "branch_id": CUTTACK["id"]},
        would_end_mastership=True,
    )
