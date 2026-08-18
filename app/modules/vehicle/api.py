# ============================================================
# WAY TERO — VEHICLE API ROUTER
# File: app/modules/vehicle/api.py
# Doc Ref: DB Schema Part 3, Sections 10-20
# Phase: 2 — Vehicle Module
# ============================================================

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.vehicle.schemas import (
    VehicleRegister,
    VehicleUpdate,
    VehicleResponse,
    VehicleCategoryResponse,
    VehicleDocumentUpload,
    VehicleDocumentResponse,
    VehiclePhotoUpload,
    VehiclePhotoResponse,
    PricingRuleResponse,
)
from app.modules.vehicle.services import VehicleService

router = APIRouter()


def get_service(db: AsyncSession = Depends(get_db)) -> VehicleService:
    return VehicleService(db=db)


def _resolve_partner_id(
    current_user: dict, requested_partner_id: Optional[int] = None
) -> int:
    """Resolve the partner whose vehicle data a caller may touch.

    Doc Ref: Security Hardening — S6 vehicle IDOR closure.
    PARTNER callers are pinned to the partner_id embedded in their JWT at
    login; any partner_id they pass in the query string is ignored. Admin/CCO
    callers must pass an explicit partner_id (they operate across partners).
    """
    if current_user.get("user_type") == "PARTNER":
        token_partner_id = current_user.get("partner_id")
        if token_partner_id:
            return int(token_partner_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Partner context missing from token — please re-login.",
        )
    if requested_partner_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="partner_id is required for non-partner callers.",
        )
    return requested_partner_id


# ---- Vehicle Categories (public) ----


@router.get(
    "/categories",
    response_model=list[VehicleCategoryResponse],
    status_code=status.HTTP_200_OK,
    summary="List vehicle categories",
)
async def list_categories(service: VehicleService = Depends(get_service)):
    return await service.list_categories()


# ---- Register / List (Partner) ----


@router.post(
    "",
    response_model=VehicleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Partner] Register a vehicle",
)
async def register_vehicle(
    body: VehicleRegister,
    partner_id: Optional[int] = None,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: VehicleService = Depends(get_service),
):
    resolved_partner_id = _resolve_partner_id(current_user, partner_id)
    return await service.register_vehicle(resolved_partner_id, body)


@router.get(
    "/partner/{partner_id}",
    status_code=status.HTTP_200_OK,
    summary="[Partner/Admin] List vehicles by partner",
)
async def list_partner_vehicles(
    partner_id: int,
    page: int = 1,
    per_page: int = 20,
    current_user: dict = Depends(
        require_roles("PARTNER", "ADMIN", "SUPER_ADMIN", "CCO")
    ),
    service: VehicleService = Depends(get_service),
):
    resolved_partner_id = _resolve_partner_id(current_user, partner_id)
    return await service.list_by_partner(
        resolved_partner_id, page=page, per_page=per_page
    )


@router.get(
    "/active",
    status_code=status.HTTP_200_OK,
    summary="List active vehicles (for booking engine)",
)
async def list_active_vehicles(
    city_id: Optional[int] = None,
    category_id: Optional[int] = None,
    page: int = 1,
    per_page: int = 20,
    current_user: dict = Depends(get_current_user),
    service: VehicleService = Depends(get_service),
):
    return await service.list_active(
        city_id=city_id, category_id=category_id, page=page, per_page=per_page
    )


# ---- Single vehicle ----


@router.get(
    "/{vehicle_uuid}",
    response_model=VehicleResponse,
    status_code=status.HTTP_200_OK,
    summary="Get vehicle by UUID",
)
async def get_vehicle(
    vehicle_uuid: UUID,
    current_user: dict = Depends(get_current_user),
    service: VehicleService = Depends(get_service),
):
    return await service.get_by_uuid(vehicle_uuid)


@router.patch(
    "/{vehicle_uuid}",
    response_model=VehicleResponse,
    status_code=status.HTTP_200_OK,
    summary="[Partner] Update vehicle details",
)
async def update_vehicle(
    vehicle_uuid: UUID,
    body: VehicleUpdate,
    partner_id: Optional[int] = None,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: VehicleService = Depends(get_service),
):
    resolved_partner_id = _resolve_partner_id(current_user, partner_id)
    return await service.update_vehicle(vehicle_uuid, resolved_partner_id, body)


# ---- Documents ----


@router.post(
    "/{vehicle_uuid}/documents",
    response_model=VehicleDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Partner] Upload a vehicle document",
)
async def upload_document(
    vehicle_uuid: UUID,
    body: VehicleDocumentUpload,
    partner_id: Optional[int] = None,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: VehicleService = Depends(get_service),
):
    resolved_partner_id = _resolve_partner_id(current_user, partner_id)
    return await service.upload_document(vehicle_uuid, resolved_partner_id, body)


# ---- Photos ----


@router.post(
    "/{vehicle_uuid}/photos",
    response_model=VehiclePhotoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[Partner] Upload a vehicle photo",
)
async def upload_photo(
    vehicle_uuid: UUID,
    body: VehiclePhotoUpload,
    partner_id: Optional[int] = None,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: VehicleService = Depends(get_service),
):
    resolved_partner_id = _resolve_partner_id(current_user, partner_id)
    return await service.upload_photo(vehicle_uuid, resolved_partner_id, body)


@router.get(
    "/{vehicle_uuid}/photos",
    response_model=list[VehiclePhotoResponse],
    status_code=status.HTTP_200_OK,
    summary="[Partner/Admin] List vehicle photos",
)
async def list_photos(
    vehicle_uuid: UUID,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: VehicleService = Depends(get_service),
):
    return await service.list_photos(vehicle_uuid)


# ---- Pricing Rules (read-only — city lookup for booking flow) ----
# Note: Create/Update/Delete pricing rules → use /admin/settings/pricing-rules


@router.get(
    "/pricing-rules/city/{city_id}",
    response_model=list[PricingRuleResponse],
    status_code=status.HTTP_200_OK,
    summary="Get pricing rules for a city",
)
async def get_pricing_rules(
    city_id: int,
    category_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
    service: VehicleService = Depends(get_service),
):
    return await service.get_pricing_rules(city_id, category_id)
