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
# The gate above says a manager may open the register. Migration 0085 says the
# register is no longer one list. These run against the developer database, so
# they assert the relationship between two callers' lists rather than a row count
# that whoever next seeds the database would break.

REGISTER = "/api/v1/admin/staff"


def _ids(client) -> set[str]:
    return {item["id"] for item in client.get(REGISTER).json()["items"]}


def _branches(client) -> set:
    return {item["branch_id"] for item in client.get(REGISTER).json()["items"]}


def test_two_branch_managers_do_not_share_a_register(
    client_hq_admin, client_cuttack_admin
) -> None:
    """The head office has staff, the sub-branch may not have any yet — but neither
    may appear in the other's list, and that holds whatever rows exist."""
    assert _branches(client_hq_admin) == {1} or _branches(client_hq_admin) == set()
    assert _branches(client_cuttack_admin) <= {2}
    assert _ids(client_hq_admin) & _ids(client_cuttack_admin) == set()


def test_a_manager_cannot_read_another_branches_staff_row(
    client_hq_admin, client_cuttack_admin
) -> None:
    """404, not 403: the answer a Cuttack manager gets for a Bhubaneswar id has to
    be the answer they get for an id that was never issued."""
    hq_only = _ids(client_hq_admin) - _ids(client_cuttack_admin)
    if not hq_only:
        pytest.skip("the head office has no staff row to reach across to")

    assert client_cuttack_admin.get(f"{REGISTER}/{hq_only.pop()}").status_code == 404


def test_the_master_admin_is_not_a_row_a_manager_enumerates(
    client_hq_admin, client_super_admin
) -> None:
    """SUPER_ADMIN has no branch, so it drops out of every branch list — and the
    service hides it from non-supers even when a branch could match it. This is
    the one test that needs the developer database to hold a master account, which
    it does: nothing works without one."""
    invisible = _ids(client_super_admin) - _ids(client_hq_admin)
    assert invisible, "a super admin's own register should show at least the master account"
