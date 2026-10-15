# ============================================================
# WAYTERO — OFFER (COUPON-BACKED MARKETING) MODELS
# File: app/modules/admin/offer_models.py
# Migration: 0077_offers
# Tables: offers, offer_hotel_links, offer_hotel_room_types, offer_tour_packages
# ============================================================

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    BigInteger,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Offer(Base):
    """A public marketing card backed by exactly one coupon.

    CAB offers scope via vehicle_category_id; HOTEL/TOUR offers scope via
    link tables (multiple hotels with per-hotel room types, or packages).
    Display requires is_published + window + the coupon still validating.
    """

    __tablename__ = "offers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slug = Column(String(160), unique=True, nullable=False)
    coupon_id = Column(
        BigInteger, ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=False
    )
    service_type = Column(String(20), nullable=False)  # CAB | HOTEL | TOUR
    vehicle_category_id = Column(
        BigInteger,
        ForeignKey("vehicle_categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    title = Column(String(255), nullable=False)
    short_description = Column(Text, nullable=True)
    terms_and_conditions = Column(Text, nullable=False, default="")
    image_url = Column(Text, nullable=False)
    badge_label = Column(String(60), nullable=True)

    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=False)
    is_published = Column(Boolean, nullable=False, default=False)
    sort_order = Column(SmallInteger, nullable=False, default=0)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    hotel_links = relationship(
        "OfferHotelLink", back_populates="offer", cascade="all, delete-orphan"
    )
    room_type_rows = relationship(
        "OfferHotelRoomType", back_populates="offer", cascade="all, delete-orphan"
    )
    tour_links = relationship(
        "OfferTourPackage", back_populates="offer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_offers_coupon", "coupon_id"),
        Index(
            "idx_offers_public",
            "sort_order",
            "valid_from",
            postgresql_where="is_published",
        ),
    )

    def __repr__(self):
        return f"<Offer {self.slug} coupon={self.coupon_id} {self.service_type}>"


class OfferHotelLink(Base):
    """A hotel included in an offer, with its own exit-request lifecycle."""

    __tablename__ = "offer_hotel_links"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    offer_id = Column(
        BigInteger, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )
    status = Column(String(20), nullable=False, default="ACTIVE")
    # ACTIVE | EXIT_REQUESTED (partner asked to quit, admin must resolve) | REMOVED
    exit_requested_at = Column(DateTime(timezone=True), nullable=True)
    exit_reason = Column(Text, nullable=True)
    exit_resolved_at = Column(DateTime(timezone=True), nullable=True)
    exit_resolved_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    offer = relationship("Offer", back_populates="hotel_links")

    __table_args__ = (
        UniqueConstraint("offer_id", "hotel_id", name="uq_offer_hotel"),
        Index("idx_ohl_hotel", "hotel_id"),
    )


class OfferHotelRoomType(Base):
    """Room categories eligible for an offer, per offer (owner picks types
    when linking each hotel; partners may adjust but keep at least one)."""

    __tablename__ = "offer_hotel_room_types"

    offer_id = Column(
        BigInteger, ForeignKey("offers.id", ondelete="CASCADE"), primary_key=True
    )
    room_category_id = Column(
        BigInteger,
        ForeignKey("hotel_room_categories.id", ondelete="CASCADE"),
        primary_key=True,
    )

    offer = relationship("Offer", back_populates="room_type_rows")


class OfferTourPackage(Base):
    """A tour package included in an offer (same exit lifecycle as hotels)."""

    __tablename__ = "offer_tour_packages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    offer_id = Column(
        BigInteger, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    tour_package_id = Column(
        BigInteger, ForeignKey("tour_packages.id", ondelete="RESTRICT"), nullable=False
    )
    status = Column(String(20), nullable=False, default="ACTIVE")
    exit_requested_at = Column(DateTime(timezone=True), nullable=True)
    exit_reason = Column(Text, nullable=True)
    exit_resolved_at = Column(DateTime(timezone=True), nullable=True)
    exit_resolved_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    offer = relationship("Offer", back_populates="tour_links")

    __table_args__ = (
        UniqueConstraint("offer_id", "tour_package_id", name="uq_offer_tour"),
        Index("idx_otp_tour", "tour_package_id"),
    )
