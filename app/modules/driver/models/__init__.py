# ============================================================
# WAY TERO — DRIVER MODELS
# File: app/modules/driver/models/__init__.py
# Doc Ref: DB Schema Part 3 — Driver (Sections 2-7, 22)
# Phase: 2 — Driver Module
# ============================================================

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import (
    Column,
    String,
    DateTime,
    Date,
    BigInteger,
    Numeric,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class Driver(Base):
    """
    Driver profile — attached to a Partner.
    Doc Ref: DB Schema Part 3, Section 2
    Status flow: PENDING → UNDER_REVIEW → APPROVED → ACTIVE → INACTIVE → SUSPENDED
    """

    __tablename__ = "drivers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=False)
    driver_code = Column(String(50), unique=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    mobile = Column(String(15), unique=True, nullable=False)
    email = Column(String(255), nullable=True)
    license_number = Column(String(100), nullable=False)
    license_expiry_date = Column(Date, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    joining_date = Column(Date, nullable=True)
    status = Column(String(50), nullable=False, default="PENDING")
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

    documents = relationship(
        "DriverDocument", back_populates="driver", cascade="all, delete-orphan"
    )
    availability = relationship(
        "DriverAvailability",
        back_populates="driver",
        uselist=False,
        cascade="all, delete-orphan",
    )
    performance = relationship(
        "DriverPerformanceSummary",
        back_populates="driver",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_driver_partner", "partner_id"),
        Index("idx_driver_status", "status"),
        Index("idx_driver_mobile", "mobile"),
    )

    def __repr__(self):
        return f"<Driver id={self.id} code={self.driver_code} status={self.status}>"


class DriverDocument(Base):
    """Driver KYC documents. Doc Ref: Section 4"""

    __tablename__ = "driver_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    driver_id = Column(
        BigInteger, ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    document_type = Column(String(100), nullable=False)
    file_url = Column(Text, nullable=False)
    file_hash = Column(String(255), nullable=True)
    verification_status = Column(String(50), default="PENDING")
    expiry_date = Column(Date, nullable=True)
    remarks = Column(Text, nullable=True)
    uploaded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(UUID(as_uuid=True), nullable=True)

    driver = relationship("Driver", back_populates="documents")

    __table_args__ = (Index("idx_driver_doc_driver", "driver_id"),)


class DriverAvailability(Base):
    """Real-time availability state. Doc Ref: Section 6"""

    __tablename__ = "driver_availability"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    driver_id = Column(
        BigInteger,
        ForeignKey("drivers.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    availability_status = Column(
        String(50), default="OFFLINE"
    )  # ONLINE / OFFLINE / ON_TRIP / BREAK
    last_online_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    driver = relationship("Driver", back_populates="availability")


class DriverPerformanceSummary(Base):
    """Cached performance metrics. Doc Ref: Section 22"""

    __tablename__ = "driver_performance_summary"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    driver_id = Column(
        BigInteger, ForeignKey("drivers.id"), unique=True, nullable=False
    )
    completed_trips = Column(BigInteger, default=0)
    cancelled_trips = Column(BigInteger, default=0)
    acceptance_rate = Column(Numeric(5, 2), default=Decimal("0.00"))
    average_rating = Column(Numeric(3, 2), default=Decimal("0.00"))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    driver = relationship("Driver", back_populates="performance")
