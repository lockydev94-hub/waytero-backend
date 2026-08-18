# ============================================================
# WAYTERO — ADMIN / SETTINGS SCHEMAS (Pydantic v2)
# File: app/modules/admin/schemas/__init__.py
# Doc Ref: Backend Architecture §2 FastAPI Structure
# ============================================================
from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Any, Dict
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


# ── System Configuration ──────────────────────────────────────────────────────


class SystemConfigOut(BaseModel):
    id: int
    config_key: str
    config_value: Optional[str]
    description: Optional[str]
    updated_at: datetime

    model_config = {"from_attributes": True}


class SystemConfigUpdate(BaseModel):
    config_value: str = Field(..., description="New value for this configuration key")
    description: Optional[str] = None


class SystemConfigBulkUpdate(BaseModel):
    configs: List[SystemConfigUpdate]


# ── App Version ───────────────────────────────────────────────────────────────


class AppVersionOut(BaseModel):
    id: int
    platform: str
    version: str
    is_force_update: bool
    release_notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class AppVersionCreate(BaseModel):
    platform: str = Field(..., pattern="^(ANDROID|IOS)$")
    version: str
    is_force_update: bool = False
    release_notes: Optional[str] = None


class AppVersionUpdate(BaseModel):
    version: Optional[str] = None
    is_force_update: Optional[bool] = None
    release_notes: Optional[str] = None


# ── Commission Group ──────────────────────────────────────────────────────────


class CommissionRuleOut(BaseModel):
    id: int
    service_type: Optional[str]
    commission_type: Optional[str]
    commission_value: Optional[Decimal]
    city_id: Optional[int]
    effective_from: Optional[date]
    effective_to: Optional[date]
    is_active: bool

    model_config = {"from_attributes": True}


class CommissionGroupOut(BaseModel):
    id: int
    group_name: str
    description: Optional[str]
    is_active: bool
    created_at: datetime
    rules: List[CommissionRuleOut] = []

    model_config = {"from_attributes": True}


class CommissionGroupCreate(BaseModel):
    group_name: str = Field(..., max_length=150)
    description: Optional[str] = None
    is_active: bool = True


class CommissionGroupUpdate(BaseModel):
    group_name: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class CommissionRuleCreate(BaseModel):
    service_type: str = Field(
        ...,
        description="Service type code (e.g. CAB, HOTEL, TOUR) — validated dynamically against DB",
    )
    commission_type: str = Field(..., pattern="^(PERCENTAGE|FLAT)$")
    commission_value: Decimal = Field(..., ge=0)
    city_id: Optional[int] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: bool = True


class CommissionRuleUpdate(BaseModel):
    commission_type: Optional[str] = Field(None, pattern="^(PERCENTAGE|FLAT)$")
    commission_value: Optional[Decimal] = Field(None, ge=0)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: Optional[bool] = None


# ── Vehicle Pricing Rule ──────────────────────────────────────────────────────


# Driver-allowance evaluation mode (migration 0049). PER_TRIP is the legacy
# flat behaviour; PER_DAY scales by trip_days (pickup → return), PER_KM by
# billable km, NONE suppresses the line entirely.
_DA_TYPES = ("PER_TRIP", "PER_DAY", "PER_KM", "NONE")
_NIGHT_CT_TYPES = ("FIXED", "PERCENTAGE", "PER_KM")
_WAIT_GRANULARITIES = ("PER_MINUTE", "PER_15_MINUTES", "PER_30_MINUTES", "PER_HOUR")


class VehiclePricingRuleOut(BaseModel):
    id: int
    city_id: int
    vehicle_category_id: int
    trip_type: Optional[str]
    base_fare: Optional[Decimal]
    minimum_km: Optional[int]
    per_km_rate: Optional[Decimal]
    driver_allowance: Optional[Decimal]
    night_charge: Optional[Decimal]
    driver_allowance_type: str = "PER_TRIP"
    night_charge_type: str = "FIXED"
    toll: Optional[Decimal] = Decimal("0")
    free_waiting_minutes: Optional[int] = 0
    actual_waiting_minutes: Optional[int] = 0
    waiting_rate_per_hour: Optional[Decimal] = Decimal("0")
    waiting_granularity: str = "PER_15_MINUTES"
    effective_from: Optional[date]
    effective_to: Optional[date]

    model_config = {"from_attributes": True}


class VehiclePricingRuleCreate(BaseModel):
    city_id: int
    vehicle_category_id: int
    trip_type: str = Field(
        ..., pattern="^(LOCAL|AIRPORT|OUTSTATION|ONE_WAY|ROUND_TRIP)$"
    )
    base_fare: Decimal = Field(..., ge=0)
    minimum_km: Optional[int] = Field(None, ge=0)
    per_km_rate: Decimal = Field(..., ge=0)
    driver_allowance: Optional[Decimal] = Field(None, ge=0)
    night_charge: Optional[Decimal] = Field(None, ge=0)
    driver_allowance_type: str = Field(
        "PER_TRIP", pattern="^(PER_TRIP|PER_DAY|PER_KM|NONE)$"
    )
    night_charge_type: str = Field("FIXED", pattern="^(FIXED|PERCENTAGE|PER_KM)$")
    toll: Optional[Decimal] = Field(Decimal("0"), ge=0)
    free_waiting_minutes: Optional[int] = Field(0, ge=0)
    actual_waiting_minutes: Optional[int] = Field(0, ge=0)
    waiting_rate_per_hour: Optional[Decimal] = Field(Decimal("0"), ge=0)
    waiting_granularity: str = Field(
        "PER_15_MINUTES",
        pattern="^(PER_MINUTE|PER_15_MINUTES|PER_30_MINUTES|PER_HOUR)$",
    )
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class VehiclePricingRuleUpdate(BaseModel):
    base_fare: Optional[Decimal] = Field(None, ge=0)
    minimum_km: Optional[int] = Field(None, ge=0)
    per_km_rate: Optional[Decimal] = Field(None, ge=0)
    driver_allowance: Optional[Decimal] = Field(None, ge=0)
    night_charge: Optional[Decimal] = Field(None, ge=0)
    driver_allowance_type: Optional[str] = Field(
        None, pattern="^(PER_TRIP|PER_DAY|PER_KM|NONE)$"
    )
    night_charge_type: Optional[str] = Field(
        None, pattern="^(FIXED|PERCENTAGE|PER_KM)$"
    )
    toll: Optional[Decimal] = Field(None, ge=0)
    free_waiting_minutes: Optional[int] = Field(None, ge=0)
    actual_waiting_minutes: Optional[int] = Field(None, ge=0)
    waiting_rate_per_hour: Optional[Decimal] = Field(None, ge=0)
    waiting_granularity: Optional[str] = Field(
        None, pattern="^(PER_MINUTE|PER_15_MINUTES|PER_30_MINUTES|PER_HOUR)$"
    )
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


# ── Notification Template ─────────────────────────────────────────────────────


class NotificationTemplateOut(BaseModel):
    id: int
    template_code: Optional[str]
    channel: Optional[str]
    template_name: Optional[str]
    template_content: Optional[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationTemplateCreate(BaseModel):
    template_code: str = Field(..., max_length=100)
    channel: str = Field(..., pattern="^(SMS|EMAIL|PUSH|IN_APP)$")
    template_name: str = Field(..., max_length=255)
    template_content: str
    is_active: bool = True


class NotificationTemplateUpdate(BaseModel):
    template_name: Optional[str] = Field(None, max_length=255)
    template_content: Optional[str] = None
    is_active: Optional[bool] = None


# ── Generic Response ──────────────────────────────────────────────────────────


class SettingsResponse(BaseModel):
    success: bool = True
    message: str


# ── API Integration ───────────────────────────────────────────────────────────


class ApiIntegrationOut(BaseModel):
    id: int
    service_name: Optional[str]
    service_type: Optional[str]
    configuration: Optional[dict] = None  # JSONB — returned as dict by asyncpg
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiIntegrationCreate(BaseModel):
    service_name: str = Field(..., max_length=100)
    service_type: str = Field(
        ...,
        description="CLOUDINARY | FIREBASE | GOOGLE_MAPS | RAZORPAY | MSG91 | WHATSAPP | FCM | MINIO | SMTP",
    )
    configuration: dict = Field(
        default_factory=dict, description="JSON config for the service"
    )
    is_active: bool = True


class ApiIntegrationUpdate(BaseModel):
    service_name: Optional[str] = Field(None, max_length=100)
    configuration: Optional[dict] = None
    is_active: Optional[bool] = None


# ── City / Master Data ────────────────────────────────────────────────────────


class CityOut(BaseModel):
    id: int
    name: str
    city_code: Optional[str]
    is_active: bool
    state_id: int
    state_name: Optional[str] = None  # populated manually in service
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    model_config = {"from_attributes": True}


class CityCreate(BaseModel):
    name: str = Field(..., max_length=150)
    city_code: Optional[str] = Field(None, max_length=50)
    state_id: int
    is_active: bool = True
    latitude: Optional[float] = Field(
        None,
        ge=-90,
        le=90,
        description="City-centre latitude. Used to bias Google Places autocomplete.",
    )
    longitude: Optional[float] = Field(
        None,
        ge=-180,
        le=180,
        description="City-centre longitude.",
    )


class CityUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    city_code: Optional[str] = Field(None, max_length=50)
    is_active: Optional[bool] = None
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)


class StateOut(BaseModel):
    id: int
    name: str
    state_code: Optional[str]
    is_active: bool
    country_id: int

    model_config = {"from_attributes": True}


# ── Vehicle Category ──────────────────────────────────────────────────────────


class VehicleCategoryOut(BaseModel):
    """Full category detail including media & SEO (migration 0018)."""

    id: int
    category_name: str
    seating_capacity: Optional[int]
    luggage_capacity: Optional[int]
    is_active: bool
    created_at: datetime
    # Media — migration 0018
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    # SEO — migration 0018
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    display_order: int = 0

    model_config = {"from_attributes": True}


class VehicleCategoryCreate(BaseModel):
    category_name: str = Field(..., max_length=100)
    seating_capacity: Optional[int] = Field(None, ge=1)
    luggage_capacity: Optional[int] = Field(None, ge=0)
    is_active: bool = True
    # Media
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    # SEO
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    display_order: int = 0


class VehicleCategoryUpdate(BaseModel):
    category_name: Optional[str] = Field(None, max_length=100)
    seating_capacity: Optional[int] = Field(None, ge=1)
    luggage_capacity: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    # Media
    image_url: Optional[str] = None
    icon_url: Optional[str] = None
    # SEO
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    display_order: Optional[int] = None


# ── Default Vehicle Pricing Rules ──────────────────────────────────────────────


class DefaultPricingRuleOut(BaseModel):
    id: int
    vehicle_category_id: int
    trip_type: str
    base_fare: Decimal
    minimum_km: int
    per_km_rate: Decimal
    driver_allowance: Decimal
    night_charge: Decimal
    driver_allowance_type: str = "PER_TRIP"
    night_charge_type: str = "FIXED"
    toll: Decimal = Decimal("0")
    free_waiting_minutes: int = 0
    actual_waiting_minutes: int = 0
    waiting_rate_per_hour: Decimal = Decimal("0")
    waiting_granularity: str = "PER_15_MINUTES"
    updated_at: datetime

    model_config = {"from_attributes": True}


class DefaultPricingRuleUpdate(BaseModel):
    """Admin can update any numeric field on a default rule."""

    base_fare: Optional[Decimal] = Field(None, ge=0)
    minimum_km: Optional[int] = Field(None, ge=0)
    per_km_rate: Optional[Decimal] = Field(None, ge=0)
    driver_allowance: Optional[Decimal] = Field(None, ge=0)
    night_charge: Optional[Decimal] = Field(None, ge=0)
    driver_allowance_type: Optional[str] = Field(
        None, pattern="^(PER_TRIP|PER_DAY|PER_KM|NONE)$"
    )
    night_charge_type: Optional[str] = Field(
        None, pattern="^(FIXED|PERCENTAGE|PER_KM)$"
    )
    toll: Optional[Decimal] = Field(None, ge=0)
    free_waiting_minutes: Optional[int] = Field(None, ge=0)
    actual_waiting_minutes: Optional[int] = Field(None, ge=0)
    waiting_rate_per_hour: Optional[Decimal] = Field(None, ge=0)
    waiting_granularity: Optional[str] = Field(
        None, pattern="^(PER_MINUTE|PER_15_MINUTES|PER_30_MINUTES|PER_HOUR)$"
    )


class EffectivePricingRuleOut(BaseModel):
    """
    Returned by the booking engine helper endpoint.
    Tells caller whether a city-specific or default rule was used.
    """

    vehicle_category_id: int
    trip_type: str
    base_fare: Decimal
    minimum_km: int
    per_km_rate: Decimal
    driver_allowance: Decimal
    night_charge: Decimal
    driver_allowance_type: str = "PER_TRIP"
    night_charge_type: str = "FIXED"
    toll: Decimal = Decimal("0")
    free_waiting_minutes: int = 0
    actual_waiting_minutes: int = 0
    waiting_rate_per_hour: Decimal = Decimal("0")
    waiting_granularity: str = "PER_15_MINUTES"
    source: str  # "city_specific" | "default"

    model_config = {"from_attributes": False}


# ── Service Types (Dynamic — Migration 0019) ──────────────────────────────────


class ServiceTypeOut(BaseModel):
    """Full service type record including media & SEO."""

    id: int
    type_code: str
    label: str
    description: Optional[str] = None
    icon_url: Optional[str] = None
    image_url: Optional[str] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    display_order: int = 0
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ServiceTypeUpdate(BaseModel):
    """Admins may update everything except type_code (immutable business key)."""

    label: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    icon_url: Optional[str] = None
    image_url: Optional[str] = None
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    display_order: Optional[int] = None
    is_active: Optional[bool] = None


class ServiceTypeCreate(BaseModel):
    """Create a brand-new platform service type."""

    type_code: str = Field(..., min_length=2, max_length=50, pattern=r"^[A-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = None
    icon_url: Optional[str] = None
    image_url: Optional[str] = None
    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)
    display_order: int = 0
    is_active: bool = True


# ════════════════════════════════════════════════════════════════
# WEBSITE CMS — Homepage Sections, Variants, Header, Footer
# Doc Ref: Migration 0044_website_cms
# ════════════════════════════════════════════════════════════════

# ── Page Section (registry of section types) ──────────────────


class PageSectionOut(BaseModel):
    id: int
    section_key: str
    display_name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    display_order: int
    is_visible: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    variant_count: int = 0
    active_variant_id: Optional[int] = None

    model_config = {"from_attributes": True}


class PageSectionUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=150)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=50)
    display_order: Optional[int] = None
    is_visible: Optional[bool] = None
    is_active: Optional[bool] = None


# ── Section Variant ────────────────────────────────────────────
# Each section template has a fixed slot set. The schema below is
# shared across all section types; the frontend picks which fields
# to render based on the parent section_key.

ANIMATION_STYLES = [
    "NONE",
    "FADE",
    "SLIDE_LEFT",
    "SLIDE_RIGHT",
    "SLIDE_UP",
    "ZOOM",
    "PARALLAX",
]
TEXT_ALIGNMENTS = ["LEFT", "CENTER", "RIGHT"]


class SectionVariantOut(BaseModel):
    id: int
    page_section_id: int
    section_key: str
    variant_name: str
    variant_tag: Optional[str] = None
    display_order: int
    is_active: bool
    headline: Optional[str] = None
    subheadline: Optional[str] = None
    body_text: Optional[str] = None
    cta_text: Optional[str] = None
    cta_link: Optional[str] = None
    background_image_url: Optional[str] = None
    background_video_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    icon_url: Optional[str] = None
    accent_color: Optional[str] = None
    animation_style: Optional[str] = None
    text_alignment: Optional[str] = None
    overlay_opacity: Optional[Decimal] = None
    content: Optional[Dict[str, Any]] = None
    service_type: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # Built explicitly from a dict by the service — see
    # CmsService._serialize_variant. from_attributes=False because the ORM
    # model doesn't carry a denormalized section_key / service_type.
    model_config = {"from_attributes": False}


class SectionVariantCreate(BaseModel):
    variant_name: str = Field(..., min_length=1, max_length=150)
    variant_tag: Optional[str] = Field(None, max_length=50)
    display_order: int = 0
    is_active: bool = False
    headline: Optional[str] = Field(None, max_length=300)
    subheadline: Optional[str] = Field(None, max_length=500)
    body_text: Optional[str] = None
    cta_text: Optional[str] = Field(None, max_length=100)
    cta_link: Optional[str] = Field(None, max_length=500)
    background_image_url: Optional[str] = None
    background_video_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    icon_url: Optional[str] = None
    accent_color: Optional[str] = Field(None, max_length=20)
    animation_style: Optional[str] = Field(None)
    text_alignment: Optional[str] = Field(None)
    overlay_opacity: Optional[Decimal] = Field(None, ge=0, le=1)
    content: Optional[Dict[str, Any]] = Field(
        None,
        description="Structured content for the section (steps, offers, faqs, "
        "destinations, hotels, stats). Keys must match the section schema.",
    )
    service_type: Optional[str] = Field(
        None,
        description="Service type this variant promotes (CAB | HOTEL | TOUR | ALL). "
        "Used by customer-web to inject the matching search form into the hero.",
        pattern="^(CAB|HOTEL|TOUR|ALL)$",
    )
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    @field_validator("animation_style")
    @classmethod
    def _valid_anim(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ANIMATION_STYLES:
            raise ValueError(f"animation_style must be one of {ANIMATION_STYLES}")
        return v

    @field_validator("text_alignment")
    @classmethod
    def _valid_align(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in TEXT_ALIGNMENTS:
            raise ValueError(f"text_alignment must be one of {TEXT_ALIGNMENTS}")
        return v


class SectionVariantUpdate(BaseModel):
    variant_name: Optional[str] = Field(None, min_length=1, max_length=150)
    variant_tag: Optional[str] = Field(None, max_length=50)
    display_order: Optional[int] = None
    headline: Optional[str] = Field(None, max_length=300)
    subheadline: Optional[str] = Field(None, max_length=500)
    body_text: Optional[str] = None
    cta_text: Optional[str] = Field(None, max_length=100)
    cta_link: Optional[str] = Field(None, max_length=500)
    background_image_url: Optional[str] = None
    background_video_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    icon_url: Optional[str] = None
    accent_color: Optional[str] = Field(None, max_length=20)
    animation_style: Optional[str] = None
    text_alignment: Optional[str] = None
    overlay_opacity: Optional[Decimal] = Field(None, ge=0, le=1)
    service_type: Optional[str] = Field(
        None,
        pattern="^(CAB|HOTEL|TOUR|ALL)$",
    )
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    variant_name: Optional[str] = Field(None, min_length=1, max_length=150)
    variant_tag: Optional[str] = Field(None, max_length=50)
    display_order: Optional[int] = None
    headline: Optional[str] = Field(None, max_length=300)
    subheadline: Optional[str] = Field(None, max_length=500)
    body_text: Optional[str] = None
    cta_text: Optional[str] = Field(None, max_length=100)
    cta_link: Optional[str] = Field(None, max_length=500)
    background_image_url: Optional[str] = None
    background_video_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    icon_url: Optional[str] = None
    accent_color: Optional[str] = Field(None, max_length=20)
    animation_style: Optional[str] = None
    text_alignment: Optional[str] = None
    overlay_opacity: Optional[Decimal] = Field(None, ge=0, le=1)
    content: Optional[Dict[str, Any]] = Field(
        None,
        description="Structured content for the section (steps, offers, faqs, "
        "destinations, hotels, stats). Keys must match the section schema.",
    )
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    @field_validator("animation_style")
    @classmethod
    def _valid_anim(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ANIMATION_STYLES:
            raise ValueError(f"animation_style must be one of {ANIMATION_STYLES}")
        return v

    @field_validator("text_alignment")
    @classmethod
    def _valid_align(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in TEXT_ALIGNMENTS:
            raise ValueError(f"text_alignment must be one of {TEXT_ALIGNMENTS}")
        return v


# ── Site Header (singleton) ────────────────────────────────────


class SiteHeaderOut(BaseModel):
    id: int
    logo_url: Optional[str] = None
    logo_alt_text: Optional[str] = None
    tagline: Optional[str] = None
    show_search_bar: bool
    show_login_button: bool
    cta_text: Optional[str] = None
    cta_link: Optional[str] = None
    support_phone: Optional[str] = None
    contact_email: Optional[str] = None
    nav_links: List[Dict[str, Any]] = []
    social_links: Dict[str, Any] = {}
    background_color: Optional[str] = None
    text_color: Optional[str] = None
    is_active: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class SiteHeaderUpdate(BaseModel):
    logo_url: Optional[str] = None
    logo_alt_text: Optional[str] = Field(None, max_length=150)
    tagline: Optional[str] = Field(None, max_length=300)
    show_search_bar: Optional[bool] = None
    show_login_button: Optional[bool] = None
    cta_text: Optional[str] = Field(None, max_length=100)
    cta_link: Optional[str] = Field(None, max_length=500)
    support_phone: Optional[str] = Field(None, max_length=20)
    contact_email: Optional[str] = Field(None, max_length=150)
    nav_links: Optional[List[Dict[str, Any]]] = None
    social_links: Optional[Dict[str, Any]] = None
    background_color: Optional[str] = Field(None, max_length=20)
    text_color: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None


# ── Site Footer (singleton) ────────────────────────────────────


class SiteFooterOut(BaseModel):
    id: int
    logo_url: Optional[str] = None
    description: Optional[str] = None
    copyright_text: Optional[str] = None
    company_address: Optional[str] = None
    support_phone: Optional[str] = None
    contact_email: Optional[str] = None
    quick_links: List[Dict[str, Any]] = []
    legal_links: List[Dict[str, Any]] = []
    social_links: Dict[str, Any] = {}
    payment_icons: List[Dict[str, Any]] = []
    app_store_links: Dict[str, Any] = {}
    background_color: Optional[str] = None
    text_color: Optional[str] = None
    is_active: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class SiteFooterUpdate(BaseModel):
    logo_url: Optional[str] = None
    description: Optional[str] = None
    copyright_text: Optional[str] = Field(None, max_length=300)
    company_address: Optional[str] = None
    support_phone: Optional[str] = Field(None, max_length=20)
    contact_email: Optional[str] = Field(None, max_length=150)
    quick_links: Optional[List[Dict[str, Any]]] = None
    legal_links: Optional[List[Dict[str, Any]]] = None
    social_links: Optional[Dict[str, Any]] = None
    payment_icons: Optional[List[Dict[str, Any]]] = None
    app_store_links: Optional[Dict[str, Any]] = None
    background_color: Optional[str] = Field(None, max_length=20)
    text_color: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None


# ── Public Homepage Bundle (returned to customer-web) ──────────


class PublicHomepageOut(BaseModel):
    """What the customer-web homepage actually renders.

    Only sections that are visible AND have an active in-window variant
    are included. The active variant's slot values are flattened into
    the section object so the frontend doesn't have to do lookups.
    """

    header: SiteHeaderOut
    footer: SiteFooterOut
    sections: List[Dict[str, Any]] = []


class CmsAuditVersionOut(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    section_key: Optional[str] = None
    action_type: str
    previous_value: Optional[Dict[str, Any]] = None
    new_value: Optional[Dict[str, Any]] = None
    changed_by_user_id: Optional[str] = None
    change_reason: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ════════════════════════════════════════════════════════════════
# BLOG SYSTEM — Blog Posts
# Doc Ref: Blog System §1 — Database Schema
#          Migration 0051_blog_posts
# ════════════════════════════════════════════════════════════════


class BlogPostOut(BaseModel):
    """Full blog post detail returned to admin and public APIs."""

    id: int
    title: str
    slug: str
    excerpt: Optional[str] = None
    content: Optional[str] = None
    featured_image_url: Optional[str] = None
    author_name: Optional[str] = None
    author_avatar_url: Optional[str] = None
    tags: list[str] = []
    is_published: bool = False
    published_at: Optional[datetime] = None
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BlogPostListOut(BaseModel):
    """Lightweight blog post summary for list views (no content body)."""

    id: int
    title: str
    slug: str
    excerpt: Optional[str] = None
    featured_image_url: Optional[str] = None
    author_name: Optional[str] = None
    author_avatar_url: Optional[str] = None
    tags: list[str] = []
    is_published: bool = False
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BlogPostCreate(BaseModel):
    """Payload for creating a new blog post."""

    title: str = Field(..., min_length=1, max_length=300)
    slug: str = Field(
        ...,
        min_length=1,
        max_length=300,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    excerpt: Optional[str] = None
    content: Optional[str] = None
    featured_image_url: Optional[str] = None
    author_name: Optional[str] = Field(None, max_length=150)
    author_avatar_url: Optional[str] = None
    tags: Optional[list[str]] = None
    is_published: bool = False
    published_at: Optional[datetime] = None
    seo_title: Optional[str] = Field(None, max_length=300)
    seo_description: Optional[str] = Field(None, max_length=500)
    seo_keywords: Optional[str] = Field(None, max_length=500)


class BlogPostUpdate(BaseModel):
    """Payload for updating an existing blog post. All fields optional."""

    title: Optional[str] = Field(None, min_length=1, max_length=300)
    slug: Optional[str] = Field(
        None,
        min_length=1,
        max_length=300,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    excerpt: Optional[str] = None
    content: Optional[str] = None
    featured_image_url: Optional[str] = None
    author_name: Optional[str] = Field(None, max_length=150)
    author_avatar_url: Optional[str] = None
    tags: Optional[list[str]] = None
    is_published: Optional[bool] = None
    published_at: Optional[datetime] = None
    seo_title: Optional[str] = Field(None, max_length=300)
    seo_description: Optional[str] = Field(None, max_length=500)
    seo_keywords: Optional[str] = Field(None, max_length=500)
