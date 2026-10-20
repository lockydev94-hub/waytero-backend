# ============================================================
# WAYTERO — TIMEOUT SWEEPER
# File: app/workers/timeout_sweeper.py
# Doc Ref:
#   BRD Part 3 §42 — Driver Assignment after partner acceptance
#   BRD Part 7 §155 — Realtime + push channel
#
# Periodic task: every minute, find cab bookings stuck in
# PENDING_PARTNER_ACCEPTANCE whose acceptance_deadline has passed. Every
# open, unanswered assignment row for the cab is marked rejected_at=NOW with
# rejection_reason_code TIMEOUT — a fan-out round has several of them, so this
# is plural by design — so the audit trail records that nobody responded.
#
# The booking then goes to the auto-assign chain (booking/services/auto_assign):
# with the feature switched off the chain reverts it to PENDING_ASSIGNMENT, which
# is all this task ever did; switched on, it offers the next ranked group of
# partners and only stops, with the reason on the record, when the rounds or the
# available partners run out.
#
# After commit we publish:
#   - WS event to the partner (so any open tab drops the Accept/Reject
#     popup)
#   - WS broadcast event for admin observers (so the admin booking
#     detail page refetches and the countdown chip clears)
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal, run_worker_async
from app.modules.booking.models import (
    CabBooking,
    BookingTimeline,
)
from app.modules.partner.constants import SYSTEM_REJECT_REASON_TIMEOUT
from app.workers.celery_app import celery_app

logger = logging.getLogger("waytero.sweeper")


async def _publish_timeout_events(
    cabs: list[tuple[CabBooking, list[int]]],
) -> None:
    """Push WS events to every partner whose offer just expired.

    A fan-out round asks several partners at once, so a timeout is not one
    partner's news — each unanswered offer closes and each of those partners has
    to see their popup go away. Uses a fresh session for the partner lookup so we
    don't extend the original transaction. Best-effort — a failure here must not
    break the sweeper.
    """
    try:
        from app.modules.notification.realtime import manager as rt_manager
        from app.modules.partner.models import Partner
    except Exception as exc:  # pragma: no cover - import-time guard
        logger.warning("sweeper.import_failed err=%s", exc)
        return

    async with AsyncSessionLocal() as db:
        for cb, recipient_partner_ids in cabs:
            for recipient_partner_id in recipient_partner_ids:
                partner_user_id = (
                    await db.execute(
                        select(Partner.user_id).where(
                            Partner.id == recipient_partner_id
                        )
                    )
                ).scalar_one_or_none()

                if partner_user_id is not None:
                    await rt_manager.send_to_user(
                        partner_user_id,
                        {
                            "event": "BOOKING_PARTNER_RESPONDED",
                            "data": {
                                "cab_booking_number": cb.booking_number,
                                "master_booking_id": cb.master_booking_id,
                                "decision": "TIMED_OUT",
                                "cab_status": cb.booking_status,
                            },
                        },
                    )

            # Admin-side nudge — broadcast because we don't track
            # per-admin WS subscribers yet. Any admin tab receives it
            # and refetches the booking detail.
            await rt_manager.broadcast(
                {
                    "event": "BOOKING_ADMIN_REFRESH",
                    "data": {
                        "cab_booking_number": cb.booking_number,
                        "master_booking_id": cb.master_booking_id,
                        "new_status": cb.booking_status,
                        "reason": "PARTNER_ACCEPTANCE_TIMEOUT",
                    },
                }
            )


async def _expire_pending_acceptances_async() -> int:
    """
    Async core: scan the expired acceptance windows, close the unanswered
    offers and hand the booking to the auto-assign chain. Returns the number of
    cabs whose window closed so the Celery log line carries a useful number.

    Run inside its own AsyncSession because Celery worker processes
    don't share the FastAPI request session.
    """
    from app.modules.booking.services.auto_assign import (
        advance_after_offers_closed,
    )
    from app.modules.booking.services.coupon_dispatch import (
        cancel_if_coupon_exhausted,
        record_coupon_response_availability,
    )

    affected_cabs: list[tuple[CabBooking, list[int]]] = []
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Lock + load candidates so two workers can't double-revert the
        # same row. SKIP LOCKED keeps the second worker moving on to the
        # next cab instead of waiting.
        candidates_q = (
            select(CabBooking)
            .options(selectinload(CabBooking.assignments))
            .where(
                CabBooking.booking_status == "PENDING_PARTNER_ACCEPTANCE",
                CabBooking.acceptance_deadline.is_not(None),
                CabBooking.acceptance_deadline < now,
            )
            .with_for_update(skip_locked=True)
        )
        cabs = (await db.execute(candidates_q)).scalars().all()
        if not cabs:
            return 0

        for cb in cabs:
            # The offer is the set of open, unanswered rows — one for a partner
            # an admin picked by hand, several for a fan-out round. Read from the
            # rows rather than `pending_partner_id`, because that column holds at
            # most one name and a round asks more than one.
            open_rows = [
                a
                for a in cb.assignments
                if a.closed_at is None
                and a.accepted_at is None
                and a.rejected_at is None
                and a.rejection_reason_code is None
            ]
            if not open_rows:
                # Somebody answered, or a human moved the booking on. A closing
                # window is not licence to write over a response.
                continue

            timed_out_partners = sorted({int(a.partner_id) for a in open_rows})
            for row in open_rows:
                await record_coupon_response_availability(db, cb, row)
                row.rejected_at = now
                row.rejection_reason_code = SYSTEM_REJECT_REASON_TIMEOUT
                row.rejection_notes = (
                    "Auto-rejected by sweeper after deadline "
                    f"{cb.acceptance_deadline.isoformat()}"
                )
                row.closed_at = now
                row.close_reason = SYSTEM_REJECT_REASON_TIMEOUT
            # Written before the chain reads the table: whether any offer is
            # still open is what decides round two.
            await db.flush()

            plural = "s" if len(timed_out_partners) != 1 else ""
            db.add(
                BookingTimeline(
                    master_booking_id=cb.master_booking_id,
                    event_type="PARTNER_ACCEPTANCE_TIMEOUT",
                    event_description=(
                        f"Partner did not respond within the acceptance window. "
                        f"Cab {cb.booking_number}: {len(timed_out_partners)} "
                        f"offer{plural} timed out."
                    ),
                    event_timestamp=now,
                )
            )

            city_id = (
                await db.execute(
                    text(
                        "SELECT city_id FROM master_bookings WHERE id = :id"
                    ),
                    {"id": cb.master_booking_id},
                )
            ).scalar_one_or_none()

            # Auto-assign off, this reverts the booking to PENDING_ASSIGNMENT
            # exactly as it always did. On, it offers the next ranked group and
            # only stops — with the reason on the record — when the rounds or the
            # partners run out.
            await advance_after_offers_closed(
                db,
                cb,
                city_id=city_id,
                now=now,
                reason_for_next_round=(
                    f"{len(timed_out_partners)} partner"
                    f"{plural} did not answer within the acceptance window"
                ),
            )
            # After the chain, not before: with rounds left it has just asked a
            # new group, and a coupon booking whose partners have all now refused
            # or timed out is the only case that cancels. Deciding this first
            # would cancel a booking the chain was about to fulfil — and the
            # round published afterwards would then reopen a cancelled cab.
            await cancel_if_coupon_exhausted(db, cb)
            # The chain moved the cab with SQL; reload so the status published to
            # the partner and admin tabs below is the status the booking is in.
            await db.refresh(cb)
            affected_cabs.append((cb, timed_out_partners))

        await db.commit()

    if affected_cabs:
        await _publish_timeout_events(affected_cabs)

    return len(affected_cabs)


@celery_app.task(
    name="waytero.timeout_sweeper.expire_pending_partner_acceptances",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def expire_pending_partner_acceptances(self) -> int:
    """
    Celery task entry-point. Runs every minute via the beat schedule.
    Synchronous wrapper around the async core because Celery's default
    worker pool expects sync callables. Uses the loop-safe runner so the
    pooled asyncpg connections are never reused across event loops
    (fixes "Future attached to a different loop" retries every minute).
    """
    try:
        return run_worker_async(_expire_pending_acceptances_async())
    except Exception as exc:  # pragma: no cover - defensive retry path
        raise self.retry(exc=exc)
