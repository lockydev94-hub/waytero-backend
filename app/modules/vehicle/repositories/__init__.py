# ============================================================
# WAY TERO — VEHICLE REPOSITORY
# File: app/modules/vehicle/repositories/__init__.py
# Doc Ref: DB Schema Part 3, Sections 10-20
# Phase: 2 — Vehicle Module
# ============================================================

from typing import Optional, List
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.vehicle.models import (
    Vehicle,
    VehicleDocument,
    VehicleAvailability,
    VehicleCategory,
    VehiclePricingRule,
    VehiclePhotoUpload,
)


class VehicleCategoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_active(self) -> List[VehicleCategory]:
        r = await self.db.execute(
            select(VehicleCategory)
            .where(VehicleCategory.is_active.is_(True))
            .order_by(VehicleCategory.category_name)
        )
        return list(r.scalars().all())

    async def get_by_id(self, cat_id: int) -> Optional[VehicleCategory]:
        r = await self.db.execute(
            select(VehicleCategory).where(VehicleCategory.id == cat_id)
        )
        return r.scalar_one_or_none()


class VehicleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _base_query(self):
        return (
            select(Vehicle)
            .options(
                selectinload(Vehicle.documents),
                selectinload(Vehicle.availability),
                selectinload(Vehicle.performance),
                selectinload(Vehicle.photos),
            )
            .where(Vehicle.deleted_at.is_(None))
        )

    async def get_by_id(self, vehicle_id: int) -> Optional[Vehicle]:
        r = await self.db.execute(self._base_query().where(Vehicle.id == vehicle_id))
        return r.scalar_one_or_none()

    async def get_by_uuid(self, vehicle_uuid: UUID) -> Optional[Vehicle]:
        r = await self.db.execute(
            self._base_query().where(Vehicle.uuid == vehicle_uuid)
        )
        return r.scalar_one_or_none()

    async def get_by_registration(self, reg_number: str) -> Optional[Vehicle]:
        r = await self.db.execute(
            select(Vehicle).where(Vehicle.registration_number == reg_number)
        )
        return r.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[Vehicle]:
        r = await self.db.execute(select(Vehicle).where(Vehicle.vehicle_code == code))
        return r.scalar_one_or_none()

    async def create(self, vehicle: Vehicle) -> Vehicle:
        self.db.add(vehicle)
        await self.db.flush()
        fetched = await self.get_by_uuid(vehicle.uuid)
        return fetched  # type: ignore[return-value]

    async def update(self, vehicle: Vehicle) -> Vehicle:
        await self.db.flush()
        fetched = await self.get_by_uuid(vehicle.uuid)
        return fetched  # type: ignore[return-value]

    async def list_by_partner(
        self, partner_id: int, skip: int = 0, limit: int = 20
    ) -> List[Vehicle]:
        r = await self.db.execute(
            self._base_query()
            .where(Vehicle.partner_id == partner_id)
            .order_by(Vehicle.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(r.scalars().all())

    async def list_active(
        self,
        city_id: Optional[int] = None,
        category_id: Optional[int] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> List[Vehicle]:
        q = self._base_query().where(Vehicle.status == "ACTIVE")
        if city_id:
            q = q.where(Vehicle.city_id == city_id)
        if category_id:
            q = q.where(Vehicle.vehicle_category_id == category_id)
        q = q.order_by(Vehicle.created_at.desc()).offset(skip).limit(limit)
        r = await self.db.execute(q)
        return list(r.scalars().all())

    async def count_by_partner(self, partner_id: int) -> int:
        r = await self.db.execute(
            select(func.count(Vehicle.id)).where(
                Vehicle.partner_id == partner_id, Vehicle.deleted_at.is_(None)
            )
        )
        return r.scalar_one()

    async def add_document(self, doc: VehicleDocument) -> VehicleDocument:
        self.db.add(doc)
        await self.db.flush()
        await self.db.refresh(doc)
        return doc

    async def get_document(
        self, doc_id: int, vehicle_id: int
    ) -> Optional[VehicleDocument]:
        r = await self.db.execute(
            select(VehicleDocument).where(
                VehicleDocument.id == doc_id,
                VehicleDocument.vehicle_id == vehicle_id,
            )
        )
        return r.scalar_one_or_none()

    async def add_photo(self, photo: VehiclePhotoUpload) -> VehiclePhotoUpload:
        self.db.add(photo)
        await self.db.flush()
        await self.db.refresh(photo)
        return photo

    async def list_photos(self, vehicle_id: int) -> List[VehiclePhotoUpload]:
        r = await self.db.execute(
            select(VehiclePhotoUpload)
            .where(VehiclePhotoUpload.vehicle_id == vehicle_id)
            .order_by(VehiclePhotoUpload.uploaded_at.asc())
        )
        return list(r.scalars().all())

    async def upsert_availability(
        self, vehicle_id: int, status: str
    ) -> VehicleAvailability:
        r = await self.db.execute(
            select(VehicleAvailability).where(
                VehicleAvailability.vehicle_id == vehicle_id
            )
        )
        avail = r.scalar_one_or_none()
        if avail:
            avail.availability_status = status
        else:
            avail = VehicleAvailability(
                vehicle_id=vehicle_id, availability_status=status
            )
            self.db.add(avail)
        await self.db.flush()
        return avail


class PricingRuleRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, rule: VehiclePricingRule) -> VehiclePricingRule:
        self.db.add(rule)
        await self.db.flush()
        await self.db.refresh(rule)
        return rule

    async def list_by_city_and_category(
        self, city_id: int, category_id: int
    ) -> List[VehiclePricingRule]:
        r = await self.db.execute(
            select(VehiclePricingRule)
            .where(
                VehiclePricingRule.city_id == city_id,
                VehiclePricingRule.vehicle_category_id == category_id,
            )
            .order_by(VehiclePricingRule.trip_type)
        )
        return list(r.scalars().all())

    async def list_by_city(self, city_id: int) -> List[VehiclePricingRule]:
        r = await self.db.execute(
            select(VehiclePricingRule)
            .where(
                VehiclePricingRule.city_id == city_id,
            )
            .order_by(
                VehiclePricingRule.vehicle_category_id, VehiclePricingRule.trip_type
            )
        )
        return list(r.scalars().all())
