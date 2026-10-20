# ============================================================
# WAYTERO — BOOKING AND INVOICE NUMBER GENERATORS
# File: app/modules/booking/services/numbering.py
# Doc Ref: BRD Part 3 §20 — sequence-based booking numbering
#          BRD Part 3 §45 — invoice created when payment is recorded
#
# Booking numbers are read off the table they belong to (`MAX(...) + 1` per
# day, same pattern as
# app.modules.booking.services._next_receipt_number): a skipped number is
# harmless there, the column is UNIQUE, and the caller retries.
#
# Invoice numbers are the opposite. They are legal documents, so a number must
# not be handed to two payments and must not depend on how many rows happen to
# be visible at the moment of reading — the four `MAX(SPLIT_PART(...))+1` and
# `COUNT(*)+1` generators this file replaces could do exactly that, because two
# concurrent collections read the same maximum before either row existed. A
# counter row taken `FOR UPDATE` is the one shape in Postgres where the second
# transaction waits for the first to finish, and the increment lives in the
# caller's transaction: a collection that rolls back leaves no number spent.
# ============================================================

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class InvoiceKind:
    """How one document kind writes its number.

    The three fields reproduce the formats already printed on customer
    documents; nothing is re-styled here, because an invoice number that
    changes shape mid-year is a hole in the books.
    """

    prefix: str
    stamp: str
    infix: str
    digits: int


INVOICE_KINDS: dict[str, InvoiceKind] = {
    # WT-INV-202610-00042
    "CAB": InvoiceKind(prefix="WT-INV-", stamp="%Y%m", infix="-", digits=5),
    # WT-HINV-2026000042 — the year and the sequence run together
    "HOTEL": InvoiceKind(prefix="WT-HINV-", stamp="%Y", infix="", digits=6),
}


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


async def _take_counter_seq(db: AsyncSession, kind: str) -> int:
    """Advance and return the counter row for one document kind.

    The row is locked, not merely read: two collections in the same second are
    stopped here, and the second one gets the number after the first. The
    increment lives in the caller's transaction, so a collection that rolls back
    leaves no number spent — and no hole, which a legal document can live with
    but a counter cannot be trusted to prevent.
    """
    seq = (
        await db.execute(
            text(
                "SELECT last_seq FROM invoice_sequences WHERE kind = :kind FOR UPDATE"
            ),
            {"kind": kind},
        )
    ).scalar_one_or_none()
    if seq is None:
        raise ValueError(
            f"invoice_sequences has no row for kind {kind!r}; migrations 0086 "
            "and 0088 seed one per configured kind"
        )
    next_seq = int(seq) + 1
    await db.execute(
        text(
            "UPDATE invoice_sequences SET last_seq = :n, updated_at = NOW() "
            "WHERE kind = :kind"
        ),
        {"n": next_seq, "kind": kind},
    )
    await db.flush()
    return next_seq


async def next_invoice_number(db: AsyncSession, kind: str) -> str:
    """Take the next invoice number for ``kind`` off its own counter row.

    `kind` is a key of `INVOICE_KINDS` — `CAB` or `HOTEL` — and the counter rows
    were seeded by migration 0086 from the highest number each table already
    held, so the first counter-issued invoice cannot collide with one the old
    generators wrote.

    An unconfigured `kind` is refused rather than started at 1: a document whose
    table already holds `WT-TINV-…-0007` numbers would issue a duplicate the
    moment an on-demand counter began at one. A new kind is added to
    `INVOICE_KINDS` and seeded with the same read-the-old-maximum step 0086
    used, once, inside a migration.
    """
    spec = INVOICE_KINDS.get(kind)
    if spec is None:
        raise ValueError(
            f"No invoice number format is configured for kind {kind!r} "
            f"(known: {', '.join(sorted(INVOICE_KINDS))})"
        )

    next_seq = await _take_counter_seq(db, kind)
    stamp = datetime.now(timezone.utc).strftime(spec.stamp)
    return f"{spec.prefix}{stamp}{spec.infix}{str(next_seq).zfill(spec.digits)}"


# WT-PAY-202610-00042. A payment number is not an invoice number — one names
# money received, the other names the tax document for a trip — so it draws on
# its own counter row ('PAYMENT', seeded by migration 0088) in the same table
# while keeping the shape of the invoice numbers customers already recognise.
PAYMENT_KIND = InvoiceKind(prefix="WT-PAY-", stamp="%Y%m", infix="-", digits=5)


async def next_payment_number(db: AsyncSession) -> str:
    """Take the next payment number off the `PAYMENT` counter."""
    next_seq = await _take_counter_seq(db, "PAYMENT")
    stamp = datetime.now(timezone.utc).strftime(PAYMENT_KIND.stamp)
    return (
        f"{PAYMENT_KIND.prefix}{stamp}{PAYMENT_KIND.infix}"
        f"{str(next_seq).zfill(PAYMENT_KIND.digits)}"
    )
