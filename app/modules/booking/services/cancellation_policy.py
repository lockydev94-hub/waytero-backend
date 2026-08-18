"""Cancellation policy engine.

Doc Ref:
  BRD Part 3 §46/§47 (Cab)
  BRD Part 4 §82 (Hotel)
  SRS Part 3 §99/§100
  SRS Part 5 §184/§185

This is the single source of truth for "given a booking or reservation and
a moment in time, what is the cancellation charge and refund amount?".

Two policy sources are supported:

  * Cab — global ladder stored in `system_configurations` (keys seeded by
    migration 0043). Per-cab there is no policy table; all cab bookings
    share the same ladder, with an `after-assignment` carve-out because
    once a partner has been notified the partner-side cost is real.

  * Hotel — per-hotel ladder stored in `hotel_policies`. Hotel owners
    edit the four tiers via the partner portal (already exists); admins
    can read it but not override per-booking.

In both cases we always return a `policy_snapshot` dict so the caller can
persist it onto the cancellation row (auditability: even if the policy
is later edited, the row records exactly what rule applied).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ════════════════════════════════════════════════════════════════
# Result type
# ════════════════════════════════════════════════════════════════


@dataclass
class CancellationQuote:
    """All the numbers + the rule that produced them.

    `charge` and `refund_amount` always satisfy: charge + refund == base
    unless the base was partial-paid (e.g. advance only) — in which case
    refund ≤ advance and the customer owes no further amount.
    """

    charge: Decimal
    refund_amount: Decimal
    refund_percent: Decimal
    tier_label: str
    policy_snapshot: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "charge": float(self.charge),
            "refund_amount": float(self.refund_amount),
            "refund_percent": float(self.refund_percent),
            "tier_label": self.tier_label,
            "policy_snapshot": self.policy_snapshot,
        }


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════


def _q2(x: Decimal) -> Decimal:
    """Quantize to 2 decimal places, banker-safe."""
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _diff_hours(now: datetime, target: datetime) -> Decimal:
    """Hours from `now` until `target`. Negative = past."""
    return Decimal((target - now).total_seconds()) / Decimal("3600")


async def _config_int(db: AsyncSession, key: str, default: int) -> int:
    row = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": key},
        )
    ).first()
    if not row:
        return default
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return default


async def _config_decimal(db: AsyncSession, key: str, default: Decimal) -> Decimal:
    row = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": key},
        )
    ).first()
    if not row:
        return default
    try:
        return Decimal(str(row[0]))
    except Exception:
        return default


# ════════════════════════════════════════════════════════════════
# Cab ladder — global, read from system_configurations
# ════════════════════════════════════════════════════════════════


async def quote_cab_cancellation(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    cab_total_amount: Decimal,
    advance_paid_total: Decimal,
    is_post_assignment: bool,
    now: Optional[datetime] = None,
) -> CancellationQuote:
    """Compute the cancellation charge + refund for a cab booking.

    Rules (in order of precedence — first match wins):

      A. `is_post_assignment` is True and the partner has already started
         preparing → apply AFTER_ASSIGNMENT_PERCENT_CAB regardless of
         time-to-pickup. Rationale: the partner has opportunity cost we
         can't reverse.
      B. `now >= pickup_time` → SAME_DAY (which means "0% past pickup" in
         practice; we treat it as no-refund once the trip has begun).
      C. hours_to_pickup >= CANCELLATION_FREE_HOURS_CAB → 100% refund.
      D. hours_to_pickup >= CANCELLATION_TIER_1_HOURS_CAB → TIER_1.
      E. hours_to_pickup >= CANCELLATION_TIER_2_HOURS_CAB → TIER_2.
      F. otherwise → SAME_DAY_PERCENT_CAB (which is 0 by default).
    """
    now = now or datetime.now(timezone.utc)

    pickup_row = (
        await db.execute(
            # CabBooking stores pickup time as `pickup_datetime` (DB Schema §7).
            # Earlier revisions of this query used `pickup_at`, which never
            # existed on cab_bookings and would 500 every refund preview.
            text("SELECT pickup_datetime FROM cab_bookings WHERE id = :id"),
            {"id": cab_booking_id},
        )
    ).first()
    pickup_at = pickup_row[0] if pickup_row else None
    if pickup_at is not None and pickup_at.tzinfo is None:
        pickup_at = pickup_at.replace(tzinfo=timezone.utc)

    # Read all 7 knobs in one round-trip.
    cfg = (
        (
            await db.execute(
                text(
                    """
                SELECT config_key, config_value FROM system_configurations
                WHERE config_key IN (
                    'CANCELLATION_FREE_HOURS_CAB',
                    'CANCELLATION_TIER_1_HOURS_CAB',
                    'CANCELLATION_TIER_1_PERCENT_CAB',
                    'CANCELLATION_TIER_2_HOURS_CAB',
                    'CANCELLATION_TIER_2_PERCENT_CAB',
                    'CANCELLATION_SAME_DAY_PERCENT_CAB',
                    'CANCELLATION_AFTER_ASSIGNMENT_PERCENT_CAB'
                )
                """
                )
            )
        )
        .mappings()
        .all()
    )
    by_key = {r["config_key"]: r["config_value"] for r in cfg}

    def _int(key: str, default: int) -> int:
        v = by_key.get(key)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _dec(key: str, default: Decimal) -> Decimal:
        v = by_key.get(key)
        try:
            return Decimal(str(v)) if v is not None else default
        except Exception:
            return default

    free_h = _int("CANCELLATION_FREE_HOURS_CAB", 2)
    t1_h = _int("CANCELLATION_TIER_1_HOURS_CAB", 12)
    t1_p = _dec("CANCELLATION_TIER_1_PERCENT_CAB", Decimal("75"))
    t2_h = _int("CANCELLATION_TIER_2_HOURS_CAB", 4)
    t2_p = _dec("CANCELLATION_TIER_2_PERCENT_CAB", Decimal("50"))
    same_day_p = _dec("CANCELLATION_SAME_DAY_PERCENT_CAB", Decimal("0"))
    after_assign_p = _dec("CANCELLATION_AFTER_ASSIGNMENT_PERCENT_CAB", Decimal("50"))

    hours_to_pickup: Optional[Decimal] = None
    if pickup_at is not None:
        hours_to_pickup = _diff_hours(now, pickup_at)

    # A. post-assignment rule.
    if is_post_assignment:
        refund_percent = after_assign_p
        tier_label = "post_assignment"
    # B. past pickup → same-day no-refund.
    elif pickup_at is not None and hours_to_pickup is not None and hours_to_pickup <= 0:
        refund_percent = same_day_p
        tier_label = "past_pickup"
    # C. free window.
    elif hours_to_pickup is not None and hours_to_pickup >= free_h:
        refund_percent = Decimal("100")
        tier_label = "free_window"
    # D. tier 1.
    elif hours_to_pickup is not None and hours_to_pickup >= t1_h:
        refund_percent = t1_p
        tier_label = "tier_1"
    # E. tier 2.
    elif hours_to_pickup is not None and hours_to_pickup >= t2_h:
        refund_percent = t2_p
        tier_label = "tier_2"
    # F. last-minute.
    else:
        refund_percent = same_day_p
        tier_label = "last_minute"

    refund_amount_raw = _q2(cab_total_amount * refund_percent / Decimal("100"))
    # The customer cannot be refunded more than they actually paid.
    refund_amount = min(refund_amount_raw, _q2(advance_paid_total))
    charge = _q2(cab_total_amount - refund_amount)

    snapshot = {
        "source": "global_cab_ladder",
        "policy_version": "system_config:v1",
        "keys": {
            "free_hours": free_h,
            "tier_1_hours": t1_h,
            "tier_1_percent": float(t1_p),
            "tier_2_hours": t2_h,
            "tier_2_percent": float(t2_p),
            "same_day_percent": float(same_day_p),
            "after_assignment_percent": float(after_assign_p),
        },
        "inputs": {
            "cab_booking_id": cab_booking_id,
            "cab_total_amount": float(cab_total_amount),
            "advance_paid_total": float(advance_paid_total),
            "is_post_assignment": bool(is_post_assignment),
            "pickup_at": pickup_at.isoformat() if pickup_at else None,
            "hours_to_pickup": (
                float(hours_to_pickup) if hours_to_pickup is not None else None
            ),
            "evaluated_at": now.isoformat(),
        },
        "tier_label": tier_label,
    }

    return CancellationQuote(
        charge=charge,
        refund_amount=refund_amount,
        refund_percent=refund_percent,
        tier_label=tier_label,
        policy_snapshot=snapshot,
    )


# ════════════════════════════════════════════════════════════════
# Hotel ladder — per-hotel, read from hotel_policies
# ════════════════════════════════════════════════════════════════


async def quote_hotel_cancellation(
    db: AsyncSession,
    *,
    hotel_reservation_id: int,
    hotel_total_amount: Decimal,
    advance_paid_total: Decimal,
    is_no_show: bool = False,
    now: Optional[datetime] = None,
) -> CancellationQuote:
    """Compute the cancellation charge + refund for a hotel reservation.

    The ladder is per-hotel, taken from `hotel_policies`:

      hours_to_checkin >= cancellation_free_hours       → 100%
      hours_to_checkin >= cancellation_tier_2_hours      → refund_percent_tier_2
      hours_to_checkin >= cancellation_tier_3_hours      → refund_percent_tier_3
      past check-in / same-day                          → refund_percent_same_day
      no_show path (caller signals)                     → no_show_refund_percent

    If the hotel has no policy row yet, we fall back to BRD §82 defaults:
    72h/100, 48h/75, 24h/50, same-day/0, no-show/0.
    """
    now = now or datetime.now(timezone.utc)

    row = (
        (
            await db.execute(
                text(
                    """
                SELECT hr.check_in_date, hp.cancellation_free_hours,
                       hp.refund_percent_tier_1, hp.cancellation_tier_2_hours,
                       hp.refund_percent_tier_2, hp.cancellation_tier_3_hours,
                       hp.refund_percent_tier_3, hp.refund_percent_same_day,
                       hp.no_show_refund_percent, hp.cancellation_policy_text
                FROM hotel_reservations hr
                LEFT JOIN hotels h ON h.id = hr.hotel_id
                LEFT JOIN hotel_policies hp ON hp.hotel_id = h.id
                WHERE hr.id = :id
                """
                ),
                {"id": hotel_reservation_id},
            )
        )
        .mappings()
        .first()
    )

    if not row:
        raise ValueError(f"hotel_reservation {hotel_reservation_id} not found")

    check_in = row["check_in_date"]
    if check_in is not None and check_in.tzinfo is None:
        check_in = check_in.replace(tzinfo=timezone.utc)
    hours_to_checkin = _diff_hours(now, check_in) if check_in else None

    free_h = row["cancellation_free_hours"] or 72
    t2_h = row["cancellation_tier_2_hours"] or 48
    t3_h = row["cancellation_tier_3_hours"] or 24
    p1 = Decimal(str(row["refund_percent_tier_1"] or 100))
    p2 = Decimal(str(row["refund_percent_tier_2"] or 75))
    p3 = Decimal(str(row["refund_percent_tier_3"] or 50))
    p_same = Decimal(str(row["refund_percent_same_day"] or 0))
    p_noshow = Decimal(str(row["no_show_refund_percent"] or 0))

    if is_no_show:
        refund_percent = p_noshow
        tier_label = "no_show"
    elif hours_to_checkin is None:
        # No check-in date set; behave as last-minute.
        refund_percent = p_same
        tier_label = "no_checkin_date"
    elif hours_to_checkin >= free_h:
        refund_percent = p1
        tier_label = "free_window"
    elif hours_to_checkin >= t2_h:
        refund_percent = p2
        tier_label = "tier_2"
    elif hours_to_checkin >= t3_h:
        refund_percent = p3
        tier_label = "tier_3"
    else:
        refund_percent = p_same
        tier_label = "same_day"

    refund_amount_raw = _q2(hotel_total_amount * refund_percent / Decimal("100"))
    refund_amount = min(refund_amount_raw, _q2(advance_paid_total))
    charge = _q2(hotel_total_amount - refund_amount)

    snapshot = {
        "source": "hotel_policy",
        "policy_version": "hotel_policies:v1",
        "keys": {
            "free_hours": free_h,
            "tier_2_hours": t2_h,
            "tier_3_hours": t3_h,
            "tier_1_percent": float(p1),
            "tier_2_percent": float(p2),
            "tier_3_percent": float(p3),
            "same_day_percent": float(p_same),
            "no_show_percent": float(p_noshow),
            "policy_text": row["cancellation_policy_text"],
        },
        "inputs": {
            "hotel_reservation_id": hotel_reservation_id,
            "hotel_total_amount": float(hotel_total_amount),
            "advance_paid_total": float(advance_paid_total),
            "is_no_show": bool(is_no_show),
            "check_in_date": check_in.isoformat() if check_in else None,
            "hours_to_checkin": (
                float(hours_to_checkin) if hours_to_checkin is not None else None
            ),
            "evaluated_at": now.isoformat(),
        },
        "tier_label": tier_label,
    }

    return CancellationQuote(
        charge=charge,
        refund_amount=refund_amount,
        refund_percent=refund_percent,
        tier_label=tier_label,
        policy_snapshot=snapshot,
    )


# ════════════════════════════════════════════════════════════════
# Tour ladder — global, read from system_configurations
# Doc Ref: BRD Part 5 §119 — TOUR CANCELLATION POLICY
# ════════════════════════════════════════════════════════════════


async def quote_tour_cancellation(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    tour_total_amount: Decimal,
    advance_paid_total: Decimal,
    now: Optional[datetime] = None,
) -> CancellationQuote:
    """Compute the cancellation charge + refund for a tour booking.

    The ladder is per-package when the tour package carries a policy row
    (`tour_package_policies`, admin/partner editable), otherwise the global
    ladder from `system_configurations` (seeded by migration 0060) applies.
    Both match BRD Part 5 §119:

      days_to_travel >= FREE_DAYS (30)  → 100% refund
      days_to_travel >= TIER_1_DAYS(15) → 75%
      days_to_travel >= TIER_2_DAYS(7)  → 50%
      otherwise (incl. last-minute)     → LAST_MINUTE percent (0)

    Refund is capped at the live advance total — the customer can never get
    back more than they actually paid. `charge` is the retained amount.
    The snapshot records which source (package policy vs global ladder)
    produced the quote, so the audit trail stays accurate even after the
    ladder moves.
    """
    now = now or datetime.now(timezone.utc)

    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT tb.travel_start_date, tb.booking_status,
                           tp.id AS package_id,
                           tpp.cancellation_free_days,
                           tpp.cancellation_tier_1_days,
                           tpp.refund_percent_tier_1,
                           tpp.cancellation_tier_2_days,
                           tpp.refund_percent_tier_2,
                           tpp.refund_percent_tier_3,
                           tpp.refund_percent_last_minute,
                           tpp.cancellation_policy_text
                    FROM tour_bookings tb
                    LEFT JOIN tour_packages tp ON tp.id = tb.package_id
                    LEFT JOIN tour_package_policies tpp
                           ON tpp.tour_package_id = tp.id
                    WHERE tb.id = :id
                    """
                ),
                {"id": tour_booking_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise ValueError(f"tour_booking {tour_booking_id} not found")

    travel_start = row["travel_start_date"]
    # travel_start_date is a Postgres DATE (returns datetime.date). Normalise
    # a datetime (if any driver ever stores one) to a date so the day delta
    # is always date - date — date.replace(tzinfo=...) would crash.
    if isinstance(travel_start, datetime):
        if travel_start.tzinfo is None:
            travel_start = travel_start.replace(tzinfo=timezone.utc)
        travel_date = travel_start.date()
    else:
        travel_date = travel_start
    days_to_travel = (
        (travel_date - now.date()).days if travel_date is not None else None
    )

    # Per-package policy wins when present; otherwise the global ladder.
    if row["cancellation_free_days"] is not None:
        free_days = int(row["cancellation_free_days"])
        t1_days = int(row["cancellation_tier_1_days"] or free_days)
        t2_days = int(row["cancellation_tier_2_days"] or t1_days)
        p1 = Decimal(str(row["refund_percent_tier_1"] or 100))
        p2 = Decimal(str(row["refund_percent_tier_2"] or 75))
        p3 = Decimal(str(row["refund_percent_tier_3"] or 50))
        p_last = Decimal(str(row["refund_percent_last_minute"] or 0))
        ladder_source = "tour_package_policy"
        policy_text = row["cancellation_policy_text"]
    else:
        free_days = await _config_int(db, "TOUR_CANCELLATION_FREE_DAYS", 30)
        t1_days = await _config_int(db, "TOUR_CANCELLATION_TIER_1_DAYS", 15)
        t2_days = await _config_int(db, "TOUR_CANCELLATION_TIER_2_DAYS", 7)
        p1 = await _config_decimal(
            db, "TOUR_CANCELLATION_TIER_1_PERCENT", Decimal("100")
        )
        p2 = await _config_decimal(
            db, "TOUR_CANCELLATION_TIER_2_PERCENT", Decimal("75")
        )
        p3 = await _config_decimal(
            db, "TOUR_CANCELLATION_TIER_3_PERCENT", Decimal("50")
        )
        p_last = await _config_decimal(
            db, "TOUR_CANCELLATION_LAST_MINUTE_PERCENT", Decimal("0")
        )
        ladder_source = "global_tour_ladder"
        policy_text = None

    if days_to_travel is None:
        refund_percent = p_last
        tier_label = "no_travel_date"
    elif days_to_travel >= free_days:
        refund_percent = p1
        tier_label = "free_window"
    elif days_to_travel >= t1_days:
        refund_percent = p2
        tier_label = "tier_1"
    elif days_to_travel >= t2_days:
        refund_percent = p3
        tier_label = "tier_2"
    else:
        refund_percent = p_last
        tier_label = "last_minute"

    refund_amount_raw = _q2(tour_total_amount * refund_percent / Decimal("100"))
    refund_amount = min(refund_amount_raw, _q2(advance_paid_total))
    charge = _q2(tour_total_amount - refund_amount)

    snapshot = {
        "source": ladder_source,
        "policy_version": (
            "tour_package_policies:v1"
            if ladder_source == "tour_package_policy"
            else "system_configurations:TOUR_CANCELLATION:*"
        ),
        "package_id": row["package_id"],
        "policy_text": policy_text,
        "keys": {
            "free_days": free_days,
            "tier_1_days": t1_days,
            "tier_2_days": t2_days,
            "tier_1_percent": float(p1),
            "tier_2_percent": float(p2),
            "tier_3_percent": float(p3),
            "last_minute_percent": float(p_last),
        },
        "inputs": {
            "tour_booking_id": tour_booking_id,
            "tour_total_amount": float(tour_total_amount),
            "advance_paid_total": float(advance_paid_total),
            "travel_start_date": (
                travel_start.isoformat() if travel_start is not None else None
            ),
            "days_to_travel": days_to_travel,
            "evaluated_at": now.isoformat(),
        },
        "tier_label": tier_label,
    }

    return CancellationQuote(
        charge=charge,
        refund_amount=refund_amount,
        refund_percent=refund_percent,
        tier_label=tier_label,
        policy_snapshot=snapshot,
    )


# ════════════════════════════════════════════════════════════════
# Effective-policy read for admin UI ("what ladder is in effect today?")
# ════════════════════════════════════════════════════════════════


async def get_cab_policy_effective(db: AsyncSession) -> Dict[str, Any]:
    """Return the current cab ladder as a dict (for the admin policy page)."""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT config_key, config_value, description FROM system_configurations
                WHERE config_key LIKE 'CANCELLATION_%_CAB'
                """
                )
            )
        )
        .mappings()
        .all()
    )
    return {
        r["config_key"]: {"value": r["config_value"], "description": r["description"]}
        for r in rows
    }


async def get_tour_policy_effective(db: AsyncSession) -> Dict[str, Any]:
    """Return the current global tour ladder as a dict (admin policy page)."""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT config_key, config_value, description FROM system_configurations
                WHERE config_key LIKE 'TOUR_CANCELLATION_%'
                """
                )
            )
        )
        .mappings()
        .all()
    )
    return {
        r["config_key"]: {"value": r["config_value"], "description": r["description"]}
        for r in rows
    }


# ════════════════════════════════════════════════════════════════
# Audit — write a row to cancellation_policy_versions before any change
# ════════════════════════════════════════════════════════════════


async def record_policy_version(
    db: AsyncSession,
    *,
    config_key: str,
    previous_value: Optional[str],
    new_value: str,
    changed_by_user_id: Optional[UUID],
    change_reason: Optional[str] = None,
) -> None:
    """Append one row to `cancellation_policy_versions`.

    Called by the admin policy-edit endpoint BEFORE the new value lands in
    `system_configurations`, so the audit trail answers "what was the
    policy in effect at the moment booking #N was cancelled" even years
    after the ladder has moved on.

    Idempotent on duplicates within the same millisecond is *not* guaranteed
    — the migration relies on the caller only updating one key per PUT.
    """
    await db.execute(
        text(
            """
            INSERT INTO cancellation_policy_versions
                (config_key, previous_value, new_value, changed_by_user_id, change_reason)
            VALUES (:k, :pv, :nv, :uid, :reason)
            """
        ),
        {
            "k": config_key,
            "pv": previous_value,
            "nv": new_value,
            "uid": str(changed_by_user_id) if changed_by_user_id is not None else None,
            "reason": change_reason,
        },
    )


# ════════════════════════════════════════════════════════════════
# Orchestrators — quote + apply + persist in one transaction
# ════════════════════════════════════════════════════════════════
#
# These functions are what every cancel path calls. They:
#   1. Read the live booking row and any live advance(s).
#   2. Run the policy math.
#   3. Write the booking_cancellations / reservation_cancellations row with
#      the policy_snapshot persisted.
#   4. Auto-refund to the customer wallet (when the source is ONLINE / UPI
#      with the platform as receiver) — see refund_to_customer_wallet.
#   5. Touch the master / cab / hotel rows (status + actor + source).
#   6. Write a booking_timeline entry.
#
# The caller is responsible for dispatching notifications — this module is
# import-safe (no circular refs) and DB-only.


CANCEL_SOURCE_CUSTOMER = "CUSTOMER"
CANCEL_SOURCE_PARTNER_REQUEST = "PARTNER_REQUEST"
CANCEL_SOURCE_ADMIN = "ADMIN"
CANCEL_SOURCE_SYSTEM = "SYSTEM"


async def _total_advance_for_cab(db: AsyncSession, cab_booking_id: int) -> Decimal:
    """Sum ACTIVE advances on a cab booking. Cancelled → already voided → 0."""
    row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) AS net
                FROM advance_payments
                WHERE cab_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    return Decimal(row[0]) if row else Decimal("0")


async def _total_advance_for_hotel(
    db: AsyncSession, hotel_reservation_id: int
) -> Decimal:
    """Sum ACTIVE advances on a hotel reservation, minus already-refunded."""
    row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) AS net
                FROM hotel_advance_payments
                WHERE reservation_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": hotel_reservation_id},
        )
    ).first()
    return Decimal(row[0]) if row else Decimal("0")


async def _total_advance_for_tour(db: AsyncSession, tour_booking_id: int) -> Decimal:
    """Sum ACTIVE advances on a tour booking, minus already-refunded."""
    row = (
        await db.execute(
            text(
                """
                SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) AS net
                FROM tour_advance_payments
                WHERE tour_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": tour_booking_id},
        )
    ).first()
    return Decimal(row[0]) if row else Decimal("0")


async def _is_post_assignment(db: AsyncSession, cab_booking_id: int) -> bool:
    """True if at least one assignment row exists for this cab (partner notified)."""
    row = (
        await db.execute(
            text(
                """
                SELECT EXISTS(
                    SELECT 1 FROM cab_booking_assignments
                    WHERE cab_booking_id = :id
                )
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    return bool(row and row[0])


async def refund_to_customer_wallet(
    db: AsyncSession,
    *,
    customer_id: int,
    amount: Decimal,
    reference: str,
    narration: str,
) -> Decimal:
    """Credit the customer's wallet for the refund.

    Idempotent at the ledger level: a duplicate `reference` would simply
    raise an IntegrityError on the customer_wallet_ledger unique index
    (if any) — but since we only call this once per cancellation row, we
    don't need that guard. Returns the new balance for caller logging.
    """
    if amount <= 0:
        return Decimal("0")

    # Resolve or create the customer wallet.
    wrow = (
        await db.execute(
            text(
                "SELECT id, available_balance FROM customer_wallets WHERE customer_id = :cid"
            ),
            {"cid": customer_id},
        )
    ).first()
    if wrow is None:
        await db.execute(
            text(
                """
                INSERT INTO customer_wallets (customer_id, available_balance,
                                              hold_balance, wallet_status, created_at, updated_at)
                VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
                """
            ),
            {"cid": customer_id},
        )
        wrow = (
            await db.execute(
                text(
                    "SELECT id, available_balance FROM customer_wallets WHERE customer_id = :cid"
                ),
                {"cid": customer_id},
            )
        ).first()

    wallet_id = wrow[0]
    old_bal = Decimal(wrow[1] or 0)
    new_bal = old_bal + amount

    await db.execute(
        text(
            """
            UPDATE customer_wallets
               SET available_balance = :bal, updated_at = NOW()
             WHERE id = :wid
            """
        ),
        {"bal": new_bal, "wid": wallet_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO customer_wallet_ledger
                (customer_wallet_id, transaction_reference, reference_type,
                 debit_amount, credit_amount, balance_after, narration, created_at)
            VALUES (:wid, :ref, 'CANCELLATION_REFUND',
                    0, :amt, :bal, :narration, NOW())
            """
        ),
        {
            "wid": wallet_id,
            "ref": reference,
            "amt": amount,
            "bal": new_bal,
            "narration": narration,
        },
    )
    return new_bal


async def _recover_advance_refund_from_partner(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    refund_amount: Decimal,
) -> Decimal:
    """Recover partner/driver-held advance cash from the partner wallet.

    When an advance was taken in hand by the partner side (received_by in
    PARTNER / DRIVER) the cash physically sits with the partner. The customer
    is refunded out of the platform ledger, so the platform debits the
    partner's wallet for the refunded portion — otherwise the platform eats
    the loss and the partner keeps both the cash and the trip (see Bug
    "refund money-source"). Pure platform-held advances (receiver ADMIN /
    ONLINE / UPI) need no recovery.

    Capped at what each partner actually holds; the wallet may go negative,
    which is a deliberate "partner owes the platform" state recorded in the
    ledger. Returns the total recovered.
    """
    if refund_amount <= Decimal("0"):
        return Decimal("0")
    rows = (
        await db.execute(
            text(
                """
                SELECT partner_id,
                       COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) AS net
                FROM advance_payments
                WHERE cab_booking_id = :id
                  AND status = 'ACTIVE'
                  AND received_by IN ('PARTNER', 'DRIVER')
                  AND partner_id IS NOT NULL
                GROUP BY partner_id
                """
            ),
            {"id": cab_booking_id},
        )
    ).all()
    if not rows:
        return Decimal("0")

    remaining = Decimal(refund_amount)
    total_recovered = Decimal("0")
    for partner_id, net in rows:
        if remaining <= 0:
            break
        take = min(remaining, Decimal(net or 0))
        if take <= 0:
            continue
        wallet = (
            await db.execute(
                text(
                    "SELECT id, available_balance FROM wallets WHERE partner_id = :pid"
                ),
                {"pid": partner_id},
            )
        ).first()
        if wallet is None:
            continue
        wallet_id = wallet[0]
        old_bal = Decimal(wallet[1] or 0)
        new_bal = old_bal - take
        ref = f"CXL-ADV-{cab_booking_id}-{int(datetime.now(timezone.utc).timestamp())}"
        await db.execute(
            text(
                "UPDATE wallets SET available_balance = :bal, updated_at = NOW() "
                "WHERE id = :wid"
            ),
            {"bal": new_bal, "wid": wallet_id},
        )
        await db.execute(
            text(
                """
                INSERT INTO wallet_ledger
                    (wallet_id, transaction_reference, reference_type,
                     debit_amount, credit_amount, balance_after, narration, created_at)
                VALUES (:wid, :ref, 'CANCELLATION_REFUND_RECOVERY',
                        :amt, 0, :bal, :narration, NOW())
                """
            ),
            {
                "wid": wallet_id,
                "ref": ref,
                "amt": take,
                "bal": new_bal,
                "narration": (
                    f"Recovered advance refund ₹{take:.2f} for cab #{cab_booking_id} "
                    f"— the partner side held the advance in cash and the customer "
                    f"was refunded on cancellation"
                ),
            },
        )
        remaining -= take
        total_recovered += take
    return total_recovered


async def _master_has_live_services(
    db: AsyncSession, *, master_booking_id: int, exclude_cab_id: int
) -> bool:
    """True if any service (cab / hotel / tour) under the master booking is
    still live. Used so cancelling one cab does not kill a master booking that
    still has an active hotel or tour on it."""
    count = (
        (
            await db.execute(
                text(
                    """
                SELECT
                  (SELECT COUNT(*) FROM cab_bookings
                    WHERE master_booking_id = :m AND id <> :cid
                      AND booking_status <> 'CANCELLED')
                + (SELECT COUNT(*) FROM hotel_reservations
                    WHERE master_booking_id = :m
                      AND reservation_status <> 'CANCELLED')
                + (SELECT COUNT(*) FROM tour_bookings
                    WHERE master_booking_id = :m
                      AND booking_status <> 'CANCELLED')
                """
                ),
                {"m": master_booking_id, "cid": exclude_cab_id},
            )
        ).scalar()
        or 0
    )
    return int(count) > 0


async def apply_cab_cancellation(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    master_booking_id: int,
    cancellation_reason: str,
    cancelled_by_user_id: UUID,
    cancelled_by_role: str,
    cancelled_source: str,
    is_no_show: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Compute the policy math, persist the cancellation rows, auto-refund.

    Returns a dict with: charge, refund_amount, refund_percent, tier_label,
    policy_snapshot, advance_refunded_total, new_wallet_balance (if any),
    master_booking_id, cab_booking_id.
    """
    now = now or datetime.now(timezone.utc)

    # 1. Pull cab fare and check the row exists.
    cb_row = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, booking_status, final_amount,
                       estimated_amount, pickup_datetime
                FROM cab_bookings
                WHERE id = :id
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    if cb_row is None:
        raise ValueError(f"cab_booking {cab_booking_id} not found")
    # customer_id comes through master_bookings — pull it.
    cust_row = (
        await db.execute(
            text("SELECT customer_id FROM master_bookings WHERE id = :id"),
            {"id": cb_row[1]},
        )
    ).first()
    customer_id = int(cust_row[0]) if cust_row else None

    cab_total = Decimal(cb_row[3] or cb_row[4] or 0)
    advance_total = await _total_advance_for_cab(db, cab_booking_id)
    post_assignment = await _is_post_assignment(db, cab_booking_id)

    quote = await quote_cab_cancellation(
        db,
        cab_booking_id=cab_booking_id,
        cab_total_amount=cab_total,
        advance_paid_total=advance_total,
        is_post_assignment=post_assignment,
        now=now,
    )

    # 2. Auto-refund to customer wallet — capped at the live advance total.
    refunded = Decimal("0")
    new_balance = Decimal("0")
    if customer_id is not None and quote.refund_amount > 0:
        reference = f"CXL-CAB-{cab_booking_id}-{int(now.timestamp())}"
        new_balance = await refund_to_customer_wallet(
            db,
            customer_id=customer_id,
            amount=quote.refund_amount,
            reference=reference,
            narration=(
                f"Auto-refund on cab cancellation {cb_row[0]}. "
                f"Reason: {cancellation_reason[:80]}"
            ),
        )
        refunded = quote.refund_amount
        # Recover partner/driver-held advance cash from the partner wallet so
        # the platform is not out of pocket for a refund it fronted to the
        # customer. Pure platform-held advances need no recovery.
        await _recover_advance_refund_from_partner(
            db,
            cab_booking_id=cab_booking_id,
            refund_amount=quote.refund_amount,
        )
        # Mark advance rows as fully refunded so the audit trail closes.
        await db.execute(
            text(
                """
                UPDATE advance_payments
                   SET refunded_amount = amount,
                       status           = CASE
                                            WHEN amount - COALESCE(refunded_amount,0) <= :amt THEN 'REFUNDED'
                                            ELSE status
                                          END
                 WHERE cab_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": cab_booking_id, "amt": quote.refund_amount},
        )

    # 3. Update cab_bookings + master_bookings + booking_cancellations + timeline.
    await db.execute(
        text(
            """
            UPDATE cab_bookings
               SET booking_status        = 'CANCELLED',
                   cancelled_by_user_id  = :uid,
                   cancelled_source      = :src,
                   updated_at            = NOW()
             WHERE id = :id
            """
        ),
        {
            "uid": str(cancelled_by_user_id),
            "src": cancelled_source,
            "id": cab_booking_id,
        },
    )
    # Master booking: only flip to CANCELLED when nothing else on it is live —
    # a hotel/tour still running keeps the master open. Also move the paid
    # amount + payment_status so the ledger doesn't keep counting refunded money.
    paid_row = (
        await db.execute(
            text(
                "SELECT COALESCE(total_paid_amount, 0) FROM master_bookings WHERE id = :m"
            ),
            {"m": master_booking_id},
        )
    ).first()
    paid_now = Decimal(paid_row[0] or 0) if paid_row else Decimal("0")
    new_paid = max(Decimal("0"), paid_now - refunded)
    new_pay_status = "REFUNDED" if new_paid <= 0 else "PARTIAL"

    if await _master_has_live_services(
        db, master_booking_id=master_booking_id, exclude_cab_id=cab_booking_id
    ):
        master_sql = text(
            """
            UPDATE master_bookings
               SET total_refund_amount = COALESCE(total_refund_amount, 0) + :refund,
                   total_paid_amount  = :paid,
                   payment_status     = :pay_status,
                   updated_at          = NOW()
             WHERE id = :m
            """
        )
    else:
        master_sql = text(
            """
            UPDATE master_bookings
               SET booking_status     = 'CANCELLED',
                   total_refund_amount = COALESCE(total_refund_amount, 0) + :refund,
                   total_paid_amount  = :paid,
                   payment_status     = :pay_status,
                   updated_at          = NOW()
             WHERE id = :m
            """
        )
    await db.execute(
        master_sql,
        {
            "refund": refunded,
            "paid": new_paid,
            "pay_status": new_pay_status,
            "m": master_booking_id,
        },
    )
    # 4. Close any active assignment so the "active" rule stays consistent
    # (mirrors the migration 0041 close pattern used elsewhere) and release the
    # vehicle + driver back to ACTIVE / OFFLINE so they become assignable again.
    open_assign = (
        await db.execute(
            text(
                """
                SELECT id, vehicle_id, driver_id FROM cab_booking_assignments
                 WHERE cab_booking_id = :id AND closed_at IS NULL
                """
            ),
            {"id": cab_booking_id},
        )
    ).first()
    await db.execute(
        text(
            """
            UPDATE cab_booking_assignments
               SET closed_at      = NOW(),
                   close_reason   = 'CANCELLED'
             WHERE cab_booking_id = :id AND closed_at IS NULL
            """
        ),
        {"id": cab_booking_id},
    )
    if open_assign is not None:
        vid, did = open_assign[1], open_assign[2]
        if vid is not None:
            await db.execute(
                text("UPDATE vehicles SET status = 'ACTIVE' WHERE id = :vid"),
                {"vid": vid},
            )
        if did is not None:
            await db.execute(
                text(
                    "UPDATE driver_availability SET availability_status = 'OFFLINE' "
                    "WHERE driver_id = :did"
                ),
                {"did": did},
            )
    # 5. booking_cancellations row (one per master booking).
    # The SQLAlchemy model declares `master_booking_id` as unique=True and
    # migration 0047 enforces that at the DB level. We still do a SELECT
    # first (rather than ON CONFLICT) because (a) some hosted DBs / older
    # snapshots may not have the constraint yet, and (b) the SQL is clearer
    # to read this way. Two cabs under one master booking call this twice;
    # the second call updates the existing row.
    existing_cxl = (
        await db.execute(
            text(
                """
                SELECT id FROM booking_cancellations
                 WHERE master_booking_id = :m
                 LIMIT 1
                """
            ),
            {"m": master_booking_id},
        )
    ).first()
    if existing_cxl is None:
        await db.execute(
            text(
                """
                INSERT INTO booking_cancellations
                    (master_booking_id, cancelled_by, cancellation_reason,
                     cancellation_charge, refund_amount, cancelled_at,
                     cancelled_source, cancelled_by_role, policy_snapshot,
                     advance_refunded_total)
                VALUES (:m, :uid, :reason, :charge, :refund, NOW(),
                        :src, :role, CAST(:snap AS JSONB), :adv_ref)
                """
            ),
            {
                "m": master_booking_id,
                "uid": str(cancelled_by_user_id),
                "reason": cancellation_reason,
                "charge": quote.charge,
                "refund": quote.refund_amount,
                "src": cancelled_source,
                "role": cancelled_by_role,
                "snap": _to_jsonb_str(quote.policy_snapshot),
                "adv_ref": refunded,
            },
        )
    else:
        await db.execute(
            text(
                """
                UPDATE booking_cancellations
                   SET cancellation_charge    = :charge,
                       refund_amount          = :refund,
                       cancelled_source       = :src,
                       cancelled_by_role      = :role,
                       policy_snapshot        = CAST(:snap AS JSONB),
                       advance_refunded_total = :adv_ref,
                       cancellation_reason    = :reason,
                       cancelled_at           = NOW()
                 WHERE master_booking_id = :m
                """
            ),
            {
                "m": master_booking_id,
                "reason": cancellation_reason,
                "charge": quote.charge,
                "refund": quote.refund_amount,
                "src": cancelled_source,
                "role": cancelled_by_role,
                "snap": _to_jsonb_str(quote.policy_snapshot),
                "adv_ref": refunded,
            },
        )
    # 6. Timeline entry.
    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp)
            VALUES (:m, 'CAB_CANCELLED',
                    :desc, NOW())
            """
        ),
        {
            "m": master_booking_id,
            "desc": (
                f"Cab {cb_row[0]} cancelled. Source: {cancelled_source}. "
                f"Role: {cancelled_by_role}. "
                f"Charge ₹{quote.charge}, refund ₹{quote.refund_amount} "
                f"(tier: {quote.tier_label}). "
                f"Reason: {cancellation_reason[:200]}"
            ),
        },
    )
    return {
        "cab_booking_id": cab_booking_id,
        "master_booking_id": master_booking_id,
        "charge": quote.charge,
        "refund_amount": quote.refund_amount,
        "advance_refunded_total": refunded,
        "refund_percent": quote.refund_percent,
        "tier_label": quote.tier_label,
        "policy_snapshot": quote.policy_snapshot,
        "new_wallet_balance": new_balance,
        "cancelled_source": cancelled_source,
        "cancelled_by_role": cancelled_by_role,
    }


async def apply_hotel_cancellation(
    db: AsyncSession,
    *,
    hotel_reservation_id: int,
    master_booking_id: int,
    cancellation_reason: str,
    cancelled_by_user_id: UUID,
    cancelled_by_role: str,
    cancelled_source: str,
    is_no_show: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Hotel-side twin of apply_cab_cancellation."""
    now = now or datetime.now(timezone.utc)

    hr_row = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, reservation_status, total_amount,
                       check_in_date
                FROM hotel_reservations
                WHERE id = :id
                """
            ),
            {"id": hotel_reservation_id},
        )
    ).first()
    if hr_row is None:
        raise ValueError(f"hotel_reservation {hotel_reservation_id} not found")

    cust_row = (
        await db.execute(
            text("SELECT customer_id FROM master_bookings WHERE id = :id"),
            {"id": hr_row[1]},
        )
    ).first()
    customer_id = int(cust_row[0]) if cust_row else None

    hotel_total = Decimal(hr_row[3] or 0)
    advance_total = await _total_advance_for_hotel(db, hotel_reservation_id)

    quote = await quote_hotel_cancellation(
        db,
        hotel_reservation_id=hotel_reservation_id,
        hotel_total_amount=hotel_total,
        advance_paid_total=advance_total,
        is_no_show=is_no_show,
        now=now,
    )

    refunded = Decimal("0")
    new_balance = Decimal("0")
    if customer_id is not None and quote.refund_amount > 0:
        reference = f"CXL-HTL-{hotel_reservation_id}-{int(now.timestamp())}"
        new_balance = await refund_to_customer_wallet(
            db,
            customer_id=customer_id,
            amount=quote.refund_amount,
            reference=reference,
            narration=(
                f"Auto-refund on hotel cancellation {hr_row[0]}. "
                f"Reason: {cancellation_reason[:80]}"
            ),
        )
        refunded = quote.refund_amount
        # Mark ACTIVE advances fully refunded; cumulative semantics match the
        # existing hotel_advance_refund endpoint.
        await db.execute(
            text(
                """
                UPDATE hotel_advance_payments
                   SET refunded_amount = amount,
                       status           = CASE
                                            WHEN amount - COALESCE(refunded_amount,0) <= :amt THEN 'REFUNDED'
                                            ELSE status
                                          END
                 WHERE reservation_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": hotel_reservation_id, "amt": quote.refund_amount},
        )

    await db.execute(
        text(
            """
            UPDATE hotel_reservations
               SET reservation_status  = 'CANCELLED',
                   cancelled_at        = NOW(),
                   cancellation_reason = :reason,
                   cancellation_charge = :charge,
                   refund_amount       = :refund,
                   cancelled_by_user_id = :uid,
                   cancelled_source     = :src,
                   updated_at           = NOW()
             WHERE id = :id
            """
        ),
        {
            "reason": cancellation_reason,
            "charge": quote.charge,
            "refund": quote.refund_amount,
            "uid": str(cancelled_by_user_id),
            "src": cancelled_source,
            "id": hotel_reservation_id,
        },
    )
    await db.execute(
        text(
            """
            UPDATE master_bookings
               SET total_refund_amount = COALESCE(total_refund_amount, 0) + :refund,
                   updated_at          = NOW()
             WHERE id = :id
            """
        ),
        {"refund": refunded, "id": master_booking_id},
    )

    # reservation_cancellations — sibling of booking_cancellations.
    await db.execute(
        text(
            """
            INSERT INTO reservation_cancellations
                (hotel_reservation_id, cancelled_by_user_id, cancelled_source,
                 cancellation_reason, cancellation_charge, refund_amount,
                 policy_snapshot, advance_refunded_total, cancelled_at)
            VALUES (:rid, :uid, :src, :reason, :charge, :refund,
                    CAST(:snap AS JSONB), :adv_ref, NOW())
            ON CONFLICT (hotel_reservation_id) DO UPDATE
              SET cancellation_charge    = EXCLUDED.cancellation_charge,
                  refund_amount          = EXCLUDED.refund_amount,
                  cancelled_source       = EXCLUDED.cancelled_source,
                  cancellation_reason    = EXCLUDED.cancellation_reason,
                  policy_snapshot        = EXCLUDED.policy_snapshot,
                  advance_refunded_total = EXCLUDED.advance_refunded_total
            """
        ),
        {
            "rid": hotel_reservation_id,
            "uid": str(cancelled_by_user_id),
            "src": cancelled_source,
            "reason": cancellation_reason,
            "charge": quote.charge,
            "refund": quote.refund_amount,
            "snap": _to_jsonb_str(quote.policy_snapshot),
            "adv_ref": refunded,
        },
    )
    # Master booking status is only flipped if no other live service remains.
    await _maybe_cancel_master(db, master_booking_id)

    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp)
            VALUES (:m, 'HOTEL_CANCELLED', :desc, NOW())
            """
        ),
        {
            "m": master_booking_id,
            "desc": (
                f"Hotel reservation {hr_row[0]} cancelled. Source: {cancelled_source}. "
                f"Role: {cancelled_by_role}. "
                f"Charge ₹{quote.charge}, refund ₹{quote.refund_amount} "
                f"(tier: {quote.tier_label}). "
                f"Reason: {cancellation_reason[:200]}"
            ),
        },
    )
    return {
        "hotel_reservation_id": hotel_reservation_id,
        "master_booking_id": master_booking_id,
        "charge": quote.charge,
        "refund_amount": quote.refund_amount,
        "advance_refunded_total": refunded,
        "refund_percent": quote.refund_percent,
        "tier_label": quote.tier_label,
        "policy_snapshot": quote.policy_snapshot,
        "new_wallet_balance": new_balance,
        "cancelled_source": cancelled_source,
        "cancelled_by_role": cancelled_by_role,
    }


async def apply_tour_cancellation(
    db: AsyncSession,
    *,
    tour_booking_id: int,
    master_booking_id: int,
    cancellation_reason: str,
    cancelled_by_user_id: UUID,
    cancelled_by_role: str,
    cancelled_source: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Tour-side twin of apply_hotel_cancellation.

    Refunds the policy amount to the customer wallet, marks ACTIVE tour
    advances as refunded, flips the tour to CANCELLED, records the master
    cancellation row + timeline, and lets `_maybe_cancel_master` decide
    whether the master booking closes.
    """
    now = now or datetime.now(timezone.utc)

    tb_row = (
        await db.execute(
            text(
                """
                SELECT id, master_booking_id, booking_status, total_amount,
                       travel_start_date
                FROM tour_bookings
                WHERE id = :id
                """
            ),
            {"id": tour_booking_id},
        )
    ).first()
    if tb_row is None:
        raise ValueError(f"tour_booking {tour_booking_id} not found")

    cust_row = (
        await db.execute(
            text("SELECT customer_id FROM master_bookings WHERE id = :id"),
            {"id": tb_row[1]},
        )
    ).first()
    customer_id = int(cust_row[0]) if cust_row else None

    tour_total = Decimal(tb_row[3] or 0)
    advance_total = await _total_advance_for_tour(db, tour_booking_id)

    quote = await quote_tour_cancellation(
        db,
        tour_booking_id=tour_booking_id,
        tour_total_amount=tour_total,
        advance_paid_total=advance_total,
        now=now,
    )

    refunded = Decimal("0")
    new_balance = Decimal("0")
    if customer_id is not None and quote.refund_amount > 0:
        reference = f"CXL-TOU-{tour_booking_id}-{int(now.timestamp())}"
        new_balance = await refund_to_customer_wallet(
            db,
            customer_id=customer_id,
            amount=quote.refund_amount,
            reference=reference,
            narration=(
                f"Auto-refund on tour cancellation {tb_row[0]}. "
                f"Reason: {cancellation_reason[:80]}"
            ),
        )
        refunded = quote.refund_amount
        # Recover partner/driver-held advance cash from the partner wallet so
        # the platform is not out of pocket for a refund it fronted to the
        # customer. Pure platform-held advances need no recovery.
        if refunded > 0:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT tp.partner_id,
                               COALESCE(SUM(tap.amount - COALESCE(tap.refunded_amount, 0)), 0) AS net
                        FROM tour_advance_payments tap
                        JOIN tour_bookings tb ON tb.id = tap.tour_booking_id
                        JOIN tour_packages tp ON tp.id = tb.package_id
                        WHERE tap.tour_booking_id = :id
                          AND tap.status = 'ACTIVE'
                          AND tap.received_by IN ('PARTNER', 'DRIVER')
                          AND tp.partner_id IS NOT NULL
                        GROUP BY tp.partner_id
                        """
                    ),
                    {"id": tour_booking_id},
                )
            ).all()
            remaining = Decimal(refunded)
            for r in rows:
                if remaining <= 0:
                    break
                pid = int(r[0])
                net = Decimal(r[1] or 0)
                take = min(remaining, net)
                if take <= 0:
                    continue
                wallet = (
                    await db.execute(
                        text(
                            "SELECT id, available_balance FROM wallets "
                            "WHERE partner_id = :pid"
                        ),
                        {"pid": pid},
                    )
                ).first()
                if wallet is None:
                    continue
                old_bal = Decimal(wallet[1] or 0)
                new_bal = old_bal - take
                ref = f"CXL-TOU-{tour_booking_id}-{int(now.timestamp())}"
                await db.execute(
                    text(
                        "UPDATE wallets SET available_balance = :bal, updated_at = NOW() "
                        "WHERE id = :wid"
                    ),
                    {"bal": new_bal, "wid": wallet[0]},
                )
                await db.execute(
                    text(
                        """
                        INSERT INTO wallet_ledger
                            (wallet_id, transaction_reference, reference_type,
                             debit_amount, credit_amount, balance_after, narration, created_at)
                        VALUES (:wid, :ref, 'CANCELLATION_REFUND_RECOVERY',
                                :amt, 0, :bal, :narration, NOW())
                        """
                    ),
                    {
                        "wid": wallet[0],
                        "ref": ref,
                        "amt": take,
                        "bal": new_bal,
                        "narration": (
                            f"Recovered advance refund ₹{float(take):,.2f} for tour "
                            f"#{tour_booking_id} — the partner side held the advance in "
                            f"cash and the customer was refunded on cancellation."
                        ),
                    },
                )
                remaining -= take
        # Mark ACTIVE advances fully refunded; cumulative semantics match the
        # cab/hotel paths.
        await db.execute(
            text(
                """
                UPDATE tour_advance_payments
                   SET refunded_amount = amount,
                       status          = CASE
                                            WHEN amount - COALESCE(refunded_amount,0) <= :amt THEN 'REFUNDED'
                                            ELSE status
                                          END
                 WHERE tour_booking_id = :id AND status = 'ACTIVE'
                """
            ),
            {"id": tour_booking_id, "amt": quote.refund_amount},
        )

    await db.execute(
        text(
            """
            UPDATE tour_bookings
               SET booking_status = 'CANCELLED',
                   updated_at     = NOW()
             WHERE id = :id
            """
        ),
        {"id": tour_booking_id},
    )
    await db.execute(
        text(
            """
            UPDATE master_bookings
               SET total_refund_amount = COALESCE(total_refund_amount, 0) + :refund,
                   updated_at          = NOW()
             WHERE id = :id
            """
        ),
        {"refund": refunded, "id": master_booking_id},
    )

    # booking_cancellations row — one per master booking (unique constraint),
    # same select-first pattern as apply_cab_cancellation.
    existing_cxl = (
        await db.execute(
            text(
                """
                SELECT id FROM booking_cancellations
                 WHERE master_booking_id = :m
                 LIMIT 1
                """
            ),
            {"m": master_booking_id},
        )
    ).first()
    if existing_cxl is None:
        await db.execute(
            text(
                """
                INSERT INTO booking_cancellations
                    (master_booking_id, cancelled_by, cancellation_reason,
                     cancellation_charge, refund_amount, cancelled_at,
                     cancelled_source, cancelled_by_role, policy_snapshot,
                     advance_refunded_total)
                VALUES (:m, :uid, :reason, :charge, :refund, NOW(),
                        :src, :role, CAST(:snap AS JSONB), :adv_ref)
                """
            ),
            {
                "m": master_booking_id,
                "uid": str(cancelled_by_user_id),
                "reason": cancellation_reason,
                "charge": quote.charge,
                "refund": quote.refund_amount,
                "src": cancelled_source,
                "role": cancelled_by_role,
                "snap": _to_jsonb_str(quote.policy_snapshot),
                "adv_ref": refunded,
            },
        )
    else:
        await db.execute(
            text(
                """
                UPDATE booking_cancellations
                   SET cancellation_charge    = :charge,
                       refund_amount          = :refund,
                       cancelled_source       = :src,
                       cancelled_by_role      = :role,
                       policy_snapshot        = CAST(:snap AS JSONB),
                       advance_refunded_total = :adv_ref,
                       cancellation_reason    = :reason,
                       cancelled_at           = NOW()
                 WHERE master_booking_id = :m
                """
            ),
            {
                "m": master_booking_id,
                "reason": cancellation_reason,
                "charge": quote.charge,
                "refund": quote.refund_amount,
                "src": cancelled_source,
                "role": cancelled_by_role,
                "snap": _to_jsonb_str(quote.policy_snapshot),
                "adv_ref": refunded,
            },
        )
    # Master booking status is only flipped if no other live service remains.
    await _maybe_cancel_master(db, master_booking_id)

    await db.execute(
        text(
            """
            INSERT INTO booking_timelines
                (master_booking_id, event_type, event_description, event_timestamp)
            VALUES (:m, 'TOUR_CANCELLED', :desc, NOW())
            """
        ),
        {
            "m": master_booking_id,
            "desc": (
                f"Tour booking {tb_row[0]} cancelled. Source: {cancelled_source}. "
                f"Role: {cancelled_by_role}. "
                f"Charge ₹{quote.charge}, refund ₹{quote.refund_amount} "
                f"(tier: {quote.tier_label}). "
                f"Reason: {cancellation_reason[:200]}"
            ),
        },
    )
    return {
        "tour_booking_id": tour_booking_id,
        "master_booking_id": master_booking_id,
        "charge": quote.charge,
        "refund_amount": quote.refund_amount,
        "advance_refunded_total": refunded,
        "refund_percent": quote.refund_percent,
        "tier_label": quote.tier_label,
        "policy_snapshot": quote.policy_snapshot,
        "new_wallet_balance": new_balance,
        "cancelled_source": cancelled_source,
        "cancelled_by_role": cancelled_by_role,
    }


async def _maybe_cancel_master(db: AsyncSession, master_booking_id: int) -> None:
    """Flip master_bookings.booking_status to CANCELLED only if every live
    service under it is already CANCELLED (terminal). Otherwise leave the
    master alone so the remaining live services continue.
    """
    await db.execute(
        text(
            """
            UPDATE master_bookings
               SET booking_status = 'CANCELLED',
                   updated_at     = NOW()
             WHERE id = :m
               AND booking_status NOT IN ('CANCELLED', 'COMPLETED', 'CLOSED')
               AND NOT EXISTS (
                   SELECT 1 FROM cab_bookings cb
                    WHERE cb.master_booking_id = :m
                      AND cb.booking_status NOT IN (
                          'CANCELLED','COMPLETED','SETTLEMENT_PENDING','SETTLED'
                      )
               )
               AND NOT EXISTS (
                   SELECT 1 FROM hotel_reservations hr
                    WHERE hr.master_booking_id = :m
                      AND hr.reservation_status NOT IN (
                          'CANCELLED','COMPLETED','SETTLED','REJECTED','NO_SHOW'
                      )
               )
               AND NOT EXISTS (
                   SELECT 1 FROM tour_bookings tb
                    WHERE tb.master_booking_id = :m
                      AND tb.booking_status NOT IN (
                          'CANCELLED','COMPLETED','SETTLED','SETTLEMENT_PENDING'
                      )
               )
            """
        ),
        {"m": master_booking_id},
    )


def _to_jsonb_str(d: Dict[str, Any]) -> str:
    """Stable JSON serialisation for casting into JSONB via CAST(:snap AS JSONB)."""
    import json

    return json.dumps(d, default=str, ensure_ascii=False)
