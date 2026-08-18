# ============================================================
# WAY TERO — PARTNER MODELS
# File: app/modules/partner/models/__init__.py
# Doc Ref: DB Schema Part 2 — Partner (Sections 4-19)
# Phase: 2 — Partner Module
# ============================================================

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    Date,
    Integer,
    BigInteger,
    Numeric,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.modules.master.models import Country, State, City  # noqa: F401


class Partner(Base):
    """
    Partner entity — cab/hotel/tour service providers.
    Doc Ref: DB Schema Part 2, Section 4
    Status flow: PENDING → UNDER_REVIEW → DOCUMENT_PENDING → APPROVED → ACTIVE → SUSPENDED → BLOCKED
    """

    __tablename__ = "partners"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    partner_code = Column(String(50), unique=True, nullable=False)
    partner_type = Column(String(50), nullable=False)  # INDIVIDUAL / COMPANY
    business_name = Column(String(255), nullable=True)
    owner_name = Column(String(255), nullable=False)
    mobile = Column(String(15), nullable=False)
    email = Column(String(255), nullable=True)
    logo_url = Column(Text, nullable=True)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=False)

    # Office / business address — Doc Ref: BRD Part 2 §20, Migration 0016
    # office_city_id MUST match city_id — enforced at application layer
    office_address_line_1 = Column(String(255), nullable=True)
    office_address_line_2 = Column(String(255), nullable=True)
    office_city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=True)
    office_state_id = Column(BigInteger, ForeignKey("states.id"), nullable=True)
    office_postal_code = Column(String(20), nullable=True)

    status = Column(String(50), nullable=False, default="PENDING")
    onboarding_source = Column(String(100), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    services = relationship(
        "PartnerService", back_populates="partner", cascade="all, delete-orphan"
    )
    documents = relationship(
        "PartnerDocument", back_populates="partner", cascade="all, delete-orphan"
    )
    gst_details = relationship(
        "PartnerGSTDetails",
        back_populates="partner",
        uselist=False,
        cascade="all, delete-orphan",
    )
    bank_accounts = relationship(
        "PartnerBankAccount", back_populates="partner", cascade="all, delete-orphan"
    )
    verification_logs = relationship(
        "PartnerVerificationLog", back_populates="partner", cascade="all, delete-orphan"
    )
    ratings = relationship(
        "PartnerRating",
        back_populates="partner",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_partner_city", "city_id"),
        Index("idx_partner_status", "status"),
        Index("idx_partner_type", "partner_type"),
    )

    def __repr__(self):
        return f"<Partner id={self.id} code={self.partner_code} status={self.status}>"


class PartnerService(Base):
    """Which services the partner provides: CAB, HOTEL, TOUR. Doc Ref: Section 7"""

    __tablename__ = "partner_services"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    partner_id = Column(
        BigInteger, ForeignKey("partners.id", ondelete="CASCADE"), nullable=False
    )
    service_type = Column(String(50), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    partner = relationship("Partner", back_populates="services")

    __table_args__ = (Index("idx_partner_service", "service_type"),)


class PartnerDocument(Base):
    """Uploaded KYC documents. Doc Ref: Section 9"""

    __tablename__ = "partner_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    partner_id = Column(
        BigInteger, ForeignKey("partners.id", ondelete="CASCADE"), nullable=False
    )
    document_type = Column(String(100), nullable=False)
    document_number = Column(String(100), nullable=True)
    file_url = Column(Text, nullable=False)
    file_hash = Column(String(255), nullable=True)
    verification_status = Column(String(50), default="PENDING")
    remarks = Column(Text, nullable=True)
    uploaded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(UUID(as_uuid=True), nullable=True)
    expiry_date = Column(Date, nullable=True)

    partner = relationship("Partner", back_populates="documents")

    __table_args__ = (
        Index("idx_partner_doc_status", "verification_status"),
        Index("idx_partner_doc_partner", "partner_id"),
    )


class PartnerGSTDetails(Base):
    """GST info for company partners. Doc Ref: Section 11"""

    __tablename__ = "partner_gst_details"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    partner_id = Column(
        BigInteger,
        ForeignKey("partners.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    gst_number = Column(String(20), unique=True, nullable=True)
    pan_number = Column(String(20), nullable=True)  # BRD §18: PAN always required
    legal_name = Column(String(255), nullable=True)
    trade_name = Column(String(255), nullable=True)
    registration_date = Column(Date, nullable=True)
    gst_status = Column(String(50), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    partner = relationship("Partner", back_populates="gst_details")


class PartnerBankAccount(Base):
    """Bank accounts for settlement. Doc Ref: Section 12"""

    __tablename__ = "partner_bank_accounts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    partner_id = Column(
        BigInteger, ForeignKey("partners.id", ondelete="CASCADE"), nullable=False
    )
    account_holder_name = Column(String(255), nullable=True)
    account_number_encrypted = Column(Text, nullable=True)
    ifsc_code = Column(String(20), nullable=True)
    bank_name = Column(String(255), nullable=True)
    branch_name = Column(String(255), nullable=True)
    is_primary = Column(Boolean, default=False)
    account_type = Column(String(20), default="SAVINGS")
    verification_status = Column(String(50), default="PENDING")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    partner = relationship("Partner", back_populates="bank_accounts")

    __table_args__ = (Index("idx_partner_bank", "partner_id"),)


class PartnerVerificationLog(Base):
    """Onboarding activity log. Doc Ref: Section 13"""

    __tablename__ = "partner_verification_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    partner_id = Column(
        BigInteger, ForeignKey("partners.id", ondelete="CASCADE"), nullable=False
    )
    action = Column(String(100), nullable=True)
    remarks = Column(Text, nullable=True)
    performed_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    partner = relationship("Partner", back_populates="verification_logs")


class PartnerRating(Base):
    """Aggregated partner rating. Doc Ref: Section 18"""

    __tablename__ = "partner_ratings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    partner_id = Column(
        BigInteger, ForeignKey("partners.id"), unique=True, nullable=False
    )
    average_rating = Column(Numeric(3, 2), default=Decimal("0.00"))
    total_reviews = Column(Integer, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    partner = relationship("Partner", back_populates="ratings")
