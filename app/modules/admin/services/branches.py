# ============================================================
# WAYTERO — BRANCH ADMIN SERVICE
# File: app/modules/admin/services/branches.py
# Doc Ref: migration 0085 (branches); Admin Portal §38 (branch scoping)
#
# The office register. A branch is the unit the whole portal now filters by, so
# writing one is a super-admin action: a branch manager who could create an
# office could move their own account into it, and every scoping rule would then
# be a rule about a row they authored.
#
# Two facts this service holds that the database cannot:
#
#   * one office per city — `uq_branches_city` enforces it, but a unique
#     violation arrives as a 500 with a constraint name in it, which is a bad way
#     to tell someone "Bhubaneswar already has a branch";
#   * exactly one head office. A UNIQUE partial index would be the strict
#     answer, and it would abort `alembic upgrade` on any database that already
#     has two — no head office is seeded, so the owner's dev database is exactly
#     such a database. Enforced here, reported by the scope audit endpoint.
#
# Never commits: get_db owns the transaction.
# ============================================================

from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.admin.cab_route_service import assert_city_active

# What a branch row looks like to every reader. `city_name` is joined rather
# than stored: the office is the city, and a second copy of its name is a second
# thing to keep true.
_LIST_SQL = """
    SELECT b.id, b.name, b.code, b.city_id, b.address,
           b.is_headquarters, b.is_active,
           c.name AS city_name, s.name AS state_name
    FROM branches b
    LEFT JOIN cities c ON c.id = b.city_id
    LEFT JOIN states s ON s.id = c.state_id
"""

# The only columns a PATCH may name. `id`, `city_name` and `state_name` are
# readable answers, not inputs.
_UPDATABLE = frozenset(
    {"name", "code", "city_id", "address", "is_headquarters", "is_active"}
)


async def list_branches(db: AsyncSession) -> List[Dict[str, Any]]:
    rows = (
        (
            await db.execute(
                text(_LIST_SQL + "ORDER BY b.is_headquarters DESC, c.name, b.name")
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def get_branch(db: AsyncSession, branch_id: int) -> Dict[str, Any]:
    row = (
        (
            await db.execute(
                text(_LIST_SQL + "WHERE b.id = :branch_id"), {"branch_id": branch_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ResourceNotFoundException("Branch", branch_id)
    return dict(row)


async def _assert_city_is_free(
    db: AsyncSession, city_id: int, *, exclude_branch_id: Optional[int] = None
) -> None:
    """One office per city — the owner's decision, checked before the constraint."""
    sql = "SELECT id, code FROM branches WHERE city_id = :city_id"
    params: Dict[str, Any] = {"city_id": city_id}
    if exclude_branch_id is not None:
        sql += " AND id <> :exclude"
        params["exclude"] = exclude_branch_id
    row = (await db.execute(text(sql), params)).first()
    if row is not None:
        raise BusinessException(
            f"{row[1]} is already this city's branch. One branch per city — edit "
            "that one instead.",
            code="BRANCH_CITY_TAKEN",
            details={"branch_id": row[0], "city_id": city_id},
        )


async def _assert_code_is_free(
    db: AsyncSession, code: str, *, exclude_branch_id: Optional[int] = None
) -> None:
    sql = "SELECT id FROM branches WHERE UPPER(code) = UPPER(:code)"
    params: Dict[str, Any] = {"code": code}
    if exclude_branch_id is not None:
        sql += " AND id <> :exclude"
        params["exclude"] = exclude_branch_id
    if (await db.execute(text(sql), params)).first() is not None:
        raise BusinessException(
            f"A branch already uses the code {code}.", code="BRANCH_CODE_TAKEN"
        )


async def _assert_single_headquarters(
    db: AsyncSession, *, exclude_branch_id: Optional[int] = None
) -> None:
    """Refusing a second HQ, not a second *candidate*.

    The head office is the branch that owns rows nobody has assigned a city to
    yet. Two of them is not a wider view — it is two accounts each convinced the
    other's unassigned work is theirs, and the portal would show both the same
    queue with different numbers.
    """
    sql = "SELECT name, code FROM branches WHERE is_headquarters = TRUE"
    params: Dict[str, Any] = {}
    if exclude_branch_id is not None:
        sql += " AND id <> :exclude"
        params["exclude"] = exclude_branch_id
    row = (await db.execute(text(sql), params)).first()
    if row is not None:
        raise BusinessException(
            f"{row[1]} is already the head office. There is one head office; make "
            "that one ordinary first if the headquarters is moving.",
            code="BRANCH_HEADQUARTERS_EXISTS",
        )


async def create_branch(
    db: AsyncSession,
    *,
    name: str,
    code: str,
    city_id: int,
    address: Optional[str],
    is_headquarters: bool,
    is_active: bool,
) -> Dict[str, Any]:
    await assert_city_active(db, city_id, "Branch city")
    await _assert_code_is_free(db, code)
    await _assert_city_is_free(db, city_id)
    if is_headquarters:
        await _assert_single_headquarters(db)

    result = await db.execute(
        text(
            """
            INSERT INTO branches (name, code, city_id, address, is_headquarters, is_active)
            VALUES (:name, :code, :city_id, :address, :is_headquarters, :is_active)
            RETURNING id
            """
        ),
        {
            "name": name,
            "code": code,
            "city_id": city_id,
            "address": address,
            "is_headquarters": is_headquarters,
            "is_active": is_active,
        },
    )
    await db.flush()

    # The new office answers through the same query the list uses, so the row the
    # caller gets back is the row with the city name joined — not a half-built
    # dict the portal would have to fill in with a second call.
    return await get_branch(db, result.scalar_one())


async def update_branch(
    db: AsyncSession, branch_id: int, changes: Dict[str, Any]
) -> Dict[str, Any]:
    """Apply only the fields that were sent.

    A partial save must not blank an address or close an office by omission —
    `is_active` in particular, because closing one logs its whole staff out.
    """
    unexpected = sorted(set(changes) - _UPDATABLE)
    if unexpected:
        # The column list is built from these keys, so this is also the guard
        # that keeps a caller's dict from naming something the SQL will paste in.
        raise ValidationException(
            f"Branch fields cannot be updated: {', '.join(unexpected)}"
        )

    current = await get_branch(db, branch_id)
    if not changes:
        return current

    if "city_id" in changes and changes["city_id"] != current["city_id"]:
        await assert_city_active(db, changes["city_id"], "Branch city")
        await _assert_city_is_free(db, changes["city_id"], exclude_branch_id=branch_id)
    if "code" in changes and (changes["code"] or "").upper() != current["code"].upper():
        await _assert_code_is_free(db, changes["code"], exclude_branch_id=branch_id)

    becoming_hq = changes.get("is_headquarters") is True and not current["is_headquarters"]
    if becoming_hq:
        await _assert_single_headquarters(db, exclude_branch_id=branch_id)

    columns = ", ".join(f"{key} = :{key}" for key in changes)
    params = dict(changes)
    params["branch_id"] = branch_id
    await db.execute(
        text(f"UPDATE branches SET {columns}, updated_at = NOW() WHERE id = :branch_id"),
        params,
    )
    await db.flush()
    return await get_branch(db, branch_id)


async def _counts_against_removal(db: AsyncSession, branch: Dict[str, Any]) -> None:
    """Refuse to delete an office anything still points at.

    Both are refusals rather than cascades on purpose. Deleting a branch that has
    staff would orphan their accounts against a branch id that no longer exists
    (`fk_users_branch` is RESTRICT, so the database would refuse it anyway — with
    a 500); deleting one whose city has bookings would quietly detach that
    history from the portal that filtered by it.
    """
    staff = (
        await db.execute(
            text("SELECT COUNT(*) FROM users WHERE branch_id = :branch_id"),
            {"branch_id": branch["id"]},
        )
    ).scalar() or 0
    if staff:
        raise BusinessException(
            f"{branch['code']} still has {staff} staff account"
            f"{'' if staff == 1 else 's'}. Move them to another branch first.",
            code="BRANCH_HAS_STAFF",
        )

    bookings = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM master_bookings WHERE city_id = :city_id"
            ),
            {"city_id": branch["city_id"]},
        )
    ).scalar() or 0
    if bookings:
        raise BusinessException(
            f"{branch['code']} still holds {bookings} booking"
            f"{'' if bookings == 1 else 's'}. Close the branch instead of "
            "deleting it, so its history stays readable.",
            code="BRANCH_HAS_BOOKINGS",
        )


async def delete_branch(db: AsyncSession, branch_id: int) -> Dict[str, Any]:
    branch = await get_branch(db, branch_id)
    await _counts_against_removal(db, branch)
    await db.execute(
        text("DELETE FROM branches WHERE id = :branch_id"), {"branch_id": branch_id}
    )
    await db.flush()
    return branch


def clean_update(payload) -> Dict[str, Any]:
    """Only the fields the caller actually sent, ready for the UPDATE.

    Lives here because the rule is the service's: `exclude_unset` is what keeps
    an omitted `is_active` from becoming `false` and locking a branch out. A
    field the schema does not carry is dropped rather than trusted.
    """
    return {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if key in _UPDATABLE
    }
