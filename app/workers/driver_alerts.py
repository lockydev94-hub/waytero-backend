# ============================================================
# WAYTERO — DRIVER ALERT SWEEPER
# File: app/workers/driver_alerts.py
# Doc Ref: 04_API_Documentation/13_DRIVER_APP_API.md
#          06_Backend_Architecture/11_DRIVER_APP_BACKEND.md §5.2
#
# Runs every minute:
#   1. Auto-resumes trips paused for more than five minutes. This is the
#      server-side safety net for a driver who closes the app after pausing.
#   2. Sends one DRIVER_BOOKING_OVERDUE push when the scheduled pickup time
#      has passed and the driver has still not started the trip.
#
# Both operations are idempotent: pause state is cleared before commit, and the
# notification outbox row is the duplicate guard for the overdue push.
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal, run_worker_async
from app.modules.booking.models import (
    BookingTimeline,
    CabBooking,
    CabBookingAssignment,
    MasterBooking,
)
from app.modules.driver.constants import (
    DRIVER_BOOKING_OVERDUE_EVENT,
    DRIVER_PAUSE_AUTO_RESUME_SECONDS,
)
from app.modules.driver.models import Driver, DriverAvailability
from app.modules.notification.models import NotificationOutbox
from app.workers.celery_app import celery_app

logger = logging.getLogger("waytero.driver_alerts")


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _auto_resume_paused() -> list[dict]:
    """Resume trips whose pause exceeded the auto-resume deadline.

    Returns a small serializable summary for the log and the realtime fan-out.
    The row lock plus the paused_at check makes this safe if the task overlaps
    with another worker or the driver tapping Resume at the same moment.
    """
    now = datetime.now(timezone.utc)
    resumed: list[dict] = []

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(CabBooking)
                    .options(selectinload(CabBooking.assignments))
                    .where(
                        CabBooking.booking_status == "STARTED",
                        CabBooking.trip_paused_at.is_not(None),
                        CabBooking.trip_paused_at
                        <= now - timedelta(seconds=DRIVER_PAUSE_AUTO_RESUME_SECONDS),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for cb in rows:
            assignment = next(
                (
                    row
                    for row in (cb.assignments or [])
                    if row.closed_at is None and row.driver_id is not None
                ),
                None,
            )
            if assignment is None:
                continue
            paused_from = _aware(cb.trip_paused_at)
            if paused_from is None:
                continue

            elapsed = max(0, int((now - paused_from).total_seconds()))
            # Core update rather than mutating the ORM object: Celery imports
            # only these task modules, and flushing a full CabBooking requires
            # every table its many FKs point at to be registered.
            await db.execute(
                update(CabBooking)
                .where(CabBooking.id == cb.id)
                .values(
                    trip_paused_seconds=int(cb.trip_paused_seconds or 0) + elapsed,
                    trip_paused_at=None,
                    # Never bridge the pause gap with a straight line on the map.
                    trip_last_lat=None,
                    trip_last_lng=None,
                    trip_last_gps_at=None,
                )
            )

            availability = (
                await db.execute(
                    select(DriverAvailability).where(
                        DriverAvailability.driver_id == assignment.driver_id
                    )
                )
            ).scalar_one_or_none()
            if availability is None:
                db.add(
                    DriverAvailability(
                        driver_id=assignment.driver_id,
                        availability_status="ON_TRIP",
                    )
                )
            else:
                await db.execute(
                    update(DriverAvailability)
                    .where(DriverAvailability.id == availability.id)
                    .values(availability_status="ON_TRIP")
                )

            driver_user_id = (
                await db.execute(
                    select(Driver.user_id).where(Driver.id == assignment.driver_id)
                )
            ).scalar_one_or_none()

            db.add(
                BookingTimeline(
                    master_booking_id=cb.master_booking_id,
                    event_type="TRIP_AUTO_RESUMED",
                    event_description=(
                        f"Trip {cb.booking_number} auto-resumed after "
                        f"{elapsed // 60} minute(s) paused."
                    ),
                    event_timestamp=now,
                )
            )
            resumed.append(
                {
                    "booking_number": cb.booking_number,
                    "service_id": cb.id,
                    "master_booking_id": cb.master_booking_id,
                    "partner_id": assignment.partner_id,
                    "driver_user_id": (
                        str(driver_user_id) if driver_user_id is not None else None
                    ),
                    "paused_seconds": elapsed,
                }
            )

        if resumed:
            await db.commit()

    if not resumed:
        return []

    # Best-effort realtime notification after the state change is durable.
    try:
        from app.modules.notification.realtime import manager as rt_manager
        from app.modules.notification.services.booking_notifications import (
            notify_booking_updated,
        )

        async with AsyncSessionLocal() as db:
            for item in resumed:
                try:
                    await notify_booking_updated(
                        db,
                        service_type="CAB",
                        master_booking_id=item["master_booking_id"],
                        service_id=item["service_id"],
                        booking_number=item["booking_number"],
                        service_status="STARTED",
                        action="DRIVER_AUTO_RESUME_TRIP",
                        partner_ids=[item["partner_id"]],
                    )
                except Exception as exc:  # pragma: no cover - best effort
                    logger.warning(
                        "driver_alerts.partner_notify_failed bn=%s err=%s",
                        item["booking_number"],
                        exc,
                    )
                if item.get("driver_user_id"):
                    try:
                        await rt_manager.send_to_user(
                            item["driver_user_id"],
                            {
                                "event": "DRIVER_TRIP_AUTO_RESUMED",
                                "data": {
                                    "cab_booking_number": item["booking_number"],
                                    "paused_seconds": item["paused_seconds"],
                                },
                            },
                        )
                    except Exception:  # pragma: no cover - best effort
                        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("driver_alerts.resume_notify_failed err=%s", exc)

    return resumed


async def _notify_overdue_pickups() -> int:
    """Send one overdue-pickup push per driver/booking.

    The assignment must still be open and the booking must still be waiting for
    the trip to start. The outbox row for the same master booking is the
    idempotency key, so the minute-by-minute sweep cannot spam the driver.
    """
    now = datetime.now(timezone.utc)
    candidates: list[dict] = []

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    CabBookingAssignment,
                    CabBooking,
                    MasterBooking,
                    Driver.user_id,
                )
                .join(CabBooking, CabBooking.id == CabBookingAssignment.cab_booking_id)
                .join(
                    MasterBooking,
                    MasterBooking.id == CabBooking.master_booking_id,
                )
                .join(Driver, Driver.id == CabBookingAssignment.driver_id)
                .where(
                    CabBookingAssignment.closed_at.is_(None),
                    CabBookingAssignment.driver_id.is_not(None),
                    Driver.user_id.is_not(None),
                    CabBooking.booking_status == "DRIVER_ASSIGNED",
                    CabBooking.pickup_datetime.is_not(None),
                    CabBooking.pickup_datetime <= now,
                    or_(
                        CabBookingAssignment.offer_status == "ACCEPTED",
                        and_(
                            CabBookingAssignment.offer_status == "PENDING",
                            or_(
                                CabBookingAssignment.offer_expires_at.is_(None),
                                CabBookingAssignment.offer_expires_at > now,
                            ),
                        ),
                    ),
                )
            )
        ).all()

        for assignment, cb, mb, driver_user_id in rows:
            existing = (
                await db.execute(
                    select(NotificationOutbox.id)
                    .where(
                        NotificationOutbox.user_id == driver_user_id,
                        NotificationOutbox.event_type == DRIVER_BOOKING_OVERDUE_EVENT,
                        NotificationOutbox.booking_id == mb.id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            pickup_at = _aware(cb.pickup_datetime)
            if pickup_at is None:
                continue
            candidates.append(
                {
                    "driver_id": assignment.driver_id,
                    "master_booking_id": mb.id,
                    "booking_number": cb.booking_number,
                    "pickup_datetime": pickup_at.isoformat(),
                    "minutes_late": max(
                        0, int((now - pickup_at).total_seconds() // 60)
                    ),
                }
            )

    if not candidates:
        return 0

    sent = 0
    for item in candidates:
        async with AsyncSessionLocal() as db:
            try:
                from app.modules.notification.services.booking_notifications import (
                    driver_booking_overdue,
                )

                await driver_booking_overdue(
                    db,
                    driver_id=item["driver_id"],
                    master_booking_id=item["master_booking_id"],
                    cab_booking_number=item["booking_number"],
                    pickup_datetime=item["pickup_datetime"],
                    minutes_late=item["minutes_late"],
                )
                sent += 1
            except Exception as exc:  # pragma: no cover - one bad push only
                logger.warning(
                    "driver_alerts.overdue_notify_failed bn=%s err=%s",
                    item["booking_number"],
                    exc,
                )
    return sent


async def _sweep_async() -> dict[str, Any]:
    resumed = await _auto_resume_paused()
    overdue = await _notify_overdue_pickups()
    return {
        "auto_resumed": len(resumed),
        "overdue_notified": overdue,
    }


@celery_app.task(
    name="waytero.driver_alerts.sweep",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def sweep_driver_alerts(self) -> dict[str, Any]:
    """Celery entry-point. Runs every minute from the beat schedule."""
    try:
        return cast(dict[str, Any], run_worker_async(_sweep_async()))
    except Exception as exc:  # pragma: no cover - defensive retry path
        raise self.retry(exc=exc)
