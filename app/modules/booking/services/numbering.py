# ============================================================
# WAYTERO — BOOKING NUMBER GENERATOR
# File: app/modules/booking/services/numbering.py
# Doc Ref: BRD Part 3 §20 — sequence-based booking numbering
#
# MAX-based sequence per day (same pattern as
# app.modules.booking.services._next_receipt_number). Reads from the
# master_bookings and cab_bookings tables, returns the next available
# number for today in the format ``MT-YYYYMMDD-NNNNN`` and
# ``CB-YYYYMMDD-NNNNN``. The five-digit suffix is per-day, so a
# concurrent insert that lands before this query completes still gets
# a unique number — the table's UNIQUE constraint catches the
# collision and the calling code retries.
# ============================================================

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _today_utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def _next_seq(
    db: AsyncSession,
    table: str,
    column: str,
    prefix: str,
    today_compact: str,
) -> str:
    """
    Read the maximum trailing integer for today's ``prefix-YYYYMMDD-…``
    rows in ``table`` and return ``prefix-YYYYMMDD-(max+1)`` as a
    zero-padded 5-digit number.

    Tables store both old-format (MT/CB with timestamp + uuid) and new
    numbers; only ``prefix-YYYYMMDD-`` rows are considered.
    """
    pattern = f"{prefix}-{today_compact}-%"
    row = (
        await db.execute(
            text(
                f"""
            SELECT MAX(CAST(SPLIT_PART({column}, '-', 3) AS INTEGER))
            FROM {table}
            WHERE {column} LIKE :pattern
        """
            ),
            {"pattern": pattern},
        )
    ).scalar()
    next_n = (row or 0) + 1
    return f"{prefix}-{today_compact}-{str(next_n).zfill(5)}"


async def next_master_booking_number(db: AsyncSession) -> str:
    """Return the next ``MT-YYYYMMDD-NNNNN`` master booking number."""
    return await _next_seq(
        db, "master_bookings", "booking_number", "MT", _today_utc_compact()
    )


async def next_cab_booking_number(db: AsyncSession) -> str:
    """Return the next ``CB-YYYYMMDD-NNNNN`` cab booking number."""
    return await _next_seq(
        db, "cab_bookings", "booking_number", "CB", _today_utc_compact()
    )
