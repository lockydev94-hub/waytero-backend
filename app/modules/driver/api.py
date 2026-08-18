from uuid import UUID
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.driver.schemas import (
    DriverCreate,
    DriverUpdate,
    DriverDocumentUpload,
    DriverAvailabilityUpdate,
)
from app.modules.driver.services import DriverService

router = APIRouter()


def get_service(db: AsyncSession = Depends(get_db)) -> DriverService:
    return DriverService(db=db)


@router.post("", status_code=status.HTTP_201_CREATED, summary="[Partner] Add a driver")
async def create_driver(
    body: DriverCreate,
    partner_id: int,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: DriverService = Depends(get_service),
):
    return await service.create_driver(partner_id, body)


@router.get(
    "/{driver_uuid}", status_code=status.HTTP_200_OK, summary="Get driver by UUID"
)
async def get_driver(
    driver_uuid: UUID,
    current_user: dict = Depends(get_current_user),
    service: DriverService = Depends(get_service),
):
    return await service.get_driver(driver_uuid)


@router.patch(
    "/{driver_uuid}", status_code=status.HTTP_200_OK, summary="Update driver profile"
)
async def update_driver(
    driver_uuid: UUID,
    body: DriverUpdate,
    current_user: dict = Depends(require_roles("PARTNER", "ADMIN", "SUPER_ADMIN")),
    service: DriverService = Depends(get_service),
):
    return await service.update_driver(driver_uuid, body)


@router.patch(
    "/{driver_uuid}/availability",
    status_code=status.HTTP_200_OK,
    summary="Update driver availability",
)
async def update_availability(
    driver_uuid: UUID,
    body: DriverAvailabilityUpdate,
    current_user: dict = Depends(get_current_user),
    service: DriverService = Depends(get_service),
):
    return await service.update_availability(driver_uuid, body)


@router.post(
    "/{driver_uuid}/documents",
    status_code=status.HTTP_201_CREATED,
    summary="Upload driver document",
)
async def upload_doc(
    driver_uuid: UUID,
    body: DriverDocumentUpload,
    current_user: dict = Depends(get_current_user),
    service: DriverService = Depends(get_service),
):
    return await service.upload_document(driver_uuid, body)
