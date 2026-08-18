# ============================================================
# WAY TERO — PARTNER REPOSITORY
# File: app/modules/partner/repositories/__init__.py
# Phase: 2 — Partner Module
# ============================================================

from typing import Optional, List
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.partner.models import (
    Partner,
    PartnerService,
    PartnerDocument,
    PartnerBankAccount,
    PartnerGSTDetails,
    PartnerVerificationLog,
)


class PartnerRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _base_query(self):
        return (
            select(Partner)
            .options(
                selectinload(Partner.services),
                selectinload(Partner.documents),
                selectinload(Partner.bank_accounts),
                selectinload(Partner.gst_details),
                selectinload(Partner.ratings),
            )
            .where(Partner.deleted_at.is_(None))
        )

    async def get_by_user_id(self, user_id: UUID) -> Optional[Partner]:
        result = await self.db.execute(
            self._base_query().where(Partner.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, partner_id: int) -> Optional[Partner]:
        result = await self.db.execute(
            self._base_query().where(Partner.id == partner_id)
        )
        return result.scalar_one_or_none()

    async def get_by_uuid(self, partner_uuid: UUID) -> Optional[Partner]:
        result = await self.db.execute(
            self._base_query().where(Partner.uuid == partner_uuid)
        )
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[Partner]:
        result = await self.db.execute(
            select(Partner).where(Partner.partner_code == code)
        )
        return result.scalar_one_or_none()

    async def create(self, partner: Partner) -> Partner:
        self.db.add(partner)
        await self.db.flush()
        await self.db.refresh(partner)
        return partner

    async def update(self, partner: Partner) -> Partner:
        await self.db.flush()
        await self.db.refresh(partner)
        return partner

    async def list_partners(
        self,
        status: Optional[str] = None,
        city_id: Optional[int] = None,
        partner_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> List[Partner]:
        q = select(Partner).where(Partner.deleted_at.is_(None))
        if status:
            q = q.where(Partner.status == status)
        if city_id:
            q = q.where(Partner.city_id == city_id)
        if partner_type:
            q = q.where(Partner.partner_type == partner_type)
        q = q.order_by(Partner.created_at.desc()).offset(skip).limit(limit)
        result = await self.db.execute(q)
        return list(result.scalars().all())

    async def count_partners(
        self, status: Optional[str] = None, city_id: Optional[int] = None
    ) -> int:
        q = select(func.count(Partner.id)).where(Partner.deleted_at.is_(None))
        if status:
            q = q.where(Partner.status == status)
        if city_id:
            q = q.where(Partner.city_id == city_id)
        result = await self.db.execute(q)
        return result.scalar_one()

    async def add_document(self, doc: PartnerDocument) -> PartnerDocument:
        self.db.add(doc)
        await self.db.flush()
        await self.db.refresh(doc)
        return doc

    async def get_document(
        self, doc_id: int, partner_id: int
    ) -> Optional[PartnerDocument]:
        result = await self.db.execute(
            select(PartnerDocument).where(
                PartnerDocument.id == doc_id,
                PartnerDocument.partner_id == partner_id,
            )
        )
        return result.scalar_one_or_none()

    async def add_bank_account(self, bank: PartnerBankAccount) -> PartnerBankAccount:
        self.db.add(bank)
        await self.db.flush()
        await self.db.refresh(bank)
        return bank

    async def add_gst(self, gst: PartnerGSTDetails) -> PartnerGSTDetails:
        self.db.add(gst)
        await self.db.flush()
        await self.db.refresh(gst)
        return gst

    async def add_verification_log(self, log: PartnerVerificationLog) -> None:
        self.db.add(log)
        await self.db.flush()

    async def get_service(
        self, partner_id: int, service_type: str
    ) -> Optional[PartnerService]:
        result = await self.db.execute(
            select(PartnerService).where(
                PartnerService.partner_id == partner_id,
                PartnerService.service_type == service_type,
            )
        )
        return result.scalar_one_or_none()

    async def add_service(self, service: PartnerService) -> PartnerService:
        self.db.add(service)
        await self.db.flush()
        await self.db.refresh(service)
        return service
