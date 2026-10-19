# ============================================================
# WAYTERO — BRANCH SCOPE UNIT TESTS
# File: tests/unit/test_branch_scope.py
# Doc Ref: migration 0085; app/core/branch_scope.py
#
# The SQL fragments and the fail-closed rules are tested here, at the pure
# function boundary. Which endpoint a given branch can reach is an integration
# question and lives in tests/api/test_branch_scope_api.py.
# ============================================================

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.branch_scope import (
    BranchScope,
    STAFF_BRANCH_TYPES,
    attach_branch,
    build_branch_scope,
    city_scope_sql,
    orm_scope_filter,
    require_path_row_scope,
    require_row_scope,
    scope_header_branch_id,
)
from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
)
from app.modules.booking.models import MasterBooking

HQ = {"id": 1, "city_id": 10, "name": "Bhubaneswar", "code": "BBS",
      "is_headquarters": True, "is_active": True}
CUTTACK = {"id": 2, "city_id": 20, "name": "Cuttack", "code": "CTK",
           "is_headquarters": False, "is_active": True}


def _staff_payload(branch, user_type="CCO"):
    return {"sub": str(uuid4()), "user_type": user_type, "role": user_type,
            "branch": branch}


def _scope(branch, *, is_super=False, include_unassigned=None):
    return BranchScope(
        actor_id=str(uuid4()),
        user_type="SUPER_ADMIN" if is_super else "CCO",
        is_super=is_super,
        branch_id=branch["id"] if branch else None,
        branch_name=branch["name"] if branch else None,
        is_headquarters=branch["is_headquarters"] if branch else False,
        cities=frozenset() if branch is None else frozenset({branch["city_id"]}),
        include_unassigned=(
            (branch["is_headquarters"] if branch else True)
            if include_unassigned is None
            else include_unassigned
        ),
    )


# ── the clause shapes ────────────────────────────────────────────


def test_unrestricted_scope_adds_no_condition() -> None:
    scope = _scope(None, is_super=True)
    clause, params = city_scope_sql(scope, "e.city_id")
    assert clause == "1 = 1"
    assert params == {}
    # The ORM form must be a no-op too, not a WHERE false.
    stmt = select(1)
    assert str(orm_scope_filter(stmt, MasterBooking.city_id, scope)) == str(stmt)


def test_head_office_scope_sees_its_city_and_the_unassigned_rows() -> None:
    clause, params = city_scope_sql(_scope(HQ), "e.city_id")
    assert clause == "(e.city_id = ANY(:scope_city_e_city_id) OR e.city_id IS NULL)"
    assert params == {"scope_city_e_city_id": [10]}


def test_sub_branch_scope_excludes_the_unassigned_rows() -> None:
    clause, params = city_scope_sql(_scope(CUTTACK), "e.city_id")
    assert clause == "e.city_id = ANY(:scope_city_e_city_id)"
    assert params == {"scope_city_e_city_id": [20]}


def test_allow_unassigned_false_stops_a_left_join_null_reading_as_head_office() -> None:
    # mb.city_id is reached by LEFT JOIN: a NULL there means the join found
    # nothing, and it must not become "HQ's work".
    clause, _ = city_scope_sql(_scope(HQ), "mb.city_id", allow_unassigned=False)
    assert clause == "mb.city_id = ANY(:scope_city_mb_city_id)"


def test_param_names_do_not_collide_across_two_scoped_columns() -> None:
    scope = _scope(HQ)
    _, first = city_scope_sql(scope, "e.city_id")
    _, second = city_scope_sql(scope, "c.city_id")
    assert set(first) & set(second) == set()


def test_explicit_param_name_is_respected() -> None:
    _, params = city_scope_sql(_scope(CUTTACK), "cl.city_id", param="mine")
    assert params == {"mine": [20]}


def test_orm_filter_matches_the_sql_fragment() -> None:
    def render(stmt) -> str:
        return str(stmt.compile(compile_kwargs={"literal_binds": True}))

    cuttack = render(
        orm_scope_filter(select(MasterBooking), MasterBooking.city_id, _scope(CUTTACK))
    )
    assert "city_id IN (20)" in cuttack
    assert "IS NULL" not in cuttack

    head_office = render(
        orm_scope_filter(select(MasterBooking), MasterBooking.city_id, _scope(HQ))
    )
    assert "city_id IN (10)" in head_office
    assert "OR master_bookings.city_id IS NULL" in head_office


# ── who gets which scope ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_branch_user_gets_only_their_own_city() -> None:
    scope = await build_branch_scope(None, _staff_payload(CUTTACK))
    assert scope.is_super is False
    assert scope.cities == frozenset({20})
    assert scope.include_unassigned is False
    assert scope.branch_name == "Cuttack"


@pytest.mark.asyncio
async def test_head_office_user_also_owns_the_unassigned_rows() -> None:
    scope = await build_branch_scope(None, _staff_payload(HQ))
    assert scope.cities == frozenset({10})
    assert scope.include_unassigned is True


@pytest.mark.asyncio
async def test_a_staff_account_with_no_branch_on_the_payload_is_denied() -> None:
    # Fail closed: get_current_user did not attach a branch (test override,
    # stale payload), which must never read as "no restriction".
    payload = _staff_payload(CUTTACK)
    del payload["branch"]
    with pytest.raises(PermissionDeniedException) as exc:
        await build_branch_scope(None, payload)
    assert exc.value.code == "BRANCH_SCOPE_UNRESOLVED"


@pytest.mark.asyncio
async def test_a_branch_user_cannot_narrow_or_leave_with_the_header() -> None:
    # build_branch_scope only honours filter_branch_id for SUPER_ADMIN; the
    # dependency is what drops the header for everybody else.
    scope = await build_branch_scope(None, _staff_payload(CUTTACK), filter_branch_id=1)
    assert scope.cities == frozenset({20})


@pytest.mark.asyncio
async def test_a_closed_branch_locks_its_staff_out() -> None:
    closed = {**CUTTACK, "is_active": False}
    with pytest.raises(PermissionDeniedException) as exc:
        await build_branch_scope(None, _staff_payload(closed))
    assert exc.value.code == "BRANCH_INACTIVE"


@pytest.mark.asyncio
async def test_non_staff_accounts_have_no_branch_scope() -> None:
    # A partner reaching a scoped admin route is a routing bug elsewhere; the
    # scope refuses rather than defaulting to "everything".
    with pytest.raises(PermissionDeniedException):
        await build_branch_scope(None, _staff_payload(None, user_type="PARTNER"))


def test_super_admin_is_not_branch_scoped() -> None:
    assert "SUPER_ADMIN" not in STAFF_BRANCH_TYPES


# ── the super admin's switcher ───────────────────────────────────


class _FakeBranchDb:
    def __init__(self, branch) -> None:
        self._branch = branch
        self.requested: list[int] = []

    async def get(self, model, pk):
        self.requested.append(pk)
        return self._branch


@pytest.mark.asyncio
async def test_super_admin_with_a_header_sees_one_branch() -> None:
    db = _FakeBranchDb(SimpleNamespace(**CUTTACK))
    scope = await build_branch_scope(db, _staff_payload(None, "SUPER_ADMIN"), filter_branch_id=2)
    assert scope.is_super is True
    assert scope.cities == frozenset({20})
    assert scope.include_unassigned is False  # Cuttack is not the head office
    assert db.requested == [2]


@pytest.mark.asyncio
async def test_super_admin_filtering_the_head_office_keeps_the_unassigned_rows() -> None:
    db = _FakeBranchDb(SimpleNamespace(**HQ))
    scope = await build_branch_scope(db, _staff_payload(None, "SUPER_ADMIN"), filter_branch_id=1)
    assert scope.cities == frozenset({10})
    assert scope.include_unassigned is True


@pytest.mark.asyncio
async def test_super_admin_without_a_header_sees_every_city() -> None:
    db = _FakeBranchDb(SimpleNamespace(**HQ))
    scope = await build_branch_scope(db, _staff_payload(None, "SUPER_ADMIN"))
    assert scope.restricted is False
    assert db.requested == []


@pytest.mark.asyncio
async def test_switching_to_a_closed_branch_is_refused() -> None:
    db = _FakeBranchDb(SimpleNamespace(**{**CUTTACK, "is_active": False}))
    with pytest.raises(PermissionDeniedException) as exc:
        await build_branch_scope(db, _staff_payload(None, "SUPER_ADMIN"), filter_branch_id=2)
    assert exc.value.code == "BRANCH_INACTIVE"


@pytest.mark.asyncio
async def test_switching_to_a_branch_that_does_not_exist_is_404() -> None:
    db = _FakeBranchDb(None)
    with pytest.raises(ResourceNotFoundException):
        await build_branch_scope(db, _staff_payload(None, "SUPER_ADMIN"), filter_branch_id=99)


class _Headers:
    def __init__(self, values: dict) -> None:
        self._values = {k.lower(): v for k, v in values.items()}

    def get(self, key, default=None):
        return self._values.get(key.lower(), default)


class _Request:
    def __init__(self, values: dict, path_params: dict | None = None) -> None:
        self.headers = _Headers(values)
        self.path_params = path_params or {}


def test_branch_header_parsing() -> None:
    assert scope_header_branch_id(_Request({})) is None
    assert scope_header_branch_id(_Request({"X-Branch-Id": ""})) is None
    assert scope_header_branch_id(_Request({"X-Branch-Id": "7"})) == 7
    # Starlette headers are case-insensitive; the axios interceptor sets one
    # casing and nothing should depend on it.
    assert scope_header_branch_id(_Request({"x-branch-id": "7"})) == 7
    with pytest.raises(BusinessException):
        scope_header_branch_id(_Request({"X-Branch-Id": "all"}))


# ── single-row enforcement ───────────────────────────────────────


class _Result:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class _FakeScopeDb:
    def __init__(self, row) -> None:
        self._row = row
        self.sql: str = ""
        self.params: dict = {}

    async def execute(self, clause, params):
        self.sql = str(clause)
        self.params = params
        return _Result(self._row)


@pytest.mark.asyncio
async def test_row_in_another_branch_reads_as_not_found() -> None:
    db = _FakeScopeDb((10,))  # the row belongs to Bhubaneswar
    with pytest.raises(ResourceNotFoundException) as exc:
        await require_row_scope(
            db, _scope(CUTTACK), table="master_bookings",
            id_column="id", id_value=4127, city_column="city_id",
        )
    assert "4127" in exc.value.message
    assert db.params == {"__scope_row_id": 4127}


@pytest.mark.asyncio
async def test_a_missing_row_is_also_not_found() -> None:
    with pytest.raises(ResourceNotFoundException):
        await require_row_scope(
            _FakeScopeDb(None), _scope(CUTTACK), table="enquiries",
            id_column="id", id_value=5, city_column="city_id",
        )


@pytest.mark.asyncio
async def test_own_branch_row_passes() -> None:
    db = _FakeScopeDb((20,))
    await require_row_scope(
        db, _scope(CUTTACK), table="enquiries",
        id_column="id", id_value=5, city_column="city_id",
    )


@pytest.mark.asyncio
async def test_unassigned_row_belongs_to_the_head_office_only() -> None:
    db = _FakeScopeDb((None,))
    await require_row_scope(  # HQ: allowed
        db, _scope(HQ), table="enquiries",
        id_column="id", id_value=5, city_column="city_id",
    )
    with pytest.raises(ResourceNotFoundException):  # Cuttack: not theirs
        await require_row_scope(
            _FakeScopeDb((None,)), _scope(CUTTACK), table="enquiries",
            id_column="id", id_value=5, city_column="city_id",
        )


@pytest.mark.asyncio
async def test_super_admin_skips_the_row_query_entirely() -> None:
    db = _FakeScopeDb((10,))
    await require_row_scope(
        db, _scope(None, is_super=True), table="enquiries",
        id_column="id", id_value=5, city_column="city_id",
    )
    assert db.sql == ""


# ── the same rule, for a whole router ────────────────────────────
# One dependency on the bookings router stands in for ~30 per-handler checks, so
# these four tests are the reason that is safe: the id is read before the
# handler runs, the check is bound with the type the column wants, and a route
# that takes no id is left alone.


_BOOKING_GUARD = require_path_row_scope(
    table="master_bookings",
    id_param="booking_id",
    city_column="city_id",
    allow_unassigned=False,
    label="Booking",
)


@pytest.mark.asyncio
async def test_the_router_guard_refuses_another_branches_booking() -> None:
    db = _FakeScopeDb((10,))  # the row is Bhubaneswar's
    with pytest.raises(ResourceNotFoundException) as exc:
        await _BOOKING_GUARD(
            request=_Request({}, {"booking_id": "4127"}),
            db=db,
            scope=_scope(CUTTACK),
        )
    assert "4127" in exc.value.message
    # An int, not the string out of the path: asyncpg will not compare a BIGINT
    # with text, and the failure it raises is an InternalClientError.
    assert db.params == {"__scope_row_id": 4127}


@pytest.mark.asyncio
async def test_the_router_guard_passes_the_owners_row() -> None:
    db = _FakeScopeDb((20,))
    await _BOOKING_GUARD(
        request=_Request({}, {"booking_id": "4127"}), db=db, scope=_scope(CUTTACK)
    )
    assert db.params == {"__scope_row_id": 4127}


@pytest.mark.asyncio
async def test_the_router_guard_leaves_a_route_with_no_id_alone() -> None:
    """The list pages on the same router are scoped in their own query, where the
    city column is already joined — a guard that ran a pointless lookup on every
    one of them would cost a query to prove nothing."""
    db = _FakeScopeDb((10,))
    await _BOOKING_GUARD(request=_Request({}, {}), db=db, scope=_scope(CUTTACK))
    assert db.sql == ""


@pytest.mark.asyncio
async def test_the_router_guard_binds_a_uuid_path_id_as_a_uuid() -> None:
    conversation_id = uuid4()
    guard = require_path_row_scope(
        table="chat_conversations", id_param="conversation_id", city_column="city_id"
    )
    db = _FakeScopeDb((20,))
    await guard(
        request=_Request({}, {"conversation_id": str(conversation_id)}),
        db=db,
        scope=_scope(CUTTACK),
    )
    assert db.params == {"__scope_row_id": conversation_id}


@pytest.mark.asyncio
async def test_the_router_guard_asks_nothing_of_a_super_admin() -> None:
    db = _FakeScopeDb((10,))
    await _BOOKING_GUARD(
        request=_Request({}, {"booking_id": "4127"}),
        db=db,
        scope=_scope(None, is_super=True),
    )
    assert db.sql == ""


@pytest.mark.asyncio
async def test_row_scope_rejects_a_non_identifier() -> None:
    # The table/column strings are source code, never request data; this keeps
    # an accidental f-string of user input from becoming SQL injection.
    with pytest.raises(ValueError):
        await require_row_scope(
            _FakeScopeDb((20,)), _scope(CUTTACK), table="enquiries; DROP TABLE x",
            id_column="id", id_value=5, city_column="city_id",
        )


# ── what get_current_user attaches ───────────────────────────────


@pytest.mark.asyncio
async def test_attach_branch_writes_the_row_facts_onto_the_payload() -> None:
    payload: dict = {"sub": str(uuid4())}
    await attach_branch(_FakeBranchDb(SimpleNamespace(**CUTTACK)), payload, 2)
    assert payload["branch"] == {
        "id": 2, "city_id": 20, "name": "Cuttack", "code": "CTK",
        "is_headquarters": False, "is_active": True,
    }


@pytest.mark.asyncio
async def test_attach_branch_of_a_dangling_id_fails_closed() -> None:
    payload: dict = {"sub": str(uuid4())}
    await attach_branch(_FakeBranchDb(None), payload, 99)
    assert payload["branch"] is None
    with pytest.raises(PermissionDeniedException):
        await build_branch_scope(None, _staff_payload(payload["branch"]))
