"""Mobile-number rules shared by the booking endpoints.

The platform requires every self-service customer to have a usable mobile on
file before a booking is created, because drivers and partners reach the
customer by phone. Verified or unverified both count — the SMS provider is an
admin toggle, so an unverified number is the expected state when SMS is off.

The test is a format check rather than a "not the ``fb_`` placeholder" string
match: it is the same rule the rest of the backend uses for a mobile
(``auth/schemas``, ``hotel/validators``, ``admin/public_leads_api``), and it
cannot be fooled by any synthetic value we have not thought of yet.

The same file owns how a typed number is matched to a stored one, because those
two have to agree: a search that reads ``+91 98765 43210`` as a different person
from ``9876543210`` is how one customer ends up with two accounts.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import text

from app.core.exceptions import BusinessException

MOBILE_PATTERN = re.compile(r"[6-9]\d{9}")

_NON_DIGITS = re.compile(r"\D")


def mobile_match_sql(column: str, param: str) -> str:
    """SQL predicate: does `column` hold the mobile bound as `:param`?

    The last ten digits of a stored mobile decide it, ignoring however the value
    was typed in, so a query and a row meet on the same ten digits:
    `9876543210`, `+91 98765 43210` and `09876543210` are one number.

    The letter guard is not decoration. A Firebase sign-in synthesises a
    placeholder mobile (`_placeholder_mobile_for_firebase` in the auth service)
    and a hex placeholder can happen to spell ten digits in mobile shape — and a
    support agent searching for a real customer must never land on somebody
    else's account because both values ended in the same digits. A mobile number
    is digits, a `+`, and spacing; letters mean the value is not a number at all.

    Two conjuncts rather than one `CASE` so an index on the digits expression
    stays usable: the guard is a recheck filter, the comparison is the seek.
    `column` is whatever the query's author qualified, never user input.
    """
    digits = f"right(regexp_replace({column}, '[^0-9]', '', 'g'), 10)"
    return f"{column} !~ '[A-Za-z]' AND {digits} = :{param}"


def normalise_mobile(value: Any) -> Optional[str]:
    """The ten digits of an Indian mobile, or None when these are not one.

    An agent reads a number off the caller-ID screen and types it the way the
    customer said it. Comparing that string exactly against a column that stores
    bare digits answered "no such customer" for a customer who books every week —
    and the desk's next move is to register them a second time, splitting their
    booking history across two accounts.

    The shapes that reach the desk are the bare ten digits, ten digits with the
    ``+91`` / ``91`` country code, and ten digits with a trunk ``0``. A number
    written with an explicit ``+`` is held to its full country-coded length:
    ``+91 98765 432`` is a truncated number, and dropping the two digits it is
    missing would otherwise leave ten that start with 9 and pass as somebody's
    mobile. Anything else returns None, which makes the caller say "that is not
    a mobile number" instead of reporting a customer who does not exist.
    """
    raw = (value or "").strip()
    digits = _NON_DIGITS.sub("", raw)
    if raw.startswith("+"):
        if len(digits) != 12 or not digits.startswith("91"):
            return None
        digits = digits[2:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not MOBILE_PATTERN.fullmatch(digits):
        return None
    return digits


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


__all__ = [
    "MOBILE_PATTERN",
    "has_real_mobile",
    "mobile_match_sql",
    "normalise_mobile",
    "require_real_mobile",
]
