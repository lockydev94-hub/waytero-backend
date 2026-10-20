# ============================================================
# WAYTERO — BRANCH SCOPE REGISTRY
# File: app/core/branch_scope_registry.py
# Doc Ref: Round 14 (multi-branch admin portal); gate in app/core/branch_scope_gate.py.
#
# Which admin screens have been reviewed for branch operation, and what "reviewed"
# means for each one.
#
# A route is one of four things:
#
#   "scoped"      — its SQL filters by the caller's branch. Claimed only where the
#                   filter was actually written: a list that merely accepts an
#                   optional ?city_id and returns the whole platform when the
#                   parameter is omitted is NOT scoped, and stays out of this file.
#   "global"      — deliberately platform-wide, so there is nothing to filter:
#                   the office register, sign-in and MPIN routes, the caller's own
#                   chat presence.
#   "super_only"  — reviewed, and it reaches across every branch, so it belongs to
#                   the master admin alone (a payment re-roll, a cross-branch
#                   timesheet). A branch account is refused with a message that
#                   says why, not with the "not reviewed yet" one.
#   absent        — nobody has looked at it. The gate answers 403
#                   BRANCH_SCOPE_UNDECLARED for the four branch roles, which is the
#                   whole point: an unreviewed screen does not get to show Cuttack's
#                   manager the rest of the platform just because no one wrote a
#                   WHERE clause yet.
#
# GET /admin/branch-scope/audit returns the current undeclared list, so the
# follow-up rounds work from the live route table rather than a spreadsheet.
# ============================================================

from __future__ import annotations

from typing import Iterable, Literal, Optional

API_PREFIX = "/api/v1"

# The prefixes this review covers. /partners, /drivers, /customers and /public are
# separate surfaces with their own ownership rules and are never looked at here.
ADMIN_SURFACES = (f"{API_PREFIX}/admin", f"{API_PREFIX}/auth/admin")

Kind = Literal["scoped", "global", "super_only"]

DECLARED: dict[str, Kind] = {
    # ── filtered by branch ──────────────────────────────────────────────────────────────
    "GET /admin/action-counts": "scoped",

    "GET /admin/analytics": "scoped",

    "GET /admin/bookings": "scoped",
    "GET /admin/bookings/accept-queue": "scoped",
    "GET /admin/bookings/hotel/switch-events": "scoped",
    "GET /admin/bookings/pending-acceptance": "scoped",
    "GET /admin/bookings/resources/drivers": "scoped",
    "GET /admin/bookings/resources/partners": "scoped",
    "GET /admin/bookings/resources/partners/{partner_id}": "scoped",
    "GET /admin/bookings/resources/vehicles": "scoped",
    "GET /admin/bookings/stats": "scoped",
    "GET /admin/bookings/{booking_id}": "scoped",
    "GET /admin/bookings/{booking_id}/cab/{cab_id}/coupon-lock": "scoped",
    "GET /admin/bookings/{booking_id}/cab/{cab_id}/download-invoice": "scoped",
    "GET /admin/bookings/{booking_id}/hotel/{hotel_id}/available-rooms": "scoped",
    "GET /admin/bookings/{booking_id}/hotel/{hotel_id}/checkout-preview": "scoped",
    "GET /admin/bookings/{booking_id}/hotel/{hotel_id}/download-invoice": "scoped",
    "GET /admin/bookings/{booking_id}/notes": "scoped",

    "GET /admin/cab-ops/breakdown/active": "scoped",
    "GET /admin/cab-ops/breakdown/{cab_booking_id}/available-partners": "scoped",
    "GET /admin/cab-ops/breakdown/{cab_booking_id}/available-vehicles": "scoped",
    "GET /admin/cab-ops/in-trip": "scoped",
    "GET /admin/cab-ops/queue": "scoped",
    "GET /admin/cab-ops/sos-alerts": "scoped",

    "GET /admin/capture/callbacks": "scoped",
    "GET /admin/capture/callbacks/stats": "scoped",
    "GET /admin/capture/enquiries": "scoped",
    "GET /admin/capture/enquiries/stats": "scoped",
    "GET /admin/capture/feedback": "scoped",
    "GET /admin/capture/quotations": "scoped",

    "GET /admin/chat/conversations": "scoped",
    "GET /admin/chat/conversations/{conversation_id}": "scoped",

    "GET /admin/customer-care": "scoped",
    "GET /admin/customer-care/cab-booking/meta": "scoped",
    "GET /admin/customer-care/customer/{customer_id}": "scoped",
    "GET /admin/customer-care/customer/{customer_id}/records": "scoped",
    "GET /admin/customer-care/hotel-booking/meta": "scoped",
    "GET /admin/customer-care/lookup": "scoped",
    "GET /admin/customer-care/stats": "scoped",

    "GET /admin/dashboard": "scoped",
    "GET /admin/dashboard/city-performance": "scoped",
    "GET /admin/dashboard/recent-activity": "scoped",
    "GET /admin/dashboard/revenue-trend": "scoped",

    "GET /admin/leads/contact-messages": "scoped",
    "GET /admin/leads/contact-messages/stats": "scoped",
    "GET /admin/leads/partner-applications": "scoped",
    "GET /admin/leads/partner-applications/stats": "scoped",

    "GET /admin/staff": "scoped",
    "GET /admin/staff/{user_id}": "scoped",
    "GET /admin/staff/{user_id}/documents": "scoped",

    "GET /admin/system-summary": "scoped",

    "GET /admin/trip-assist/active": "scoped",
    "GET /admin/trip-assist/commission-preview": "scoped",
    "GET /admin/trip-assist/customer-wallet": "scoped",
    "GET /admin/trip-assist/download-invoice": "scoped",
    "GET /admin/trip-assist/lookup": "scoped",
    "GET /admin/trip-assist/pricing": "scoped",

    "PATCH /admin/bookings/{booking_id}/cab/{cab_id}/edit": "scoped",

    "PATCH /admin/capture/callbacks/{callback_id}": "scoped",
    "PATCH /admin/capture/enquiries/{enquiry_id}": "scoped",
    "PATCH /admin/capture/quotations/{quotation_id}": "scoped",

    "PATCH /admin/customer-care/{log_id}": "scoped",

    "PATCH /admin/leads/contact-messages/{message_id}/read": "scoped",
    "PATCH /admin/leads/partner-applications/{application_id}": "scoped",

    "PATCH /admin/staff/{user_id}": "scoped",
    "PATCH /admin/staff/{user_id}/activate": "scoped",
    "PATCH /admin/staff/{user_id}/profile": "scoped",
    "PATCH /admin/staff/{user_id}/suspend": "scoped",

    "POST /admin/bookings/hotel/{reservation_id}/advance-refund": "scoped",
    "POST /admin/bookings/hotel/{reservation_id}/switch": "scoped",
    "POST /admin/bookings/hotel/{reservation_id}/switch/quote": "scoped",
    "POST /admin/bookings/{booking_id}/accept": "scoped",
    "POST /admin/bookings/{booking_id}/cab/{cab_id}/assign-driver": "scoped",
    "POST /admin/bookings/{booking_id}/cab/{cab_id}/assign-partner": "scoped",
    "POST /admin/bookings/{booking_id}/cab/{cab_id}/final-amount": "scoped",
    "POST /admin/bookings/{booking_id}/cab/{cab_id}/reassign": "scoped",
    "POST /admin/bookings/{booking_id}/cab/{cab_id}/reschedule": "scoped",
    "POST /admin/bookings/{booking_id}/cancel": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/add-charges": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/cancel": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/check-in": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/check-out": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/collect-payment": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/complete": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/confirm": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/generate-invoice": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/in-house": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/no-show": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/record-advance": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/reject": "scoped",
    "POST /admin/bookings/{booking_id}/hotel/{hotel_id}/settle": "scoped",
    "POST /admin/bookings/{booking_id}/notes": "scoped",
    "POST /admin/bookings/{booking_id}/sync-hotel-payment": "scoped",

    "POST /admin/cab-ops/breakdown/{cab_booking_id}/handover": "scoped",
    "POST /admin/cab-ops/breakdown/{cab_booking_id}/report": "scoped",
    "POST /admin/cab-ops/breakdown/{cab_booking_id}/swap-same-partner": "scoped",
    "POST /admin/cab-ops/queue/{cab_booking_id}/reassign": "scoped",

    "POST /admin/capture/enquiries/{enquiry_id}/quotation": "scoped",
    "POST /admin/capture/feedback/{feedback_id}/approve": "scoped",
    "POST /admin/capture/feedback/{feedback_id}/reject": "scoped",

    "POST /admin/chat/conversations/{conversation_id}/assign": "scoped",
    "POST /admin/chat/conversations/{conversation_id}/close": "scoped",
    "POST /admin/chat/conversations/{conversation_id}/messages": "scoped",

    "POST /admin/customer-care": "scoped",
    "POST /admin/customer-care/cab-booking": "scoped",
    "POST /admin/customer-care/cab-booking/quote": "scoped",
    "POST /admin/customer-care/hotel-booking": "scoped",
    "POST /admin/customer-care/hotel-booking/quote": "scoped",
    "POST /admin/customer-care/register-customer": "scoped",

    "POST /admin/staff": "scoped",
    "POST /admin/staff/{user_id}/generate-id-card": "scoped",
    "POST /admin/staff/{user_id}/reset-password": "scoped",
    "POST /admin/staff/{user_id}/upload-document": "scoped",
    "POST /admin/staff/{user_id}/upload-photo": "scoped",

    "POST /admin/trip-assist/close-trip": "scoped",
    "POST /admin/trip-assist/collect-payment": "scoped",
    "POST /admin/trip-assist/generate-invoice": "scoped",
    "POST /admin/trip-assist/start-trip": "scoped",
    "POST /admin/trip-assist/update-invoice-url": "scoped",

    "PUT /admin/customer-care/cab-booking/{cab_booking_id}": "scoped",
    "PUT /admin/customer-care/hotel-booking/{reservation_id}": "scoped",

    # ── platform-wide on purpose — nothing to filter ──────────────────────────
    "DELETE /admin/branches/{branch_id}": "global",

    "DELETE /auth/admin/mpin": "global",

    "GET /admin/branches": "global",
    "GET /admin/branches/{branch_id}": "global",

    "GET /admin/cab-ops/breakdown/reasons": "global",

    "GET /admin/branch-scope/audit": "global",

    "GET /auth/admin/session-config": "global",
    "GET /auth/admin/work-hours": "global",

    "PATCH /admin/branches/{branch_id}": "global",
    "POST /admin/branches": "global",

    "POST /admin/chat/presence": "global",

    "POST /auth/admin/heartbeat": "global",
    "POST /auth/admin/login/otp": "global",
    "POST /auth/admin/login/password": "global",
    "POST /auth/admin/mpin/change": "global",
    "POST /auth/admin/mpin/otp": "global",
    "POST /auth/admin/mpin/set": "global",
    "POST /auth/admin/mpin/verify": "global",
    "POST /auth/admin/send-otp": "global",
    "PUT /auth/admin/idle-lock-minutes": "global",

    # ── reviewed: acts on every branch, so the master admin only ──────────────
    "GET /admin/staff-work-hours": "super_only",

    "POST /admin/bookings/backfill-hotel-payment-sync": "super_only",
}


def normalise_path(path_template: str) -> str:
    """`/api/v1/admin/bookings/{booking_id}/` → `/admin/bookings/{booking_id}`.

    The registry is keyed on the route *template*, not the request path, so
    /admin/bookings/4127 is classified by the same entry as /admin/bookings/{id}.
    """
    path = path_template
    if path.startswith(API_PREFIX):
        path = path[len(API_PREFIX):]
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def route_key(method: str, path_template: str) -> str:
    return f"{method.upper()} {normalise_path(path_template)}"


def classify(method: str, path_template: str) -> Optional[Kind]:
    return DECLARED.get(route_key(method, path_template))


def is_admin_surface(path_template: str) -> bool:
    return normalise_path(path_template).startswith(
        tuple(p[len(API_PREFIX):] for p in ADMIN_SURFACES)
    )


def admin_route_keys(routes: Iterable) -> list[str]:
    """Every admin (and /auth/admin) route the running app exposes."""
    keys: set[str] = set()
    for route in routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or not is_admin_surface(path):
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            keys.add(route_key(method, path))
    return sorted(keys)


def undeclared_routes(routes: Iterable) -> list[str]:
    """The punch list: admin routes nobody has classified yet."""
    return [key for key in admin_route_keys(routes) if key not in DECLARED]


def stale_keys(routes: Iterable) -> list[str]:
    """Registry entries that no longer match a real route.

    A typo here would not 403 anything — it would silently leave a reviewed screen
    undeclared, or worse declare a route that has since been renamed while the
    renamed one stays undeclared. The unit test asserts this set stays empty.
    """
    live = set(admin_route_keys(routes))
    return sorted(set(DECLARED) - live)


def counts(routes: Iterable) -> dict[str, int]:
    live = admin_route_keys(routes)
    declared = [key for key in live if key in DECLARED]
    return {
        "admin_routes": len(live),
        "scoped": sum(1 for k in declared if DECLARED[k] == "scoped"),
        "global": sum(1 for k in declared if DECLARED[k] == "global"),
        "super_only": sum(1 for k in declared if DECLARED[k] == "super_only"),
        "undeclared": len(live) - len(declared),
        "stale_registry_entries": len(stale_keys(routes)),
    }
