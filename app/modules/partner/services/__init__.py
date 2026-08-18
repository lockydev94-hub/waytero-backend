# ============================================================
# WAY TERO — PARTNER SERVICE
# File: app/modules/partner/services/__init__.py
# Doc Ref: BRD Part 2 — Partner Management (Sections 17-19)
# Phase: 2 — Partner Module
# ============================================================

import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ResourceNotFoundException,
    BusinessException,
    DuplicateResourceException,
)
from app.core.logging import get_logger
from app.modules.partner.models import (
    Partner,
    PartnerDocument,
    PartnerBankAccount,
    PartnerGSTDetails,
    PartnerVerificationLog,
    PartnerRating,
)
from app.modules.partner.repositories import PartnerRepository
from app.modules.partner.schemas import (
    PartnerRegisterRequest,
    PartnerProfileUpdate,
    PartnerResponse,
    PartnerListResponse,
    PartnerDocumentUpload,
    BankAccountCreate,
    GSTDetailsCreate,
    PartnerServiceUpdate,
    PartnerStatusUpdate,
    DocumentResponse,
    BankAccountResponse,
    PartnerServiceResponse,
)

logger = get_logger(__name__)


def _generate_partner_code() -> str:
    import random

    return f"PRT-{random.randint(100000, 999999)}"


# Valid status transitions per docs
VALID_STATUS_TRANSITIONS = {
    "PENDING": ["UNDER_REVIEW"],
    "UNDER_REVIEW": ["DOCUMENT_PENDING", "APPROVED"],
    "DOCUMENT_PENDING": ["UNDER_REVIEW", "APPROVED"],
    "APPROVED": ["ACTIVE", "SUSPENDED"],
    "ACTIVE": ["SUSPENDED", "BLOCKED"],
    "SUSPENDED": ["ACTIVE", "BLOCKED"],
    "BLOCKED": [],
}


class PartnerService:
    def __init__(self, db: AsyncSession):
        self.repo = PartnerRepository(db)

    async def register(
        self, user_id: UUID, data: PartnerRegisterRequest
    ) -> PartnerResponse:
        """Create partner profile after OTP verification. Doc Ref: BRD Section 19 Step 1"""
        existing = await self.repo.get_by_user_id(user_id)
        if existing:
            raise DuplicateResourceException("Partner", "user")

        code = _generate_partner_code()
        while await self.repo.get_by_code(code):
            code = _generate_partner_code()

        partner = Partner(
            uuid=uuid.uuid4(),
            user_id=user_id,
            partner_code=code,
            partner_type=data.partner_type,
            owner_name=data.owner_name,
            business_name=data.business_name,
            mobile=data.mobile,
            email=data.email,
            city_id=data.city_id,
            status="PENDING",
            onboarding_source=data.onboarding_source,
        )
        partner = await self.repo.create(partner)

        # Auto-create rating record
        rating = PartnerRating(partner_id=partner.id)
        self.repo.db.add(rating)
        await self.repo.db.flush()

        await self.repo.add_verification_log(
            PartnerVerificationLog(
                partner_id=partner.id,
                action="PARTNER_REGISTERED",
                remarks="Partner application created",
                performed_by=user_id,
            )
        )

        # Auto-create partner wallet (PREPAID, balance = 0)
        from sqlalchemy import text as _text

        await self.repo.db.execute(
            _text(
                """
            INSERT INTO wallets (partner_id, wallet_type, available_balance, hold_balance, credit_limit, wallet_status, created_at)
            VALUES (:pid, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW())
            ON CONFLICT (partner_id) DO NOTHING
        """
            ),
            {"pid": partner.id},
        )

        # get_db auto-commits on success; nothing to do here.
        # Re-fetch with all relationships eagerly loaded to avoid MissingGreenlet
        partner = await self.repo.get_by_id(partner.id)
        logger.info("partner_registered", partner_id=partner.id, code=code)

        # ── Email: partner registration received ──
        try:
            from app.infrastructure.email import send_event_email

            await send_event_email(
                self.repo.db,
                event_type="partner_registered",
                to_email=data.email or "",
                to_name=data.owner_name or None,
                context={
                    "name": data.owner_name or data.business_name or "partner",
                    "message": (
                        f"Thank you for registering with WayTero. Your application "
                        f"({code}) is now under review — we typically respond within 24–48 hours."
                    ),
                    "body": [
                        "Our verification team will review your business details and "
                        "documents. You'll get an email as soon as your partner account "
                        "is approved.",
                    ],
                    "details": [
                        ("Partner code", code),
                        ("Business name", data.business_name),
                        ("Status", "Pending review"),
                    ],
                },
                related_type="PARTNER",
                related_id=partner.id,
            )
        except Exception:  # pragma: no cover — email must never break registration
            pass

        return PartnerResponse.model_validate(partner)

    async def get_profile(self, user_id: UUID) -> PartnerResponse:
        partner = await self.repo.get_by_user_id(user_id)
        if not partner:
            raise ResourceNotFoundException("Partner")
        return PartnerResponse.model_validate(partner)

    async def get_by_uuid(self, partner_uuid: UUID) -> PartnerResponse:
        partner = await self.repo.get_by_uuid(partner_uuid)
        if not partner:
            raise ResourceNotFoundException("Partner", partner_uuid)
        return PartnerResponse.model_validate(partner)

    async def update_profile(
        self, user_id: UUID, data: PartnerProfileUpdate
    ) -> PartnerResponse:
        partner = await self.repo.get_by_user_id(user_id)
        if not partner:
            raise ResourceNotFoundException("Partner")
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(partner, field, value)
        partner.updated_at = datetime.now(timezone.utc)
        await self.repo.update(partner)
        # Re-fetch with selectinload to avoid MissingGreenlet on relationships
        partner = await self.repo.get_by_user_id(user_id)
        return PartnerResponse.model_validate(partner)

    async def update_status(
        self, partner_uuid: UUID, data: PartnerStatusUpdate, admin_user_id: UUID
    ) -> PartnerResponse:
        """Admin status transition. Enforces valid status flow."""
        partner = await self.repo.get_by_uuid(partner_uuid)
        if not partner:
            raise ResourceNotFoundException("Partner", partner_uuid)

        allowed = VALID_STATUS_TRANSITIONS.get(partner.status, [])
        if data.status not in allowed:
            raise BusinessException(
                f"Cannot transition partner from {partner.status} to {data.status}",
                code="INVALID_STATUS_TRANSITION",
            )

        old_status = partner.status
        partner.status = data.status
        partner.updated_at = datetime.now(timezone.utc)
        if data.status == "APPROVED":
            partner.approved_at = datetime.now(timezone.utc)
            partner.approved_by = admin_user_id

        await self.repo.update(partner)
        await self.repo.add_verification_log(
            PartnerVerificationLog(
                partner_id=partner.id,
                action=f"STATUS_{data.status}",
                remarks=data.remarks
                or f"Status changed from {old_status} to {data.status}",
                performed_by=admin_user_id,
            )
        )
        # get_db auto-commits on success.
        # Re-fetch with selectinload to avoid MissingGreenlet on relationships
        partner = await self.repo.get_by_uuid(partner_uuid)
        logger.info(
            "partner_status_updated",
            partner_id=partner.id,
            old=old_status,
            new=data.status,
        )
        return PartnerResponse.model_validate(partner)

    async def upload_document(
        self, user_id: UUID, data: PartnerDocumentUpload
    ) -> DocumentResponse:
        partner = await self.repo.get_by_user_id(user_id)
        if not partner:
            raise ResourceNotFoundException("Partner")
        doc = PartnerDocument(
            partner_id=partner.id,
            document_type=data.document_type,
            file_url=data.file_url,
            expiry_date=data.expiry_date,
            verification_status="PENDING",
        )
        doc = await self.repo.add_document(doc)
        logger.info(
            "partner_document_uploaded",
            partner_id=partner.id,
            doc_type=data.document_type,
        )
        return DocumentResponse.model_validate(doc)

    async def add_bank_account(
        self, user_id: UUID, data: BankAccountCreate
    ) -> BankAccountResponse:
        partner = await self.repo.get_by_user_id(user_id)
        if not partner:
            raise ResourceNotFoundException("Partner")
        bank = PartnerBankAccount(
            partner_id=partner.id,
            **data.model_dump(),
        )
        bank = await self.repo.add_bank_account(bank)
        logger.info("partner_bank_added", partner_id=partner.id)
        return BankAccountResponse.model_validate(bank)

    async def add_gst_details(self, user_id: UUID, data: GSTDetailsCreate) -> dict:
        partner = await self.repo.get_by_user_id(user_id)
        if not partner:
            raise ResourceNotFoundException("Partner")
        if partner.gst_details:
            raise DuplicateResourceException("GST Details", "partner")
        gst = PartnerGSTDetails(partner_id=partner.id, **data.model_dump())
        await self.repo.add_gst(gst)
        return {"message": "GST details added successfully"}

    async def toggle_service(
        self, user_id: UUID, data: PartnerServiceUpdate
    ) -> PartnerServiceResponse:
        partner = await self.repo.get_by_user_id(user_id)
        if not partner:
            raise ResourceNotFoundException("Partner")
        service = await self.repo.get_service(partner.id, data.service_type)
        if service:
            service.is_active = data.is_active
            await self.repo.db.flush()
        else:
            service = PartnerService(
                partner_id=partner.id,
                service_type=data.service_type,
                is_active=data.is_active,
            )
            service = await self.repo.add_service(service)
        return PartnerServiceResponse.model_validate(service)

    async def list_partners(
        self,
        status: Optional[str] = None,
        city_id: Optional[int] = None,
        partner_type: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        skip = (page - 1) * per_page
        partners = await self.repo.list_partners(
            status=status,
            city_id=city_id,
            partner_type=partner_type,
            skip=skip,
            limit=per_page,
        )
        total = await self.repo.count_partners(status=status, city_id=city_id)
        return {
            "items": [PartnerListResponse.model_validate(p) for p in partners],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }
