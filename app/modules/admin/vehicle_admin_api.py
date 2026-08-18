# ============================================================
# WAYTERO — ADMIN VEHICLE API
# File: app/modules/admin/vehicle_admin_api.py
# Prefix: /admin/vehicles  (registered in app/api/router.py)
# Doc Ref:
#   BRD Part 3 §133-138 — Vehicle Maintenance Records
#   Docs/07_Frontend_Architecture/02_REACT_ADMIN_PORTAL.md §19 — Vehicle Management Module
#
# Reads return the bare payload the admin portal expects; writes use
# success_response envelope. Nothing here commits — get_db owns the
# transaction.
# ============================================================

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.vehicle.models import Vehicle, VehicleMaintenance
from app.shared.responses.base import success_response

router = APIRouter()

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")


# ════════════════════════════════════════════════════════════════
# SCHEMAS
# ════════════════════════════════════════════════════════════════


class MaintenanceCreate(BaseModel):
    maintenance_type: str = Field(..., min_length=2, max_length=100)
    service_date: date
    next_due_date: Optional[date] = None
    cost: Optional[Decimal] = None
    remarks: Optional[str] = None


class MaintenanceUpdate(BaseModel):
    maintenance_type: Optional[str] = Field(None, min_length=2, max_length=100)
    service_date: Optional[date] = None
    next_due_date: Optional[date] = None
    cost: Optional[Decimal] = None
    remarks: Optional[str] = None


class MaintenanceOut(BaseModel):
    id: int
    vehicle_id: int
    maintenance_type: Optional[str]
    service_date: Optional[str]
    next_due_date: Optional[str]
    cost: Optional[float]
    remarks: Optional[str]
    days_until_due: Optional[int]
    is_overdue: bool
    created_at: str


class MaintenanceListResponse(BaseModel):
    items: list[MaintenanceOut]
    total: int


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════


def _row_to_out(row: dict[str, Any], today: date) -> MaintenanceOut:
    ndd = row.get("next_due_date")
    days = (ndd - today).days if isinstance(ndd, date) else None
    sd = row.get("service_date")
    return MaintenanceOut(
        id=int(row["id"]),
        vehicle_id=int(row["vehicle_id"]),
        maintenance_type=row.get("maintenance_type"),
        service_date=(sd.isoformat() if sd else None),
        next_due_date=ndd.isoformat() if isinstance(ndd, date) else None,
        cost=float(row["cost"]) if row.get("cost") is not None else None,
        remarks=row.get("remarks"),
        days_until_due=days,
        is_overdue=bool(days is not None and days < 0),
        created_at=row["created_at"].isoformat() if row.get("created_at") else "",
    )


async def _vehicle_or_404(db: AsyncSession, vehicle_id: int) -> Vehicle:
    veh = (
        await db.execute(
            select(Vehicle).where(
                Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if veh is None:
        raise ResourceNotFoundException("Vehicle", vehicle_id)
    return veh


# ════════════════════════════════════════════════════════════════
# LIST — per vehicle
# ════════════════════════════════════════════════════════════════


@router.get(
    "/{vehicle_id}/maintenance",
    response_model=MaintenanceListResponse,
    summary="Service log for one vehicle",
)
async def list_vehicle_maintenance(
    vehicle_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> MaintenanceListResponse:
    await _vehicle_or_404(db, vehicle_id)
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT id, vehicle_id, maintenance_type, service_date,
                       next_due_date, cost, remarks, created_at
                FROM vehicle_maintenance
                WHERE vehicle_id = :vid
                ORDER BY service_date DESC NULLS LAST, id DESC
                LIMIT :limit
                """
                ),
                {"vid": vehicle_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    items = [_row_to_out(dict(r), date.today()) for r in rows]
    total = (
        await db.execute(
            text("SELECT COUNT(*) FROM vehicle_maintenance WHERE vehicle_id = :vid"),
            {"vid": vehicle_id},
        )
    ).scalar() or 0
    return MaintenanceListResponse(items=items, total=int(total))


# ════════════════════════════════════════════════════════════════
# CREATE
# ════════════════════════════════════════════════════════════════


@router.post(
    "/{vehicle_id}/maintenance",
    summary="Log a new service / repair entry",
)
async def create_vehicle_maintenance(
    vehicle_id: int,
    payload: MaintenanceCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _vehicle_or_404(db, vehicle_id)
    if payload.next_due_date and payload.next_due_date < payload.service_date:
        raise ValidationException(
            "next_due_date cannot be earlier than service_date.",
            details={"field": "next_due_date"},
        )
    if payload.cost is not None and payload.cost < 0:
        raise ValidationException(
            "cost cannot be negative.",
            details={"field": "cost"},
        )

    record = VehicleMaintenance(
        vehicle_id=vehicle_id,
        maintenance_type=payload.maintenance_type,
        service_date=payload.service_date,
        next_due_date=payload.next_due_date,
        cost=payload.cost,
        remarks=payload.remarks,
    )
    db.add(record)
    await db.flush()
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, vehicle_id, maintenance_type, service_date,
                       next_due_date, cost, remarks, created_at
                FROM vehicle_maintenance WHERE id = :id
                """
                ),
                {"id": record.id},
            )
        )
        .mappings()
        .one()
    )
    return success_response(
        "Maintenance entry recorded", _row_to_out(dict(row), date.today()).model_dump()
    )


# ════════════════════════════════════════════════════════════════
# UPDATE
# ════════════════════════════════════════════════════════════════


@router.patch(
    "/{vehicle_id}/maintenance/{maintenance_id}",
    summary="Edit an existing maintenance entry",
)
async def update_vehicle_maintenance(
    vehicle_id: int,
    maintenance_id: int,
    payload: MaintenanceUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _vehicle_or_404(db, vehicle_id)
    record = (
        await db.execute(
            select(VehicleMaintenance).where(
                VehicleMaintenance.id == maintenance_id,
                VehicleMaintenance.vehicle_id == vehicle_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise ResourceNotFoundException("Maintenance entry", maintenance_id)

    if payload.maintenance_type is not None:
        record.maintenance_type = payload.maintenance_type
    if payload.service_date is not None:
        record.service_date = payload.service_date
    if payload.next_due_date is not None:
        record.next_due_date = payload.next_due_date
    if payload.cost is not None:
        if payload.cost < 0:
            raise ValidationException(
                "cost cannot be negative.", details={"field": "cost"}
            )
        record.cost = payload.cost
    if payload.remarks is not None:
        record.remarks = payload.remarks

    if (
        record.next_due_date
        and record.service_date
        and record.next_due_date < record.service_date
    ):
        raise ValidationException(
            "next_due_date cannot be earlier than service_date.",
            details={"field": "next_due_date"},
        )

    await db.flush()
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT id, vehicle_id, maintenance_type, service_date,
                       next_due_date, cost, remarks, created_at
                FROM vehicle_maintenance WHERE id = :id
                """
                ),
                {"id": maintenance_id},
            )
        )
        .mappings()
        .one()
    )
    return success_response(
        "Maintenance entry updated", _row_to_out(dict(row), date.today()).model_dump()
    )


# ════════════════════════════════════════════════════════════════
# DELETE
# ════════════════════════════════════════════════════════════════


@router.delete(
    "/{vehicle_id}/maintenance/{maintenance_id}",
    summary="Remove a maintenance entry",
)
async def delete_vehicle_maintenance(
    vehicle_id: int,
    maintenance_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    record = (
        await db.execute(
            select(VehicleMaintenance).where(
                VehicleMaintenance.id == maintenance_id,
                VehicleMaintenance.vehicle_id == vehicle_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise ResourceNotFoundException("Maintenance entry", maintenance_id)
    await db.delete(record)
    await db.flush()
    return success_response("Maintenance entry removed")


# ════════════════════════════════════════════════════════════════
# FLEET-WIDE LIST — service desk overview
# ════════════════════════════════════════════════════════════════


@router.get(
    "/maintenance/due",
    summary="Every vehicle whose next service is due in ≤ N days (or overdue)",
)
async def list_due_maintenance(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=0, le=365),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """One row per (vehicle, latest open due-date) for the service-desk
    dashboard. Cap on days lets ops dial between "tomorrow" and "next quarter"
    without the response growing unbounded.
    """
    sql = text(
        """
        WITH latest_due AS (
            SELECT DISTINCT ON (vehicle_id)
                vehicle_id,
                next_due_date,
                maintenance_type,
                service_date,
                cost,
                remarks
            FROM vehicle_maintenance
            WHERE next_due_date IS NOT NULL
            ORDER BY vehicle_id, next_due_date ASC
        )
        SELECT
            ld.vehicle_id,
            v.registration_number,
            v.vehicle_code,
            p.business_name      AS partner_name,
            p.partner_code       AS partner_code,
            vc.category_name     AS category_name,
            ld.maintenance_type,
            ld.service_date,
            ld.next_due_date,
            ld.cost,
            ld.remarks,
            (ld.next_due_date - CURRENT_DATE) AS days_until_due
        FROM latest_due ld
        JOIN vehicles v            ON v.id = ld.vehicle_id
        JOIN vehicle_categories vc ON vc.id = v.vehicle_category_id
        JOIN partners p            ON p.id = v.partner_id
        WHERE v.deleted_at IS NULL
          AND ld.next_due_date <= CURRENT_DATE + (:days || ' days')::interval
        ORDER BY ld.next_due_date ASC
        LIMIT :limit
        """
    )
    rows = (await db.execute(sql, {"days": str(days), "limit": limit})).mappings().all()
    today = date.today()
    items = []
    for r in rows:
        ndd = r["next_due_date"]
        days_until = (ndd - today).days if ndd else None
        items.append(
            {
                "vehicle_id": int(r["vehicle_id"]),
                "registration_number": r["registration_number"],
                "vehicle_code": r["vehicle_code"],
                "partner_name": r["partner_name"],
                "partner_code": r["partner_code"],
                "category_name": r["category_name"],
                "maintenance_type": r["maintenance_type"],
                "service_date": (
                    r["service_date"].isoformat() if r["service_date"] else None
                ),
                "next_due_date": ndd.isoformat() if ndd else None,
                "cost": float(r["cost"]) if r["cost"] is not None else None,
                "remarks": r["remarks"],
                "days_until_due": days_until,
                "is_overdue": bool(days_until is not None and days_until < 0),
            }
        )
    return {"items": items, "total": len(items), "days_window": days}


__all__ = ["router"]
