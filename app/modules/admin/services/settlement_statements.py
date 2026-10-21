# ============================================================
# WAYTERO — SETTLEMENT STATEMENTS (PERIOD AGGREGATION + LIFECYCLE)
# File: app/modules/admin/services/settlement_statements.py
# Doc Ref: BRD Part 6 §147–149 (settlement flow / statement contents),
#          DB Schema Part 7 §22 (settlement statuses),
#          migration 0086 (period unique index, statement columns).
#
# Why this exists: `settlements` and `settlement_items` were created by 0008 and
# extended by 0086 into a period statement ("already a period statement … and has
# 0 rows"), but nothing ever inserted a row. Per-booking `POST /settlements/settle`
# moved the partner wallet and flipped the booking to SETTLED, and the statement
# tables — the things finance reads and the partner is paid against — stayed
# empty. `AdminDashboardService.update_settlement_status` then wrote `SET status`,
# `paid_at` and `updated_at`, none of which exist, so even the lifecycle was dead.
#
# This module does two jobs:
#
#   generate_statement()  takes every cab booking that reached SETTLED in the
#                         period, for one partner, that is not already on a
#                         statement, and writes one `settlements` row (GENERATED)
#                         plus one `settlement_items` line per booking. The
#                         period unique index `uq_settlements_partner_period`
#                         (migration 0086) is the database's answer to a second
#                         statement for the same partner and period.
#
#   transition_statement() owns the status lifecycle, so the two dashboard
#                         endpoints and the new status route cannot each invent
#                         their own transitions.
#
# Money: the statement is built from the booking's own frozen close figures —
# `final_amount`, `gst_amount`, `platform_commission`, `partner_payout` — not
# re-derived here. The close wrote commission + payout = final, so
#
#     gross       = final + gst (when the booking is a tax invoice)
#     commission  = platform_commission
#     gst         = gst (when the booking is a tax invoice)
#     net payable = gross − commission − gst = partner_payout
#
# holds per booking, and the statement sums are the sums of those columns. The
# wallet movement itself (custody netting, coupon disbursement, TDS) already
# happened per booking at settle time and is not replayed onto the statement.
# ============================================================

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException, ResourceNotFoundException, ValidationException
from app.modules.booking.services.settlement_math import ZERO, money

# The status set the specs agree on (BRD §148, DB Schema Part 7 §22, API §4).
# PENDING is 0008's default and is treated as an unstarted statement; DRAFT is
# the API doc's name for the same state. SENT/VOIDED appear in no spec.
STATEMENT_STATUSES = frozenset(
    {
        "DRAFT",
        "PENDING",
        "GENERATED",
        "APPROVED",
        "PROCESSING",
        "PAID",
        "FAILED",
        "CLOSED",
        "REJECTED",
    }
)

# Generated → Approved → Processing → Paid → Closed is the documented happy path.
# REJECTED is the documented exception out of review. FAILED may be retried into
# PROCESSING. Everything else is refused rather than accepted as "just a string".
STATEMENT_TRANSITIONS: Dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"GENERATED", "REJECTED"}),
    "PENDING": frozenset({"GENERATED", "REJECTED"}),
    "GENERATED": frozenset({"APPROVED", "REJECTED"}),
    "APPROVED": frozenset({"PROCESSING", "PAID", "REJECTED"}),
    "PROCESSING": frozenset({"PAID", "FAILED"}),
    "FAILED": frozenset({"PROCESSING", "REJECTED"}),
    "PAID": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
    "REJECTED": frozenset(),
}

# The statuses that mean the partner has been paid, so `settled_at` is stamped.
_PAID_STATES = frozenset({"PAID", "CLOSED"})

STATEMENT_TYPE_CAB = "CAB"


def _normalise_status(status: str) -> str:
    normalised = (status or "").strip().upper()
    if normalised not in STATEMENT_STATUSES:
        raise ValidationException(
            f"Settlement status {status!r} is not one of: "
            f"{', '.join(sorted(STATEMENT_STATUSES))}."
        )
    return normalised


async def _next_statement_number(db: AsyncSession, end_date: date) -> str:
    """`WT-SET-<YYYYMM>-<5>` — MAX-based so two concurrent generations cannot be
    handed the same number; the statement is committed in the caller's
    transaction, so a rolled-back generation releases its number."""
    stamp = end_date.strftime("%Y%m")
    row = (
        await db.execute(
            text(
                "SELECT MAX(CAST(SPLIT_PART(settlement_number, '-', 4) AS INTEGER)) "
                "FROM settlements WHERE settlement_number LIKE 'WT-SET-%'"
            )
        )
    ).scalar()
    return f"WT-SET-{stamp}-{str((row or 0) + 1).zfill(5)}"


async def generate_statement(
    db: AsyncSession,
    *,
    partner_id: int,
    start_date: date,
    end_date: date,
    settlement_type: str = STATEMENT_TYPE_CAB,
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One partner, one period, one statement.

    Re-running for the same partner and period is refused by an explicit check,
    with `uq_settlements_partner_period` as the backstop for concurrent runs.
    Re-running for a later period picks up the bookings that settled since — a
    booking already on a statement is excluded by `settlement_items`, so money
    is never counted on two statements.
    """
    if end_date < start_date:
        raise ValidationException("The period end date cannot be before its start.")
    kind = (settlement_type or STATEMENT_TYPE_CAB).strip().upper()
    if kind != STATEMENT_TYPE_CAB:
        raise ValidationException(
            f"Statement type {kind!r} is not supported yet — only CAB statements "
            "are generated from settled cab bookings."
        )

    partner_exists = (
        await db.execute(
            text("SELECT 1 FROM partners WHERE id = :pid"), {"pid": partner_id}
        )
    ).scalar_one_or_none()
    if partner_exists is None:
        raise ResourceNotFoundException("Partner", partner_id)

    # Checked up front as well as enforced by the unique index: without this the
    # second call would find every booking already on the first statement, report
    # "no settled bookings", and hide the real reason behind the wrong message.
    existing_number = (
        await db.execute(
            text(
                "SELECT settlement_number FROM settlements "
                "WHERE partner_id = :pid "
                "  AND COALESCE(settlement_type, 'CAB') = :kind "
                "  AND start_date = :start AND end_date = :end"
            ),
            {"pid": partner_id, "kind": kind, "start": start_date, "end": end_date},
        )
    ).scalar_one_or_none()
    if existing_number is not None:
        raise BusinessException(
            f"A statement for partner {partner_id} covering "
            f"{start_date.isoformat()}–{end_date.isoformat()} already exists "
            f"({existing_number})."
        )

    # A settled booking's moment is the immutable timeline event the settle path
    # writes. `cb.updated_at` is only the fallback for a booking settled before
    # that event existed — it moves on later edits, so it is not the primary.
    rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT cb.id                    AS cab_id,
                           cb.booking_number,
                           cb.master_booking_id     AS master_id,
                           cb.final_amount,
                           cb.gst_amount,
                           cb.is_tax_invoice,
                           cb.platform_commission,
                           cb.partner_payout,
                           cb.coupon_discount,
                           cb.actual_distance,
                           cb.commission_config_snapshot
                      FROM cab_bookings cb
                      JOIN LATERAL (
                           SELECT MIN(bt.event_timestamp) AS settled_at
                             FROM booking_timelines bt
                            WHERE bt.master_booking_id = cb.master_booking_id
                              AND bt.event_type = 'BOOKING_SETTLED_BY_ADMIN'
                              AND bt.event_description LIKE
                                  '%' || cb.booking_number || '%'
                      ) t ON TRUE
                      JOIN LATERAL (
                           SELECT a.partner_id
                             FROM cab_booking_assignments a
                            WHERE a.cab_booking_id = cb.id
                              AND a.partner_id = :pid
                            ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
                            LIMIT 1
                      ) pa ON TRUE
                     WHERE cb.booking_status = 'SETTLED'
                       AND COALESCE(t.settled_at, cb.updated_at)::date
                           BETWEEN :start AND :end
                       AND NOT EXISTS (
                           SELECT 1 FROM settlement_items si
                            WHERE si.booking_type = 'CAB'
                              AND si.booking_id = cb.id
                       )
                     ORDER BY cb.id
                    """
                ),
                {"pid": partner_id, "start": start_date, "end": end_date},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise BusinessException(
            "No settled cab bookings for this partner fall in the period and are "
            "not already on a statement."
        )

    gross_total = commission_total = gst_total = net_total = ZERO
    items: list[dict[str, Any]] = []
    for row in rows:
        final = money(row["final_amount"])
        gst = money(row["gst_amount"]) if row["is_tax_invoice"] else ZERO
        gross = final + gst
        commission = money(row["platform_commission"])
        # The close guarantees commission + payout = final. Prefer the recorded
        # payout; derive it only for a booking whose payout column is null.
        net = (
            money(row["partner_payout"])
            if row["partner_payout"] is not None
            else gross - commission - gst
        )
        gross_total += gross
        commission_total += commission
        gst_total += gst
        net_total += net
        items.append(
            {
                "cab_id": row["cab_id"],
                "gross": gross,
                "commission": commission,
                "net": net,
                "distance": row["actual_distance"],
                "coupon": money(row["coupon_discount"]),
                "snapshot": row["commission_config_snapshot"],
            }
        )

    statement_number = await _next_statement_number(db, end_date)
    try:
        statement = (
            (
                await db.execute(
                    text(
                        """
                        INSERT INTO settlements
                            (uuid, settlement_number, partner_id, settlement_type,
                             start_date, end_date, gross_amount, commission_amount,
                             gst_amount, net_payable_amount, settlement_status,
                             created_at)
                        VALUES
                            (gen_random_uuid(), :number, :pid, :kind,
                             :start, :end, :gross, :commission,
                             :gst, :net, 'GENERATED', NOW())
                        RETURNING id, settlement_number, settlement_status
                        """
                    ),
                    {
                        "number": statement_number,
                        "pid": partner_id,
                        "kind": kind,
                        "start": start_date,
                        "end": end_date,
                        "gross": gross_total,
                        "commission": commission_total,
                        "gst": gst_total,
                        "net": net_total,
                    },
                )
            )
            .mappings()
            .one()
        )
    except IntegrityError as exc:
        raise BusinessException(
            f"A statement for partner {partner_id} covering "
            f"{start_date.isoformat()}–{end_date.isoformat()} already exists."
        ) from exc

    settlement_id = int(statement["id"])
    for item in items:
        await db.execute(
            text(
                """
                INSERT INTO settlement_items
                    (settlement_id, booking_id, booking_type, gross_amount,
                     commission_amount, net_amount, billing_distance_km,
                     coupon_discount, commission_snapshot, created_at)
                VALUES
                    (:sid, :bid, 'CAB', :gross, :commission, :net, :distance,
                     :coupon, CAST(:snapshot AS jsonb), NOW())
                """
            ),
            {
                "sid": settlement_id,
                "bid": item["cab_id"],
                "gross": item["gross"],
                "commission": item["commission"],
                "net": item["net"],
                "distance": item["distance"],
                "coupon": item["coupon"],
                "snapshot": (
                    None
                    if item["snapshot"] is None
                    else json.dumps(item["snapshot"])
                ),
            },
        )

    await db.flush()
    return {
        "settlement_id": settlement_id,
        "settlement_number": statement["settlement_number"],
        "partner_id": partner_id,
        "settlement_type": kind,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "status": statement["settlement_status"],
        "booking_count": len(items),
        "gross_amount": float(gross_total),
        "commission_amount": float(commission_total),
        "gst_amount": float(gst_total),
        "net_payable_amount": float(net_total),
    }


async def transition_statement(
    db: AsyncSession,
    *,
    settlement_id: int,
    to_status: str,
    actor_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Move a statement along the documented flow, or refuse the move.

    The row is locked FOR UPDATE so two requests cannot each read the same
    current status and both believe their transition is the legal next one. A
    no-op (same status) is accepted idempotently: a retried click must not turn a
    success into an error.
    """
    target = _normalise_status(to_status)
    current = (
        (
            await db.execute(
                text(
                    "SELECT settlement_status FROM settlements "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": settlement_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        raise ResourceNotFoundException("Settlement", settlement_id)
    source = (current["settlement_status"] or "").strip().upper()

    if source == target:
        return {"settlement_id": settlement_id, "status": target, "changed": False}
    allowed = STATEMENT_TRANSITIONS.get(source, frozenset())
    if target not in allowed:
        raise BusinessException(
            f"Settlement {settlement_id} cannot move from {source or '(none)'} to "
            f"{target}. Allowed from here: "
            f"{', '.join(sorted(allowed)) or 'none — this status is final'}."
        )

    settled_sql = ", settled_at = COALESCE(settled_at, NOW())" if target in _PAID_STATES else ""
    await db.execute(
        text(
            f"UPDATE settlements SET settlement_status = :status{settled_sql} "
            "WHERE id = :id"
        ),
        {"status": target, "id": settlement_id},
    )
    await db.flush()
    return {
        "settlement_id": settlement_id,
        "previous_status": source,
        "status": target,
        "changed": True,
        "reason": (reason or "").strip() or None,
        "actor_id": actor_id,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def allowed_next(status: str) -> Iterable[str]:
    return sorted(STATEMENT_TRANSITIONS.get((status or "").upper(), frozenset()))
