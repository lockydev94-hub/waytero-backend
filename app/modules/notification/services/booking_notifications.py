# ============================================================
# WAY TERO — BOOKING NOTIFICATION TRIGGERS
# File: app/modules/notification/services/booking_notifications.py
# Doc Ref: BRD Part 3 §42 + BRD Part 7 §155
#
# Thin helpers used by the booking / partner modules to fire
# notifications without each call site re-implementing the data lookup.
#
# Every helper:
#   1. Writes a NotificationOutbox row (durable).
#   2. Pushes over WebSocket (real-time UI).
#   3. Pushes via FCM (background push).
#
# All failures degrade to a log line — callers never get blocked on
# notification delivery.
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notification.services import dispatch, dispatch_many
from app.modules.partner.models import Partner

logger = logging.getLogger("waytero.notifications.booking")

# User types that log into the admin portal. New website bookings fan out
# to every ACTIVE admin user so whoever is online can accept immediately.
ADMIN_USER_TYPES = (
    "SUPER_ADMIN",
    "ADMIN",
    "CCO",
    "VERIFICATION_OFFICER",
    "FINANCE_MANAGER",
)


async def _partner_user_id(db: AsyncSession, partner_id: int) -> Optional[UUID]:
    row = (
        await db.execute(select(Partner.user_id).where(Partner.id == partner_id))
    ).scalar_one_or_none()
    return row


async def _master_payment_status(
    db: AsyncSession, master_booking_id: int
) -> Optional[str]:
    """Resolve the master booking's payment_status (single source of truth —
    cab_bookings has no payment_status column)."""
    from app.modules.booking.models import MasterBooking

    row = (
        await db.execute(
            select(MasterBooking.payment_status).where(
                MasterBooking.id == master_booking_id
            )
        )
    ).scalar_one_or_none()
    return row


async def _admin_user_ids(db: AsyncSession) -> list[UUID]:
    """Active admin-portal user ids (SUPER_ADMIN / ADMIN / staff roles)."""
    from sqlalchemy import text

    # Literal list on purpose — raw text() doesn't expand bind tuples and
    # Postgres coerces the unknown literals to the user_type_enum column.
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id FROM users "
                    "WHERE user_type IN ('SUPER_ADMIN','ADMIN','CCO',"
                    "'VERIFICATION_OFFICER','FINANCE_MANAGER') "
                    "AND is_active = TRUE"
                )
            )
        )
        .scalars()
        .all()
    )
    return [UUID(str(r)) for r in rows]


async def admin_booking_requested(
    db: AsyncSession,
    *,
    master_booking_id: int,
    service_type: str,  # CAB | HOTEL | TOUR
    booking_number: str,
    title: str,
    body: str,
    data: dict | None = None,
) -> None:
    """
    A customer just placed a booking on the website (cab / hotel / tour).
    Every active admin user gets a realtime event + inbox row so the
    admin portal can pop the accept modal with a ringtone.

    When no admin is online the WS push is a no-op, but the inbox rows
    persist — the admin portal re-pulls GET /admin/bookings/accept-queue
    on login to catch up on everything that queued while offline.
    """
    try:
        admin_ids = await _admin_user_ids(db)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("booking_notify.admin_lookup_failed err=%s", exc)
        return
    if not admin_ids:
        return
    payload = {
        "master_booking_id": master_booking_id,
        "service_type": service_type,
        "booking_number": booking_number,
        **(data or {}),
    }
    try:
        await dispatch_many(
            db,
            user_ids=admin_ids,
            event_type="ADMIN_BOOKING_REQUESTED",
            title=title,
            body=body,
            data=payload,
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "booking_notify.admin_dispatch_failed event=ADMIN_BOOKING_REQUESTED err=%s",
            exc,
        )


async def partner_hotel_booking_requested(
    db: AsyncSession,
    *,
    partner_id: int,
    master_booking_id: int,
    reservation_id: int,
    reservation_number: str,
    hotel_name: str,
    check_in_date: str | None,
    check_out_date: str | None,
    total_amount: float | None,
) -> None:
    """A hotel reservation for one of this partner's hotels was placed on
    the website. The partner is the fixed assignee — popup + push so they
    can accept/reject and start managing it."""
    uid = await _partner_user_id(db, partner_id)
    if uid is None:
        logger.warning(
            "booking_notify.skip_hotel partner_id=%s reason=no_user_link", partner_id
        )
        return
    title = f"New hotel booking: {reservation_number}"
    body = (
        f"Hotel {hotel_name or ''} has a new reservation. ".strip()
        + " Please accept or reject to start managing it."
    )
    try:
        await dispatch(
            db,
            user_id=uid,
            event_type="PARTNER_HOTEL_BOOKING_REQUESTED",
            title=title,
            body=body,
            data={
                "reservation_id": reservation_id,
                "master_booking_id": master_booking_id,
                "reservation_number": reservation_number,
                "hotel_name": hotel_name,
                "check_in_date": check_in_date or "",
                "check_out_date": check_out_date or "",
                "total_amount": total_amount,
            },
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "booking_notify.dispatch_failed event=PARTNER_HOTEL_BOOKING_REQUESTED err=%s",
            exc,
        )


async def partner_tour_booking_requested(
    db: AsyncSession,
    *,
    partner_id: int,
    master_booking_id: int,
    tour_booking_id: int,
    tour_booking_number: str,
    package_name: str,
    travel_start_date: str | None,
    persons_count: int | None,
    total_amount: float | None,
) -> None:
    """A customer booked one of this partner's tour packages on the website.
    The package's partner is the fixed assignee — popup + push so they can
    accept/reject the booking."""
    uid = await _partner_user_id(db, partner_id)
    if uid is None:
        logger.warning(
            "booking_notify.skip_tour partner_id=%s reason=no_user_link", partner_id
        )
        return
    title = f"New tour booking: {tour_booking_number}"
    body = (
        f"{package_name or 'Tour package'} has a new booking. "
        f"Please accept or reject to start managing it."
    )
    try:
        await dispatch(
            db,
            user_id=uid,
            event_type="PARTNER_TOUR_BOOKING_REQUESTED",
            title=title,
            body=body,
            data={
                "tour_booking_id": tour_booking_id,
                "master_booking_id": master_booking_id,
                "tour_booking_number": tour_booking_number,
                "package_name": package_name,
                "travel_start_date": travel_start_date or "",
                "persons_count": persons_count,
                "total_amount": total_amount,
            },
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "booking_notify.dispatch_failed event=PARTNER_TOUR_BOOKING_REQUESTED err=%s",
            exc,
        )


async def partner_assignment_requested(
    db: AsyncSession,
    *,
    partner_id: int,
    master_booking_id: int,
    cab_booking_number: str,
    pickup_location: str | None,
    pickup_datetime: str | None,
    deadline_iso: str,
) -> None:
    """
    Admin just assigned the cab to the partner. The partner needs to know
    RIGHT NOW (popup + push) so they can accept or reject before the
    10-minute timer runs out.
    """
    uid = await _partner_user_id(db, partner_id)
    if uid is None:
        logger.warning(
            "booking_notify.skip partner_id=%s reason=no_user_link", partner_id
        )
        return

    title = f"New cab booking: {cab_booking_number}"
    body = (
        f"You've been assigned a cab booking. "
        f"{'Pickup: ' + pickup_location + '. ' if pickup_location else ''}"
        f"Please accept or reject within 10 minutes."
    )
    try:
        await dispatch(
            db,
            user_id=uid,
            event_type="PARTNER_ASSIGNMENT_REQUESTED",
            title=title,
            body=body,
            data={
                "cab_booking_number": cab_booking_number,
                "master_booking_id": master_booking_id,
                "pickup_location": pickup_location or "",
                "pickup_datetime": pickup_datetime or "",
                "acceptance_deadline": deadline_iso,
            },
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "booking_notify.dispatch_failed event=PARTNER_ASSIGNMENT_REQUESTED err=%s",
            exc,
        )


async def partner_responded(
    db: AsyncSession,
    *,
    partner_id: int,
    master_booking_id: int,
    cab_booking_number: str,
    decision: str,  # "ACCEPTED" | "REJECTED" | "TIMED_OUT"
    reason_code: str | None = None,
    reason_label: str | None = None,
) -> None:
    """
    Admin-side notification — the partner accepted/rejected/timed-out.
    Notifies all admin users (staff). The dispatch helper handles
    fanning out via the realtime manager.broadcast channel later if
    needed; for now admins see this in the notifications dropdown.
    """
    # We don't have a direct admin → user mapping here. Admin users
    # pick up admin-side notifications through the audit_logs / in-app
    # inbox. For partner-driven acceptance/rejection events admin is
    # primarily watching the booking detail page, which uses its own
    # WS event (see booking_ws_events below). So this helper is
    # intentionally a no-op for now — kept for forward compatibility.
    _ = (
        partner_id,
        master_booking_id,
        cab_booking_number,
        decision,
        reason_code,
        reason_label,
    )


# ── Breakdown / swap notifications (Spec: Cab Breakdown → Vehicle Swap) ──


async def _customer_user_id(db: AsyncSession, master_booking_id: int) -> Optional[UUID]:
    """Look up the customer's user_id from the master booking."""
    from sqlalchemy import text

    row = (
        await db.execute(
            text(
                "SELECT c.user_id FROM customers c "
                "JOIN master_bookings mb ON mb.customer_id = c.id "
                "WHERE mb.id = :mbid"
            ),
            {"mbid": master_booking_id},
        )
    ).scalar_one_or_none()
    return row


async def customer_breakdown_notice(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_number: str,
    reason_label: str,
    pickup_location: str | None,
) -> None:
    """Customer-facing notification: their cab broke down, ops is on it.

    Sent after every report_breakdown call. Reassures the customer that
    a replacement is being arranged so they don't cancel / leave the
    app in frustration.
    """
    uid = await _customer_user_id(db, master_booking_id)
    if uid is None:
        logger.warning(
            "breakdown_notify.skip master_booking_id=%s reason=no_customer",
            master_booking_id,
        )
        return
    title = "Cab issue — replacement being arranged"
    body = (
        f"Your cab {cab_booking_number} has reported a "
        f"{reason_label.lower()}. Our operations team has been notified "
        f"and is arranging a replacement vehicle. "
        f"We appreciate your patience."
    )
    try:
        await dispatch(
            db,
            user_id=uid,
            event_type="CAB_BREAKDOWN_REPORTED",
            title=title,
            body=body,
            data={
                "cab_booking_number": cab_booking_number,
                "master_booking_id": master_booking_id,
                "pickup_location": pickup_location or "",
            },
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "breakdown_notify.dispatch_failed cab=%s err=%s",
            cab_booking_number,
            exc,
        )


async def customer_swap_notice(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_number: str,
    swap_kind: str,  # "SAME_PARTNER" | "HANDOVER"
    new_vehicle_reg: str | None,
    new_driver_name: str | None,
    new_driver_mobile: str | None,
) -> None:
    """Customer-facing notification after a successful swap/handover.

    Sent after swap_vehicle_same_partner or handover_to_new_partner
    completes so the customer sees the new driver / vehicle details in
    their app right away (they'd otherwise be staring at a stale UI).
    """
    uid = await _customer_user_id(db, master_booking_id)
    if uid is None:
        logger.warning(
            "swap_notify.skip master_booking_id=%s reason=no_customer",
            master_booking_id,
        )
        return
    if swap_kind == "HANDOVER":
        title = "Your cab has been changed"
        body = (
            f"A replacement cab {cab_booking_number} is on the way "
            f"({new_vehicle_reg or 'vehicle TBD'}). "
            f"Driver: {new_driver_name or 'to be assigned'}. "
            f"We apologise for the interruption."
        )
    else:
        title = "Cab details updated"
        body = (
            f"Your cab {cab_booking_number} has been swapped to "
            f"{new_vehicle_reg or 'another vehicle'} with "
            f"{new_driver_name or 'a new driver'} "
            f"({new_driver_mobile or ''}). Same partner — "
            f"trip continues without interruption."
        )
    try:
        await dispatch(
            db,
            user_id=uid,
            event_type="CAB_VEHICLE_SWAPPED",
            title=title,
            body=body,
            data={
                "cab_booking_number": cab_booking_number,
                "master_booking_id": master_booking_id,
                "swap_kind": swap_kind,
                "new_vehicle_reg": new_vehicle_reg or "",
                "new_driver_name": new_driver_name or "",
                "new_driver_mobile": new_driver_mobile or "",
            },
            booking_id=master_booking_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "swap_notify.dispatch_failed cab=%s err=%s",
            cab_booking_number,
            exc,
        )


async def notify_admins_booking_resolved(
    db: AsyncSession,
    *,
    service_type: str,  # CAB | HOTEL | TOUR
    master_booking_id: int,
    service_number: str,
    decision: str,  # CONFIRMED / CANCELLED / REJECTED…
    resolved_by: str = "PARTNER",
) -> None:
    """A partner accepted/rejected a fixed-assignment booking (hotel / tour).

    Tells every online admin so their realtime accept modal drops the item
    and the ringtone stops — otherwise the admin's popup would keep ringing
    for a booking the partner already handled.

    Best-effort WS fan-out only (no outbox rows — this is not a user-facing
    notification, it is a UI-sync signal). Callers must never block on it.
    """
    try:
        admin_ids = await _admin_user_ids(db)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("booking_notify.admin_lookup_failed err=%s", exc)
        return
    if not admin_ids:
        return
    try:
        from app.modules.notification.realtime import manager as _rt

        for uid in admin_ids:
            try:
                await _rt.send_to_user(
                    uid,
                    {
                        "event": "BOOKING_RESOLVED",
                        "data": {
                            "service_type": service_type,
                            "master_booking_id": master_booking_id,
                            "service_number": service_number,
                            "decision": decision,
                            "resolved_by": resolved_by,
                        },
                    },
                )
            except Exception:  # pragma: no cover
                continue
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "booking_notify.resolved_fanout_failed mb=%s err=%s",
            master_booking_id,
            exc,
        )


async def notify_booking_updated(
    db: AsyncSession,
    *,
    service_type: str,  # CAB | HOTEL | TOUR
    master_booking_id: int,
    service_id: int,
    booking_number: str,
    service_status: str,
    action: str,
    payment_status: Optional[str] = None,
    partner_ids: Iterable[int] | None = None,
) -> None:
    """Fan out a `BOOKING_UPDATED` WS event after any cab/tour lifecycle
    mutation (assign / accept / reject / assign-driver / start / close /
    collect-payment / advance / invoice / settle / cancel).

    Delivered to the booking's partner user(s), every online admin user so
    open booking-manage pages on either portal refresh themselves in real
    time, AND the customer who placed the booking so their "My Bookings"
    page (customer-web / customer app) reloads live. Mirrors
    notify_hotel_booking_updated but generic across service types.

    Best-effort WS fan-out only — never blocks a caller.
    """
    try:
        from app.modules.notification.realtime import manager as _rt

        # CabBooking has no payment_status column — the master booking is the
        # source of truth. Resolve it here so call sites can pass None safely.
        if payment_status is None:
            payment_status = await _master_payment_status(db, master_booking_id)

        payload = {
            "event": "BOOKING_UPDATED",
            "data": {
                "service_type": service_type,
                "master_booking_id": master_booking_id,
                "service_id": service_id,
                "booking_number": booking_number,
                "service_status": service_status,
                "action": action,
                "payment_status": payment_status,
            },
        }

        recipients: set[str] = set()
        # The booking's partner(s) — resolve user ids.
        if partner_ids:
            for pid in partner_ids:
                p_user = await _partner_user_id(db, int(pid))
                if p_user is not None:
                    recipients.add(str(p_user))
        # Every online admin user.
        for admin_uid in await _admin_user_ids(db):
            recipients.add(str(admin_uid))
        # The customer who placed the booking — their "My Bookings" page
        # (customer-web / customer app) refreshes in real time.
        try:
            cust_user = await _customer_user_id(db, master_booking_id)
            if cust_user is not None:
                recipients.add(str(cust_user))
        except Exception:  # pragma: no cover - best effort
            pass

        for recipient in recipients:
            try:
                await _rt.send_to_user(recipient, payload)
            except Exception:  # pragma: no cover - one bad socket must not stop others
                continue
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "booking_notify.updated_fanout_failed mb=%s svc=%s err=%s",
            master_booking_id,
            service_type,
            exc,
        )


async def notify_hotel_booking_updated(
    db: AsyncSession,
    *,
    master_booking_id: int,
    reservation_id: int,
    reservation_number: str,
    hotel_status: str,
    action: str,
    partner_id: Optional[int] = None,
    payment_collected_status: Optional[str] = None,
) -> None:
    """Fan out a `HOTEL_BOOKING_UPDATED` WS event after any hotel lifecycle
    mutation (confirm / check-in / in-house / check-out / charges / advance /
    invoice / collect-payment / complete / settle / no-show / cancel / switch /
    split).

    Delivered to the hotel's partner user, every online admin user so the
    open booking-manage page on either portal refreshes itself without the
    user pressing refresh, AND the customer who placed the booking so their
    "My Bookings" page reloads live. Best-effort WS fan-out only — never
    blocks a caller.
    """
    try:
        from app.modules.notification.realtime import manager as _rt

        payload = {
            "event": "HOTEL_BOOKING_UPDATED",
            "data": {
                "service_type": "HOTEL",
                "master_booking_id": master_booking_id,
                "reservation_id": reservation_id,
                "reservation_number": reservation_number,
                "hotel_status": hotel_status,
                "action": action,
                "payment_collected_status": payment_collected_status,
            },
        }

        recipients: set[str] = set()
        # The hotel's partner (owner) — resolve its user id.
        if partner_id is not None:
            p_user = await _partner_user_id(db, partner_id)
            if p_user is not None:
                recipients.add(str(p_user))
        # Every online admin user.
        for admin_uid in await _admin_user_ids(db):
            recipients.add(str(admin_uid))
        # The customer who placed the booking — their "My Bookings" page
        # (customer-web / customer app) refreshes in real time.
        try:
            cust_user = await _customer_user_id(db, master_booking_id)
            if cust_user is not None:
                recipients.add(str(cust_user))
        except Exception:  # pragma: no cover - best effort
            pass

        for recipient in recipients:
            try:
                await _rt.send_to_user(recipient, payload)
            except Exception:  # pragma: no cover - one bad socket must not stop others
                continue
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "booking_notify.hotel_updated_fanout_failed mb=%s res=%s err=%s",
            master_booking_id,
            reservation_id,
            exc,
        )
