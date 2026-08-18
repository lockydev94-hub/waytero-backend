# ============================================================
# WAYTERO — BREAKDOWN / VEHICLE-SWAP SERVICE
# File: app/modules/booking/services/breakdown.py
# Doc Ref:
#   BRD Part 3 §42 — Driver Assignment after partner acceptance
#   BRD Part 6 §155 — Settlement edge cases for mid-trip changes
#   Spec: Cab Breakdown → Vehicle Swap (in-trip)
#
# The single source of truth for mid-trip vehicle breakdowns. All three flows
# (driver report, partner report, admin manual report) funnel through this
# module so the same business rules and timeline events fire regardless of
# who triggers the breakdown.
#
# Settlement model (locked with the spec):
#   • Same-partner swap      → commission + payout unchanged. Latest assignment
#                              still points at the same partner, so close-trip
#                              and _compute_position behave naturally.
#   • Different-partner handover → original partner gets ZERO payout. Their
#                              assignment row is closed with reason
#                              PARTNER_HANDOVER. The new partner's assignment
#                              row becomes the active one and earns the full
#                              payout when trip closes.
#   • Cash already collected by the broken-down driver → stays with them
#                              (settled off-platform, per spec).
#
# Raises WayTeroException subclasses only — never HTTPException. The handler
# in main.py turns them into the standard response envelope.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.booking.models import (
    BookingTimeline,
    CabBooking,
    CabBookingAssignment,
    MasterBooking,
)
from app.modules.booking.services.assignment import get_active_assignment
from app.modules.partner.constants import BREAKDOWN_REASON_CODES
from app.modules.driver.models import Driver, DriverAvailability
from app.modules.vehicle.models import Vehicle

# ════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════

REPORTER_DRIVER = "DRIVER"
REPORTER_PARTNER = "PARTNER"
REPORTER_ADMIN = "ADMIN"

# Cab-status values that mean a vehicle is on the road (or about to be).
# These are the only states from which a breakdown report is valid.
IN_FLIGHT_STATUSES = frozenset({"DRIVER_ASSIGNED", "STARTED"})

# Cab-status values from which the swap/handover service can resume.
SWAPPABLE_STATUSES = frozenset({"BREAKDOWN_REPORTED", "AWAITING_SWAP"})

# Why an assignment row was closed.
CLOSE_REASON_TRIP_COMPLETED = "TRIP_COMPLETED"
CLOSE_REASON_VEHICLE_BREAKDOWN = "VEHICLE_BREAKDOWN"
CLOSE_REASON_PARTNER_HANDOVER = "PARTNER_HANDOVER"
CLOSE_REASON_ADMIN_REASSIGN = "ADMIN_REASSIGN"

# assignment_type values written by this module.
ASSIGN_TYPE_SWAP_SAME = "SWAP_SAME_PARTNER"
ASSIGN_TYPE_SWAP_HANDOVER = "SWAP_HANDOVER"
ASSIGN_TYPE_BREAKDOWN_REPLACEMENT = "BREAKDOWN_REPLACEMENT"

CONFIG_BREAKDOWN_DEADLINE = "BREAKDOWN_ACCEPTANCE_DEADLINE_MINUTES"
CONFIG_SELF_SERVE_SWAP = "PARTNER_SELF_SERVE_SWAP_ENABLED"

# Vehicle/driver availability strings (kept here so we don't depend on the
# sibling modules' private constants).
VEHICLE_STATUS_ACTIVE = "ACTIVE"
VEHICLE_STATUS_ON_TRIP = "ON_TRIP"
VEHICLE_STATUS_MAINTENANCE = "MAINTENANCE"
DRIVER_AVAIL_BREAK = "BREAK"


# ════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ════════════════════════════════════════════════════════════════


def _get_active_assignment(cb: CabBooking) -> CabBookingAssignment:
    """Return the assignment row that is currently active for this booking.

    "Active" = closed_at IS NULL. Prefer this helper everywhere instead of
    sorting by assigned_at, because a closed assignment is also "latest" by
    timestamp and would otherwise be picked up by accident after a handover.
    """
    open_rows = [a for a in (cb.assignments or []) if a.closed_at is None]
    if not open_rows:
        raise BusinessException(
            f"Booking {cb.booking_number} has no active assignment — cannot proceed.",
            code="NO_ACTIVE_ASSIGNMENT",
        )
    # Most-recent first; ties broken by id (insertion order) for determinism.
    open_rows.sort(key=lambda a: (a.assigned_at, a.id), reverse=True)
    return open_rows[0]


async def _get_cab_with_assignments(
    db: AsyncSession, cab_id: int
) -> tuple[MasterBooking, CabBooking]:
    """Load the cab + master booking + eager-loaded assignments. 404 if missing."""
    row = (
        await db.execute(
            select(CabBooking)
            .options(
                selectinload(CabBooking.assignments),
                selectinload(CabBooking.master_booking),
            )
            .where(CabBooking.id == cab_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ResourceNotFoundException("CabBooking", cab_id)
    mb = row.master_booking
    if mb is None:
        # Schema guarantees it, but defend anyway.
        raise ResourceNotFoundException("MasterBooking for cab", cab_id)
    return mb, row


async def _timeline(
    db: AsyncSession,
    master_booking_id: int,
    event_type: str,
    description: str,
    created_by: Optional[UUID] = None,
) -> None:
    """Append an event to the booking timeline."""
    db.add(
        BookingTimeline(
            master_booking_id=master_booking_id,
            event_type=event_type,
            event_description=description,
            event_timestamp=datetime.now(timezone.utc),
            created_by=created_by,
        )
    )


async def _set_vehicle_status(db: AsyncSession, vehicle_id: int, status: str) -> None:
    """Flip a vehicle's status. No-op if the row doesn't exist."""
    if vehicle_id is None:
        return
    await db.execute(
        update(Vehicle).where(Vehicle.id == vehicle_id).values(status=status)
    )


async def _set_driver_availability(
    db: AsyncSession, driver_id: Optional[int], status: str
) -> None:
    """Flip a driver's availability_status. No-op if the row doesn't exist."""
    if driver_id is None:
        return
    await db.execute(
        update(DriverAvailability)
        .where(DriverAvailability.driver_id == driver_id)
        .values(availability_status=status)
    )


async def _config_bool(db: AsyncSession, key: str, default: bool) -> bool:
    raw = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": key},
        )
    ).scalar_one_or_none()
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


async def _config_int(db: AsyncSession, key: str, default: int) -> int:
    raw = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": key},
        )
    ).scalar_one_or_none()
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _current_km(cb: CabBooking) -> Decimal:
    """The odometer reading right now — end_km if set, else start_km."""
    if cb.trip_end_km is not None:
        return Decimal(str(cb.trip_end_km))
    if cb.trip_start_km is not None:
        return Decimal(str(cb.trip_start_km))
    return Decimal("0.00")


async def _void_partner_side_advances(
    db: AsyncSession,
    cab_booking_id: int,
    *,
    original_partner_id: int,
    original_driver_id: Optional[int],
) -> list[int]:
    """Void every ACTIVE advance payment on this booking whose `received_by`
    was the original partner or driver.

    ADMIN-received advances stay ACTIVE — that money is with the platform
    already. Only PARTNER/DRIVER-received advances are voided so the new
    partner's settlement position doesn't get unfairly debited for cash
    they never touched.

    Returns the list of voided advance IDs so the caller can write a
    timeline entry. Voiding is soft (status → VOIDED, voided_at set) per
    the partial-unique-index design in AdvancePayment.
    """
    from app.modules.booking.models import AdvancePayment

    rows = (
        (
            await db.execute(
                select(AdvancePayment).where(
                    AdvancePayment.cab_booking_id == cab_booking_id,
                    AdvancePayment.status == "ACTIVE",
                )
            )
        )
        .scalars()
        .all()
    )

    voided: list[int] = []
    now = datetime.now(timezone.utc)
    for adv in rows:
        if adv.received_by not in {"PARTNER", "DRIVER"}:
            continue
        # Only void if the advance was with the original (broken-down) side.
        # If received_by=DRIVER, match by driver_id; if PARTNER, match by
        # partner_id.
        if adv.received_by == "DRIVER" and adv.driver_id != original_driver_id:
            continue
        if adv.received_by == "PARTNER" and adv.partner_id != original_partner_id:
            continue
        adv.status = "VOIDED"
        adv.voided_at = now
        adv.void_reason = (
            "Voided on partner handover after vehicle breakdown. The original "
            "partner / driver must reconcile cash physically held with the "
            "platform outside this booking."
        )
        voided.append(adv.id)
    return voided


# We avoid an extra dependency by importing selectinload at module level only
# in this helper (the rest of the file doesn't need eager loading).
from sqlalchemy.orm import selectinload  # noqa: E402  (kept at bottom for grouping)
from sqlalchemy import update  # noqa: E402


# ════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════


async def report_breakdown(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    reported_by: str,
    reported_by_user_id: Optional[UUID],
    reporter_partner_id: Optional[int],
    reporter_driver_id: Optional[int],
    reason_code: str,
    latitude: Optional[Decimal] = None,
    longitude: Optional[Decimal] = None,
    notes: Optional[str] = None,
) -> dict:
    """Mark a cab booking as broken down mid-trip.

    Valid reporters:
      • DRIVER  — must be the driver on the *active* assignment
      • PARTNER — must be the partner on the *active* assignment
      • ADMIN   — any admin staff (used as fallback when driver/partner app
                  is unavailable)

    The cab moves to BREAKDOWN_REPORTED. The current vehicle goes to
    MAINTENANCE, the current driver goes on BREAK. The booking's KM so far
    is snapshotted onto pre_swap_actual_km so the admin dashboard can show
    "8.3 / 32.0 km covered when breakdown happened".

    Idempotency: re-reporting on an already-broken booking just refreshes the
    location/reason fields and is fine. Re-reporting on a non-in-flight
    booking is a ValidationException.
    """
    if reason_code not in BREAKDOWN_REASON_CODES:
        raise ValidationException(
            f"Unknown breakdown reason '{reason_code}'.",
            details={"allowed": sorted(BREAKDOWN_REASON_CODES)},
        )
    if reported_by not in {REPORTER_DRIVER, REPORTER_PARTNER, REPORTER_ADMIN}:
        raise ValidationException(
            f"Unknown reporter '{reported_by}'. Use DRIVER, PARTNER or ADMIN."
        )

    mb, cb = await _get_cab_with_assignments(db, cab_booking_id)

    # Idempotent refresh path — already broken down, just update fields.
    if cb.booking_status in {"BREAKDOWN_REPORTED", "AWAITING_SWAP"}:
        cb.breakdown_reason = reason_code
        if latitude is not None:
            cb.breakdown_latitude = latitude
        if longitude is not None:
            cb.breakdown_longitude = longitude
        if notes:
            cb.breakdown_reported_at = datetime.now(timezone.utc)
        await _timeline(
            db,
            mb.id,
            f"BREAKDOWN_REPORTED_BY_{reported_by}",
            (
                f"Breakdown re-confirmed on {cb.booking_number}. "
                f"Reason={reason_code}. Notes={notes or '-'}"
            ),
            created_by=reported_by_user_id,
        )
        return _breakdown_snapshot(cb)

    if cb.booking_status not in IN_FLIGHT_STATUSES:
        raise BusinessException(
            f"Cannot report breakdown for cab in status '{cb.booking_status}'. "
            "Only DRIVER_ASSIGNED or STARTED bookings can be broken down.",
            code="INVALID_BREAKDOWN_STATE",
        )

    active = _get_active_assignment(cb)

    # Authorization for non-admin reporters.
    if reported_by == REPORTER_DRIVER:
        if reporter_driver_id is None or active.driver_id != reporter_driver_id:
            raise PermissionDeniedException(
                "Only the assigned driver may report a breakdown for this cab."
            )
    elif reported_by == REPORTER_PARTNER:
        if reporter_partner_id is None or active.partner_id != reporter_partner_id:
            raise PermissionDeniedException(
                "Only the assigned partner may report a breakdown for this cab."
            )

    now = datetime.now(timezone.utc)
    current_km = _current_km(cb)

    # Cab-level snapshot.
    cb.booking_status = "BREAKDOWN_REPORTED"
    cb.breakdown_reason = reason_code
    cb.breakdown_latitude = latitude
    cb.breakdown_longitude = longitude
    cb.breakdown_reported_at = now
    cb.breakdown_reported_by = reported_by
    cb.pre_swap_actual_km = current_km
    # Capture original partner on first breakdown so a future handover can
    # identify the "lost-trip" partner for the UI.
    if cb.original_partner_id is None:
        cb.original_partner_id = active.partner_id

    # Vehicle → MAINTENANCE so it stops showing up as assignable.
    if active.vehicle_id is not None:
        cb_v = (
            await db.execute(select(Vehicle).where(Vehicle.id == active.vehicle_id))
        ).scalar_one_or_none()
        if cb_v is not None:
            cb_v.status = VEHICLE_STATUS_MAINTENANCE

    # Driver → BREAK.
    if active.driver_id is not None:
        await _set_driver_availability(db, active.driver_id, DRIVER_AVAIL_BREAK)

    await _timeline(
        db,
        mb.id,
        f"BREAKDOWN_REPORTED_BY_{reported_by}",
        (
            f"Breakdown reported on {cb.booking_number}. "
            f"Reason={reason_code} | "
            f"KmCovered={current_km} | "
            f"Vehicle#{active.vehicle_id}→MAINTENANCE | "
            f"Driver#{active.driver_id}→BREAK | "
            f"Notes={notes or '-'}"
        ),
        created_by=reported_by_user_id,
    )

    # Customer-facing push + outbox (best-effort).
    try:
        from app.modules.notification.services.booking_notifications import (
            customer_breakdown_notice,
        )

        await customer_breakdown_notice(
            db,
            master_booking_id=mb.id,
            cab_booking_number=cb.booking_number,
            reason_label=reason_code.replace("_", " ").title(),
            pickup_location=cb.pickup_location,
        )
    except Exception as exc:  # pragma: no cover - never block on notify
        import logging

        logging.getLogger("waytero.breakdown").warning(
            "customer_notice.failed cab=%s err=%s", cb.booking_number, exc
        )

    return _breakdown_snapshot(cb)


async def swap_vehicle_same_partner(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    new_vehicle_id: int,
    new_driver_id: int,
    performed_by: str,
    performed_by_user_id: Optional[UUID],
    performed_by_partner_id: Optional[int],
) -> dict:
    """Swap the active vehicle/driver to another one belonging to the SAME partner.

    Allowed for ADMIN unconditionally and for PARTNER if
    PARTNER_SELF_SERVE_SWAP_ENABLED is true AND the new resources belong to
    them.

    No commission/payout change — same partner, same commission rule, the
    latest assignment row still points at the same partner_id.
    """
    if performed_by not in {REPORTER_ADMIN, REPORTER_PARTNER}:
        raise ValidationException(
            "swap_vehicle_same_partner may only be performed by ADMIN or the "
            "current partner."
        )

    mb, cb = await _get_cab_with_assignments(db, cab_booking_id)
    if cb.booking_status not in SWAPPABLE_STATUSES:
        raise BusinessException(
            f"Cannot swap vehicle while cab is in '{cb.booking_status}'. "
            "The booking must be in BREAKDOWN_REPORTED or AWAITING_SWAP.",
            code="INVALID_SWAP_STATE",
        )

    active = _get_active_assignment(cb)

    # Resolve new vehicle + driver.
    new_vehicle = (
        await db.execute(select(Vehicle).where(Vehicle.id == new_vehicle_id))
    ).scalar_one_or_none()
    if new_vehicle is None:
        raise ResourceNotFoundException("Vehicle", new_vehicle_id)
    new_driver = (
        await db.execute(select(Driver).where(Driver.id == new_driver_id))
    ).scalar_one_or_none()
    if new_driver is None:
        raise ResourceNotFoundException("Driver", new_driver_id)

    # Same-partner guard.
    if new_vehicle.partner_id != active.partner_id:
        raise BusinessException(
            "Replacement vehicle belongs to a different partner. "
            "Use handover_to_new_partner for cross-partner moves.",
            code="DIFFERENT_PARTNER_NOT_ALLOWED",
        )
    if new_driver.partner_id != active.partner_id:
        raise BusinessException(
            "Replacement driver belongs to a different partner. "
            "Use handover_to_new_partner for cross-partner moves.",
            code="DIFFERENT_PARTNER_NOT_ALLOWED",
        )

    # Authorisation for partner-initiated swaps.
    if performed_by == REPORTER_PARTNER:
        if (
            performed_by_partner_id is None
            or performed_by_partner_id != active.partner_id
        ):
            raise PermissionDeniedException(
                "Partners may only swap their own vehicles."
            )
        enabled = await _config_bool(db, CONFIG_SELF_SERVE_SWAP, default=True)
        if not enabled:
            raise PermissionDeniedException(
                "Partner self-serve swap is disabled by configuration. "
                "Ask an admin to perform the swap."
            )

    # Vehicle/driver must be available.
    if new_vehicle.status not in {VEHICLE_STATUS_ACTIVE}:
        raise BusinessException(
            f"Vehicle {new_vehicle.registration_number} is in status "
            f"'{new_vehicle.status}' and cannot be assigned. Only ACTIVE "
            "vehicles are eligible.",
            code="VEHICLE_NOT_AVAILABLE",
        )
    # Driver active?
    if new_driver.status not in {"APPROVED", "ACTIVE"}:
        raise BusinessException(
            f"Driver '{new_driver.full_name}' is in status "
            f"'{new_driver.status}' and cannot be assigned.",
            code="DRIVER_NOT_AVAILABLE",
        )

    now = datetime.now(timezone.utc)
    current_km = _current_km(cb)

    # Close the old assignment.
    active.closed_at = now
    active.close_reason = CLOSE_REASON_VEHICLE_BREAKDOWN
    active.km_at_assignment_end = current_km

    # Create the new active assignment row.
    new_assign = CabBookingAssignment(
        cab_booking_id=cb.id,
        partner_id=active.partner_id,
        vehicle_id=new_vehicle.id,
        driver_id=new_driver.id,
        assigned_by=performed_by_user_id,
        assigned_at=now,
        assignment_type=ASSIGN_TYPE_SWAP_SAME,
        km_at_assignment_start=current_km,
    )
    db.add(new_assign)

    # Mark new resources busy.
    new_vehicle.status = VEHICLE_STATUS_ON_TRIP
    await _set_driver_availability(db, new_driver.id, "ON_TRIP")

    # Cab-level bookkeeping.
    cb.swap_count = (cb.swap_count or 0) + 1
    cb.last_swap_at = now
    cb.is_breakdown_swap = True
    cb.booking_status = "DRIVER_ASSIGNED"
    # Re-arm partner-acceptance tracking so the standard partner flow is the
    # entry point for `start-trip` next. The new assignment is for the same
    # partner so acceptance isn't required; we just clear the gate fields.
    cb.acceptance_deadline = None
    cb.partner_responded_at = now
    cb.pending_partner_id = None

    await _timeline(
        db,
        mb.id,
        "VEHICLE_SWAPPED_SAME_PARTNER",
        (
            f"Same-partner swap on {cb.booking_number}: "
            f"Vehicle#{active.vehicle_id}→#{new_vehicle.id} | "
            f"Driver#{active.driver_id}→#{new_driver.id} | "
            f"Partner#{active.partner_id} (unchanged) | "
            f"KmAtSwap={current_km}"
        ),
        created_by=performed_by_user_id,
    )

    # Snapshot needs the new assignment id; flush so it's populated.
    await db.flush()
    return _breakdown_snapshot(cb, new_assign_id=new_assign.id)


async def handover_to_new_partner(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    new_partner_id: int,
    performed_by_user_id: Optional[UUID],
    acceptance_deadline_minutes: Optional[int] = None,
) -> dict:
    """Hand a broken-down booking over to a DIFFERENT partner.

    The original partner's assignment row is closed with reason
    PARTNER_HANDOVER. A new assignment row is opened for the new partner —
    vehicle_id and driver_id NULL so the new partner picks them later, just
    like a fresh assignment. The cab moves to PENDING_PARTNER_ACCEPTANCE
    reusing the existing acceptance-flow fields, so the standard partner
    accept/reject/sweeper paths all work.

    Original partner: zero payout. New partner: full payout at trip close.
    """
    mb, cb = await _get_cab_with_assignments(db, cab_booking_id)
    if cb.booking_status not in SWAPPABLE_STATUSES:
        raise BusinessException(
            f"Cannot handover booking while in '{cb.booking_status}'. "
            "The booking must be in BREAKDOWN_REPORTED or AWAITING_SWAP.",
            code="INVALID_HANDOVER_STATE",
        )
    if cb.booking_status in {"SETTLEMENT_PENDING", "SETTLED"}:
        raise BusinessException(
            "Cannot handover a booking that has already been settled.",
            code="BOOKING_ALREADY_SETTLED",
        )

    active = _get_active_assignment(cb)

    # Resolve new partner.
    from app.modules.partner.models import Partner as _Partner  # local import

    new_partner = (
        await db.execute(select(_Partner).where(_Partner.id == new_partner_id))
    ).scalar_one_or_none()
    if new_partner is None:
        raise ResourceNotFoundException("Partner", new_partner_id)
    if new_partner.partner_status not in {"APPROVED", "ACTIVE"}:
        raise BusinessException(
            f"Partner {new_partner.business_name} is in status "
            f"'{new_partner.partner_status}' and cannot accept new bookings.",
            code="PARTNER_NOT_ACTIVE",
        )
    if new_partner.id == active.partner_id:
        raise BusinessException(
            "Handover target is the same partner as the current assignment. "
            "Use swap_vehicle_same_partner instead.",
            code="SAME_PARTNER_USE_SWAP",
        )

    now = datetime.now(timezone.utc)
    current_km = _current_km(cb)
    deadline_minutes = acceptance_deadline_minutes or await _config_int(
        db, CONFIG_BREAKDOWN_DEADLINE, default=15
    )
    deadline = now + timedelta(minutes=deadline_minutes)

    # Close the original assignment.
    active.closed_at = now
    active.close_reason = CLOSE_REASON_PARTNER_HANDOVER
    active.km_at_assignment_end = current_km

    # New assignment: same booking, new partner, no driver/vehicle yet.
    new_assign = CabBookingAssignment(
        cab_booking_id=cb.id,
        partner_id=new_partner.id,
        vehicle_id=None,
        driver_id=None,
        assigned_by=performed_by_user_id,
        assigned_at=now,
        assignment_type=ASSIGN_TYPE_SWAP_HANDOVER,
        acceptance_deadline=deadline,
        km_at_assignment_start=current_km,
    )
    db.add(new_assign)

    # Cab-level state — reuse the existing acceptance-gate fields.
    cb.swap_count = (cb.swap_count or 0) + 1
    cb.last_swap_at = now
    cb.is_breakdown_swap = True
    cb.booking_status = "PENDING_PARTNER_ACCEPTANCE"
    cb.pending_partner_id = new_partner.id
    cb.acceptance_deadline = deadline
    cb.partner_responded_at = None

    # original_partner_id already set on first report; if not (e.g. admin
    # manually clicked handover before any report was logged) make sure it's
    # set so reports can show who lost the trip.
    if cb.original_partner_id is None:
        cb.original_partner_id = active.partner_id

    # ── Settlement custody reset ──────────────────────────────────────────
    # After a handover the new partner is the active assignment, so any
    # cash/advance that was sitting with the broken-down driver or the
    # original partner must NOT be counted against the new partner's
    # settlement position. We:
    #   • Clear cash_pending_at so the new driver doesn't get billed for
    #     cash that physically isn't theirs.
    #   • Zero cash_amount_due (it represents the broken-down driver's takings).
    #   • Void any ACTIVE advance whose received_by is the original partner
    #     or driver. The platform-admin advance stays — that money is already
    #     with WayTero and will still be netted at settlement.
    # The original partner's reconciliation (what they owe the platform from
    # the cash they were holding) is reported separately in the admin
    # settlements dashboard; it is NOT auto-deducted from anyone's wallet here.
    cb.cash_pending_at = "NONE"
    cb.cash_amount_due = Decimal("0.00")
    cb.payment_collected_by = None

    voided_advance_ids = await _void_partner_side_advances(
        db,
        cb.id,
        original_partner_id=active.partner_id,
        original_driver_id=active.driver_id,
    )
    if voided_advance_ids:
        await _timeline(
            db,
            mb.id,
            "ADVANCE_VOIDED_ON_HANDOVER",
            (
                f"Voided {len(voided_advance_ids)} advance payment(s) on "
                f"{cb.booking_number} because the booking was handed over "
                f"from Partner#{active.partner_id}. "
                f"Advance IDs={voided_advance_ids}. "
                "Original partner must reconcile the cash physically held."
            ),
            created_by=performed_by_user_id,
        )

    await _timeline(
        db,
        mb.id,
        "VEHICLE_HANDOVERED_NEW_PARTNER",
        (
            f"Handover on {cb.booking_number}: "
            f"Partner#{active.partner_id}→#{new_partner.id} | "
            f"Original gets zero payout | "
            f"Acceptance deadline={deadline.isoformat()} | "
            f"KmAtHandover={current_km}"
        ),
        created_by=performed_by_user_id,
    )

    await db.flush()
    return _breakdown_snapshot(
        cb,
        new_assign_id=new_assign.id,
        new_partner_id=new_partner.id,
        acceptance_deadline=deadline,
    )


async def resume_after_swap(
    db: AsyncSession,
    *,
    cab_booking_id: int,
    performed_by_user_id: Optional[UUID],
) -> dict:
    """Optional helper: confirm a resumed trip after a same-partner swap.

    Most of the time the partner's `start-trip` endpoint will move the cab
    from DRIVER_ASSIGNED to STARTED and write the normal
    TRIP_STARTED_BY_PARTNER timeline event. This helper just writes a
    TRIP_RESUMED_AFTER_SWAP audit entry if the booking ever passed through
    a breakdown, so reports can show the full timeline. It's a no-op for
    bookings that have never had a breakdown.
    """
    mb, cb = await _get_cab_with_assignments(db, cab_booking_id)
    if not cb.is_breakdown_swap:
        return _breakdown_snapshot(cb)
    await _timeline(
        db,
        mb.id,
        "TRIP_RESUMED_AFTER_SWAP",
        f"Trip {cb.booking_number} resumed after vehicle swap "
        f"(swap_count={cb.swap_count}).",
        created_by=performed_by_user_id,
    )
    return _breakdown_snapshot(cb)


# ════════════════════════════════════════════════════════════════
# NORMAL LIFECYCLE — close the active assignment when trip completes
# ════════════════════════════════════════════════════════════════


async def mark_resources_on_trip(
    db: AsyncSession,
    *,
    vehicle_id: Optional[int],
    driver_id: Optional[int],
    on_trip: bool,
) -> None:
    """Flip a vehicle's status and a driver's availability together.

    Used at trip start (`on_trip=True`) and at trip end / breakdown
    (`on_trip=False`). Centralises the writes so the schema-level states
    Vehicle.ON_TRIP / Vehicle.ACTIVE and DriverAvailability.ON_TRIP / .BREAK
    are kept in sync with reality rather than just being defined in the
    schema and never written.

    Safe to call with None vehicle_id / driver_id — those rows are skipped.
    """
    if vehicle_id is not None:
        await db.execute(
            update(Vehicle)
            .where(Vehicle.id == vehicle_id)
            .values(status=VEHICLE_STATUS_ON_TRIP if on_trip else VEHICLE_STATUS_ACTIVE)
        )
    if driver_id is not None:
        await db.execute(
            update(DriverAvailability)
            .where(DriverAvailability.driver_id == driver_id)
            .values(availability_status=("ON_TRIP" if on_trip else "OFFLINE"))
        )


async def close_assignment_on_trip_complete(
    db: AsyncSession, *, cab_booking_id: int, end_km: Optional[Decimal] = None
) -> Optional[int]:
    """Mark the currently-open assignment row closed with reason
    TRIP_COMPLETED. Called from the partner /admin close-trip endpoints so
    every successful booking lands with a single closed assignment at the
    end and no stale open rows that would confuse "active = closed_at IS NULL".

    Returns the closed assignment's id, or None if there was nothing open
    (which should not happen in a well-formed trip but is tolerated).

    Idempotent — running twice is safe: the second call returns None.
    """
    cb_row = (
        await db.execute(select(CabBooking).where(CabBooking.id == cab_booking_id))
    ).scalar_one_or_none()
    if cb_row is None:
        return None
    active = get_active_assignment(cb_row)
    if active is None:
        return None
    now = datetime.now(timezone.utc)
    active.closed_at = now
    active.close_reason = CLOSE_REASON_TRIP_COMPLETED
    if end_km is not None:
        active.km_at_assignment_end = Decimal(str(end_km))
    elif cb_row.trip_end_km is not None:
        active.km_at_assignment_end = Decimal(str(cb_row.trip_end_km))
    return active.id


# ════════════════════════════════════════════════════════════════
# READ HELPERS
# ════════════════════════════════════════════════════════════════


async def list_active_breakdowns(
    db: AsyncSession, *, limit: int = 50, offset: int = 0
) -> Sequence[CabBooking]:
    """Return cab bookings currently in BREAKDOWN_REPORTED or AWAITING_SWAP."""
    rows = (
        (
            await db.execute(
                select(CabBooking)
                .options(selectinload(CabBooking.assignments))
                .where(
                    CabBooking.booking_status.in_(
                        ["BREAKDOWN_REPORTED", "AWAITING_SWAP"]
                    )
                )
                .order_by(CabBooking.breakdown_reported_at.desc().nullslast())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return rows


def _breakdown_snapshot(
    cb: CabBooking,
    *,
    new_assign_id: Optional[int] = None,
    new_partner_id: Optional[int] = None,
    acceptance_deadline: Optional[datetime] = None,
) -> dict:
    """Plain-dict snapshot returned to API callers."""
    active = None
    open_rows = [a for a in (cb.assignments or []) if a.closed_at is None]
    if open_rows:
        open_rows.sort(key=lambda a: (a.assigned_at, a.id), reverse=True)
        active = open_rows[0]
    return {
        "cab_booking_id": cb.id,
        "cab_booking_number": cb.booking_number,
        "booking_status": cb.booking_status,
        "swap_count": cb.swap_count,
        "is_breakdown_swap": cb.is_breakdown_swap,
        "breakdown_reason": cb.breakdown_reason,
        "breakdown_reported_at": (
            cb.breakdown_reported_at.isoformat() if cb.breakdown_reported_at else None
        ),
        "breakdown_reported_by": cb.breakdown_reported_by,
        "breakdown_latitude": (
            float(cb.breakdown_latitude) if cb.breakdown_latitude is not None else None
        ),
        "breakdown_longitude": (
            float(cb.breakdown_longitude)
            if cb.breakdown_longitude is not None
            else None
        ),
        "pre_swap_actual_km": (
            float(cb.pre_swap_actual_km) if cb.pre_swap_actual_km is not None else None
        ),
        "original_partner_id": cb.original_partner_id,
        "active_assignment_id": active.id if active else new_assign_id,
        "active_partner_id": active.partner_id if active else new_partner_id,
        "active_vehicle_id": active.vehicle_id if active else None,
        "active_driver_id": active.driver_id if active else None,
        "acceptance_deadline": (
            acceptance_deadline.isoformat()
            if acceptance_deadline
            else cb.acceptance_deadline.isoformat() if cb.acceptance_deadline else None
        ),
        "last_swap_at": cb.last_swap_at.isoformat() if cb.last_swap_at else None,
    }
