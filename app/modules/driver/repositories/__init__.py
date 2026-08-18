from typing import Optional, List
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.modules.driver.models import Driver, DriverDocument


class DriverRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _base_query(self):
        return (
            select(Driver)
            .options(selectinload(Driver.documents), selectinload(Driver.availability))
            .where(Driver.deleted_at.is_(None))
        )

    async def get_by_id(self, driver_id: int) -> Optional[Driver]:
        r = await self.db.execute(self._base_query().where(Driver.id == driver_id))
        return r.scalar_one_or_none()

    async def get_by_uuid(self, driver_uuid: UUID) -> Optional[Driver]:
        r = await self.db.execute(self._base_query().where(Driver.uuid == driver_uuid))
        return r.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[Driver]:
        r = await self.db.execute(select(Driver).where(Driver.driver_code == code))
        return r.scalar_one_or_none()

    async def get_by_mobile(self, mobile: str) -> Optional[Driver]:
        r = await self.db.execute(select(Driver).where(Driver.mobile == mobile))
        return r.scalar_one_or_none()

    async def create(self, driver: Driver) -> Driver:
        self.db.add(driver)
        await self.db.flush()
        await self.db.refresh(driver)
        return driver

    async def update(self, driver: Driver) -> Driver:
        await self.db.flush()
        await self.db.refresh(driver)
        return driver

    async def list_drivers(
        self,
        partner_id: Optional[int] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> List[Driver]:
        q = select(Driver).where(Driver.deleted_at.is_(None))
        if partner_id:
            q = q.where(Driver.partner_id == partner_id)
        if status:
            q = q.where(Driver.status == status)
        q = q.order_by(Driver.created_at.desc()).offset(skip).limit(limit)
        r = await self.db.execute(q)
        return list(r.scalars().all())

    async def add_document(self, doc: DriverDocument) -> DriverDocument:
        self.db.add(doc)
        await self.db.flush()
        await self.db.refresh(doc)
        return doc
