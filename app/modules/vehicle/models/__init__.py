# ============================================================
# WAY TERO — VEHICLE MODELS
# File: app/modules/vehicle/models/__init__.py
# Doc Ref: DB Schema Part 3 — Vehicle (Sections 10-20, 23)
# Phase: 2 — Vehicle Module
# ============================================================

import uuid
from datetime import datetime, timezone
from decimal import Decimal
import sqlalchemy as sa
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


class VehicleCategory(Base):
    """Master vehicle types (HATCHBACK, SEDAN, SUV…). Doc Ref: Section 10
    Migration 0018 adds: image_url, icon_url, seo_title, seo_description, seo_keywords, display_order
    """

    __tablename__ = "vehicle_categories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    category_name = Column(String(100), unique=True, nullable=False)
    seating_capacity = Column(Integer, nullable=True)
    luggage_capacity = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Media (Cloudinary URLs) — added in migration 0018
    image_url = Column(sa.Text, nullable=True)  # hero/listing image for website
    icon_url = Column(sa.Text, nullable=True)  # small icon for cards/filters

    # SEO metadata for website category pages — added in migration 0018
    seo_title = Column(String(120), nullable=True)
    seo_description = Column(String(320), nullable=True)
    seo_keywords = Column(String(500), nullable=True)
    display_order = Column(sa.Integer, nullable=False, default=0)

    vehicles = relationship("Vehicle", back_populates="category")
    pricing_rules = relationship("VehiclePricingRule", back_populates="category")

    def __repr__(self):
        return f"<VehicleCategory {self.category_name}>"


class Vehicle(Base):
    """
    Partner vehicle entity.
    Doc Ref: DB Schema Part 3, Section 12
    Status flow: PENDING → UNDER_REVIEW → APPROVED → ACTIVE → ON_TRIP → MAINTENANCE → SUSPENDED
    """

    __tablename__ = "vehicles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=False)
    vehicle_category_id = Column(
        BigInteger, ForeignKey("vehicle_categories.id"), nullable=False
    )
    vehicle_code = Column(String(50), unique=True, nullable=True)
    registration_number = Column(String(50), unique=True, nullable=False)
    vehicle_brand = Column(String(100), nullable=True)
    vehicle_model = Column(String(100), nullable=True)
    manufacturing_year = Column(Integer, nullable=True)
    fuel_type = Column(String(50), nullable=True)
    seating_capacity = Column(Integer, nullable=True)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=False)
    status = Column(String(50), nullable=False, default="PENDING")
    approved_at = Column(DateTime(timezone=True), nullable=True)
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

    category = relationship("VehicleCategory", back_populates="vehicles")
    documents = relationship(
        "VehicleDocument", back_populates="vehicle", cascade="all, delete-orphan"
    )
    photos = relationship(
        "VehiclePhotoUpload",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="VehiclePhotoUpload.uploaded_at",
    )
    availability = relationship(
        "VehicleAvailability",
        back_populates="vehicle",
        uselist=False,
        cascade="all, delete-orphan",
    )
    performance = relationship(
        "VehiclePerformanceSummary",
        back_populates="vehicle",
        uselist=False,
        cascade="all, delete-orphan",
    )
    maintenance_records = relationship(
        "VehicleMaintenance",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="VehicleMaintenance.service_date.desc()",
    )

    __table_args__ = (
        Index("idx_vehicle_partner", "partner_id"),
        Index("idx_vehicle_city", "city_id"),
        Index("idx_vehicle_status", "status"),
        Index("idx_vehicle_category", "vehicle_category_id"),
    )

    def __repr__(self):
        return f"<Vehicle id={self.id} reg={self.registration_number} status={self.status}>"


class VehicleDocument(Base):
    """Vehicle RC, Insurance, Fitness etc. Doc Ref: Section 14"""

    __tablename__ = "vehicle_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = Column(
        BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False
    )
    document_type = Column(String(100), nullable=True)
    file_url = Column(Text, nullable=True)
    expiry_date = Column(Date, nullable=True)
    verification_status = Column(String(50), default="PENDING")
    uploaded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)

    vehicle = relationship("Vehicle", back_populates="documents")

    __table_args__ = (Index("idx_vehicle_doc_vehicle", "vehicle_id"),)


class VehicleAvailability(Base):
    """Current availability state. Doc Ref: Section 17"""

    __tablename__ = "vehicle_availability"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = Column(
        BigInteger,
        ForeignKey("vehicles.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    availability_status = Column(String(50), default="UNAVAILABLE")
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    vehicle = relationship("Vehicle", back_populates="availability")


class VehiclePricingRule(Base):
    """City + category based fare pricing. Doc Ref: Section 18

    Added migration 0049:
      - driver_allowance_type: PER_TRIP / PER_DAY / PER_KM / NONE
        (PER_TRIP preserves legacy flat behaviour; PER_DAY scales by trip
        days computed from cab_bookings.pickup_datetime → return_datetime.)
      - night_charge_type: FIXED / PERCENTAGE / PER_KM
      - toll: flat toll amount
      - free_waiting_minutes: free waiting period in minutes
      - actual_waiting_minutes: recorded actual waiting minutes
      - waiting_rate_per_hour: rate for waiting charge per hour
      - waiting_granularity: PER_MINUTE / PER_15_MINUTES / PER_30_MINUTES / PER_HOUR
    """

    __tablename__ = "vehicle_pricing_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=False)
    vehicle_category_id = Column(
        BigInteger, ForeignKey("vehicle_categories.id"), nullable=False
    )
    trip_type = Column(
        String(50), nullable=True
    )  # LOCAL / AIRPORT / OUTSTATION / ONE_WAY / ROUND_TRIP
    base_fare = Column(Numeric(12, 2), nullable=True)
    minimum_km = Column(Integer, nullable=True)
    per_km_rate = Column(Numeric(12, 2), nullable=True)
    driver_allowance = Column(Numeric(12, 2), nullable=True)
    night_charge = Column(Numeric(12, 2), nullable=True)
    # Migration 0049: PER_TRIP default keeps legacy flat behaviour.
    driver_allowance_type = Column(
        String(16), nullable=False, default="PER_TRIP", server_default="PER_TRIP"
    )
    # Night charge type: FIXED / PERCENTAGE / PER_KM
    night_charge_type = Column(
        String(16), nullable=False, default="FIXED", server_default="FIXED"
    )
    # Toll amount (flat)
    toll = Column(Numeric(12, 2), nullable=True, default=0)
    # Waiting charge fields
    free_waiting_minutes = Column(Integer, nullable=True, default=0)
    actual_waiting_minutes = Column(Integer, nullable=True, default=0)
    waiting_rate_per_hour = Column(Numeric(12, 2), nullable=True, default=0)
    waiting_granularity = Column(
        String(16),
        nullable=False,
        default="PER_15_MINUTES",
        server_default="PER_15_MINUTES",
    )
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)

    category = relationship("VehicleCategory", back_populates="pricing_rules")

    __table_args__ = (Index("idx_pricing_city_cat", "city_id", "vehicle_category_id"),)


class VehiclePerformanceSummary(Base):
    """Cached performance metrics. Doc Ref: Section 23"""

    __tablename__ = "vehicle_performance_summary"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = Column(
        BigInteger, ForeignKey("vehicles.id"), unique=True, nullable=False
    )
    total_trips = Column(Integer, default=0)
    total_distance = Column(Numeric(12, 2), default=Decimal("0.00"))
    average_rating = Column(Numeric(3, 2), default=Decimal("0.00"))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    vehicle = relationship("Vehicle", back_populates="performance")


class DefaultVehiclePricingRule(Base):
    """
    Platform-level default pricing used when no city-specific rule exists.
    Doc Ref: DB Schema Part 3 §18 — vehicle_pricing_rules (fallback layer)
    BRD Part 3 §35 — Fare Engine: pricing must never be hardcoded.

    Added migration 0049:
      - driver_allowance_type: PER_TRIP / PER_DAY / PER_KM / NONE
      - night_charge_type: FIXED / PERCENTAGE / PER_KM
      - toll: flat toll amount
      - free_waiting_minutes / actual_waiting_minutes / waiting_rate_per_hour / waiting_granularity
    """

    __tablename__ = "default_vehicle_pricing_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_category_id = Column(
        BigInteger, ForeignKey("vehicle_categories.id"), nullable=False
    )
    trip_type = Column(String(50), nullable=False)
    base_fare = Column(Numeric(12, 2), nullable=False, default=0)
    minimum_km = Column(Integer, nullable=False, default=0)
    per_km_rate = Column(Numeric(12, 2), nullable=False, default=0)
    driver_allowance = Column(Numeric(12, 2), nullable=False, default=0)
    night_charge = Column(Numeric(12, 2), nullable=False, default=0)
    # Migration 0049: PER_TRIP default keeps legacy flat behaviour.
    driver_allowance_type = Column(
        String(16), nullable=False, default="PER_TRIP", server_default="PER_TRIP"
    )
    # Night charge type: FIXED / PERCENTAGE / PER_KM
    night_charge_type = Column(
        String(16), nullable=False, default="FIXED", server_default="FIXED"
    )
    # Toll amount (flat)
    toll = Column(Numeric(12, 2), nullable=False, default=0)
    # Waiting charge fields
    free_waiting_minutes = Column(Integer, nullable=False, default=0)
    actual_waiting_minutes = Column(Integer, nullable=False, default=0)
    waiting_rate_per_hour = Column(Numeric(12, 2), nullable=False, default=0)
    waiting_granularity = Column(
        String(16),
        nullable=False,
        default="PER_15_MINUTES",
        server_default="PER_15_MINUTES",
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        onupdate=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        nullable=False,
    )

    category = relationship("VehicleCategory", foreign_keys=[vehicle_category_id])

    __table_args__ = (
        Index("idx_def_pricing_cat_trip", "vehicle_category_id", "trip_type"),
    )

    def __repr__(self):
        return f"<DefaultVehiclePricingRule cat={self.vehicle_category_id} type={self.trip_type}>"


class VehicleVerificationAssignment(Base):
    """Assignment of a verification officer to a vehicle. Doc Ref: Migration 0017"""

    __tablename__ = "vehicle_verification_assignments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = Column(
        BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False
    )
    officer_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    unassigned_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        Index("idx_vva_vehicle", "vehicle_id"),
        Index("idx_vva_officer", "officer_id"),
    )


class VehiclePhotoUpload(Base):
    """Categorized vehicle photos for verification. Doc Ref: Migration 0017"""

    __tablename__ = "vehicle_photo_uploads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = Column(
        BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False
    )
    photo_type = Column(
        String(50), nullable=False
    )  # FRONT|BACK|LEFT|RIGHT|INTERIOR|ODOMETER|ENGINE|OTHER
    file_url = Column(Text, nullable=False)
    caption = Column(String(255), nullable=True)
    verification_status = Column(String(50), default="PENDING", nullable=False)
    uploaded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    remarks = Column(Text, nullable=True)

    vehicle = relationship("Vehicle", back_populates="photos")
    __table_args__ = (Index("idx_vpu_vehicle", "vehicle_id"),)


class VehicleMaintenance(Base):
    """Service / repair log per vehicle. Doc Ref: BRD Part 3 �133-138, DB Schema Part 3 �20

    The table is created by migration 0006. Service_date + next_due_date
    let the ops console show "due in 12 days" badges. Cost is nullable so
    warranty work / manufacturer recalls can be logged without a rupee tag.
    """

    __tablename__ = "vehicle_maintenance"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = Column(
        BigInteger, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False
    )
    maintenance_type = Column(String(100), nullable=True)
    service_date = Column(Date, nullable=True)
    next_due_date = Column(Date, nullable=True)
    cost = Column(Numeric(12, 2), nullable=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    vehicle = relationship("Vehicle", back_populates="maintenance_records")
    __table_args__ = (Index("idx_vehicle_maintenance_vehicle", "vehicle_id"),)
