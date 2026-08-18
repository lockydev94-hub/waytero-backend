# ============================================================
# WAY TERO — BOOKING MODELS
# File: app/modules/booking/models/__init__.py
# Doc Ref: DB Schema Part 4 — Booking Engine
# Phase: 3 — Booking Module
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
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.modules.master.models import Country, State, City  # noqa: F401


# ── Cancellation-engine tables (migration 0043) ─────────────────────────────
# Doc Ref: BRD Part 3 §46/§47, BRD Part 4 §82
# These tables back the admin-managed cancellation policy. They live in the
# booking module because every other module already imports booking models
# here, and it keeps cancellation concerns co-located with the booking
# lifecycle they belong to.


class CancellationPolicyVersion(Base):
    """Append-only audit of every admin edit to a global cab ladder key.

    Doc Ref: BRD Part 8 §213 (audit retention). The corresponding admin
    endpoint writes a row *before* the new value is committed, so we can
    always answer "what policy was in effect at the moment booking #N was
    cancelled".
    """

    __tablename__ = "cancellation_policy_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    config_key = Column(String(100), nullable=False)
    previous_value = Column(String(500), nullable=True)
    new_value = Column(String(500), nullable=False)
    changed_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    change_reason = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_policy_versions_key_time", "config_key", "created_at"),
    )


class BookingCancellationRequest(Base):
    """Partner asks admin to cancel a cab or hotel booking.

    Status lifecycle: PENDING → APPROVED | REJECTED | WITHDRAWN | AUTO_CLOSED.
    On APPROVED the cancel handler runs and the row is preserved as history.
    Doc Ref: BRD Part 3 §46 (partner cancellation), §47 (refund rules).
    """

    __tablename__ = "booking_cancellation_requests"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    booking_type = Column(String(20), nullable=False)  # CAB | HOTEL
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=True
    )
    cab_booking_id = Column(BigInteger, ForeignKey("cab_bookings.id"), nullable=True)
    hotel_reservation_id = Column(
        BigInteger, ForeignKey("hotel_reservations.id"), nullable=True
    )

    requested_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    requested_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    requested_reason = Column(Text, nullable=False)
    requested_reason_code = Column(String(40), nullable=True)

    refund_preview_json = Column(JSONB, nullable=True)

    status = Column(String(20), nullable=False, default="PENDING")
    reviewed_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(booking_type = 'CAB'  AND cab_booking_id IS NOT NULL "
            "AND hotel_reservation_id IS NULL) OR "
            "(booking_type = 'HOTEL' AND hotel_reservation_id IS NOT NULL "
            "AND cab_booking_id IS NULL)",
            name="chk_req_target",
        ),
        Index("idx_cancel_req_status", "status", "requested_at"),
        Index("idx_cancel_req_cab", "cab_booking_id"),
        Index("idx_cancel_req_hotel", "hotel_reservation_id"),
        Index("idx_cancel_req_partner", "requested_by_user_id", "requested_at"),
    )


class ReservationCancellation(Base):
    """Sibling of booking_cancellations, but for the hotel reservation level.

    Carries the policy snapshot that was applied so the audit trail is intact
    even if the hotel later edits its own policy or the global config moves.
    Doc Ref: SRS Part 5 §184/§185.
    """

    __tablename__ = "reservation_cancellations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id"),
        nullable=False,
        unique=True,
    )
    cancelled_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    cancelled_source = Column(
        String(20), nullable=False
    )  # CUSTOMER | PARTNER_REQUEST | ADMIN | SYSTEM
    cancellation_reason = Column(Text, nullable=True)
    cancellation_charge = Column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    refund_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    policy_snapshot = Column(JSONB, nullable=True)
    advance_refunded_total = Column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    cancelled_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MasterBooking(Base):
    """
    Master Travel Booking — one MTB per customer trip.
    Can contain cab + hotel + tour sub-bookings.
    Doc Ref: DB Schema Part 4, Section 3
    Status: DRAFT → PENDING_PAYMENT → CONFIRMED → IN_PROGRESS → COMPLETED → CLOSED
    """

    __tablename__ = "master_bookings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    booking_number = Column(String(100), unique=True, nullable=False)
    customer_id = Column(BigInteger, ForeignKey("customers.id"), nullable=False)
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=False)
    booking_status = Column(String(50), nullable=False, default="DRAFT")
    payment_status = Column(String(50), nullable=False, default="PENDING")
    total_amount = Column(Numeric(12, 2), default=Decimal("0.00"))
    total_paid_amount = Column(Numeric(12, 2), default=Decimal("0.00"))
    total_refund_amount = Column(Numeric(12, 2), default=Decimal("0.00"))
    journey_start_date = Column(Date, nullable=True)
    journey_end_date = Column(Date, nullable=True)
    remarks = Column(Text, nullable=True)
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

    services = relationship(
        "BookingService", back_populates="master_booking", cascade="all, delete-orphan"
    )
    cab_bookings = relationship(
        "CabBooking", back_populates="master_booking", cascade="all, delete-orphan"
    )
    hotel_bookings = relationship(
        "HotelBooking", back_populates="master_booking", cascade="all, delete-orphan"
    )
    timelines = relationship(
        "BookingTimeline", back_populates="master_booking", cascade="all, delete-orphan"
    )
    cancellation = relationship(
        "BookingCancellation", back_populates="master_booking", uselist=False
    )
    reviews = relationship("BookingReview", back_populates="master_booking")
    documents = relationship(
        "BookingDocument", back_populates="master_booking", cascade="all, delete-orphan"
    )
    notes = relationship(
        "BookingNote", back_populates="master_booking", cascade="all, delete-orphan"
    )
    contacts = relationship(
        "BookingContact", back_populates="master_booking", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_booking_customer", "customer_id"),
        Index("idx_booking_status", "booking_status"),
        Index("idx_booking_city", "city_id"),
        Index("idx_booking_created", "created_at"),
    )

    def __repr__(self):
        return f"<MasterBooking {self.booking_number} status={self.booking_status}>"


class BookingService(Base):
    """Links each service (CAB/HOTEL/TOUR) to the master booking. Doc Ref: Section 5"""

    __tablename__ = "booking_services"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    service_type = Column(String(50), nullable=False)
    service_reference_id = Column(BigInteger, nullable=False)
    service_status = Column(String(50), nullable=True)
    service_amount = Column(Numeric(12, 2), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    master_booking = relationship("MasterBooking", back_populates="services")

    __table_args__ = (
        Index("idx_booking_service_type", "service_type"),
        Index("idx_booking_service_mbid", "master_booking_id"),
    )


class CabBooking(Base):
    """
    Cab service booking under a master booking.
    Doc Ref: DB Schema Part 4, Section 7
    Trip types: LOCAL / AIRPORT / OUTSTATION / ONE_WAY / ROUND_TRIP
    """

    __tablename__ = "cab_bookings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    booking_number = Column(String(100), unique=True, nullable=False)
    trip_type = Column(String(50), nullable=True)
    vehicle_category_id = Column(
        BigInteger, ForeignKey("vehicle_categories.id"), nullable=True
    )
    pickup_location = Column(Text, nullable=True)
    pickup_latitude = Column(Numeric(10, 7), nullable=True)
    pickup_longitude = Column(Numeric(10, 7), nullable=True)
    drop_location = Column(Text, nullable=True)
    drop_latitude = Column(Numeric(10, 7), nullable=True)
    drop_longitude = Column(Numeric(10, 7), nullable=True)
    pickup_datetime = Column(DateTime(timezone=True), nullable=True)
    # ── Return datetime (added migration 0049) ────────────────────────────────
    # Captured for ROUND_TRIP so the fare engine can compute trip_days when
    # the rule's driver_allowance_type = PER_DAY. NULL for single-day trips
    # (PER_TRIP / PER_KM / NONE don't need it).
    return_datetime = Column(DateTime(timezone=True), nullable=True)
    estimated_distance = Column(Numeric(12, 2), nullable=True)
    estimated_amount = Column(Numeric(12, 2), nullable=True)
    final_amount = Column(Numeric(12, 2), nullable=True)
    booking_status = Column(String(50), nullable=False, default="PENDING_ASSIGNMENT")
    # ── Partner acceptance gate (added migration 0039) ────────────────────────
    # acceptance_deadline is set when status flips to
    # PENDING_PARTNER_ACCEPTANCE; cleared when the partner accepts /
    # rejects / admin reassigns / sweeper times out.
    acceptance_deadline = Column(DateTime(timezone=True), nullable=True)
    partner_responded_at = Column(DateTime(timezone=True), nullable=True)
    pending_partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=True)
    # ── Trip Assistance fields (added migration 0021) ──────────────────────────
    trip_start_km = Column(Numeric(12, 2), nullable=True)
    trip_started_at = Column(DateTime(timezone=True), nullable=True)
    trip_end_km = Column(Numeric(12, 2), nullable=True)
    trip_ended_at = Column(DateTime(timezone=True), nullable=True)
    actual_distance = Column(Numeric(12, 2), nullable=True)
    payment_mode = Column(String(20), nullable=True)  # CASH | ONLINE | WALLET
    payment_collected_by = Column(
        String(20), nullable=True
    )  # DRIVER | PARTNER | PLATFORM
    cash_pending_at = Column(
        String(20), nullable=True, default="NONE"
    )  # DRIVER | PARTNER | NONE
    # Rupees the driver actually took in hand (fare − coupon − advance). Stored at
    # payment time because total_paid_amount is overwritten with the full fare.
    cash_amount_due = Column(Numeric(14, 2), nullable=True)
    platform_commission = Column(Numeric(14, 2), nullable=True)
    partner_payout = Column(Numeric(14, 2), nullable=True)
    invoice_url = Column(Text, nullable=True)
    invoice_number = Column(String(100), unique=True, nullable=True)
    # ── GST / tax invoice (added migration 0024) ──────────────────────────────
    # Computed at close-trip from system config GST_ENABLED/GST_RATE and
    # persisted here so settlement + invoices read the real values (the ORM
    # previously omitted these columns, so GST silently zeroed everywhere).
    gst_rate = Column(Numeric(5, 2), nullable=True, default=Decimal("0.00"))
    gst_amount = Column(Numeric(14, 2), nullable=True, default=Decimal("0.00"))
    is_tax_invoice = Column(Boolean, nullable=True, default=False)
    # ── Coupon columns (added migration 0048) ────────────────────────────────
    # Persisted at booking creation time so the close-trip / invoice math
    # agrees with what the customer saw in the BookingReviewModal. coupon_id is
    # a soft FK (ON DELETE SET NULL) — the coupon may be deleted later, but
    # coupon_code is the immutable snapshot for audit/display.
    coupon_code = Column(String(50), nullable=True)
    coupon_id = Column(
        BigInteger, ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True
    )
    coupon_discount = Column(Numeric(12, 2), nullable=True, default=Decimal("0.00"))
    # ── Vehicle breakdown / swap audit (added migration 0041) ────────────────
    # is_breakdown_swap is True once at least one swap has happened. swap_count
    # tracks how many. original_partner_id is set on the FIRST handover so the
    # "lost-trip" partner can be identified after their assignment row closes.
    # pre_swap_actual_km is captured at breakdown report time for UI / audit
    # only — it does NOT feed pro-rata billing (original partner always gets
    # zero payout on handover). breakdown_* columns capture the original event.
    is_breakdown_swap = Column(Boolean, nullable=False, default=False)
    swap_count = Column(Integer, nullable=False, default=0)
    last_swap_at = Column(DateTime(timezone=True), nullable=True)
    pre_swap_actual_km = Column(Numeric(12, 2), nullable=True)
    original_partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=True)
    breakdown_reason = Column(String(50), nullable=True)
    breakdown_latitude = Column(Numeric(10, 7), nullable=True)
    breakdown_longitude = Column(Numeric(10, 7), nullable=True)
    breakdown_reported_at = Column(DateTime(timezone=True), nullable=True)
    breakdown_reported_by = Column(String(20), nullable=True)
    # ── Cancellation-engine columns (migration 0043) ──
    cancelled_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    cancelled_source = Column(String(20), nullable=True)
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

    master_booking = relationship("MasterBooking", back_populates="cab_bookings")
    assignments = relationship(
        "CabBookingAssignment",
        back_populates="cab_booking",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_cab_booking_status", "booking_status"),
        Index("idx_cab_booking_mbid", "master_booking_id"),
    )

    def __repr__(self):
        return f"<CabBooking {self.booking_number} status={self.booking_status}>"


class CabBookingAssignment(Base):
    """Links a cab booking to a partner/driver/vehicle. Doc Ref: Section 8"""

    __tablename__ = "cab_booking_assignments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cab_booking_id = Column(BigInteger, ForeignKey("cab_bookings.id"), nullable=False)
    partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=False)
    vehicle_id = Column(BigInteger, ForeignKey("vehicles.id"), nullable=True)
    driver_id = Column(BigInteger, ForeignKey("drivers.id"), nullable=True)
    assigned_by = Column(UUID(as_uuid=True), nullable=True)
    assigned_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    assignment_type = Column(String(50), default="MANUAL")
    # ── Partner acceptance audit (added migration 0039) ───────────────────────
    # A row's acceptance_deadline is set when the cab is in
    # PENDING_PARTNER_ACCEPTANCE for that row. accepted_at / rejected_at
    # are mutually exclusive — set on partner sign-off. rejection_reason_code
    # is one of the REJECT_REASONS codes from app.modules.partner.constants
    # plus the system codes TIMEOUT / ADMIN_REASSIGN.
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason_code = Column(String(50), nullable=True)
    rejection_notes = Column(Text, nullable=True)
    acceptance_deadline = Column(DateTime(timezone=True), nullable=True)
    # ── Swap lifecycle (added migration 0041) ───────────────────────────────
    # closed_at IS NULL means this is the *currently active* assignment.
    # close_reason explains how it ended:
    #   TRIP_COMPLETED  — normal close at trip end
    #   VEHICLE_BREAKDOWN — vehicle broke down mid-trip
    #   PARTNER_HANDOVER  — booking was handed to a different partner
    #   ADMIN_REASSIGN    — admin manually reassigned before trip start
    # assignment_type additionally carries SWAP_SAME_PARTNER / SWAP_HANDOVER
    # / BREAKDOWN_REPLACEMENT for the rows that replaced a broken-down vehicle.
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(String(50), nullable=True)
    km_at_assignment_start = Column(Numeric(12, 2), nullable=True)
    km_at_assignment_end = Column(Numeric(12, 2), nullable=True)

    cab_booking = relationship("CabBooking", back_populates="assignments")

    __table_args__ = (
        Index("idx_cab_assign_cab", "cab_booking_id"),
        Index("idx_cab_assign_driver", "driver_id"),
    )


class BookingTimeline(Base):
    """Audit trail of all events on a booking. Doc Ref: Section 14"""

    __tablename__ = "booking_timelines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    event_type = Column(String(100), nullable=True)
    event_description = Column(Text, nullable=True)
    event_timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    created_by = Column(UUID(as_uuid=True), nullable=True)

    master_booking = relationship("MasterBooking", back_populates="timelines")

    __table_args__ = (Index("idx_timeline_booking", "master_booking_id"),)


class BookingCancellation(Base):
    """Cancellation details. Doc Ref: Section 16"""

    __tablename__ = "booking_cancellations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False, unique=True
    )
    cancelled_by = Column(UUID(as_uuid=True), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    cancellation_charge = Column(Numeric(12, 2), default=Decimal("0.00"))
    refund_amount = Column(Numeric(12, 2), default=Decimal("0.00"))
    cancelled_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # ── Cancellation-engine columns (migration 0043) ──
    cancelled_source = Column(
        String(20), nullable=False, default="ADMIN"
    )  # CUSTOMER | PARTNER_REQUEST | ADMIN | SYSTEM
    cancelled_by_role = Column(String(30), nullable=True)
    policy_snapshot = Column(JSONB, nullable=True)
    advance_refunded_total = Column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )

    master_booking = relationship("MasterBooking", back_populates="cancellation")


class BookingReview(Base):
    """Customer review for a completed booking."""

    __tablename__ = "booking_reviews"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    customer_id = Column(BigInteger, ForeignKey("customers.id"), nullable=False)
    rating = Column(Integer, nullable=True)
    review = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    master_booking = relationship("MasterBooking", back_populates="reviews")

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_review_rating"),
    )


class BookingDocument(Base):
    """Vouchers, confirmations, invoices. Doc Ref: Section 19"""

    __tablename__ = "booking_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    document_type = Column(String(100), nullable=True)
    file_url = Column(Text, nullable=True)
    generated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    master_booking = relationship("MasterBooking", back_populates="documents")


class BookingNote(Base):
    """Internal CCO notes on a booking."""

    __tablename__ = "booking_notes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    note = Column(Text, nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    master_booking = relationship("MasterBooking", back_populates="notes")


class AdvancePayment(Base):
    """
    Money collected from the customer before trip end.
    Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody

    received_by is the custody signal that drives settlement direction:
    ADMIN means the platform holds the money, PARTNER/DRIVER means the
    partner side does. payment_mode is constrained by received_by in
    app/modules/booking/services (ADMIN may take ONLINE, the others may not).

    A booking carries at most one ACTIVE row, enforced by the partial unique
    index ux_advance_payments_active_booking. Voiding flips status rather than
    deleting, which frees the index while preserving the audit trail.
    """

    __tablename__ = "advance_payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cab_booking_id = Column(
        BigInteger, ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False
    )
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id", ondelete="CASCADE"), nullable=False
    )
    booking_number = Column(String(50), nullable=False)
    receipt_number = Column(String(50), unique=True, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    payment_mode = Column(String(20), nullable=False)  # CASH | ONLINE | UPI
    received_by = Column(String(20), nullable=False)  # ADMIN | PARTNER | DRIVER
    partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=True)
    driver_id = Column(BigInteger, ForeignKey("drivers.id"), nullable=True)
    reference_note = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")  # ACTIVE | VOIDED
    refunded_amount = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    collected_by_user_id = Column(UUID(as_uuid=True), nullable=False)
    collected_by_role = Column(
        String(20), nullable=False
    )  # ADMIN | PARTNER — source portal
    collected_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    voided_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    void_reason = Column(Text, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_advance_amount_positive"),
        Index("idx_advance_payments_status_collected", "status", "collected_at"),
        Index("idx_advance_payments_partner", "partner_id"),
    )

    def __repr__(self):
        return f"<AdvancePayment {self.receipt_number} {self.amount} {self.received_by} {self.status}>"


class BookingContact(Base):
    """Traveller contact details."""

    __tablename__ = "booking_contacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    contact_name = Column(String(255), nullable=True)
    mobile = Column(String(15), nullable=True)
    email = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    master_booking = relationship("MasterBooking", back_populates="contacts")


class HotelBooking(Base):
    """
    Hotel service booking under a master booking.
    Doc Ref: BRD Part 4 §57-92
    Status flow:
      PENDING_PAYMENT → AWAITING_HOTEL_CONFIRMATION → CONFIRMED → CHECKED_IN
      → IN_HOUSE → CHECKED_OUT → COMPLETED → SETTLED
      (+ CANCELLED, NO_SHOW, REJECTED from various points)
    Booking number format: WT-HTL-YYYYNNNNN
    """

    __tablename__ = "hotel_bookings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    booking_number = Column(String(100), unique=True, nullable=False)

    # Hotel & room info (denormalised at booking time)
    hotel_id = Column(BigInteger, ForeignKey("hotels.id"), nullable=True)
    hotel_name = Column(String(255), nullable=True)
    hotel_address = Column(Text, nullable=True)
    room_category_id = Column(BigInteger, nullable=True)
    room_category_name = Column(String(255), nullable=True)
    room_type = Column(String(100), nullable=True)  # STANDARD | DELUXE | SUITE etc.
    meal_plan = Column(String(50), nullable=True)  # EP | CP | MAP | AP

    # Stay details
    check_in_date = Column(Date, nullable=True)
    check_out_date = Column(Date, nullable=True)
    num_nights = Column(Integer, nullable=True)
    num_rooms = Column(Integer, nullable=True, default=1)
    num_guests = Column(Integer, nullable=True, default=1)

    # Pricing
    base_amount = Column(Numeric(12, 2), nullable=True)
    taxes_amount = Column(Numeric(12, 2), nullable=True, default=Decimal("0.00"))
    additional_charges = Column(Numeric(12, 2), nullable=True, default=Decimal("0.00"))
    final_amount = Column(Numeric(12, 2), nullable=True)

    # Check-in/out timestamps and notes
    actual_check_in_at = Column(DateTime(timezone=True), nullable=True)
    actual_check_out_at = Column(DateTime(timezone=True), nullable=True)
    check_in_id_proof = Column(String(100), nullable=True)  # AADHAAR | PASSPORT | DL
    check_in_id_number = Column(String(100), nullable=True)
    check_out_notes = Column(Text, nullable=True)

    # Cancellation
    cancellation_reason = Column(Text, nullable=True)
    cancellation_charge = Column(Numeric(12, 2), nullable=True, default=Decimal("0.00"))
    refund_amount = Column(Numeric(12, 2), nullable=True, default=Decimal("0.00"))

    booking_status = Column(String(50), nullable=False, default="PENDING_PAYMENT")

    # Voucher / confirmation reference
    hotel_confirmation_number = Column(String(100), nullable=True)
    voucher_url = Column(Text, nullable=True)

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

    master_booking = relationship("MasterBooking", back_populates="hotel_bookings")

    __table_args__ = (
        Index("idx_hotel_booking_status", "booking_status"),
        Index("idx_hotel_booking_mbid", "master_booking_id"),
        Index("idx_hotel_booking_hotel", "hotel_id"),
    )

    def __repr__(self):
        return f"<HotelBooking {self.booking_number} status={self.booking_status}>"
