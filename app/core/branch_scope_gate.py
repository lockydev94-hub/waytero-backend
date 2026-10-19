# ============================================================
# WAYTERO — BRANCH SCOPE GATE
# File: app/core/branch_scope_gate.py
# Doc Ref: app/core/branch_scope_registry.py (the classification it reads); plan B3.
#
# One dependency, mounted once on `api_router`, that closes every admin route the
# branch review has not reached yet.
#
# Why it hangs off the router instead of each endpoint: ~490 admin routes across
# 30 modules. A per-endpoint check is a check that gets forgotten — and forgetting
# does not produce an error, it produces another branch's booking list rendering
# normally. Mounted on the router it propagates to every route included in it,
# including the one someone adds next week, which is then closed the day it is
# written.
#
# What this gate decides is *whether the screen may run at all* for a branch
# account. It never filters a row: which rows are readable is decided in SQL, from
# the user's database row (see branch_scope.py), because that is the only place
# that can see a row. Accordingly it reads the caller's role off the token the
# auth middleware already decoded — the same source `require_roles` uses for every
# other role decision in the API — and it costs no query, which matters because it
# runs on every request, including the website's.
#
# What it deliberately does not do:
#   * it never touches /public, /partners, /drivers or /customers — other surfaces
#     with other ownership rules; a partner reaching /admin/hotels/… is answered
#     by that route's own role dependency, in one voice rather than two;
#   * it never speaks to an anonymous request. Sign-in, OTP and session-config
#     live under /auth/admin, which the auth middleware leaves public; telling
#     someone who is not logged in that a screen "has not been branch-reviewed"
#     would be a lie about a login form;
#   * it never locks out SUPER_ADMIN, who is not in STAFF_BRANCH_TYPES — the
#     person who works the punch list has to be able to open the screens on it.
# ============================================================

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.core.branch_scope import STAFF_BRANCH_TYPES
from app.core.branch_scope_registry import classify, normalise_path
from app.core.exceptions import PermissionDeniedException
from app.core.logging import get_logger

logger = get_logger(__name__)

# /api/v1 stripped, because normalise_path strips it from the route template too.
GUARDED_PREFIXES = ("/admin", "/auth/admin")

_UNDECLARED_MESSAGE = (
    "This screen has not been reviewed for branch operation yet. It is closed to "
    "a branch account instead of showing every branch's rows, which is what it "
    "would do today. The master admin can see the list of screens still waiting "
    "for review at GET /admin/branch-scope/audit."
)

_PLATFORM_ONLY_MESSAGE = (
    "This runs across every branch at once, so the master admin performs it."
)


async def require_declared_branch_scope(request: Request) -> None:
    # The route template, not the request path: /admin/bookings/4127 is the same
    # screen as /admin/bookings/{booking_id}, and "is that booking yours" is a
    # row question that this gate has no business answering.
    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    path = normalise_path(template)

    if not path.startswith(GUARDED_PREFIXES):
        return

    payload: Any = getattr(request.state, "user_payload", None)
    if payload is None:
        # No caller — the middleware left this path public on purpose.
        return

    user_type = payload.get("user_type")
    if user_type not in STAFF_BRANCH_TYPES:
        return

    kind = classify(request.method, template)
    if kind in ("scoped", "global"):
        return

    where = f"{request.method} {path}"
    who = {
        "actor_id": payload.get("sub"),
        "user_type": user_type,
        "route": where,
    }

    if kind == "super_only":
        logger.warning("branch_scope_platform_only", **who)
        raise PermissionDeniedException(
            message=_PLATFORM_ONLY_MESSAGE, code="BRANCH_SCOPE_PLATFORM_ONLY"
        )

    logger.warning("branch_scope_undeclared", **who)
    raise PermissionDeniedException(
        message=_UNDECLARED_MESSAGE, code="BRANCH_SCOPE_UNDECLARED"
    )
