# ============================================================
# WAY TERO — CUSTOMER MODELS
# File: app/modules/customer/models/__init__.py
# Doc Ref: DB Schema Part 2 — Customer (Sections 2, 3)
# Phase: 2 — Customer Module
# ============================================================

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    String,
    Boolean,
    DateTime,
    Date,
    BigInteger,
    Numeric,
    ForeignKey,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.modules.master.models import Country, State, City  # noqa: F401


class Customer(Base):
    """
    Customer profile — one record per customer user.
    Linked to users table via user_id.
    Doc Ref: DB Schema Part 2, Section 2
    """

    __tablename__ = "customers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    customer_code = Column(String(50), unique=True, nullable=False)
    first_name = Column(String(150), nullable=True)
    last_name = Column(String(150), nullable=True)
    gender = Column(String(20), nullable=True)
    date_of_birth = Column(Date, nullable=True)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=True)
    referral_code = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
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

    addresses = relationship(
        "CustomerAddress", back_populates="customer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_customers_city", "city_id"),
        Index("idx_customers_active", "is_active"),
        Index("idx_customers_referral", "referral_code"),
    )

    def __repr__(self):
        return f"<Customer id={self.id} code={self.customer_code}>"

    @property
    def full_name(self) -> str:
        parts = [p for p in [self.first_name, self.last_name] if p]
        return " ".join(parts) if parts else ""


class CustomerAddress(Base):
    """
    Customer delivery / pickup addresses.
    Doc Ref: DB Schema Part 2, Section 3
    """

    __tablename__ = "customer_addresses"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    customer_id = Column(
        BigInteger, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    address_type = Column(String(50), nullable=True)  # HOME / WORK / OTHER
    address_line_1 = Column(String(255), nullable=True)
    address_line_2 = Column(String(255), nullable=True)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=True)
    state_id = Column(BigInteger, ForeignKey("states.id"), nullable=True)
    postal_code = Column(String(20), nullable=True)
    latitude = Column(Numeric(10, 7), nullable=True)
    longitude = Column(Numeric(10, 7), nullable=True)
    is_default = Column(Boolean, default=False, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    customer = relationship("Customer", back_populates="addresses")

    __table_args__ = (Index("idx_customer_addr_customer", "customer_id"),)

    def __repr__(self):
        return f"<CustomerAddress id={self.id} customer_id={self.customer_id} type={self.address_type}>"
