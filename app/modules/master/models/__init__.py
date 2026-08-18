# ============================================================
# WAYTERO — MASTER / REFERENCE DATA MODELS
# File: app/modules/master/models/__init__.py
# Doc Ref:
#   DB Schema Part 1, Section 9  → countries
#   DB Schema Part 1, Section 10 → states
#   DB Schema Part 1, Section 11 → cities
#
# These three tables are created by migration 0003_phase2_master_data.
# The ORM models here exist purely so SQLAlchemy can resolve
# ForeignKey("cities.id"), ForeignKey("states.id"), etc. across all
# other modules — without them the ORM throws NoReferencedTableError
# on any write that touches city_id.
# ============================================================

import sqlalchemy as sa
from sqlalchemy import Column, BigInteger, String, Boolean, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.core.database import Base


class Country(Base):
    """
    ISO country reference table.
    Doc Ref: DB Schema Part 1, Section 9
    """

    __tablename__ = "countries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(150), nullable=False)
    iso_code = Column(String(10))
    phone_code = Column(String(10))
    is_active = Column(Boolean, default=True)

    states = relationship("State", back_populates="country")


class State(Base):
    """
    State / province reference table.
    Doc Ref: DB Schema Part 1, Section 10
    """

    __tablename__ = "states"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    country_id = Column(BigInteger, ForeignKey("countries.id"), nullable=False)
    name = Column(String(150), nullable=False)
    state_code = Column(String(20))
    is_active = Column(Boolean, default=True)

    country = relationship("Country", back_populates="states")
    cities = relationship("City", back_populates="state")


class City(Base):
    """
    City reference table.
    Doc Ref: DB Schema Part 1, Section 11
    Used as FK target by: commission_rules, vehicles, vehicle_pricing_rules,
    partners, customers, bookings.

    `latitude` / `longitude` (migration 0046) are the city-centre coordinates
    used to bias Google Places autocomplete in the customer-web hero search
    form. Nullable because they're optional at the DB level — admin sets
    them via Settings → Master Data → Cities → "Find coordinates" button.
    """

    __tablename__ = "cities"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    state_id = Column(BigInteger, ForeignKey("states.id"), nullable=False)
    name = Column(String(150), nullable=False)
    city_code = Column(String(50))
    is_active = Column(Boolean, default=True)
    latitude = Column(sa.Numeric(10, 7), nullable=True)
    longitude = Column(sa.Numeric(10, 7), nullable=True)

    state = relationship("State", back_populates="cities")

    __table_args__ = (Index("idx_cities_state", "state_id"),)


class ServiceType(Base):
    """
    Dynamic service type master table.
    Doc Ref: DB Schema Part 2 §8 — CAB | HOTEL | TOUR
    Migration 0019 — service_types
    type_code is the immutable business key; id is surrogate PK.
    """

    __tablename__ = "service_types"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    type_code = Column(String(50), unique=True, nullable=False)
    label = Column(String(150), nullable=False)
    description = Column(sa.Text, nullable=True)
    icon_url = Column(sa.Text, nullable=True)
    image_url = Column(sa.Text, nullable=True)
    seo_title = Column(String(120), nullable=True)
    seo_description = Column(String(320), nullable=True)
    seo_keywords = Column(String(500), nullable=True)
    display_order = Column(sa.Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )

    __table_args__ = (Index("idx_service_types_code", "type_code"),)

    def __repr__(self):
        return f"<ServiceType {self.type_code}>"
