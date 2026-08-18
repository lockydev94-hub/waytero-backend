# ============================================================
# WAYTERO — ADMIN CAB BREAKDOWN / SWAP API
# File: app/modules/admin/cab_breakdown_api.py
# Prefix: /admin/cab-ops/breakdown  (registered in app/api/router.py)
# Doc Ref:
#   BRD Part 3 §40  — Auto Assignment Engine
#   BRD Part 3 §42  — Driver Assignment after partner acceptance
#   BRD Part 6 §155 — Settlement edge cases (handover)
#   Spec: Cab Breakdown → Vehicle Swap (in-trip)
#
# Endpoints power the Cab-Ops "Breakdown & Swap" dashboard. Three writes:
#   POST /admin/cab-ops/breakdown/report                — admin logs breakdown
#   POST /admin/cab-ops/breakdown/{cab_id}/swap-same     — same-partner swap
#   POST /admin/cab-ops/breakdown/{cab_id}/handover      — cross-partner
# Three reads (powers the swap / handover modal pickers):
#   GET  /admin/cab-ops/breakdown/active                 — list current
#   GET  /admin/cab-ops/breakdown/{cab_id}/available-vehicles
#   GET  /admin/cab-ops/breakdown/{cab_id}/available-partners
#
# All business rules live in app/modules/booking/services/breakdown.py;
# this file is a thin transport layer.
# ============================================================

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.modules.booking.services import breakdown as breakdown_service
from app.modules.partner.constants import BREAKDOWN_REASONS
from app.shared.responses.base import success_response

router = APIRouter()
ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN", "CCO")
ADMIN_ONLY = ("SUPER_ADMIN", "ADMIN")


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class ReportBreakdownRequest(BaseModel):
    reason_code: str = Field(..., description="One of BREAKDOWN_REASON_CODES")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    notes: Optional[str] = None


class SwapSamePartnerRequest(BaseModel):
    new_vehicle_id: int
    new_driver_id: int
    notes: Optional[str] = None


class HandoverRequest(BaseModel):
    new_partner_id: int
    acceptance_deadline_minutes: Optional[int] = Field(
        None, ge=1, le=240, description="Override default deadline (default 15)"
    )
    notes: Optional[str] = None


class BreakdownReasonsResponse(BaseModel):
    items: list[dict[str, str]]


class ActiveBreakdownItem(BaseModel):
    cab_booking_id: int
    booking_number: str
    master_booking_id: int
    cab_status: str
    breakdown_reason: Optional[str]
    breakdown_reported_at: Optional[str]
    breakdown_reported_by: Optional[str]
    breakdown_latitude: Optional[float]
    breakdown_longitude: Optional[float]
    pre_swap_actual_km: Optional[float]
    swap_count: int
    is_breakdown_swap: bool
    pickup_location: Optional[str]
    drop_location: Optional[str]
    estimated_amount: Optional[float]
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    city_id: Optional[int]
    city_name: Optional[str]
    # Original (broken-down) partner — for display + admin reconciliation.
    original_partner_id: Optional[int]
    original_partner_name: Optional[str]
    # Active (current) partner — for the "you handed off to…" banner.
    active_partner_id: Optional[int]
    active_partner_name: Optional[str]
    active_driver_id: Optional[int]
    active_driver_name: Optional[str]
    active_driver_mobile: Optional[str]
    active_vehicle_id: Optional[int]
    active_vehicle_number: Optional[str]
    acceptance_deadline: Optional[str]
    last_swap_at: Optional[str]


class ActiveBreakdownsResponse(BaseModel):
    items: list[ActiveBreakdownItem]
    total: int


class AvailableVehicleItem(BaseModel):
    vehicle_id: int
    registration_number: str
    make: Optional[str]
    model: Optional[str]
    color: Optional[str]
    vehicle_category_id: Optional[int]
    vehicle_category_name: Optional[str]
    partner_id: int
    partner_name: str
    driver_id: Optional[int] = None
    driver_name: Optional[str] = None
    driver_mobile: Optional[str] = None
    seats: Optional[int] = None


class AvailableVehiclesResponse(BaseModel):
    same_partner: list[AvailableVehicleItem]
    other_partners: list[AvailableVehicleItem]


class AvailablePartnerItem(BaseModel):
    partner_id: int
    business_name: str
    partner_code: Optional[str]
    mobile: Optional[str]
    free_vehicle_count: int
    city_id: Optional[int]
    city_name: Optional[str]


class AvailablePartnersResponse(BaseModel):
    items: list[AvailablePartnerItem]


# ════════════════════════════════════════════════════════════════
# ENUM-LIKE ENDPOINTS
# ════════════════════════════════════════════════════════════════


@router.get(
    "/reasons",
    response_model=BreakdownReasonsResponse,
    summary="Reasons a driver/partner/admin may report for a vehicle breakdown",
)
async def list_breakdown_reasons(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
) -> BreakdownReasonsResponse:
    """Returned to the breakdown-report modal so the UI can render a
    dropdown without hard-coding the codes. Same catalogue is used by the
    driver app and the partner portal — see app.modules.partner.constants.
    """
    return BreakdownReasonsResponse(items=BREAKDOWN_REASONS)


# ════════════════════════════════════════════════════════════════
# READ — list of currently-broken-down bookings
# ════════════════════════════════════════════════════════════════


@router.get(
    "/active",
    response_model=ActiveBreakdownsResponse,
    summary="Cab bookings currently in BREAKDOWN_REPORTED or AWAITING_SWAP",
)
async def list_active_breakdowns(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    city_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> ActiveBreakdownsResponse:
    params: dict[str, Any] = {"limit": limit}
    where = ["cb.booking_status IN ('BREAKDOWN_REPORTED', 'AWAITING_SWAP')"]
    if city_id is not None:
        where.append("mb.city_id = :city_id")
        params["city_id"] = city_id
    where_clause = " AND ".join(where)

    sql = text(
        f"""
        SELECT
            cb.id                       AS cab_booking_id,
            cb.booking_number,
            cb.master_booking_id,
            cb.booking_status           AS cab_status,
            cb.breakdown_reason,
            cb.breakdown_reported_at,
            cb.breakdown_reported_by,
            cb.breakdown_latitude,
            cb.breakdown_longitude,
            cb.pre_swap_actual_km,
            cb.swap_count,
            cb.is_breakdown_swap,
            cb.pickup_location,
            cb.drop_location,
            cb.estimated_amount,
            cb.original_partner_id,
            cb.last_swap_at,
            cb.acceptance_deadline,
            TRIM(CONCAT(c.first_name, ' ', COALESCE(c.last_name, ''))) AS customer_name,
            u.mobile_number              AS customer_mobile,
            mb.city_id,
            ci.name                      AS city_name,
            orig_p.business_name         AS original_partner_name,
            a.partner_id                 AS active_partner_id,
            act_p.business_name          AS active_partner_name,
            a.driver_id                  AS active_driver_id,
            d.full_name                  AS active_driver_name,
            d.mobile                     AS active_driver_mobile,
            a.vehicle_id                 AS active_vehicle_id,
            v.registration_number        AS active_vehicle_number
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        JOIN customers       c  ON c.id  = mb.customer_id
        JOIN users           u  ON u.id  = c.user_id
        LEFT JOIN cities     ci ON ci.id = mb.city_id
        LEFT JOIN partners   orig_p ON orig_p.id = cb.original_partner_id
        LEFT JOIN LATERAL (
            SELECT a.partner_id, a.driver_id, a.vehicle_id
            FROM cab_booking_assignments a
            WHERE a.cab_booking_id = cb.id AND a.closed_at IS NULL
            ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
            LIMIT 1
        ) a ON TRUE
        LEFT JOIN partners  act_p ON act_p.id = a.partner_id
        LEFT JOIN drivers   d     ON d.id    = a.driver_id
        LEFT JOIN vehicles  v     ON v.id    = a.vehicle_id
        WHERE {where_clause}
        ORDER BY cb.breakdown_reported_at DESC NULLS LAST, cb.id DESC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, params)).mappings().all()
    items = [ActiveBreakdownItem(**dict(r)) for r in rows]
    return ActiveBreakdownsResponse(items=items, total=len(items))


# ════════════════════════════════════════════════════════════════
# READ — available vehicles to swap into
# ════════════════════════════════════════════════════════════════


@router.get(
    "/{cab_booking_id}/available-vehicles",
    response_model=AvailableVehiclesResponse,
    summary="Vehicles eligible to swap into a broken-down cab (same + other partners)",
)
async def list_available_vehicles_for_swap(
    cab_booking_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> AvailableVehiclesResponse:
    """Powers the swap modal. Returns:
    - same_partner: ACTIVE vehicles + ACTIVE drivers belonging to the same
      partner as the broken-down booking, same vehicle_category_id.
    - other_partners: same, for any APPROVED/ACTIVE partner in the same city.

    Excludes vehicles already on a non-finished trip (Vehicle.status not in
    ACTIVE) and drivers not in APPROVED/ACTIVE.
    """
    cab_row = (
        (
            await db.execute(
                text(
                    """
                SELECT cb.id, cb.vehicle_category_id, cb.master_booking_id,
                       a.partner_id, mb.city_id
                FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                LEFT JOIN LATERAL (
                    SELECT a.partner_id
                    FROM cab_booking_assignments a
                    WHERE a.cab_booking_id = cb.id AND a.closed_at IS NULL
                    ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
                    LIMIT 1
                ) a ON TRUE
                WHERE cb.id = :id
                """
                ),
                {"id": cab_booking_id},
            )
        )
        .mappings()
        .first()
    )
    if cab_row is None:
        from app.core.exceptions import ResourceNotFoundException

        raise ResourceNotFoundException("CabBooking", cab_booking_id)

    cat_id = cab_row["vehicle_category_id"]
    partner_id = cab_row["partner_id"]
    city_id = cab_row["city_id"]

    base_filter = [
        "v.status = 'ACTIVE'",
        "v.partner_id = p.id",
    ]
    if cat_id is not None:
        base_filter.append("v.vehicle_category_id = :cat_id")

    # Same-partner vehicles (with their default driver if one exists).
    same_sql = text(
        f"""
        SELECT v.id AS vehicle_id, v.registration_number, v.make, v.model, v.color,
               v.vehicle_category_id, vc.category_name AS vehicle_category_name,
               v.partner_id, p.business_name AS partner_name, v.seats,
               da.driver_id, da.driver_name, da.driver_mobile
        FROM vehicles v
        JOIN partners p ON p.id = v.partner_id
        LEFT JOIN vehicle_categories vc ON vc.id = v.vehicle_category_id
        LEFT JOIN LATERAL (
            SELECT d.id AS driver_id, d.full_name AS driver_name, d.mobile AS driver_mobile
            FROM drivers d
            LEFT JOIN driver_availability dav ON dav.driver_id = d.id
            WHERE d.partner_id = v.partner_id
              AND d.status IN ('APPROVED','ACTIVE')
              AND COALESCE(dav.availability_status, 'OFFLINE') = 'ONLINE'
            ORDER BY d.id ASC
            LIMIT 1
        ) da ON TRUE
        WHERE v.partner_id = :pid AND {" AND ".join(base_filter)}
        ORDER BY v.registration_number ASC
        """
    )
    same_rows = (
        await db.execute(
            same_sql,
            {"pid": partner_id, "cat_id": cat_id},
        )
        .mappings()
        .all()
    )

    # Other-partner vehicles — same city.
    other_sql = text(
        f"""
        SELECT v.id AS vehicle_id, v.registration_number, v.make, v.model, v.color,
               v.vehicle_category_id, vc.category_name AS vehicle_category_name,
               v.partner_id, p.business_name AS partner_name, v.seats,
               da.driver_id, da.driver_name, da.driver_mobile
        FROM vehicles v
        JOIN partners p ON p.id = v.partner_id
        LEFT JOIN vehicle_categories vc ON vc.id = v.vehicle_category_id
        LEFT JOIN LATERAL (
            SELECT d.id AS driver_id, d.full_name AS driver_name, d.mobile AS driver_mobile
            FROM drivers d
            LEFT JOIN driver_availability dav ON dav.driver_id = d.id
            WHERE d.partner_id = v.partner_id
              AND d.status IN ('APPROVED','ACTIVE')
              AND COALESCE(dav.availability_status, 'OFFLINE') = 'ONLINE'
            ORDER BY d.id ASC
            LIMIT 1
        ) da ON TRUE
        WHERE p.partner_status IN ('APPROVED','ACTIVE')
          AND p.id <> :pid
          AND p.city_id = :city_id
          AND {" AND ".join(base_filter)}
        ORDER BY p.business_name ASC, v.registration_number ASC
        """
    )
    other_rows = (
        await db.execute(
            other_sql,
            {"pid": partner_id, "city_id": city_id, "cat_id": cat_id},
        )
        .mappings()
        .all()
    )

    return AvailableVehiclesResponse(
        same_partner=[AvailableVehicleItem(**dict(r)) for r in same_rows],
        other_partners=[AvailableVehicleItem(**dict(r)) for r in other_rows],
    )


# ════════════════════════════════════════════════════════════════
# READ — available partners for handover
# ════════════════════════════════════════════════════════════════


@router.get(
    "/{cab_booking_id}/available-partners",
    response_model=AvailablePartnersResponse,
    summary="Partners eligible to take over a broken-down booking via handover",
)
async def list_available_partners_for_handover(
    cab_booking_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> AvailablePartnersResponse:
    """All APPROVED/ACTIVE partners in the same city (excluding the current
    one) with at least one free vehicle of the required category. Powers
    the handover modal in Cab-Ops.
    """
    cab_row = (
        (
            await db.execute(
                text(
                    """
                SELECT cb.id, cb.vehicle_category_id, cb.master_booking_id,
                       a.partner_id, mb.city_id
                FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                LEFT JOIN LATERAL (
                    SELECT a.partner_id
                    FROM cab_booking_assignments a
                    WHERE a.cab_booking_id = cb.id AND a.closed_at IS NULL
                    ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
                    LIMIT 1
                ) a ON TRUE
                WHERE cb.id = :id
                """
                ),
                {"id": cab_booking_id},
            )
        )
        .mappings()
        .first()
    )
    if cab_row is None:
        from app.core.exceptions import ResourceNotFoundException

        raise ResourceNotFoundException("CabBooking", cab_booking_id)

    cat_id = cab_row["vehicle_category_id"]
    current_pid = cab_row["partner_id"]
    city_id = cab_row["city_id"]

    cat_filter = "AND v.vehicle_category_id = :cat_id" if cat_id is not None else ""
    sql = text(
        f"""
        SELECT
            p.id AS partner_id,
            p.business_name,
            p.partner_code,
            p.mobile,
            p.city_id,
            ci.name AS city_name,
            COUNT(v.id) AS free_vehicle_count
        FROM partners p
        LEFT JOIN cities ci ON ci.id = p.city_id
        LEFT JOIN vehicles v
               ON v.partner_id = p.id
              AND v.status = 'ACTIVE'
              {cat_filter}
        WHERE p.partner_status IN ('APPROVED','ACTIVE')
          AND p.id <> :pid
          AND p.city_id = :city_id
        GROUP BY p.id, p.business_name, p.partner_code, p.mobile, p.city_id, ci.name
        HAVING COUNT(v.id) > 0
        ORDER BY p.business_name ASC
        """
    )
    rows = (
        await db.execute(
            sql,
            {"pid": current_pid, "city_id": city_id, "cat_id": cat_id},
        )
        .mappings()
        .all()
    )
    return AvailablePartnersResponse(
        items=[AvailablePartnerItem(**dict(r)) for r in rows]
    )


# ════════════════════════════════════════════════════════════════
# WRITE — admin manually logs a breakdown
# ════════════════════════════════════════════════════════════════


@router.post(
    "/{cab_booking_id}/report",
    summary="Admin logs a breakdown on behalf of driver/partner",
)
async def admin_report_breakdown(
    cab_booking_id: int,
    payload: ReportBreakdownRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Use case: driver can't reach the app, partner calls the helpline,
    ops logs the breakdown and starts the swap/handover flow."""
    from decimal import Decimal

    actor_uuid = current_user.get("sub")
    snapshot = await breakdown_service.report_breakdown(
        db,
        cab_booking_id=cab_booking_id,
        reported_by=breakdown_service.REPORTER_ADMIN,
        reported_by_user_id=UUID(actor_uuid) if actor_uuid else None,
        reporter_partner_id=None,
        reporter_driver_id=None,
        reason_code=payload.reason_code,
        latitude=(
            Decimal(str(payload.latitude)) if payload.latitude is not None else None
        ),
        longitude=(
            Decimal(str(payload.longitude)) if payload.longitude is not None else None
        ),
        notes=payload.notes,
    )
    return success_response(
        "Breakdown reported by admin",
        snapshot,
    )


# ════════════════════════════════════════════════════════════════
# WRITE — same-partner swap
# ════════════════════════════════════════════════════════════════


@router.post(
    "/{cab_booking_id}/swap-same-partner",
    summary="Swap to another vehicle/driver of the SAME partner",
)
async def admin_swap_same_partner(
    cab_booking_id: int,
    payload: SwapSamePartnerRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Admin-driven same-partner swap. The same operation is exposed to
    the partner portal at /partners/me/bookings/{n}/swap-vehicle — partners
    can self-serve when PARTNER_SELF_SERVE_SWAP_ENABLED is true.
    """
    actor_uuid = current_user.get("sub")
    snapshot = await breakdown_service.swap_vehicle_same_partner(
        db,
        cab_booking_id=cab_booking_id,
        new_vehicle_id=payload.new_vehicle_id,
        new_driver_id=payload.new_driver_id,
        performed_by=breakdown_service.REPORTER_ADMIN,
        performed_by_user_id=UUID(actor_uuid) if actor_uuid else None,
        performed_by_partner_id=None,
    )
    return success_response(
        "Vehicle swapped within same partner",
        snapshot,
    )


# ════════════════════════════════════════════════════════════════
# WRITE — handover to a different partner
# ════════════════════════════════════════════════════════════════


@router.post(
    "/{cab_booking_id}/handover",
    summary="Hand a broken-down booking over to a DIFFERENT partner",
)
async def admin_handover_to_new_partner(
    cab_booking_id: int,
    payload: HandoverRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Closes the broken-down partner's assignment (zero payout) and opens
    a new one for `new_partner_id`. The new partner must accept via the
    standard partner acceptance flow (deadline configurable, default 15
    min). Original cash stays with the broken-down driver — see
    breakdown.handover_to_new_partner for the custody reset.
    """
    actor_uuid = current_user.get("sub")
    snapshot = await breakdown_service.handover_to_new_partner(
        db,
        cab_booking_id=cab_booking_id,
        new_partner_id=payload.new_partner_id,
        performed_by_user_id=UUID(actor_uuid) if actor_uuid else None,
        acceptance_deadline_minutes=payload.acceptance_deadline_minutes,
    )
    return success_response(
        "Booking handed over to new partner",
        snapshot,
    )


__all__ = ["router"]
