# ============================================================
# WAYTERO — LANDING PAGE SCHEMAS
# File: app/modules/admin/schemas/landing_pages.py
# Doc Ref: City + Service-Type Landing Pages §Phase 1
#
# Caps mirror app/shared/utilities/seo.py and the DB columns created by
# migration 0069 (slug 120, seo_title 120, seo_description 320,
# seo_keywords 500) so a payload that passes here can never fail on INSERT.
#
# Block validation is deliberately strict on WRITE and tolerant on READ:
# a typo'd block type must not publish a hole in the page, but a block type
# added by a later deploy must not 500 a page that is already indexed.
# ============================================================

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.admin.constants.landing_pages import (
    BLOCK_TYPES,
    CITY_PAGE_TYPES,
    MAX_BLOCKS,
    MAX_FAQS,
    MAX_GALLERY,
    MAX_HIGHLIGHTS,
    MAX_STATS,
    PAGE_TYPES,
    STATE_PAGE_TYPES,
)

PageTypeLiteral = Literal[
    "CITY_HUB",
    "CITY_CABS",
    "CITY_HOTELS",
    "CITY_TOURS",
    "STATE_HOTELS",
    "STATE_TOURS",
]

BlockTypeLiteral = Literal[
    "prose",
    "highlights",
    "stats",
    "gallery",
    "faq",
    "fare_matrix",
    "vehicle_grid",
    "hotel_grid",
    "tour_grid",
    "steps",
    "cta_band",
    "internal_links",
]


# ── Shared validators ────────────────────────────────────────────────────────


def _clean(value: Optional[str]) -> Optional[str]:
    """Empty-string means "not authored" so the service can derive it."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class _LandingSlugMixin:
    slug: Optional[str] = Field(None, min_length=1, max_length=120)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: Optional[str]) -> Optional[str]:
        from app.modules.admin.constants.landing_pages import SLUG_RE

        if v is not None and not SLUG_RE.match(v):
            raise ValueError(
                "slug must be lowercase letters, digits and single hyphens"
            )
        return v


class _LandingPayloadMixin:
    """Copy / imagery / structured-content fields shared by create+update."""

    display_name: Optional[str] = Field(None, max_length=150)
    eyebrow: Optional[str] = Field(None, max_length=100)
    headline: Optional[str] = Field(None, max_length=300)
    subheadline: Optional[str] = Field(None, max_length=500)
    body_text: Optional[str] = None

    banner_image_url: Optional[str] = Field(None, max_length=1000)
    banner_mobile_url: Optional[str] = Field(None, max_length=1000)
    banner_video_url: Optional[str] = Field(None, max_length=1000)
    banner_alt_text: Optional[str] = Field(None, max_length=150)
    og_image_url: Optional[str] = Field(None, max_length=1000)
    overlay_opacity: Optional[float] = Field(None, ge=0, le=1)

    gallery: Optional[List[Dict[str, Any]]] = None
    highlights: Optional[List[Dict[str, Any]]] = None
    stats: Optional[List[Dict[str, Any]]] = None
    blocks: Optional[List[Dict[str, Any]]] = None
    faqs: Optional[List[Dict[str, Any]]] = None

    cta_text: Optional[str] = Field(None, max_length=100)
    cta_link: Optional[str] = Field(None, max_length=500)
    cta2_text: Optional[str] = Field(None, max_length=100)
    cta2_link: Optional[str] = Field(None, max_length=500)

    seo_title: Optional[str] = Field(None, max_length=120)
    seo_description: Optional[str] = Field(None, max_length=320)
    seo_keywords: Optional[str] = Field(None, max_length=500)

    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    display_order: Optional[int] = None

    @field_validator(
        "display_name",
        "eyebrow",
        "headline",
        "subheadline",
        "banner_image_url",
        "banner_mobile_url",
        "banner_video_url",
        "banner_alt_text",
        "og_image_url",
        "cta_text",
        "cta_link",
        "cta2_text",
        "cta2_link",
        "seo_title",
        "seo_description",
        "seo_keywords",
        mode="after",
    )
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        return _clean(v)

    @field_validator("gallery", "highlights", "stats", "blocks", "faqs", mode="after")
    @classmethod
    def _validate_lists(
        cls, v: Optional[List[Dict[str, Any]]], info
    ) -> Optional[List[Dict[str, Any]]]:
        if v is None:
            return None
        limits = {
            "gallery": MAX_GALLERY,
            "highlights": MAX_HIGHLIGHTS,
            "stats": MAX_STATS,
            "blocks": MAX_BLOCKS,
            "faqs": MAX_FAQS,
        }
        limit = limits[info.field_name]
        if len(v) > limit:
            raise ValueError(f"maximum {limit} items allowed")
        for index, item in enumerate(v):
            if not isinstance(item, dict):
                raise ValueError(f"item {index} must be an object")
        if info.field_name == "blocks":
            for index, block in enumerate(v):
                btype = block.get("type")
                if btype not in BLOCK_TYPES:
                    raise ValueError(
                        f"block {index}: unknown type {btype!r}; "
                        f"expected one of {sorted(BLOCK_TYPES)}"
                    )
        if info.field_name == "faqs":
            for index, faq in enumerate(v):
                if not (faq.get("question") or "").strip():
                    raise ValueError(f"faq {index}: question is required")
                if not (faq.get("answer") or "").strip():
                    raise ValueError(f"faq {index}: answer is required")
        if info.field_name == "gallery":
            for index, tile in enumerate(v):
                if not (tile.get("image_url") or "").strip():
                    raise ValueError(f"gallery {index}: image_url is required")
        return v


# ── Admin CRUD payloads ──────────────────────────────────────────────────────


class LandingPageCreate(_LandingSlugMixin, _LandingPayloadMixin, BaseModel):
    """Create payload. ``slug`` optional — derived from page_type + scope."""

    page_type: PageTypeLiteral
    city_id: Optional[int] = Field(None, ge=1)
    state_id: Optional[int] = Field(None, ge=1)
    is_published: bool = False

    @model_validator(mode="after")
    def _check_scope(self) -> "LandingPageCreate":
        _validate_scope(self.page_type, self.city_id, self.state_id)
        return self


class LandingPageUpdate(_LandingSlugMixin, _LandingPayloadMixin, BaseModel):
    """Partial update. Every field optional; unset means "leave as is"."""

    page_type: Optional[PageTypeLiteral] = None
    city_id: Optional[int] = Field(None, ge=1)
    state_id: Optional[int] = Field(None, ge=1)
    is_published: Optional[bool] = None
    # Sent by the admin once the editor has acknowledged the live-404 warning
    # that comes from changing a published slug. Without it, a slug that no
    # longer matches its page_type pattern is rejected.
    slug_confirmed: bool = False


class LandingPagePublishIn(BaseModel):
    is_published: bool
    # Publishing is gated harder than saving (see LandingPageService.publish).
    confirmed: bool = False


# ── Admin read payloads ──────────────────────────────────────────────────────


class LandingPageOut(BaseModel):
    """Full admin view — includes derived copy and imagery provenance."""

    id: int
    slug: str
    page_type: PageTypeLiteral
    city_id: Optional[int] = None
    state_id: Optional[int] = None
    city_name: Optional[str] = None
    state_name: Optional[str] = None
    service_type: Optional[str] = None
    display_name: str

    eyebrow: Optional[str] = None
    headline: Optional[str] = None
    subheadline: Optional[str] = None
    body_text: Optional[str] = None

    banner_image_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
    banner_video_url: Optional[str] = None
    banner_alt_text: Optional[str] = None
    og_image_url: Optional[str] = None
    overlay_opacity: Optional[float] = None

    gallery: List[Dict[str, Any]] = []
    highlights: List[Dict[str, Any]] = []
    stats: List[Dict[str, Any]] = []
    blocks: List[Dict[str, Any]] = []
    faqs: List[Dict[str, Any]] = []

    cta_text: Optional[str] = None
    cta_link: Optional[str] = None
    cta2_text: Optional[str] = None
    cta2_link: Optional[str] = None

    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None

    is_published: bool = False
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    display_order: int = 0

    # Derived by the service, never stored: what the website would actually
    # render for the empty slots. Lets the admin preview without publishing.
    derived: Dict[str, Any] = {}
    image_source: Literal["admin", "curated", "gradient"] = "gradient"

    created_by_user_id: Optional[UUID] = None
    updated_by_user_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LandingPageListItemOut(BaseModel):
    """Slim admin row — no JSONB bodies, just what the list table shows."""

    id: int
    slug: str
    page_type: PageTypeLiteral
    city_id: Optional[int] = None
    state_id: Optional[int] = None
    city_name: Optional[str] = None
    state_name: Optional[str] = None
    service_type: Optional[str] = None
    display_name: str
    headline: Optional[str] = None
    is_published: bool = False
    display_order: int = 0
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    seo_keywords: Optional[str] = None
    image_source: Literal["admin", "curated", "gradient"] = "gradient"
    block_count: int = 0
    faq_count: int = 0
    gallery_count: int = 0
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Public payloads (customer-web) ───────────────────────────────────────────


class LandingPublicRef(BaseModel):
    """One entry of GET /public/landing-pages — the sitemap / SSG feed."""

    slug: str
    page_type: PageTypeLiteral
    service_type: Optional[str] = None
    scope: Dict[str, Any] = {}
    canonical_path: str
    updated_at: datetime


class LandingHeroOut(BaseModel):
    headline: Optional[str] = None
    subheadline: Optional[str] = None
    eyebrow: Optional[str] = None
    body_text: Optional[str] = None
    image_url: Optional[str] = None
    mobile_image_url: Optional[str] = None
    video_url: Optional[str] = None
    alt_text: Optional[str] = None
    overlay_opacity: float = 0.55
    image_source: Literal["admin", "curated", "gradient"] = "gradient"


class LandingSeoOut(BaseModel):
    title: str
    description: str
    keywords: List[str] = []
    canonical_path: str
    og_image_url: Optional[str] = None


class LandingFareRow(BaseModel):
    vehicle_category_id: int
    vehicle_category: str
    icon_url: Optional[str] = None
    seats: Optional[int] = None
    luggage: Optional[int] = None
    trip_type: str
    base_fare: float
    per_km_rate: float
    minimum_km: int
    sample_distance_km: float
    estimated_total: float
    source: Literal["city_specific", "default"]


class LandingPriceStatsOut(BaseModel):
    hotel_from: Optional[float] = None
    tour_from: Optional[float] = None
    cab_from: Optional[float] = None
    hotel_count: int = 0
    tour_count: int = 0
    vehicle_count: int = 0
    currency: str = "INR"


class LandingPagePublicOut(BaseModel):
    """Full bundle for one published page — one fetch renders the whole page."""

    slug: str
    page_type: PageTypeLiteral
    service_type: Optional[str] = None
    scope: Dict[str, Any] = {}
    hero: LandingHeroOut
    blocks: List[Dict[str, Any]] = []
    highlights: List[Dict[str, Any]] = []
    gallery: List[Dict[str, Any]] = []
    faqs: List[Dict[str, Any]] = []
    stats: List[Dict[str, Any]] = []
    cta: Dict[str, Optional[str]] = {}
    seo: LandingSeoOut
    prices: Optional[LandingPriceStatsOut] = None
    cab_fares: List[LandingFareRow] = []
    hotels: List[Dict[str, Any]] = []
    tours: List[Dict[str, Any]] = []
    vehicles: List[Dict[str, Any]] = []
    siblings: List[Dict[str, Any]] = []
    nearby: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validate_scope(
    page_type: Optional[str], city_id: Optional[int], state_id: Optional[int]
) -> None:
    """page_type, scope column and (indirectly) service_type must agree."""
    if page_type is None:
        return
    if page_type not in PAGE_TYPES:
        raise ValueError(f"unknown page_type {page_type!r}")
    if page_type in CITY_PAGE_TYPES:
        if city_id is None or state_id is not None:
            raise ValueError(f"{page_type} requires city_id and no state_id")
    elif page_type in STATE_PAGE_TYPES:
        if state_id is None or city_id is not None:
            raise ValueError(f"{page_type} requires state_id and no city_id")
