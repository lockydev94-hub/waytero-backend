# ============================================================
# WAY TERO — VEHICLE SERVICE
# File: app/modules/vehicle/services/__init__.py
# Doc Ref: DB Schema Part 3, Sections 10-20
# Doc Ref: Backend Architecture — Module Structure
# Phase: 2 — Vehicle Module
# ============================================================

import random
import uuid
from datetime import datetime, timezone
from typing import Optional, List
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ResourceNotFoundException,
    BusinessException,
    DuplicateResourceException,
    PermissionDeniedException,
)
from app.core.logging import get_logger
from app.modules.vehicle.models import (
    Vehicle,
    VehicleDocument,
    VehiclePricingRule,
    VehiclePhotoUpload,
)
from app.modules.vehicle.repositories import (
    VehicleRepository,
    VehicleCategoryRepository,
    PricingRuleRepository,
)
from app.modules.vehicle.schemas import (
    VehicleRegister,
    VehicleUpdate,
    VehicleStatusUpdate,
    VehicleResponse,
    VehicleListResponse,
    VehicleCategoryResponse,
    VehicleDocumentUpload,
    VehicleDocumentResponse,
    VehiclePhotoUpload as VehiclePhotoUploadSchema,
    VehiclePhotoResponse,
    PricingRuleCreate,
    PricingRuleResponse,
)

logger = get_logger(__name__)

# Valid status transitions — Doc Ref: DB Schema Part 3, Section 12
_STATUS_TRANSITIONS = {
    "PENDING": {"UNDER_REVIEW", "SUSPENDED"},
    "UNDER_REVIEW": {"APPROVED", "PENDING", "SUSPENDED"},
    "APPROVED": {"ACTIVE", "SUSPENDED"},
    "ACTIVE": {"MAINTENANCE", "SUSPENDED", "INACTIVE"},
    "MAINTENANCE": {"ACTIVE", "SUSPENDED"},
    "INACTIVE": {"ACTIVE", "SUSPENDED"},
    "SUSPENDED": {"ACTIVE", "PENDING"},
}


def _generate_vehicle_code() -> str:
    return f"VHL-{random.randint(100000, 999999)}"


class VehicleService:
    def __init__(self, db: AsyncSession):
        self.repo = VehicleRepository(db)
        self.cat_repo = VehicleCategoryRepository(db)
        self.pricing_repo = PricingRuleRepository(db)

    # ---- Categories ----

    async def list_categories(self) -> List[VehicleCategoryResponse]:
        cats = await self.cat_repo.list_active()
        return [VehicleCategoryResponse.model_validate(c) for c in cats]

    # ---- Register Vehicle ----

    async def register_vehicle(
        self, partner_id: int, data: VehicleRegister
    ) -> VehicleResponse:
        """
        Register a new vehicle under a partner.
        Doc Ref: DB Schema Part 3, Section 12
        Business Rule: registration_number must be unique across platform.
        """
        existing = await self.repo.get_by_registration(data.registration_number.upper())
        if existing:
            raise DuplicateResourceException(
                "Vehicle", "registration_number", data.registration_number
            )

        cat = await self.cat_repo.get_by_id(data.vehicle_category_id)
        if not cat:
            raise ResourceNotFoundException("VehicleCategory", data.vehicle_category_id)

        code = _generate_vehicle_code()
        while await self.repo.get_by_code(code):
            code = _generate_vehicle_code()

        vehicle = Vehicle(
            uuid=uuid.uuid4(),
            partner_id=partner_id,
            vehicle_category_id=data.vehicle_category_id,
            vehicle_code=code,
            registration_number=data.registration_number.upper(),
            vehicle_brand=data.vehicle_brand,
            vehicle_model=data.vehicle_model,
            manufacturing_year=data.manufacturing_year,
            fuel_type=data.fuel_type,
            seating_capacity=data.seating_capacity,
            city_id=data.city_id,
            status="PENDING",
        )
        vehicle = await self.repo.create(vehicle)
        logger.info(
            "vehicle_registered",
            vehicle_id=vehicle.id,
            code=code,
            reg=vehicle.registration_number,
            partner_id=partner_id,
        )
        return VehicleResponse.model_validate(vehicle)

    # ---- Read ----

    async def get_by_uuid(self, vehicle_uuid: UUID) -> VehicleResponse:
        vehicle = await self.repo.get_by_uuid(vehicle_uuid)
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", vehicle_uuid)
        return VehicleResponse.model_validate(vehicle)

    # Required document types for a vehicle to be verified
    _REQUIRED_DOC_TYPES = frozenset(
        {"RC", "INSURANCE", "FITNESS_CERTIFICATE", "PERMIT", "PUC"}
    )

    async def list_by_partner(
        self, partner_id: int, page: int = 1, per_page: int = 20
    ) -> dict:
        skip = (page - 1) * per_page
        vehicles = await self.repo.list_by_partner(
            partner_id, skip=skip, limit=per_page
        )
        total = await self.repo.count_by_partner(partner_id)

        items = []
        for v in vehicles:
            # Count required docs that have been uploaded (docs loaded via selectinload)
            uploaded = {d.document_type for d in (v.documents or []) if d.document_type}
            doc_count = len(uploaded & self._REQUIRED_DOC_TYPES)
            row = VehicleListResponse.model_validate(v)
            row.doc_count = doc_count
            items.append(row)

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }

    async def list_active(
        self,
        city_id: Optional[int] = None,
        category_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        skip = (page - 1) * per_page
        vehicles = await self.repo.list_active(
            city_id=city_id, category_id=category_id, skip=skip, limit=per_page
        )
        return {
            "items": [VehicleListResponse.model_validate(v) for v in vehicles],
            "page": page,
            "per_page": per_page,
        }

    # ---- Update ----

    async def update_vehicle(
        self, vehicle_uuid: UUID, partner_id: int, data: VehicleUpdate
    ) -> VehicleResponse:
        vehicle = await self.repo.get_by_uuid(vehicle_uuid)
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", vehicle_uuid)
        if vehicle.partner_id != partner_id:
            raise PermissionDeniedException("You do not own this vehicle.")

        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(vehicle, field, value)
        vehicle.updated_at = datetime.now(timezone.utc)

        vehicle = await self.repo.update(vehicle)
        logger.info("vehicle_updated", vehicle_id=vehicle.id)
        return VehicleResponse.model_validate(vehicle)

    async def update_status(
        self, vehicle_uuid: UUID, data: VehicleStatusUpdate, updated_by: UUID
    ) -> VehicleResponse:
        """Admin-only status transition with validation."""
        vehicle = await self.repo.get_by_uuid(vehicle_uuid)
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", vehicle_uuid)

        allowed = _STATUS_TRANSITIONS.get(vehicle.status, set())
        if data.status not in allowed:
            raise BusinessException(
                message=f"Cannot transition vehicle from {vehicle.status} → {data.status}.",
                code="INVALID_STATUS_TRANSITION",
            )

        vehicle.status = data.status
        vehicle.updated_at = datetime.now(timezone.utc)
        if data.status == "APPROVED":
            vehicle.approved_at = datetime.now(timezone.utc)

        vehicle = await self.repo.update(vehicle)
        logger.info(
            "vehicle_status_updated",
            vehicle_id=vehicle.id,
            new_status=data.status,
            by=str(updated_by),
        )

        # Sync availability record when vehicle becomes ACTIVE
        if data.status == "ACTIVE":
            await self.repo.upsert_availability(vehicle.id, "AVAILABLE")

        return VehicleResponse.model_validate(vehicle)

    # ---- Documents ----

    async def upload_document(
        self, vehicle_uuid: UUID, partner_id: int, data: VehicleDocumentUpload
    ) -> VehicleDocumentResponse:
        vehicle = await self.repo.get_by_uuid(vehicle_uuid)
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", vehicle_uuid)
        if vehicle.partner_id != partner_id:
            raise PermissionDeniedException("You do not own this vehicle.")

        doc = VehicleDocument(
            vehicle_id=vehicle.id,
            document_type=data.document_type,
            file_url=data.file_url,
            expiry_date=data.expiry_date,
            verification_status="PENDING",
        )
        doc = await self.repo.add_document(doc)
        logger.info(
            "vehicle_document_uploaded",
            vehicle_id=vehicle.id,
            doc_id=doc.id,
            doc_type=data.document_type,
        )
        return VehicleDocumentResponse.model_validate(doc)

    # ---- Photos ----

    async def upload_photo(
        self, vehicle_uuid: UUID, partner_id: int, data: VehiclePhotoUploadSchema
    ) -> VehiclePhotoResponse:
        vehicle = await self.repo.get_by_uuid(vehicle_uuid)
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", vehicle_uuid)
        if vehicle.partner_id != partner_id:
            raise PermissionDeniedException("You do not own this vehicle.")

        photo = VehiclePhotoUpload(
            vehicle_id=vehicle.id,
            photo_type=data.photo_type,
            file_url=data.file_url,
            caption=data.caption,
            verification_status="PENDING",
        )
        photo = await self.repo.add_photo(photo)
        logger.info(
            "vehicle_photo_uploaded",
            vehicle_id=vehicle.id,
            photo_id=photo.id,
            photo_type=data.photo_type,
        )
        return VehiclePhotoResponse.model_validate(photo)

    async def list_photos(self, vehicle_uuid: UUID) -> list[VehiclePhotoResponse]:
        vehicle = await self.repo.get_by_uuid(vehicle_uuid)
        if not vehicle:
            raise ResourceNotFoundException("Vehicle", vehicle_uuid)
        photos = await self.repo.list_photos(vehicle.id)
        return [VehiclePhotoResponse.model_validate(p) for p in photos]

    # ---- Pricing Rules ----

    async def create_pricing_rule(self, data: PricingRuleCreate) -> PricingRuleResponse:
        rule = VehiclePricingRule(**data.model_dump())
        rule = await self.pricing_repo.create(rule)
        logger.info(
            "pricing_rule_created",
            rule_id=rule.id,
            city_id=data.city_id,
            category_id=data.vehicle_category_id,
        )
        return PricingRuleResponse.model_validate(rule)

    async def get_pricing_rules(
        self, city_id: int, category_id: Optional[int] = None
    ) -> List[PricingRuleResponse]:
        if category_id:
            rules = await self.pricing_repo.list_by_city_and_category(
                city_id, category_id
            )
        else:
            rules = await self.pricing_repo.list_by_city(city_id)
        return [PricingRuleResponse.model_validate(r) for r in rules]
