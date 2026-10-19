"""
The dashboard aggregates and the sidebar badge UNION, as one branch would run
them (Round 14 — Admin Portal §38).

Two failure classes are worth pinning here rather than through an endpoint,
because neither one raises an error:

  * a fragment that counts the whole platform while the page under it counts one
    branch — the badge and the list disagree and both look plausible;
  * a scope predicate joined with AND to the last term of an OR chain, which is
    valid SQL and silently the wrong number ("trips needing assistance, or any
    completed trip without a cash record in my city").

Both are visible by reading the statement a caller would actually run, which is
what these tests do. The API tests run it against Postgres; they cannot tell
that the number is another branch's.
"""
import pytest

from app.core.branch_scope import BranchScope
from app.modules.admin.services.dashboard import (
    ACTION_COUNTS_CACHE_PREFIX,
    _action_counts_statement,
    action_counts_cache_key,
)

SUPER = BranchScope(
    actor_id="11111111-1111-1111-1111-111111111111",
    user_type="SUPER_ADMIN",
    is_super=True,
    branch_id=None,
    branch_name=None,
    is_headquarters=False,
    cities=frozenset(),
    include_unassigned=True,
)
HQ = BranchScope(
    actor_id="22222222-2222-2222-2222-222222222222",
    user_type="ADMIN",
    is_super=False,
    branch_id=1,
    branch_name="Bhubaneswar",
    is_headquarters=True,
    cities=frozenset({5}),
    include_unassigned=True,
)
CUTTACK = BranchScope(
    actor_id="33333333-3333-3333-3333-333333333333",
    user_type="ADMIN",
    is_super=False,
    branch_id=2,
    branch_name="Cuttack",
    is_headquarters=False,
    cities=frozenset({3}),
    include_unassigned=False,
)

NAV_KEYS = [
    "bookings",
    "cab-ops",
    "trip-assistance",
    "customer-care",
    "chat",
    "callback-requests",
    "contact-messages",
    "enquiries",
    "partner-applications",
    "guest-feedback",
    "account-deletion",
]


def _arms(sql: str) -> list[str]:
    return sql.split("\nUNION ALL\n")


def _nav_keys(sql: str) -> list[str]:
    out = []
    for arm in _arms(sql):
        marker = "SELECT '"
        start = arm.index(marker) + len(marker)
        out.append(arm[start : arm.index("'", start)])
    return out


def _top_level_or(arm: str) -> int:
    """How many `OR` sit at parenthesis depth 0 in this arm's WHERE clause.

    A queue condition written `A OR B OR C` has to be parenthesised before
    `AND (city = ANY(:x))` is appended, or the scope binds only to C.
    """
    where = arm[arm.index("WHERE") :]
    depth = 0
    count = 0
    for i, char in enumerate(where):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and where.startswith(" OR ", i):
            count += 1
    return count


def test_a_super_admins_statement_carries_no_city_predicate() -> None:
    sql, params = _action_counts_statement(SUPER)
    assert params == {}
    assert "ANY(" not in sql
    assert _nav_keys(sql) == NAV_KEYS


def test_every_arm_of_a_branchs_statement_is_scoped() -> None:
    sql, params = _action_counts_statement(CUTTACK)
    arms = _arms(sql)
    assert len(arms) == len(NAV_KEYS)
    assert all("= ANY(" in arm for arm in arms), [a for a in arms if "= ANY(" not in a]
    # One bind per arm: reusing a name across two arms is how a clause ends up
    # filtering on the column the other arm named.
    assert len(params) == len(NAV_KEYS)
    assert {tuple(value) for value in params.values()} == {(3,)}


@pytest.mark.parametrize("who", [HQ, CUTTACK])
def test_no_arm_leaves_its_queue_condition_unparenthesised(who) -> None:
    sql, _ = _action_counts_statement(who)
    for arm in _arms(sql):
        key = arm[arm.index("SELECT '") + 8 :][:20]
        assert _top_level_or(arm) == 0, f"{key.split(chr(39))[0]}: an unguarded OR"


def test_only_the_head_office_counts_the_rows_that_have_no_city_yet() -> None:
    """NULL city is head-office work (owner ruling), never a sub-branch's — and
    never a way for a booking's city to be missing and read as belonging here.

    Asserted on the scope's own columns rather than on the string `IS NULL`,
    which the queue conditions legitimately use (`payment_mode IS NULL` is a
    cash-follow-up, not a location).
    """
    hq_sql, _ = _action_counts_statement(HQ)
    cuttack_sql, _ = _action_counts_statement(CUTTACK)
    for column in ("cl.city_id", "cc.city_id", "cr.city_id", "cm.city_id", "e.city_id"):
        assert f"{column} IS NULL" in hq_sql
        assert f"{column} IS NULL" not in cuttack_sql
    # A cab row's city is its master booking's, and that column is NOT NULL: an
    # unassigned booking would be a bug, so it must not join the head office.
    cab_arms = [a for a in _arms(hq_sql) if "FROM cab_bookings" in a]
    assert cab_arms and all("mb.city_id = ANY" in a for a in cab_arms)
    assert all("IS NULL" not in a.split("master_bookings mb")[1] for a in cab_arms)


def test_the_badge_cache_is_namespaced_per_office() -> None:
    """One shared key would serve whichever branch polled first to every other
    sidebar, and the first caller after a deploy is whoever happens to be
    logged in — wrong in production, right half the time in testing."""
    assert action_counts_cache_key(SUPER) == f"{ACTION_COUNTS_CACHE_PREFIX}:all"
    assert action_counts_cache_key(HQ).endswith(":1")
    assert action_counts_cache_key(CUTTACK).endswith(":2")
    assert len({action_counts_cache_key(s) for s in (SUPER, HQ, CUTTACK)}) == 3
