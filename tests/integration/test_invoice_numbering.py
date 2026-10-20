# ============================================================
# WAYTERO — INVOICE NUMBERING
# File: tests/integration/test_invoice_numbering.py
# Doc Ref: Round 15 C4a; migration 0086 (invoice_sequences).
#
# An invoice number is a legal identifier, so the two things that matter about
# the generator are that it never gives the same number to two payments and that
# it does not burn a number for a payment that never happened. Both are
# properties of the counter row being locked inside the caller's transaction,
# which is exactly what reading MAX(...) + 1 off the table could not promise:
# two collections in the same second saw the same maximum.
#
# Everything runs in one transaction that is always rolled back, including the
# deliberate deletion of a counter row — the owner's real invoice book is not
# touched, and the last assertion is that the counter is where it was.
# ============================================================
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.modules.booking.services.numbering import (
    INVOICE_KINDS,
    next_invoice_number,
)


def _dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")


def _fetch(sql: str, *args) -> list[dict]:
    import asyncpg

    async def run():
        conn = await asyncpg.connect(_dsn())
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _counter(kind: str) -> int:
    return int(_fetch(
        "SELECT last_seq FROM invoice_sequences WHERE kind = $1", kind
    )[0]["last_seq"])


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


def test_two_invoices_in_a_row_take_two_numbers() -> None:
    """The claim is only that consecutive calls give consecutive numbers — the
    counter is global per kind, so a gap from another month's document is normal
    and a repeat never is."""
    async def flow(session: AsyncSession) -> list[str]:
        first = await next_invoice_number(session, "CAB")
        second = await next_invoice_number(session, "CAB")
        await session.flush()
        return [first, second]

    numbers = _with_session(flow)
    first_seq = int(numbers[0].rsplit("-", 1)[1])
    second_seq = int(numbers[1].rsplit("-", 1)[1])
    assert first_seq != second_seq
    assert second_seq == first_seq + 1


def test_a_cab_invoice_keeps_the_shape_the_customer_already_holds() -> None:
    """`WT-INV-<YYYYMM>-<5 digits>`: the month is the month the money came in,
    and re-styling an invoice number mid-year would be a hole in the books."""
    async def flow(session: AsyncSession) -> str:
        return await next_invoice_number(session, "CAB")

    number = _with_session(flow)
    assert re.fullmatch(r"WT-INV-\d{6}-\d{5}", number), number
    assert number.split("-")[2] == datetime.now(timezone.utc).strftime("%Y%m")


def test_a_hotel_invoice_keeps_its_year_and_six_digits() -> None:
    """`WT-HINV-<YYYY><6 digits>` with no separator — the format the hotel
    generator printed before the counter existed, and the reason the two kinds
    are seeded from their columns differently in migration 0086."""
    async def flow(session: AsyncSession) -> str:
        return await next_invoice_number(session, "HOTEL")

    number = _with_session(flow)
    assert re.fullmatch(r"WT-HINV-\d{4}\d{6}", number), number
    assert number[len(INVOICE_KINDS["HOTEL"].prefix) :][:4] == str(
        datetime.now(timezone.utc).year
    )


def test_the_counter_row_is_locked_while_the_number_is_taken() -> None:
    """The one property that makes a shared counter safe: a second transaction
    cannot read the row until the first one ends. Proved by trying — from another
    connection, `NOWAIT`, while this transaction still holds the lock."""

    async def flow(session: AsyncSession) -> None:
        await next_invoice_number(session, "CAB")
        await session.flush()

        import asyncpg

        conn = await asyncpg.connect(_dsn())
        try:
            import asyncpg.exceptions as pg_exc

            with pytest.raises(pg_exc.PostgresError) as exc_info:
                await conn.fetch(
                    "SELECT last_seq FROM invoice_sequences "
                    "WHERE kind = 'CAB' FOR UPDATE NOWAIT"
                )
            # 55P03 lock_not_available — the row is still this transaction's.
            assert exc_info.value.sqlstate == "55P03"
        finally:
            await conn.close()

    _with_session(flow)


def test_an_unconfigured_kind_is_refused_not_started_at_one() -> None:
    """A table that already has invoice numbers of its own cannot be handed a
    counter that begins at 00001 — the duplicate would surface as a UNIQUE
    violation on somebody's payment, or as two identical invoices when the
    column has no unique index at all."""
    async def flow(session: AsyncSession) -> str:
        try:
            await next_invoice_number(session, "TOUR")
        except ValueError as exc:
            return str(exc)
        raise AssertionError("an unconfigured kind issued a number")

    message = _with_session(flow)
    assert "TOUR" in message
    assert "CAB" in message and "HOTEL" in message


def test_a_missing_counter_row_is_refused_not_guessed() -> None:
    """Delete the row and the generator says so instead of inventing a starting
    point — and the deletion is rolled back, so the owner's counter is untouched
    either way."""
    async def flow(session: AsyncSession) -> str:
        await session.execute(
            text("DELETE FROM invoice_sequences WHERE kind = 'CAB'")
        )
        await session.flush()
        try:
            await next_invoice_number(session, "CAB")
        except ValueError as exc:
            return str(exc)
        raise AssertionError("a number came out of a missing counter")

    message = _with_session(flow)
    assert "invoice_sequences" in message
    assert "0086" in message


def test_rolled_back_numbers_are_not_spent() -> None:
    """The increment lives in the caller's transaction, so a collection that
    fails leaves no hole in the invoice book — a payment that was never recorded
    should not have taken a number with it."""
    before = _counter("CAB")

    async def flow(session: AsyncSession) -> None:
        await next_invoice_number(session, "CAB")

    _with_session(flow)
    assert _counter("CAB") == before
