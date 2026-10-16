# ============================================================
# WAYTERO — CAB ROUTE (ADMIN-MANAGED INTERCITY CORRIDOR) MODELS
# File: app/modules/admin/cab_route_models.py
# Migration: 0079_cab_routes
# Tables: cab_routes, cab_route_vehicle_categories
# ============================================================

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    BigInteger,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


DISTANCE_SOURCES = {"GOOGLE", "HAVERSINE", "MANUAL"}


class CabRoute(Base):
    """An inter-city corridor: endpoints + a distance, never a price.

    `pricing_city_id` is resolved when the route is saved, so a public request
    reads one city's fare rules instead of probing three candidates and can
    report the rule source honestly.
    """

    __tablename__ = "cab_routes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slug = Column(String(160), unique=True, nullable=False)
    origin_label = Column(String(200), nullable=False)
    destination_label = Column(String(200), nullable=False)
    origin_city_id = Column(
        BigInteger, ForeignKey("cities.id", ondelete="SET NULL"), nullable=True
    )
    destination_city_id = Column(
        BigInteger, ForeignKey("cities.id", ondelete="SET NULL"), nullable=True
    )
    pricing_city_id = Column(
        BigInteger, ForeignKey("cities.id", ondelete="RESTRICT"), nullable=False
    )
    distance_km = Column(Numeric(12, 2), nullable=False)
    # GOOGLE (Distance Matrix) | HAVERSINE (fallback estimate) | MANUAL (admin typed)
    distance_source = Column(String(10), nullable=False, default="MANUAL")

    description = Column(Text, nullable=False, default="")
    terms_and_conditions = Column(Text, nullable=False, default="")
    image_url = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=False)
    sort_order = Column(SmallInteger, nullable=False, default=0)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    vehicle_links = relationship(
        "CabRouteVehicleCategory",
        back_populates="route",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index(
            "idx_cab_routes_public",
            "sort_order",
            "id",
            postgresql_where="is_active",
        ),
        Index("idx_cab_routes_pricing_city", "pricing_city_id"),
    )

    def __repr__(self):
        return f"<CabRoute {self.slug} {self.origin_label}->{self.destination_label}>"


class CabRouteVehicleCategory(Base):
    """A vehicle category the admin offers on this route — the only vehicles a
    customer sees on the route page."""

    __tablename__ = "cab_route_vehicle_categories"

    route_id = Column(
        BigInteger, ForeignKey("cab_routes.id", ondelete="CASCADE"), primary_key=True
    )
    vehicle_category_id = Column(
        BigInteger,
        ForeignKey("vehicle_categories.id", ondelete="CASCADE"),
        primary_key=True,
    )

    route = relationship("CabRoute", back_populates="vehicle_links")

    def __repr__(self):
        return f"<CabRouteVehicleCategory {self.route_id}/{self.vehicle_category_id}>"
