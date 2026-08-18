# ============================================================
# WAYTERO — TIMEOUT SWEEPER
# File: app/workers/timeout_sweeper.py
# Doc Ref:
#   BRD Part 3 §42 — Driver Assignment after partner acceptance
#   BRD Part 7 §155 — Realtime + push channel
#
# Periodic task: every minute, find cab bookings stuck in
# PENDING_PARTNER_ACCEPTANCE whose acceptance_deadline has passed
# and revert them to PENDING_ASSIGNMENT. The latest assignment row
# for the cab is marked rejected_at=NOW with rejection_reason_code
# TIMEOUT so the audit trail records that no human responded.
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
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.modules.booking.models import (
    CabBooking,
    CabBookingAssignment,
    BookingTimeline,
)
from app.modules.partner.constants import SYSTEM_REJECT_REASON_TIMEOUT
from app.workers.celery_app import celery_app

logger = logging.getLogger("waytero.sweeper")


async def _publish_timeout_events(cabs: list[CabBooking]) -> None:
    """Push WS events for the cabs we just reverted.

    Uses a fresh session for the partner lookup so we don't extend the
    original transaction. Best-effort — a failure here must not break
    the sweeper.
    """
    try:
        from app.modules.notification.realtime import manager as rt_manager
        from app.modules.partner.models import Partner
    except Exception as exc:  # pragma: no cover - import-time guard
        logger.warning("sweeper.import_failed err=%s", exc)
        return

    async with AsyncSessionLocal() as db:
        for cb in cabs:
            partner_user_id = None
            if cb.pending_partner_id is not None:
                partner_user_id = (
                    await db.execute(
                        select(Partner.user_id).where(
                            Partner.id == cb.pending_partner_id
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
                            "cab_status": "PENDING_ASSIGNMENT",
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
                        "new_status": "PENDING_ASSIGNMENT",
                        "reason": "PARTNER_ACCEPTANCE_TIMEOUT",
                    },
                }
            )


async def _expire_pending_acceptances_async() -> int:
    """
    Async core: scan + revert. Returns count of cabs reverted so the
    Celery log line carries a useful number.

    Run inside its own AsyncSession because Celery worker processes
    don't share the FastAPI request session.
    """
    affected_cabs: list[CabBooking] = []
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
            latest: Optional[CabBookingAssignment] = None
            if cb.assignments:
                latest = sorted(
                    cb.assignments,
                    key=lambda a: a.assigned_at,
                    reverse=True,
                )[0]
            if latest and latest.accepted_at is None and latest.rejected_at is None:
                latest.rejected_at = now
                latest.rejection_reason_code = SYSTEM_REJECT_REASON_TIMEOUT
                latest.rejection_notes = (
                    f"Auto-rejected by sweeper after deadline "
                    f"{cb.acceptance_deadline.isoformat() if cb.acceptance_deadline else 'n/a'}"
                )

            cb.booking_status = "PENDING_ASSIGNMENT"  # type: ignore[assignment]
            cb.partner_responded_at = now  # type: ignore[assignment]
            cb.acceptance_deadline = None  # type: ignore[assignment]
            cb.pending_partner_id = None  # type: ignore[assignment]

            db.add(
                BookingTimeline(
                    master_booking_id=cb.master_booking_id,
                    event_type="PARTNER_ACCEPTANCE_TIMEOUT",
                    event_description=(
                        f"Partner did not respond within the acceptance window. "
                        f"Cab {cb.booking_number} returned to PENDING_ASSIGNMENT."
                    ),
                    event_timestamp=now,
                )
            )
            affected_cabs.append(cb)

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
    worker pool expects sync callables.
    """
    import asyncio

    try:
        return asyncio.run(_expire_pending_acceptances_async())
    except Exception as exc:  # pragma: no cover - defensive retry path
        raise self.retry(exc=exc)
