# ============================================================
# WAYTERO — PARTNER REJECT-REASON CONSTANTS
# File: app/modules/partner/constants/reject_reasons.py
# Doc Ref: BRD Part 3 §42 — Driver Assignment after partner acceptance
# ============================================================
#
# When a partner rejects a cab booking they have been assigned, they
# pick one of these codes (plus optional free-text notes). The values
# are stored on cab_booking_assignments.rejection_reason_code and
# echoed in the booking_timelines audit log.
#
# Two extra "system" codes are reserved for non-partner actions and
# are NOT exposed in the partner portal UI:
#   TIMEOUT          — Celery sweeper reverted the assignment because
#                      the partner never accepted / rejected.
#   ADMIN_REASSIGN   — Admin reassigned the cab to a different partner
#                      before this one responded.
# ============================================================

REJECT_REASONS: list[dict[str, str]] = [
    {"code": "VEHICLE_UNAVAILABLE", "label": "Vehicle not available"},
    {"code": "DRIVER_UNAVAILABLE", "label": "Driver not available"},
    {"code": "CAPACITY_ISSUE", "label": "Vehicle capacity insufficient"},
    {"code": "OUTSIDE_SERVICE_AREA", "label": "Pickup outside service area"},
    {"code": "PRICING_DISAGREEMENT", "label": "Pricing disagreement"},
    {"code": "OTHER", "label": "Other (specify in notes)"},
]

# Mid-trip breakdown reasons. These are written to cab_bookings.breakdown_reason
# (not the assignment rejection code) and are exposed to drivers, partners and
# admin so the reason for a vehicle swap is captured uniformly.
BREAKDOWN_REASONS: list[dict[str, str]] = [
    {"code": "VEHICLE_BREAKDOWN", "label": "Vehicle broke down"},
    {"code": "ACCIDENT", "label": "Vehicle involved in an accident"},
    {"code": "DRIVER_UNWELL", "label": "Driver unwell / medical emergency"},
    {"code": "OTHER", "label": "Other (specify in notes)"},
]

BREAKDOWN_REASON_CODES: set[str] = {r["code"] for r in BREAKDOWN_REASONS}

REJECT_REASON_CODES: set[str] = {r["code"] for r in REJECT_REASONS}

SYSTEM_REJECT_REASON_TIMEOUT = "TIMEOUT"
SYSTEM_REJECT_REASON_ADMIN_REASSIGN = "ADMIN_REASSIGN"

SYSTEM_REJECT_REASON_CODES: set[str] = {
    SYSTEM_REJECT_REASON_TIMEOUT,
    SYSTEM_REJECT_REASON_ADMIN_REASSIGN,
}
