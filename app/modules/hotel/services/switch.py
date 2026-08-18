# ============================================================
# WAY TERO — HOTEL SWITCH / MID-STAY SPLIT SERVICE
# File: app/modules/hotel/services/switch.py
# Doc Ref: Hotel Switch Spec
# Migration: 0042_hotel_switch
#
# Two flows live here:
#
#   1. PRE-CHECKIN SWITCH  (status ∈ PENDING_PAYMENT / AWAITING_HOTEL_CONFIRMATION /
#                           CONFIRMED)
#      The whole original reservation is cancelled and replaced by a new
#      reservation at a different hotel for the same date range. Inventory and
#      money move as a unit; nothing is consumed yet.
#
#   2. POST-CHECKIN SPLIT   (status ∈ CHECKED_IN / IN_HOUSE)
#      The customer has already stayed at the original for some nights. The
#      original reservation is *truncated* (not cancelled) — `check_out_date`
#      and money columns are rewritten to the consumed-nights bill, inventory
#      for the remaining nights is released, and a new reservation is created
#      for those remaining nights at a different hotel.
#
# Both flows share the same advance redistribution (ROLLOVER vs NONE) and the
# same audit row in hotel_reservation_split_events. Settlement reads
# `taxable_amount` / `total_amount` per row, so a split just produces two
# independent rows that each settle themselves — no settlement-code change.
# ============================================================

from __future__ import annotations

import json as _json
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.booking.services import rollup_hotel_totals_into_master
from app.modules.hotel.constants import (
    HOTEL_ADVANCE_STRATEGIES,
    HOTEL_ADVANCE_STRATEGY_NONE,
    HOTEL_ADVANCE_STRATEGY_ROLLOVER,
    HOTEL_ADVANCE_REFUND_RECORDED,
    HOTEL_ADVANCE_ROLLOVER,
    HOTEL_SPLIT_INITIATED,
    HOTEL_SPLIT_COMPLETED,
    HOTEL_SPLIT_TYPE_POST_CHECKIN_SPLIT,
    HOTEL_SPLIT_TYPE_PRE_CHECKIN_SWITCH,
    HOTEL_SWITCH_INITIATED,
    HOTEL_SWITCH_COMPLETED,
)
from app.modules.hotel.models import Hotel, HotelReservation
from app.modules.hotel.services.billing import next_receipt_number
from app.modules.hotel.services.quote import compute_hotel_quote


_ZERO = Decimal("0.00")

# Statuses that mean "no nights consumed yet" — a pre-checkin switch cancels
# the whole reservation. AWAITING_HOTEL_CONFIRMATION is the admin-created
# pre-payment state from customer_care_api; once confirmed the partner has
# accepted but the customer still hasn't checked in.
_PRE_CHECKIN_STATUSES = {"PENDING_PAYMENT", "AWAITING_HOTEL_CONFIRMATION", "CONFIRMED"}
# Statuses that mean "the customer is in the room" — a post-checkin split must
# keep the original reservation open for the consumed nights.
_POST_CHECKIN_STATUSES = {"CHECKED_IN", "IN_HOUSE"}


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────


def _hydrate(row) -> HotelReservation:
    """Map a raw `hotel_reservations` row dict back into a transient
    HotelReservation instance. The service is short-lived and rolls back on
    error, so we don't need this row to participate in the identity map —
    callers read fresh with their own SELECTs at the end of the flow."""
    res = HotelReservation()
    for k in row.keys():
        setattr(res, k, row[k])
    return res


async def _advance_total(db: AsyncSession, reservation_id: int) -> Decimal:
    """Net of refunds. Mirrors services.billing.total_advance but runs the
    SQL itself instead of loading ORM rows — call sites need a single number."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) AS net "
                    "FROM hotel_advance_payments "
                    "WHERE hotel_reservation_id = :rid AND status = 'ACTIVE'"
                ),
                {"rid": reservation_id},
            )
        )
        .mappings()
        .one()
    )
    return Decimal(str(row["net"] or 0))


async def _log_timeline(
    db: AsyncSession,
    master_booking_id: int,
    event_type: str,
    description: str,
    created_by: Optional[str] = None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO booking_timelines "
            "  (master_booking_id, event_type, event_description, event_timestamp, created_by) "
            "VALUES (:mb_id, :etype, :desc, NOW(), :by)"
        ),
        {
            "mb_id": master_booking_id,
            "etype": event_type,
            "desc": description,
            "by": created_by,
        },
    )


def _new_booking_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = str(_uuid.uuid4()).replace("-", "")[:6].upper()
    return f"HR-{ts}-{uid}"


def _new_master_booking_number() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid = str(_uuid.uuid4()).replace("-", "")[:6].upper()
    return f"MB-{ts}-{uid}"


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────


async def quote_switch(
    db: AsyncSession,
    original: HotelReservation,
    *,
    target_hotel_id: int,
    target_room_category_id: int,
    target_check_in: date,
    target_check_out: date,
    adults_count: int = 1,
    children_count: int = 0,
    extra_beds: int = 0,
) -> dict:
    """Read-only preview of a hotel switch or mid-stay split.

    Returns the money the customer would be billed for the new stay, how much
    of the original's advance is available to roll over, the suggested
    redistribution strategy, and whether the target hotel has rooms.

    `remaining_nights` is derived from the original's lifecycle:
      - CONFIRMED → planned nights minus any consumed nights (typically 0).
      - CHECKED_IN / IN_HOUSE → planned nights minus actual nights stayed.

    Raises ValidationException if there is nothing left to transfer (every
    night already consumed) and BusinessException if the target hotel has
    no inventory for the requested window.
    """
    nights_total = int(original.nights or 0)
    today = datetime.now(timezone.utc).date()
    consumed = 0
    if str(original.reservation_status).upper() in _POST_CHECKIN_STATUSES:
        anchor = (
            original.actual_check_in_at.date()
            if original.actual_check_in_at
            else original.check_in_date
        )
        consumed = max(0, (today - anchor).days)
    remaining = max(0, nights_total - consumed)
    if remaining <= 0:
        raise ValidationException(
            "This reservation has no remaining nights to transfer — every night is already consumed."
        )

    target = (
        (
            await db.execute(
                text("SELECT * FROM hotels WHERE id = :id AND deleted_at IS NULL"),
                {"id": target_hotel_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not target:
        raise ResourceNotFoundException("Target hotel", target_hotel_id)
    if str(target["status"]) != "ACTIVE":
        raise BusinessException(
            f"Target hotel is not active (status: {target['status']})."
        )

    # Hotel ORM instance for the pricing engine.
    target_hotel = Hotel()
    for k in target.keys():
        setattr(target_hotel, k, target[k])

    q = await compute_hotel_quote(
        db,
        target_hotel,
        target_room_category_id,
        target_check_in,
        target_check_out,
        rooms_count=int(original.rooms_count or 1),
        adults_count=adults_count,
        children_count=children_count,
        extra_beds=extra_beds,
    )

    advance_net = await _advance_total(db, int(original.id))
    target_total = Decimal(str(q["total_amount"]))
    refund_preview = money(max(_ZERO, advance_net - target_total))
    rollover_amount = money(min(advance_net, target_total))
    strategy = (
        HOTEL_ADVANCE_STRATEGY_ROLLOVER
        if advance_net <= target_total
        else HOTEL_ADVANCE_STRATEGY_NONE
    )

    # Inventory check on the target window.
    rooms = int(original.rooms_count or 1)
    inv_rows = (
        (
            await db.execute(
                text(
                    "SELECT inventory_date, available_rooms FROM hotel_inventory "
                    "WHERE room_category_id = :cid "
                    "  AND inventory_date >= :dfrom AND inventory_date < :dto "
                    "  AND is_stop_sell = FALSE"
                ),
                {
                    "cid": target_room_category_id,
                    "dfrom": target_check_in,
                    "dto": target_check_out,
                },
            )
        )
        .mappings()
        .all()
    )
    unavailable = [
        r["inventory_date"].isoformat()
        for r in inv_rows
        if int(r["available_rooms"] or 0) < rooms
    ]

    return {
        "original_reservation_id": int(original.id),
        "original_reservation_number": original.reservation_number,
        "original_status": str(original.reservation_status),
        "nights_total": nights_total,
        "nights_consumed": consumed,
        "nights_remaining": remaining,
        "nights_transferred": (target_check_out - target_check_in).days,
        "target_quote": {
            "hotel_id": int(target_hotel.id),
            "hotel_name": str(target_hotel.hotel_name),
            "room_category_id": target_room_category_id,
            "check_in_date": target_check_in.isoformat(),
            "check_out_date": target_check_out.isoformat(),
            "nights": int(q["nights"]),
            "rooms_count": int(q["rooms_count"]),
            "base_amount": float(Decimal(str(q["base_amount"]))),
            "taxable_amount": float(Decimal(str(q["taxable_amount"]))),
            "gst_amount": float(Decimal(str(q["gst_amount"]))),
            "total_amount": float(target_total),
            "platform_commission": float(Decimal(str(q["platform_commission"]))),
            "partner_payout": float(Decimal(str(q["partner_payout"]))),
        },
        "money_moves": {
            "advance_net": float(advance_net),
            "rollover_amount": float(rollover_amount),
            "refund_preview": float(refund_preview),
            "suggested_strategy": strategy,
        },
        "inventory_available": not unavailable,
        "unavailable_dates": unavailable,
    }


async def switch_hotel_pre_checkin(
    db: AsyncSession,
    original: HotelReservation,
    *,
    target_hotel_id: int,
    target_room_category_id: int,
    target_check_in: date,
    target_check_out: date,
    strategy: str,
    reason: str,
    actor_id: Optional[UUID],
    notes: Optional[str] = None,
    adults_count: int = 1,
    children_count: int = 0,
    extra_beds: int = 0,
) -> HotelReservation:
    """Pre-checkin whole-reservation switch.

    Cancels the original (releasing all its inventory) and creates a new
    reservation at the target hotel for the full original date range, then
    redistributes advances per `strategy`. No nights are consumed on either
    side; both reservations still reconcile to zero checked-in nights.
    """
    if strategy not in HOTEL_ADVANCE_STRATEGIES:
        raise ValidationException(
            f"Unknown advance strategy '{strategy}'. Allowed: {', '.join(HOTEL_ADVANCE_STRATEGIES)}"
        )
    status = str(original.reservation_status or "").upper()
    if status not in _PRE_CHECKIN_STATUSES:
        raise ValidationException(
            f"Pre-checkin switch requires status in {sorted(_PRE_CHECKIN_STATUSES)}, "
            f"got {status or 'UNKNOWN'}. For an in-progress stay use split_stay_after_partial_checkin()."
        )

    # Quote the target stay.
    target = (
        (
            await db.execute(
                text("SELECT * FROM hotels WHERE id = :id AND deleted_at IS NULL"),
                {"id": target_hotel_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not target:
        raise ResourceNotFoundException("Target hotel", target_hotel_id)
    target_hotel = Hotel()
    for k in target.keys():
        setattr(target_hotel, k, target[k])

    q = await compute_hotel_quote(
        db,
        target_hotel,
        target_room_category_id,
        target_check_in,
        target_check_out,
        rooms_count=int(original.rooms_count or 1),
        adults_count=adults_count,
        children_count=children_count,
        extra_beds=extra_beds,
    )

    # Lock the original to serialize concurrent switches.
    locked = (
        await db.execute(
            text("SELECT id FROM hotel_reservations WHERE id = :id FOR UPDATE"),
            {"id": int(original.id)},
        )
    ).scalar_one_or_none()
    if locked is None:
        raise ResourceNotFoundException("Hotel reservation", int(original.id))

    # Release original's inventory for the full original window.
    await db.execute(
        text(
            "UPDATE hotel_inventory "
            "   SET booked_rooms    = GREATEST(booked_rooms - :rooms, 0), "
            "       available_rooms  = available_rooms + :rooms, "
            "       updated_at = NOW() "
            " WHERE room_category_id = :cid "
            "   AND inventory_date >= :dfrom AND inventory_date < :dto"
        ),
        {
            "rooms": int(original.rooms_count or 1),
            "cid": int(original.room_category_id),
            "dfrom": original.check_in_date,
            "dto": original.check_out_date,
        },
    )

    # Take inventory on the target window; oversell CHECK enforces availability.
    try:
        await db.execute(
            text(
                "UPDATE hotel_inventory "
                "   SET booked_rooms   = booked_rooms + :rooms, "
                "       available_rooms = available_rooms - :rooms, "
                "       updated_at = NOW() "
                " WHERE room_category_id = :cid "
                "   AND inventory_date >= :dfrom AND inventory_date < :dto "
                "   AND is_stop_sell = FALSE"
            ),
            {
                "rooms": int(original.rooms_count or 1),
                "cid": target_room_category_id,
                "dfrom": target_check_in,
                "dto": target_check_out,
            },
        )
    except Exception:
        await db.rollback()
        raise BusinessException(
            "Not enough rooms available at the target hotel for the requested window."
        )

    # Cancel the original reservation; keep the row for audit but mark
    # cancelled so it stops counting toward the master totals.
    await db.execute(
        text(
            "UPDATE hotel_reservations SET "
            "  reservation_status = 'CANCELLED', "
            "  cancellation_reason = :reason, "
            "  cancelled_at = NOW(), "
            "  is_split_stay = TRUE, "
            "  switched_at = NOW(), "
            "  switched_by_user_id = :actor, "
            "  switched_reason = :reason, "
            "  split_advance_strategy = :strategy, "
            "  updated_at = NOW() "
            "WHERE id = :id"
        ),
        {
            "reason": reason,
            "actor": str(actor_id) if actor_id else None,
            "strategy": strategy,
            "id": int(original.id),
        },
    )

    # Create the new reservation: a fresh master booking keeps the two halves
    # independently settleable. Same booking_service structure as customer-care.
    new_master_number = _new_master_booking_number()
    new_mb_id = (
        await db.execute(
            text(
                "INSERT INTO master_bookings ("
                "  uuid, booking_number, customer_id, city_id, "
                "  booking_status, payment_status, "
                "  total_amount, total_paid_amount, total_refund_amount, "
                "  journey_start_date, remarks, created_at, updated_at"
                ") VALUES ("
                "  :uuid, :bnum, :cid, :city_id, "
                "  'CONFIRMED', 'PENDING', "
                "  :amount, 0, 0, "
                "  :jdate, :remarks, NOW(), NOW()"
                ") RETURNING id"
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "bnum": new_master_number,
                "cid": original.customer_id,
                "city_id": (
                    int(target["city_id"]) if target["city_id"] is not None else None
                ),
                "amount": Decimal(str(q["total_amount"])),
                "jdate": target_check_in,
                "remarks": f"Hotel switch from reservation {original.reservation_number}",
            },
        )
    ).scalar_one()

    new_svc_id = (
        await db.execute(
            text(
                "INSERT INTO booking_services ("
                "  master_booking_id, service_type, service_reference_id, "
                "  service_status, service_amount, created_at"
                ") VALUES (:mb, 'HOTEL', 0, 'CONFIRMED', :amount, NOW()) RETURNING id"
            ),
            {"mb": new_mb_id, "amount": Decimal(str(q["total_amount"]))},
        )
    ).scalar_one()

    new_res_number = _new_booking_number()
    new_res_id = (
        await db.execute(
            text(
                "INSERT INTO hotel_reservations ("
                "  uuid, master_booking_id, booking_service_id, hotel_id, room_category_id, "
                "  customer_id, reservation_number, "
                "  check_in_date, check_out_date, nights, rooms_count, room_nights, "
                "  adults_count, children_count, "
                "  base_amount, extra_charges, discount_amount, taxable_amount, "
                "  gst_percent, gst_amount, is_tax_invoice, total_amount, "
                "  platform_commission, partner_payout, "
                "  rate_snapshot, commission_config_snapshot, "
                "  reservation_status, special_requests, "
                "  is_split_stay, original_reservation_id, "
                "  switched_at, switched_by_user_id, switched_reason, split_advance_strategy, "
                "  confirmed_at, created_at, updated_at"
                ") VALUES ("
                "  :uuid, :mb, :svc, :hotel_id, :cat_id, "
                "  :cust_id, :res_num, "
                "  :cin, :cout, :nights, :rooms, :room_nights, "
                "  :adults, :children, "
                "  :base, 0, 0, :taxable, "
                "  :gst_pct, :gst_amt, :is_tax_inv, :total, "
                "  :commission, :payout, "
                "  CAST(:rate_snap AS JSONB), CAST(:comm_snap AS JSONB), "
                "  'CONFIRMED', :special, "
                "  TRUE, :orig_id, "
                "  NOW(), :actor, :reason, :strategy, "
                "  NOW(), NOW(), NOW()"
                ") RETURNING id"
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "mb": new_mb_id,
                "svc": new_svc_id,
                "hotel_id": target_hotel_id,
                "cat_id": target_room_category_id,
                "cust_id": original.customer_id,
                "res_num": new_res_number,
                "cin": target_check_in,
                "cout": target_check_out,
                "nights": int(q["nights"]),
                "rooms": int(original.rooms_count or 1),
                "room_nights": int(q["room_nights"]),
                "adults": adults_count,
                "children": children_count,
                "base": Decimal(str(q["base_amount"])),
                "taxable": Decimal(str(q["taxable_amount"])),
                "gst_pct": Decimal(str(q["gst_percent"])),
                "gst_amt": Decimal(str(q["gst_amount"])),
                "is_tax_inv": bool(q["is_tax_invoice"]),
                "total": Decimal(str(q["total_amount"])),
                "commission": Decimal(str(q["platform_commission"])),
                "payout": Decimal(str(q["partner_payout"])),
                "rate_snap": _json.dumps(q["rate_snapshot"]),
                "comm_snap": _json.dumps(q["commission_snapshot"]),
                "special": notes,
                "orig_id": int(original.id),
                "actor": str(actor_id) if actor_id else None,
                "reason": reason,
                "strategy": strategy,
            },
        )
    ).scalar_one()

    # Point the service link at the new reservation row now that we have its id.
    await db.execute(
        text("UPDATE booking_services SET service_reference_id = :rid WHERE id = :sid"),
        {"rid": new_res_id, "sid": new_svc_id},
    )

    # Advance redistribution.
    refund_issued = _ZERO
    advance_redistributed = _ZERO
    adv_rows = (
        (
            await db.execute(
                text(
                    "SELECT id, amount, COALESCE(refunded_amount, 0) AS refunded_amount "
                    "FROM hotel_advance_payments "
                    "WHERE hotel_reservation_id = :rid AND status = 'ACTIVE' "
                    "ORDER BY collected_at ASC"
                ),
                {"rid": int(original.id)},
            )
        )
        .mappings()
        .all()
    )

    if strategy == HOTEL_ADVANCE_STRATEGY_ROLLOVER:
        remaining_target = Decimal(str(q["total_amount"]))
        for adv in adv_rows:
            available = Decimal(str(adv["amount"])) - Decimal(
                str(adv["refunded_amount"])
            )
            if available <= _ZERO or remaining_target <= _ZERO:
                continue
            move = min(available, remaining_target)
            new_amount = move
            await db.execute(
                text(
                    "UPDATE hotel_advance_payments "
                    "   SET refunded_amount = :refund "
                    " WHERE id = :id"
                ),
                {"refund": money(available), "id": int(adv["id"])},
            )
            await db.execute(
                text(
                    "INSERT INTO hotel_advance_payments ("
                    "  hotel_reservation_id, master_booking_id, receipt_number, "
                    "  amount, payment_mode, received_by, reference_number, notes, "
                    "  status, refunded_amount, collected_at, created_at"
                    ") VALUES ("
                    "  :rid, :mb, :rcpt, :amount, :mode, :by, :ref, :notes, "
                    "  'ACTIVE', 0, NOW(), NOW()"
                    ")"
                ),
                {
                    "rid": int(new_res_id),
                    "mb": int(new_mb_id),
                    "rcpt": await next_receipt_number(db),
                    "amount": new_amount,
                    "mode": "ROLLOVER",
                    "by": "ADMIN",
                    "ref": f"ROLLOVER-FROM-{int(original.id)}",
                    "notes": f"Rollover from advance {int(adv['id'])} of reservation {original.reservation_number}",
                },
            )
            remaining_target = money(remaining_target - new_amount)
            advance_redistributed = money(advance_redistributed + new_amount)
        refund_issued = await _advance_total(db, int(original.id))
    else:  # HOTEL_ADVANCE_STRATEGY_NONE
        for adv in adv_rows:
            await db.execute(
                text(
                    "UPDATE hotel_advance_payments "
                    "   SET refunded_amount = :refund, "
                    "       reference_number = COALESCE(reference_number, 'MANUAL_PENDING') "
                    " WHERE id = :id"
                ),
                {
                    "refund": Decimal(str(adv["amount"])),
                    "id": int(adv["id"]),
                },
            )
            refund_issued = money(refund_issued + Decimal(str(adv["amount"])))

    # Audit row.
    await db.execute(
        text(
            "INSERT INTO hotel_reservation_split_events ("
            "  original_reservation_id, new_reservation_id, split_type, "
            "  nights_transferred, original_nights_consumed, "
            "  original_final_amount, new_total_amount, "
            "  refund_issued, advance_redistributed, "
            "  advance_split_strategy, notes, created_by_user_id, created_at"
            ") VALUES ("
            "  :orig, :new, :stype, "
            "  :nights, :consumed, "
            "  :orig_amt, :new_amt, "
            "  :refund, :rollover, "
            "  :strategy, :notes, :actor, NOW()"
            ")"
        ),
        {
            "orig": int(original.id),
            "new": int(new_res_id),
            "stype": HOTEL_SPLIT_TYPE_PRE_CHECKIN_SWITCH,
            "nights": int((target_check_out - target_check_in).days),
            "consumed": 0,
            "orig_amt": _ZERO,  # whole original cancelled, no truncated bill.
            "new_amt": Decimal(str(q["total_amount"])),
            "refund": refund_issued,
            "rollover": advance_redistributed,
            "strategy": strategy,
            "notes": notes,
            "actor": str(actor_id) if actor_id else None,
        },
    )

    # Timeline events on each side.
    actor_str = str(actor_id) if actor_id else None
    await _log_timeline(
        db,
        int(original.master_booking_id),
        HOTEL_SWITCH_INITIATED,
        f"Hotel switch initiated from {original.reservation_number}",
        created_by=actor_str,
    )
    if strategy == HOTEL_ADVANCE_STRATEGY_ROLLOVER:
        await _log_timeline(
            db,
            int(original.master_booking_id),
            HOTEL_ADVANCE_ROLLOVER,
            f"Advance of {advance_redistributed} rolled over to new reservation {new_res_number}",
            created_by=actor_str,
        )
    else:
        await _log_timeline(
            db,
            int(original.master_booking_id),
            HOTEL_ADVANCE_REFUND_RECORDED,
            f"Advances marked for refund (amount {refund_issued}) — admin must record Razorpay ref",
            created_by=actor_str,
        )
    await _log_timeline(
        db,
        int(new_mb_id),
        HOTEL_SWITCH_COMPLETED,
        f"New hotel reservation {new_res_number} created via switch from {original.reservation_number}",
        created_by=actor_str,
    )

    # Roll the master totals up on both sides.
    await rollup_hotel_totals_into_master(db, int(original.master_booking_id))
    await rollup_hotel_totals_into_master(db, int(new_mb_id))

    new_res = (
        (
            await db.execute(
                text("SELECT * FROM hotel_reservations WHERE id = :id"),
                {"id": int(new_res_id)},
            )
        )
        .mappings()
        .one()
    )
    return _hydrate(new_res)


async def split_stay_after_partial_checkin(
    db: AsyncSession,
    original: HotelReservation,
    *,
    target_hotel_id: int,
    target_room_category_id: int,
    target_check_in: Optional[date] = None,
    target_check_out: Optional[date] = None,
    strategy: str,
    reason: str,
    actor_id: Optional[UUID],
    notes: Optional[str] = None,
    adults_count: int = 1,
    children_count: int = 0,
    extra_beds: int = 0,
) -> HotelReservation:
    """Post-checkin mid-stay split.

    The customer has already stayed some nights at the original. We:
      - Truncate the original's check_out_date and bill to the consumed nights
        (it stays in IN_HOUSE so the customer can check out normally).
      - Release inventory for the unconsumed nights.
      - Create a new reservation at the target hotel for [today+1, original.check_out].
      - Redistribute advances per `strategy`.
    """
    if strategy not in HOTEL_ADVANCE_STRATEGIES:
        raise ValidationException(
            f"Unknown advance strategy '{strategy}'. Allowed: {', '.join(HOTEL_ADVANCE_STRATEGIES)}"
        )
    status = str(original.reservation_status or "").upper()
    if status not in _POST_CHECKIN_STATUSES:
        raise ValidationException(
            f"Mid-stay split requires status in {sorted(_POST_CHECKIN_STATUSES)}, "
            f"got {status or 'UNKNOWN'}."
        )

    today = datetime.now(timezone.utc).date()
    anchor = (
        original.actual_check_in_at.date()
        if original.actual_check_in_at
        else original.check_in_date
    )
    consumed = max(0, (today - anchor).days)
    if consumed <= 0:
        raise ValidationException(
            "Cannot split a stay where no nights have been consumed yet."
        )
    if consumed >= int(original.nights or 0):
        raise ValidationException(
            "Every night of this stay is already consumed — nothing to split."
        )

    # Defaults: new reservation picks up where the original would have ended.
    if target_check_in is None:
        target_check_in = today + timedelta(days=1)
    if target_check_out is None:
        target_check_out = original.check_out_date
    if target_check_out <= target_check_in:
        raise ValidationException("target_check_out must be after target_check_in.")

    # Load target hotel.
    target = (
        (
            await db.execute(
                text("SELECT * FROM hotels WHERE id = :id AND deleted_at IS NULL"),
                {"id": target_hotel_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not target:
        raise ResourceNotFoundException("Target hotel", target_hotel_id)
    target_hotel = Hotel()
    for k in target.keys():
        setattr(target_hotel, k, target[k])

    q = await compute_hotel_quote(
        db,
        target_hotel,
        target_room_category_id,
        target_check_in,
        target_check_out,
        rooms_count=int(original.rooms_count or 1),
        adults_count=adults_count,
        children_count=children_count,
        extra_beds=extra_beds,
    )

    # Lock the original.
    locked = (
        await db.execute(
            text("SELECT id FROM hotel_reservations WHERE id = :id FOR UPDATE"),
            {"id": int(original.id)},
        )
    ).scalar_one_or_none()
    if locked is None:
        raise ResourceNotFoundException("Hotel reservation", int(original.id))

    # Recompute the truncated original bill: consumed nights only. We replace
    # `nights`, `room_nights`, `base_amount`, `taxable_amount`, `gst_amount`,
    # `total_amount`, `platform_commission`, `partner_payout`, `rate_snapshot`,
    # and `commission_config_snapshot` to match the consumed window — settlement
    # reads those columns verbatim.
    if target_check_in <= original.check_out_date:
        truncated_check_out = target_check_in  # original ends where new begins
    else:
        truncated_check_out = original.check_out_date

    nights_consumed = max(1, (truncated_check_out - anchor).days)
    original_hotel = (
        (
            await db.execute(
                text("SELECT * FROM hotels WHERE id = :id"),
                {"id": int(original.hotel_id)},
            )
        )
        .mappings()
        .one()
    )
    original_hotel_obj = Hotel()
    for k in original_hotel.keys():
        setattr(original_hotel_obj, k, original_hotel[k])

    # Build the consumed-nights quote against the ORIGINAL hotel/category so
    # settlement of the original half matches what was already billed.
    orig_q = await compute_hotel_quote(
        db,
        original_hotel_obj,
        int(original.room_category_id),
        anchor,
        truncated_check_out,
        rooms_count=int(original.rooms_count or 1),
        adults_count=int(original.adults_count or 1),
        children_count=int(original.children_count or 0),
        extra_beds=int(getattr(original, "extra_beds", 0) or 0),
    )

    # Release inventory on the original's unconsumed window.
    if truncated_check_out < original.check_out_date:
        await db.execute(
            text(
                "UPDATE hotel_inventory "
                "   SET booked_rooms    = GREATEST(booked_rooms - :rooms, 0), "
                "       available_rooms  = available_rooms + :rooms, "
                "       updated_at = NOW() "
                " WHERE room_category_id = :cid "
                "   AND inventory_date >= :dfrom AND inventory_date < :dto"
            ),
            {
                "rooms": int(original.rooms_count or 1),
                "cid": int(original.room_category_id),
                "dfrom": truncated_check_out,
                "dto": original.check_out_date,
            },
        )

    # Take inventory on the target window.
    try:
        await db.execute(
            text(
                "UPDATE hotel_inventory "
                "   SET booked_rooms   = booked_rooms + :rooms, "
                "       available_rooms = available_rooms - :rooms, "
                "       updated_at = NOW() "
                " WHERE room_category_id = :cid "
                "   AND inventory_date >= :dfrom AND inventory_date < :dto "
                "   AND is_stop_sell = FALSE"
            ),
            {
                "rooms": int(original.rooms_count or 1),
                "cid": target_room_category_id,
                "dfrom": target_check_in,
                "dto": target_check_out,
            },
        )
    except Exception:
        await db.rollback()
        raise BusinessException(
            "Not enough rooms available at the target hotel for the requested window."
        )

    import json as _json

    await db.execute(
        text(
            "UPDATE hotel_reservations SET "
            "  check_out_date = :cout, "
            "  nights = :nights, "
            "  room_nights = :room_nights, "
            "  base_amount = :base, "
            "  taxable_amount = :taxable, "
            "  gst_percent = :gst_pct, "
            "  gst_amount = :gst_amt, "
            "  total_amount = :total, "
            "  platform_commission = :commission, "
            "  partner_payout = :payout, "
            "  rate_snapshot = CAST(:rate_snap AS JSONB), "
            "  commission_config_snapshot = CAST(:comm_snap AS JSONB), "
            "  is_split_stay = TRUE, "
            "  switched_at = NOW(), "
            "  switched_by_user_id = :actor, "
            "  switched_reason = :reason, "
            "  split_advance_strategy = :strategy, "
            "  updated_at = NOW() "
            "WHERE id = :id"
        ),
        {
            "cout": truncated_check_out,
            "nights": int(orig_q["nights"]),
            "room_nights": int(orig_q["room_nights"]),
            "base": Decimal(str(orig_q["base_amount"])),
            "taxable": Decimal(str(orig_q["taxable_amount"])),
            "gst_pct": Decimal(str(orig_q["gst_percent"])),
            "gst_amt": Decimal(str(orig_q["gst_amount"])),
            "total": Decimal(str(orig_q["total_amount"])),
            "commission": Decimal(str(orig_q["platform_commission"])),
            "payout": Decimal(str(orig_q["partner_payout"])),
            "rate_snap": _json.dumps(orig_q["rate_snapshot"]),
            "comm_snap": _json.dumps(orig_q["commission_snapshot"]),
            "actor": str(actor_id) if actor_id else None,
            "reason": reason,
            "strategy": strategy,
            "id": int(original.id),
        },
    )

    # New reservation under a fresh master so each half settles cleanly.
    new_master_number = _new_master_booking_number()
    new_mb_id = (
        await db.execute(
            text(
                "INSERT INTO master_bookings ("
                "  uuid, booking_number, customer_id, city_id, "
                "  booking_status, payment_status, "
                "  total_amount, total_paid_amount, total_refund_amount, "
                "  journey_start_date, remarks, created_at, updated_at"
                ") VALUES ("
                "  :uuid, :bnum, :cid, :city_id, "
                "  'CONFIRMED', 'PENDING', "
                "  :amount, 0, 0, "
                "  :jdate, :remarks, NOW(), NOW()"
                ") RETURNING id"
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "bnum": new_master_number,
                "cid": original.customer_id,
                "city_id": (
                    int(target["city_id"]) if target["city_id"] is not None else None
                ),
                "amount": Decimal(str(q["total_amount"])),
                "jdate": target_check_in,
                "remarks": f"Mid-stay split from reservation {original.reservation_number}",
            },
        )
    ).scalar_one()

    new_svc_id = (
        await db.execute(
            text(
                "INSERT INTO booking_services ("
                "  master_booking_id, service_type, service_reference_id, "
                "  service_status, service_amount, created_at"
                ") VALUES (:mb, 'HOTEL', 0, 'CONFIRMED', :amount, NOW()) RETURNING id"
            ),
            {"mb": new_mb_id, "amount": Decimal(str(q["total_amount"]))},
        )
    ).scalar_one()

    new_res_number = _new_booking_number()
    new_res_id = (
        await db.execute(
            text(
                "INSERT INTO hotel_reservations ("
                "  uuid, master_booking_id, booking_service_id, hotel_id, room_category_id, "
                "  customer_id, reservation_number, "
                "  check_in_date, check_out_date, nights, rooms_count, room_nights, "
                "  adults_count, children_count, "
                "  base_amount, extra_charges, discount_amount, taxable_amount, "
                "  gst_percent, gst_amount, is_tax_invoice, total_amount, "
                "  platform_commission, partner_payout, "
                "  rate_snapshot, commission_config_snapshot, "
                "  reservation_status, special_requests, "
                "  is_split_stay, original_reservation_id, "
                "  switched_at, switched_by_user_id, switched_reason, split_advance_strategy, "
                "  confirmed_at, created_at, updated_at"
                ") VALUES ("
                "  :uuid, :mb, :svc, :hotel_id, :cat_id, "
                "  :cust_id, :res_num, "
                "  :cin, :cout, :nights, :rooms, :room_nights, "
                "  :adults, :children, "
                "  :base, 0, 0, :taxable, "
                "  :gst_pct, :gst_amt, :is_tax_inv, :total, "
                "  :commission, :payout, "
                "  CAST(:rate_snap AS JSONB), CAST(:comm_snap AS JSONB), "
                "  'CONFIRMED', :special, "
                "  TRUE, :orig_id, "
                "  NOW(), :actor, :reason, :strategy, "
                "  NOW(), NOW(), NOW()"
                ") RETURNING id"
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "mb": new_mb_id,
                "svc": new_svc_id,
                "hotel_id": target_hotel_id,
                "cat_id": target_room_category_id,
                "cust_id": original.customer_id,
                "res_num": new_res_number,
                "cin": target_check_in,
                "cout": target_check_out,
                "nights": int(q["nights"]),
                "rooms": int(original.rooms_count or 1),
                "room_nights": int(q["room_nights"]),
                "adults": adults_count,
                "children": children_count,
                "base": Decimal(str(q["base_amount"])),
                "taxable": Decimal(str(q["taxable_amount"])),
                "gst_pct": Decimal(str(q["gst_percent"])),
                "gst_amt": Decimal(str(q["gst_amount"])),
                "is_tax_inv": bool(q["is_tax_invoice"]),
                "total": Decimal(str(q["total_amount"])),
                "commission": Decimal(str(q["platform_commission"])),
                "payout": Decimal(str(q["partner_payout"])),
                "rate_snap": _json.dumps(q["rate_snapshot"]),
                "comm_snap": _json.dumps(q["commission_snapshot"]),
                "special": notes,
                "orig_id": int(original.id),
                "actor": str(actor_id) if actor_id else None,
                "reason": reason,
                "strategy": strategy,
            },
        )
    ).scalar_one()

    await db.execute(
        text("UPDATE booking_services SET service_reference_id = :rid WHERE id = :sid"),
        {"rid": new_res_id, "sid": new_svc_id},
    )

    # Advance redistribution against the new (target) total.
    refund_issued = _ZERO
    advance_redistributed = _ZERO
    adv_rows = (
        (
            await db.execute(
                text(
                    "SELECT id, amount, COALESCE(refunded_amount, 0) AS refunded_amount "
                    "FROM hotel_advance_payments "
                    "WHERE hotel_reservation_id = :rid AND status = 'ACTIVE' "
                    "ORDER BY collected_at ASC"
                ),
                {"rid": int(original.id)},
            )
        )
        .mappings()
        .all()
    )

    if strategy == HOTEL_ADVANCE_STRATEGY_ROLLOVER:
        remaining_target = Decimal(str(q["total_amount"]))
        for adv in adv_rows:
            available = Decimal(str(adv["amount"])) - Decimal(
                str(adv["refunded_amount"])
            )
            if available <= _ZERO or remaining_target <= _ZERO:
                continue
            move = min(available, remaining_target)
            await db.execute(
                text(
                    "UPDATE hotel_advance_payments SET refunded_amount = :refund WHERE id = :id"
                ),
                {"refund": money(available), "id": int(adv["id"])},
            )
            await db.execute(
                text(
                    "INSERT INTO hotel_advance_payments ("
                    "  hotel_reservation_id, master_booking_id, receipt_number, "
                    "  amount, payment_mode, received_by, reference_number, notes, "
                    "  status, refunded_amount, collected_at, created_at"
                    ") VALUES ("
                    "  :rid, :mb, :rcpt, :amount, :mode, :by, :ref, :notes, "
                    "  'ACTIVE', 0, NOW(), NOW()"
                    ")"
                ),
                {
                    "rid": int(new_res_id),
                    "mb": int(new_mb_id),
                    "rcpt": await next_receipt_number(db),
                    "amount": move,
                    "mode": "ROLLOVER",
                    "by": "ADMIN",
                    "ref": f"ROLLOVER-FROM-{int(original.id)}",
                    "notes": f"Post-checkin rollover from advance {int(adv['id'])} of {original.reservation_number}",
                },
            )
            remaining_target = money(remaining_target - move)
            advance_redistributed = money(advance_redistributed + move)
        refund_issued = await _advance_total(db, int(original.id))
    else:
        for adv in adv_rows:
            await db.execute(
                text(
                    "UPDATE hotel_advance_payments "
                    "   SET refunded_amount = :refund, "
                    "       reference_number = COALESCE(reference_number, 'MANUAL_PENDING') "
                    " WHERE id = :id"
                ),
                {"refund": Decimal(str(adv["amount"])), "id": int(adv["id"])},
            )
            refund_issued = money(refund_issued + Decimal(str(adv["amount"])))

    # Audit row.
    await db.execute(
        text(
            "INSERT INTO hotel_reservation_split_events ("
            "  original_reservation_id, new_reservation_id, split_type, "
            "  nights_transferred, original_nights_consumed, "
            "  original_final_amount, new_total_amount, "
            "  refund_issued, advance_redistributed, "
            "  advance_split_strategy, notes, created_by_user_id, created_at"
            ") VALUES ("
            "  :orig, :new, :stype, "
            "  :nights, :consumed, "
            "  :orig_amt, :new_amt, "
            "  :refund, :rollover, "
            "  :strategy, :notes, :actor, NOW()"
            ")"
        ),
        {
            "orig": int(original.id),
            "new": int(new_res_id),
            "stype": HOTEL_SPLIT_TYPE_POST_CHECKIN_SPLIT,
            "nights": int((target_check_out - target_check_in).days),
            "consumed": int(nights_consumed),
            "orig_amt": Decimal(str(orig_q["total_amount"])),
            "new_amt": Decimal(str(q["total_amount"])),
            "refund": refund_issued,
            "rollover": advance_redistributed,
            "strategy": strategy,
            "notes": notes,
            "actor": str(actor_id) if actor_id else None,
        },
    )

    actor_str = str(actor_id) if actor_id else None
    await _log_timeline(
        db,
        int(original.master_booking_id),
        HOTEL_SPLIT_INITIATED,
        f"Mid-stay split initiated on {original.reservation_number} ({nights_consumed} nights consumed)",
        created_by=actor_str,
    )
    if strategy == HOTEL_ADVANCE_STRATEGY_ROLLOVER:
        await _log_timeline(
            db,
            int(original.master_booking_id),
            HOTEL_ADVANCE_ROLLOVER,
            f"Advance of {advance_redistributed} rolled over to {new_res_number}",
            created_by=actor_str,
        )
    else:
        await _log_timeline(
            db,
            int(original.master_booking_id),
            HOTEL_ADVANCE_REFUND_RECORDED,
            f"Advances marked for refund (amount {refund_issued}) — admin must record Razorpay ref",
            created_by=actor_str,
        )
    await _log_timeline(
        db,
        int(new_mb_id),
        HOTEL_SPLIT_COMPLETED,
        f"New reservation {new_res_number} created via mid-stay split from {original.reservation_number}",
        created_by=actor_str,
    )

    await rollup_hotel_totals_into_master(db, int(original.master_booking_id))
    await rollup_hotel_totals_into_master(db, int(new_mb_id))

    new_res = (
        (
            await db.execute(
                text("SELECT * FROM hotel_reservations WHERE id = :id"),
                {"id": int(new_res_id)},
            )
        )
        .mappings()
        .one()
    )
    return _hydrate(new_res)


def money(value: Decimal) -> Decimal:
    """Money helper — round half-up to 2 dp."""
    from decimal import ROUND_HALF_UP

    if value is None:
        return _ZERO
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


__all__ = [
    "quote_switch",
    "switch_hotel_pre_checkin",
    "split_stay_after_partial_checkin",
]
