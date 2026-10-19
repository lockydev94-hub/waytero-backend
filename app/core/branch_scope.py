# ============================================================
# WAYTERO — BRANCH SCOPE
# File: app/core/branch_scope.py
# Doc Ref: migration 0085 (branches, users.branch_id); Admin Portal §38 (roles).
#
# One place that answers "which rows may this caller see".
#
# A branch is one city (owner decision, enforced by uq_branches_city), and the
# city is already on every transactional row, so scoping is a city predicate —
# not a second key on every table. BranchScope carries the cities a caller may
# read plus whether rows with no city yet are theirs, and the helpers below turn
# that into a WHERE clause (raw SQL), a .where() (ORM), or a 404 (single row).
#
# Three rules the rest of the file exists to enforce:
#
#   1. Enforcement reads the DATABASE row, never the JWT. get_current_user loads
#      the User anyway, so branch facts are attached there; a token issued before
#      the super admin moved someone's branch is stale on its own terms, and the
#      next request is already correct.
#
#   2. Fail closed. A staff account whose branch cannot be resolved is denied,
#      not treated as "no restriction". Missing data must never widen access.
#
#   3. Out-of-scope reads are 404, not 403. A 403 tells a Cuttack manager that
#      booking 4127 exists and is Bhubaneswar's; a 404 tells them nothing, which
#      is the same answer they would get if it did not exist at all.
# ============================================================

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import Depends, Request
from sqlalchemy import Select, ColumnElement, or_, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
)
from app.core.logging import get_logger
from app.modules.master.models import Branch

logger = get_logger(__name__)

# The roles that work inside one branch. SUPER_ADMIN is deliberately absent:
# one master admin, every branch.
STAFF_BRANCH_TYPES = frozenset(
    {"ADMIN", "CCO", "VERIFICATION_OFFICER", "FINANCE_MANAGER"}
)
SUPER_ADMIN_TYPE = "SUPER_ADMIN"

# The super admin's topbar branch switcher sends this; a branch user sending it
# is ignored, never honoured (get_branch_scope only reads it for SUPER_ADMIN).
BRANCH_HEADER = "X-Branch-Id"

# Identifiers interpolated into SQL by require_row_scope come from source code,
# never from a request — this assertion keeps that a fact rather than a habit.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


@dataclass(frozen=True)
class BranchScope:
    """What one request is allowed to see.

    `cities` is the whole answer: empty means every city (an unfiltered super
    admin), one entry means that branch, and `include_unassigned` says whether
    rows nobody has given a city yet are also theirs — true only for the head
    office, which is where an enquiry with no destination goes to be worked.
    """

    actor_id: str
    user_type: str
    is_super: bool
    branch_id: Optional[int]
    branch_name: Optional[str]
    is_headquarters: bool
    cities: frozenset[int]
    include_unassigned: bool

    @property
    def restricted(self) -> bool:
        return bool(self.cities)

    @property
    def city_id(self) -> Optional[int]:
        """The single city when there is exactly one, else None.

        One branch is one city, so this is the normal case; it exists so a
        caller that has its own `city_id` parameter can reuse it.
        """
        if len(self.cities) == 1:
            return next(iter(self.cities))
        return None

    def allows_city(self, city_id: Optional[int]) -> bool:
        if not self.restricted:
            return True
        if city_id is None:
            return self.include_unassigned
        return city_id in self.cities


async def attach_branch(
    db: AsyncSession, payload: dict[str, Any], branch_id: int
) -> None:
    """Put the caller's branch on the auth payload, from the row.

    Called by get_current_user (lazily, to keep the two modules free of an
    import cycle) so every scoped decision is one dict lookup away instead of a
    second query per endpoint. A dangling id becomes None and then fails closed
    in _scope_for_staff — FK RESTRICT makes that unreachable, but "unreachable"
    is not the same as "safe to treat as no restriction".
    """
    branch = await db.get(Branch, branch_id)
    payload["branch"] = (
        None
        if branch is None
        else {
            "id": branch.id,
            "name": branch.name,
            "code": branch.code,
            "city_id": branch.city_id,
            "is_headquarters": branch.is_headquarters,
            "is_active": branch.is_active,
        }
    )


def _slug(column: str) -> str:
    return re.sub(r"\W+", "_", column).strip("_") or "column"


def city_scope_sql(
    scope: BranchScope,
    column: str,
    *,
    allow_unassigned: bool = True,
    param: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """WHERE fragment + params for a raw-SQL query.

    `column` must be qualified (`e.city_id`, `mb.city_id`) because these
    fragments are appended to queries that already join several tables.

    `allow_unassigned=False` is for a city reached through a LEFT JOIN: there a
    NULL means "the join found nothing", not "head office work", and letting it
    match would hand another branch's row to HQ.

    ANY(:name) with a list is the same shape capture_admin_api already uses for
    `= ANY(:mobiles)`, so one code path covers one city and (later) many.
    """
    if not scope.restricted:
        return "1 = 1", {}

    name = param or f"scope_city_{_slug(column)}"
    clause = f"{column} = ANY(:{name})"
    if scope.include_unassigned and allow_unassigned:
        clause = f"({clause} OR {column} IS NULL)"
    return clause, {name: sorted(scope.cities)}


class ScopeSql:
    """One statement's worth of city predicates, with their params collected.

    `city_scope_sql` returns a clause *and* the parameter it needs, and a
    dashboard query wants half a dozen of them in a single statement. Doing that
    pairing here instead of at each call site is what turns two mistakes that are
    otherwise easy to make — a clause whose param was never bound (a runtime
    error, caught by luck) and a clause bound to another fragment's param (a
    wrong number, caught by nothing) — into one that cannot happen.
    """

    def __init__(self, scope: BranchScope) -> None:
        self.scope = scope
        self.params: dict[str, Any] = {}

    def city(
        self,
        column: str,
        *,
        param: str,
        allow_unassigned: bool = True,
    ) -> str:
        """The predicate for `column`, its param registered under `param`.

        `param` is required because two fragments of one statement must not
        share a bind name: the same list bound twice under one name is the same
        value, but a name reused across *different* columns is how a clause ends
        up filtering on the wrong one.
        """
        if not _IDENTIFIER.match(param):
            raise ValueError(f"ScopeSql.city: {param} must be a bare identifier")
        clause, extra = city_scope_sql(
            self.scope, column, param=param, allow_unassigned=allow_unassigned
        )
        self.params.update(extra)
        return clause

    def through_bookings(self, alias: str, *, param: str) -> str:
        """A service row's city is its master booking's.

        cab_bookings, hotel_reservations and tour_bookings carry no location of
        their own — the master booking they hang off does, which is why no
        service table needed a column in migration 0085. EXISTS rather than a
        join, because these fragments are COUNTs and a join would let one master
        with two services be counted twice.
        """
        if not _IDENTIFIER.match(alias):
            raise ValueError(f"ScopeSql.through_bookings: {alias} must be a bare identifier")
        clause = self.city("mb.city_id", param=param, allow_unassigned=False)
        return (
            f"EXISTS (SELECT 1 FROM master_bookings mb "
            f"WHERE mb.id = {alias}.master_booking_id AND {clause})"
        )


def orm_scope_filter(
    stmt: Select,
    column: ColumnElement,
    scope: BranchScope,
    *,
    allow_unassigned: bool = True,
) -> Select:
    """The same predicate for a SQLAlchemy select."""
    if not scope.restricted:
        return stmt
    predicate = column.in_(sorted(scope.cities))
    if scope.include_unassigned and allow_unassigned:
        predicate = or_(predicate, column.is_(None))
    return stmt.where(predicate)


def scope_header_branch_id(request: Request) -> Optional[int]:
    """The X-Branch-Id value as an int, or None when absent."""
    raw = request.headers.get(BRANCH_HEADER)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise BusinessException(
            f"{BRANCH_HEADER} must be a branch id", code="BRANCH_HEADER_INVALID"
        )


async def load_active_branch(db: AsyncSession, branch_id: int) -> Branch:
    """The branch row, or the failure that goes with not having one.

    Public because assigning a person to an office needs the same two answers
    as the super admin's switcher: no such branch (404) and a closed one (403).
    A staff route that skipped this would happily file a new hire under an
    office that no longer exists.
    """
    branch = await db.get(Branch, branch_id)
    if branch is None:
        raise ResourceNotFoundException("Branch", branch_id)
    if not branch.is_active:
        raise PermissionDeniedException(
            "That branch is closed", code="BRANCH_INACTIVE"
        )
    return branch


def _scope_for_staff(current_user: dict[str, Any]) -> BranchScope:
    """A branch account sees its own city, plus unassigned rows if it is HQ.

    A missing branch here means the row could not be resolved — for a staff
    account that is a failure, not a free pass.
    """
    branch = current_user.get("branch")
    user_type = str(current_user.get("user_type") or current_user.get("role") or "")
    if not branch:
        logger.warning(
            "branch_scope_unresolved",
            actor_id=str(current_user.get("sub")),
            user_type=user_type,
        )
        raise PermissionDeniedException(
            "This account has no branch assigned. Sign in again or ask a "
            "super admin to assign one.",
            code="BRANCH_SCOPE_UNRESOLVED",
        )
    if not branch.get("is_active"):
        raise PermissionDeniedException(
            "Your branch is closed", code="BRANCH_INACTIVE"
        )
    return BranchScope(
        actor_id=str(current_user.get("sub")),
        user_type=user_type,
        is_super=False,
        branch_id=branch["id"],
        branch_name=branch["name"],
        is_headquarters=bool(branch["is_headquarters"]),
        cities=frozenset({branch["city_id"]}),
        include_unassigned=bool(branch["is_headquarters"]),
    )


async def _scope_for_super(
    db: AsyncSession, current_user: dict[str, Any], filter_branch_id: Optional[int]
) -> BranchScope:
    """Super admin: every city, unless the switcher picked one branch."""
    user_type = str(current_user.get("user_type") or current_user.get("role") or "")
    if filter_branch_id is None:
        return BranchScope(
            actor_id=str(current_user.get("sub")),
            user_type=user_type,
            is_super=True,
            branch_id=None,
            branch_name=None,
            is_headquarters=False,
            cities=frozenset(),
            include_unassigned=True,
        )
    branch = await load_active_branch(db, filter_branch_id)
    return BranchScope(
        actor_id=str(current_user.get("sub")),
        user_type=user_type,
        is_super=True,
        branch_id=branch.id,
        branch_name=branch.name,
        is_headquarters=bool(branch.is_headquarters),
        cities=frozenset({branch.city_id}),
        include_unassigned=bool(branch.is_headquarters),
    )


async def build_branch_scope(
    db: AsyncSession,
    current_user: dict[str, Any],
    *,
    filter_branch_id: Optional[int] = None,
) -> BranchScope:
    """Scope for any payload, without a Request — for service-layer callers.

    Only a super admin may narrow to a branch: a header from a branch user is
    ignored rather than honoured, or the switcher would be a way to leave your
    own branch.
    """
    user_type = str(current_user.get("user_type") or current_user.get("role") or "")
    if user_type == SUPER_ADMIN_TYPE:
        return await _scope_for_super(db, current_user, filter_branch_id)
    if user_type in STAFF_BRANCH_TYPES:
        return _scope_for_staff(current_user)
    raise PermissionDeniedException(
        "Branch scope is undefined for this account", code="BRANCH_SCOPE_NOT_APPLICABLE"
    )


async def get_branch_scope(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> BranchScope:
    """Dependency form of build_branch_scope, resolved once per request.

    Cached on request.state because a single list endpoint can want it three
    times: the count query, the page query and the aggregate above them.
    """
    cached = getattr(request.state, "branch_scope", None)
    if cached is not None:
        return cached
    scope = await build_branch_scope(
        db, current_user, filter_branch_id=scope_header_branch_id(request)
    )
    request.state.branch_scope = scope
    return scope


def _coerce_id(raw: Any) -> Any:
    """Bind a path parameter with the type its column expects.

    request.path_params is read before FastAPI casts anything, so every id
    arrives as a string. asyncpg will not quietly turn "12" into a BIGINT or a
    UUID, so the conversion happens here rather than in a database error.
    """
    text_value = str(raw)
    if text_value.isdigit():
        return int(text_value)
    try:
        return uuid.UUID(text_value)
    except (ValueError, AttributeError):
        return text_value


def require_path_row_scope(
    *,
    table: str,
    id_param: str,
    city_column: str,
    id_column: str = "id",
    join: str = "",
    label: Optional[str] = None,
    allow_unassigned: bool = True,
) -> Callable:
    """Router-level guard for resources addressed by an id in the path.

    Attached to a router once instead of copied into every route: the bookings
    router has ~30 of them, and a check that lives in each handler is a check
    that goes missing from one. Because it is a dependency it runs before the
    handler, so the row is never loaded, mutated or emailed before it has been
    established that this caller's branch owns it.

    Routes whose path has no `id_param` (list pages, queues, the resource
    pickers) fall through untouched — they are scoped in their own query, where
    the city column is already joined.
    """

    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db),
        scope: BranchScope = Depends(get_branch_scope),
    ) -> None:
        raw = request.path_params.get(id_param)
        if raw is None:
            return
        await require_row_scope(
            db,
            scope,
            table=table,
            id_column=id_column,
            id_value=_coerce_id(raw),
            city_column=city_column,
            join=join,
            label=label,
            allow_unassigned=allow_unassigned,
        )

    return dependency


async def require_row_scope(
    db: AsyncSession,
    scope: BranchScope,
    *,
    table: str,
    id_column: str,
    id_value: Any,
    city_column: str,
    join: str = "",
    label: Optional[str] = None,
    allow_unassigned: bool = True,
) -> None:
    """Deny a by-id read/write on a row that belongs to another branch.

    Call it before the mutation on PATCH/{id} routes: the list pages already
    filter, but a route that takes an id in the path has to check the row.

    Both "no such row" and "row is not yours" raise 404 (see rule 3 in the
    header comment). The denial is written to the application log, not to
    audit_logs: get_db rolls the transaction back when a route raises, so a
    DB row written here would be discarded on exactly the requests it records.
    """
    if not scope.restricted:
        return

    for name, value in (
        ("table", table),
        ("id_column", id_column),
        ("city_column", city_column),
    ):
        if not _IDENTIFIER.match(value):
            raise ValueError(f"require_row_scope: {name} must be a bare identifier")

    sql = sa_text(
        f"SELECT {city_column} AS city FROM {table} {join} "
        f"WHERE {id_column} = :__scope_row_id"
    )
    row = (await db.execute(sql, {"__scope_row_id": id_value})).first()
    city = row[0] if row is not None else None

    if row is None:
        reason: Optional[str] = "row_not_found"
    elif city is None:
        unassigned_is_theirs = scope.include_unassigned and allow_unassigned
        reason = None if unassigned_is_theirs else "row_unassigned"
    elif city in scope.cities:
        reason = None
    else:
        reason = "row_in_other_branch"

    if reason is not None:
        logger.warning(
            "branch_scope_denied",
            actor_id=scope.actor_id,
            user_type=scope.user_type,
            branch_id=scope.branch_id,
            table=table,
            row_id=str(id_value),
            reason=reason,
            row_city=city,
            scope_cities=sorted(scope.cities),
        )
        raise ResourceNotFoundException(
            label or table.replace("_", " ").capitalize(), id_value
        )
