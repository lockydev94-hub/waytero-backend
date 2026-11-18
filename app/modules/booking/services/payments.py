# ============================================================
# WAYTERO — CAB PAYMENT LEDGER
# File: app/modules/booking/services/payments.py
# Doc Ref: DB Schema Part 7 §3 (payments), §4 (types), §5 (statuses),
#          §7 (refunds), §22 (payment rules); Round 15 plan C4b;
#          BRD Part 3 §45 (invoice and payment at trip close).
#
# Why this exists: `payments` was migrated by the finance schema and never
# written to. Two admin reports read it, so both reported "no payments" for
# customers who had paid, and the one column that was supposed to say whether a
# booking is paid — `master_bookings.payment_status` — was hard-coded to 'PAID'
# by whichever endpoint recorded a balance, without looking at the advance, the
# coupon or the fare. A booking the customer had only half paid read 'PAID' to
# finance, to the desk and to the customer's own history.
#
# The rule this module enforces: money moves, a row appears, and the master's
# payment state is derived from those rows and never typed in.
#
#   ADVANCE           one live row per `advance_payments` receipt — the ledger
#                     twin of the custody slip. The receipt keeps its own job
#                     (numbered slip, who physically holds the cash, void
#                     columns); this row is what the books count. The partial
#                     unique index `ux_payments_live_advance` is what makes the
#                     1:1 a fact of the database.
#   BALANCE_PAYMENT   the amount still owed when the closed trip is paid, after
#   FULL_PAYMENT      the advance and this cab's coupon. FULL when no advance
#                     was ever held, BALANCE when one was — the distinction
#                     DB Schema Part 7 §4 names and the reports group on.
#                     Unique per booking while live (`ux_payments_live_balance`),
#                     which is the database answer to a double collection.
#
# Money back does not delete anything: `refunded_amount` accumulates on the row
# it came out of and the status follows it (PARTIAL_REFUNDED / REFUNDED), the
# same way an advance receipt accumulates its refund rather than being rewritten.
# The CHECK constraints added by migration 0088 refuse a status that
# contradicts the amount, so a row cannot claim to be refunded while holding
# every rupee.
#
# Why raw SQL here and not the ORM: this module's writes are read back in the
# same request by services that use `text()` (the settlement math, the invoice,
# the derive below), and the app's session runs with `autoflush=False` — an ORM
# insert would sit in the unit of work while those reads went to the database and
# seen nothing. The `Payment` model in `booking.models` exists for the reports
# that read the ledger, not for these writes.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.modules.booking.services.numbering import (
    next_payment_number,
)
from app.modules.booking.services.settlement_math import ZERO, money

# Money the platform is holding: a SUCCESS row, or a REFUNDED/PARTIAL_REFUNDED
# row minus whatever has come back out of it. PENDING and FAILED are not money —
# they are a gateway that never settled — and a voided row is an administrative
# reversal, so the derive's WHERE clause spells all three out rather than
# trusting a constant a caller could pass by.


async def _master_customer_id(db: AsyncSession, master_booking_id: int) -> int:
    row = (
        await db.execute(
            text("SELECT customer_id FROM master_bookings WHERE id = :m"),
            {"m": master_booking_id},
        )
    ).scalar_one_or_none()
    if row is None:
        raise BusinessException(
            f"Master booking {master_booking_id} does not exist, so this "
            "payment cannot be recorded against it."
        )
    return int(row)


async def _insert_payment(
    db: AsyncSession,
    *,
    master_booking_id: int,
    payment_type: str,
    payment_method: Optional[str],
    amount: Decimal,
    cab_booking_id: Optional[int] = None,
    advance_payment_id: Optional[int] = None,
    received_by: Optional[str] = None,
    collected_by_user_id: Optional[Any] = None,
    collected_by_role: Optional[str] = None,
    reference_note: Optional[str] = None,
) -> Dict[str, Any]:
    """One money-in row, numbered off the `PAYMENT` counter.

    `reference_note` rides on `gateway_reference`, the only free-text column the
    finance table has: the platform is not taking card payments yet, and when it
    does the gateway columns carry the gateway's own identifiers.
    """
    customer_id = await _master_customer_id(db, master_booking_id)
    payment_number = await next_payment_number(db)

    try:
        row = (
            (
                await db.execute(
                    text(
                        """
                        INSERT INTO payments
                            (uuid, payment_number, master_booking_id, customer_id,
                             cab_booking_id, advance_payment_id, payment_type,
                             payment_method, payment_status, amount, refunded_amount,
                             received_by, collected_by_user_id, collected_by_role,
                             gateway_reference, payment_datetime, created_at)
                        VALUES
                            (gen_random_uuid(), :number, :mb, :cust, :cab, :adv,
                             :type, :method, 'SUCCESS', :amount, 0,
                             :received_by, :uid, :role, :note, NOW(), NOW())
                        RETURNING id, payment_number, amount, payment_status
                        """
                    ),
                    {
                        "number": payment_number,
                        "mb": master_booking_id,
                        "cust": customer_id,
                        "cab": cab_booking_id,
                        "adv": advance_payment_id,
                        "type": payment_type,
                        "method": payment_method,
                        "amount": money(amount),
                        "received_by": received_by,
                        "uid": collected_by_user_id,
                        "role": (collected_by_role or "").upper() or None,
                        "note": reference_note,
                    },
                )
            )
            .mappings()
            .one()
        )
    except IntegrityError as exc:
        # Either partial unique index tripping is a statement about the money,
        # not a bug to paper over: this advance is already on the books, or this
        # trip has already been paid. The session is left failed on purpose —
        # `get_db` rolls it back on the way out, so the receipt insert in the
        # same transaction cannot survive without its ledger line.
        detail = getattr(exc, "orig", exc)
        raise BusinessException(
            f"This booking's money has already been recorded once "
            f"({type(detail).__name__}: {detail})."
        ) from exc

    return dict(row)


async def record_advance_payment(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_id: int,
    advance_payment_id: int,
    amount: Any,
    payment_mode: str,
    received_by: str,
    collected_by_user_id: Any,
    collected_by_role: Optional[str],
    receipt_number: str,
) -> Dict[str, Any]:
    """The ledger line for an advance the platform just took.

    Called by `collect_advance` with the receipt row it inserted, so the two
    cannot disagree about the amount, the mode or who holds the money.
    """
    result = await _insert_payment(
        db,
        master_booking_id=master_booking_id,
        payment_type="ADVANCE",
        payment_method=payment_mode,
        amount=amount,
        cab_booking_id=cab_booking_id,
        advance_payment_id=advance_payment_id,
        received_by=received_by,
        collected_by_user_id=collected_by_user_id,
        collected_by_role=collected_by_role,
        reference_note=f"Receipt {receipt_number}",
    )
    await derive_master_payment_status(db, master_booking_id)
    return result


async def record_final_payment(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_id: int,
    amount: Any,
    payment_mode: str,
    received_by: Optional[str],
    collected_by_user_id: Optional[Any] = None,
    collected_by_role: Optional[str] = None,
    reference_note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The ledger line for the balance collected at the end of a trip.

    Returns None when there is nothing to record: a coupon or an advance that
    covered the fare exactly means no money moved, and `ck_payments_amount_positive`
    (and common sense) refuse a ₹0.00 payment row. The caller's own books are
    still re-derived either way, because "the balance was zero" is itself a fact
    about the master's payment state.
    """
    collected = money(amount)
    if collected <= ZERO:
        return None

    # FULL_PAYMENT when the customer never paid anything in front of this,
    # BALANCE_PAYMENT when an advance is still being held against the fare.
    held_advance = (
        await db.execute(
            text(
                "SELECT 1 FROM payments "
                "WHERE master_booking_id = :mb AND cab_booking_id = :cb "
                "  AND payment_type = 'ADVANCE' AND voided_at IS NULL "
                "  AND payment_status IN ('SUCCESS','PARTIAL_REFUNDED','REFUNDED') "
                "LIMIT 1"
            ),
            {"mb": master_booking_id, "cb": cab_booking_id},
        )
    ).scalar_one_or_none()

    result = await _insert_payment(
        db,
        master_booking_id=master_booking_id,
        payment_type="BALANCE_PAYMENT" if held_advance else "FULL_PAYMENT",
        payment_method=payment_mode,
        amount=collected,
        cab_booking_id=cab_booking_id,
        received_by=received_by,
        collected_by_user_id=collected_by_user_id,
        collected_by_role=collected_by_role,
        reference_note=reference_note,
    )
    await derive_master_payment_status(db, master_booking_id)
    return result


async def void_advance_payment(
    db: AsyncSession,
    *,
    master_booking_id: int,
    advance_payment_id: int,
    reason: str,
) -> bool:
    """Void the ledger line of a receipt the admin voided.

    The row is kept and marked, exactly as the receipt itself is: money came in
    and was taken back out of the books administratively, and deleting the line
    would leave the customer's history shorter than the truth.
    """
    r = await db.execute(
        text(
            """
            UPDATE payments
               SET voided_at = NOW(),
                   void_reason = :reason
             WHERE advance_payment_id = :adv
               AND payment_type = 'ADVANCE'
               AND voided_at IS NULL
            """
        ),
        {
            "reason": (reason or "").strip()[:255] or "Advance receipt voided",
            "adv": advance_payment_id,
        },
    )
    await derive_master_payment_status(db, master_booking_id)
    return r.rowcount > 0


async def mark_payment_refunded(
    db: AsyncSession,
    *,
    payment_id: int,
    refund_amount: Any,
) -> Optional[str]:
    """Pull `refund_amount` out of one payment row and move its status with it.

    Returns the status the row now carries, or None when the row is not live — a
    voided or already-deleted payment cannot be refunded again, and silently
    returning SUCCESS would let a caller believe the money was tracked.
    """
    amount = money(refund_amount)
    if amount <= ZERO:
        return None
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, refunded_amount, refunded_amount + :amt AS new_refunded,
                       amount
                  FROM payments
                 WHERE id = :pid
                   AND voided_at IS NULL
                 FOR UPDATE
                """
                ),
                {"pid": payment_id, "amt": amount},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if money(row["new_refunded"]) > money(row["amount"]):
        raise BusinessException(
            f"Refund ₹{amount:.2f} takes payment {payment_id} past the "
            f"₹{money(row['amount']):.2f} it holds "
            f"(already refunded ₹{money(row['refunded_amount']):.2f})."
        )
    new_status = (
        "REFUNDED"
        if money(row["new_refunded"]) == money(row["amount"])
        else "PARTIAL_REFUNDED"
    )
    await db.execute(
        text(
            """
            UPDATE payments
               SET refunded_amount = :new_refunded,
                   payment_status = :status
             WHERE id = :pid
            """
        ),
        {
            "new_refunded": money(row["new_refunded"]),
            "status": new_status,
            "pid": payment_id,
        },
    )
    return new_status


async def live_advance_payment_id(
    db: AsyncSession, advance_payment_id: int
) -> Optional[int]:
    """The ledger row that carries a given receipt, if it is still live."""
    return (
        await db.execute(
            text(
                "SELECT id FROM payments "
                "WHERE advance_payment_id = :adv AND payment_type = 'ADVANCE' "
                "  AND voided_at IS NULL"
            ),
            {"adv": advance_payment_id},
        )
    ).scalar_one_or_none()


async def live_advance_payment_for_cab(
    db: AsyncSession, cab_booking_id: int
) -> Optional[int]:
    """The live ADVANCE ledger row for a cab booking, by booking rather than by
    receipt. A cancellation refunds the advance knowing only the booking, so the
    ledger lookup follows the same key; the partial unique index guarantees at
    most one live row, so this is the sole one."""
    return (
        await db.execute(
            text(
                "SELECT id FROM payments "
                "WHERE cab_booking_id = :cb AND payment_type = 'ADVANCE' "
                "  AND voided_at IS NULL "
                "  AND payment_status IN ('SUCCESS','PARTIAL_REFUNDED')"
            ),
            {"cb": cab_booking_id},
        )
    ).scalar_one_or_none()


async def record_refund_document(
    db: AsyncSession,
    *,
    payment_id: int,
    refund_amount: Any,
    reason: str,
    status: str = "COMPLETED",
    requested_at: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """The finance document for money that actually left the platform.

    `refunds` hangs off a payment (DB Schema Part 7 §7), which is the other
    reason the ledger has to exist: without it there is nowhere honest to point
    a refund at, and the admin customer report
    (`api_dashboard.py:4715`) joins through it to show what came back.

    Only money that has moved gets a row. An admin marking an advance
    refundable-again does not; a cancellation that credited the customer's
    wallet does, and is passed COMPLETED with the moment the credit landed.
    """
    amount = money(refund_amount)
    if amount <= ZERO:
        return None
    when = requested_at or datetime.now(timezone.utc)
    normalized_status = (status or "").strip().upper()
    # `completed_at` is decided here rather than with a `CASE :status = ...` in
    # the SQL: asyncpg deduces one type per positional parameter, and binding
    # `status` both into a varchar column and against a text literal makes it
    # refuse the statement ("inconsistent types … text versus character varying").
    completed_at = when if normalized_status == "COMPLETED" else None
    payment_number = (
        await db.execute(
            text("SELECT payment_number FROM payments WHERE id = :pid"),
            {"pid": payment_id},
        )
    ).scalar_one_or_none()
    if payment_number is None:
        return None
    # A payment can be refunded more than once (a partial refund, then the rest),
    # and `refunds.refund_number` is unique — one `RF-<payment>` per payment is
    # not enough. Number each instalment off how many already point at this
    # payment, so the document stays nameable and traceable to the line it
    # settles without colliding with its siblings.
    already = int(
        (
            await db.execute(
                text("SELECT COUNT(*) FROM refunds WHERE payment_id = :pid"),
                {"pid": payment_id},
            )
        ).scalar_one()
    )
    r = await db.execute(
        text(
            """
            INSERT INTO refunds
                (uuid, payment_id, refund_number, refund_amount, refund_reason,
                 refund_status, requested_at, completed_at)
            VALUES
                (gen_random_uuid(), :pid, :number, :amount, :reason, :status,
                 :requested_at, CAST(:completed_at AS timestamptz))
            RETURNING id, refund_number, refund_amount, refund_status
            """
        ),
        {
            "pid": payment_id,
            "number": f"RF-{payment_number}-{already + 1}",
            "amount": amount,
            "reason": (reason or "").strip() or "Refund",
            "status": normalized_status,
            "requested_at": when,
            "completed_at": completed_at,
        },
    )
    return dict(r.mappings().one())


async def derive_master_payment_status(
    db: AsyncSession, master_booking_id: int
) -> Optional[Dict[str, Any]]:
    """Set `payment_status` and `total_paid_amount` from the ledger.

    The whole point: the master's payment state is arithmetic over the payments
    the customer actually made, so a booking is 'PAID' when the money says so —
    not because some endpoint assumed it while recording a balance.

      nothing held                  → PENDING
      held < what the fare costs    → PARTIAL
      held >= what the fare costs   → PAID

    `total_amount` is deliberately not written here. It is the value of the
    booking and belongs to the writers that set it today (the close, the
    amendment, the hotel and tour rollups); this function answers a different
    question — how much of it has arrived — and overwriting the first to compute
    the second is how two endpoints came to disagree about one number.

    A master that also carries a hotel or a tour line is left alone entirely:
    `rollup_hotel_totals_into_master` / `rollup_tour_totals_into_master` already
    own these two columns for mixed masters and compute them from their own
    service sums. Two writers deriving one column from different sums is the
    drift this round is removing, not something to reintroduce.
    """
    services = (
        (
            await db.execute(
                text(
                    "SELECT service_type FROM booking_services "
                    "WHERE master_booking_id = :m"
                ),
                {"m": master_booking_id},
            )
        )
        .scalars()
        .all()
    )
    if set(services) & {"HOTEL", "TOUR"}:
        return None

    held = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - refunded_amount), 0) AS paid
                  FROM payments
                 WHERE master_booking_id = :m
                   AND payment_status IN ('SUCCESS','PARTIAL_REFUNDED','REFUNDED')
                   AND voided_at IS NULL
                """
            ),
            {"m": master_booking_id},
        )
    ).scalar_one()
    paid = money(held)

    # What the live cab lines on this master cost: the closed fare with GST on
    # top when it is a tax invoice and this cab's coupon off it, or the quoted
    # figure while the trip is still running. A cancelled line owes nothing, so
    # the money a cancelled booking still holds does not keep the master stuck in
    # PARTIAL — the ledger rows and the timeline carry that story instead.
    payable = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN COALESCE(cb.final_amount, 0) > 0 THEN
                            cb.final_amount
                            + (CASE WHEN cb.is_tax_invoice
                                    THEN COALESCE(cb.gst_amount, 0) ELSE 0 END)
                            - COALESCE(cb.coupon_discount, 0)
                        ELSE COALESCE(cb.estimated_amount, 0)
                             - COALESCE(cb.coupon_discount, 0)
                    END), 0)
                  FROM cab_bookings cb
                 WHERE cb.master_booking_id = :m
                   AND cb.booking_status <> 'CANCELLED'
                """
            ),
            {"m": master_booking_id},
        )
    ).scalar_one()
    owed = money(payable)

    if paid <= ZERO:
        status = "PENDING"
    elif paid < owed:
        status = "PARTIAL"
    else:
        status = "PAID"

    await db.execute(
        text(
            """
            UPDATE master_bookings
               SET payment_status = :status,
                   total_paid_amount = :paid,
                   updated_at = NOW()
             WHERE id = :m
            """
        ),
        {"status": status, "paid": paid, "m": master_booking_id},
    )
    return {"payment_status": status, "total_paid_amount": paid, "payable": owed}
