# ============================================================
# WAYTERO — CAB TRIP TYPE AVAILABILITY
# File: app/modules/booking/services/cab_availability.py
# Doc Ref:
#   BRD Part 3 §35 — Fare engine
#   Admin Settings → Cab Booking
#
# One runtime switch per bookable cab product. The admin toggles
# `CAB_BOOKING_<TYPE>_ENABLED` in system_configurations; every booking
# surface (website, customer app, customer care desk) reads the same row.
#
# Disabling a type stops new quotes and bookings. It never edits or cancels
# trips that already exist — operations still need to service those.
# ============================================================

from typing import Iterable, List, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The order the public website and the admin desk present these in.
CAB_TRIP_TYPES: Sequence[str] = (
    "LOCAL",
    "AIRPORT",
    "OUTSTATION",
    "ONE_WAY",
    "ROUND_TRIP",
    "DAILY",
)

CAB_TRIP_LABELS = {
    "LOCAL": "Local",
    "AIRPORT": "Airport",
    "OUTSTATION": "Outstation",
    "ONE_WAY": "One Way",
    "ROUND_TRIP": "Round Trip",
    "DAILY": "Cab Rent",
}

# A missing row falls back to the product default rather than to "enabled".
# One Way is switched off for the launch configuration; the migration seeds
# the row, and this default keeps a manual delete from silently turning it on.
_DEFAULT_ENABLED = {code: code != "ONE_WAY" for code in CAB_TRIP_TYPES}

_TRUTHY = {"1", "true", "yes", "on", "enabled"}


def cab_booking_config_key(trip_type: str) -> str:
    return f"CAB_BOOKING_{trip_type.strip().upper()}_ENABLED"


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def cab_trip_label(trip_type: str) -> str:
    code = (trip_type or "").strip().upper()
    return CAB_TRIP_LABELS.get(code, code.replace("_", " ").title())


def cab_booking_disabled_message(trip_type: str) -> str:
    return (
        f"{cab_trip_label(trip_type)} cab bookings are currently disabled. "
        "An administrator can turn this trip type back on in Settings → Cab Booking."
    )


async def enabled_cab_trip_types(db: AsyncSession) -> List[str]:
    """The trip types the admin has left bookable, in display order.

    One query reads every switch; an absent key uses `_DEFAULT_ENABLED`.
    Unknown values are treated as disabled, so a typo cannot accidentally
    open a product the administrator meant to keep closed.
    """
    result = await db.execute(
        text(
            "SELECT config_key, config_value FROM system_configurations "
            "WHERE config_key LIKE 'CAB_BOOKING_%_ENABLED'"
        )
    )
    stored = {row.config_key: row.config_value for row in result.mappings().all()}

    enabled: List[str] = []
    for code in CAB_TRIP_TYPES:
        key = cab_booking_config_key(code)
        value = stored.get(key)
        if value is None:
            is_enabled = _DEFAULT_ENABLED[code]
        else:
            is_enabled = _is_truthy(value)
        if is_enabled:
            enabled.append(code)
    return enabled


async def is_cab_trip_type_enabled(db: AsyncSession, trip_type: str) -> bool:
    code = (trip_type or "").strip().upper()
    if code not in CAB_TRIP_TYPES:
        # Unknown/new products are not silently blocked by this feature.
        return True
    return code in await enabled_cab_trip_types(db)


async def filter_enabled_cab_trip_types(
    db: AsyncSession, trip_types: Iterable[str]
) -> List[str]:
    """Keep only enabled codes, preserving the caller's order."""
    enabled = set(await enabled_cab_trip_types(db))
    return [code for code in trip_types if code in enabled]
