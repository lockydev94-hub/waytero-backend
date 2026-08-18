# ============================================================
# WAYTERO — ADMIN CAB OPERATIONS API
# File: app/modules/admin/cab_ops_api.py
# Prefix: /admin/cab-ops  (registered in app/api/router.py)
# Doc Ref:
#   BRD Part 3 §40 Auto Assignment Engine — what the operations console shows
#   BRD Part 3 §44 Live Trip Tracking    — what in-trip cards display
#   BRD Part 7 §155                       — realtime + push channel
#
# Three read endpoints feed the /cab-ops page:
#   GET /admin/cab-ops/queue          — bookings waiting on a partner
#   GET /admin/cab-ops/in-trip        — DRIVER_ASSIGNED + STARTED
#   GET /admin/cab-ops/sos-alerts     — UNRESOLVED trip-assistance flags
#
# One write endpoint lets CCO/ADMIN bulk-reassign a stuck booking back
# into PENDING_ASSIGNMENT — the case where a partner stopped responding
# without timing out and the admin wants to push the booking to the next
# eligible partner without going through the per-booking detail page.
#
# Reads return the bare payload the admin-portal expects (envelope shape
# is added by the caller); writes use success_response / error envelope.
# Nothing here commits — get_db owns the transaction.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import BusinessException, ResourceNotFoundException
from app.shared.responses.base import success_response

router = APIRouter()

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN", "CCO")
ADMIN_ONLY = ("SUPER_ADMIN", "ADMIN")


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class CabOpsQueueItem(BaseModel):
    cab_booking_id: int
    booking_number: str
    master_booking_id: int
    pickup_location: Optional[str]
    drop_location: Optional[str]
    pickup_datetime: Optional[str]
    trip_type: Optional[str]
    estimated_amount: Optional[float]
    cab_status: str
    pending_partner_id: Optional[int]
    pending_partner_name: Optional[str]
    pending_partner_code: Optional[str]
    acceptance_deadline: Optional[str]
    seconds_to_deadline: Optional[int]
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    city_id: Optional[int]
    city_name: Optional[str]
    vehicle_category_id: Optional[int]
    vehicle_category_label: Optional[str]
    created_at: str


class CabOpsInTripItem(BaseModel):
    cab_booking_id: int
    booking_number: str
    master_booking_id: int
    cab_status: str
    pickup_location: Optional[str]
    drop_location: Optional[str]
    pickup_latitude: Optional[float]
    pickup_longitude: Optional[float]
    trip_started_at: Optional[str]
    trip_ended_at: Optional[str]
    payment_mode: Optional[str]
    payment_collected_by: Optional[str]
    final_amount: Optional[float]
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    driver_id: Optional[int]
    driver_name: Optional[str]
    driver_mobile: Optional[str]
    vehicle_id: Optional[int]
    vehicle_number: Optional[str]
    partner_id: Optional[int]
    partner_name: Optional[str]
    city_id: Optional[int]
    city_name: Optional[str]


class CabOpsSosAlert(BaseModel):
    cab_booking_id: int
    booking_number: str
    master_booking_id: int
    cab_status: str
    raised_at: str
    flag: str
    note: Optional[str]
    customer_name: Optional[str]
    customer_mobile: Optional[str]
    driver_id: Optional[int]
    driver_name: Optional[str]
    driver_mobile: Optional[str] = None
    partner_id: Optional[int]
    partner_name: Optional[str]


class CabOpsQueueResponse(BaseModel):
    items: list[CabOpsQueueItem]
    total: int


class CabOpsInTripResponse(BaseModel):
    items: list[CabOpsInTripItem]
    total: int


class CabOpsSosResponse(BaseModel):
    items: list[CabOpsSosAlert]
    total: int


class ReassignCabRequest(BaseModel):
    remarks: Optional[str] = None


# ════════════════════════════════════════════════════════════════
# QUEUE  — bookings waiting on a partner to accept
# ════════════════════════════════════════════════════════════════


@router.get(
    "/queue",
    response_model=CabOpsQueueResponse,
    summary="Bookings waiting on a partner",
)
async def list_cab_ops_queue(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    city_id: Optional[int] = Query(None),
    partner_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> CabOpsQueueResponse:
    params: dict[str, Any] = {"limit": limit}
    filters = [
        "cb.booking_status IN ('PENDING_PARTNER_ACCEPTANCE')",
    ]
    if city_id is not None:
        filters.append("mb.city_id = :city_id")
        params["city_id"] = city_id
    if partner_id is not None:
        filters.append("cb.pending_partner_id = :partner_id")
        params["partner_id"] = partner_id

    where_clause = " AND ".join(filters)
    sql = text(
        f"""
        SELECT
            cb.id              AS cab_booking_id,
            cb.booking_number  AS booking_number,
            cb.master_booking_id,
            cb.pickup_location,
            cb.drop_location,
            cb.pickup_datetime,
            cb.trip_type,
            cb.estimated_amount,
            cb.booking_status  AS cab_status,
            cb.pending_partner_id,
            cb.acceptance_deadline,
            p.business_name    AS pending_partner_name,
            p.partner_code     AS pending_partner_code,
            TRIM(CONCAT(c.first_name, ' ', COALESCE(c.last_name, ''))) AS customer_name,
            u.mobile_number    AS customer_mobile,
            mb.city_id,
            ci.name            AS city_name,
            cb.vehicle_category_id,
            vc.category_name   AS vehicle_category_label,
            cb.created_at
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        JOIN customers       c  ON c.id  = mb.customer_id
        JOIN users           u  ON u.id  = c.user_id
        LEFT JOIN cities     ci ON ci.id = mb.city_id
        LEFT JOIN partners   p  ON p.id  = cb.pending_partner_id
        LEFT JOIN vehicle_categories vc ON vc.id = cb.vehicle_category_id
        WHERE {where_clause}
        ORDER BY cb.acceptance_deadline ASC NULLS LAST, cb.created_at ASC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, params)).mappings().all()

    now = datetime.now(timezone.utc)
    items: list[CabOpsQueueItem] = []
    for r in rows:
        deadline = r["acceptance_deadline"]
        seconds = None
        if deadline is not None:
            dlu = deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)
            seconds = int((dlu - now).total_seconds())
        items.append(
            CabOpsQueueItem(
                cab_booking_id=int(r["cab_booking_id"]),
                booking_number=str(r["booking_number"]),
                master_booking_id=int(r["master_booking_id"]),
                pickup_location=r["pickup_location"],
                drop_location=r["drop_location"],
                pickup_datetime=(
                    r["pickup_datetime"].isoformat() if r["pickup_datetime"] else None
                ),
                trip_type=r["trip_type"],
                estimated_amount=(
                    float(r["estimated_amount"])
                    if r["estimated_amount"] is not None
                    else None
                ),
                cab_status=str(r["cab_status"]),
                pending_partner_id=(
                    int(r["pending_partner_id"])
                    if r["pending_partner_id"] is not None
                    else None
                ),
                pending_partner_name=r["pending_partner_name"],
                pending_partner_code=r["pending_partner_code"],
                acceptance_deadline=deadline.isoformat() if deadline else None,
                seconds_to_deadline=seconds,
                customer_name=r["customer_name"],
                customer_mobile=r["customer_mobile"],
                city_id=int(r["city_id"]) if r["city_id"] is not None else None,
                city_name=r["city_name"],
                vehicle_category_id=(
                    int(r["vehicle_category_id"])
                    if r["vehicle_category_id"] is not None
                    else None
                ),
                vehicle_category_label=r["vehicle_category_label"],
                created_at=r["created_at"].isoformat() if r["created_at"] else "",
            )
        )

    total_row = (
        (
            await db.execute(
                text(
                    f"""
                SELECT COUNT(*) FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                WHERE {where_clause}
                """
                ),
                params,
            )
        ).scalar()
        or 0
    )
    return CabOpsQueueResponse(items=items, total=int(total_row))


# ════════════════════════════════════════════════════════════════
# IN-TRIP — DRIVER_ASSIGNED + STARTED cab bookings
# ════════════════════════════════════════════════════════════════


@router.get(
    "/in-trip", response_model=CabOpsInTripResponse, summary="Active trips on the road"
)
async def list_cab_ops_in_trip(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    city_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
) -> CabOpsInTripResponse:
    params: dict[str, Any] = {"limit": limit}
    statuses = ["DRIVER_ASSIGNED", "STARTED"]
    if status_filter and status_filter in statuses:
        statuses = [status_filter]
    placeholders = ",".join(f":st_{i}" for i, _ in enumerate(statuses))
    for i, st in enumerate(statuses):
        params[f"st_{i}"] = st
    filters = [f"cb.booking_status IN ({placeholders})"]
    if city_id is not None:
        filters.append("mb.city_id = :city_id")
        params["city_id"] = city_id
    where_clause = " AND ".join(filters)

    sql = text(
        f"""
        SELECT
            cb.id              AS cab_booking_id,
            cb.booking_number  AS booking_number,
            cb.master_booking_id,
            cb.booking_status  AS cab_status,
            cb.pickup_location,
            cb.drop_location,
            cb.pickup_latitude,
            cb.pickup_longitude,
            cb.trip_started_at,
            cb.trip_ended_at,
            cb.payment_mode,
            cb.payment_collected_by,
            cb.final_amount,
            TRIM(CONCAT(c.first_name, ' ', COALESCE(c.last_name, ''))) AS customer_name,
            u.mobile_number    AS customer_mobile,
            a.driver_id,
            d.full_name        AS driver_name,
            d.mobile           AS driver_mobile,
            a.vehicle_id,
            v.registration_number AS vehicle_number,
            a.partner_id,
            p.business_name    AS partner_name,
            mb.city_id,
            ci.name            AS city_name
        FROM cab_bookings cb
        JOIN master_bookings mb ON mb.id = cb.master_booking_id
        JOIN customers       c  ON c.id  = mb.customer_id
        JOIN users           u  ON u.id  = c.user_id
        LEFT JOIN cities     ci ON ci.id = mb.city_id
        LEFT JOIN LATERAL (
            SELECT a.driver_id, a.vehicle_id, a.partner_id
            FROM cab_booking_assignments a
            WHERE a.cab_booking_id = cb.id
            ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
            LIMIT 1
        ) a ON TRUE
        LEFT JOIN drivers    d  ON d.id  = a.driver_id
        LEFT JOIN vehicles   v  ON v.id  = a.vehicle_id
        LEFT JOIN partners   p  ON p.id  = a.partner_id
        WHERE {where_clause}
        ORDER BY
            CASE cb.booking_status WHEN 'STARTED' THEN 0 ELSE 1 END,
            cb.trip_started_at DESC NULLS LAST,
            cb.created_at DESC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, params)).mappings().all()
    items = [
        CabOpsInTripItem(
            cab_booking_id=int(r["cab_booking_id"]),
            booking_number=str(r["booking_number"]),
            master_booking_id=int(r["master_booking_id"]),
            cab_status=str(r["cab_status"]),
            pickup_location=r["pickup_location"],
            drop_location=r["drop_location"],
            pickup_latitude=(
                float(r["pickup_latitude"])
                if r["pickup_latitude"] is not None
                else None
            ),
            pickup_longitude=(
                float(r["pickup_longitude"])
                if r["pickup_longitude"] is not None
                else None
            ),
            trip_started_at=(
                r["trip_started_at"].isoformat() if r["trip_started_at"] else None
            ),
            trip_ended_at=(
                r["trip_ended_at"].isoformat() if r["trip_ended_at"] else None
            ),
            payment_mode=r["payment_mode"],
            payment_collected_by=r["payment_collected_by"],
            final_amount=(
                float(r["final_amount"]) if r["final_amount"] is not None else None
            ),
            customer_name=r["customer_name"],
            customer_mobile=r["customer_mobile"],
            driver_id=int(r["driver_id"]) if r["driver_id"] is not None else None,
            driver_name=r["driver_name"],
            driver_mobile=r["driver_mobile"],
            vehicle_id=int(r["vehicle_id"]) if r["vehicle_id"] is not None else None,
            vehicle_number=r["vehicle_number"],
            partner_id=int(r["partner_id"]) if r["partner_id"] is not None else None,
            partner_name=r["partner_name"],
            city_id=int(r["city_id"]) if r["city_id"] is not None else None,
            city_name=r["city_name"],
        )
        for r in rows
    ]

    total_row = (
        (
            await db.execute(
                text(
                    f"""
                SELECT COUNT(*) FROM cab_bookings cb
                JOIN master_bookings mb ON mb.id = cb.master_booking_id
                WHERE {where_clause}
                """
                ),
                params,
            )
        ).scalar()
        or 0
    )
    return CabOpsInTripResponse(items=items, total=int(total_row))


# ════════════════════════════════════════════════════════════════
# SOS ALERTS — anything in booking_timeline flagged SOS / escalation
# ════════════════════════════════════════════════════════════════


@router.get(
    "/sos-alerts",
    response_model=CabOpsSosResponse,
    summary="Open SOS + escalation flags tied to active cab bookings",
)
async def list_cab_ops_sos_alerts(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> CabOpsSosResponse:
    """Surfaces the booking_timeline rows that the trip-assistance / driver
    apps write when a customer presses SOS or a driver reports an issue.

    The timeline is linked to master_bookings (no cab_booking_id column
    exists), so we join through master_bookings to the cab booking. A
    master booking may carry more than one cab booking in rare flows
    (multi-leg); we surface the most-recently-updated one.

    Filtered to entries on cab bookings whose status is still recoverable
    (DRIVER_ASSIGNED / STARTED / PENDING_PARTNER_ACCEPTANCE / ASSIGNED /
    BREAKDOWN_REPORTED / AWAITING_SWAP) so resolved/cancelled bookings
    don't pollute the console.
    """
    sql = text(
        """
        SELECT
            cb.id          AS cab_booking_id,
            cb.booking_number,
            cb.master_booking_id,
            cb.booking_status AS cab_status,
            bt.event_timestamp AS raised_at,
            bt.event_type  AS flag,
            bt.event_description AS note,
            TRIM(CONCAT(c.first_name, ' ', COALESCE(c.last_name, ''))) AS customer_name,
            u.mobile_number AS customer_mobile,
            a.driver_id,
            d.full_name       AS driver_name,
            d.mobile          AS driver_mobile,
            a.partner_id,
            p.business_name AS partner_name
        FROM booking_timelines bt
        JOIN master_bookings mb ON mb.id = bt.master_booking_id
        JOIN customers c ON c.id = mb.customer_id
        JOIN users u ON u.id = c.user_id
        JOIN cab_bookings cb
              ON cb.master_booking_id = mb.id
             AND cb.booking_status IN (
                 'PENDING_PARTNER_ACCEPTANCE', 'ASSIGNED',
                 'DRIVER_ASSIGNED', 'STARTED',
                 'BREAKDOWN_REPORTED', 'AWAITING_SWAP'
             )
        LEFT JOIN LATERAL (
            SELECT a.driver_id, a.partner_id
            FROM cab_booking_assignments a
            WHERE a.cab_booking_id = cb.id
            ORDER BY a.assigned_at DESC NULLS LAST, a.id DESC
            LIMIT 1
        ) a ON TRUE
        LEFT JOIN drivers  d ON d.id = a.driver_id
        LEFT JOIN partners p ON p.id = a.partner_id
        WHERE bt.event_type IN ('SOS_RAISED', 'ESCALATION_RAISED', 'BREAKDOWN_REPORTED')
        ORDER BY bt.event_timestamp DESC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, {"limit": limit})).mappings().all()
    items = [
        CabOpsSosAlert(
            cab_booking_id=int(r["cab_booking_id"]),
            booking_number=str(r["booking_number"]),
            master_booking_id=int(r["master_booking_id"]),
            cab_status=str(r["cab_status"]),
            raised_at=r["raised_at"].isoformat() if r["raised_at"] else "",
            flag=str(r["flag"]),
            note=r["note"],
            customer_name=r["customer_name"],
            customer_mobile=r["customer_mobile"],
            driver_id=int(r["driver_id"]) if r["driver_id"] is not None else None,
            driver_name=r["driver_name"],
            driver_mobile=r["driver_mobile"],
            partner_id=int(r["partner_id"]) if r["partner_id"] is not None else None,
            partner_name=r["partner_name"],
        )
        for r in rows
    ]
    return CabOpsSosResponse(items=items, total=len(items))


# ════════════════════════════════════════════════════════════════
# WRITE — bulk-reassign a stuck cab booking
# ════════════════════════════════════════════════════════════════


@router.post(
    "/queue/{cab_booking_id}/reassign",
    summary="Force a stuck PENDING_PARTNER_ACCEPTANCE back to PENDING_ASSIGNMENT",
)
async def reassign_cab_booking(
    cab_booking_id: int,
    payload: ReassignCabRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ONLY)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The timeout sweeper does this every minute automatically, but admins
    need a manual lever for the case where the partner stopped responding
    without timing out (tab closed, app crash) and the admin wants to push
    the booking to the next eligible partner now rather than wait.
    """
    cab = (
        (
            await db.execute(
                text(
                    """
                SELECT id, booking_number, booking_status, master_booking_id
                FROM cab_bookings
                WHERE id = :id
                """
                ),
                {"id": cab_booking_id},
            )
        )
        .mappings()
        .first()
    )
    if cab is None:
        raise ResourceNotFoundException("Cab booking", cab_booking_id)
    if cab["booking_status"] not in (
        "PENDING_PARTNER_ACCEPTANCE",
        "PENDING_ASSIGNMENT",
        "ASSIGNED",
    ):
        raise BusinessException(
            f"Cannot reassign cab booking in status {cab['booking_status']}.",
            code="CAB_INVALID_REASSIGN_STATE",
        )

    now = datetime.now(timezone.utc)
    actor_id = current_user.get("sub")

    await db.execute(
        text(
            """
            UPDATE cab_bookings
            SET booking_status = 'PENDING_ASSIGNMENT',
                pending_partner_id = NULL,
                acceptance_deadline = NULL,
                partner_responded_at = :now,
                updated_at = :now
            WHERE id = :id
            """
        ),
        {"id": cab_booking_id, "now": now},
    )

    await db.execute(
        text(
            """
            INSERT INTO booking_timelines (
                master_booking_id, event_type, event_description,
                event_timestamp, created_by
            ) VALUES (
                :mb_id, 'ADMIN_FORCE_REASSIGN', :note,
                :now, :actor_id
            )
            """
        ),
        {
            "mb_id": int(cab["master_booking_id"]),
            "note": (payload.remarks or "Manual reassign from cab-ops console"),
            "actor_id": actor_id,
            "now": now,
        },
    )

    await db.execute(
        text(
            """
            UPDATE cab_booking_assignments
            SET rejection_reason_code = COALESCE(rejection_reason_code, 'ADMIN_FORCE_REASSIGN'),
                rejection_remarks = COALESCE(rejection_remarks, :note)
            WHERE cab_booking_id = :id
            """
        ),
        {
            "id": cab_booking_id,
            "note": payload.remarks or "Admin force-reassign",
        },
    )

    return success_response(
        f"Cab booking {cab['booking_number']} returned to PENDING_ASSIGNMENT",
        {
            "cab_booking_id": cab_booking_id,
            "booking_number": cab["booking_number"],
            "new_status": "PENDING_ASSIGNMENT",
        },
    )


__all__ = ["router"]
