# ============================================================
# WAY TERO — PLATFORM TIMEZONE HELPER TESTS
# File: tests/unit/test_hotel_timezone.py
# Doc Ref: BRD Part 1 §13 (Platform Locale); Migration 0009_platform_config_seed
#
# `app.core.timezone` is the single source of truth for the platform zone.
# These tests pin:
#   1. PLATFORM_TIMEZONE is read from system_configurations and honoured.
#   2. It degrades to the seeded default (Asia/Kolkata) when the row is
#      missing, the IANA name is invalid, or the DB is briefly unavailable.
#   3. The 60 s in-memory cache means a second read does not hit the DB.
#   4. Naive datetimes are treated as platform-local; aware ones converted.
#
# The module keeps a process-wide cache, so every test clears it via the
# autouse fixture — otherwise a stale zone leaks between tests.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from zoneinfo import ZoneInfo

from app.core.timezone import (
    DEFAULT_PLATFORM_TZ,
    as_utc,
    format_in_platform_tz,
    get_platform_tz,
    get_platform_tz_name,
    get_platform_tz_name_sync,
    get_platform_tz_sync,
    invalidate_cache,
    to_platform_tz,
)

DEFAULT = "Asia/Kolkata"
HAS_TZ_OFFSET = 5 * 3600 + 30 * 60  # Asia/Kolkata → UTC+05:30


@pytest.fixture(autouse=True)
def _reset_tz_cache():
    invalidate_cache()
    yield
    invalidate_cache()


class _FakeRowResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    """Routes the single SQL read in get_platform_tz to a canned value."""

    def __init__(self, value=None, *, raise_on_execute=False):
        self.value = value
        self.raise_on_execute = raise_on_execute

    async def execute(self, _stmt, _params=None):
        if self.raise_on_execute:
            raise RuntimeError("db unavailable")
        return _FakeRowResult((self.value,))


def _kolkata() -> ZoneInfo:
    return ZoneInfo(DEFAULT)


# ── Sync fallback ─────────────────────────────────────────────────────────


def test_sync_fallback_default_before_any_fetch():
    assert get_platform_tz_sync().key == DEFAULT


def test_sync_fallback_name_default_before_any_fetch():
    assert get_platform_tz_name_sync() == DEFAULT


# ── Async read + cache ────────────────────────────────────────────────────


async def test_get_platform_tz_defaults_when_row_absent():
    zone = await get_platform_tz(_FakeDb(None))
    assert zone.key == DEFAULT_PLATFORM_TZ
    assert zone == _kolkata()
    name = await get_platform_tz_name(_FakeDb(None))
    assert name == DEFAULT_PLATFORM_TZ


async def test_get_platform_tz_honors_stored_value():
    zone = await get_platform_tz(_FakeDb("UTC"))
    assert zone.key == "UTC"


async def test_get_platform_tz_falls_back_on_invalid_iana():
    zone = await get_platform_tz(_FakeDb("IST/GMT+5:30"))
    assert zone.key == DEFAULT_PLATFORM_TZ


async def test_get_platform_tz_falls_back_when_db_unavailable():
    zone = await get_platform_tz(_FakeDb(raise_on_execute=True))
    assert zone.key == DEFAULT_PLATFORM_TZ


async def test_cached_value_used_within_ttl():
    # First read populates the cache from the "Asia/Kolkata" row.
    first = await get_platform_tz(_FakeDb("Asia/Kolkata"))
    assert first.key == "Asia/Kolkata"

    # A second call with a *different* row must NOT re-hit the DB — the
    # cache (fresh within the 60 s TTL) wins.
    second = await get_platform_tz(_FakeDb("UTC"))
    assert second.key == "Asia/Kolkata"

    # The sync accessor agrees.
    assert get_platform_tz_sync().key == "Asia/Kolkata"


async def test_invalidate_cache_forces_refetch():
    await get_platform_tz(_FakeDb("Asia/Kolkata"))
    invalidate_cache()
    zone = await get_platform_tz(_FakeDb("UTC"))
    assert zone.key == "UTC"


async def test_name_tracks_cached_zone():
    await get_platform_tz(_FakeDb("Asia/Kolkata"))
    assert get_platform_tz_name_sync() == "Asia/Kolkata"


# ── Conversion helpers ────────────────────────────────────────────────────


def test_to_platform_tz_naive_is_platform_local():
    dt = datetime(2026, 8, 15, 14, 30)
    out = to_platform_tz(dt)
    assert out.tzinfo is not None
    assert out.utcoffset().total_seconds() == HAS_TZ_OFFSET
    assert out.year == 2026 and out.hour == 14 and out.minute == 30


def test_to_platform_tz_aware_is_converted():
    dt = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
    out = to_platform_tz(dt)
    assert out.utcoffset().total_seconds() == HAS_TZ_OFFSET
    # 09:00 UTC == 14:30 Asia/Kolkata
    assert out.hour == 14 and out.minute == 30


def test_to_platform_tz_other_zone_converted():
    dt = datetime(2026, 8, 15, 10, 0, tzinfo=ZoneInfo("UTC"))
    out = to_platform_tz(dt)
    assert out.hour == 15 and out.minute == 30


def test_as_utc_naive_treated_as_platform_local():
    dt = datetime(2026, 8, 15, 14, 30)
    out = as_utc(dt)
    assert out.tzinfo == timezone.utc
    # 14:30 Asia/Kolkata == 09:00 UTC
    assert out.hour == 9 and out.minute == 0


def test_as_utc_aware_converted_to_utc():
    dt = datetime(2026, 8, 15, 14, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    out = as_utc(dt)
    assert out.tzinfo == timezone.utc
    assert out.hour == 9 and out.minute == 0


def test_format_in_platform_tz_renders_platform_local():
    dt = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
    rendered = format_in_platform_tz(dt)
    # 09:00 UTC renders as 14:30 in the platform zone.
    assert rendered == "2026-08-15 14:30"


def test_format_in_platform_tz_none_dash():
    assert format_in_platform_tz(None) == "—"
