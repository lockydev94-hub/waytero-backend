# ============================================================
# WAYTERO — CAB AUTO-ASSIGN (RANKED FAN-OUT)
# File: app/modules/booking/services/auto_assign.py
# Doc Ref: Round 15 plan C3; migration 0086 (needs_attention, the
#          auto_assign_* configuration keys).
#
# Today one booking is offered to one partner: an admin picks a name, the
# partner accepts or refuses, and a sweeper takes the booking back when the
# acceptance window closes. This service adds the part the owner described —
# "available cabs → assign to partner → accept/timeout → next partner" — by
# offering a booking to a small ranked group of partners at once and moving to
# the next group when nobody answers.
#
# Three rules hold it together:
#
# 1. The offer ROWS are the offer. `cab_bookings.pending_partner_id` is one
#    column and a round has several partners, so a fan-out round leaves it NULL
#    and the open, unanswered `cab_booking_assignments` rows say who is being
#    asked. Nothing reads the pointer to decide whether a partner may answer.
# 2. Ranking is by work already on their plate, then id. The schema carries no
#    partner rating that means anything yet, so nothing is invented — a partner
#    running three trips is not offered a fourth before a partner running none.
# 3. A partner-funded coupon is a contract about WHO may drive. The same cohort
#    lock the admin's manual assign enforces is applied here, because offering
#    the trip to a partner who cannot take the discount would strand money.
#
# It is off by default (`auto_assign_enabled = false`, migration 0086). With the
# switch off, every function here reads the configuration and does nothing, so
# the first deploy changes nothing a partner can feel.
# ============================================================
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("waytero.auto_assign")

ASSIGNMENT_TYPE = "AUTO_OFFER"

# The statuses in which a vehicle is out on the road and not available for a
# new offer. Same set the admin assign screen uses for `available_cabs_at_offer`,
# so an auto-offer and a manual one count availability identically.
BUSY_STATUSES = ("ASSIGNED", "DRIVER_ASSIGNED", "STARTED")

DEFAULTS = {
    "auto_assign_enabled": "false",
    "auto_assign_round_size": "3",
    "auto_assign_max_rounds": "3",
    "auto_assign_timeout_seconds": "180",
}

# `cab_booking_assignments.close_reason` documented values (migration 0041) plus
# the ones the chain writes. The codes live in
# app.modules.partner.constants (TIMEOUT / ADMIN_REASSIGN /
# ACCEPTED_BY_ANOTHER) — callers pass them in, because a fan-out close spelled
# one way here and another way in the coupon exhaustion check that reads it is
# how an accepted trip gets cancelled as "every partner declined".


@dataclass(frozen=True)
class AutoAssignSettings:
    enabled: bool
    round_size: int
    max_rounds: int
    timeout_seconds: int

    @property
    def acceptance_window(self) -> timedelta:
        return timedelta(seconds=self.timeout_seconds)


def _as_int(raw: object, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def settings(db: AsyncSession) -> AutoAssignSettings:
    """The four configuration rows, with the seeded values as the fallback.

    Read every round rather than cached: a super admin turning this on, or
    shortening the window, should take effect on the next offer, not on the next
    deploy.
    """
    rows = (
        (
            await db.execute(
                text(
                    "SELECT config_key, config_value FROM system_configurations "
                    "WHERE config_key = ANY(:keys)"
                ),
                {"keys": list(DEFAULTS)},
            )
        )
        .mappings()
        .all()
    )
    found = {r["config_key"]: r["config_value"] for r in rows}

    def value(key: str) -> str:
        raw = found.get(key)
        return DEFAULTS[key] if raw is None else str(raw)

    return AutoAssignSettings(
        enabled=value("auto_assign_enabled").strip().lower()
        in ("true", "1", "yes", "on"),
        round_size=_as_int(value("auto_assign_round_size"), 3),
        max_rounds=_as_int(value("auto_assign_max_rounds"), 3),
        timeout_seconds=_as_int(value("auto_assign_timeout_seconds"), 180),
    )


# ── who is eligible, and in what order ─────────────────────────────────────

# The candidate query is one statement so the ranking is decided by the
# database, in one place: a partner in the booking's city, approved, cab
# service switched on, owning a vehicle of the exact category the customer
# booked that is not already out on a trip, and not already offered this
# booking. Ordered by trips in hand then id — deterministic, so two sweeps of
# the same booking pick the same partners, and no partner is starved by an
# alphabetical rule.
_CANDIDATES_SQL = """
SELECT p.id AS partner_id,
       p.business_name,
       p.user_id,
       (SELECT COUNT(*)
          FROM cab_booking_assignments a
          JOIN cab_bookings cb2 ON cb2.id = a.cab_booking_id
         WHERE a.partner_id = p.id
           AND a.closed_at IS NULL
           AND cb2.booking_status IN ('ASSIGNED', 'DRIVER_ASSIGNED', 'STARTED')
       ) AS open_trips,
       (SELECT COUNT(*)
          FROM vehicles v
         WHERE v.partner_id = p.id
           AND v.status IN ('APPROVED', 'ACTIVE')
           AND v.deleted_at IS NULL
           AND v.city_id = :city_id
           AND v.vehicle_category_id = :category_id
           AND NOT EXISTS (
                 SELECT 1 FROM cab_booking_assignments a3
                 JOIN cab_bookings cb3 ON cb3.id = a3.cab_booking_id
                WHERE a3.vehicle_id = v.id
                  AND cb3.booking_status IN ('ASSIGNED', 'DRIVER_ASSIGNED', 'STARTED'))
       ) AS free_cabs
FROM partners p
WHERE p.city_id = :city_id
  AND p.status IN ('APPROVED', 'ACTIVE')
  AND EXISTS (SELECT 1 FROM partner_services ps
               WHERE ps.partner_id = p.id AND ps.service_type = 'CAB'
                 AND COALESCE(ps.is_active, TRUE))
  AND NOT EXISTS (SELECT 1 FROM cab_booking_assignments o
                   WHERE o.cab_booking_id = :cab_booking_id
                     AND o.partner_id = p.id)
  COHORT_PREDICATE
ORDER BY open_trips ASC, p.id ASC
LIMIT :round_size
"""


def _cohort_predicate(cohort_ids: Sequence[int] | None) -> str:
    """`AND p.id IN (…)` for a partner-funded coupon, or nothing at all.

    The ids are rendered as integers, not interpolated: whatever arrives from the
    coupon table goes through `int()`, so a value that is not a number cannot
    reach the SQL text.
    """
    if not cohort_ids:
        return ""
    ids = ", ".join(str(int(pid)) for pid in cohort_ids)
    return f"  AND p.id IN ({ids})"


async def candidates(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    city_id: int,
    category_id: int,
    cohort_ids: Optional[Sequence[int]] = None,
    round_size: int = 3,
) -> list[dict]:
    """Partners who can take this trip right now, best-placed first."""
    sql = _CANDIDATES_SQL.replace("  COHORT_PREDICATE", _cohort_predicate(cohort_ids))
    rows = (
        (
            await db.execute(
                text(sql),
                {
                    "cab_booking_id": cab_booking_id,
                    "city_id": city_id,
                    "category_id": category_id,
                    "round_size": round_size,
                },
            )
        )
        .mappings()
        .all()
    )
    # A partner with no free vehicle of that category is not "busy but eligible":
    # the trip would sit on them and the customer would wait for nothing.
    return [dict(r) for r in rows if int(r["free_cabs"]) > 0]


# ── the round ───────────────────────────────────────────────────────────────


async def published_rounds(db: AsyncSession, cab_booking_id: int) -> int:
    """How many fan-out rounds this booking has already had.

    A round is identified by the deadline its rows share, so the count is the
    number of distinct deadlines among this booking's auto-offer rows — no new
    column and no counter that can drift from the rows it describes.
    """
    return int(
        (
            await db.execute(
                text(
                    "SELECT COUNT(DISTINCT acceptance_deadline) "
                    "FROM cab_booking_assignments "
                    "WHERE cab_booking_id = :id AND assignment_type = :type"
                ),
                {
                    "id": cab_booking_id,
                    "type": ASSIGNMENT_TYPE,
                },
            )
        ).scalar_one()
    )


async def publish_round(
    db: AsyncSession,
    cb,
    *,
    city_id: int,
    cohort_ids: Optional[Sequence[int]],
    settings_: AutoAssignSettings,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Offer the booking to the next ranked group. Returns who was offered.

    Writes exactly what a manual assign writes — one open assignment row per
    partner carrying the deadline and the availability the partner had when they
    were asked — plus the booking back into PENDING_PARTNER_ACCEPTANCE with that
    shared deadline. `pending_partner_id` stays NULL: the rows are the offer.
    """
    when = now or datetime.now(timezone.utc)
    deadline = when + settings_.acceptance_window

    offered = await candidates(
        db,
        cab_booking_id=cb.id,
        city_id=city_id,
        category_id=cb.vehicle_category_id,
        cohort_ids=cohort_ids,
        round_size=settings_.round_size,
    )
    if not offered:
        return []

    for partner in offered:
        await db.execute(
            text(
                "INSERT INTO cab_booking_assignments "
                "       (cab_booking_id, partner_id, assigned_at, assignment_type, "
                "        acceptance_deadline, available_cabs_at_offer) "
                "VALUES (:cb, :pid, :at, :type, :deadline, :free_cabs)"
            ),
            {
                "cb": cb.id,
                "pid": int(partner["partner_id"]),
                "at": when,
                "type": ASSIGNMENT_TYPE,
                "deadline": deadline,
                "free_cabs": int(partner["free_cabs"]),
            },
        )

    await db.execute(
        text(
            "UPDATE cab_bookings SET booking_status = "
            "              'PENDING_PARTNER_ACCEPTANCE', "
            "       acceptance_deadline = :deadline, "
            "       pending_partner_id = NULL, "
            "       partner_responded_at = NULL, "
            "       needs_attention = NULL, "
            "       updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"deadline": deadline, "id": cb.id},
    )

    round_no = await published_rounds(db, cb.id)
    names = ", ".join(
        f"{p['partner_id']}({int(p['free_cabs'])} cab)" for p in offered
    )
    await db.execute(
        text(
            "INSERT INTO booking_timelines "
            "       (master_booking_id, event_type, event_description, "
            "        event_timestamp, created_by) "
            "VALUES (:mb, 'AUTO_ASSIGN_OFFERED', :detail, :ts, NULL)"
        ),
        {
            "mb": cb.master_booking_id,
            "ts": when,
            "detail": (
                f"Auto-assign round {round_no} by WayTero system — offered to "
                f"{len(offered)} partner(s) [{names}], each until "
                f"{deadline:%d %b %Y %H:%M}."
            ),
        },
    )
    # `published_rounds` counts a round by the deadline its rows share, so that
    # deadline has to survive being closed or the chain would count zero rounds
    # forever and never stop.
    deadline_iso = deadline.isoformat()
    for partner in offered:
        partner["deadline_iso"] = deadline_iso

    # The caller commits: `get_db` commits for a request, the sweeper commits its
    # own pass. A service only flushes what it wrote.
    await db.flush()
    return offered


# ── the chain ───────────────────────────────────────────────────────────────


async def _coupon_cohort(db: AsyncSession, cab_booking_id: int) -> Optional[list[int]]:
    """Partners a partner-funded coupon binds this booking to, or None.

    Reuses the admin assign screen's own lock so the fan-out cannot offer a trip
    the money says only a coupon cohort may drive.
    """
    from app.modules.admin.booking_api import _partner_coupon_lock

    lock = await _partner_coupon_lock(db, cab_booking_id)
    if not lock:
        return None
    return list(lock["eligible_partner_ids"] or [])


async def open_offer_partner_ids(db: AsyncSession, cab_booking_id: int) -> list[int]:
    """Partners holding an offer they have not answered yet."""
    rows = (
        await db.execute(
            text(
                "SELECT DISTINCT partner_id FROM cab_booking_assignments "
                "WHERE cab_booking_id = :id AND closed_at IS NULL "
                "  AND accepted_at IS NULL AND rejected_at IS NULL "
                "ORDER BY partner_id"
            ),
            {"id": cab_booking_id},
        )
    ).scalars().all()
    return [int(r) for r in rows]


async def close_open_offers(
    db: AsyncSession,
    cab_booking_id: int,
    *,
    reason: str,
    now: datetime,
    exclude_partner_id: Optional[int] = None,
    note: Optional[str] = None,
) -> list[int]:
    """Close every unanswered offer on the booking. Returns who was closed.

    `exclude_partner_id` is how an acceptance closes the other partners' rows
    while leaving the accepting partner's own row — that one is answered, not
    closed as if nobody replied.
    """
    params: dict = {"id": cab_booking_id, "now": now, "reason": reason, "note": note}
    where = (
        " WHERE cab_booking_id = :id AND closed_at IS NULL "
        "   AND accepted_at IS NULL AND rejected_at IS NULL"
    )
    if exclude_partner_id is not None:
        where += " AND partner_id <> :exclude"
        params["exclude"] = int(exclude_partner_id)

    ids = list(
        (
            await db.execute(
                text(
                    "UPDATE cab_booking_assignments "
                    "   SET closed_at = :now, close_reason = :reason, "
                    "       rejected_at = COALESCE(rejected_at, :now), "
                    "       rejection_reason_code = COALESCE("
                    "           rejection_reason_code, :reason), "
                    "       rejection_notes = COALESCE(rejection_notes, :note) "
                    + where
                    + " RETURNING partner_id"
                ),
                params,
            )
        )
        .scalars()
        .all()
    )
    return [int(i) for i in ids]


async def revert_to_pending(db: AsyncSession, cb, *, now: datetime) -> None:
    """Put the booking back on the admin's queue, unanswered and unexpired."""
    await db.execute(
        text(
            "UPDATE cab_bookings SET booking_status = 'PENDING_ASSIGNMENT', "
            "       acceptance_deadline = NULL, pending_partner_id = NULL, "
            "       partner_responded_at = :now, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"now": now, "id": cb.id},
    )


async def _timeline(
    db: AsyncSession, cb, event_type: str, detail: str, now: datetime
) -> None:
    await db.execute(
        text(
            "INSERT INTO booking_timelines "
            "       (master_booking_id, event_type, event_description, "
            "        event_timestamp, created_by) "
            "VALUES (:mb, :type, :detail, :ts, NULL)"
        ),
        {
            "mb": cb.master_booking_id,
            "type": event_type,
            "detail": detail,
            "ts": now,
        },
    )


async def advance_after_offers_closed(
    db: AsyncSession,
    cb,
    *,
    city_id: int,
    now: Optional[datetime] = None,
    reason_for_next_round: str,
) -> tuple[str, list[dict]]:
    """One round has ended with nobody accepting. Decide what happens next.

    Returns `(outcome, offered)` where outcome is one of:

      `NEXT_ROUND`   — another group was offered,
      `WAITING`      — offers are still open with other partners,
      `FOR_HUMAN`    — the chain stops and the booking goes back to the queue,
      `EXHAUSTED`    — the chain stopped because it ran out of rounds, marked on
                       the booking so the queue can show why nobody is being asked.

    Disabled auto-assign takes the `FOR_HUMAN` path, which is exactly what the
    sweeper did before this service existed.
    """
    when = now or datetime.now(timezone.utc)

    if await open_offer_partner_ids(db, cb.id):
        return ("WAITING", [])

    config = await settings(db)
    if not config.enabled:
        await revert_to_pending(db, cb, now=when)
        return ("FOR_HUMAN", [])

    rounds = await published_rounds(db, cb.id)
    if rounds >= config.max_rounds:
        await revert_to_pending(db, cb, now=when)
        await db.execute(
            text(
                "UPDATE cab_bookings SET needs_attention = "
                "              'AUTO_ASSIGN_EXHAUSTED' "
                "WHERE id = :id"
            ),
            {"id": cb.id},
        )
        await _timeline(
            db,
            cb,
            "AUTO_ASSIGN_EXHAUSTED",
            (
                f"Auto-assign stopped after {rounds} round(s) with nobody "
                f"accepting ({reason_for_next_round}). Cab {cb.booking_number} "
                "is back on the assignment queue and needs a partner picked by "
                "hand."
            ),
            when,
        )
        return ("EXHAUSTED", [])

    cohort = await _coupon_cohort(db, cb.id)
    offered = await publish_round(
        db,
        cb,
        city_id=city_id,
        cohort_ids=cohort,
        settings_=config,
        now=when,
    )
    if not offered:
        await revert_to_pending(db, cb, now=when)
        await _timeline(
            db,
            cb,
            "AUTO_ASSIGN_NO_PARTNERS",
            (
                f"Auto-assign stopped: no partner in this city has a free cab of "
                f"the booked category ({reason_for_next_round}). Cab "
                f"{cb.booking_number} is back on the assignment queue."
            ),
            when,
        )
        return ("FOR_HUMAN", [])

    return ("NEXT_ROUND", offered)


async def begin_chain(db: AsyncSession, cb, *, city_id: int) -> list[dict]:
    """Start the chain for a booking that has just been created.

    A no-op when auto-assign is switched off, and a no-op once the booking has
    any assignment row at all — a hand-picked partner is already an answer to
    "who is being asked", and this service does not second-guess one.
    """
    config = await settings(db)
    if not config.enabled:
        return []

    already_offered = (
        await db.execute(
            text(
                "SELECT 1 FROM cab_booking_assignments "
                "WHERE cab_booking_id = :id LIMIT 1"
            ),
            {"id": cb.id},
        )
    ).scalar()
    if already_offered is not None:
        return []

    cohort = await _coupon_cohort(db, cb.id)
    return await publish_round(
        db, cb, city_id=city_id, cohort_ids=cohort, settings_=config
    )


async def notify_offered_partners(db: AsyncSession, cb, offered: list[dict]) -> None:
    """The popup + push each partner needs to answer in time.

    Best effort, one partner at a time: a partner whose notification fails still
    has the offer row, and the booking must not change state because a push
    failed.
    """
    from app.modules.notification.services.booking_notifications import (
        partner_assignment_requested,
    )

    for partner in offered:
        try:
            await partner_assignment_requested(
                db,
                partner_id=int(partner["partner_id"]),
                master_booking_id=cb.master_booking_id,
                cab_booking_number=cb.booking_number,
                pickup_location=cb.pickup_location,
                pickup_datetime=(
                    cb.pickup_datetime.isoformat() if cb.pickup_datetime else None
                ),
                deadline_iso=str(partner["deadline_iso"]),
            )
        except Exception as exc:  # pragma: no cover - never block the chain
            logger.warning(
                "auto_assign.notify_failed cab=%s partner_id=%s err=%s",
                getattr(cb, "booking_number", "?"),
                partner.get("partner_id"),
                exc,
            )
