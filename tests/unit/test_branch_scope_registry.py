# ============================================================
# WAYTERO — BRANCH SCOPE REGISTRY UNIT TESTS
# File: tests/unit/test_branch_scope_registry.py
#
# The registry is a list of claims about other people's code, so the claims are
# what gets tested here: a typo in a key does not fail loudly, it silently leaves
# a scoped screen undeclared (403 for a branch manager) or declares a screen
# scoped that never filters. The route table the app is serving is the only
# reference that can tell.
# ============================================================

from __future__ import annotations

import pytest

from app.core.branch_scope_registry import (
    DECLARED,
    Kind,
    admin_route_keys,
    classify,
    counts,
    is_admin_surface,
    normalise_path,
    route_key,
    stale_keys,
    undeclared_routes,
)
from app.main import app

VALID_KINDS = {"scoped", "global", "super_only"}
VALID_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


def _live_keys() -> set[str]:
    return set(admin_route_keys(app.routes))


def test_normalise_path_strips_the_api_prefix_and_trailing_slash() -> None:
    assert normalise_path("/api/v1/admin/bookings") == "/admin/bookings"
    assert normalise_path("/api/v1/admin/bookings/") == "/admin/bookings"
    assert normalise_path("/admin/branches/{branch_id}") == "/admin/branches/{branch_id}"


def test_keys_are_well_formed_method_space_path() -> None:
    for key, kind in DECLARED.items():
        method, _, path = key.partition(" ")
        assert method in VALID_METHODS, key
        assert path.startswith("/"), key
        assert kind in VALID_KINDS, key
        assert kind == DECLARED[key]


def test_registry_declares_only_routes_that_actually_exist() -> None:
    """A renamed or misspelt route would otherwise keep its old classification
    while the real one silently loses its branch filter."""
    assert stale_keys(app.routes) == []


def test_classification_is_case_insensitive_on_the_method() -> None:
    assert classify("get", "/api/v1/admin/staff") == "scoped"
    assert classify("GET", "/api/v1/admin/staff") == "scoped"


def test_a_param_path_is_classified_by_its_template() -> None:
    """/admin/bookings/4127 is the same screen as /admin/bookings/{booking_id}."""
    assert classify("GET", "/api/v1/admin/bookings/{booking_id}") == "scoped"
    assert route_key("GET", "/api/v1/admin/bookings/4127") == "GET /admin/bookings/4127"
    # The real request path is not a template, so it is not classified — which is
    # why the gate reads request.scope["route"].path and never the URL.
    assert classify("GET", "/api/v1/admin/bookings/4127") is None


def test_only_admin_surfaces_are_guarded() -> None:
    assert is_admin_surface("/api/v1/admin/settlements")
    assert is_admin_surface("/api/v1/auth/admin/login/password")
    for other in (
        "/api/v1/public/hotel/search",
        "/api/v1/partners/bookings",
        "/api/v1/drivers/profile",
        "/api/v1/customers/me",
        "/api/v1/chat/availability",
        "/health",
    ):
        assert not is_admin_surface(other), other


def test_the_daily_queues_are_declared_scoped() -> None:
    """The screens a branch manager opens first thing, by name, so a refactor
    that drops their classification fails here rather than in the browser."""
    for key in (
        "GET /admin/bookings",
        "GET /admin/cab-ops/queue",
        "GET /admin/cab-ops/in-trip",
        "GET /admin/customer-care",
        "GET /admin/chat/conversations",
        "GET /admin/capture/enquiries",
        "GET /admin/leads/partner-applications",
        "GET /admin/trip-assist/active",
        "GET /admin/staff",
        "GET /admin/dashboard",
        "GET /admin/action-counts",
    ):
        assert DECLARED.get(key) == "scoped", key


def test_screens_not_reviewed_this_round_stay_undeclared() -> None:
    """Deny by default is the promise. If one of these ever appears in DECLARED,
    the reviewer has to have actually filtered its SQL — and this test is the
    tripwire that the claim was made without it."""
    live = _live_keys()
    for key in (
        "GET /admin/hotels",
        "GET /admin/partners",
        "GET /admin/vehicles",
        "GET /admin/wallets/customers",
        "GET /admin/reports/bookings",
        "GET /admin/audit-logs",
        "GET /admin/tours/bookings",
        "GET /admin/dashboard/expiring-documents",
    ):
        assert key in live, key
        assert key not in DECLARED, key


def test_settlement_is_reviewed_as_master_admin_only() -> None:
    """The settlement module was declared in the round that gave it a statement
    ledger. It is not on the punch list any more, and it is not open to a branch
    account either: no settlement query filters by branch."""
    for key in (
        "GET /admin/settlements",
        "GET /admin/settlements/statements",
        "POST /admin/settlements/settle",
        "POST /admin/settlements/statements/generate",
        "PATCH /admin/settlements/statements/{settlement_id}/status",
    ):
        assert DECLARED.get(key) == "super_only", key


def test_the_settings_surface_is_classified_whole() -> None:
    """Every /admin/settings route is reviewed: reads platform-wide (the booking
    screens' pickers live here), writes the master admin's. A route added here
    next month starts undeclared, which is the point."""
    live = [
        key
        for key in _live_keys()
        if key.split(" ", 1)[1].startswith("/admin/settings/")
    ]
    assert live
    assert [key for key in live if key not in DECLARED] == []
    for key in live:
        expected = "global" if key.startswith("GET ") else "super_only"
        assert DECLARED[key] == expected, key


def test_every_auth_admin_route_is_classified() -> None:
    """The gate polices /auth/admin too, but those routes are the sign-in
    ceremony, so an unclassified one cannot be left to the punch list — it would
    be a screen nobody is looking at. This is the check that keeps that true."""
    unclassified = [
        key
        for key in _live_keys()
        if key.split(" ", 1)[1].startswith("/auth/admin") and key not in DECLARED
    ]
    assert unclassified == []


def test_undeclared_routes_are_the_complement_of_the_registry() -> None:
    live = _live_keys()
    undeclared = set(undeclared_routes(app.routes))
    assert undeclared == live - set(DECLARED)
    assert not (undeclared & set(DECLARED))


def test_counts_add_up_to_the_live_route_table() -> None:
    c = counts(app.routes)
    assert c["admin_routes"] == len(_live_keys())
    assert c["scoped"] + c["global"] + c["super_only"] + c["undeclared"] == c["admin_routes"]
    assert c["stale_registry_entries"] == 0
    # A round that scoped more screens should raise this number; it dropping means
    # someone deleted a registry entry without re-reviewing the route.
    assert c["scoped"] >= 119


@pytest.mark.parametrize(
    "key,kind",
    [
        ("GET /admin/branches", "global"),
        ("POST /auth/admin/login/password", "global"),
        ("GET /admin/cab-ops/breakdown/reasons", "global"),
        ("POST /admin/bookings/backfill-hotel-payment-sync", "super_only"),
        ("GET /admin/staff-work-hours", "super_only"),
    ],
)
def test_platform_wide_routes_are_classified_rather_than_forgotten(
    key: str, kind: Kind
) -> None:
    assert DECLARED[key] == kind
