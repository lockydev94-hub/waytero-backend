"""Partner-funded cab offer audit and exhaustion (caller owns the transaction)."""

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException, ResourceNotFoundException
from app.modules.booking.models import CabBooking, CabBookingAssignment


async def _partner_funded_usage(db: AsyncSession, cab_booking_id: int):
    # The redemption snapshot, never the coupon's mutable current configuration.
    return (
        (
            await db.execute(
                text(
                    """
                SELECT coupon_id, eligible_partner_ids FROM coupon_usages
                WHERE cab_booking_id = :id
                  AND coupon_type IN ('PARTNER_ONLY', 'PLATFORM_PARTNER')
                """
                ),
                {"id": cab_booking_id},
            )
        )
        .mappings()
        .one_or_none()
    )


async def ensure_coupon_partner_eligible(
    db: AsyncSession, cb: CabBooking, partner_id: int
) -> None:
    """An old offer cannot authorize fulfillment outside the joined cohort."""
    usage = await _partner_funded_usage(db, cb.id)
    if usage is None:
        return
    cohort = set(usage["eligible_partner_ids"] or [])
    joined = (
        await db.execute(
            text(
                """
                SELECT id FROM coupon_partner_rules
                WHERE coupon_id = :coupon_id AND partner_id = :partner_id
                FOR SHARE
                """
            ),
            {"coupon_id": usage["coupon_id"], "partner_id": partner_id},
        )
    ).first()
    if joined is None or (cohort and partner_id not in cohort):
        raise BusinessException(
            "Only an eligible joined coupon partner can fulfill this cab booking.",
            code="COUPON_PARTNER_NOT_ELIGIBLE",
        )


async def record_coupon_response_availability(
    db: AsyncSession, cb: CabBooking, assignment: CabBookingAssignment
) -> None:
    """Snapshot actual matching supply BEFORE writing a rejection/timeout.

    Zero is evidence of no supply, not refusal. Unknown/error is never turned
    into evidence. TIMEOUT remains a system response regardless of this count.
    """
    if await _partner_funded_usage(db, cb.id) is None:
        return
    if (
        assignment.rejection_reason_code == "ADMIN_REASSIGN"
        or assignment.close_reason == "ADMIN_REASSIGN"
    ):
        return
    if assignment.cab_booking_id != cb.id:
        raise BusinessException("Assignment does not belong to this cab booking.")
    city_id = (
        await db.execute(
            text("SELECT city_id FROM master_bookings WHERE id = :id"),
            {"id": cb.master_booking_id},
        )
    ).scalar_one_or_none()
    if city_id is None or cb.vehicle_category_id is None or cb.pickup_datetime is None:
        raise BusinessException(
            "Coupon availability requires the booking city, vehicle category and pickup time.",
            code="COUPON_AVAILABILITY_CONTEXT_REQUIRED",
        )
    if cb.trip_type == "ROUND_TRIP" and cb.return_datetime is None:
        raise BusinessException(
            "Round-trip coupon availability requires the return time.",
            code="COUPON_AVAILABILITY_CONTEXT_REQUIRED",
        )

    from app.modules.booking.services.coupon_availability import available_cab_counts

    pid = int(assignment.partner_id)
    counts = await available_cab_counts(
        db,
        [pid],
        city_id,
        cb.vehicle_category_id,
        cb.pickup_datetime,
        cb.return_datetime,
        exclude_cab_id=cb.id,
    )
    count = counts.get(pid)
    if type(count) is not int or count < 0:
        raise BusinessException(
            "Coupon partner availability could not be established.",
            code="COUPON_AVAILABILITY_UNKNOWN",
        )
    assignment.available_cabs_at_decline = count
    assignment.availability_checked_at = datetime.now(timezone.utc)


async def cancel_if_coupon_exhausted(db: AsyncSession, cb: CabBooking) -> bool:
    """Cancel only when every snapshotted candidate declined or timed out.

    Callers lock the cab before changing the offer. Flush ORM responses before
    SQL reads/writes; lock again here to serialize standalone exhaustion checks.
    No commit, notifications, coupon conversion or usage-counter decrement.
    """
    await db.flush()
    state = (
        await db.execute(
            text(
                "SELECT booking_status, trip_started_at FROM cab_bookings "
                "WHERE id = :id FOR UPDATE"
            ),
            {"id": cb.id},
        )
    ).first()
    if state is None:
        raise ResourceNotFoundException("Cab booking", cb.id)
    if (
        state[0] not in {"PENDING_ASSIGNMENT", "PENDING_PARTNER_ACCEPTANCE"}
        or state[1] is not None
    ):
        return False
    usage = await _partner_funded_usage(db, cb.id)
    if usage is None or not usage["eligible_partner_ids"]:
        return False
    cohort = set(usage["eligible_partner_ids"])
    assignments = (
        (
            await db.execute(
                text(
                    """
                SELECT partner_id, accepted_at, rejected_at, rejection_reason_code,
                       closed_at, close_reason
                FROM cab_booking_assignments WHERE cab_booking_id = :id
                ORDER BY assigned_at DESC, id DESC
                """
                ),
                {"id": cb.id},
            )
        )
        .mappings()
        .all()
    )
    latest_responses: dict[int, bool] = {}
    for assignment in assignments:
        # Even a non-cohort live offer must be resolved before cancellation.
        if assignment["closed_at"] is None and (
            assignment["accepted_at"] is not None
            or (
                assignment["rejected_at"] is None
                and assignment["rejection_reason_code"] != "TIMEOUT"
            )
        ):
            return False
        if (
            assignment["rejection_reason_code"] == "ADMIN_REASSIGN"
            or assignment["close_reason"] == "ADMIN_REASSIGN"
            or assignment["partner_id"] not in cohort
        ):
            continue
        if assignment["accepted_at"] is not None:
            # A later acceptance supersedes an earlier decline; it is not failure.
            latest_responses.setdefault(assignment["partner_id"], False)
        elif (
            assignment["rejected_at"] is not None
            or assignment["rejection_reason_code"] == "TIMEOUT"
        ):
            latest_responses.setdefault(assignment["partner_id"], True)
    if not all(latest_responses.get(partner_id, False) for partner_id in cohort):
        return False

    from app.modules.booking.services.cancellation_policy import apply_cab_cancellation

    await apply_cab_cancellation(
        db,
        cab_booking_id=cb.id,
        master_booking_id=cb.master_booking_id,
        cancellation_reason="All eligible coupon partners declined or timed out",
        cancelled_by_user_id=None,
        cancelled_by_role="SYSTEM",
        cancelled_source="SYSTEM",
        supplier_failure=True,
    )
    # Raw SQL cancellation must not be overwritten by the caller's ORM instance.
    await db.refresh(cb)
    return True
