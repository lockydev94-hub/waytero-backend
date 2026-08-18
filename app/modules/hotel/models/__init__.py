# ============================================================
# WAY TERO — HOTEL MODELS
# File: app/modules/hotel/models/__init__.py
# Doc Ref: DB Schema Part 5 — Hotel Management
#          BRD Part 4 §57-92, SRS Part 5 §153-194
# Migration: 0031_hotel_module
# ============================================================

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.modules.master.models import Country, State, City  # noqa: F401


def _utcnow():
    return datetime.now(timezone.utc)


# ============================================================
# MASTERS
# ============================================================


class HotelCategory(Base):
    """Master property types (BUDGET, RESORT, HOMESTAY…). Doc Ref: SRS Part 5 §157"""

    __tablename__ = "hotel_categories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    category_code = Column(String(50), unique=True, nullable=False)
    label = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)

    image_url = Column(Text, nullable=True)
    icon_url = Column(Text, nullable=True)

    seo_title = Column(String(120), nullable=True)
    seo_description = Column(String(320), nullable=True)
    seo_keywords = Column(String(500), nullable=True)

    display_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    hotels = relationship("Hotel", back_populates="category")

    def __repr__(self):
        return f"<HotelCategory {self.category_code}>"


class HotelAmenity(Base):
    """Master amenity list. icon_name is a MUI icon key, not a URL. Doc Ref: BRD Part 4 §64"""

    __tablename__ = "hotel_amenities"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    amenity_code = Column(String(50), unique=True, nullable=False)
    amenity_name = Column(String(100), nullable=False)
    icon_name = Column(String(100), nullable=True)
    amenity_group = Column(String(50), nullable=True)
    display_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self):
        return f"<HotelAmenity {self.amenity_code}>"


class HotelGstSlab(Base):
    """Tariff-based room GST slabs. Doc Ref: BRD Part 4 §88

    The slab is a function of the nightly tariff per room, not of the hotel, so
    one property can carry a 5% Standard room and an 18% Suite.
    """

    __tablename__ = "hotel_gst_slabs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slab_name = Column(String(100), nullable=False)
    tariff_from = Column(Numeric(12, 2), nullable=False)
    tariff_to = Column(Numeric(12, 2), nullable=True)  # NULL = open-ended top slab
    gst_percent = Column(Numeric(5, 2), nullable=False)
    has_input_credit = Column(Boolean, nullable=False, default=False)
    hsn_code = Column(String(20), nullable=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self):
        return f"<HotelGstSlab {self.slab_name} {self.gst_percent}%>"


# ============================================================
# THE PROPERTY
# ============================================================


class Hotel(Base):
    """Hotel property. Doc Ref: DB Schema Part 5 §2, SRS Part 5 §155

    Status flow: DRAFT → PENDING → UNDER_REVIEW → APPROVED → ACTIVE
                 (+ DOCUMENT_PENDING, REJECTED, INACTIVE, SUSPENDED, BLOCKED)
    """

    __tablename__ = "hotels"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    partner_id = Column(BigInteger, ForeignKey("partners.id"), nullable=False)
    hotel_code = Column(String(50), unique=True, nullable=False)

    hotel_name = Column(String(255), nullable=False)
    hotel_type = Column(String(100), nullable=True)
    hotel_category_id = Column(
        BigInteger, ForeignKey("hotel_categories.id"), nullable=True
    )
    star_rating = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    short_description = Column(String(500), nullable=True)

    gst_number = Column(String(20), nullable=True)
    pan_number = Column(String(20), nullable=True)

    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=False)
    state_id = Column(BigInteger, ForeignKey("states.id"), nullable=True)
    address = Column(Text, nullable=True)
    address_line_2 = Column(String(255), nullable=True)
    landmark = Column(String(255), nullable=True)
    postal_code = Column(String(20), nullable=True)
    latitude = Column(Numeric(10, 7), nullable=True)
    longitude = Column(Numeric(10, 7), nullable=True)

    contact_person = Column(String(255), nullable=True)
    contact_number = Column(String(15), nullable=True)
    alternate_number = Column(String(15), nullable=True)
    email = Column(String(255), nullable=True)
    website_url = Column(Text, nullable=True)

    total_rooms = Column(Integer, nullable=False, default=0)
    confirmation_mode = Column(
        String(30), nullable=False, default="MANUAL_CONFIRMATION"
    )
    room_allocation_mode = Column(String(30), nullable=False, default="AT_CHECK_IN")

    # Only meaningful when the global GST_ENABLED config is on — that switch wins.
    tax_mode = Column(String(20), nullable=False, default="EXCLUSIVE")
    is_gst_registered = Column(Boolean, nullable=False, default=False)

    slug = Column(String(255), unique=True, nullable=True)
    seo_title = Column(String(120), nullable=True)
    seo_description = Column(String(320), nullable=True)
    seo_keywords = Column(String(500), nullable=True)
    is_featured = Column(Boolean, nullable=False, default=False)
    display_order = Column(Integer, nullable=False, default=0)

    status = Column(String(50), nullable=False, default="DRAFT")
    rejection_reason = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    is_own_risk_approved = Column(Boolean, nullable=False, default=False)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    category = relationship("HotelCategory", back_populates="hotels")
    amenity_mappings = relationship(
        "HotelAmenityMapping", back_populates="hotel", cascade="all, delete-orphan"
    )
    images = relationship(
        "HotelImage", back_populates="hotel", cascade="all, delete-orphan"
    )
    documents = relationship(
        "HotelDocument", back_populates="hotel", cascade="all, delete-orphan"
    )
    policy = relationship(
        "HotelPolicy",
        back_populates="hotel",
        uselist=False,
        cascade="all, delete-orphan",
    )
    commission_configs = relationship(
        "HotelCommissionConfig", back_populates="hotel", cascade="all, delete-orphan"
    )
    room_categories = relationship(
        "HotelRoomCategory", back_populates="hotel", cascade="all, delete-orphan"
    )
    rooms = relationship(
        "HotelRoom", back_populates="hotel", cascade="all, delete-orphan"
    )
    verification_assignments = relationship(
        "HotelVerificationAssignment",
        back_populates="hotel",
        cascade="all, delete-orphan",
    )
    rating = relationship(
        "HotelRating",
        back_populates="hotel",
        uselist=False,
        cascade="all, delete-orphan",
    )
    performance = relationship(
        "HotelPerformanceSummary",
        back_populates="hotel",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Hotel {self.hotel_code} {self.hotel_name}>"


class HotelAmenityMapping(Base):
    """Hotel ↔ amenity join. Doc Ref: DB Schema Part 5 §11"""

    __tablename__ = "hotel_amenity_mappings"
    __table_args__ = (
        UniqueConstraint("hotel_id", "amenity_id", name="uq_hotel_amenity"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    amenity_id = Column(BigInteger, ForeignKey("hotel_amenities.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    hotel = relationship("Hotel", back_populates="amenity_mappings")
    amenity = relationship("HotelAmenity")


class HotelImage(Base):
    """Cloudinary hotel gallery. Doc Ref: DB Schema Part 5 §17

    At most one is_primary row per hotel — held by the partial unique index
    uq_hotel_primary_image, not by application code.
    """

    __tablename__ = "hotel_images"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    image_url = Column(Text, nullable=False)
    thumbnail_url = Column(Text, nullable=True)
    caption = Column(String(255), nullable=True)
    image_type = Column(String(50), nullable=False, default="GALLERY")
    display_order = Column(Integer, nullable=False, default=0)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    hotel = relationship("Hotel", back_populates="images")


class HotelDocument(Base):
    """Hotel compliance documents. Doc Ref: DB Schema Part 5 §18, BRD Part 4 §60"""

    __tablename__ = "hotel_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    document_type = Column(String(100), nullable=False)
    document_number = Column(String(100), nullable=True)
    file_url = Column(Text, nullable=False)
    issue_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)
    verification_status = Column(String(50), nullable=False, default="PENDING")
    remarks = Column(Text, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    hotel = relationship("Hotel", back_populates="documents")


class HotelPolicy(Base):
    """One policy row per hotel. Doc Ref: BRD Part 4 §77, §82

    Three fixed cancellation tiers because BRD §82 specifies exactly three plus
    same-day; an arbitrary-N child table would buy flexibility nobody asked for.
    """

    __tablename__ = "hotel_policies"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger,
        ForeignKey("hotels.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    check_in_time = Column(String(10), nullable=True, default="12:00")
    check_out_time = Column(String(10), nullable=True, default="11:00")
    early_check_in_allowed = Column(Boolean, nullable=False, default=False)
    late_check_out_allowed = Column(Boolean, nullable=False, default=False)

    cancellation_free_hours = Column(Integer, nullable=True, default=72)
    refund_percent_tier_1 = Column(Numeric(5, 2), nullable=True, default=100)
    cancellation_tier_2_hours = Column(Integer, nullable=True, default=48)
    refund_percent_tier_2 = Column(Numeric(5, 2), nullable=True, default=75)
    cancellation_tier_3_hours = Column(Integer, nullable=True, default=24)
    refund_percent_tier_3 = Column(Numeric(5, 2), nullable=True, default=50)
    refund_percent_same_day = Column(Numeric(5, 2), nullable=True, default=0)
    no_show_refund_percent = Column(Numeric(5, 2), nullable=True, default=0)

    couples_allowed = Column(Boolean, nullable=False, default=True)
    unmarried_couples_allowed = Column(Boolean, nullable=False, default=False)
    local_id_accepted = Column(Boolean, nullable=False, default=True)
    pets_allowed = Column(Boolean, nullable=False, default=False)
    smoking_allowed = Column(Boolean, nullable=False, default=False)
    alcohol_allowed = Column(Boolean, nullable=False, default=True)
    min_guest_age = Column(Integer, nullable=True)
    extra_bed_charge = Column(Numeric(12, 2), nullable=True)
    child_free_age_limit = Column(Integer, nullable=True, default=5)
    house_rules = Column(Text, nullable=True)
    cancellation_policy_text = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    hotel = relationship("Hotel", back_populates="policy")


class HotelCommissionConfig(Base):
    """Per-hotel commission override. Doc Ref: BRD Part 4 §87

    Exists because commission_rules carries one commission_type and one value and
    so cannot express HYBRID. Superseding inserts a new row and deactivates the
    old one, keeping historical bookings explainable.
    """

    __tablename__ = "hotel_commission_configs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )

    commission_type = Column(String(20), nullable=False)  # PERCENTAGE | FLAT | HYBRID
    commission_percent = Column(Numeric(6, 3), nullable=False, default=0)
    commission_flat = Column(Numeric(12, 2), nullable=False, default=0)
    min_commission = Column(Numeric(12, 2), nullable=True)
    max_commission = Column(Numeric(12, 2), nullable=True)

    applies_to = Column(String(20), nullable=False, default="PER_BOOKING")
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    remarks = Column(Text, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    hotel = relationship("Hotel", back_populates="commission_configs")


# ============================================================
# ROOMS, RATES, INVENTORY
# ============================================================


class HotelRoomCategory(Base):
    """The sellable unit. Doc Ref: DB Schema Part 5 §5, SRS Part 5 §160"""

    __tablename__ = "hotel_room_categories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    category_name = Column(String(150), nullable=False)
    room_type = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)

    base_occupancy = Column(Integer, nullable=False, default=2)
    max_adults = Column(Integer, nullable=False, default=2)
    max_children = Column(Integer, nullable=False, default=1)
    max_occupancy = Column(Integer, nullable=False, default=3)
    extra_bed_allowed = Column(Boolean, nullable=False, default=False)
    extra_bed_charge = Column(Numeric(12, 2), nullable=False, default=0)
    extra_adult_charge = Column(Numeric(12, 2), nullable=False, default=0)
    extra_child_charge = Column(Numeric(12, 2), nullable=False, default=0)

    bed_type = Column(String(100), nullable=True)
    room_size_sqft = Column(Integer, nullable=True)
    view_type = Column(String(100), nullable=True)
    floor_range = Column(String(50), nullable=True)

    base_price = Column(Numeric(12, 2), nullable=False, default=0)
    published_price = Column(Numeric(12, 2), nullable=True)
    min_sellable_price = Column(Numeric(12, 2), nullable=True)
    meal_plan = Column(String(20), nullable=False, default="EP")  # EP | CP | MAP | AP
    is_refundable = Column(Boolean, nullable=False, default=True)

    total_rooms = Column(Integer, nullable=False, default=0)
    display_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    hotel = relationship("Hotel", back_populates="room_categories")
    amenities = relationship(
        "HotelRoomCategoryAmenity",
        back_populates="room_category",
        cascade="all, delete-orphan",
    )
    images = relationship(
        "HotelRoomCategoryImage",
        back_populates="room_category",
        cascade="all, delete-orphan",
    )
    rate_plans = relationship(
        "HotelRoomRatePlan",
        back_populates="room_category",
        cascade="all, delete-orphan",
    )
    rooms = relationship(
        "HotelRoom", back_populates="room_category", cascade="all, delete-orphan"
    )
    inventory = relationship(
        "HotelInventory", back_populates="room_category", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<HotelRoomCategory {self.category_name}>"


class HotelRoomCategoryAmenity(Base):
    """Room-level amenity join. Doc Ref: BRD Part 4 §64"""

    __tablename__ = "hotel_room_category_amenities"
    __table_args__ = (
        UniqueConstraint("room_category_id", "amenity_id", name="uq_room_cat_amenity"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    room_category_id = Column(
        BigInteger,
        ForeignKey("hotel_room_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    amenity_id = Column(BigInteger, ForeignKey("hotel_amenities.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    room_category = relationship("HotelRoomCategory", back_populates="amenities")
    amenity = relationship("HotelAmenity")


class HotelRoomCategoryImage(Base):
    """Cloudinary room gallery. Doc Ref: DB Schema Part 5 §17"""

    __tablename__ = "hotel_room_category_images"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    room_category_id = Column(
        BigInteger,
        ForeignKey("hotel_room_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    image_url = Column(Text, nullable=False)
    caption = Column(String(255), nullable=True)
    display_order = Column(Integer, nullable=False, default=0)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    room_category = relationship("HotelRoomCategory", back_populates="images")


class HotelRoomRatePlan(Base):
    """Date-ranged rate override. Doc Ref: BRD Part 4 §70

    Overlaps are allowed and resolved by precedence
    (FESTIVAL > SEASONAL > WEEKEND > PROMOTIONAL) then priority desc then id desc.
    Forbidding overlap would make "Diwali inside the winter season" unrepresentable.
    """

    __tablename__ = "hotel_room_rate_plans"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    room_category_id = Column(
        BigInteger,
        ForeignKey("hotel_room_categories.id", ondelete="CASCADE"),
        nullable=False,
    )

    plan_name = Column(String(150), nullable=False)
    plan_type = Column(
        String(30), nullable=False
    )  # WEEKEND | SEASONAL | FESTIVAL | PROMOTIONAL
    priority = Column(Integer, nullable=False, default=0)

    date_from = Column(Date, nullable=False)
    date_to = Column(Date, nullable=False)
    day_of_week_mask = Column(String(20), nullable=True)  # e.g. "0000011" Mon..Sun

    rate_mode = Column(
        String(20), nullable=False, default="ABSOLUTE"
    )  # ABSOLUTE | PERCENT | DELTA
    rate_value = Column(Numeric(12, 2), nullable=False)

    min_nights = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    room_category = relationship("HotelRoomCategory", back_populates="rate_plans")

    def __repr__(self):
        return f"<HotelRoomRatePlan {self.plan_name} {self.plan_type}>"


class HotelRoom(Base):
    """Physical room. Optional — BRD §66 calls room numbers recommended, not required."""

    __tablename__ = "hotel_rooms"
    __table_args__ = (
        UniqueConstraint("hotel_id", "room_number", name="uq_hotel_room_number"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    room_category_id = Column(
        BigInteger,
        ForeignKey("hotel_room_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    room_number = Column(String(50), nullable=False)
    floor_number = Column(String(20), nullable=True)
    room_status = Column(String(50), nullable=False, default="AVAILABLE")
    remarks = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    hotel = relationship("Hotel", back_populates="rooms")
    room_category = relationship("HotelRoomCategory", back_populates="rooms")


class HotelInventory(Base):
    """Per room-category per DATE availability. Doc Ref: SRS Part 5 §161-162, §165

    available_rooms carries a DB CHECK (>= 0). That constraint, not an
    application check, is what satisfies SRS Rule 64 under concurrency.
    """

    __tablename__ = "hotel_inventory"
    __table_args__ = (
        UniqueConstraint(
            "room_category_id", "inventory_date", name="uq_hotel_inv_cat_date"
        ),
        sa.CheckConstraint("available_rooms >= 0", name="ck_hotel_inv_nonneg"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    room_category_id = Column(
        BigInteger,
        ForeignKey("hotel_room_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    inventory_date = Column(Date, nullable=False)
    total_rooms = Column(Integer, nullable=False, default=0)
    booked_rooms = Column(Integer, nullable=False, default=0)
    blocked_rooms = Column(Integer, nullable=False, default=0)
    held_rooms = Column(Integer, nullable=False, default=0)
    available_rooms = Column(Integer, nullable=False, default=0)
    rate_override = Column(Numeric(12, 2), nullable=True)
    is_stop_sell = Column(Boolean, nullable=False, default=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    room_category = relationship("HotelRoomCategory", back_populates="inventory")


# ============================================================
# VERIFICATION
# ============================================================


class HotelVerificationAssignment(Base):
    """Officer assignment. Mirrors vehicle_verification_assignments (migration 0017).

    Reassignment deactivates existing rows and inserts a new one.
    """

    __tablename__ = "hotel_verification_assignments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    officer_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assigned_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    unassigned_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)

    hotel = relationship("Hotel", back_populates="verification_assignments")


class HotelVerificationLog(Base):
    """Append-only status audit trail. Doc Ref: BRD Part 4 §91

    Records from_status/to_status, which partner_verification_logs does not —
    without them a status history is guesswork. The service exposes no
    update or delete.
    """

    __tablename__ = "hotel_verification_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger, ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False
    )
    action = Column(String(100), nullable=False)
    from_status = Column(String(50), nullable=True)
    to_status = Column(String(50), nullable=True)
    remarks = Column(Text, nullable=True)
    performed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ============================================================
# AGGREGATES
# ============================================================


class HotelRating(Base):
    """Denormalised rating aggregate. Doc Ref: DB Schema Part 5 §19"""

    __tablename__ = "hotel_ratings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger,
        ForeignKey("hotels.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    average_rating = Column(Numeric(3, 2), nullable=False, default=0)
    total_reviews = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    hotel = relationship("Hotel", back_populates="rating")


class HotelPerformanceSummary(Base):
    """Denormalised performance aggregate. Doc Ref: DB Schema Part 5 §20"""

    __tablename__ = "hotel_performance_summary"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id = Column(
        BigInteger,
        ForeignKey("hotels.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    total_bookings = Column(Integer, nullable=False, default=0)
    occupancy_rate = Column(Numeric(5, 2), nullable=False, default=0)
    total_revenue = Column(Numeric(14, 2), nullable=False, default=0)
    cancellation_rate = Column(Numeric(5, 2), nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    hotel = relationship("Hotel", back_populates="performance")


# ============================================================
# RESERVATIONS — schema only this phase, no service layer
# ============================================================


class HotelReservation(Base):
    """Hotel sub-booking. Doc Ref: DB Schema Part 5 §12, SRS Part 5 §169-170

    The three *_snapshot columns freeze the resolved rate, commission config and
    cancellation policy at confirmation. BRD Rule 25 forbids later config edits
    from changing a confirmed booking, and current config cannot reconstruct
    what was in force at the time.
    """

    __tablename__ = "hotel_reservations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    master_booking_id = Column(
        BigInteger, ForeignKey("master_bookings.id"), nullable=False
    )
    booking_service_id = Column(
        BigInteger, ForeignKey("booking_services.id"), nullable=True
    )
    hotel_id = Column(BigInteger, ForeignKey("hotels.id"), nullable=False)
    room_category_id = Column(
        BigInteger, ForeignKey("hotel_room_categories.id"), nullable=False
    )
    customer_id = Column(BigInteger, nullable=True)
    reservation_number = Column(String(100), unique=True, nullable=True)

    check_in_date = Column(Date, nullable=False)
    check_out_date = Column(Date, nullable=False)
    nights = Column(Integer, nullable=False, default=1)
    rooms_count = Column(Integer, nullable=False, default=1)
    room_nights = Column(Integer, nullable=False, default=1)
    adults_count = Column(Integer, nullable=False, default=1)
    children_count = Column(Integer, nullable=False, default=0)

    base_amount = Column(Numeric(12, 2), nullable=False, default=0)
    extra_charges = Column(Numeric(12, 2), nullable=False, default=0)
    discount_amount = Column(Numeric(12, 2), nullable=False, default=0)
    taxable_amount = Column(Numeric(12, 2), nullable=False, default=0)
    gst_percent = Column(Numeric(5, 2), nullable=False, default=0)
    gst_amount = Column(Numeric(12, 2), nullable=False, default=0)
    is_tax_invoice = Column(Boolean, nullable=False, default=False)
    total_amount = Column(Numeric(12, 2), nullable=False, default=0)
    platform_commission = Column(Numeric(12, 2), nullable=False, default=0)
    partner_payout = Column(Numeric(12, 2), nullable=False, default=0)

    rate_snapshot = Column(JSONB, nullable=True)
    commission_config_snapshot = Column(JSONB, nullable=True)
    cancellation_policy_snapshot = Column(JSONB, nullable=True)

    reservation_status = Column(String(50), nullable=False, default="PENDING_PAYMENT")
    voucher_url = Column(Text, nullable=True)
    special_requests = Column(Text, nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Admin lifecycle columns (migration 0034)
    hotel_confirmation_number = Column(String(100), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    cancellation_charge = Column(Numeric(12, 2), nullable=False, default=0)
    refund_amount = Column(Numeric(12, 2), nullable=False, default=0)
    # Cancellation-engine columns (migration 0043)
    cancelled_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    cancelled_source = Column(String(20), nullable=True)
    check_in_id_proof = Column(String(100), nullable=True)
    check_in_id_number = Column(String(100), nullable=True)
    actual_check_in_at = Column(DateTime(timezone=True), nullable=True)
    actual_check_out_at = Column(DateTime(timezone=True), nullable=True)
    check_out_notes = Column(Text, nullable=True)

    # Billing, invoicing and payment custody (migration 0035)
    coupon_code = Column(String(50), nullable=True)
    coupon_discount = Column(Numeric(12, 2), nullable=False, default=0)
    overtime_hours = Column(Numeric(6, 2), nullable=False, default=0)
    overtime_charge = Column(Numeric(12, 2), nullable=False, default=0)
    invoice_number = Column(String(50), nullable=True)
    invoice_url = Column(Text, nullable=True)
    invoice_generated_at = Column(DateTime(timezone=True), nullable=True)
    # PENDING | PARTIAL | PAID — advances move this too, not just final payment.
    payment_collected_status = Column(String(20), nullable=False, default="PENDING")
    payment_mode = Column(String(20), nullable=True)
    # Who physically holds the money: ADMIN (platform) or PARTNER (property).
    payment_collected_by = Column(String(20), nullable=True)
    payment_reference = Column(String(120), nullable=True)
    payment_collected_at = Column(DateTime(timezone=True), nullable=True)

    # ── Switch / split-stay lineage (migration 0042_hotel_switch) ──
    # `is_split_stay` is set on BOTH halves of a split (original + new).
    # `original_reservation_id` points from the new row back to the original;
    # on the original row itself the column stays NULL.
    # `switched_reason` is informational — used for filtering / reports.
    # `split_advance_strategy` captures which advance-redistribution rule
    # applied (currently ROLLOVER or NONE).
    is_split_stay = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    original_reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="SET NULL"),
        nullable=True,
    )
    switched_at = Column(DateTime(timezone=True), nullable=True)
    switched_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    switched_reason = Column(String(50), nullable=True)
    split_advance_strategy = Column(String(20), nullable=True)

    guests = relationship(
        "HotelReservationGuest",
        back_populates="reservation",
        cascade="all, delete-orphan",
    )
    checkin = relationship(
        "HotelCheckin",
        back_populates="reservation",
        uselist=False,
        cascade="all, delete-orphan",
    )
    checkout = relationship(
        "HotelCheckout",
        back_populates="reservation",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<HotelReservation {self.reservation_number}>"


class HotelReservationSplitEvent(Base):
    """Append-only audit row for every hotel switch / mid-stay split.
    Doc Ref: Hotel Switch Spec; Migration: 0042_hotel_switch.

    Captures the money moves for one switch/split:
      - split_type: PRE_CHECKIN_SWITCH | POST_CHECKIN_SPLIT
      - nights_transferred: how many of the original's planned nights moved
      - original_nights_consumed: how many of the original's planned nights
        were already consumed before the split (POST_CHECKIN_SPLIT only;
        0 for PRE_CHECKIN_SWITCH).
      - original_final_amount: the truncated bill frozen onto the original.
      - new_total_amount: the full bill of the new reservation.
      - refund_issued: any refund the customer gets back from the original's
        advances under NONE strategy (or the unused portion under ROLLOVER).
      - advance_redistributed: amount rolled over from the original onto the
        new reservation (ROLLOVER strategy only).
      - advance_split_strategy: ROLLOVER | NONE — mirrors hotel_reservations.

    UNIQUE (original_reservation_id, new_reservation_id) is the safety net
    against double-recording the same switch from a retried endpoint.
    """

    __tablename__ = "hotel_reservation_split_events"
    __table_args__ = (
        UniqueConstraint(
            "original_reservation_id",
            "new_reservation_id",
            name="uq_split_event",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    original_reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    new_reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="CASCADE"),
        nullable=False,
    )

    split_type = Column(String(20), nullable=False)
    nights_transferred = Column(Integer, nullable=False)
    original_nights_consumed = Column(Integer, nullable=False)

    original_final_amount = Column(Numeric(14, 2), nullable=False, default=0)
    new_total_amount = Column(Numeric(14, 2), nullable=False, default=0)
    refund_issued = Column(Numeric(14, 2), nullable=False, default=0)
    advance_redistributed = Column(Numeric(14, 2), nullable=False, default=0)
    advance_split_strategy = Column(String(20), nullable=False)

    notes = Column(Text, nullable=True)
    created_by_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    original = relationship(
        "HotelReservation",
        foreign_keys=[original_reservation_id],
    )
    new_reservation = relationship(
        "HotelReservation",
        foreign_keys=[new_reservation_id],
    )

    def __repr__(self) -> str:
        return (
            f"<HotelReservationSplitEvent {self.split_type} "
            f"original={self.original_reservation_id} new={self.new_reservation_id}>"
        )


class HotelReservationGuest(Base):
    """Guest roster. Doc Ref: DB Schema Part 5 §14"""

    __tablename__ = "hotel_reservation_guests"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    guest_name = Column(String(255), nullable=False)
    mobile = Column(String(15), nullable=True)
    gender = Column(String(20), nullable=True)
    age = Column(Integer, nullable=True)
    id_type = Column(String(50), nullable=True)
    id_number = Column(String(100), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    reservation = relationship("HotelReservation", back_populates="guests")


class HotelCheckin(Base):
    """Check-in event. Doc Ref: DB Schema Part 5 §15, BRD Part 4 §78"""

    __tablename__ = "hotel_checkins"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    check_in_datetime = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    allocated_rooms = Column(Text, nullable=True)
    checked_in_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    reservation = relationship("HotelReservation", back_populates="checkin")


class HotelCheckout(Base):
    """Check-out event. Doc Ref: DB Schema Part 5 §16, BRD Part 4 §79-80, §84"""

    __tablename__ = "hotel_checkouts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    check_out_datetime = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    additional_charges = Column(Numeric(12, 2), nullable=False, default=0)
    final_bill_amount = Column(Numeric(12, 2), nullable=False, default=0)
    actual_nights = Column(Integer, nullable=True)
    checked_out_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    reservation = relationship("HotelReservation", back_populates="checkout")


class HotelAdvancePayment(Base):
    """
    Money collected from the customer against a hotel stay before final billing.
    Doc Ref: BRD Part 3 §45 — Advance collection & settlement custody
    Migration: 0035_hotel_payments

    Kept separate from advance_payments because that table is keyed on
    cab_booking_id and permits only one ACTIVE row per booking. A hotel stay
    takes advances repeatedly (deposit at booking, top-ups during the stay),
    so there is deliberately no single-active index here.

    received_by is the custody signal that drives settlement direction, exactly
    as on the cab side: ADMIN means the platform holds the money, PARTNER means
    the hotel partner side does. Voiding flips status rather than deleting, to
    preserve the audit trail.
    """

    __tablename__ = "hotel_advance_payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    hotel_reservation_id = Column(
        BigInteger,
        ForeignKey("hotel_reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    master_booking_id = Column(
        BigInteger,
        ForeignKey("master_bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    receipt_number = Column(String(50), unique=True, nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    payment_mode = Column(String(20), nullable=False)  # CASH | ONLINE | UPI | WALLET
    received_by = Column(String(20), nullable=False, default="ADMIN")  # ADMIN | PARTNER
    reference_number = Column(String(120), nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")  # ACTIVE | VOIDED
    refunded_amount = Column(Numeric(14, 2), nullable=False, default=0)
    collected_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    collected_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    voided_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    void_reason = Column(Text, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    def __repr__(self):
        return (
            f"<HotelAdvancePayment {self.receipt_number} "
            f"{self.amount} {self.received_by} {self.status}>"
        )


__all__ = [
    "HotelCategory",
    "HotelAmenity",
    "HotelGstSlab",
    "Hotel",
    "HotelAmenityMapping",
    "HotelImage",
    "HotelDocument",
    "HotelPolicy",
    "HotelCommissionConfig",
    "HotelRoomCategory",
    "HotelRoomCategoryAmenity",
    "HotelRoomCategoryImage",
    "HotelRoomRatePlan",
    "HotelRoom",
    "HotelInventory",
    "HotelVerificationAssignment",
    "HotelVerificationLog",
    "HotelRating",
    "HotelPerformanceSummary",
    "HotelReservation",
    "HotelReservationSplitEvent",
    "HotelReservationGuest",
    "HotelCheckin",
    "HotelCheckout",
    "HotelAdvancePayment",
]
