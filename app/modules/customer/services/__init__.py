# ============================================================
# WAY TERO — CUSTOMER SERVICE
# File: app/modules/customer/services/__init__.py
# Doc Ref: Module Structure — business logic in service
# Phase: 2 — Customer Module
# ============================================================

import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ResourceNotFoundException
from app.core.logging import get_logger
from app.modules.customer.models import Customer, CustomerAddress
from app.modules.customer.repositories import CustomerRepository
from app.modules.customer.schemas import (
    CustomerProfileUpdate,
    CustomerResponse,
    CustomerListResponse,
    AddressCreate,
    AddressResponse,
)

logger = get_logger(__name__)


def _generate_customer_code() -> str:
    """Generate sequential-style customer code: CUS-XXXXXX"""
    import random

    return f"CUS-{random.randint(100000, 999999)}"


class CustomerService:
    def __init__(self, db: AsyncSession):
        self.repo = CustomerRepository(db)

    async def get_or_create_profile(
        self, user_id: UUID, first_name: str = "", last_name: str = ""
    ) -> CustomerResponse:
        """
        Called after OTP login — creates Customer profile if first login.
        Doc Ref: BRD Part 2, Section 14 — Customer Registration.
        """
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            code = _generate_customer_code()
            # Ensure uniqueness
            while await self.repo.get_by_code(code):
                code = _generate_customer_code()

            customer = Customer(
                uuid=uuid.uuid4(),
                user_id=user_id,
                customer_code=code,
                first_name=first_name or None,
                last_name=last_name or None,
                is_active=True,
            )
            customer = await self.repo.create(customer)
            # Auto-create customer wallet (balance = 0)
            from sqlalchemy import text as _text

            await self.repo.db.execute(
                _text(
                    """
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance, wallet_status, created_at, updated_at)
                VALUES (:cid, 0, 0, 'ACTIVE', NOW(), NOW())
                ON CONFLICT (customer_id) DO NOTHING
            """
                ),
                {"cid": customer.id},
            )
            await self.repo.db.commit()
            logger.info("customer_created", customer_id=customer.id, code=code)

        return CustomerResponse.model_validate(customer)

    async def get_profile(self, user_id: UUID) -> CustomerResponse:
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            raise ResourceNotFoundException("Customer")
        # Join the users row so callers (booking flow) can see the mobile /
        # email on the same record — saves a round-trip when deciding whether
        # the customer has already verified a phone number.
        user_row = (
            (
                await self.repo.db.execute(
                    __import__("sqlalchemy").text(
                        "SELECT mobile_number, email FROM users WHERE id = :uid"
                    ),
                    {"uid": str(user_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        out = CustomerResponse.model_validate(customer)
        if user_row:
            out.mobile_number = user_row.get("mobile_number")
            out.email = user_row.get("email")
        return out

    async def get_by_uuid(self, customer_uuid: UUID) -> CustomerResponse:
        customer = await self.repo.get_by_uuid(customer_uuid)
        if not customer:
            raise ResourceNotFoundException("Customer", customer_uuid)
        return CustomerResponse.model_validate(customer)

    async def update_profile(
        self, user_id: UUID, data: CustomerProfileUpdate
    ) -> CustomerResponse:
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            raise ResourceNotFoundException("Customer")

        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(customer, field, value)
        customer.updated_at = datetime.now(timezone.utc)

        customer = await self.repo.update(customer)
        logger.info("customer_updated", customer_id=customer.id)
        return CustomerResponse.model_validate(customer)

    async def deactivate(self, user_id: UUID) -> None:
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            raise ResourceNotFoundException("Customer")
        customer.is_active = False
        customer.deleted_at = datetime.now(timezone.utc)
        await self.repo.update(customer)
        logger.info("customer_deactivated", customer_id=customer.id)

    async def list_customers(
        self, city_id: Optional[int] = None, page: int = 1, per_page: int = 20
    ):
        skip = (page - 1) * per_page
        customers = await self.repo.list_active(
            city_id=city_id, skip=skip, limit=per_page
        )
        total = await self.repo.count_active(city_id=city_id)
        return {
            "items": [CustomerListResponse.model_validate(c) for c in customers],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }

    # ---- Address Management ----

    async def add_address(self, user_id: UUID, data: AddressCreate) -> AddressResponse:
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            raise ResourceNotFoundException("Customer")

        if data.is_default:
            await self.repo.clear_default_addresses(customer.id)

        address = CustomerAddress(
            customer_id=customer.id,
            **data.model_dump(),
        )
        address = await self.repo.add_address(address)
        logger.info(
            "customer_address_added", customer_id=customer.id, address_id=address.id
        )
        return AddressResponse.model_validate(address)

    async def set_default_address(
        self, user_id: UUID, address_id: int
    ) -> AddressResponse:
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            raise ResourceNotFoundException("Customer")

        address = await self.repo.get_address(address_id, customer.id)
        if not address:
            raise ResourceNotFoundException("Address", address_id)

        await self.repo.clear_default_addresses(customer.id)
        address.is_default = True
        await self.repo.add_address(address)
        return AddressResponse.model_validate(address)

    async def delete_address(self, user_id: UUID, address_id: int) -> None:
        customer = await self.repo.get_by_user_id(user_id)
        if not customer:
            raise ResourceNotFoundException("Customer")
        address = await self.repo.get_address(address_id, customer.id)
        if not address:
            raise ResourceNotFoundException("Address", address_id)
        await self.repo.delete_address(address)
        logger.info(
            "customer_address_deleted", customer_id=customer.id, address_id=address_id
        )
