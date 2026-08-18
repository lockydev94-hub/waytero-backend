import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import (
    ResourceNotFoundException,
    DuplicateResourceException,
    BusinessException,
)
from app.core.logging import get_logger
from app.modules.driver.models import (
    Driver,
    DriverDocument,
    DriverAvailability,
    DriverPerformanceSummary,
)
from app.modules.driver.repositories import DriverRepository
from app.modules.driver.schemas import (
    DriverCreate,
    DriverUpdate,
    DriverDocumentUpload,
    DriverAvailabilityUpdate,
    DriverStatusUpdate,
    DriverResponse,
    DriverListResponse,
)

logger = get_logger(__name__)

VALID_STATUS_TRANSITIONS = {
    "PENDING": ["UNDER_REVIEW"],
    "UNDER_REVIEW": ["APPROVED", "PENDING"],
    "APPROVED": ["ACTIVE"],
    "ACTIVE": ["INACTIVE", "SUSPENDED"],
    "INACTIVE": ["ACTIVE", "SUSPENDED"],
    "SUSPENDED": ["ACTIVE"],
}


def _gen_code() -> str:
    import random

    return f"DRV-{random.randint(100000, 999999)}"


class DriverService:
    def __init__(self, db: AsyncSession):
        self.repo = DriverRepository(db)

    async def create_driver(
        self, partner_id: int, data: DriverCreate
    ) -> DriverResponse:
        existing = await self.repo.get_by_mobile(data.mobile)
        if existing:
            raise DuplicateResourceException("Driver", "mobile")
        code = _gen_code()
        while await self.repo.get_by_code(code):
            code = _gen_code()
        driver = Driver(
            uuid=uuid.uuid4(),
            partner_id=partner_id,
            driver_code=code,
            **data.model_dump(),
        )
        driver = await self.repo.create(driver)
        # Init availability record
        avail = DriverAvailability(driver_id=driver.id, availability_status="OFFLINE")
        self.repo.db.add(avail)
        perf = DriverPerformanceSummary(driver_id=driver.id)
        self.repo.db.add(perf)
        await self.repo.db.flush()
        logger.info("driver_created", driver_id=driver.id, code=code)
        return DriverResponse.model_validate(driver)

    async def get_driver(self, driver_uuid: UUID) -> DriverResponse:
        driver = await self.repo.get_by_uuid(driver_uuid)
        if not driver:
            raise ResourceNotFoundException("Driver", driver_uuid)
        return DriverResponse.model_validate(driver)

    async def update_driver(
        self, driver_uuid: UUID, data: DriverUpdate
    ) -> DriverResponse:
        driver = await self.repo.get_by_uuid(driver_uuid)
        if not driver:
            raise ResourceNotFoundException("Driver", driver_uuid)
        for f, v in data.model_dump(exclude_unset=True).items():
            setattr(driver, f, v)
        driver.updated_at = datetime.now(timezone.utc)
        driver = await self.repo.update(driver)
        return DriverResponse.model_validate(driver)

    async def update_status(
        self, driver_uuid: UUID, data: DriverStatusUpdate, admin_id: UUID
    ) -> DriverResponse:
        driver = await self.repo.get_by_uuid(driver_uuid)
        if not driver:
            raise ResourceNotFoundException("Driver", driver_uuid)
        allowed = VALID_STATUS_TRANSITIONS.get(driver.status, [])
        if data.status not in allowed:
            raise BusinessException(
                f"Cannot transition driver from {driver.status} to {data.status}",
                code="INVALID_STATUS_TRANSITION",
            )
        driver.status = data.status
        if data.status == "APPROVED":
            driver.approved_at = datetime.now(timezone.utc)
            driver.approved_by = admin_id
        driver.updated_at = datetime.now(timezone.utc)
        await self.repo.update(driver)
        return DriverResponse.model_validate(driver)

    async def update_availability(
        self, driver_uuid: UUID, data: DriverAvailabilityUpdate
    ) -> dict:
        driver = await self.repo.get_by_uuid(driver_uuid)
        if not driver:
            raise ResourceNotFoundException("Driver", driver_uuid)
        if driver.status != "ACTIVE":
            raise BusinessException(
                "Driver must be ACTIVE to update availability", code="DRIVER_NOT_ACTIVE"
            )
        if not driver.availability:
            driver.availability = DriverAvailability(driver_id=driver.id)
            self.repo.db.add(driver.availability)
        driver.availability.availability_status = data.availability_status
        if data.availability_status == "ONLINE":
            driver.availability.last_online_at = datetime.now(timezone.utc)
        driver.availability.updated_at = datetime.now(timezone.utc)
        await self.repo.db.flush()
        return {"availability_status": data.availability_status}

    async def upload_document(
        self, driver_uuid: UUID, data: DriverDocumentUpload
    ) -> dict:
        driver = await self.repo.get_by_uuid(driver_uuid)
        if not driver:
            raise ResourceNotFoundException("Driver", driver_uuid)
        doc = DriverDocument(
            driver_id=driver.id,
            document_type=data.document_type,
            file_url=data.file_url,
            expiry_date=data.expiry_date,
        )
        await self.repo.add_document(doc)
        return {"message": "Document uploaded", "document_type": data.document_type}

    async def list_drivers(
        self,
        partner_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        skip = (page - 1) * per_page
        drivers = await self.repo.list_drivers(
            partner_id=partner_id, status=status, skip=skip, limit=per_page
        )
        return {
            "items": [DriverListResponse.model_validate(d) for d in drivers],
            "page": page,
            "per_page": per_page,
        }
