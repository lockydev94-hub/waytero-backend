# ============================================================
# WAYTERO — COUPON SYSTEM MODELS
# File: app/modules/admin/coupon_models.py
# Migration: 0020_coupon_system
# Tables: coupons, coupon_service_rules, coupon_city_rules,
#         coupon_customer_rules, coupon_usages
# ============================================================

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
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.modules.master.models import City  # noqa: F401
from app.modules.customer.models import Customer  # noqa: F401


class Coupon(Base):
    """
    Master coupon definition.
    apply_to: ALL | CAB | HOTEL | TOUR
    discount_type: PERCENTAGE | FLAT
    is_customer_specific: if True, only coupon_customer_rules mobiles can redeem
    is_city_specific: if True, only coupon_city_rules cities are eligible
    """

    __tablename__ = "coupons"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    coupon_code = Column(String(50), unique=True, nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    apply_to = Column(String(50), nullable=False, default="ALL")
    # ALL | CAB | HOTEL | TOUR

    discount_type = Column(String(20), nullable=False)
    # PERCENTAGE | FLAT
    discount_value = Column(Numeric(12, 2), nullable=False)
    max_discount_amount = Column(Numeric(12, 2), nullable=True)
    # Cap in ₹ for PERCENTAGE coupons; also used as hard city-price guard

    min_booking_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))

    max_usage_total = Column(Integer, nullable=True)  # None = unlimited
    max_usage_per_customer = Column(Integer, nullable=False, default=1)
    current_usage_count = Column(Integer, nullable=False, default=0)

    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=False)

    is_customer_specific = Column(Boolean, nullable=False, default=False)
    is_city_specific = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    service_rules = relationship(
        "CouponServiceRule", back_populates="coupon", cascade="all, delete-orphan"
    )
    city_rules = relationship(
        "CouponCityRule", back_populates="coupon", cascade="all, delete-orphan"
    )
    customer_rules = relationship(
        "CouponCustomerRule", back_populates="coupon", cascade="all, delete-orphan"
    )
    usages = relationship("CouponUsage", back_populates="coupon")

    __table_args__ = (
        Index("idx_coupon_code", "coupon_code"),
        Index("idx_coupon_active", "is_active"),
        Index("idx_coupon_validity", "valid_from", "valid_to"),
    )

    def __repr__(self):
        return f"<Coupon {self.coupon_code} type={self.discount_type}>"


class CouponServiceRule(Base):
    """
    Restricts coupon to specific service_type and optionally vehicle_category.
    If no rows exist here, the coupon applies to all services.
    If rows exist, only matching service_type (+ vehicle_category_id) is eligible.
    """

    __tablename__ = "coupon_service_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id = Column(
        BigInteger, ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    service_type = Column(String(50), nullable=False)  # CAB | HOTEL | TOUR
    vehicle_category_id = Column(
        BigInteger,
        ForeignKey("vehicle_categories.id", ondelete="CASCADE"),
        nullable=True,
    )
    # NULL = all vehicle categories for this service_type

    coupon = relationship("Coupon", back_populates="service_rules")

    __table_args__ = (Index("idx_csr_coupon", "coupon_id"),)


class CouponCityRule(Base):
    """
    City-level eligibility gate.
    max_discount_override: if set, discount is additionally capped at this value
    (typically set to city base_fare from vehicle_pricing_rules).
    """

    __tablename__ = "coupon_city_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id = Column(
        BigInteger, ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    city_id = Column(
        BigInteger, ForeignKey("cities.id", ondelete="CASCADE"), nullable=False
    )
    max_discount_override = Column(Numeric(12, 2), nullable=True)
    # If not None, discount cannot exceed this amount for this city

    coupon = relationship("Coupon", back_populates="city_rules")

    __table_args__ = (
        Index("idx_ccr_coupon", "coupon_id"),
        Index("idx_ccr_city", "city_id"),
    )


class CouponCustomerRule(Base):
    """
    Whitelist of mobile numbers allowed to use a customer-specific coupon.
    customer_id resolved at first redemption.
    """

    __tablename__ = "coupon_customer_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id = Column(
        BigInteger, ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False
    )
    mobile_number = Column(String(15), nullable=False)
    customer_id = Column(
        BigInteger, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )

    coupon = relationship("Coupon", back_populates="customer_rules")

    __table_args__ = (
        Index("idx_ccust_coupon", "coupon_id"),
        Index("idx_ccust_mobile", "mobile_number"),
    )


class CouponUsage(Base):
    """Immutable redemption audit trail."""

    __tablename__ = "coupon_usages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id = Column(
        BigInteger, ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id = Column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id", ondelete="RESTRICT"), nullable=True
    )
    discount_applied = Column(Numeric(12, 2), nullable=False)
    used_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    coupon = relationship("Coupon", back_populates="usages")

    __table_args__ = (
        Index("idx_cu_coupon", "coupon_id"),
        Index("idx_cu_customer", "customer_id"),
    )
