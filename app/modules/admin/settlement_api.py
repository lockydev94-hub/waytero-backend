# ============================================================
# WAYTERO — ADMIN SETTLEMENT API
# File: app/modules/admin/settlement_api.py
# Prefix: /admin/settlements
#
# Purpose:
#   Separate settlement page logic — admin settles bookings
#   AFTER payment has been collected and invoice generated
#   on the Trip Assistance page.
#
# Settlement is a NET POSITION, not a per-mode branch (BRD Part 6 §147).
#
#   Money can be split across both sides of the ledger — the platform takes an
#   advance, the driver takes the balance in cash — so payment_mode alone cannot
#   say which way the wallet should move. Custody can:
#
#     partner_held = advance  (when the partner or driver received it)
#                  + balance  (when taken in cash/UPI by the driver or partner)
#
#     net = partner_payout − partner_held − coupon_discount
#
#     net > 0 → credit the partner wallet
#     net < 0 → debit  the partner wallet (blocked if the balance won't cover it)
#     net = 0 → no movement
#
#   Plain cash still nets to −commission and plain online still nets to the full
#   payout, so the familiar cases are unchanged. See _compute_position.
#
# Coupon record:
#   If a coupon was used, admin owes the discount amount to the partner
#   (coupon discount should not reduce partner's earnings).
#   A pending_coupon_disbursements record is written.
#   Separate "Coupon Disbursements" page shows these for admin to approve.
#
# Doc Ref: BRD Part 6 §129, §137-143, §147
# ============================================================

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import math

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.modules.booking import services as advance_service
from app.modules.booking.models import (
    MasterBooking,
    CabBooking,
    BookingTimeline,
)
from app.modules.customer.models import Customer
from app.modules.partner.models import Partner
from app.modules.auth.models.user import User
from app.modules.admin.coupon_models import CouponUsage

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


async def _log_timeline(db, master_booking_id: int, event_type: str, description: str):
    entry = BookingTimeline(
        master_booking_id=master_booking_id,
        event_type=event_type,
        event_description=description,
        event_timestamp=datetime.now(timezone.utc),
    )
    db.add(entry)


async def _lock_settlement_target(
    db: AsyncSession, table: str, column: str, value: str
) -> None:
    """Acquire a FOR UPDATE lock on a settlement target row (S7 concurrency fix).

    ``table`` and ``column`` are hardcoded call-site constants (never user
    input), so the f-string interpolation is safe. A second concurrent settle
    on the same booking blocks here until the first transaction commits, then
    re-reads the status — which the first request flipped to SETTLED — and is
    rejected, preventing double wallet credits/debits.
    """
    await db.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :value FOR UPDATE"),
        {"value": value},
    )


async def _get_cab_booking(db: AsyncSession, booking_number: str):
    from sqlalchemy.orm import selectinload

    cb = (
        await db.execute(
            select(CabBooking).where(
                CabBooking.booking_number == booking_number.strip().upper()
            )
        )
    ).scalar_one_or_none()
    if not cb:
        raise HTTPException(404, f"Cab booking '{booking_number}' not found.")

    mb = (
        await db.execute(
            select(MasterBooking)
            .options(
                selectinload(MasterBooking.cab_bookings).selectinload(
                    CabBooking.assignments
                ),
                selectinload(MasterBooking.timelines),
            )
            .where(MasterBooking.id == cb.master_booking_id)
        )
    ).scalar_one_or_none()
    if not mb:
        raise HTTPException(404, "Master booking not found.")
    return mb, cb


async def _get_partner_wallet(db: AsyncSession, partner_id: int):
    """Fetch partner wallet with row-level lock for update."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, available_balance, wallet_status FROM wallets WHERE partner_id = :pid FOR UPDATE"
                ),
                {"pid": partner_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return row


async def _credit_partner_wallet(
    db: AsyncSession,
    wallet_id: int,
    amount: float,
    current_balance: float,
    reference: str,
    narration: str,
    reference_type: str = "SETTLEMENT",
):
    new_bal = round(current_balance + amount, 2)
    await db.execute(
        text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
        {"bal": new_bal, "wid": wallet_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO wallet_ledger
                (wallet_id, transaction_reference, reference_type,
                 debit_amount, credit_amount, balance_after, narration, created_at)
            VALUES (:wid, :ref, :rtype, 0, :credit, :bal_after, :narration, NOW())
        """
        ),
        {
            "wid": wallet_id,
            "ref": reference,
            "credit": amount,
            "rtype": reference_type,
            "bal_after": new_bal,
            "narration": narration,
        },
    )
    return new_bal


async def _debit_partner_wallet(
    db: AsyncSession,
    wallet_id: int,
    amount: float,
    current_balance: float,
    reference: str,
    narration: str,
    reference_type: str = "COMMISSION_DEBIT",
):
    new_bal = round(current_balance - amount, 2)
    await db.execute(
        text("UPDATE wallets SET available_balance = :bal WHERE id = :wid"),
        {"bal": new_bal, "wid": wallet_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO wallet_ledger
                (wallet_id, transaction_reference, reference_type,
                 debit_amount, credit_amount, balance_after, narration, created_at)
            VALUES (:wid, :ref, :rtype, :debit, 0, :bal_after, :narration, NOW())
        """
        ),
        {
            "wid": wallet_id,
            "ref": reference,
            "debit": amount,
            "rtype": reference_type,
            "bal_after": new_bal,
            "narration": narration,
        },
    )
    return new_bal


# ════════════════════════════════════════════════════════════════
# NET POSITION
#
# Settlement used to branch on payment_mode alone, which cannot express the
# common case where the platform takes the advance and the driver takes the
# balance — the money ends up split across both sides of the ledger.
#
# So we stop asking "how was it paid" and ask "who is holding it". Whatever the
# partner side physically holds is netted against what they have earned:
#
#   partner_held = advance  (if the partner or driver received it)
#                + balance  (if it was taken in cash/UPI by the driver or partner)
#
#   net = partner_payout − partner_held − coupon_discount
#
#   net > 0  → the partner is owed money      → credit the wallet
#   net < 0  → the partner is holding ours    → debit the wallet
#   net == 0 → everything already nets out    → no wallet movement
#
# Coupon is subtracted because it is platform-funded and reimbursed separately
# through pending_coupon_disbursements; leaving it in paid the partner twice.
#
# On a GST booking the cash the partner holds includes the GST the customer
# paid, while partner_payout does not — so GST correctly lands on the debit
# side, i.e. the partner remits it to the platform.
# ════════════════════════════════════════════════════════════════

# Modes where the money physically changes hands at the vehicle.
_HAND_TO_HAND_MODES = {"CASH", "UPI"}
_PARTNER_SIDE_COLLECTORS = {"DRIVER", "PARTNER"}


def _compute_position(
    *,
    final: float,
    coupon_discount: float,
    partner_payout: float,
    advance: Optional[dict],
    payment_mode: Optional[str],
    payment_collected_by: Optional[str],
    gst_amount: float = 0.0,
    is_tax_invoice: bool = False,
) -> dict:
    """The full settlement arithmetic for one booking. Pure — no DB, no writes."""
    advance_amount = float(advance["amount"]) if advance else 0.0
    advance_receiver = advance["received_by"] if advance else None

    balance_due = advance_service.compute_balance_due(
        final_amount=final,
        coupon_discount=coupon_discount,
        advance_paid=advance_amount,
        gst_amount=gst_amount,
        is_tax_invoice=is_tax_invoice,
    )

    advance_held = (
        advance_amount
        if advance_receiver in advance_service.PARTNER_SIDE_RECEIVERS
        else 0.0
    )
    balance_held = (
        balance_due
        if (payment_mode or "").upper() in _HAND_TO_HAND_MODES
        and (payment_collected_by or "").upper() in _PARTNER_SIDE_COLLECTORS
        else 0.0
    )

    partner_held = round(advance_held + balance_held, 2)
    net = round(partner_payout - partner_held - coupon_discount, 2)

    return {
        "advance_paid": advance_amount,
        "advance_received_by": advance_receiver,
        "advance_held_by_partner": advance_held,
        "balance_due": balance_due,
        "balance_held_by_partner": balance_held,
        "partner_held": partner_held,
        "coupon_discount": coupon_discount,
        "partner_payout": partner_payout,
        "net_settlement": net,
        "wallet_direction": "CREDIT" if net > 0 else ("DEBIT" if net < 0 else "NONE"),
    }


async def _build_settlement_detail(
    db: AsyncSession, mb: MasterBooking, cb: CabBooking
) -> dict:
    """Build enriched settlement detail for the 3-step modal."""
    # Customer
    cust = (
        await db.execute(select(Customer).where(Customer.id == mb.customer_id))
    ).scalar_one_or_none()
    cust_user = None
    if cust:
        cust_user = (
            await db.execute(select(User).where(User.id == cust.user_id))
        ).scalar_one_or_none()

    # Partner
    partner_id = None
    partner_name = partner_mobile = None
    partner_wallet_balance = None
    if cb.assignments:
        # Use the active assignment (closed_at IS NULL). After a partner
        # handover, the original assignment is closed and the new one is
        # active — this naturally picks up the new partner who earns the
        # full payout, while the closed row is preserved for audit.
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        partner_id = latest.partner_id
        p = (
            await db.execute(select(Partner).where(Partner.id == partner_id))
        ).scalar_one_or_none()
        if p:
            partner_name = p.business_name or f"Partner #{p.id}"
            partner_mobile = p.mobile
        # Fetch wallet balance
        w = (
            (
                await db.execute(
                    text(
                        "SELECT available_balance FROM wallets WHERE partner_id = :pid"
                    ),
                    {"pid": partner_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if w:
            partner_wallet_balance = float(w["available_balance"] or 0)

    # Coupon discount
    coupon_q = await db.execute(
        select(func.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)

    final = float(cb.final_amount or 0)
    commission = float(cb.platform_commission or 0)
    payout = float(cb.partner_payout or 0)

    advance = await advance_service.get_active_advance(db, cb.id)
    position = _compute_position(
        final=final,
        coupon_discount=coupon_discount,
        partner_payout=payout,
        advance=advance,
        payment_mode=cb.payment_mode,
        payment_collected_by=cb.payment_collected_by,
        gst_amount=float(getattr(cb, "gst_amount", 0) or 0),
        is_tax_invoice=bool(getattr(cb, "is_tax_invoice", False)),
    )
    advance_paid = position["advance_paid"]
    balance_due = position["balance_due"]

    # Check pending coupon disbursements for this booking
    cpd = (
        (
            await db.execute(
                text(
                    "SELECT id, coupon_discount_amount, status FROM pending_coupon_disbursements WHERE cab_booking_id = :bid"
                ),
                {"bid": cb.id},
            )
        )
        .mappings()
        .one_or_none()
    )
    coupon_disbursement = None
    if cpd:
        coupon_disbursement = {
            "id": cpd["id"],
            "amount": float(cpd["coupon_discount_amount"] or 0),
            "status": cpd["status"],
        }

    return {
        # Step 1: Booking details
        "booking_number": mb.booking_number,
        "cab_booking_number": cb.booking_number,
        "cab_status": cb.booking_status,
        "customer_name": cust.full_name if cust else None,
        "customer_mobile": cust_user.mobile_number if cust_user else None,
        "pickup_location": cb.pickup_location,
        "drop_location": cb.drop_location,
        "actual_distance": float(cb.actual_distance) if cb.actual_distance else None,
        "trip_type": cb.trip_type,
        "invoice_number": cb.invoice_number,
        "invoice_url": cb.invoice_url,
        # Step 2: Payment details
        "final_amount": final,
        "coupon_discount": coupon_discount,
        "advance_paid": advance_paid,
        "advance": advance_service.advance_summary(advance),
        "balance_due": balance_due,
        "payment_mode": cb.payment_mode,
        "payment_collected_by": cb.payment_collected_by,
        "cash_pending_at": cb.cash_pending_at or "NONE",
        # Step 3: Settlement amounts — the full net-position arithmetic, so the
        # modal can show the reasoning rather than a bare number.
        "platform_commission": commission,
        "partner_payout": payout,
        "position": position,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "partner_mobile": partner_mobile,
        "partner_wallet_balance": partner_wallet_balance,
        "coupon_disbursement": coupon_disbursement,
        "is_settled": cb.booking_status == "SETTLED",
    }


# ════════════════════════════════════════════════════════════════
# MIGRATION CHECK: ensure pending_coupon_disbursements table exists
# Created inline if not present (no migration needed — idempotent DDL)
# ════════════════════════════════════════════════════════════════


async def _ensure_coupon_disbursements_table(db: AsyncSession):
    # Table + indexes are created by Alembic migration 0023_customer_wallet.
    # This function is intentionally a no-op; kept so call-sites need no change.
    pass


# ════════════════════════════════════════════════════════════════
# LIST SETTLEMENTS — GET /admin/settlements/list
# Shows bookings ready for settlement (COMPLETED with payment recorded)
# and already settled bookings (SETTLED)
# ════════════════════════════════════════════════════════════════


@router.get("/list", tags=["Settlements"])
async def list_settlements(
    status_filter: str = Query("PENDING", description="PENDING | SETTLED | ALL"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    """
    List bookings for settlement:
    PENDING — COMPLETED status + payment recorded (payment_mode not null)
    SETTLED — SETTLED status
    ALL — both
    """
    from sqlalchemy.orm import selectinload

    if status_filter == "PENDING":
        status_cond = CabBooking.booking_status.in_(["COMPLETED", "SETTLEMENT_PENDING"])
        mode_cond = CabBooking.payment_mode.is_not(None)
    elif status_filter == "SETTLED":
        status_cond = CabBooking.booking_status == "SETTLED"
        mode_cond = CabBooking.payment_mode.is_not(None)
    else:
        status_cond = CabBooking.booking_status.in_(
            ["COMPLETED", "SETTLEMENT_PENDING", "SETTLED"]
        )
        mode_cond = CabBooking.payment_mode.is_not(None)

    total = (
        await db.execute(
            select(func.count(CabBooking.id)).where(status_cond, mode_cond)
        )
    ).scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                select(CabBooking)
                .options(selectinload(CabBooking.assignments))
                .where(status_cond, mode_cond)
                .order_by(CabBooking.id.desc())
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for cb in rows:
        mb = (
            await db.execute(
                select(MasterBooking)
                .options(
                    selectinload(MasterBooking.cab_bookings).selectinload(
                        CabBooking.assignments
                    ),
                    selectinload(MasterBooking.timelines),
                )
                .where(MasterBooking.id == cb.master_booking_id)
            )
        ).scalar_one_or_none()
        if mb:
            items.append(await _build_settlement_detail(db, mb, cb))

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "status_filter": status_filter,
        "items": items,
    }


# ════════════════════════════════════════════════════════════════
# BOOKING DETAIL — GET /admin/settlements/booking?booking_number=
# Fetch 3-step detail for a specific booking
# ════════════════════════════════════════════════════════════════


@router.get("/booking", tags=["Settlements"])
async def get_settlement_booking(
    booking_number: str = Query(..., description="Cab booking number"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    mb, cb = await _get_cab_booking(db, booking_number)
    return await _build_settlement_detail(db, mb, cb)


# ════════════════════════════════════════════════════════════════
# SETTLE BOOKING — POST /admin/settlements/settle
#
# Works out the partner's net position (see _compute_position) and moves the
# wallet once, in whichever direction the arithmetic points. Worked examples:
#
#   Cash to driver, no advance:
#     held = fare, net = (fare − commission) − fare = −commission  → debit commission
#
#   Online, no advance:
#     held = 0,    net = payout                                    → credit payout
#
#   Advance to ADMIN, balance in cash to the driver:
#     held = balance, net = advance − commission
#     → the platform is holding the advance, so when it exceeds the commission
#       the partner is credited the difference instead of being asked to top up.
#
#   Advance to PARTNER, balance online:
#     held = advance, net = payout − advance
#
# If coupon discount > 0 a pending_coupon_disbursement record is written, and
# the coupon is subtracted from net so it is not paid through both channels.
#
# ════════════════════════════════════════════════════════════════


class SettleRequest(BaseModel):
    booking_number: str


@router.post("/settle", tags=["Settlements"])
async def settle_booking(
    payload: SettleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
):
    """
    Settle a booking after payment collection.
    See full logic above.
    """
    await _ensure_coupon_disbursements_table(db)
    # S7: serialize concurrent settlements on the same booking — the second
    # request blocks here until the first commits, then sees SETTLED and is
    # rejected (guarding against double wallet credit).
    await _lock_settlement_target(
        db, "cab_bookings", "booking_number", payload.booking_number.strip().upper()
    )
    mb, cb = await _get_cab_booking(db, payload.booking_number)

    # Guard: must be COMPLETED or SETTLEMENT_PENDING with payment recorded
    if cb.booking_status not in ("COMPLETED", "SETTLEMENT_PENDING"):
        raise HTTPException(
            400,
            f"Cannot settle: booking is '{cb.booking_status}'. "
            "Only COMPLETED or SETTLEMENT_PENDING bookings can be settled.",
        )
    if not cb.payment_mode:
        raise HTTPException(400, "Record payment before settling.")
    if not cb.invoice_number:
        raise HTTPException(400, "Generate invoice before settling.")

    mode = cb.payment_mode.upper()
    final = float(cb.final_amount or 0)
    commission = float(cb.platform_commission or 0)
    payout = float(cb.partner_payout or 0)

    # Get partner — prefer the active assignment row so a handover doesn't
    # re-credit the original partner.
    partner_id = None
    if cb.assignments:
        active = next(
            (a for a in cb.assignments if a.closed_at is None),
            None,
        )
        latest = (
            active
            or sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
        )
        partner_id = latest.partner_id
    if not partner_id:
        raise HTTPException(400, "No partner assigned to this booking. Cannot settle.")

    # Coupon discount
    coupon_q = await db.execute(
        select(func.sum(CouponUsage.discount_applied)).where(
            CouponUsage.master_booking_id == mb.id
        )
    )
    coupon_discount = float(coupon_q.scalar() or 0)

    wallet_row = await _get_partner_wallet(db, partner_id)
    if not wallet_row:
        raise HTTPException(
            400, "Partner does not have a wallet. Cannot process settlement."
        )
    if wallet_row["wallet_status"] != "ACTIVE":
        raise HTTPException(
            400,
            f"Partner wallet is '{wallet_row['wallet_status']}'. Cannot process settlement.",
        )

    partner_wallet_balance = float(wallet_row["available_balance"] or 0)
    wallet_id = wallet_row["id"]

    # ── Net position ──────────────────────────────────────────────────────────
    # Direction comes from who is holding the money, not from payment_mode. See
    # the comment above _compute_position for why.
    advance = await advance_service.get_active_advance(db, cb.id)
    position = _compute_position(
        final=final,
        coupon_discount=coupon_discount,
        partner_payout=payout,
        advance=advance,
        payment_mode=cb.payment_mode,
        payment_collected_by=cb.payment_collected_by,
        gst_amount=float(getattr(cb, "gst_amount", 0) or 0),
        is_tax_invoice=bool(getattr(cb, "is_tax_invoice", False)),
    )
    partner_held = position["partner_held"]
    net = position["net_settlement"]

    # The arithmetic, spelled out — this is the trail finance reads back.
    workings = (
        f"Fare ₹{final:.2f} | Commission ₹{commission:.2f} | Payout ₹{payout:.2f} | "
        f"Coupon ₹{coupon_discount:.2f} | "
        f"Advance ₹{position['advance_paid']:.2f}"
        + (f" received by {position['advance_received_by']}" if advance else " (none)")
        + f" | Balance ₹{position['balance_due']:.2f} via {mode}"
        + (
            f" collected by {cb.payment_collected_by}"
            if cb.payment_collected_by
            else ""
        )
        + f" | Partner side holds ₹{partner_held:.2f} | "
        f"Net = {payout:.2f} − {partner_held:.2f} − {coupon_discount:.2f} = ₹{net:.2f}"
    )

    new_bal = partner_wallet_balance
    if net > 0:
        new_bal = await _credit_partner_wallet(
            db,
            wallet_id,
            net,
            partner_wallet_balance,
            reference=cb.booking_number,
            narration=f"Settlement credit — {cb.booking_number} | {workings}",
            reference_type="SETTLEMENT",
        )
        settlement_note = (
            f"₹{net:.2f} credited to partner wallet. "
            f"Partner wallet balance: ₹{new_bal:.2f}. "
        )
    elif net < 0:
        owed = abs(net)
        if partner_wallet_balance < owed:
            raise HTTPException(
                400,
                f"Insufficient partner wallet balance to settle. "
                f"Required: ₹{owed:.2f} | "
                f"Partner wallet: ₹{partner_wallet_balance:.2f} | "
                f"Shortfall: ₹{owed - partner_wallet_balance:.2f}. "
                "Partner must top up their wallet before settlement.",
            )
        new_bal = await _debit_partner_wallet(
            db,
            wallet_id,
            owed,
            partner_wallet_balance,
            reference=cb.booking_number,
            narration=f"Settlement debit — {cb.booking_number} | {workings}",
            reference_type="SETTLEMENT_DEBIT",
        )
        settlement_note = (
            f"₹{owed:.2f} debited from partner wallet — the partner side is "
            f"holding ₹{partner_held:.2f} against a payout of ₹{payout:.2f}. "
            f"Partner wallet balance: ₹{new_bal:.2f}. "
        )
    else:
        settlement_note = (
            "No wallet movement — what the partner side holds exactly matches "
            "what they are owed. "
        )

    if position["advance_held_by_partner"] > 0:
        settlement_note += (
            f"Advance ₹{position['advance_held_by_partner']:.2f} was received by "
            f"{position['advance_received_by']}. "
        )
    elif position["advance_paid"] > 0:
        settlement_note += (
            f"Advance ₹{position['advance_paid']:.2f} was received by the platform. "
        )
    if (
        position["balance_held_by_partner"] > 0
        and (cb.cash_pending_at or "NONE") == "DRIVER"
    ):
        settlement_note += (
            f"Cash ₹{position['balance_held_by_partner']:.2f} still pending at DRIVER "
            "— partner must collect it via the Partner Portal."
        )

    # ── Write coupon disbursement record ─────────────────────────────────────
    coupon_disbursement_id = None
    if coupon_discount > 0:
        # Check if already exists
        existing_cpd = (
            await db.execute(
                text(
                    "SELECT id FROM pending_coupon_disbursements WHERE cab_booking_id = :bid"
                ),
                {"bid": cb.id},
            )
        ).scalar_one_or_none()
        if not existing_cpd:
            res = await db.execute(
                text(
                    """
                    INSERT INTO pending_coupon_disbursements
                        (cab_booking_id, master_booking_id, partner_id, customer_id,
                         coupon_discount_amount, booking_number, invoice_number,
                         status, created_at)
                    VALUES
                        (:bid, :mbid, :pid, :cid, :amt, :bn, :inv, 'PENDING', NOW())
                    RETURNING id
                """
                ),
                {
                    "bid": cb.id,
                    "mbid": mb.id,
                    "pid": partner_id,
                    "cid": mb.customer_id,
                    "amt": coupon_discount,
                    "bn": cb.booking_number,
                    "inv": cb.invoice_number,
                },
            )
            coupon_disbursement_id = res.scalar_one()
            settlement_note += (
                f" Coupon disbursement of ₹{coupon_discount:.2f} recorded "
                f"(pending admin approval on Coupon Disbursements page)."
            )

    # ── TDS Deduction (Section 194C — COMPANY partners only) ─────────────────
    # TDS @ 1% deducted from partner payout when TDS_ENABLED = true.
    # Individual partners: not liable. Company partners: TDS certificate via Form 16A.
    tds_deducted = 0.0
    tds_record_id = None
    partner_obj = (
        await db.execute(select(Partner).where(Partner.id == partner_id))
    ).scalar_one_or_none()
    if partner_obj and partner_obj.partner_type == "COMPANY":
        tds_cfg_rows = (
            (
                await db.execute(
                    text(
                        "SELECT config_key, config_value FROM system_configurations WHERE config_key IN ('TDS_ENABLED', 'TDS_RATE')"
                    )
                )
            )
            .mappings()
            .all()
        )
        tds_cfg = {r["config_key"]: r["config_value"] for r in tds_cfg_rows}
        tds_on = tds_cfg.get("TDS_ENABLED", "true").lower() == "true"
        tds_rate_pct = float(tds_cfg.get("TDS_RATE", "1") or "1")
        if tds_on and payout > 0:
            from datetime import date as _date

            tds_deducted = round(payout * tds_rate_pct / 100, 2)
            from app.modules.partner.models import PartnerGSTDetails as _PGST

            gst_row = (
                await db.execute(select(_PGST).where(_PGST.partner_id == partner_id))
            ).scalar_one_or_none()
            pan_number = gst_row.pan_number if gst_row else None
            ref_date = _date.today()
            _fy_y = ref_date.year
            _fy_str = (
                f"{_fy_y-1}-{str(_fy_y)[2:]}"
                if ref_date.month < 4
                else f"{_fy_y}-{str(_fy_y+1)[2:]}"
            )
            _m = ref_date.month
            _q = (
                "Q1"
                if _m in (4, 5, 6)
                else "Q2" if _m in (7, 8, 9) else "Q3" if _m in (10, 11, 12) else "Q4"
            )
            _month_key = f"{ref_date.year}-{ref_date.month:02d}"
            try:
                res_tds = await db.execute(
                    text(
                        """
                    INSERT INTO tds_deduction_records
                        (partner_id, cab_booking_id, month, gross_payout, tds_rate,
                         tds_amount, net_payout, pan_number, deducted_at, financial_year, quarter)
                    VALUES
                        (:pid, :cbid, :mo, :gpayout, :rate, :tds, :netpayout, :pan, NOW(), :fy, :q)
                    ON CONFLICT (cab_booking_id) DO UPDATE SET
                        tds_amount = EXCLUDED.tds_amount, net_payout = EXCLUDED.net_payout,
                        deducted_at = NOW()
                    RETURNING id
                """
                    ),
                    {
                        "pid": partner_id,
                        "cbid": cb.id,
                        "mo": _month_key,
                        "gpayout": payout,
                        "rate": tds_rate_pct,
                        "tds": tds_deducted,
                        "netpayout": payout - tds_deducted,
                        "pan": pan_number,
                        "fy": _fy_str,
                        "q": _q,
                    },
                )
                tds_record_id = res_tds.scalar_one()
                # Deduct TDS from partner wallet (TDS withheld by platform)
                if tds_deducted > 0:
                    if new_bal < tds_deducted:
                        raise HTTPException(
                            400,
                            f"Insufficient partner wallet balance to deduct TDS "
                            f"₹{tds_deducted:.2f}. Partner wallet: ₹{new_bal:.2f}. "
                            "Partner must top up their wallet.",
                        )
                    new_bal_after_tds = await _debit_partner_wallet(
                        db,
                        wallet_id,
                        tds_deducted,
                        new_bal,
                        reference=cb.booking_number,
                        narration=f"TDS @ {tds_rate_pct}% (Sec 194C) deducted — {cb.booking_number} | FY {_fy_str} {_q}",
                        reference_type="TDS_DEDUCTION",
                    )
                    new_bal = new_bal_after_tds
                    settlement_note += f" TDS ₹{tds_deducted:.2f} ({tds_rate_pct}%) withheld under Sec 194C."
            except Exception as _e:
                import logging as _log

                _log.getLogger(__name__).warning(
                    f"TDS record insert failed (non-fatal): {_e}"
                )

    # ── Mark booking settled ──────────────────────────────────────────────────
    cb.booking_status = "SETTLED"
    mb.booking_status = "CLOSED"

    await _log_timeline(
        db,
        mb.id,
        "BOOKING_SETTLED_BY_ADMIN",
        f"Admin settled {cb.booking_number} via Settlements page. "
        f"{workings}. " + settlement_note,
    )
    await db.commit()

    # ── Email: settlement summary to the partner ──
    try:
        from app.infrastructure.email import send_event_email

        partner_email = (partner_obj.email or "").strip() if partner_obj else ""
        if partner_email:
            partner_name = (
                partner_obj.business_name or partner_obj.owner_name or "partner"
            )
            await send_event_email(
                db,
                event_type="partner_settlement",
                to_email=partner_email,
                to_name=partner_name,
                context={
                    "name": partner_name,
                    "message": (
                        f"Settlement completed for booking {cb.booking_number}. "
                        "Your wallet balance has been updated."
                    ),
                    "details": [
                        ("Booking", cb.booking_number),
                        ("Trip fare", f"₹{final:,.2f}"),
                        ("Platform commission", f"₹{commission:,.2f}"),
                        ("Partner payout", f"₹{payout:,.2f}"),
                        ("Wallet balance after", f"₹{new_bal:,.2f}"),
                    ],
                    **(
                        {"amount": net, "amount_label": "Net settlement"}
                        if net > 0
                        else {}
                    ),
                },
                related_type="CAB_BOOKING",
                related_id=cb.id,
            )
    except Exception:  # pragma: no cover — email must never break settlement
        pass

    return {
        "success": True,
        "message": "Booking settled successfully.",
        "booking_number": cb.booking_number,
        "cab_status": "SETTLED",
        "booking_status": "CLOSED",
        "payment_mode": mode,
        "final_amount": final,
        "platform_commission": commission,
        "partner_payout": payout,
        "coupon_discount": coupon_discount,
        "position": position,
        "partner_wallet_balance": new_bal,
        "coupon_disbursement_id": coupon_disbursement_id,
        "tds_deducted": tds_deducted,
        "tds_record_id": tds_record_id,
        "settlement_note": settlement_note,
    }


# ════════════════════════════════════════════════════════════════
# HOTEL SETTLEMENT
#
# Same net-position principle as cab (see _compute_position), adapted to how a
# stay records money:
#
#   * Every payment — deposit, top-up, or the final balance — is a row in
#     hotel_advance_payments carrying its own custody (received_by). There is no
#     separate "balance in cash" branch as on the cab side: partner_held is just
#     the sum of the PARTNER-custody rows, net of refunds.
#
#   * The reservation freezes taxable_amount NET of the coupon (billing.build_bill
#     subtracts it before tax). Cab charges commission on the pre-coupon fare and
#     reimburses the coupon separately, so to stay consistent we add the coupon
#     back before charging commission:
#
#         gross_taxable   = taxable_amount + coupon_discount
#         commission      = compute_commission(gross_taxable, config)
#         partner_payout  = gross_taxable − commission
#
#   * net = partner_payout − partner_held − coupon_discount
#         > 0  credit wallet | < 0  debit wallet (blocked if short) | 0  no move
#
#   Commission is resolved live (resolve_hotel_commission) because the admin
#   check-out flow never snapshots it onto the reservation — platform_commission
#   and partner_payout stay 0 until we freeze them here. GST rides on the
#   partner's held cash exactly as on the cab side, so a partner holding a
#   GST-inclusive payment remits the tax back through the debit.
#
# Doc Ref: BRD Part 6 §129, §137-143, §147; Part 4 §57-92 (hotel lifecycle)
# ════════════════════════════════════════════════════════════════

_HOTEL_PARTNER_SIDE = {"PARTNER"}


async def _get_hotel_reservation(db: AsyncSession, reservation_number: str):
    """Resolve a reservation number to (master booking, reservation, hotel)."""
    from app.modules.hotel.models import HotelReservation, Hotel

    hr = (
        await db.execute(
            select(HotelReservation).where(
                HotelReservation.reservation_number
                == reservation_number.strip().upper()
            )
        )
    ).scalar_one_or_none()
    if not hr:
        raise HTTPException(404, f"Hotel reservation '{reservation_number}' not found.")
    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == hr.master_booking_id)
        )
    ).scalar_one_or_none()
    if not mb:
        raise HTTPException(404, "Master booking not found.")
    hotel = (
        await db.execute(select(Hotel).where(Hotel.id == hr.hotel_id))
    ).scalar_one_or_none()
    if not hotel:
        raise HTTPException(404, "Hotel not found for this reservation.")
    return mb, hr, hotel


def _hotel_partner_held(advances) -> float:
    """Cash the property is holding — the PARTNER-custody payment rows, net of refunds."""
    held = Decimal("0")
    for a in advances:
        if (a.received_by or "").upper() in _HOTEL_PARTNER_SIDE:
            held += Decimal(str(a.amount)) - Decimal(str(a.refunded_amount or 0))
    return round(float(held), 2)


async def _resolve_hotel_commission_amounts(db: AsyncSession, hr, hotel):
    """Commission + payout on the PRE-coupon taxable base, resolved live.

    Returns (commission, partner_payout, snapshot_dict, gross_taxable).
    """
    from app.modules.hotel.services import resolve_hotel_commission
    from app.modules.hotel.services.pricing import (
        compute_commission,
        commission_snapshot,
    )

    coupon = Decimal(str(hr.coupon_discount or 0))
    gross_taxable = Decimal(str(hr.taxable_amount or 0)) + coupon
    config = await resolve_hotel_commission(db, hotel)
    cres = compute_commission(
        gross_taxable, config, room_nights=int(hr.room_nights or 1)
    )
    return (
        float(cres.commission_amount),
        float(cres.partner_payout),
        commission_snapshot(cres),
        float(gross_taxable),
    )


async def _resolve_hotel_tds(db: AsyncSession, partner_id, payout: float):
    """TDS (Sec 194C) applicable to a hotel settlement — COMPANY partners only.

    Returns (tds_deducted, tds_rate_pct, tds_enabled). tds_deducted is 0 for
    non-COMPANY partners, when TDS is disabled, or when payout <= 0.
    """
    from app.modules.partner.models import Partner as _Partner

    partner_obj = (
        await db.execute(select(_Partner).where(_Partner.id == partner_id))
    ).scalar_one_or_none()
    if not partner_obj or partner_obj.partner_type != "COMPANY":
        return 0.0, 0.0, False
    tds_cfg_rows = (
        (
            await db.execute(
                text(
                    "SELECT config_key, config_value FROM system_configurations "
                    "WHERE config_key IN ('TDS_ENABLED', 'TDS_RATE')"
                )
            )
        )
        .mappings()
        .all()
    )
    tds_cfg = {r["config_key"]: r["config_value"] for r in tds_cfg_rows}
    tds_on = tds_cfg.get("TDS_ENABLED", "true").lower() == "true"
    tds_rate_pct = float(tds_cfg.get("TDS_RATE", "1") or "1")
    if not tds_on or payout <= 0:
        return 0.0, tds_rate_pct, tds_on
    tds_deducted = round(payout * tds_rate_pct / 100, 2)
    return tds_deducted, tds_rate_pct, tds_on


def _compute_hotel_position(
    *,
    grand_total: float,
    coupon_discount: float,
    partner_payout: float,
    total_paid: float,
    partner_held: float,
    tds_deducted: float = 0.0,
) -> dict:
    """Pure hotel settlement arithmetic — no DB, no writes. Mirrors _compute_position.

    TDS (Sec 194C, COMPANY partners) is folded into the net so the wallet
    movement happens once and the DEBIT sufficiency guard covers the full
    amount the partner owes (net + TDS) — a separate post-hoc TDS debit would
    otherwise be able to push the wallet balance negative.
    """
    partner_held = round(partner_held, 2)
    platform_held = round(total_paid - partner_held, 2)
    balance_due = round(max(grand_total - total_paid, 0.0), 2)
    net = round(partner_payout - partner_held - coupon_discount - tds_deducted, 2)
    return {
        "grand_total": round(grand_total, 2),
        "total_paid": round(total_paid, 2),
        "advance_paid": round(total_paid, 2),
        "partner_held": partner_held,
        "platform_held": platform_held,
        "balance_due": balance_due,
        "coupon_discount": round(coupon_discount, 2),
        "partner_payout": round(partner_payout, 2),
        "tds_deducted": round(tds_deducted, 2),
        "net_settlement": net,
        "wallet_direction": "CREDIT" if net > 0 else ("DEBIT" if net < 0 else "NONE"),
    }


async def _build_hotel_settlement_detail(
    db: AsyncSession, mb: MasterBooking, hr, hotel
) -> dict:
    """Enriched hotel settlement detail for the settlements list + modal."""
    from app.modules.hotel.services import billing as hotel_billing

    # Customer
    cust = (
        await db.execute(select(Customer).where(Customer.id == mb.customer_id))
    ).scalar_one_or_none()
    cust_user = None
    if cust:
        cust_user = (
            await db.execute(select(User).where(User.id == cust.user_id))
        ).scalar_one_or_none()

    # Partner (owner of the hotel)
    partner_id = hotel.partner_id
    partner_name = partner_mobile = None
    partner_wallet_balance = None
    if partner_id:
        p = (
            await db.execute(select(Partner).where(Partner.id == partner_id))
        ).scalar_one_or_none()
        if p:
            partner_name = p.business_name or f"Partner #{p.id}"
            partner_mobile = p.mobile
        w = (
            (
                await db.execute(
                    text(
                        "SELECT available_balance FROM wallets WHERE partner_id = :pid"
                    ),
                    {"pid": partner_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if w:
            partner_wallet_balance = float(w["available_balance"] or 0)

    coupon_discount = float(hr.coupon_discount or 0)
    gst_amount = float(hr.gst_amount or 0)
    grand_total = float(hr.total_amount or 0)

    # Commission: use the frozen figures once settled, otherwise resolve live.
    if hr.reservation_status == "SETTLED" and float(hr.partner_payout or 0) > 0:
        commission = float(hr.platform_commission or 0)
        payout = float(hr.partner_payout or 0)
        gross_taxable = float(hr.taxable_amount or 0) + coupon_discount
    else:
        commission, payout, _snap, gross_taxable = (
            await _resolve_hotel_commission_amounts(db, hr, hotel)
        )

    advances = await hotel_billing.list_advances(db, hr.id)
    total_paid = float(hotel_billing.total_advance(advances))
    partner_held = _hotel_partner_held(advances)

    # TDS (Sec 194C) — COMPANY partners only; folded into the net position so
    # the settle modal and the actual wallet movement agree.
    tds_deducted, tds_rate_pct, _tds_on = (
        await _resolve_hotel_tds(db, partner_id, payout)
        if partner_id
        else (0.0, 0.0, False)
    )

    position = _compute_hotel_position(
        grand_total=grand_total,
        coupon_discount=coupon_discount,
        partner_payout=payout,
        total_paid=total_paid,
        partner_held=partner_held,
        tds_deducted=tds_deducted,
    )

    cpd = (
        (
            await db.execute(
                text(
                    "SELECT id, coupon_discount_amount, status FROM pending_coupon_disbursements "
                    "WHERE hotel_reservation_id = :hid"
                ),
                {"hid": hr.id},
            )
        )
        .mappings()
        .one_or_none()
    )
    coupon_disbursement = None
    if cpd:
        coupon_disbursement = {
            "id": cpd["id"],
            "amount": float(cpd["coupon_discount_amount"] or 0),
            "status": cpd["status"],
        }

    return {
        "service_type": "HOTEL",
        # Step 1: Booking details
        "booking_number": mb.booking_number,
        "reservation_number": hr.reservation_number,
        "hotel_status": hr.reservation_status,
        "hotel_name": hotel.hotel_name,
        "customer_name": cust.full_name if cust else None,
        "customer_mobile": cust_user.mobile_number if cust_user else None,
        "check_in_date": hr.check_in_date.isoformat() if hr.check_in_date else None,
        "check_out_date": hr.check_out_date.isoformat() if hr.check_out_date else None,
        "nights": hr.nights,
        "rooms": hr.rooms_count,
        "invoice_number": hr.invoice_number,
        "invoice_url": hr.invoice_url,
        # Step 2: Payment details
        "taxable_amount": gross_taxable,
        "gst_amount": gst_amount,
        "grand_total": grand_total,
        "coupon_discount": coupon_discount,
        "advance_paid": position["advance_paid"],
        "balance_due": position["balance_due"],
        "payment_mode": hr.payment_mode,
        "payment_collected_by": hr.payment_collected_by,
        "payment_collected_status": hr.payment_collected_status,
        # Step 3: Settlement amounts — the full net-position arithmetic.
        "platform_commission": commission,
        "partner_payout": payout,
        "tds_deducted": tds_deducted,
        "tds_rate_pct": tds_rate_pct,
        "position": position,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "partner_mobile": partner_mobile,
        "partner_wallet_balance": partner_wallet_balance,
        "coupon_disbursement": coupon_disbursement,
        "is_settled": hr.reservation_status == "SETTLED",
    }


async def _settle_hotel_reservation(
    db: AsyncSession, mb: MasterBooking, hr, hotel
) -> dict:
    """Core hotel settlement — wallet/ledger/TDS/coupon writes. Does NOT commit.

    Shared by the Settlements page (POST /hotel/settle) and the booking-detail
    settle button so both take the identical financial path.

    Split-stay invariant (migration 0042_hotel_switch): when a stay is split,
    each half has its own hotel_reservations row with its own taxable_amount
    and total_amount snapshotted onto it. This function settles one row at a
    time and is intentionally NOT aware of any predecessor — the original half
    carries the truncated (consumed-nights) bill and settles on those amounts,
    while the new half settles on its own bill. Commission, TDS, coupon and
    partner-held-money math each use the row's own snapshot, so settlement
    correctness is preserved without any special-case logic here.
    """
    from app.modules.hotel.services import billing as hotel_billing
    from app.modules.hotel.models import HotelReservation as _HR

    if hr.reservation_status != "COMPLETED":
        raise HTTPException(
            400,
            f"Cannot settle: hotel status is '{hr.reservation_status}'. "
            "Only COMPLETED reservations can be settled.",
        )
    if not hr.invoice_number:
        raise HTTPException(400, "Generate the invoice before settling.")
    if hr.payment_collected_status != "PAID":
        raise HTTPException(400, "Collect the full balance before settling this stay.")

    partner_id = hotel.partner_id
    if not partner_id:
        raise HTTPException(400, "This hotel has no partner assigned. Cannot settle.")

    commission, payout, snap, gross_taxable = await _resolve_hotel_commission_amounts(
        db, hr, hotel
    )
    coupon_discount = float(hr.coupon_discount or 0)
    gst_amount = float(hr.gst_amount or 0)
    grand_total = float(hr.total_amount or 0)

    # TDS (Sec 194C) — COMPANY partners only. Resolved up-front and folded into
    # the net position below, so the wallet movement happens ONCE and the DEBIT
    # sufficiency guard covers the full amount the partner owes (net + TDS). A
    # separate post-hoc TDS debit could otherwise drive the wallet negative.
    tds_deducted, tds_rate_pct, _tds_on = await _resolve_hotel_tds(
        db, partner_id, payout
    )
    tds_record_id = None

    advances = await hotel_billing.list_advances(db, hr.id)
    total_paid = float(hotel_billing.total_advance(advances))
    partner_held = _hotel_partner_held(advances)

    position = _compute_hotel_position(
        grand_total=grand_total,
        coupon_discount=coupon_discount,
        partner_payout=payout,
        total_paid=total_paid,
        partner_held=partner_held,
        tds_deducted=tds_deducted,
    )
    net = position["net_settlement"]

    wallet_row = await _get_partner_wallet(db, partner_id)
    if not wallet_row:
        raise HTTPException(
            400, "Partner does not have a wallet. Cannot process settlement."
        )
    if wallet_row["wallet_status"] != "ACTIVE":
        raise HTTPException(
            400,
            f"Partner wallet is '{wallet_row['wallet_status']}'. Cannot process settlement.",
        )
    partner_wallet_balance = float(wallet_row["available_balance"] or 0)
    wallet_id = wallet_row["id"]

    ref = hr.reservation_number or f"HR-{hr.id}"
    tds_clause = f" − TDS ₹{tds_deducted:.2f}" if tds_deducted > 0 else ""
    workings = (
        f"Taxable ₹{gross_taxable:.2f} (coupon added back ₹{coupon_discount:.2f}) | "
        f"Commission ₹{commission:.2f} | Payout ₹{payout:.2f} | GST ₹{gst_amount:.2f} | "
        f"Collected ₹{total_paid:.2f} (property holds ₹{partner_held:.2f}) | "
        f"Net = {payout:.2f} − {partner_held:.2f} − {coupon_discount:.2f}"
        f"{tds_clause} = ₹{net:.2f}"
    )

    new_bal = partner_wallet_balance
    if net > 0:
        new_bal = await _credit_partner_wallet(
            db,
            wallet_id,
            net,
            partner_wallet_balance,
            reference=ref,
            narration=f"Hotel settlement credit — {ref} | {workings}",
            reference_type="SETTLEMENT",
        )
        settlement_note = f"₹{net:.2f} credited to partner wallet. Partner wallet balance: ₹{new_bal:.2f}. "
        if tds_deducted > 0:
            settlement_note += f" TDS ₹{tds_deducted:.2f} ({tds_rate_pct}%) included in the credit (Sec 194C). "
    elif net < 0:
        owed = abs(net)
        if partner_wallet_balance < owed:
            raise HTTPException(
                400,
                f"Insufficient partner wallet balance to settle. "
                f"Required: ₹{owed:.2f} (includes TDS ₹{tds_deducted:.2f} where applicable) | "
                f"Partner wallet: ₹{partner_wallet_balance:.2f} | "
                f"Shortfall: ₹{owed - partner_wallet_balance:.2f}. "
                "Partner must top up their wallet before settlement.",
            )
        new_bal = await _debit_partner_wallet(
            db,
            wallet_id,
            owed,
            partner_wallet_balance,
            reference=ref,
            narration=f"Hotel settlement debit — {ref} | {workings}",
            reference_type="SETTLEMENT_DEBIT",
        )
        settlement_note = (
            f"₹{owed:.2f} debited from partner wallet — the property is holding "
            f"₹{partner_held:.2f} against a payout of ₹{payout:.2f}. "
            f"Partner wallet balance: ₹{new_bal:.2f}. "
        )
        if tds_deducted > 0:
            settlement_note += f" TDS ₹{tds_deducted:.2f} ({tds_rate_pct}%) included in the debit (Sec 194C). "
    else:
        settlement_note = "No wallet movement — what the property holds exactly matches what they are owed. "

    # ── Coupon disbursement (platform-funded, reimbursed separately) ─────────
    coupon_disbursement_id = None
    if coupon_discount > 0:
        existing_cpd = (
            await db.execute(
                text(
                    "SELECT id FROM pending_coupon_disbursements WHERE hotel_reservation_id = :hid"
                ),
                {"hid": hr.id},
            )
        ).scalar_one_or_none()
        if not existing_cpd:
            res = await db.execute(
                text(
                    """
                    INSERT INTO pending_coupon_disbursements
                        (hotel_reservation_id, master_booking_id, partner_id, customer_id,
                         coupon_discount_amount, booking_number, invoice_number,
                         service_type, status, created_at)
                    VALUES
                        (:hid, :mbid, :pid, :cid, :amt, :bn, :inv, 'HOTEL', 'PENDING', NOW())
                    RETURNING id
                """
                ),
                {
                    "hid": hr.id,
                    "mbid": mb.id,
                    "pid": partner_id,
                    "cid": hr.customer_id or mb.customer_id,
                    "amt": coupon_discount,
                    "bn": ref,
                    "inv": hr.invoice_number,
                },
            )
            coupon_disbursement_id = res.scalar_one()
            settlement_note += (
                f" Coupon disbursement of ₹{coupon_discount:.2f} recorded "
                f"(pending admin approval on Coupon Disbursements page)."
            )

    # ── TDS Deduction (Section 194C — COMPANY partners only) ─────────────────
    # TDS is already folded into `net` above, so no separate wallet debit here —
    # the tds_deduction_records row is the statutory register entry. This
    # guarantees the wallet never goes negative from a post-hoc TDS debit.
    if tds_deducted > 0:
        from datetime import date as _date
        from app.modules.partner.models import PartnerGSTDetails as _PGST

        gst_row = (
            await db.execute(select(_PGST).where(_PGST.partner_id == partner_id))
        ).scalar_one_or_none()
        pan_number = gst_row.pan_number if gst_row else None
        ref_date = _date.today()
        _fy_y = ref_date.year
        _fy_str = (
            f"{_fy_y-1}-{str(_fy_y)[2:]}"
            if ref_date.month < 4
            else f"{_fy_y}-{str(_fy_y+1)[2:]}"
        )
        _m = ref_date.month
        _q = (
            "Q1"
            if _m in (4, 5, 6)
            else "Q2" if _m in (7, 8, 9) else "Q3" if _m in (10, 11, 12) else "Q4"
        )
        _month_key = f"{ref_date.year}-{ref_date.month:02d}"
        try:
            res_tds = await db.execute(
                text(
                    """
                INSERT INTO tds_deduction_records
                    (partner_id, hotel_reservation_id, service_type, month, gross_payout,
                     tds_rate, tds_amount, net_payout, pan_number, deducted_at,
                     financial_year, quarter)
                VALUES
                    (:pid, :hid, 'HOTEL', :mo, :gpayout, :rate, :tds, :netpayout, :pan,
                     NOW(), :fy, :q)
                ON CONFLICT (hotel_reservation_id) DO UPDATE SET
                    tds_amount = EXCLUDED.tds_amount, net_payout = EXCLUDED.net_payout,
                    deducted_at = NOW()
                RETURNING id
            """
                ),
                {
                    "pid": partner_id,
                    "hid": hr.id,
                    "mo": _month_key,
                    "gpayout": payout,
                    "rate": tds_rate_pct,
                    "tds": tds_deducted,
                    "netpayout": payout - tds_deducted,
                    "pan": pan_number,
                    "fy": _fy_str,
                    "q": _q,
                },
            )
            tds_record_id = res_tds.scalar_one()
        except Exception as _e:
            import logging as _log

            _log.getLogger(__name__).warning(
                f"Hotel TDS record insert failed (non-fatal): {_e}"
            )

    # ── Freeze commission onto the reservation + mark settled ────────────────
    hr.platform_commission = Decimal(str(commission))
    hr.partner_payout = Decimal(str(payout))
    hr.commission_config_snapshot = snap
    hr.reservation_status = "SETTLED"

    # Close the master booking once every service on it is settled/closed out.
    # The session runs with autoflush=False (see app/core/database.py), so the
    # `hr.reservation_status = "SETTLED"` set just above is NOT yet visible to a
    # SELECT — flush it first, otherwise the read-back sees the stale COMPLETED
    # status and the master is left CONFIRMED forever (the reported bug).
    await db.flush()
    # Re-roll hotel totals/collections onto the master as the last thing before
    # close. This is the safety net for any prior state-mutating endpoint that
    # forgot to sync — even if every previous call missed, settle guarantees a
    # consistent master row right before the CLOSED write.
    from app.modules.booking.services import rollup_hotel_totals_into_master

    await rollup_hotel_totals_into_master(db, mb.id, mb)
    cab_statuses = (
        (
            await db.execute(
                select(CabBooking.booking_status).where(
                    CabBooking.master_booking_id == mb.id
                )
            )
        )
        .scalars()
        .all()
    )
    # Force the reservation being settled to SETTLED in this snapshot regardless
    # of what the row read returns, so a stale identity-map value can't block the
    # close.
    hotel_rows = (
        await db.execute(
            select(_HR.id, _HR.reservation_status).where(_HR.master_booking_id == mb.id)
        )
    ).all()
    hotel_statuses = ["SETTLED" if hid == hr.id else st for hid, st in hotel_rows]
    all_done = all(s in {"SETTLED", "CANCELLED"} for s in cab_statuses) and all(
        s in {"SETTLED", "CANCELLED", "REJECTED", "NO_SHOW"} for s in hotel_statuses
    )
    if all_done:
        mb.booking_status = "CLOSED"

    await _log_timeline(
        db,
        mb.id,
        "HOTEL_SETTLED_BY_ADMIN",
        f"Admin settled hotel reservation {ref} via Settlements page. {workings}. "
        + settlement_note,
    )

    return {
        "success": True,
        "message": "Hotel booking settled successfully.",
        "service_type": "HOTEL",
        "booking_number": mb.booking_number,
        "reservation_number": hr.reservation_number,
        "hotel_status": "SETTLED",
        "booking_status": mb.booking_status,
        "payment_mode": hr.payment_mode,
        "taxable_amount": gross_taxable,
        "grand_total": grand_total,
        "platform_commission": commission,
        "partner_payout": payout,
        "coupon_discount": coupon_discount,
        "position": position,
        "partner_wallet_balance": new_bal,
        "coupon_disbursement_id": coupon_disbursement_id,
        "tds_deducted": tds_deducted,
        "tds_record_id": tds_record_id,
        "settlement_note": settlement_note,
    }


# ── Hotel settlement endpoints ───────────────────────────────────────────────


@router.get("/hotel/list", tags=["Settlements"])
async def list_hotel_settlements(
    status_filter: str = Query("PENDING", description="PENDING | SETTLED | ALL"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    """List hotel reservations for settlement.

    PENDING — COMPLETED, invoice generated, balance fully collected (PAID)
    SETTLED — already settled
    ALL — both
    """
    from app.modules.hotel.models import HotelReservation as _HR

    paid_cond = (_HR.payment_collected_status == "PAID") & (
        _HR.invoice_number.is_not(None)
    )
    if status_filter == "PENDING":
        status_cond = (_HR.reservation_status == "COMPLETED") & paid_cond
    elif status_filter == "SETTLED":
        status_cond = _HR.reservation_status == "SETTLED"
    else:
        status_cond = (
            (_HR.reservation_status.in_(["COMPLETED", "SETTLED"])) & paid_cond
        ) | (_HR.reservation_status == "SETTLED")

    total = (
        await db.execute(select(func.count(_HR.id)).where(status_cond))
    ).scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                select(_HR)
                .where(status_cond)
                .order_by(_HR.id.desc())
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for hr in rows:
        mb, hr2, hotel = await _get_hotel_reservation(db, hr.reservation_number)
        items.append(await _build_hotel_settlement_detail(db, mb, hr2, hotel))

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "status_filter": status_filter,
        "items": items,
    }


@router.get("/hotel/booking", tags=["Settlements"])
async def get_hotel_settlement_booking(
    reservation_number: str = Query(..., description="Hotel reservation number"),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    mb, hr, hotel = await _get_hotel_reservation(db, reservation_number)
    return await _build_hotel_settlement_detail(db, mb, hr, hotel)


class HotelSettleRequest(BaseModel):
    reservation_number: str


@router.post("/hotel/settle", tags=["Settlements"])
async def settle_hotel_booking(
    payload: HotelSettleRequest,
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    """Settle a hotel reservation after check-out, invoicing and payment collection."""
    # S7: lock the reservation row so concurrent settles can't double-credit.
    await _lock_settlement_target(
        db,
        "hotel_reservations",
        "reservation_number",
        payload.reservation_number.strip().upper(),
    )
    mb, hr, hotel = await _get_hotel_reservation(db, payload.reservation_number)
    result = await _settle_hotel_reservation(db, mb, hr, hotel)
    await db.commit()
    return result


# ════════════════════════════════════════════════════════════════
# TOUR SETTLEMENTS — GET /admin/settlements/tour/list
# List tour bookings for settlement (COMPLETED with invoice + full payment)
# ════════════════════════════════════════════════════════════════


def _compute_tour_position(
    *,
    total_amount: float,
    partner_payout: float,
    total_paid: float,
    partner_held: float,
) -> dict:
    """Pure tour settlement arithmetic — no DB, no writes. Mirrors the hotel
    position: custody (who received the money) drives the wallet direction.

      net = partner_payout − partner_held
        net > 0  → CREDIT partner wallet   (platform holds money it owes)
        net < 0  → DEBIT  partner wallet   (partner holds more than owed)
        net = 0  → no movement
    """
    partner_held = round(partner_held, 2)
    platform_held = round(total_paid - partner_held, 2)
    balance_due = round(max(total_amount - total_paid, 0.0), 2)
    net = round(partner_payout - partner_held, 2)
    return {
        "grand_total": round(total_amount, 2),
        "total_paid": round(total_paid, 2),
        "advance_paid": round(total_paid, 2),
        "partner_held": partner_held,
        "platform_held": platform_held,
        "balance_due": balance_due,
        "partner_payout": round(partner_payout, 2),
        "net_settlement": net,
        "wallet_direction": "CREDIT" if net > 0 else ("DEBIT" if net < 0 else "NONE"),
    }


async def _build_tour_settlement_detail(db: AsyncSession, tour) -> dict:
    """Enriched tour settlement detail for the settlements list + modal."""
    from app.modules.tour.models import TourPackage, TourAdvancePayment
    from app.modules.partner.models import Partner as _Partner

    mb = (
        await db.execute(
            select(MasterBooking).where(MasterBooking.id == tour.master_booking_id)
        )
    ).scalar_one_or_none()
    package = (
        await db.execute(select(TourPackage).where(TourPackage.id == tour.package_id))
    ).scalar_one_or_none()

    # Customer
    cust = cust_user = None
    if mb:
        cust = (
            await db.execute(select(Customer).where(Customer.id == mb.customer_id))
        ).scalar_one_or_none()
        if cust:
            cust_user = (
                await db.execute(select(User).where(User.id == cust.user_id))
            ).scalar_one_or_none()

    # Partner (owner of the tour package)
    partner_id = package.partner_id if package else None
    partner_name = partner_mobile = None
    partner_wallet_balance = None
    if partner_id:
        p = (
            await db.execute(select(_Partner).where(_Partner.id == partner_id))
        ).scalar_one_or_none()
        if p:
            partner_name = p.business_name or f"Partner #{p.id}"
            partner_mobile = p.mobile
        w = (
            (
                await db.execute(
                    text(
                        "SELECT available_balance FROM wallets WHERE partner_id = :pid"
                    ),
                    {"pid": partner_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if w:
            partner_wallet_balance = float(w["available_balance"] or 0)

    # Payments — active advances with custody.
    advances = (
        (
            await db.execute(
                select(TourAdvancePayment)
                .where(
                    TourAdvancePayment.tour_booking_id == tour.id,
                    TourAdvancePayment.status == "ACTIVE",
                )
                .order_by(TourAdvancePayment.id)
            )
        )
        .scalars()
        .all()
    )
    total_paid = float(sum(a.amount or 0 for a in advances))
    partner_held = float(
        sum(a.amount or 0 for a in advances if a.received_by in ("PARTNER", "DRIVER"))
    )

    total_amount = float(tour.total_amount or 0)
    payout = float(tour.partner_payout or 0)
    if tour.booking_status == "SETTLED" and payout > 0:
        commission = float(tour.platform_commission or 0)
    else:
        commission = float(tour.platform_commission or 0)

    position = _compute_tour_position(
        total_amount=total_amount,
        partner_payout=payout,
        total_paid=total_paid,
        partner_held=partner_held,
    )

    cust_name = None
    if cust:
        cust_name = " ".join(filter(None, [cust.first_name, cust.last_name]))

    return {
        "service_type": "TOUR",
        # Step 1: Booking details
        "booking_number": mb.booking_number if mb else None,
        "tour_booking_number": tour.booking_number,
        "tour_status": tour.booking_status,
        "package_name": package.package_name if package else None,
        "destination": package.destination if package else None,
        "duration_days": package.duration_days if package else None,
        "duration_nights": package.duration_nights if package else None,
        "travel_start_date": (
            tour.travel_start_date.isoformat() if tour.travel_start_date else None
        ),
        "travel_end_date": (
            tour.travel_end_date.isoformat() if tour.travel_end_date else None
        ),
        "persons_count": tour.persons_count,
        "customer_name": cust_name,
        "customer_mobile": cust_user.mobile_number if cust_user else None,
        "invoice_number": tour.invoice_number,
        # Step 2: Payment details
        "total_amount": total_amount,
        "additional_amount": float(tour.additional_amount or 0),
        "advance_paid": position["advance_paid"],
        "balance_due": position["balance_due"],
        "advance_received_by": tour.advance_received_by,
        "advances": [
            {
                "id": a.id,
                "receipt_number": a.receipt_number,
                "amount": float(a.amount),
                "payment_mode": a.payment_mode,
                "received_by": a.received_by,
                "collected_by_role": a.collected_by_role,
                "collected_at": a.collected_at.isoformat() if a.collected_at else None,
            }
            for a in advances
        ],
        # Step 3: Settlement amounts — the full net-position arithmetic.
        "platform_commission": commission,
        "partner_payout": payout,
        "position": position,
        "partner_id": partner_id,
        "partner_name": partner_name,
        "partner_mobile": partner_mobile,
        "partner_wallet_balance": partner_wallet_balance,
        "is_settled": tour.booking_status == "SETTLED",
    }


async def _get_tour_booking(db: AsyncSession, booking_number: str):
    from app.modules.tour.models import TourBooking

    tour = (
        await db.execute(
            select(TourBooking).where(TourBooking.booking_number == booking_number)
        )
    ).scalar_one_or_none()
    if not tour:
        raise HTTPException(404, "Tour booking not found")
    return tour


@router.get("/tour/list", tags=["Settlements"])
async def list_tour_settlements(
    status_filter: str = Query("PENDING", description="PENDING | SETTLED | ALL"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    """List tour bookings for settlement.

    PENDING — COMPLETED, invoice generated, balance fully collected (PAID)
    SETTLED — already settled
    ALL — both
    """
    from app.modules.tour.models import TourBooking as _TB

    if status_filter == "PENDING":
        status_cond = (
            (_TB.booking_status == "COMPLETED")
            & (_TB.invoice_number.is_not(None))
            & (_TB.payment_status == "PAID")
        )
    elif status_filter == "SETTLED":
        status_cond = _TB.booking_status == "SETTLED"
    else:
        status_cond = _TB.booking_status.in_(["COMPLETED", "SETTLED"])

    total = (
        await db.execute(select(func.count(_TB.id)).where(status_cond))
    ).scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                select(_TB)
                .where(status_cond)
                .order_by(_TB.id.desc())
                .offset(offset)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for tour in rows:
        items.append(await _build_tour_settlement_detail(db, tour))

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "status_filter": status_filter,
        "items": items,
    }


@router.get("/tour/booking", tags=["Settlements"])
async def get_tour_settlement_booking(
    booking_number: str = Query(..., description="Tour booking number"),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    tour = await _get_tour_booking(db, booking_number)
    return await _build_tour_settlement_detail(db, tour)


class TourSettleRequest(BaseModel):
    booking_number: str


@router.post("/tour/settle", tags=["Settlements"])
async def settle_tour_booking(
    payload: TourSettleRequest,
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    """Settle a tour booking after completion, invoicing and full payment."""
    from app.modules.tour.services import settle_booking

    # S7: lock the tour booking row so concurrent settles can't double-credit.
    await _lock_settlement_target(
        db, "tour_bookings", "booking_number", payload.booking_number.strip()
    )
    tour = await _get_tour_booking(db, payload.booking_number)
    result = await settle_booking(db, tour_booking_id=tour.id, user_id="SYSTEM")
    await db.commit()
    return {
        "success": True,
        "message": "Tour booking settled successfully.",
        "data": result,
    }


# ════════════════════════════════════════════════════════════════
# COUPON DISBURSEMENTS — GET /admin/settlements/coupon-disbursements
# List pending coupon disbursements admin owes to partners
# ════════════════════════════════════════════════════════════════


@router.get("/coupon-disbursements", tags=["Settlements"])
async def list_coupon_disbursements(
    status_filter: str = Query("PENDING", description="PENDING | DISBURSED | ALL"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    """List pending coupon disbursements — admin owes these amounts to partners."""
    await _ensure_coupon_disbursements_table(db)

    if status_filter == "ALL":
        where_clause = "WHERE 1=1"
        where_params: dict = {}
    else:
        where_clause = "WHERE pcd.status = :status"
        where_params: dict = {"status": status_filter}

    count_res = await db.execute(
        text(f"SELECT COUNT(*) FROM pending_coupon_disbursements pcd {where_clause}"),
        where_params,
    )
    total = count_res.scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                text(
                    f"""
        SELECT
            pcd.id,
            pcd.cab_booking_id,
            pcd.hotel_reservation_id,
            pcd.service_type,
            pcd.master_booking_id,
            pcd.partner_id,
            pcd.customer_id,
            pcd.coupon_discount_amount,
            pcd.booking_number,
            pcd.invoice_number,
            pcd.status,
            pcd.disbursed_at,
            pcd.created_at,
            p.business_name AS partner_name,
            p.mobile AS partner_mobile,
            w.available_balance AS partner_wallet_balance
        FROM pending_coupon_disbursements pcd
        LEFT JOIN partners p ON p.id = pcd.partner_id
        LEFT JOIN wallets w ON w.partner_id = pcd.partner_id
        {where_clause}
        ORDER BY pcd.created_at DESC
        LIMIT :limit OFFSET :offset
    """
                ),
                {**where_params, "limit": page_size, "offset": offset},
            )
        )
        .mappings()
        .all()
    )

    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "cab_booking_id": r["cab_booking_id"],
                "hotel_reservation_id": r["hotel_reservation_id"],
                "service_type": r["service_type"]
                or ("HOTEL" if r["hotel_reservation_id"] else "CAB"),
                "booking_number": r["booking_number"],
                "invoice_number": r["invoice_number"],
                "partner_id": r["partner_id"],
                "partner_name": r["partner_name"],
                "partner_mobile": (
                    str(r["partner_mobile"]) if r["partner_mobile"] else None
                ),
                "coupon_discount_amount": float(r["coupon_discount_amount"] or 0),
                "partner_wallet_balance": float(r["partner_wallet_balance"] or 0),
                "status": r["status"],
                "disbursed_at": (
                    r["disbursed_at"].isoformat() if r["disbursed_at"] else None
                ),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total > 0 else 1,
        "items": items,
    }


# ════════════════════════════════════════════════════════════════
# DISBURSE COUPON — POST /admin/settlements/disburse-coupon
# Admin approves and credits coupon discount amount to partner wallet
# ════════════════════════════════════════════════════════════════


class DisburseRequest(BaseModel):
    disbursement_id: int


@router.post("/disburse-coupon", tags=["Settlements"])
async def disburse_coupon(
    payload: DisburseRequest,
    current_user: dict = Depends(
        require_roles("ADMIN", "SUPER_ADMIN", "FINANCE_MANAGER")
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin approves coupon disbursement → credits partner wallet with
    the coupon discount amount.
    """
    await _ensure_coupon_disbursements_table(db)

    # Fetch disbursement record
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM pending_coupon_disbursements WHERE id = :id FOR UPDATE"
                ),
                {"id": payload.disbursement_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(
            404, f"Disbursement record #{payload.disbursement_id} not found."
        )
    if row["status"] == "DISBURSED":
        raise HTTPException(400, "This coupon disbursement has already been processed.")

    partner_id = row["partner_id"]
    amount = float(row["coupon_discount_amount"] or 0)
    if amount <= 0:
        raise HTTPException(400, "Disbursement amount is zero. Nothing to disburse.")

    # Load partner wallet
    wallet_row = await _get_partner_wallet(db, partner_id)
    if not wallet_row:
        raise HTTPException(400, "Partner wallet not found.")
    if wallet_row["wallet_status"] != "ACTIVE":
        raise HTTPException(400, f"Partner wallet is '{wallet_row['wallet_status']}'.")

    # Credit partner wallet
    new_bal = await _credit_partner_wallet(
        db,
        wallet_id=wallet_row["id"],
        amount=amount,
        current_balance=float(wallet_row["available_balance"] or 0),
        reference=row["booking_number"],
        narration=(
            f"Coupon discount disbursement — {row['booking_number']} | "
            f"Invoice: {row['invoice_number'] or 'N/A'} | "
            f"Amount: ₹{amount:.2f} (partner's earnings restored for coupon discount)"
        ),
    )

    # Mark disbursed
    await db.execute(
        text(
            "UPDATE pending_coupon_disbursements SET status = 'DISBURSED', disbursed_at = NOW() WHERE id = :id"
        ),
        {"id": payload.disbursement_id},
    )

    # Log to booking timeline
    await _log_timeline(
        db,
        row["master_booking_id"],
        "COUPON_DISBURSEMENT_CREDITED",
        f"Admin disbursed coupon amount ₹{amount:.2f} to partner wallet. "
        f"New partner wallet balance: ₹{new_bal:.2f}. "
        f"Booking: {row['booking_number']}.",
    )

    await db.commit()

    return {
        "success": True,
        "message": f"₹{amount:.2f} credited to partner wallet for coupon disbursement.",
        "disbursement_id": payload.disbursement_id,
        "partner_id": partner_id,
        "amount_credited": amount,
        "partner_wallet_new_balance": new_bal,
    }


# ════════════════════════════════════════════════════════════════
# HANDOVER RECONCILIATION REPORT
#
# After a partner handover (migration 0041), the original partner's
# partner-side custody — cash collected by the broken-down driver, plus
# any advance payment with received_by=PARTNER — must be reconciled
# outside the booking settlement. This report surfaces what the original
# partner still owes the platform so finance can chase it.
# ════════════════════════════════════════════════════════════════


@router.get(
    "/handover-reconciliation",
    tags=["Settlements"],
    summary="List cash/advance the original (handed-over) partner still owes",
)
async def list_handover_reconciliation(
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN", "FINANCE")),
    db: AsyncSession = Depends(get_db),
    partner_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Returns one row per cab booking that has been handed over, with the
    cash and advance the original partner physically holds but the booking
    settlement no longer captures (because the new partner is now active).

    The platform does NOT auto-deduct any of this — admin/finance must
    reconcile it manually with the original partner.
    """
    sql = text(
        """
        SELECT
            cb.id              AS cab_booking_id,
            cb.booking_number,
            cb.booking_status,
            cb.original_partner_id,
            orig_p.business_name   AS original_partner_name,
            orig_p.mobile          AS original_partner_mobile,
            cb.swap_count,
            cb.last_swap_at,
            cb.cash_amount_due,
            cb.cash_pending_at,
            COALESCE(
                (SELECT SUM(amount) FROM advance_payments
                 WHERE cab_booking_id = cb.id
                   AND status = 'ACTIVE'
                   AND received_by IN ('PARTNER','DRIVER')
                ), 0
            ) AS active_partner_side_advance,
            COALESCE(
                (SELECT array_agg(receipt_number)
                 FROM advance_payments
                 WHERE cab_booking_id = cb.id
                   AND status = 'ACTIVE'
                   AND received_by IN ('PARTNER','DRIVER')
                ), ARRAY[]::text[]
            ) AS active_partner_side_advance_receipts,
            COALESCE(
                (SELECT SUM(amount) FROM advance_payments
                 WHERE cab_booking_id = cb.id
                   AND status = 'VOIDED'
                   AND voided_at >= cb.last_swap_at
                ), 0
            ) AS voided_on_handover_advance
        FROM cab_bookings cb
        JOIN partners orig_p ON orig_p.id = cb.original_partner_id
        WHERE cb.is_breakdown_swap = TRUE
          AND cb.original_partner_id IS NOT NULL
          AND (CAST(:pid AS bigint) IS NULL OR cb.original_partner_id = CAST(:pid AS bigint))
        ORDER BY cb.last_swap_at DESC NULLS LAST, cb.id DESC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, {"pid": partner_id, "limit": limit})).mappings().all()
    items = []
    for r in rows:
        cash_due = float(r["cash_amount_due"] or 0)
        advance_active = float(r["active_partner_side_advance"] or 0)
        advance_voided = float(r["voided_on_handover_advance"] or 0)
        items.append(
            {
                "cab_booking_id": int(r["cab_booking_id"]),
                "booking_number": str(r["booking_number"]),
                "cab_status": str(r["booking_status"]),
                "original_partner_id": int(r["original_partner_id"]),
                "original_partner_name": r["original_partner_name"],
                "original_partner_mobile": r["original_partner_mobile"],
                "swap_count": int(r["swap_count"] or 0),
                "last_swap_at": (
                    r["last_swap_at"].isoformat() if r["last_swap_at"] else None
                ),
                # Outstanding reconciliation amounts.
                "outstanding_cash": cash_due,
                "outstanding_advance": advance_active,
                "voided_on_handover_advance": advance_voided,
                "total_outstanding": round(cash_due + advance_active, 2),
                "active_partner_side_advance_receipts": list(
                    r["active_partner_side_advance_receipts"] or []
                ),
            }
        )
    return {"items": items, "total": len(items)}
