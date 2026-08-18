# ============================================================
# WAY TERO — CUSTOMER REPOSITORY
# File: app/modules/customer/repositories/__init__.py
# Doc Ref: Module Structure — DB queries only in repository
# Phase: 2 — Customer Module
# ============================================================

from typing import Optional, List
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.customer.models import Customer, CustomerAddress


class CustomerRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_user_id(self, user_id: UUID) -> Optional[Customer]:
        result = await self.db.execute(
            select(Customer)
            .options(selectinload(Customer.addresses))
            .where(Customer.user_id == user_id, Customer.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, customer_id: int) -> Optional[Customer]:
        result = await self.db.execute(
            select(Customer)
            .options(selectinload(Customer.addresses))
            .where(Customer.id == customer_id, Customer.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_uuid(self, customer_uuid: UUID) -> Optional[Customer]:
        result = await self.db.execute(
            select(Customer)
            .options(selectinload(Customer.addresses))
            .where(Customer.uuid == customer_uuid, Customer.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, customer_code: str) -> Optional[Customer]:
        result = await self.db.execute(
            select(Customer).where(Customer.customer_code == customer_code)
        )
        return result.scalar_one_or_none()

    async def create(self, customer: Customer) -> Customer:
        self.db.add(customer)
        await self.db.flush()
        await self.db.refresh(customer)
        return customer

    async def update(self, customer: Customer) -> Customer:
        await self.db.flush()
        await self.db.refresh(customer)
        return customer

    async def list_active(
        self, city_id: Optional[int] = None, skip: int = 0, limit: int = 20
    ) -> List[Customer]:
        q = select(Customer).where(
            Customer.is_active.is_(True), Customer.deleted_at.is_(None)
        )
        if city_id:
            q = q.where(Customer.city_id == city_id)
        q = q.order_by(Customer.created_at.desc()).offset(skip).limit(limit)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def count_active(self, city_id: Optional[int] = None) -> int:
        from sqlalchemy import func

        q = select(func.count(Customer.id)).where(
            Customer.is_active.is_(True), Customer.deleted_at.is_(None)
        )
        if city_id:
            q = q.where(Customer.city_id == city_id)
        result = await self.db.execute(q)
        return result.scalar_one()

    # ---- Address operations ----

    async def add_address(self, address: CustomerAddress) -> CustomerAddress:
        self.db.add(address)
        await self.db.flush()
        await self.db.refresh(address)
        return address

    async def get_address(
        self, address_id: int, customer_id: int
    ) -> Optional[CustomerAddress]:
        result = await self.db.execute(
            select(CustomerAddress).where(
                CustomerAddress.id == address_id,
                CustomerAddress.customer_id == customer_id,
            )
        )
        return result.scalar_one_or_none()

    async def clear_default_addresses(self, customer_id: int) -> None:
        await self.db.execute(
            update(CustomerAddress)
            .where(CustomerAddress.customer_id == customer_id)
            .values(is_default=False)
        )

    async def delete_address(self, address: CustomerAddress) -> None:
        await self.db.delete(address)
        await self.db.flush()
