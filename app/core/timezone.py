# ============================================================
# WAY TERO — PLATFORM TIMEZONE
# File: app/core/timezone.py
#
# Single source of truth for "what timezone are we in".
# Reads PLATFORM_TIMEZONE from system_configurations, with a short
# in-memory cache so the DB is not hit on every request.
#
# Why a helper instead of importing zoneinfo directly:
#   - Hotel billing/settlement math (overtime, night boundaries, late
#     check-out) is anchored to the platform's local time, not UTC.
#   - The admin settings page can change PLATFORM_TIMEZONE at runtime.
#     Hard-coding `Asia/Kolkata` (the seeded default) would silently
#     break overtime math if the platform ever moves to UTC or another
#     IANA zone.
#   - DateTime(timezone=True) columns store UTC, but the comparison
#     logic must be done in platform-local to avoid a guest who checks
#     out "23:30 IST" being billed for the next day.
#
# Defaults to Asia/Kolkata when the row is missing or the IANA value is
# invalid — that's the seed value in migration 0009_platform_config_seed.
#
# Doc Ref: BRD Part 1 §13 (Platform Locale), DB Schema Part 1 §12
# ============================================================

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The seed value in migration 0009. Used as the fallback when the
# configuration row is missing or the IANA name is invalid.
DEFAULT_PLATFORM_TZ = "Asia/Kolkata"

CONFIG_KEY = "PLATFORM_TIMEZONE"

# How long the cached zoneinfo is considered fresh. 60 s is short enough
# that an admin settings change is reflected within a minute, and long
# enough that a 100 RPS hotel booking page never hits the DB for the tz.
_CACHE_TTL_SECONDS = 60.0

# ── Module-level cache ───────────────────────────────────────────────────
# Stored in two slots so a concurrent reader can swap them atomically
# without ever holding the lock during a DB round-trip.
_cache_lock = threading.Lock()
_cache_zone: Optional[ZoneInfo] = None
_cache_zone_name: Optional[str] = None
_cache_fetched_at: float = 0.0


def _build_zone(name: str) -> ZoneInfo:
    """Build a ZoneInfo, falling back to the default on an unknown name.

    A bad value in system_configurations (someone typed "IST" or
    "GMT+5:30") would otherwise raise at every check-out. The fallback
    keeps the platform working; the admin settings page should validate
    the input before saving.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_PLATFORM_TZ)


def get_platform_tz_sync() -> ZoneInfo:
    """Sync accessor — uses the cache only, no DB read.

    Safe to call from anywhere (request handler, billing math, logging).
    Returns the cached zone if it was populated by a prior
    `get_platform_tz(db)` call, otherwise returns the default. The
    default is correct on a fresh boot because the platform timezone
    is seeded in migration 0009.
    """
    with _cache_lock:
        if _cache_zone is not None:
            return _cache_zone
    return ZoneInfo(DEFAULT_PLATFORM_TZ)


def get_platform_tz_name_sync() -> str:
    """Sync accessor for the IANA name (used by the UI to label timestamps)."""
    global _cache_zone_name
    with _cache_lock:
        if _cache_zone_name is not None:
            return _cache_zone_name
    return DEFAULT_PLATFORM_TZ


async def get_platform_tz(db: AsyncSession) -> ZoneInfo:
    """Async accessor — reads the DB on cache miss.

    Call this once per request at the top of an endpoint (or wherever
    a session is available) and pass the result through, or rely on
    the sync fallback if the request is purely numeric.
    """
    global _cache_zone, _cache_zone_name, _cache_fetched_at

    # Fast path — cache hit.
    with _cache_lock:
        if (
            _cache_zone is not None
            and (time.monotonic() - _cache_fetched_at) < _CACHE_TTL_SECONDS
        ):
            return _cache_zone

    # Slow path — read the row. The lock is released during the DB call
    # so concurrent requests don't queue, then re-taken to swap in the
    # fresh value. Stale-while-revalidate would be slightly nicer but
    # the budget is small (1 row, primary key) so a single round-trip is
    # fine.
    try:
        row = (
            await db.execute(
                text(
                    "SELECT config_value FROM system_configurations WHERE config_key = :k"
                ),
                {"k": CONFIG_KEY},
            )
        ).first()
        name = (row[0] if row else None) or DEFAULT_PLATFORM_TZ
    except Exception:
        # If the DB is briefly unavailable, fall back to the default
        # rather than fail the entire request — the math degrades to
        # Asia/Kolkata, which is the seeded value.
        name = DEFAULT_PLATFORM_TZ

    new_zone = _build_zone(name)
    with _cache_lock:
        _cache_zone = new_zone
        _cache_zone_name = name
        _cache_fetched_at = time.monotonic()
    return new_zone


async def get_platform_tz_name(db: AsyncSession) -> str:
    """Async accessor for the IANA name (used by the UI to label timestamps)."""
    global _cache_zone_name
    with _cache_lock:
        if (
            _cache_zone_name is not None
            and (time.monotonic() - _cache_fetched_at) < _CACHE_TTL_SECONDS
        ):
            return _cache_zone_name
    await get_platform_tz(db)
    with _cache_lock:
        return _cache_zone_name or DEFAULT_PLATFORM_TZ


def invalidate_cache() -> None:
    """Drop the cached zone — call from the admin settings endpoint that
    updates PLATFORM_TIMEZONE so the change is visible immediately
    instead of after the TTL expires.
    """
    global _cache_zone, _cache_zone_name, _cache_fetched_at
    with _cache_lock:
        _cache_zone = None
        _cache_zone_name = None
        _cache_fetched_at = 0.0


# ── Convenience helpers ─────────────────────────────────────────────────


def to_platform_tz(dt: datetime) -> datetime:
    """Convert any datetime (naive or aware) to the cached platform zone.

    Naive values are assumed to be in the platform zone — that matches
    the partner-portal convention of sending a wall-clock from the
    device, which has no tzinfo. Aware values are converted. The result
    is always tz-aware in the platform zone.
    """
    zone = get_platform_tz_sync()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def to_platform_tz_with_zone(dt: datetime, zone: ZoneInfo) -> datetime:
    """Same as to_platform_tz but takes the zone explicitly — used when
    the caller already fetched it via get_platform_tz(db) and wants to
    avoid the second cache lookup.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def as_utc(dt: datetime) -> datetime:
    """Normalise a stamp to UTC for storage in DateTime(timezone=True).

    Naive values are treated as platform-local; aware values are
    converted. Returns an aware UTC datetime.
    """
    return to_platform_tz(dt).astimezone(timezone.utc)


def as_utc_with_zone(dt: datetime, zone: ZoneInfo) -> datetime:
    """Same as as_utc with an explicit zone."""
    return to_platform_tz_with_zone(dt, zone).astimezone(timezone.utc)


def format_in_platform_tz(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a datetime as a UI-friendly string in the platform zone.

    Returns "—" for None so the UI can use the helper directly.
    """
    if dt is None:
        return "—"
    local = to_platform_tz(dt)
    return local.strftime(fmt)


# ── Misc ───────────────────────────────────────────────────────────────

__all__ = [
    "DEFAULT_PLATFORM_TZ",
    "CONFIG_KEY",
    "get_platform_tz",
    "get_platform_tz_sync",
    "get_platform_tz_name",
    "get_platform_tz_name_sync",
    "invalidate_cache",
    "to_platform_tz",
    "to_platform_tz_with_zone",
    "as_utc",
    "as_utc_with_zone",
    "format_in_platform_tz",
]
