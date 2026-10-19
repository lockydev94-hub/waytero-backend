"""
Who may reach /admin/staff — the privilege that was live before Round 14.

The dashboard router is mounted with a router-level ALL_STAFF gate, which is
SUPER_ADMIN, ADMIN, CCO, FINANCE_MANAGER and VERIFICATION_OFFICER. Every staff
route inherited that and nothing more, so a CCO could read the whole staff
register, PATCH another account's role and mobile, rewrite their profile and
bank details, mark an ID card printed and read their uploaded documents. The
sibling write routes (create, activate, suspend, reset-password) already named
require_admin() explicitly — the gap was the read/edit half of the same
resource, which is easy to miss because a route with no dependency looks
authenticated when it is only *reachable*.

Pinned here per route, because "the router is gated" is exactly the reasoning
that left them open.
"""
from uuid import uuid4

import pytest

STAFF = f"/api/v1/admin/staff/{uuid4()}"

# (method, path, kwargs) for every /admin/staff route.
STAFF_ROUTES = [
    ("get", "/api/v1/admin/staff", {}),
    ("get", STAFF, {}),
    ("patch", STAFF, {"json": {}}),
    ("patch", f"{STAFF}/activate", {"json": {}}),
    ("patch", f"{STAFF}/suspend", {"json": {}}),
    ("patch", f"{STAFF}/profile", {"json": {}}),
    ("post", f"{STAFF}/reset-password", {"json": {}}),
    ("post", f"{STAFF}/generate-id-card", {"json": {}}),
    ("get", f"{STAFF}/documents", {}),
    ("post", f"{STAFF}/upload-photo", {}),
    ("post", f"{STAFF}/upload-document", {}),
]


def _call(client, method: str, path: str, kwargs: dict):
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path,kwargs", STAFF_ROUTES)
@pytest.mark.parametrize(
    "who", ["client_cco", "client_finance_manager", "client_verification_officer"]
)
def test_staff_routes_are_closed_to_roles_that_do_not_manage_staff(
    request, who, method, path, kwargs
) -> None:
    client = request.getfixturevalue(who)
    assert _call(client, method, path, kwargs).status_code == 403, (
        f"{who} reached {method.upper()} {path}"
    )


@pytest.mark.parametrize("method,path,kwargs", STAFF_ROUTES)
def test_a_manager_passes_the_gate(client_staff, method, path, kwargs) -> None:
    """Not 403 — an unknown id is a 404 and an empty body is a 422, both of
    which happen after the gate, which is the only thing under test."""
    response = _call(client_staff, method, path, kwargs)
    assert response.status_code != 403, f"ADMIN blocked from {method.upper()} {path}"


def test_the_staff_register_still_answers_for_a_manager(client_staff) -> None:
    response = client_staff.get("/api/v1/admin/staff")
    assert response.status_code == 200
    assert {"items", "total", "page", "page_size"} <= set(response.json())


# ── And by which office ───────────────────────────────────────────────────────
# The gate above says a manager may open the register. Migration 0085 made the
# register one list per office.
#
# One client per test, on purpose: the async engine is a module-level singleton
# and every TestClient runs its own event loop, so a test that asks for two
# clients fails inside its fixtures with asyncpg's "attached to a different
# loop". Two managers not sharing a register is therefore pinned as each
# manager's own invariant, which says the same thing without needing the other's
# rows. Reaching across by id is pinned in tests/unit/test_staff_branch_rules.py
# — that is where the 404 decision is made, and it needs no database at all.

REGISTER = "/api/v1/admin/staff"

BRANCH_STAFF_TYPES = {"ADMIN", "CCO", "VERIFICATION_OFFICER", "FINANCE_MANAGER"}


def _items(client) -> list[dict]:
    return client.get(REGISTER).json()["items"]


@pytest.mark.parametrize(
    "who,own_branch", [("client_hq_admin", 1), ("client_cuttack_admin", 2)]
)
def test_a_manager_enumerates_only_their_own_office(request, who, own_branch) -> None:
    """The branch ids are 0085's bootstrap rows, not invented ones: 1 is the
    Bhubaneswar head office, 2 is Cuttack. How many people either office has today
    is the owner's business, so what is asserted is which offices can appear."""
    items = _items(request.getfixturevalue(who))
    assert {item["branch_id"] for item in items} <= {own_branch}
    assert {item["user_type"] for item in items} <= BRANCH_STAFF_TYPES


def test_the_master_admin_is_not_a_row_a_manager_enumerates(client_hq_admin) -> None:
    """SUPER_ADMIN carries no branch, which is precisely why it must not surface in
    a branch register: whoever could PATCH that account's role would hold the only
    account that can assign branches at all."""
    assert "SUPER_ADMIN" not in {
        item["user_type"] for item in _items(client_hq_admin)
    }


def test_the_master_admin_still_sees_the_whole_org(client_super_admin) -> None:
    """The other half of the filter — what it excludes is the caller's branch and
    the master account's absence from it, not the register. A super admin is the
    one caller whose list contains the master account."""
    user_types = {item["user_type"] for item in _items(client_super_admin)}
    assert user_types <= BRANCH_STAFF_TYPES | {"SUPER_ADMIN"}
    assert "SUPER_ADMIN" in user_types, (
        "the developer database has no master admin, so this suite has nothing to "
        "check the super admin's own view against"
    )
