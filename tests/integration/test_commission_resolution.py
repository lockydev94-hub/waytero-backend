# ============================================================
# WAYTERO — COMMISSION RULE RESOLUTION
# File: tests/integration/test_commission_resolution.py
# Doc Ref: Round 15 C4a; DB Schema Part 2 §15 (commission_rules)
#
# Which percentage (or flat amount) the platform keeps is decided once per
# closed trip, and it used to be decided by four hand-written lookups that
# filtered `commission_type = 'PERCENTAGE'` and then took an unordered
# `LIMIT 1` over every city's rules. Two consequences, both money:
#
#   * a city configured with a FLAT CAB rule (Bhubaneswar is, at ₹900) was
#     skipped and the trip was closed at the platform default;
#   * a city with no rule of its own could be billed whichever city's rule the
#     planner happened to return first.
#
# These tests run against the real Postgres so the effective-date filtering and
# the "exactly one row in force" precedence are proved in SQL rather than in a
# mock. Each test seeds inside its own transaction and the transaction is always
# rolled back, so the owner's commission sheets are not touched.
# ============================================================
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.admin.services import CommissionService
from app.modules.booking.services.settlement_math import compute_close_economics

# These tests seed their own rules, and they seed them under a service type the
# platform does not sell. Reusing "CAB" would make the assertions depend on
# however many city-less CAB rules the development database happens to hold
# today — and the whole point of one of them is that exactly one rule answers.
_SERVICE = "BOAT"


def _with_session(work):
    """Run `work(session)` on a private engine inside a transaction that is
    always rolled back."""

    async def run():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                trans = await conn.begin()
                session = AsyncSession(
                    bind=conn,
                    expire_on_commit=False,
                    autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
                try:
                    return await work(session)
                finally:
                    await session.close()
                    await trans.rollback()
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _new_city(session: AsyncSession, label: str) -> int:
    state_id = (await session.execute(text("SELECT min(id) FROM states"))).scalar_one()
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO cities (state_id, name, is_active) "
                    "VALUES (:s, :n, true) RETURNING id"
                ),
                {"s": state_id, "n": f"_wt_comm_{label}"},
            )
        ).scalar_one()
    )


async def _new_group(session: AsyncSession, label: str) -> int:
    return int(
        (
            await session.execute(
                text("INSERT INTO commission_groups (group_name) VALUES (:n) RETURNING id"),
                {"n": f"_wt_comm_{label}"},
            )
        ).scalar_one()
    )


async def _new_rule(
    session: AsyncSession,
    *,
    group_id: int,
    city_id,
    commission_type: str = "PERCENTAGE",
    value="10",
    service_type: str = _SERVICE,
    effective_from=None,
    effective_to=None,
    is_active: bool = True,
) -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO commission_rules "
                    "(commission_group_id, service_type, commission_type, "
                    " commission_value, city_id, effective_from, effective_to, is_active) "
                    "VALUES (:g, :st, :ct, :v, :c, :ef, :et, :a) RETURNING id"
                ),
                {
                    "g": group_id,
                    "st": service_type,
                    "ct": commission_type,
                    "v": value,
                    "c": city_id,
                    "ef": effective_from,
                    "et": effective_to,
                    "a": is_active,
                },
            )
        ).scalar_one()
    )


async def _setup(session: AsyncSession) -> dict:
    """A group plus cities to hang rules on, for this transaction only."""
    out = {"group_id": await _new_group(session, "group")}
    for key in ("percent_city", "flat_city", "empty_city", "novalue_city", "conflict_city"):
        out[key] = await _new_city(session, key)
    out["percent_rule"] = await _new_rule(
        session, group_id=out["group_id"], city_id=out["percent_city"], value="18"
    )
    out["flat_rule"] = await _new_rule(
        session,
        group_id=out["group_id"],
        city_id=out["flat_city"],
        commission_type="FLAT",
        value="900",
    )
    await _new_rule(
        session, group_id=out["group_id"], city_id=out["novalue_city"], value=None
    )
    await _new_rule(
        session, group_id=out["group_id"], city_id=out["conflict_city"], value="11"
    )
    await _new_rule(
        session, group_id=out["group_id"], city_id=out["conflict_city"], value="12"
    )
    return out


def _resolve(city_key: str, *, for_date=None, service_type: str = _SERVICE):
    """Resolve one booking-shaped question and hand back the rule plus the ids
    it was expected to pick, so the assertions can name the row."""

    async def flow(session: AsyncSession):
        env = await _setup(session)
        rule = await CommissionService.resolve(
            session,
            city_id=env[city_key],
            service_type=service_type,
            for_date=for_date,
        )
        return rule, env

    return _with_session(flow)


# ── precedence ────────────────────────────────────────────────


def test_the_partner_s_own_city_rule_answers_first() -> None:
    rule, env = _resolve("percent_city")
    assert rule.commission_type == "PERCENTAGE"
    assert rule.value == Decimal("18.00")
    assert rule.source == "city"
    assert rule.rule_id == env["percent_rule"]


def test_a_flat_city_rule_is_used_instead_of_the_platform_default() -> None:
    """The regression this round exists for.

    Before: `commission_type = 'PERCENTAGE'` in the WHERE clause made a FLAT
    rule invisible, so a ₹1000 fare was closed at the default 10% — ₹100 —
    against the ₹900 the admin had configured for that city.
    """
    rule, env = _resolve("flat_city")
    assert rule.commission_type == "FLAT"
    assert rule.value == Decimal("900.00")
    assert rule.rule_id == env["flat_rule"]

    econ = compute_close_economics(final_amount=1000, commission=rule)
    assert econ.platform_commission == Decimal("900.00")
    assert econ.partner_payout == Decimal("100.00")


def test_a_city_with_no_rule_takes_the_platform_rule_not_a_neighbour_s() -> None:
    """An empty city must never inherit another city's percentage.

    The old "general" fallback removed the city filter instead of requiring
    `city_id IS NULL`, so with rules for several cities `LIMIT 1` decided.
    """

    async def flow(session: AsyncSession):
        env = await _setup(session)
        platform_rule = await _new_rule(
            session,
            group_id=env["group_id"],
            city_id=None,
            value="7.5",
        )
        rule = await CommissionService.resolve(
            session, city_id=env["empty_city"], service_type=_SERVICE
        )
        assert rule.source == "platform-wide"
        assert rule.rule_id == platform_rule
        assert rule.value == Decimal("7.50")

    _with_session(flow)


def test_a_service_with_no_rules_anywhere_uses_the_documented_default() -> None:
    """Nothing is configured for `SUBMARINE`, so no rule can answer.

    The fallback is tagged `default_10pct` with no rule id, so a booking's
    snapshot can never be read later as "rule #7 said this" when no rule did.
    """
    rule, _ = _resolve("empty_city", service_type="SUBMARINE")
    assert rule.source == "default_10pct"
    assert rule.rule_id is None
    assert rule.commission_type == "PERCENTAGE"
    assert rule.value == Decimal("10")


def test_an_inactive_rule_does_not_answer_even_when_it_is_the_only_city_rule() -> None:
    async def flow(session: AsyncSession):
        env = await _setup(session)
        city = await _new_city(session, "inactive")
        await _new_rule(
            session, group_id=env["group_id"], city_id=city, value="33", is_active=False
        )
        rule = await CommissionService.resolve(
            session, city_id=city, service_type=_SERVICE
        )
        # BOAT has no platform-wide rule either, so the only legitimate answer
        # is the tagged default — the deactivated 33% never surfaces.
        assert rule.source == "default_10pct"
        assert rule.value == Decimal("10")

    _with_session(flow)


# ── effective dates ───────────────────────────────────────────


def test_only_the_rule_in_force_on_the_day_answers() -> None:
    """Effective dates exist so a superseded sheet stops applying on the day it
    was superseded. Ignoring them bills a partner on a rule the admin replaced.
    """

    async def flow(session: AsyncSession):
        env = await _setup(session)
        city = await _new_city(session, "dated")
        group_id = env["group_id"]
        # Windows are built around the day the test runs, and every resolve call
        # names its own day. Leaving any call to default to "today" would make
        # the assertion depend on the wall clock rolling over mid-run.
        day = datetime.now(timezone.utc).date()
        expired = await _new_rule(
            session,
            group_id=group_id,
            city_id=city,
            value="40",
            effective_from=day - timedelta(days=30),
            effective_to=day - timedelta(days=1),
        )
        future = await _new_rule(
            session,
            group_id=group_id,
            city_id=city,
            value="50",
            effective_from=day + timedelta(days=1),
        )
        live = await _new_rule(
            session,
            group_id=group_id,
            city_id=city,
            value="15",
            effective_from=day - timedelta(days=1),
            effective_to=day + timedelta(days=1),
        )

        rule = await CommissionService.resolve(
            session, city_id=city, service_type=_SERVICE, for_date=day
        )
        assert rule.rule_id == live
        assert rule.value == Decimal("15.00")

        # A back-dated close reads the sheet that was in force that day, which
        # is exactly what a dispute about an old trip needs.
        back_dated = await CommissionService.resolve(
            session,
            city_id=city,
            service_type=_SERVICE,
            for_date=day - timedelta(days=2),
        )
        assert back_dated.rule_id == expired

        ahead = await CommissionService.resolve(
            session,
            city_id=city,
            service_type=_SERVICE,
            for_date=day + timedelta(days=2),
        )
        assert ahead.rule_id == future

    _with_session(flow)


# ── refusing to guess ─────────────────────────────────────────


def test_two_rules_in_force_for_one_city_is_refused_not_guessed() -> None:
    """Row order deciding a partner's commission is not a decision."""
    with pytest.raises(BusinessException) as exc_info:
        _resolve("conflict_city")
    assert "in force" in exc_info.value.message
    assert exc_info.value.status_code == 422


def test_a_rule_configured_without_a_value_raises_instead_of_defaulting() -> None:
    """Reading a blank rule as 10% is the same invention this round removed: the
    admin left the field empty, the platform charged a number nobody set."""
    with pytest.raises(BusinessException) as exc_info:
        _resolve("novalue_city")
    assert "no value" in exc_info.value.message
