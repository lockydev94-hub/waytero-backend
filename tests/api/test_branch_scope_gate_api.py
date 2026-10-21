# ============================================================
# WAYTERO — BRANCH SCOPE GATE API TESTS
# File: tests/api/test_branch_scope_gate_api.py
# Doc Ref: plan B3; app/core/branch_scope_gate.py, branch_scope_registry.py
#
# The gate is one dependency on api_router, so what these prove is that it is
# really attached to the router and not to one module: an unreviewed screen
# refuses a branch account from whichever router happens to serve it, a reviewed
# one still answers, and the website, partner and driver surfaces are untouched.
#
# One client fixture per test, maximum — the async engine is a module singleton
# and a second TestClient boots a second loop over the same connections.
# ============================================================

import pytest

UNDECLARED_SCREENS = [
    "/api/v1/admin/hotels",
    "/api/v1/admin/partners",
    "/api/v1/admin/vehicles",
    "/api/v1/admin/audit-logs",
    "/api/v1/admin/dashboard/expiring-documents",
]

REVIEWED_SCREENS = [
    "/api/v1/admin/bookings",
    "/api/v1/admin/cab-ops/queue",
    "/api/v1/admin/customer-care",
    "/api/v1/admin/chat/conversations",
    "/api/v1/admin/capture/enquiries",
    "/api/v1/admin/leads/partner-applications",
    "/api/v1/admin/trip-assist/active",
    "/api/v1/admin/dashboard",
    "/api/v1/admin/action-counts",
]


@pytest.mark.parametrize("path", UNDECLARED_SCREENS)
def test_unreviewed_screen_refuses_a_cuttack_manager(client_cuttack_cco, path) -> None:
    response = client_cuttack_cco.get(path)
    assert response.status_code == 403, path
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "BRANCH_SCOPE_UNDECLARED", path
    # The refusal names the screen, not the row: a branch account learns that a
    # menu item exists (it can see it in the portal), nothing about another
    # office's data.
    assert "branch" in body["message"].lower()


@pytest.mark.parametrize("path", UNDECLARED_SCREENS[:3])
def test_the_master_admin_still_opens_an_unreviewed_screen(
    client_super_admin, path
) -> None:
    """The owner has to be able to read the screen they are being told to
    review — locking it out would make the punch list unworkable."""
    assert client_super_admin.get(path).status_code != 403


@pytest.mark.parametrize("path", REVIEWED_SCREENS)
def test_a_reviewed_screen_still_answers_for_a_branch_account(
    client_cuttack_cco, path
) -> None:
    response = client_cuttack_cco.get(path)
    assert response.status_code == 200, (path, response.text[:200])
    body = response.json()
    if isinstance(body, dict):
        assert body.get("code") != "BRANCH_SCOPE_UNDECLARED"


def test_a_branch_manager_opens_their_own_staff_register(client_cuttack_admin) -> None:
    """Out of the CCO list above on purpose: /admin/staff is branch-scoped AND
    manager-only, and the 403 a CCO gets there is the role gate's answer, not the
    branch gate's."""
    assert client_cuttack_admin.get("/api/v1/admin/staff").status_code == 200


def test_a_route_that_touches_every_branch_says_so(client_hq_admin) -> None:
    """Distinct from the punch-list refusal: this screen was reviewed and the
    answer is "the master admin runs it", not "nobody has looked yet"."""
    response = client_hq_admin.get(
        "/api/v1/admin/staff-work-hours?date_from=2026-01-01&date_to=2026-01-31"
    )
    assert response.status_code == 403
    assert response.json()["code"] == "BRANCH_SCOPE_PLATFORM_ONLY"


def test_settlement_is_reviewed_as_master_admin_only(client_hq_admin) -> None:
    """Settlements became a reviewed screen in the round that gave them a
    statement ledger. No settlement query filters by branch, so a branch account
    is refused by name rather than told the screen has not been reviewed."""
    response = client_hq_admin.get("/api/v1/admin/settlements")
    assert response.status_code == 403
    assert response.json()["code"] == "BRANCH_SCOPE_PLATFORM_ONLY"


def test_a_branch_office_can_still_read_master_data(client_cuttack_cco) -> None:
    """The city picker behind the booking screens lives under /admin/settings.
    Closing the whole module to branch accounts would have taken the picker
    with it, so reads are declared platform-wide and only writes are refused."""
    response = client_cuttack_cco.get("/api/v1/admin/settings/master/cities")
    assert response.status_code == 200, response.text[:200]


def test_a_branch_manager_cannot_change_the_platform_configuration(
    client_cuttack_admin,
) -> None:
    """Configuration writes are the master admin's. The branch gate answers by
    name before the endpoint runs, the same way it answers a cross-branch
    timesheet — the Settings page's role dependency is not the only guard."""
    response = client_cuttack_admin.put(
        "/api/v1/admin/settings/configurations/GST_ENABLED",
        json={"config_value": "true"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "BRANCH_SCOPE_PLATFORM_ONLY"


def test_the_punch_list_is_readable_only_by_the_master_admin(client_hq_admin) -> None:
    """Declared global on purpose — a manager opening it would be refused by the
    role dependency below it, not by the gate, and the difference is what tells
    the owner the classification worked."""
    response = client_hq_admin.get("/api/v1/admin/branch-scope/audit")
    assert response.status_code == 403
    assert response.json().get("code") != "BRANCH_SCOPE_UNDECLARED"


def test_the_audit_reports_the_live_route_table(client_super_admin) -> None:
    response = client_super_admin.get("/api/v1/admin/branch-scope/audit")
    assert response.status_code == 200
    body = response.json()

    registry = body["registry"]
    assert registry["stale_registry_entries"] == 0
    assert (
        registry["scoped"] + registry["global"] + registry["super_only"]
        + registry["undeclared"]
        == registry["admin_routes"]
    )
    assert registry["scoped"] >= 119

    undeclared = body["undeclared_routes"]
    assert "GET /admin/hotels" in undeclared
    assert "GET /admin/bookings" not in undeclared

    # Every office the owner registered, with how many staff accounts point at
    # it — an office with nobody in it is the reason a queue looks empty.
    assert len(body["branches"]) >= 2
    assert all("staff_accounts" in row for row in body["branches"])

    # The count the plan says must be observable rather than assumed: nothing is
    # seeded here, and no name or address is invented for the report.
    assert body["super_admin_accounts"]["count"] == len(
        body["super_admin_accounts"]["accounts"]
    )
    assert body["super_admin_accounts"]["count"] >= 1

    # Rows with no city are head-office work by owner decision; this is the
    # number that says how much of the queue a branch cannot see yet.
    tables = {row["table_name"] for row in body["queue_rows_without_a_city"]}
    assert tables == {
        "enquiries",
        "callback_requests",
        "quotations",
        "contact_messages",
        "partner_applications",
        "chat_conversations",
        "customer_care_logs",
    }
    for row in body["queue_rows_without_a_city"]:
        assert row["without_city"] <= row["total"]


def test_the_gate_never_answers_for_a_partner(client) -> None:
    """A partner reaching /partners is not a branch question, and a partner
    reaching /admin/hotels is legitimate — that router serves a partner's own
    hotel rows and answers the question with its own role dependency. The branch
    gate stays out of both, so neither request can come back with a branch-review
    message: two systems saying 403 in different voices is how an ownership bug
    becomes unreadable."""
    branch_codes = ("BRANCH_SCOPE_UNDECLARED", "BRANCH_SCOPE_PLATFORM_ONLY")
    for path in ("/api/v1/partners/me/bookings", "/api/v1/admin/hotels"):
        body = client.get(path).json()
        code = body.get("code") if isinstance(body, dict) else None
        assert code not in branch_codes, (path, body)


def test_the_public_website_is_not_classified_at_all(client_super_admin) -> None:
    """The website's own routes stay reachable — the gate looks at /admin and
    /auth/admin only, and a staff token must not be able to change that either
    way by accident."""
    response = client_super_admin.get("/api/v1/public/homepage")
    assert response.status_code == 200, response.text[:200]
