# ============================================================
# WAY TERO — ADVANCE PAYMENT SERVICE
# File: app/modules/booking/services/__init__.py
# Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
#
# The single source of truth for advance-payment rules. Both portals call in
# here (admin/advance_api.py and partner/booking_api.py) so the two can never
# drift apart on who may take money, in what form, or how the balance is
# derived from it.
#
# Raises WayTeroException subclasses only — never HTTPException. The handler in
# main.py turns them into the standard response envelope.
# ============================================================

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import select, func, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    DuplicateResourceException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.booking.models import MasterBooking, BookingService
from app.modules.hotel.models import HotelAdvancePayment, HotelReservation
from app.modules.tour.models import TourAdvancePayment, TourBooking

# ════════════════════════════════════════════════════════════════
# RULES
# ════════════════════════════════════════════════════════════════

RECEIVER_ADMIN = "ADMIN"
RECEIVER_PARTNER = "PARTNER"
RECEIVER_DRIVER = "DRIVER"

STATUS_ACTIVE = "ACTIVE"
STATUS_VOIDED = "VOIDED"

# Who may take money in which form. ONLINE means the platform payment gateway,
# so only the platform can be its receiver — a partner or driver marked as
# having received an ONLINE payment would leave custody ambiguous, and custody
# is the one thing settlement runs on.
MODE_BY_RECEIVER: Dict[str, set] = {
    RECEIVER_ADMIN: {"CASH", "ONLINE", "UPI"},
    RECEIVER_PARTNER: {"CASH", "UPI"},
    RECEIVER_DRIVER: {"CASH", "UPI"},
}

# Receivers whose custody sits on the partner side of the settlement ledger.
# Used by settlement to work out the partner's net position.
PARTNER_SIDE_RECEIVERS = {RECEIVER_PARTNER, RECEIVER_DRIVER}

# An advance is money taken before the fare is final. Once the trip is closed
# the balance has already been computed and stored, so an advance recorded
# after that point would silently invalidate it.
COLLECTABLE_STATUSES = {
    "PENDING_ASSIGNMENT",
    "ASSIGNED",
    "DRIVER_ASSIGNED",
    "STARTED",
}


# ════════════════════════════════════════════════════════════════
# BALANCE
# ════════════════════════════════════════════════════════════════


def compute_balance_due(
    *,
    final_amount: float,
    coupon_discount: float = 0.0,
    advance_paid: float = 0.0,
    gst_amount: float = 0.0,
    is_tax_invoice: bool = False,
) -> float:
    """
    What the customer still owes at trip end.

    Balance is computed on the gross figure — fare plus GST when the booking is
    a tax invoice — because that is what the customer-facing PDF prints
    (invoice_pdf_service). Trip-close and collect-payment previously worked net
    of GST, so on a GST booking the stored cash figure and the printed invoice
    disagreed. Both now go through here.
    """
    gross = round(
        float(final_amount or 0) + (float(gst_amount or 0) if is_tax_invoice else 0.0),
        2,
    )
    return max(
        0.0, round(gross - float(coupon_discount or 0) - float(advance_paid or 0), 2)
    )


# ════════════════════════════════════════════════════════════════
# READ
# ════════════════════════════════════════════════════════════════


async def get_active_advance(
    db: AsyncSession, cab_booking_id: int
) -> Optional[Dict[str, Any]]:
    """
    The live advance on a booking, or None.

    Replaces the old `float(mb.total_paid_amount or 0)` reads, which carried no
    mode, no receiver and no receipt — and were destroyed the moment final
    payment overwrote that field.
    """
    row = (
        (
            await db.execute(
                text(
                    """
            SELECT id, cab_booking_id, master_booking_id, booking_number,
                   receipt_number, amount, payment_mode, received_by,
                   partner_id, driver_id, reference_note, status,
                   collected_by_user_id, collected_by_role, collected_at
            FROM advance_payments
            WHERE cab_booking_id = :cbid AND status = 'ACTIVE'
        """
                ),
                {"cbid": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def get_advance_amount(db: AsyncSession, cab_booking_id: int) -> float:
    """Convenience for the many call sites that only need the number."""
    adv = await get_active_advance(db, cab_booking_id)
    return float(adv["amount"]) if adv else 0.0


def advance_summary(adv: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Serialise an advance row for an API response."""
    if not adv:
        return None
    return {
        "id": adv["id"],
        "receipt_number": adv["receipt_number"],
        "amount": float(adv["amount"]),
        "payment_mode": adv["payment_mode"],
        "received_by": adv["received_by"],
        "reference_note": adv["reference_note"],
        "status": adv["status"],
        "collected_by_role": adv["collected_by_role"],
        "collected_at": (
            adv["collected_at"].isoformat() if adv["collected_at"] else None
        ),
    }


async def _next_receipt_number(db: AsyncSession) -> str:
    """
    WT-ADV-YYYYMM-00001. MAX-based rather than COUNT-based so a concurrent
    insert cannot hand two receipts the same number — the same rule the invoice
    and cash-collection numbering follow.
    """
    row = (
        await db.execute(
            text(
                """
        SELECT MAX(CAST(SPLIT_PART(receipt_number, '-', 4) AS INTEGER))
        FROM advance_payments
        WHERE receipt_number LIKE 'WT-ADV-%'
    """
            )
        )
    ).scalar()
    seq = (row or 0) + 1
    return f"WT-ADV-{datetime.now(timezone.utc).strftime('%Y%m')}-{str(seq).zfill(5)}"


async def _latest_assignment(
    db: AsyncSession, cab_booking_id: int
) -> Optional[Dict[str, Any]]:
    """
    cab_booking_assignments has no UNIQUE on cab_booking_id — reassignment adds
    a row rather than replacing one. Latest wins, the rule used throughout the
    booking APIs.
    """
    row = (
        (
            await db.execute(
                text(
                    """
            SELECT partner_id, driver_id
            FROM cab_booking_assignments
            WHERE cab_booking_id = :cbid
            ORDER BY assigned_at DESC NULLS LAST, id DESC
            LIMIT 1
        """
                ),
                {"cbid": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


async def get_eligibility(db: AsyncSession, cab_booking_id: int) -> Dict[str, Any]:
    """
    Everything the collect-advance form needs to render itself correctly:
    whether collection is open at all, and which receivers and modes are legal
    on this booking right now.
    """
    cb = (
        (
            await db.execute(
                text(
                    """
            SELECT id, booking_number, booking_status, payment_mode,
                   final_amount, estimated_amount
            FROM cab_bookings WHERE id = :cbid
        """
                ),
                {"cbid": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not cb:
        raise ResourceNotFoundException("Cab booking", cab_booking_id)

    existing = await get_active_advance(db, cab_booking_id)
    assignment = await _latest_assignment(db, cab_booking_id)
    partner_id = assignment.get("partner_id") if assignment else None
    driver_id = assignment.get("driver_id") if assignment else None

    # Until a partner is on the booking there is nobody but the platform who
    # could have taken the money.
    allowed_receivers = [RECEIVER_ADMIN]
    if partner_id:
        allowed_receivers.append(RECEIVER_PARTNER)
        if driver_id:
            allowed_receivers.append(RECEIVER_DRIVER)

    fare = float(cb["final_amount"] or cb["estimated_amount"] or 0)

    reason: Optional[str] = None
    if existing:
        reason = "An advance has already been collected for this booking."
    elif cb["payment_mode"]:
        reason = "Final payment has already been recorded for this booking."
    elif cb["booking_status"] not in COLLECTABLE_STATUSES:
        reason = f"Advance cannot be collected while the booking is '{cb['booking_status']}'."
    elif fare <= 0:
        reason = "This booking has no fare yet."

    return {
        "booking_number": cb["booking_number"],
        "advance": advance_summary(existing),
        "can_collect": reason is None,
        "blocked_reason": reason,
        "allowed_receivers": allowed_receivers,
        "modes_by_receiver": {
            r: sorted(MODE_BY_RECEIVER[r]) for r in allowed_receivers
        },
        "max_amount": fare,
        "is_assigned": bool(partner_id),
        "has_driver": bool(driver_id),
    }


# ════════════════════════════════════════════════════════════════
# WRITE
# ════════════════════════════════════════════════════════════════


async def create_advance(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    amount: float,
    payment_mode: str,
    received_by: str,
    user_id: str,
    source_role: str,
    reference_note: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record an advance against a cab booking.

    Does not commit — get_db commits on a clean return and rolls back on the
    exceptions raised here.
    """
    mode = (payment_mode or "").strip().upper()
    receiver = (received_by or "").strip().upper()

    if receiver not in MODE_BY_RECEIVER:
        raise ValidationException(
            f"received_by must be one of: {', '.join(sorted(MODE_BY_RECEIVER))}."
        )
    if mode not in MODE_BY_RECEIVER[receiver]:
        raise ValidationException(
            f"{receiver} cannot receive an advance by {mode or '(none)'}. "
            f"Allowed for {receiver}: {', '.join(sorted(MODE_BY_RECEIVER[receiver]))}."
        )

    # Lock the booking row so a concurrent request cannot pass the checks below
    # against state this one is about to change.
    cb = (
        (
            await db.execute(
                text(
                    """
            SELECT id, master_booking_id, booking_number, booking_status,
                   payment_mode, final_amount, estimated_amount
            FROM cab_bookings WHERE id = :cbid
            FOR UPDATE
        """
                ),
                {"cbid": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not cb:
        raise ResourceNotFoundException("Cab booking", cab_booking_id)

    if cb["payment_mode"]:
        raise BusinessException(
            "Final payment has already been recorded for this booking, so an "
            "advance can no longer be collected."
        )
    if cb["booking_status"] not in COLLECTABLE_STATUSES:
        raise BusinessException(
            f"Advance cannot be collected while the booking is "
            f"'{cb['booking_status']}'. Advance is only available before the "
            "trip is closed."
        )

    if amount is None or float(amount) <= 0:
        raise ValidationException("Advance amount must be greater than zero.")

    fare = float(cb["final_amount"] or cb["estimated_amount"] or 0)
    if fare <= 0:
        raise BusinessException(
            "This booking has no fare yet — set the fare before collecting an advance."
        )
    if round(float(amount), 2) > round(fare, 2):
        raise ValidationException(
            f"Advance ₹{float(amount):.2f} cannot exceed the booking fare ₹{fare:.2f}."
        )

    assignment = await _latest_assignment(db, cab_booking_id)
    partner_id = assignment.get("partner_id") if assignment else None
    driver_id = assignment.get("driver_id") if assignment else None

    # The rule that keeps an unassigned booking unambiguous: with no partner on
    # it, the platform is the only party that could have taken the money.
    if not partner_id and receiver != RECEIVER_ADMIN:
        raise BusinessException(
            "This booking is not assigned to a partner yet, so the advance can "
            "only be received by ADMIN."
        )
    if receiver == RECEIVER_DRIVER and not driver_id:
        raise BusinessException(
            "No driver is assigned to this booking, so the advance cannot be "
            "received by DRIVER."
        )

    # Friendly message for the ordinary case. The partial unique index below is
    # what actually guarantees it.
    if await get_active_advance(db, cab_booking_id):
        raise DuplicateResourceException("Advance payment", "booking")

    receipt_number = await _next_receipt_number(db)

    try:
        row = (
            (
                await db.execute(
                    text(
                        """
                INSERT INTO advance_payments
                    (cab_booking_id, master_booking_id, booking_number, receipt_number,
                     amount, payment_mode, received_by, partner_id, driver_id,
                     reference_note, status, collected_by_user_id, collected_by_role,
                     collected_at, created_at)
                VALUES
                    (:cbid, :mbid, :bn, :receipt, :amount, :mode, :receiver,
                     :partner_id, :driver_id, :note, 'ACTIVE', :uid, :role,
                     NOW(), NOW())
                RETURNING id, receipt_number, amount, payment_mode, received_by,
                          reference_note, status, collected_by_role, collected_at
            """
                    ),
                    {
                        "cbid": cab_booking_id,
                        "mbid": cb["master_booking_id"],
                        "bn": cb["booking_number"],
                        "receipt": receipt_number,
                        "amount": Decimal(str(amount)),
                        "mode": mode,
                        "receiver": receiver,
                        # Snapshot of who held the money, so a later reassignment cannot
                        # rewrite history.
                        "partner_id": (
                            partner_id if receiver in PARTNER_SIDE_RECEIVERS else None
                        ),
                        "driver_id": driver_id if receiver == RECEIVER_DRIVER else None,
                        "note": (reference_note or "").strip() or None,
                        "uid": user_id,
                        "role": (source_role or "").strip().upper(),
                    },
                )
            )
            .mappings()
            .one()
        )
    except IntegrityError:
        # ux_advance_payments_active_booking tripped — a concurrent request took
        # an advance on this booking between our check above and this insert.
        # get_db rolls the session back on the way out.
        raise DuplicateResourceException("Advance payment", "booking")

    balance = compute_balance_due(final_amount=fare, advance_paid=float(amount))
    await _log_timeline(
        db,
        cb["master_booking_id"],
        "ADVANCE_COLLECTED",
        f"Advance ₹{float(amount):.2f} collected via {mode}, received by {receiver}. "
        f"Receipt {receipt_number}. Recorded from the {(source_role or '').upper()} portal. "
        f"Fare ₹{fare:.2f}, indicative balance ₹{balance:.2f}.",
        user_id,
    )

    result = dict(row)
    result["balance_after_advance"] = balance
    result["fare"] = fare
    return result


async def void_advance(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    user_id: str,
    reason: str,
) -> Dict[str, Any]:
    """
    Cancel an advance so a corrected one can be recorded. Admin-only; the route
    layer enforces that.

    The row is kept and flipped to VOIDED rather than deleted — that releases
    the partial unique index without losing the audit trail.
    """
    if not reason or not reason.strip():
        raise ValidationException("A reason is required to void an advance.")

    cb = (
        (
            await db.execute(
                text(
                    """
            SELECT id, master_booking_id, booking_number, payment_mode
            FROM cab_bookings WHERE id = :cbid
            FOR UPDATE
        """
                ),
                {"cbid": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not cb:
        raise ResourceNotFoundException("Cab booking", cab_booking_id)

    # Once the balance has been collected the advance is already baked into that
    # figure, so removing it here would silently corrupt the settlement.
    if cb["payment_mode"]:
        raise BusinessException(
            "Final payment has already been recorded — this advance can no "
            "longer be voided."
        )

    adv = await get_active_advance(db, cab_booking_id)
    if not adv:
        raise ResourceNotFoundException("Active advance payment", cab_booking_id)

    await db.execute(
        text(
            """
            UPDATE advance_payments
            SET status = 'VOIDED',
                voided_by_user_id = :uid,
                void_reason = :reason,
                voided_at = NOW()
            WHERE id = :aid AND status = 'ACTIVE'
        """
        ),
        {"uid": user_id, "reason": reason.strip(), "aid": adv["id"]},
    )

    await _log_timeline(
        db,
        cb["master_booking_id"],
        "ADVANCE_VOIDED",
        f"Advance {adv['receipt_number']} of ₹{float(adv['amount']):.2f} "
        f"(received by {adv['received_by']} via {adv['payment_mode']}) voided. "
        f"Reason: {reason.strip()}",
        user_id,
    )

    return {
        "receipt_number": adv["receipt_number"],
        "amount": float(adv["amount"]),
        "received_by": adv["received_by"],
        "payment_mode": adv["payment_mode"],
    }


# ════════════════════════════════════════════════════════════════
# RECEIPT
# ════════════════════════════════════════════════════════════════

_RECEIPT_CONFIG_KEYS = (
    "PLATFORM_NAME",
    "PLATFORM_LOGO_URL",
    "BUSINESS_LEGAL_NAME",
    "BUSINESS_GST_NUMBER",
    "BUSINESS_REGISTERED_ADDRESS",
    "SUPPORT_EMAIL",
    "SUPPORT_PHONE",
)


async def build_receipt_pdf(db: AsyncSession, cab_booking_id: int) -> Dict[str, Any]:
    """
    Render the advance receipt for a booking, returning ``{filename, pdf}``.

    Lives here rather than in either portal so the customer gets a byte-identical
    document no matter which side collected the money.
    """
    from app.modules.admin.invoice_pdf_service import generate_advance_receipt_pdf

    adv = await get_active_advance(db, cab_booking_id)
    if not adv:
        raise ResourceNotFoundException("Active advance payment", cab_booking_id)

    ctx = (
        (
            await db.execute(
                text(
                    """
            SELECT cb.booking_number      AS cab_booking_number,
                   cb.trip_type,
                   cb.pickup_location,
                   cb.drop_location,
                   cb.pickup_datetime,
                   cb.final_amount,
                   cb.estimated_amount,
                   mb.booking_number      AS master_booking_number,
                   ci.name                AS city_name,
                   vc.category_name       AS vehicle_category_name,
                   cu.first_name, cu.last_name,
                   u.mobile_number,
                   p.business_name, p.owner_name,
                   d.full_name            AS driver_name
            FROM cab_bookings cb
            JOIN master_bookings mb   ON mb.id = cb.master_booking_id
            LEFT JOIN cities ci       ON ci.id = mb.city_id
            LEFT JOIN vehicle_categories vc ON vc.id = cb.vehicle_category_id
            LEFT JOIN customers cu    ON cu.id = mb.customer_id
            LEFT JOIN users u         ON u.id  = cu.user_id
            LEFT JOIN partners p      ON p.id  = :partner_id
            LEFT JOIN drivers d       ON d.id  = :driver_id
            WHERE cb.id = :cbid
        """
                ),
                {
                    "cbid": cab_booking_id,
                    # The snapshot on the advance row, not the current assignment — the
                    # receipt must keep naming whoever actually took the money.
                    "partner_id": adv["partner_id"],
                    "driver_id": adv["driver_id"],
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if not ctx:
        raise ResourceNotFoundException("Cab booking", cab_booking_id)

    cfg_rows = (
        (
            await db.execute(
                text(
                    """
            SELECT config_key, config_value FROM system_configurations
            WHERE config_key = ANY(:keys)
        """
                ),
                {"keys": list(_RECEIPT_CONFIG_KEYS)},
            )
        )
        .mappings()
        .all()
    )
    cfg = {r["config_key"]: (r["config_value"] or "") for r in cfg_rows}

    customer_name = (
        " ".join(filter(None, [ctx["first_name"], ctx["last_name"]])).strip() or None
    )

    partner_label = ctx["business_name"] or ctx["owner_name"]
    receiver = adv["received_by"]
    receiver_name = (
        ctx["driver_name"]
        if receiver == RECEIVER_DRIVER
        else partner_label if receiver == RECEIVER_PARTNER else None
    )

    amount = float(adv["amount"])
    fare = float(ctx["final_amount"] or ctx["estimated_amount"] or 0)

    pdf = generate_advance_receipt_pdf(
        receipt_number=adv["receipt_number"],
        cab_booking_number=ctx["cab_booking_number"],
        booking_number=ctx["master_booking_number"],
        customer_name=customer_name,
        customer_mobile=ctx["mobile_number"],
        city_name=ctx["city_name"],
        trip_type=ctx["trip_type"],
        pickup_location=ctx["pickup_location"],
        drop_location=ctx["drop_location"],
        pickup_datetime=ctx["pickup_datetime"],
        vehicle_category_name=ctx["vehicle_category_name"],
        partner_name=partner_label,
        driver_name=ctx["driver_name"],
        amount=amount,
        payment_mode=adv["payment_mode"],
        received_by=receiver,
        receiver_name=receiver_name,
        reference_note=adv["reference_note"],
        collected_at=adv["collected_at"],
        estimated_amount=fare or None,
        # No GST on an advance — it is a part-payment against a fare that is not
        # yet final, so the tax point has not been reached.
        balance_after_advance=(
            compute_balance_due(final_amount=fare, advance_paid=amount)
            if fare
            else None
        ),
        platform_name=cfg.get("PLATFORM_NAME") or "WayTero",
        platform_logo_url=cfg.get("PLATFORM_LOGO_URL", ""),
        business_legal_name=cfg.get("BUSINESS_LEGAL_NAME", ""),
        business_gst_number=cfg.get("BUSINESS_GST_NUMBER", ""),
        business_registered_address=cfg.get("BUSINESS_REGISTERED_ADDRESS", ""),
        support_email=cfg.get("SUPPORT_EMAIL", ""),
        support_phone=cfg.get("SUPPORT_PHONE", ""),
    )

    return {"filename": f"Advance_Receipt_{adv['receipt_number']}.pdf", "pdf": pdf}


async def _log_timeline(
    db: AsyncSession,
    master_booking_id: int,
    event_type: str,
    description: str,
    user_id: Optional[str] = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp, created_by)
            VALUES (:mbid, :etype, :desc, NOW(), :uid)
        """
        ),
        {
            "mbid": master_booking_id,
            "etype": event_type,
            "desc": description,
            "uid": user_id,
        },
    )


# ════════════════════════════════════════════════════════════════
# HOTEL → MASTER ROLLUP
# Doc Ref: BRD Part 4 §92 — Hotel totals must roll up to master
#          BRD Part 3 §45 — Advance collection (custody-neutral)
#
# The cab flow writes payment_status / total_paid_amount onto the master
# inline at payment time (partner/booking_api.py). The hotel flow never did —
# it only touched hotel_reservations and hotel_advance_payments — so a hotel
# booking that was checked-out, paid and settled still showed the master row
# as it was at creation: PENDING, ₹0 paid, the pre-checkout total. This
# reconciles it.
#
# Scoped to HOTEL-only bookings on purpose: mixed cab+hotel masters keep the
# cab inline accounting untouched (no combined bookings exist today). Call it
# after any event that changes a hotel total or collection (check-out,
# advance, payment, add-charges, invoice generation).
#
# Lives here rather than in admin/booking_api.py so both admin and partner
# routers share one implementation. Was previously the private
# `_sync_master_from_hotel` in admin/booking_api.py:397, kept here under the
# public name `rollup_hotel_totals_into_master` so new mutations don't have
# to remember the helper is private.
# ════════════════════════════════════════════════════════════════


async def rollup_hotel_totals_into_master(
    db: AsyncSession,
    master_booking_id: int,
    mb: Optional[MasterBooking] = None,
) -> None:
    """
    Roll hotel reservation totals and collections up onto the umbrella master
    booking. Idempotent: re-running produces the same result.

    Parameters
    ----------
    db
        Async session.
    master_booking_id
        The master booking to roll up. If `mb` is None the function loads a
        fresh copy from the DB.
    mb
        Optional in-memory MasterBooking. Pass it when the caller already has
        the row loaded to skip the SELECT and avoid a stale identity-map read
        on the same session.

    Behavior
    --------
    Runs for any master that contains at least one HOTEL service — pure-HOTEL
    or mixed (HOTEL + CAB, HOTEL + TOUR). For mixed masters this still rolls
    up hotel totals/collections only; cab accounting stays on its own inline
    writes. Mixed masters used to be silently no-op'd, which is how a stale
    PENDING/₹0 master row would survive every state change.
    """
    if mb is None:
        mb = (
            await db.execute(
                select(MasterBooking).where(MasterBooking.id == master_booking_id)
            )
        ).scalar_one_or_none()
        if mb is None:
            return  # Caller decides whether a missing master is an error.

    svc_types = set(
        (
            await db.execute(
                select(BookingService.service_type).where(
                    BookingService.master_booking_id == master_booking_id
                )
            )
        )
        .scalars()
        .all()
    )
    if "HOTEL" not in svc_types:
        return

    # Sum active reservations (a cancelled/rejected/no-show room contributes nothing).
    total = (
        await db.execute(
            select(func.coalesce(func.sum(HotelReservation.total_amount), 0)).where(
                HotelReservation.master_booking_id == master_booking_id,
                HotelReservation.reservation_status.notin_(
                    ["CANCELLED", "REJECTED", "NO_SHOW"]
                ),
            )
        )
    ).scalar() or 0

    # What the customer actually paid in — advances + final payment, net of
    # refunds — regardless of whether ADMIN or PARTNER took custody (both are
    # the guest's money).
    paid = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        HotelAdvancePayment.amount
                        - func.coalesce(HotelAdvancePayment.refunded_amount, 0)
                    ),
                    0,
                )
            ).where(
                HotelAdvancePayment.master_booking_id == master_booking_id,
                HotelAdvancePayment.status == "ACTIVE",
            )
        )
    ).scalar() or 0

    from decimal import Decimal  # local import keeps the top of file clean.

    total_d = Decimal(str(total))
    paid_d = Decimal(str(paid))

    mb.total_amount = total_d
    mb.total_paid_amount = paid_d
    if paid_d <= 0:
        mb.payment_status = "PENDING"
    elif paid_d < total_d:
        mb.payment_status = "PARTIAL"
    else:
        mb.payment_status = "PAID"

    # Keep the service-line amount in step with the (possibly re-priced)
    # reservation.
    await db.execute(
        update(BookingService)
        .where(
            BookingService.master_booking_id == master_booking_id,
            BookingService.service_type == "HOTEL",
        )
        .values(service_amount=total_d)
    )


async def rollup_tour_totals_into_master(
    db: AsyncSession,
    master_booking_id: int,
    mb: Optional[MasterBooking] = None,
) -> None:
    """Roll tour booking totals and collections up onto the umbrella master.

    Idempotent. Mirrors `rollup_hotel_totals_into_master` but reads from
    `tour_bookings` / `tour_advance_payments`. Runs for any master that has
    at least one TOUR service — pure-tour or mixed. Without this the admin
    bookings list showed `total_paid_amount: 0` for tour-only masters and the
    master stayed in `PENDING_PAYMENT / PENDING` even after the customer had
    paid.

    Called after `collect_advance`, `void_advance`, and the settle path so
    the umbrella row mirrors reality.
    """
    if mb is None:
        mb = (
            await db.execute(
                select(MasterBooking).where(MasterBooking.id == master_booking_id)
            )
        ).scalar_one_or_none()
        if mb is None:
            return

    svc_types = set(
        (
            await db.execute(
                select(BookingService.service_type).where(
                    BookingService.master_booking_id == master_booking_id
                )
            )
        )
        .scalars()
        .all()
    )
    if "TOUR" not in svc_types:
        return

    # Sum active tours (a cancelled tour contributes nothing). Tour totals
    # grow over time as charges are added; the rollup always reflects the
    # current sum.
    total = (
        await db.execute(
            select(func.coalesce(func.sum(TourBooking.total_amount), 0)).where(
                TourBooking.master_booking_id == master_booking_id,
                TourBooking.booking_status != "CANCELLED",
            )
        )
    ).scalar() or 0

    paid = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        TourAdvancePayment.amount
                        - func.coalesce(TourAdvancePayment.refunded_amount, 0)
                    ),
                    0,
                )
            ).where(
                TourAdvancePayment.master_booking_id == master_booking_id,
                TourAdvancePayment.status == "ACTIVE",
            )
        )
    ).scalar() or 0

    total_d = Decimal(str(total))
    paid_d = Decimal(str(paid))

    mb.total_amount = total_d
    mb.total_paid_amount = paid_d
    if paid_d <= 0:
        mb.payment_status = "PENDING"
    elif paid_d < total_d:
        mb.payment_status = "PARTIAL"
    else:
        mb.payment_status = "PAID"

    await db.execute(
        update(BookingService)
        .where(
            BookingService.master_booking_id == master_booking_id,
            BookingService.service_type == "TOUR",
        )
        .values(service_amount=total_d)
    )
