"""Mobile-number rules shared by the booking endpoints.

The platform requires every self-service customer to have a usable mobile on
file before a booking is created, because drivers and partners reach the
customer by phone. Verified or unverified both count — the SMS provider is an
admin toggle, so an unverified number is the expected state when SMS is off.

The test is a format check rather than a "not the ``fb_`` placeholder" string
match: it is the same rule the rest of the backend uses for a mobile
(``auth/schemas``, ``hotel/validators``, ``admin/public_leads_api``), and it
cannot be fooled by any synthetic value we have not thought of yet.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from app.core.exceptions import BusinessException

MOBILE_PATTERN = re.compile(r"[6-9]\d{9}")


def has_real_mobile(mobile_number: Any) -> bool:
    """True when the value is a plausible Indian mobile number."""
    return bool(MOBILE_PATTERN.fullmatch(mobile_number or ""))


async def require_real_mobile(db: Any, user_id: Any) -> None:
    """Raise 422 MOBILE_REQUIRED unless the user has a real mobile on file.

    Call this at the very top of a booking handler, before any side effect —
    sequence numbers, coupon reservations and inventory decrements do not roll
    back with the request.
    """
    mobile_number = await db.scalar(
        text("SELECT mobile_number FROM users WHERE id = :uid"),
        {"uid": str(user_id)},
    )
    # users.mobile_number is NOT NULL, so None can only mean the account row is
    # gone; let the caller's own customer lookup report that.
    if mobile_number is None:
        return
    if not has_real_mobile(mobile_number):
        raise BusinessException(
            "Add your mobile number before booking.",
            code="MOBILE_REQUIRED",
        )


__all__ = ["MOBILE_PATTERN", "has_real_mobile", "require_real_mobile"]
