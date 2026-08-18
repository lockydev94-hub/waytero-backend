# ============================================================
# WAY TERO — HOTEL SCHEMAS
# File: app/modules/hotel/schemas/__init__.py
# Doc Ref: DB Schema Part 5, API Doc 09_HOTEL_API
#
# Step-by-step onboarding means almost every update field is Optional: an admin
# saves what they have and returns later. The submit gate, not the schema, is
# what enforces completeness.
# ============================================================

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

ORM = ConfigDict(from_attributes=True)


# ============================================================
# MASTERS
# ============================================================


class HotelCategoryCreate(BaseModel):
    category_code: str = Field(..., max_length=50)
    label: str = Field(..., max_length=150)
    description: Optional[str] = None
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    display_order: int = 0
    is_active: bool = True


class HotelCategoryUpdate(BaseModel):
    label: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


class HotelCategoryResponse(BaseModel):
    id: int
    category_code: str
    label: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    display_order: int
    is_active: bool
    model_config = ORM


class HotelAmenityCreate(BaseModel):
    amenity_code: str = Field(..., max_length=50)
    amenity_name: str = Field(..., max_length=100)
    icon_name: Optional[str] = Field(None, max_length=100)
    amenity_group: Optional[str] = Field(None, max_length=50)
    display_order: int = 0
    is_active: bool = True


class HotelAmenityUpdate(BaseModel):
    amenity_name: Optional[str] = Field(None, max_length=100)
    icon_name: Optional[str] = Field(None, max_length=100)
    amenity_group: Optional[str] = Field(None, max_length=50)
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


class HotelAmenityResponse(BaseModel):
    id: int
    amenity_code: str
    amenity_name: str
    icon_name: Optional[str] = None
    amenity_group: Optional[str] = None
    display_order: int
    is_active: bool
    model_config = ORM


class GstSlabCreate(BaseModel):
    slab_name: str = Field(..., max_length=100)
    tariff_from: Decimal
    tariff_to: Optional[Decimal] = None
    gst_percent: Decimal
    has_input_credit: bool = False
    hsn_code: Optional[str] = Field(None, max_length=20)
    effective_from: date
    effective_to: Optional[date] = None
    notes: Optional[str] = None


class GstSlabUpdate(BaseModel):
    slab_name: Optional[str] = Field(None, max_length=100)
    tariff_from: Optional[Decimal] = None
    tariff_to: Optional[Decimal] = None
    gst_percent: Optional[Decimal] = None
    has_input_credit: Optional[bool] = None
    hsn_code: Optional[str] = Field(None, max_length=20)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    notes: Optional[str] = None


class GstSlabResponse(BaseModel):
    id: int
    slab_name: str
    tariff_from: Decimal
    tariff_to: Optional[Decimal] = None
    gst_percent: Decimal
    has_input_credit: bool
    hsn_code: Optional[str] = None
    effective_from: date
    effective_to: Optional[date] = None
    is_active: bool
    notes: Optional[str] = None
    model_config = ORM


# ============================================================
# HOTEL — STAGE 1 CREATE
# ============================================================


class HotelCreate(BaseModel):
    """Stage 1. Deliberately small — an admin should be able to create the
    record and fill in the rest over several sittings."""

    partner_id: int
    hotel_name: str = Field(..., max_length=255)
    hotel_category_id: Optional[int] = None
    hotel_type: Optional[str] = Field(None, max_length=100)
    star_rating: Optional[int] = Field(None, ge=1, le=7)
    city_id: int
    state_id: Optional[int] = None
    address: Optional[str] = None
    contact_person: Optional[str] = Field(None, max_length=255)
    contact_number: Optional[str] = Field(None, max_length=15)
    email: Optional[str] = Field(None, max_length=255)


class HotelProfileUpdate(BaseModel):
    """Stage 2. Every field optional — partial saves are the norm."""

    hotel_name: Optional[str] = Field(None, max_length=255)
    hotel_category_id: Optional[int] = None
    hotel_type: Optional[str] = Field(None, max_length=100)
    star_rating: Optional[int] = Field(None, ge=1, le=7)
    description: Optional[str] = None
    short_description: Optional[str] = Field(None, max_length=500)

    gst_number: Optional[str] = Field(None, max_length=20)
    pan_number: Optional[str] = Field(None, max_length=20)

    city_id: Optional[int] = None
    state_id: Optional[int] = None
    address: Optional[str] = None
    address_line_2: Optional[str] = Field(None, max_length=255)
    landmark: Optional[str] = Field(None, max_length=255)
    postal_code: Optional[str] = Field(None, max_length=20)
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None

    contact_person: Optional[str] = Field(None, max_length=255)
    contact_number: Optional[str] = Field(None, max_length=15)
    alternate_number: Optional[str] = Field(None, max_length=15)
    email: Optional[str] = Field(None, max_length=255)
    website_url: Optional[str] = None

    confirmation_mode: Optional[str] = None
    room_allocation_mode: Optional[str] = None


class HotelSeoUpdate(BaseModel):
    """Powers the customer website's property page."""

    slug: Optional[str] = Field(None, max_length=255)
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    is_featured: Optional[bool] = None
    display_order: Optional[int] = None


class HotelTaxUpdate(BaseModel):
    """tax_mode only takes effect while the global GST_ENABLED config is on."""

    tax_mode: str = Field(..., description="EXCLUSIVE | INCLUSIVE | EXEMPT")
    is_gst_registered: bool = False
    gst_number: Optional[str] = Field(None, max_length=20)


class HotelAmenitiesUpdate(BaseModel):
    amenity_ids: list[int] = Field(default_factory=list)


# ============================================================
# MEDIA & DOCUMENTS
# ============================================================


class HotelImageCreate(BaseModel):
    image_url: str
    thumbnail_url: Optional[str] = None
    caption: Optional[str] = Field(None, max_length=255)
    image_type: str = "GALLERY"
    display_order: int = 0
    is_primary: bool = False


class HotelImageUpdate(BaseModel):
    caption: Optional[str] = Field(None, max_length=255)
    image_type: Optional[str] = None
    display_order: Optional[int] = None
    is_primary: Optional[bool] = None


class HotelImageResponse(BaseModel):
    id: int
    hotel_id: int
    image_url: str
    thumbnail_url: Optional[str] = None
    caption: Optional[str] = None
    image_type: str
    display_order: int
    is_primary: bool
    created_at: datetime
    model_config = ORM


class HotelImagesReorder(BaseModel):
    ordered_ids: list[int] = Field(..., min_length=1)


class HotelDocumentCreate(BaseModel):
    document_type: str = Field(..., max_length=100)
    document_number: Optional[str] = Field(None, max_length=100)
    file_url: str
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None


class HotelDocumentVerify(BaseModel):
    verification_status: str = Field(..., description="VERIFIED | REJECTED")
    remarks: Optional[str] = None


class HotelDocumentResponse(BaseModel):
    id: int
    hotel_id: int
    document_type: str
    document_number: Optional[str] = None
    file_url: str
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    verification_status: str
    remarks: Optional[str] = None
    uploaded_at: datetime
    verified_at: Optional[datetime] = None
    model_config = ORM


# ============================================================
# POLICIES
# ============================================================


class HotelPolicyUpdate(BaseModel):
    check_in_time: Optional[str] = Field(None, max_length=10)
    check_out_time: Optional[str] = Field(None, max_length=10)
    early_check_in_allowed: Optional[bool] = None
    late_check_out_allowed: Optional[bool] = None

    cancellation_free_hours: Optional[int] = None
    refund_percent_tier_1: Optional[Decimal] = None
    cancellation_tier_2_hours: Optional[int] = None
    refund_percent_tier_2: Optional[Decimal] = None
    cancellation_tier_3_hours: Optional[int] = None
    refund_percent_tier_3: Optional[Decimal] = None
    refund_percent_same_day: Optional[Decimal] = None
    no_show_refund_percent: Optional[Decimal] = None

    couples_allowed: Optional[bool] = None
    unmarried_couples_allowed: Optional[bool] = None
    local_id_accepted: Optional[bool] = None
    pets_allowed: Optional[bool] = None
    smoking_allowed: Optional[bool] = None
    alcohol_allowed: Optional[bool] = None
    min_guest_age: Optional[int] = None
    extra_bed_charge: Optional[Decimal] = None
    child_free_age_limit: Optional[int] = None
    house_rules: Optional[str] = None
    cancellation_policy_text: Optional[str] = None


class HotelPolicyResponse(BaseModel):
    id: int
    hotel_id: int
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    early_check_in_allowed: bool
    late_check_out_allowed: bool
    cancellation_free_hours: Optional[int] = None
    refund_percent_tier_1: Optional[Decimal] = None
    cancellation_tier_2_hours: Optional[int] = None
    refund_percent_tier_2: Optional[Decimal] = None
    cancellation_tier_3_hours: Optional[int] = None
    refund_percent_tier_3: Optional[Decimal] = None
    refund_percent_same_day: Optional[Decimal] = None
    no_show_refund_percent: Optional[Decimal] = None
    couples_allowed: bool
    unmarried_couples_allowed: bool
    local_id_accepted: bool
    pets_allowed: bool
    smoking_allowed: bool
    alcohol_allowed: bool
    min_guest_age: Optional[int] = None
    extra_bed_charge: Optional[Decimal] = None
    child_free_age_limit: Optional[int] = None
    house_rules: Optional[str] = None
    cancellation_policy_text: Optional[str] = None
    model_config = ORM


# ============================================================
# COMMISSION
# ============================================================


class HotelCommissionCreate(BaseModel):
    commission_type: str = Field(..., description="PERCENTAGE | FLAT | HYBRID")
    commission_percent: Decimal = Decimal("0")
    commission_flat: Decimal = Decimal("0")
    min_commission: Optional[Decimal] = None
    max_commission: Optional[Decimal] = None
    applies_to: str = "PER_BOOKING"
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    remarks: Optional[str] = None


class HotelCommissionResponse(BaseModel):
    id: int
    hotel_id: int
    commission_type: str
    commission_percent: Decimal
    commission_flat: Decimal
    min_commission: Optional[Decimal] = None
    max_commission: Optional[Decimal] = None
    applies_to: str
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: bool
    remarks: Optional[str] = None
    created_at: datetime
    model_config = ORM


class ResolvedCommissionResponse(BaseModel):
    """What would actually be charged, and where the number came from — so an
    admin can tell an override from an inherited default."""

    commission_type: str
    commission_percent: Decimal
    commission_flat: Decimal
    applies_to: str
    source: str
    min_commission: Optional[Decimal] = None
    max_commission: Optional[Decimal] = None
    config_id: Optional[int] = None
    example: Optional[dict[str, Any]] = None


# ============================================================
# ROOM CATEGORIES
# ============================================================


class RoomCategoryCreate(BaseModel):
    category_name: str = Field(..., max_length=150)
    room_type: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None

    base_occupancy: int = 2
    max_adults: int = 2
    max_children: int = 1
    max_occupancy: int = 3
    extra_bed_allowed: bool = False
    extra_bed_charge: Decimal = Decimal("0")
    extra_adult_charge: Decimal = Decimal("0")
    extra_child_charge: Decimal = Decimal("0")

    bed_type: Optional[str] = Field(None, max_length=100)
    room_size_sqft: Optional[int] = None
    view_type: Optional[str] = Field(None, max_length=100)
    floor_range: Optional[str] = Field(None, max_length=50)

    base_price: Decimal = Decimal("0")
    published_price: Optional[Decimal] = None
    min_sellable_price: Optional[Decimal] = None
    meal_plan: str = "EP"
    is_refundable: bool = True

    total_rooms: int = 0
    display_order: int = 0
    amenity_ids: list[int] = Field(default_factory=list)


class RoomCategoryUpdate(BaseModel):
    category_name: Optional[str] = Field(None, max_length=150)
    room_type: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    base_occupancy: Optional[int] = None
    max_adults: Optional[int] = None
    max_children: Optional[int] = None
    max_occupancy: Optional[int] = None
    extra_bed_allowed: Optional[bool] = None
    extra_bed_charge: Optional[Decimal] = None
    extra_adult_charge: Optional[Decimal] = None
    extra_child_charge: Optional[Decimal] = None
    bed_type: Optional[str] = Field(None, max_length=100)
    room_size_sqft: Optional[int] = None
    view_type: Optional[str] = Field(None, max_length=100)
    floor_range: Optional[str] = Field(None, max_length=50)
    base_price: Optional[Decimal] = None
    published_price: Optional[Decimal] = None
    min_sellable_price: Optional[Decimal] = None
    meal_plan: Optional[str] = None
    is_refundable: Optional[bool] = None
    total_rooms: Optional[int] = None
    display_order: Optional[int] = None
    is_active: Optional[bool] = None
    amenity_ids: Optional[list[int]] = None


class RoomCategoryImageCreate(BaseModel):
    image_url: str
    caption: Optional[str] = Field(None, max_length=255)
    display_order: int = 0
    is_primary: bool = False


class RoomCategoryImageResponse(BaseModel):
    id: int
    room_category_id: int
    image_url: str
    caption: Optional[str] = None
    display_order: int
    is_primary: bool
    model_config = ORM


class RoomCategoryResponse(BaseModel):
    id: int
    hotel_id: int
    category_name: str
    room_type: Optional[str] = None
    description: Optional[str] = None
    base_occupancy: int
    max_adults: int
    max_children: int
    max_occupancy: int
    extra_bed_allowed: bool
    extra_bed_charge: Decimal
    extra_adult_charge: Decimal
    extra_child_charge: Decimal
    bed_type: Optional[str] = None
    room_size_sqft: Optional[int] = None
    view_type: Optional[str] = None
    floor_range: Optional[str] = None
    base_price: Decimal
    published_price: Optional[Decimal] = None
    min_sellable_price: Optional[Decimal] = None
    meal_plan: str
    is_refundable: bool
    total_rooms: int
    display_order: int
    is_active: bool
    images: list[RoomCategoryImageResponse] = Field(default_factory=list)
    amenity_ids: list[int] = Field(default_factory=list)
    rate_plan_count: int = 0
    physical_room_count: int = 0
    model_config = ORM


# ============================================================
# PHYSICAL ROOMS
# ============================================================


class RoomCreate(BaseModel):
    room_category_id: int
    room_number: str = Field(..., max_length=50)
    floor_number: Optional[str] = Field(None, max_length=20)
    remarks: Optional[str] = None


class BulkRoomCreate(BaseModel):
    """Generate room_number_prefix + a padded counter, e.g. 101..120."""

    room_category_id: int
    prefix: str = Field("", max_length=20)
    start_number: int = Field(..., ge=0)
    count: int = Field(..., ge=1, le=500)
    floor_number: Optional[str] = Field(None, max_length=20)
    pad_width: int = Field(0, ge=0, le=6)

    # Clients legitimately send `null` for "no prefix" / "no padding" rather
    # than omitting the key. Without this the request 422s on a field the
    # caller was trying to leave at its default.
    @field_validator("prefix", mode="before")
    @classmethod
    def _prefix_default(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("pad_width", mode="before")
    @classmethod
    def _pad_width_default(cls, v: Any) -> Any:
        return 0 if v is None else v

    # Floors are a string column ("G", "LG", "12"); accept a bare number too.
    @field_validator("floor_number", mode="before")
    @classmethod
    def _floor_to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, (int, float)) else v


class RoomUpdate(BaseModel):
    room_number: Optional[str] = Field(None, max_length=50)
    floor_number: Optional[str] = Field(None, max_length=20)
    room_status: Optional[str] = None
    remarks: Optional[str] = None
    is_active: Optional[bool] = None


class RoomActiveReservation(BaseModel):
    """The live booking currently holding a physical room. Present only while the
    reservation is CHECKED_IN or IN_HOUSE; cleared automatically at check-out."""

    reservation_id: int
    reservation_number: Optional[str] = None
    guest_name: Optional[str] = None
    check_out_date: Optional[date] = None
    status: str


class RoomResponse(BaseModel):
    id: int
    hotel_id: int
    room_category_id: int
    room_number: str
    floor_number: Optional[str] = None
    room_status: str
    remarks: Optional[str] = None
    is_active: bool
    active_reservation: Optional[RoomActiveReservation] = None
    model_config = ORM


# ============================================================
# RATE PLANS
# ============================================================


class RatePlanCreate(BaseModel):
    room_category_id: int
    plan_name: str = Field(..., max_length=150)
    plan_type: str = Field(
        ..., description="WEEKEND | SEASONAL | FESTIVAL | PROMOTIONAL"
    )
    priority: int = 0
    date_from: date
    date_to: date
    day_of_week_mask: Optional[str] = Field(None, max_length=20)
    rate_mode: str = "ABSOLUTE"
    rate_value: Decimal
    min_nights: int = 1


class RatePlanUpdate(BaseModel):
    plan_name: Optional[str] = Field(None, max_length=150)
    plan_type: Optional[str] = None
    priority: Optional[int] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    day_of_week_mask: Optional[str] = Field(None, max_length=20)
    rate_mode: Optional[str] = None
    rate_value: Optional[Decimal] = None
    min_nights: Optional[int] = None
    is_active: Optional[bool] = None


class RatePlanResponse(BaseModel):
    id: int
    hotel_id: int
    room_category_id: int
    plan_name: str
    plan_type: str
    priority: int
    date_from: date
    date_to: date
    day_of_week_mask: Optional[str] = None
    rate_mode: str
    rate_value: Decimal
    min_nights: int
    is_active: bool
    created_at: datetime
    model_config = ORM


class RatePreviewNight(BaseModel):
    """One night of the preview calendar. plan_name is the point — it makes
    rate-plan precedence legible instead of something to be guessed at."""

    date: date
    rate: Decimal
    source: str
    plan_id: Optional[int] = None
    plan_name: Optional[str] = None
    gst_percent: Decimal = Decimal("0")
    gst_amount: Decimal = Decimal("0")
    total_with_tax: Decimal = Decimal("0")


class RatePreviewResponse(BaseModel):
    room_category_id: int
    date_from: date
    date_to: date
    base_price: Decimal
    tax_mode: str
    platform_gst_enabled: bool
    nights: list[RatePreviewNight] = Field(default_factory=list)
    total: Decimal = Decimal("0")
    total_with_tax: Decimal = Decimal("0")


# ============================================================
# INVENTORY
# ============================================================


class InventoryGenerate(BaseModel):
    room_category_id: Optional[int] = None  # None = every category
    date_from: date
    date_to: date


class InventoryUpdate(BaseModel):
    room_category_id: int
    date_from: date
    date_to: date
    days_of_week: Optional[list[int]] = Field(
        None,
        description="0=Monday … 6=Sunday. Omit to apply to every date in range.",
    )
    total_rooms: Optional[int] = None
    blocked_rooms: Optional[int] = None
    rate_override: Optional[Decimal] = None
    is_stop_sell: Optional[bool] = None


class InventoryResponse(BaseModel):
    id: int
    hotel_id: int
    room_category_id: int
    inventory_date: date
    total_rooms: int
    booked_rooms: int
    blocked_rooms: int
    held_rooms: int
    available_rooms: int
    rate_override: Optional[Decimal] = None
    is_stop_sell: bool
    model_config = ORM


# ============================================================
# VERIFICATION
# ============================================================


class AssignOfficerRequest(BaseModel):
    officer_id: UUID
    notes: Optional[str] = None


class ApproveHotelRequest(BaseModel):
    """own_risk skips officer verification. It is recorded permanently on the
    hotel so the decision is attributable later."""

    own_risk: bool = False
    remarks: Optional[str] = None


class RejectHotelRequest(BaseModel):
    reason: str = Field(..., min_length=3)


class StatusChangeRequest(BaseModel):
    # The target status is implied by the endpoint (/suspend, /block, …), so it
    # is accepted but unused. Required here would force clients to send a value
    # no handler reads.
    status: Optional[str] = None
    remarks: Optional[str] = None


class VerificationAssignmentResponse(BaseModel):
    id: int
    hotel_id: int
    officer_id: UUID
    assigned_by: UUID
    assigned_at: datetime
    unassigned_at: Optional[datetime] = None
    is_active: bool
    notes: Optional[str] = None
    officer_name: Optional[str] = None
    officer_mobile: Optional[str] = None
    model_config = ORM


class VerificationLogResponse(BaseModel):
    id: int
    hotel_id: int
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    remarks: Optional[str] = None
    performed_by: Optional[UUID] = None
    performed_by_name: Optional[str] = None
    created_at: datetime
    model_config = ORM


# ============================================================
# READINESS
# ============================================================


class ReadinessCheck(BaseModel):
    key: str
    label: str
    passed: bool
    blocks_submit: bool
    blocks_approve: bool
    hint: Optional[str] = None


class ReadinessResponse(BaseModel):
    """One server-side computation drives both the submit gate and the UI
    completeness meter, so the two can never disagree."""

    hotel_id: int
    status: str
    completeness_percent: int
    can_submit: bool
    can_approve: bool
    checks: list[ReadinessCheck] = Field(default_factory=list)


# ============================================================
# HOTEL RESPONSES
# ============================================================


class HotelListItem(BaseModel):
    id: int
    uuid: UUID
    hotel_code: str
    hotel_name: str
    status: str
    star_rating: Optional[int] = None
    city_id: int
    city_name: Optional[str] = None
    state_name: Optional[str] = None
    partner_id: int
    partner_name: Optional[str] = None
    hotel_category_id: Optional[int] = None
    category_label: Optional[str] = None
    total_rooms: int
    room_category_count: int = 0
    primary_image_url: Optional[str] = None
    is_own_risk_approved: bool = False
    is_featured: bool = False
    completeness_percent: int = 0
    assigned_officer_name: Optional[str] = None
    created_at: datetime
    model_config = ORM


class HotelDetailResponse(BaseModel):
    id: int
    uuid: UUID
    hotel_code: str
    partner_id: int
    partner_name: Optional[str] = None
    hotel_name: str
    hotel_type: Optional[str] = None
    hotel_category_id: Optional[int] = None
    category_label: Optional[str] = None
    star_rating: Optional[int] = None
    description: Optional[str] = None
    short_description: Optional[str] = None

    gst_number: Optional[str] = None
    pan_number: Optional[str] = None

    city_id: int
    city_name: Optional[str] = None
    state_id: Optional[int] = None
    state_name: Optional[str] = None
    address: Optional[str] = None
    address_line_2: Optional[str] = None
    landmark: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None

    contact_person: Optional[str] = None
    contact_number: Optional[str] = None
    alternate_number: Optional[str] = None
    email: Optional[str] = None
    website_url: Optional[str] = None

    total_rooms: int
    confirmation_mode: str
    room_allocation_mode: str

    tax_mode: str
    is_gst_registered: bool
    platform_gst_enabled: bool = False

    slug: Optional[str] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    is_featured: bool
    display_order: int

    status: str
    rejection_reason: Optional[str] = None
    submitted_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    activated_at: Optional[datetime] = None
    is_own_risk_approved: bool

    amenity_ids: list[int] = Field(default_factory=list)
    images: list[HotelImageResponse] = Field(default_factory=list)
    documents: list[HotelDocumentResponse] = Field(default_factory=list)
    policy: Optional[HotelPolicyResponse] = None
    commission: Optional[ResolvedCommissionResponse] = None
    assigned_officer: Optional[VerificationAssignmentResponse] = None
    readiness: Optional[ReadinessResponse] = None
    allowed_transitions: list[str] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime
    model_config = ORM


class HotelStatsResponse(BaseModel):
    total: int = 0
    draft: int = 0
    pending: int = 0
    under_review: int = 0
    document_pending: int = 0
    approved: int = 0
    active: int = 0
    rejected: int = 0
    inactive: int = 0
    suspended: int = 0
    blocked: int = 0
    own_risk_approved: int = 0
    unassigned_in_pipeline: int = 0
