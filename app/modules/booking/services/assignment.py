# ============================================================
# WAYTERO — ASSIGNMENT LOOKUP HELPERS
# File: app/modules/booking/services/assignment.py
#
# Single source of truth for "who is currently assigned to this cab".
# Used everywhere we used to write:
#
#     sorted(cb.assignments, key=lambda a: a.assigned_at, reverse=True)[0]
#
# …which is wrong once migration 0041 introduces closed_at: a closed
# (handovered) assignment is also the most-recent, so it would be picked up
# by accident. Active = closed_at IS NULL.
# ============================================================

from __future__ import annotations

from typing import Optional, List

from app.modules.booking.models import CabBooking, CabBookingAssignment


def get_active_assignment(cb: CabBooking) -> Optional[CabBookingAssignment]:
    """Return the currently-active assignment row, or None if none is open.

    Active = closed_at IS NULL. Ties broken by id descending so the most
    recently inserted open row wins.
    """
    open_rows: List[CabBookingAssignment] = [
        a for a in (cb.assignments or []) if a.closed_at is None
    ]
    if not open_rows:
        return None
    open_rows.sort(key=lambda a: (a.assigned_at, a.id), reverse=True)
    return open_rows[0]


def get_active_partner_id(cb: CabBooking) -> Optional[int]:
    """Shortcut for `get_active_assignment(cb).partner_id` with a safe default."""
    a = get_active_assignment(cb)
    return a.partner_id if a else None


def get_active_vehicle_id(cb: CabBooking) -> Optional[int]:
    a = get_active_assignment(cb)
    return a.vehicle_id if a else None


def get_active_driver_id(cb: CabBooking) -> Optional[int]:
    a = get_active_assignment(cb)
    return a.driver_id if a else None


def get_active_partner_assignment(
    cb: CabBooking, partner_id: int
) -> Optional[CabBookingAssignment]:
    """Active assignment for a *specific* partner. Used by partner-side endpoints.

    For partner portal endpoints that filter assignments by partner_id, this
    returns the still-open row matching that partner (or None if the partner
    is no longer on this booking after a handover).
    """
    rows = [
        a
        for a in (cb.assignments or [])
        if a.partner_id == partner_id and a.closed_at is None
    ]
    if not rows:
        return None
    rows.sort(key=lambda a: (a.assigned_at, a.id), reverse=True)
    return rows[0]
