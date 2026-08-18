"""Tour package catalog and booking models.

The tour module deliberately keeps the package catalog separate from the hotel
and cab inventories.  A package can reference those services in its itinerary
and component metadata without taking ownership of their operational records.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class TourPackage(Base):
    __tablename__ = "tour_packages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=False)
    package_code = Column(String(50), unique=True, nullable=False)
    slug = Column(String(255), unique=True, nullable=False)
    package_name = Column(String(255), nullable=False)
    package_type = Column(String(50), nullable=False, default="FIXED")
    destination = Column(String(255), nullable=False)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=False)
    duration_days = Column(Integer, nullable=False)
    duration_nights = Column(Integer, nullable=False)
    minimum_persons = Column(Integer, nullable=False, default=1)
    maximum_persons = Column(Integer, nullable=True)
    short_description = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    terms_and_conditions = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default="DRAFT")
    rejection_reason = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("idx_tour_packages_status", "status"),
        Index("idx_tour_packages_partner", "partner_id"),
        Index("idx_tour_packages_city", "city_id"),
    )


class TourItinerary(Base):
    __tablename__ = "tour_itineraries"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    package_id = Column(
        BigInteger, ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False
    )
    day_number = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    overnight_city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=True)
    activities = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    __table_args__ = (
        UniqueConstraint("package_id", "day_number", name="uq_tour_itinerary_day"),
    )


class TourPackageInclusion(Base):
    __tablename__ = "tour_package_inclusions"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    package_id = Column(
        BigInteger, ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False
    )
    inclusion_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TourPackageExclusion(Base):
    __tablename__ = "tour_package_exclusions"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    package_id = Column(
        BigInteger, ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False
    )
    exclusion_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TourPackageMedia(Base):
    __tablename__ = "tour_package_media"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    package_id = Column(
        BigInteger, ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False
    )
    media_type = Column(String(20), nullable=False, default="IMAGE")
    media_url = Column(Text, nullable=False)
    caption = Column(String(255), nullable=True)
    display_order = Column(Integer, nullable=False, default=0)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TourPackagePricing(Base):
    __tablename__ = "tour_package_pricing"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    package_id = Column(
        BigInteger, ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False
    )
    persons_count = Column(Integer, nullable=False)
    package_price = Column(Numeric(12, 2), nullable=False)
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    __table_args__ = (
        UniqueConstraint("package_id", "persons_count", name="uq_tour_pricing_pax"),
    )


class TourBooking(Base):
    __tablename__ = "tour_bookings"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    package_id = Column(BigInteger, ForeignKey("tour_packages.id"), nullable=False)
    booking_number = Column(String(100), unique=True, nullable=False)
    travel_start_date = Column(Date, nullable=False)
    travel_end_date = Column(Date, nullable=False)
    persons_count = Column(Integer, nullable=False)
    total_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    platform_commission = Column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    partner_payout = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    payment_status = Column(String(30), nullable=False, default="PENDING")
    booking_status = Column(String(50), nullable=False, default="PENDING_CONFIRMATION")
    special_requests = Column(Text, nullable=True)

    # ── Tour booking management (migration 0057) ────────────
    # Trip execution details — filled step by step by admin / partner.
    pickup_location = Column(Text, nullable=True)
    pickup_datetime = Column(DateTime(timezone=True), nullable=True)
    vehicle_id = Column(
        BigInteger, ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    driver_id = Column(
        BigInteger, ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True
    )
    hotel_details = Column(Text, nullable=True)
    other_details = Column(Text, nullable=True)
    itinerary_snapshot = Column(JSONB, nullable=True)
    # Money: additional charges from trip modifications + advance custody.
    additional_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    additional_charge_note = Column(Text, nullable=True)
    advance_total = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    advance_received_by = Column(String(20), nullable=True)  # ADMIN | PARTNER | DRIVER
    invoice_number = Column(String(50), nullable=True)
    invoiced_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    __table_args__ = (
        Index("idx_tour_bookings_status", "booking_status"),
        Index("idx_tour_bookings_travel_date", "travel_start_date"),
        Index("idx_tour_bookings_vehicle", "vehicle_id"),
        Index("idx_tour_bookings_driver", "driver_id"),
    )


class TourParticipant(Base):
    __tablename__ = "tour_participants"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tour_booking_id = Column(
        BigInteger, ForeignKey("tour_bookings.id", ondelete="CASCADE"), nullable=False
    )
    participant_name = Column(String(255), nullable=False)
    mobile = Column(String(15), nullable=True)
    age = Column(Integer, nullable=True)
    gender = Column(String(20), nullable=True)
    id_type = Column(String(50), nullable=True)
    id_number = Column(String(100), nullable=True)


class TourAdvancePayment(Base):
    """
    Money collected from the customer against a tour booking before final
    billing. Mirrors HotelAdvancePayment — a tour takes advances repeatedly
    (deposit at booking, top-ups, balance on completion), so there is
    deliberately no single-active index here.

    received_by is the custody signal that drives settlement direction:
    ADMIN means the platform holds the money, PARTNER/DRIVER means the
    partner side does. Voiding flips status rather than deleting, to
    preserve the audit trail.
    Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
    Migration: 0057_tour_booking_manage
    """

    __tablename__ = "tour_advance_payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tour_booking_id = Column(
        BigInteger, ForeignKey("tour_bookings.id", ondelete="CASCADE"), nullable=False
    )
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id", ondelete="CASCADE"), nullable=False
    )
    receipt_number = Column(String(50), unique=True, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    payment_mode = Column(String(20), nullable=False)  # CASH | ONLINE | UPI | WALLET
    received_by = Column(
        String(20), nullable=False, default="ADMIN"
    )  # ADMIN | PARTNER | DRIVER
    reference_number = Column(String(120), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")  # ACTIVE | VOIDED
    refunded_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    collected_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    collected_by_role = Column(
        String(20), nullable=False, default="ADMIN"
    )  # ADMIN | PARTNER
    collected_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    voided_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    void_reason = Column(Text, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self):
        return (
            f"<TourAdvancePayment {self.receipt_number} "
            f"{self.amount} {self.received_by} {self.status}>"
        )


class TourBookingCharge(Base):
    """
    Additional charge on a tour booking from a trip modification — extra
    days, extra stops, late changes. Added by admin or partner while the
    trip is being managed; rolls into the booking total and the final
    invoice, and is split with the partner at settlement.
    Migration: 0057_tour_booking_manage
    """

    __tablename__ = "tour_booking_charges"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tour_booking_id = Column(
        BigInteger, ForeignKey("tour_bookings.id", ondelete="CASCADE"), nullable=False
    )
    label = Column(String(255), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    reason = Column(Text, nullable=True)
    added_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    added_by_role = Column(
        String(20), nullable=False, default="ADMIN"
    )  # ADMIN | PARTNER
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self):
        return f"<TourBookingCharge {self.label} {self.amount}>"


__all__ = [
    "TourPackage",
    "TourItinerary",
    "TourPackageInclusion",
    "TourPackageExclusion",
    "TourPackageMedia",
    "TourPackagePricing",
    "TourBooking",
    "TourParticipant",
    "TourAdvancePayment",
    "TourBookingCharge",
]
