# ============================================================
# WAYTERO — BRANCH ADMIN SERVICE UNIT TESTS
# File: tests/unit/test_branch_admin_service.py
# Doc Ref: migration 0085 (branches); app/modules/admin/services/branches.py
#
# The office register's rules are three facts the database cannot hold (one
# office per city, one head office, no deleting an office something still points
# at), so they live in the service and are tested here against a recording fake.
# What matters is not only that a rule raises, but that the refusing path wrote
# nothing — a check that runs after the INSERT protects nobody.
# ============================================================

import pytest
from pydantic import ValidationError

from app.core.exceptions import BusinessException, ValidationException
from app.modules.admin.branch_api import BranchCreate, BranchUpdate
from app.modules.admin.services import branches as service

HQ_ROW = {
    "id": 1, "name": "Bhubaneswar", "code": "BBS", "city_id": 10,
    "address": None, "is_headquarters": True, "is_active": True,
    "city_name": "Bhubaneswar", "state_name": "Odisha",
}
CUTTACK_ROW = {
    "id": 2, "name": "Cuttack", "code": "CTK", "city_id": 20,
    "address": "Bank Square", "is_headquarters": False, "is_active": True,
    "city_name": "Cuttack", "state_name": "Odisha",
}
NEW_ROW = {
    "id": 7, "name": "Puri", "code": "URI", "city_id": 30,
    "address": None, "is_headquarters": False, "is_active": True,
    "city_name": "Puri", "state_name": "Odisha",
}

CITY_IS_ACTIVE = "SELECT 1 FROM cities"
INSERT_BRANCH = "INSERT INTO branches"
UPDATE_BRANCH = "UPDATE branches"
DELETE_BRANCH = "DELETE FROM branches"
READ_ONE_BRANCH = "WHERE b.id = :branch_id"


class _Result:
    """One statement's answer, in whichever shape the service asks for it."""

    def __init__(self, rows) -> None:
        self._rows = rows or []

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def scalar_one(self):
        if not self._rows:
            raise AssertionError("expected exactly one row, got none")
        return self._rows[0][0]


class _RecordingDb:
    """Answers by SQL fragment and remembers every statement it was given.

    Fragments are matched in insertion order, so the more specific one has to be
    listed first. A statement nothing answers returns no rows, which every
    "already taken?" probe in the service reads as free.
    """

    def __init__(self, **answers) -> None:
        self._answers = answers
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, clause, params=None) -> _Result:
        sql = str(clause)
        self.calls.append((sql, dict(params or {})))
        for fragment, rows in self._answers.items():
            if fragment in sql:
                return _Result(rows)
        return _Result(None)

    async def flush(self) -> None:
        """The service flushes and never commits — get_db owns the transaction."""

    def writes(self) -> list[tuple[str, dict]]:
        return [
            (sql, params)
            for sql, params in self.calls
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]

    def last_write(self) -> tuple[str, dict]:
        writes = self.writes()
        assert writes, "expected a write statement to have run"
        return writes[-1]


class _Sent:
    """A PATCH body that reports exactly the fields the caller put in it."""

    def __init__(self, **fields) -> None:
        self._fields = fields

    def model_dump(self, *, exclude_unset: bool = False) -> dict:
        return dict(self._fields)


# ── creating ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_new_office_answers_with_the_city_joined_in() -> None:
    db = _RecordingDb(
        **{
            CITY_IS_ACTIVE: [(1,)],
            INSERT_BRANCH: [(7,)],
            READ_ONE_BRANCH: [NEW_ROW],
        }
    )
    created = await service.create_branch(
        db, name="Puri", code="URI", city_id=30, address=None,
        is_headquarters=False, is_active=True,
    )
    # The caller gets the same shape the list gives, city name included — the
    # portal must not need a second request to render what it just created.
    assert created["id"] == 7
    assert created["city_name"] == "Puri"


@pytest.mark.asyncio
async def test_a_city_already_has_its_office() -> None:
    db = _RecordingDb(
        **{
            CITY_IS_ACTIVE: [(1,)],
            "WHERE city_id = :city_id": [(2, "CTK")],
        }
    )
    with pytest.raises(BusinessException) as exc:
        await service.create_branch(
            db, name="Cuttack North", code="CTN", city_id=20, address=None,
            is_headquarters=False, is_active=True,
        )
    assert exc.value.code == "BRANCH_CITY_TAKEN"
    assert "CTK" in exc.value.message
    assert db.writes() == []


@pytest.mark.asyncio
async def test_the_code_is_taken_even_in_another_case() -> None:
    db = _RecordingDb(**{CITY_IS_ACTIVE: [(1,)], "UPPER(code)": [(2,)]})
    with pytest.raises(BusinessException) as exc:
        await service.create_branch(
            db, name="Second Cuttack", code="ctk", city_id=21, address=None,
            is_headquarters=False, is_active=True,
        )
    assert exc.value.code == "BRANCH_CODE_TAKEN"
    assert db.writes() == []


@pytest.mark.asyncio
async def test_only_one_head_office_exists() -> None:
    db = _RecordingDb(
        **{CITY_IS_ACTIVE: [(1,)], "is_headquarters = TRUE": [(1, "BBS")]}
    )
    with pytest.raises(BusinessException) as exc:
        await service.create_branch(
            db, name="Puri", code="URI", city_id=30, address=None,
            is_headquarters=True, is_active=True,
        )
    assert exc.value.code == "BRANCH_HEADQUARTERS_EXISTS"
    assert db.writes() == []


@pytest.mark.asyncio
async def test_the_city_must_be_a_real_active_one() -> None:
    db = _RecordingDb(**{CITY_IS_ACTIVE: []})
    with pytest.raises(ValidationException):
        await service.create_branch(
            db, name="Nowhere", code="NWX", city_id=999, address=None,
            is_headquarters=False, is_active=True,
        )
    assert db.writes() == []


# ── editing ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_partial_edit_writes_only_the_field_that_was_sent() -> None:
    db = _RecordingDb(**{READ_ONE_BRANCH: [CUTTACK_ROW], UPDATE_BRANCH: []})
    await service.update_branch(db, 2, {"name": "Cuttack Head Branch"})
    sql, params = db.last_write()
    assert "name = :name" in sql
    # The two things a partial save must never do by omission: blank the address
    # and close the office.
    assert "is_active" not in sql
    assert "address" not in sql
    assert params == {"name": "Cuttack Head Branch", "branch_id": 2}
    assert "updated_at = NOW()" in sql


@pytest.mark.asyncio
async def test_keeping_the_same_city_does_not_take_the_city_from_itself() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW], UPDATE_BRANCH: [],
           "AND id <> :exclude": []}
    )
    await service.update_branch(db, 2, {"city_id": 20, "code": "CTK2"})
    # The office did not move, so no probe for another owner of its city: a
    # self-collision would refuse a legitimate code change.
    assert not any("WHERE city_id = :city_id" in sql for sql, _ in db.calls)
    assert db.last_write()[1]["code"] == "CTK2"


@pytest.mark.asyncio
async def test_a_field_the_register_does_not_have_is_refused() -> None:
    db = _RecordingDb()
    with pytest.raises(ValidationException):
        await service.update_branch(db, 2, {"name": "x", "password": "y"})
    assert db.calls == []


@pytest.mark.asyncio
async def test_promoting_an_office_is_allowed_when_no_head_office_exists() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW], "is_headquarters = TRUE": [],
           UPDATE_BRANCH: []}
    )
    await service.update_branch(db, 2, {"is_headquarters": True})
    assert "is_headquarters = :is_headquarters" in db.last_write()[0]


@pytest.mark.asyncio
async def test_promoting_a_second_head_office_is_refused() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW], "is_headquarters = TRUE": [(1, "BBS")]}
    )
    with pytest.raises(BusinessException) as exc:
        await service.update_branch(db, 2, {"is_headquarters": True})
    assert exc.value.code == "BRANCH_HEADQUARTERS_EXISTS"
    assert db.writes() == []


@pytest.mark.asyncio
async def test_renaming_an_office_never_reopens_a_taken_city() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW], CITY_IS_ACTIVE: [(1,)],
           "WHERE city_id = :city_id": [(9, "ZZ")],
           UPDATE_BRANCH: []}
    )
    with pytest.raises(BusinessException) as exc:
        await service.update_branch(db, 2, {"city_id": 44, "code": "NEW"})
    assert exc.value.code == "BRANCH_CITY_TAKEN"
    assert db.writes() == []


# ── deleting ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_office_with_staff_is_not_deleted() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW],
           "SELECT COUNT(*) FROM users WHERE branch_id": [(3,)]}
    )
    with pytest.raises(BusinessException) as exc:
        await service.delete_branch(db, 2)
    assert exc.value.code == "BRANCH_HAS_STAFF"
    assert db.writes() == []


@pytest.mark.asyncio
async def test_an_office_with_bookings_is_not_deleted() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW],
           "SELECT COUNT(*) FROM users WHERE branch_id": [(0,)],
           "SELECT COUNT(*) FROM master_bookings WHERE city_id": [(148,)]}
    )
    with pytest.raises(BusinessException) as exc:
        await service.delete_branch(db, 2)
    assert exc.value.code == "BRANCH_HAS_BOOKINGS"
    assert "Close the branch" in exc.value.message
    assert db.writes() == []


@pytest.mark.asyncio
async def test_an_empty_office_deletes() -> None:
    db = _RecordingDb(
        **{READ_ONE_BRANCH: [CUTTACK_ROW],
           "SELECT COUNT(*) FROM users WHERE branch_id": [(0,)],
           "SELECT COUNT(*) FROM master_bookings WHERE city_id": [(0,)],
           DELETE_BRANCH: []}
    )
    deleted = await service.delete_branch(db, 2)
    assert deleted["code"] == "CTK"
    assert DELETE_BRANCH in db.last_write()[0]


# ── what a PATCH body is allowed to mean ─────────────────────────


def test_an_omitted_field_is_not_a_write() -> None:
    assert service.clean_update(BranchUpdate()) == {}


def test_a_sent_false_is_still_a_write() -> None:
    # The trap this helper exists for: `is_active: false` is a real instruction,
    # and a naive "drop falsy values" filter would swallow it.
    assert service.clean_update(BranchUpdate(is_active=False)) == {"is_active": False}


def test_clearing_an_address_is_an_explicit_null() -> None:
    assert service.clean_update(BranchUpdate(address=None)) == {"address": None}


def test_a_field_a_client_cannot_write_is_dropped_not_trusted() -> None:
    # The UPDATE's column list is built from what clean_update returns, so this
    # filter is also the guard against a key becoming SQL.
    assert service.clean_update(_Sent(is_active=False, branch_id=9)) == {
        "is_active": False
    }


def test_a_null_name_is_a_400_not_a_silent_no_op() -> None:
    with pytest.raises(ValidationError):
        BranchUpdate(name=None)
    with pytest.raises(ValidationError):
        BranchCreate(name="   ")


def test_codes_are_stored_canonically() -> None:
    payload = BranchCreate(name="Puri", code=" puRi ", city_id=30, address=None)
    assert payload.code == "PURI"
    assert BranchUpdate(code=" ctz ").code == "CTZ"
