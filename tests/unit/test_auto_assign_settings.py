# ============================================================
# WAYTERO — AUTO-ASSIGN CONFIGURATION & COHORT GUARD
# File: tests/unit/test_auto_assign_settings.py
# Doc Ref: Round 15 C3; migration 0086 (the auto_assign_* keys).
#
# Two things that must be true before any round is published, and that a
# database cannot check for us:
#   - the switches are read the way the super admin set them, and a missing or
#     nonsense row falls back to the seeded value instead of an exception,
#   - the coupon cohort reaches SQL as integers, never as text.
# The rounds themselves live in
# tests/integration/test_cab_auto_assign_flow.py.
# ============================================================
from __future__ import annotations

from datetime import timedelta

import pytest

from app.modules.booking.services.auto_assign import (
    DEFAULTS,
    AutoAssignSettings,
    _as_int,
    _cohort_predicate,
    settings,
)


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict]:
        return self._rows


class _StubDb:
    """Answers the one SELECT the settings reader makes."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.sql: list[str] = []

    async def execute(self, stmt, *_a, **_kw) -> _Result:
        self.sql.append(str(stmt))
        return _Result(self._rows)


# ── the switches ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_read_the_configured_values() -> None:
    db = _StubDb(
        [
            {"config_key": "auto_assign_enabled", "config_value": "true"},
            {"config_key": "auto_assign_round_size", "config_value": "4"},
            {"config_key": "auto_assign_max_rounds", "config_value": "5"},
            {"config_key": "auto_assign_timeout_seconds", "config_value": "90"},
        ]
    )
    config = await settings(db)  # type: ignore[arg-type]

    assert (config.enabled, config.round_size, config.max_rounds) == (True, 4, 5)
    assert config.acceptance_window == timedelta(seconds=90)
    # One statement, one round trip — the chain reads this on every expiry.
    assert len(db.sql) == 1
    assert "config_key = ANY(:keys)" in db.sql[0]


@pytest.mark.asyncio
async def test_settings_fall_back_to_the_seeded_defaults() -> None:
    """A database that has never had migration 0086 applied, or a row someone
    deleted, must not stop a booking from being offered — and must not silently
    switch the feature on."""
    config = await settings(_StubDb([]))  # type: ignore[arg-type]

    assert config.enabled is False
    assert config.round_size == int(DEFAULTS["auto_assign_round_size"])
    assert config.max_rounds == int(DEFAULTS["auto_assign_max_rounds"])
    assert config.timeout_seconds == int(DEFAULTS["auto_assign_timeout_seconds"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", ["true", "TRUE", " true ", "1", "yes", "on"],
)
async def test_the_switch_is_read_as_a_switch_not_as_a_literal(raw: str) -> None:
    """The value column is text, and an admin types into it."""
    config = await settings(  # type: ignore[arg-type]
        _StubDb([{"config_key": "auto_assign_enabled", "config_value": raw}])
    )
    assert config.enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["false", "0", "off", "", "maybe", None])
async def test_anything_else_leaves_the_chain_off(raw: object) -> None:
    """Default-deny: a half-typed value must not start offering real partners
    trips nobody meant to automate."""
    config = await settings(  # type: ignore[arg-type]
        _StubDb([{"config_key": "auto_assign_enabled", "config_value": raw}])
    )
    assert config.enabled is False


@pytest.mark.parametrize(
    "raw,expected",
    [("3", 3), (" 4 ", 4), ("0", 3), ("-1", 3), ("abc", 3), (None, 3), ("", 3)],
)
def test_a_number_is_a_number_or_the_default(raw: object, expected: int) -> None:
    """Zero or negative rounds, zero partners or a zero-second window would turn
    the chain into a loop that never stops asking — the defaults win instead."""
    assert _as_int(raw, 3) == expected


def test_acceptance_window_is_the_timeout_in_seconds() -> None:
    config = AutoAssignSettings(
        enabled=True, round_size=2, max_rounds=3, timeout_seconds=180
    )
    assert config.acceptance_window == timedelta(minutes=3)


# ── the coupon cohort cannot become SQL text ─────────────────────────────────


def test_no_cohort_means_no_extra_predicate() -> None:
    assert _cohort_predicate(None) == ""
    assert _cohort_predicate([]) == ""


def test_a_cohort_becomes_an_integer_list_not_a_string() -> None:
    assert _cohort_predicate([7, 3]) == "  AND p.id IN (7, 3)"
    # Stored as text in the coupon table, rendered as numbers here.
    assert _cohort_predicate(["7", "3"]) == "  AND p.id IN (7, 3)"


@pytest.mark.parametrize(
    "poison",
    ["1) OR 1=1 --", "7; DROP TABLE users", "1 OR 1=1", "abc"],
)
def test_something_that_is_not_a_number_never_reaches_the_statement(
    poison: str,
) -> None:
    """The cohort comes from a coupon row an admin edited. `int()` is what keeps
    the rendered text numeric: anything else raises here, at the boundary,
    instead of becoming part of the SQL."""
    with pytest.raises((ValueError, TypeError)):
        _cohort_predicate([poison])
