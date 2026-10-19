# ============================================================
# WAYTERO — BRANCH SCOPE GATE UNIT TESTS
# File: tests/unit/test_branch_scope_gate.py
#
# The gate is one function over (method, route template, caller role), so it is
# tested as one function — no database, no TestClient, no per-endpoint case.
# What the endpoints themselves return to a branch caller is the API suite's job.
# ============================================================

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import branch_scope_gate as gate
from app.core.exceptions import PermissionDeniedException


class _Route:
    def __init__(self, path: str) -> None:
        self.path = path


def _request(
    template: str,
    *,
    user_type: str | None = "CCO",
    method: str = "GET",
    matched_route: bool = True,
) -> SimpleNamespace:
    """The shape the gate actually reads: a matched route template and whatever
    the auth middleware left on request.state."""
    state = SimpleNamespace()
    if user_type is not None:
        state.user_payload = {"sub": "user-1", "user_type": user_type}
    scope = {}
    if matched_route:
        scope["route"] = _Route("/api/v1" + template)
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path="/api/v1" + template),
        scope=scope,
        state=state,
    )


@pytest.mark.asyncio
async def test_an_undeclared_screen_is_closed_to_a_branch_account() -> None:
    with pytest.raises(PermissionDeniedException) as raised:
        await gate.require_declared_branch_scope(_request("/admin/hotels"))
    assert raised.value.code == "BRANCH_SCOPE_UNDECLARED"
    assert raised.value.status_code == 403
    # The refusal says what is missing (a review), not what the screen contains.
    assert "not been reviewed" in raised.value.message


@pytest.mark.asyncio
async def test_a_reviewed_screen_is_left_alone_to_filter_its_own_sql() -> None:
    """The gate says yes and does nothing else. A gate that also tried to filter
    would hide a missing WHERE clause behind a working 200."""
    for template in (
        "/admin/bookings",
        "/admin/bookings/{booking_id}",
        "/admin/customer-care",
        "/admin/chat/conversations/{conversation_id}",
    ):
        await gate.require_declared_branch_scope(_request(template))


@pytest.mark.asyncio
async def test_platform_wide_routes_pass_without_a_branch_decision() -> None:
    for template, method in (
        ("/admin/branches", "GET"),
        ("/admin/cab-ops/breakdown/reasons", "GET"),
        ("/admin/chat/presence", "POST"),
    ):
        await gate.require_declared_branch_scope(_request(template, method=method))


@pytest.mark.parametrize("user_type", ["ADMIN", "CCO", "VERIFICATION_OFFICER", "FINANCE_MANAGER"])
@pytest.mark.asyncio
async def test_a_route_that_touches_every_branch_is_refused_by_name(
    user_type: str,
) -> None:
    """Distinct from the punch-list refusal: this screen was reviewed, and the
    answer is "the master admin runs it", not "nobody has looked yet"."""
    with pytest.raises(PermissionDeniedException) as raised:
        await gate.require_declared_branch_scope(_request("/admin/staff-work-hours", user_type=user_type))
    assert raised.value.code == "BRANCH_SCOPE_PLATFORM_ONLY"
    assert "every branch" in raised.value.message


@pytest.mark.asyncio
async def test_the_master_admin_is_never_locked_out_of_an_unreviewed_screen() -> None:
    """The owner has to be able to open a screen that is on the punch list in
    order to review it."""
    await gate.require_declared_branch_scope(_request("/admin/hotels", user_type="SUPER_ADMIN"))


@pytest.mark.parametrize("user_type", ["CUSTOMER", "PARTNER", "DRIVER"])
@pytest.mark.asyncio
async def test_other_surfaces_are_not_this_gates_business(user_type: str) -> None:
    """A partner reaching /admin/hotels/… is a partner-portal question, answered
    by that route's own role dependency. Two systems saying 403 in different
    voices is how an ownership bug becomes unreadable."""
    await gate.require_declared_branch_scope(_request("/admin/hotels", user_type=user_type))


@pytest.mark.parametrize(
    "template",
    [
        "/public/hotel/search",
        "/partners/me/bookings",
        "/drivers/profile",
        "/customers/me",
        "/chat/availability",
    ],
)
@pytest.mark.asyncio
async def test_a_non_admin_path_is_never_classified(template: str) -> None:
    """Even for a staff token: the website and the partner portal are their own
    surfaces, and a branch account browsing them is not this gate's business."""
    await gate.require_declared_branch_scope(_request(template))


@pytest.mark.asyncio
async def test_an_anonymous_request_is_not_answered() -> None:
    """/auth/admin/login is public by design. A 403 that said "this screen has not
    been branch-reviewed" would be a lie about a sign-in form — and the portal
    could not sign in at all."""
    await gate.require_declared_branch_scope(
        _request("/auth/admin/login/password", user_type=None)
    )


@pytest.mark.asyncio
async def test_a_signed_in_staff_account_on_a_public_auth_route_is_still_checked() -> None:
    """The /auth/admin surface is policed too: a route added there next round
    that returns rows has to be classified, not waved through because of its
    prefix."""
    with pytest.raises(PermissionDeniedException) as raised:
        await gate.require_declared_branch_scope(
            _request("/auth/admin/some-new-data-route")
        )
    assert raised.value.code == "BRANCH_SCOPE_UNDECLARED"


@pytest.mark.asyncio
async def test_the_route_template_is_used_not_the_request_path() -> None:
    """/admin/bookings/4127 is a booking the manager may not see. That answer is a
    404 from the row check, and it is not this gate's to give."""
    request = _request("/admin/bookings/{booking_id}")
    request.url = SimpleNamespace(path="/api/v1/admin/bookings/4127")
    await gate.require_declared_branch_scope(request)


@pytest.mark.asyncio
async def test_a_request_with_no_matched_route_still_classifies_by_path() -> None:
    """Starlette normally sets scope["route"]; a mount that does not must not turn
    the gate off, or every route under such a mount would be open by accident."""
    with pytest.raises(PermissionDeniedException):
        await gate.require_declared_branch_scope(
            _request("/admin/hotels", matched_route=False)
        )


@pytest.mark.asyncio
async def test_the_method_is_part_of_the_decision() -> None:
    """A screen can be reviewed for reading and still not for writing."""
    await gate.require_declared_branch_scope(_request("/admin/branches", method="POST"))
    with pytest.raises(PermissionDeniedException):
        await gate.require_declared_branch_scope(
            _request("/admin/leads/partner-applications", method="DELETE")
        )
