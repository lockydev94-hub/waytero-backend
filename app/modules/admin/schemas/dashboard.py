# ============================================================
# WAYTERO — ADMIN DASHBOARD SCHEMAS (Pydantic v2)
# File: app/modules/admin/schemas/dashboard.py
# Doc Ref: Admin API §3 — Dashboard, §4 — System Summary,
#          §5 — Users, §8 — Partners, §9 — Drivers,
#          §10 — Vehicles, §11 — Bookings, §16 — Settlements,
#          §23 — Reports, §24 — Audit Logs
# ============================================================

from __future__ import annotations
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Any, Dict
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field, field_validator


# ── Dashboard KPIs ────────────────────────────────────────────────────────────


class DashboardStats(BaseModel):
    today_cab_bookings: int = 0
    today_hotel_reservations: int = 0
    active_trips: int = 0
    today_revenue: Decimal = Decimal("0")
    pending_settlements: Decimal = Decimal("0")
    pending_partner_approvals: int = 0


class SystemSummary(BaseModel):
    total_customers: int = 0
    total_partners: int = 0
    active_partners: int = 0
    active_hotels: int = 0
    total_hotels: int = 0
    monthly_revenue: Decimal = Decimal("0")
    pending_hotel_approvals: int = 0


class CityPerformanceItem(BaseModel):
    city: str
    bookings: int


class RevenueTrendItem(BaseModel):
    month: str
    revenue: float
    bookings: int


class RecentActivityItem(BaseModel):
    id: str
    type: str
    label: str
    time: str
    status: str


# ── User Management ───────────────────────────────────────────────────────────


class AdminUserOut(BaseModel):
    id: UUID
    user_code: Optional[str]
    first_name: str
    last_name: Optional[str]
    mobile_number: str
    email: Optional[str]
    user_type: Optional[str]
    status: Optional[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminUserListResponse(BaseModel):
    items: List[AdminUserOut]
    total: int
    page: int
    page_size: int


class UserStatusUpdate(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


# ── Partner Management ────────────────────────────────────────────────────────


class AdminPartnerOut(BaseModel):
    id: int
    partner_code: Optional[str]
    partner_type: Optional[str]  # INDIVIDUAL | COMPANY
    business_name: Optional[str]
    owner_name: Optional[str]  # aliased from contact_person in SQL
    contact_person: Optional[str]  # kept for backward compat
    mobile: Optional[str]  # aliased from mobile_number in SQL
    mobile_number: Optional[str]  # kept for backward compat
    email: Optional[str]
    status: str
    city_id: Optional[int]
    has_cab_service: Optional[bool] = None  # True if CAB service is linked
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class AdminPartnerListResponse(BaseModel):
    items: List[AdminPartnerOut]
    total: int
    page: int
    page_size: int


class PartnerActionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


# ── Driver Management ─────────────────────────────────────────────────────────


class AdminDriverOut(BaseModel):
    id: int
    driver_code: Optional[str]
    full_name: Optional[str]
    mobile_number: Optional[str]
    license_number: Optional[str]
    status: str
    city_id: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminDriverCreate(BaseModel):
    """Admin creates a driver under a specific partner."""

    partner_id: int
    full_name: str = Field(..., max_length=255)
    mobile: str
    email: Optional[str] = None
    license_number: str = Field(..., max_length=100)
    license_expiry_date: Optional[str] = None  # ISO date string
    date_of_birth: Optional[str] = None
    joining_date: Optional[str] = None
    address: Optional[str] = None
    # Gov ID
    gov_id_type: Optional[str] = None  # AADHAAR | PAN | PASSPORT | VOTER_ID
    gov_id_number: Optional[str] = None


class AdminDriverDocumentOut(BaseModel):
    id: int
    document_type: str
    file_url: str
    verification_status: Optional[str]
    expiry_date: Optional[str]
    remarks: Optional[str]
    uploaded_at: datetime
    model_config = {"from_attributes": True}


class AdminDriverDetail(BaseModel):
    """Full driver detail for admin drawer view."""

    id: int
    driver_code: str
    partner_id: int
    full_name: str
    mobile_number: Optional[str]
    email: Optional[str]
    license_number: Optional[str]
    license_expiry_date: Optional[str]
    date_of_birth: Optional[str]
    joining_date: Optional[str]
    status: str
    approved_at: Optional[datetime]
    created_at: datetime
    documents: List[AdminDriverDocumentOut] = []
    availability_status: Optional[str] = None
    completed_trips: Optional[int] = None
    average_rating: Optional[float] = None
    model_config = {"from_attributes": True}


class AdminDriverListResponse(BaseModel):
    items: List[AdminDriverOut]
    total: int
    page: int
    page_size: int


class AdminDriverDocumentCreate(BaseModel):
    """Admin uploads a document (including PHOTO) for a driver."""

    document_type: str = Field(
        ...,
        description="DRIVING_LICENSE | AADHAAR | PAN | PHOTO | POLICE_VERIFICATION | MEDICAL_CERTIFICATE",
    )
    file_url: str = Field(
        ...,
        description="Cloudinary secure_url after upload via /admin/settings/upload-media",
    )
    expiry_date: Optional[str] = None  # ISO date string, optional


class DriverActionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


# ── Vehicle Management ────────────────────────────────────────────────────────


class AdminVehicleOut(BaseModel):
    id: int
    vehicle_code: Optional[str]
    registration_number: Optional[str]
    make: Optional[str]
    model: Optional[str]
    vehicle_category_id: Optional[int]
    vehicle_category: Optional[str] = (
        None  # category_name joined from vehicle_categories
    )
    status: Optional[str]
    partner_id: Optional[int]
    partner_name: Optional[str] = None  # owner_name joined from partners
    created_at: datetime

    model_config = {"from_attributes": True, "extra": "allow"}


class AdminVehicleListResponse(BaseModel):
    items: List[AdminVehicleOut]
    total: int
    page: int
    page_size: int


class VehicleActionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


# ── Booking Operations ────────────────────────────────────────────────────────


class AdminBookingOut(BaseModel):
    id: int
    uuid: Optional[UUID] = None
    booking_number: Optional[str] = None
    booking_status: str
    payment_status: str
    total_amount: Optional[Decimal] = None
    journey_start_date: Optional[date] = None
    journey_end_date: Optional[date] = None
    city_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_mobile: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminBookingListResponse(BaseModel):
    items: List[AdminBookingOut]
    total: int
    page: int
    page_size: int


class BookingCancelRequest(BaseModel):
    reason: str = Field(..., max_length=500)


class ManualAssignRequest(BaseModel):
    partner_id: Optional[int] = None
    driver_id: Optional[int] = None


# ── Settlement ────────────────────────────────────────────────────────────────


class AdminSettlementOut(BaseModel):
    id: int
    settlement_number: Optional[str]
    partner_id: Optional[int]
    net_payable_amount: Optional[Decimal]
    status: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class AdminSettlementListResponse(BaseModel):
    items: List[AdminSettlementOut]
    total: int
    page: int
    page_size: int


# ── Audit Logs ────────────────────────────────────────────────────────────────


class AuditLogOut(BaseModel):
    id: int
    user_id: Optional[int]
    module_name: str
    entity_name: Optional[str]
    entity_id: Optional[int]
    action_type: str
    old_values: Optional[Dict[str, Any]]
    new_values: Optional[Dict[str, Any]]
    ip_address: Optional[str]
    user_agent: Optional[str] = None
    request_id: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditLogListResponse(BaseModel):
    items: List[AuditLogOut]
    total: int
    page: int
    page_size: int


class AuditLogSummary(BaseModel):
    """
    Aggregate counts for the audit dashboard header strip.
    All counts are best-effort — they may race with concurrent writes, but
    the admin view doesn't need a transactional snapshot.
    """

    total: int = 0
    today: int = 0
    this_week: int = 0
    unique_users_today: int = 0
    by_module: Dict[str, int] = Field(default_factory=dict)
    by_action_type: Dict[str, int] = Field(default_factory=dict)


# ── Reports ───────────────────────────────────────────────────────────────────


class RevenueReport(BaseModel):
    period: str
    total_revenue: Decimal
    total_bookings: int
    avg_booking_value: Decimal


class BookingReport(BaseModel):
    period: str
    total_bookings: int
    completed: int
    cancelled: int
    pending: int


class PartnerReport(BaseModel):
    total_partners: int
    active: int
    pending: int
    suspended: int


class SettlementReport(BaseModel):
    total_settlements: int
    pending_amount: Decimal
    paid_amount: Decimal
    pending_count: int


# ── Notifications Broadcast ───────────────────────────────────────────────────


class BroadcastRequest(BaseModel):
    channel: str = Field(..., pattern="^(SMS|WHATSAPP|EMAIL|PUSH|IN_APP)$")
    message: str = Field(..., min_length=1, max_length=1000)
    title: Optional[str] = Field(None, max_length=255)


class BroadcastResponse(BaseModel):
    success: bool = True
    message: str
    queued_count: int = 0


# ── Generic Admin Response ────────────────────────────────────────────────────


class AdminActionResponse(BaseModel):
    success: bool = True
    message: str


# ── Staff User Management ──────────────────────────────────────────────────────
# Doc Ref: BRD Part 2 §12 User Types — Admin, CCO, Verification Officer, Finance Manager
# Doc Ref: BRD Part 8 §196 Admin User Types

STAFF_ROLES = {"ADMIN", "CCO", "VERIFICATION_OFFICER", "FINANCE_MANAGER"}


# ── Staff Profile (extended fields) ──────────────────────────────────────────
# Doc Ref: DB 0014_staff_profiles | Employee Lifecycle §13


class StaffProfileCreate(BaseModel):
    """Extended profile data for staff member — personal, employment, address, bank, nominee."""

    # Personal
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(
        None, description="MALE | FEMALE | OTHER | PREFER_NOT_TO_SAY"
    )
    blood_group: Optional[str] = Field(
        None, description="A+ | A- | B+ | B- | AB+ | AB- | O+ | O-"
    )
    personal_email: Optional[str] = None
    emergency_contact_name: Optional[str] = Field(None, max_length=150)
    emergency_contact_phone: Optional[str] = Field(None, max_length=20)

    # Employment
    designation: Optional[str] = Field(None, max_length=150)
    department: Optional[str] = Field(None, max_length=150)
    employment_type: Optional[str] = Field(
        "FULL_TIME", description="FULL_TIME | PART_TIME | CONTRACT | INTERN"
    )
    joining_date: Optional[date] = None

    # Address
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    pincode: Optional[str] = Field(None, max_length=10)
    country: Optional[str] = Field("India", max_length=100)

    # Identity Documents
    pan_number: Optional[str] = Field(None, max_length=20)
    aadhar_number: Optional[str] = Field(None, max_length=20)
    driving_license_number: Optional[str] = Field(None, max_length=30)

    # Bank Details
    bank_name: Optional[str] = Field(None, max_length=150)
    bank_account_number: Optional[str] = Field(None, max_length=30)
    bank_ifsc_code: Optional[str] = Field(None, max_length=20)
    bank_branch: Optional[str] = Field(None, max_length=150)
    bank_account_type: Optional[str] = Field(None, description="SAVINGS | CURRENT")

    # Nominee
    nominee_name: Optional[str] = Field(None, max_length=150)
    nominee_relation: Optional[str] = Field(None, max_length=50)
    nominee_phone: Optional[str] = Field(None, max_length=20)
    nominee_address: Optional[str] = None

    # ID Card
    id_card_notes: Optional[str] = None


class StaffProfileOut(BaseModel):
    """Extended staff profile response."""

    id: Optional[str] = None
    user_id: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    blood_group: Optional[str] = None
    personal_email: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    employment_type: Optional[str] = None
    joining_date: Optional[date] = None
    employee_id: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    country: Optional[str] = None
    pan_number: Optional[str] = None
    aadhar_number: Optional[str] = None
    driving_license_number: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc_code: Optional[str] = None
    bank_branch: Optional[str] = None
    bank_account_type: Optional[str] = None
    nominee_name: Optional[str] = None
    nominee_relation: Optional[str] = None
    nominee_phone: Optional[str] = None
    nominee_address: Optional[str] = None
    id_card_status: Optional[str] = None
    id_card_generated_at: Optional[datetime] = None
    id_card_notes: Optional[str] = None

    model_config = {"from_attributes": True}


class StaffUserCreate(BaseModel):
    """Schema to create an internal staff member by Super Admin."""

    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    email: EmailStr
    mobile_number: str = Field(..., min_length=10, max_length=20)
    password: str = Field(..., min_length=8, max_length=128)
    user_type: str = Field(
        ..., description="ADMIN | CCO | VERIFICATION_OFFICER | FINANCE_MANAGER"
    )
    profile_image_url: Optional[str] = None
    # Extended profile — passed inline during creation
    profile: Optional[StaffProfileCreate] = None

    @field_validator("user_type")
    @classmethod
    def validate_staff_role(cls, v: str) -> str:
        if v not in STAFF_ROLES:
            raise ValueError(f"user_type must be one of {sorted(STAFF_ROLES)}")
        return v


class StaffUserUpdate(BaseModel):
    """Partial update for a staff member."""

    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = None
    mobile_number: Optional[str] = Field(None, min_length=10, max_length=20)
    user_type: Optional[str] = None
    profile_image_url: Optional[str] = None
    profile: Optional[StaffProfileCreate] = None

    @field_validator("user_type")
    @classmethod
    def validate_staff_role(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in STAFF_ROLES:
            raise ValueError(f"user_type must be one of {sorted(STAFF_ROLES)}")
        return v


class StaffUserOut(BaseModel):
    """Staff member response schema — includes extended profile."""

    id: UUID
    user_code: Optional[str]
    first_name: str
    last_name: Optional[str]
    email: Optional[str]
    mobile_number: str
    user_type: str
    status: str
    is_active: bool
    profile_image_url: Optional[str]
    created_at: datetime
    last_login_at: Optional[datetime]
    # Extended profile (None if not yet created)
    profile: Optional[StaffProfileOut] = None

    model_config = {"from_attributes": True}


class StaffUserListResponse(BaseModel):
    items: List[StaffUserOut]
    total: int
    page: int
    page_size: int


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)


class StaffIdCardGenerateRequest(BaseModel):
    notes: Optional[str] = Field(
        None, description="ID card instructions / printing notes"
    )


# ── Staff Document Upload ──────────────────────────────────────
STAFF_DOC_TYPES = [
    "PHOTO",
    "ID_CARD_FRONT",
    "ID_CARD_BACK",
    "PAN_CARD",
    "AADHAR_CARD",
    "OFFER_LETTER",
    "DRIVING_LICENSE",
    "OTHER",
]


class StaffDocumentOut(BaseModel):
    """One uploaded document for a staff member."""

    id: str
    document_type: str
    document_name: Optional[str] = None
    file_url: str
    file_type: Optional[str] = None
    is_verified: bool = False
    notes: Optional[str] = None
    uploaded_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
