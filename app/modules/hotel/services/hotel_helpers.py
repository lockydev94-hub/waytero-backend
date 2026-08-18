# ============================================================
# WAY TERO — HOTEL HELPERS
# File: app/modules/hotel/services/hotel_helpers.py
#
# Cross-cutting helpers used by both the partner and admin
# hotel-booking APIs. Kept here so the check-in date guard and any
# future "is X off by more than 1 day" / "is X on time" rules live in
# exactly one place. Both APIs import from this module so the user
# experience and the audit trail are identical regardless of which
# portal records the action.
# ============================================================
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def check_in_date_mismatch(
    hr,
    actual_check_in_at: Optional[datetime],
) -> Optional[dict]:
    """Return a mismatch descriptor if the recorded check-in date is more than
    one day away from the booked ``check_in_date``; otherwise None.

    The check is done in the **platform timezone** so a guest who arrives at
    23:30 IST on the eve of their booked date is not flagged — only actual
    date drift (early arrival, late arrival) trips the guard. Both portals
    return the same descriptor, so the user-facing modal in the partner-portal
    and the admin-portal surfaces the identical warning.

    Returns a dict with ``code``, ``booking_date``, ``actual_date``,
    ``delta_days`` and a human ``message``. Caller is expected to raise
    HTTP 409 with this payload when the request did not include the override
    flag, or persist a `remarks` note when the user did confirm.
    """
    if hr is None or actual_check_in_at is None:
        return None
    booked = getattr(hr, "check_in_date", None)
    if not booked:
        return None
    try:
        from app.core.timezone import to_platform_tz

        actual_date = to_platform_tz(actual_check_in_at).date()
    except Exception:
        # If we cannot resolve the zone, fall back to UTC and skip the guard.
        if actual_check_in_at.tzinfo is None:
            return None
        actual_date = actual_check_in_at.astimezone(timezone.utc).date()
    delta_days = (actual_date - booked).days
    if abs(delta_days) < 1:
        return None
    direction = "after" if delta_days > 0 else "before"
    return {
        "code": "CHECKIN_DATE_MISMATCH",
        "booking_date": booked.isoformat(),
        "actual_date": actual_date.isoformat(),
        "delta_days": delta_days,
        "message": (
            f"You recorded the guest's check-in on {actual_date.isoformat()}, "
            f"which is {abs(delta_days)} day(s) {direction} the booked "
            f"check-in date {booked.isoformat()}. Confirm to proceed."
        ),
    }
