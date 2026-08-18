# ============================================================
# WAYTERO — ADMIN / SETTINGS MODELS
# File: app/modules/admin/models/__init__.py
# Doc Ref:
#   DB Schema Part 1, Section 12  → system_configurations
#   DB Schema Part 1, Section 13  → app_versions
#   DB Schema Part 1, Section 14  → api_integrations
#   DB Schema Part 2, Section 14  → commission_groups
#   DB Schema Part 2, Section 15  → commission_rules
#   DB Schema Part 3, Section 18  → vehicle_pricing_rules
#   DB Schema Part 8, Section 11  → notification_templates
# ============================================================

from datetime import datetime, timezone
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from app.core.database import Base

# Import master/reference models first — ensures SQLAlchemy metadata
# resolves ForeignKey('cities.id') before CommissionRule is mapped.
from app.modules.master.models import Country, State, City  # noqa: F401


class SystemConfiguration(Base):
    """
    Key-value store for all platform-wide configurable settings.
    Doc Ref: DB Schema Part 1, Section 12
    Examples: GST_PERCENTAGE, OTP_EXPIRY_MINUTES, MAX_LOGIN_ATTEMPTS,
              BOOKING_CANCELLATION_HOURS, WALLET_MINIMUM_BALANCE
    """

    __tablename__ = "system_configurations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    config_key = Column(String(200), unique=True, nullable=False)
    config_value = Column(Text)
    description = Column(Text)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AppVersion(Base):
    """
    Controls mobile app force-update status.
    Doc Ref: DB Schema Part 1, Section 13
    Platforms: ANDROID | IOS
    """

    __tablename__ = "app_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    platform = Column(String(50))  # ANDROID | IOS
    version = Column(String(50))
    is_force_update = Column(Boolean, default=False)
    release_notes = Column(Text)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ApiIntegration(Base):
    """
    Third-party service credentials / configuration.
    Doc Ref: DB Schema Part 1, Section 14
    """

    __tablename__ = "api_integrations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_name = Column(String(100))
    service_type = Column(String(100))
    configuration = Column(JSONB)  # JSONB — asyncpg returns dict automatically
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class CommissionGroup(Base):
    """
    Logical grouping for commission rule sets.
    Doc Ref: DB Schema Part 2, Section 14
    """

    __tablename__ = "commission_groups"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    group_name = Column(String(150), unique=True, nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    rules = relationship(
        "CommissionRule", back_populates="group", cascade="all, delete-orphan"
    )


class CommissionRule(Base):
    """
    Per-service-type commission rule within a group.
    Doc Ref: DB Schema Part 2, Section 15
    commission_type: PERCENTAGE | FLAT
    service_type: CAB | HOTEL | TOUR
    """

    __tablename__ = "commission_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    commission_group_id = Column(
        BigInteger, ForeignKey("commission_groups.id"), nullable=False
    )
    service_type = Column(String(50))  # CAB | HOTEL | TOUR
    commission_type = Column(String(50))  # PERCENTAGE | FLAT
    commission_value = Column(Numeric(12, 2))
    city_id = Column(BigInteger, ForeignKey("cities.id"), nullable=True)
    effective_from = Column(Date)
    effective_to = Column(Date)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    group = relationship("CommissionGroup", back_populates="rules")

    __table_args__ = (Index("idx_commission_city", "city_id"),)


# VehiclePricingRule lives in app.modules.vehicle.models — imported there to avoid duplicate table.
# Admin services import it from that module directly.


class NotificationTemplate(Base):
    """
    Reusable templates for SMS / Email / Push notifications.
    Doc Ref: DB Schema Part 8, Section 11
    channel: SMS | EMAIL | PUSH | IN_APP
    """

    __tablename__ = "notification_templates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    template_code = Column(String(100), unique=True)
    channel = Column(String(50))  # SMS | EMAIL | PUSH | IN_APP
    template_name = Column(String(255))
    template_content = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ── Website CMS — Homepage Section Registry & Variants ──────────────────────
# Doc Ref: Migration 0044_website_cms
# Lets admins manage every homepage section (Hero, Services, Tours, …)
# and create multiple design variants per section. Exactly one variant
# per section can be active at any time (partial unique index).
# Header & footer live in single-row singleton tables.


class PageSection(Base):
    """
    Registry of every homepage section the platform knows about.

    section_key is the immutable business key the frontend renders on
    (HERO, SERVICES, TOUR_PACKAGES, …). Adding a new section_type is
    a migration that seeds a row here — no code change required.
    """

    __tablename__ = "page_sections"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    section_key = Column(String(50), unique=True, nullable=False)
    display_name = Column(String(150), nullable=False)
    description = Column(Text)
    icon = Column(String(50))
    display_order = Column(Integer, nullable=False, default=0)
    is_visible = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    variants = relationship(
        "SectionVariant",
        back_populates="page_section",
        cascade="all, delete-orphan",
        order_by="SectionVariant.display_order",
    )

    __table_args__ = (Index("idx_page_sections_order", "display_order"),)


class SectionVariant(Base):
    """
    A concrete design of a homepage section.

    Each row is one design (Default Hero, Festival Hero, Outstation
    Promo Hero, …). Exactly one variant per section may have
    is_active = TRUE at a time — enforced by the partial unique
    index uniq_variants_active_per_section.

    Content slots are typed per the section template (HERO, SERVICES,
    …). Fields the section does not use are simply left NULL; the
    frontend only renders the columns that matter for the section_key.

    Animation & display:
        animation_style: NONE | FADE | SLIDE_LEFT | SLIDE_RIGHT |
                         SLIDE_UP | ZOOM | PARALLAX
        text_alignment:  LEFT | CENTER | RIGHT
        overlay_opacity: 0.00 — 1.00 (numeric(3,2))

    Scheduling (optional):
        starts_at / ends_at — the variant is rendered only inside
        the window when is_active is true. Outside the window the
        renderer falls back to the next active variant, or hides
        the section if none exists.
    """

    __tablename__ = "section_variants"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    page_section_id = Column(
        BigInteger, ForeignKey("page_sections.id", ondelete="CASCADE"), nullable=False
    )
    variant_name = Column(String(150), nullable=False)
    variant_tag = Column(String(50))
    display_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=False)

    # Common typed slots
    headline = Column(String(300))
    subheadline = Column(String(500))
    body_text = Column(Text)
    cta_text = Column(String(100))
    cta_link = Column(String(500))
    background_image_url = Column(Text)
    background_video_url = Column(Text)
    mobile_image_url = Column(Text)
    icon_url = Column(Text)
    accent_color = Column(String(20))

    # Animation & display
    animation_style = Column(String(50))
    text_alignment = Column(String(20))
    overlay_opacity = Column(Numeric(3, 2))

    # Structured content (JSONB) — free-form payload for sections whose
    # copy cannot live in the fixed typed slots. Authored by admins via a
    # JSON editor in the admin portal and spread into the public homepage
    # variant payload so renderers read it via `pick(variant, key, …)`.
    # Content keys are defined per section schema, e.g.:
    #   HOW_IT_WORKS → {"steps": [{title, description, icon}]}
    #   OFFERS       → {"offers": [{title, description, badge, cta_*}]}
    #   FAQ          → {"faqs": [{question, answer}]}
    #   STATS        → {"stats": [{value, suffix, label}]}
    #   POPULAR_DESTINATIONS → {"destinations": [{name, state, price}]}
    #   FEATURED_HOTELS      → {"hotels": [{name, city, rating, …}]}
    content = Column(JSONB)

    # Service type binding — when set, customer-web injects the matching
    # search form into this section (CAB search, HOTEL search, …).
    # When NULL, no inline search is rendered — section is decorative.
    service_type = Column(String(20))

    # Scheduling
    starts_at = Column(DateTime(timezone=True))
    ends_at = Column(DateTime(timezone=True))

    # Audit
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    page_section = relationship("PageSection", back_populates="variants")

    __table_args__ = (
        Index("idx_variants_section", "page_section_id", "display_order"),
    )


class SiteHeaderConfig(Base):
    """
    Singleton table (one row, enforced by UNIQUE on `singleton`)
    holding the global website header configuration.

    nav_links / social_links are JSONB so we can evolve the shape
    without a migration. The shape is documented in the schema.
    """

    __tablename__ = "site_header_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    singleton = Column(Boolean, nullable=False, default=True, unique=True)
    logo_url = Column(Text)
    logo_alt_text = Column(String(150))
    tagline = Column(String(300))
    show_search_bar = Column(Boolean, nullable=False, default=True)
    show_login_button = Column(Boolean, nullable=False, default=True)
    cta_text = Column(String(100))
    cta_link = Column(String(500))
    support_phone = Column(String(20))
    contact_email = Column(String(150))
    nav_links = Column(JSONB, nullable=False, default=list)
    social_links = Column(JSONB, nullable=False, default=dict)
    background_color = Column(String(20))
    text_color = Column(String(20))
    is_active = Column(Boolean, nullable=False, default=True)
    updated_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SiteFooterConfig(Base):
    """
    Singleton table (one row) holding the global website footer.
    """

    __tablename__ = "site_footer_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    singleton = Column(Boolean, nullable=False, default=True, unique=True)
    logo_url = Column(Text)
    description = Column(Text)
    copyright_text = Column(String(300))
    company_address = Column(Text)
    support_phone = Column(String(20))
    contact_email = Column(String(150))
    quick_links = Column(JSONB, nullable=False, default=list)
    legal_links = Column(JSONB, nullable=False, default=list)
    social_links = Column(JSONB, nullable=False, default=dict)
    payment_icons = Column(JSONB, nullable=False, default=list)
    app_store_links = Column(JSONB, nullable=False, default=dict)
    background_color = Column(String(20))
    text_color = Column(String(20))
    is_active = Column(Boolean, nullable=False, default=True)
    updated_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CmsAuditVersion(Base):
    """
    Append-only audit of CMS mutations (variant activate/deactivate,
    section visibility toggle, header/footer update). Mirrors the
    shape of cancellation_policy_versions.
    """

    __tablename__ = "cms_audit_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(BigInteger, nullable=False)
    section_key = Column(String(50))
    action_type = Column(String(50), nullable=False)
    previous_value = Column(JSONB)
    new_value = Column(JSONB)
    changed_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    change_reason = Column(Text)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class BlogPost(Base):
    """
    Blog post — the core content entity for the WayTero blog system.

    Each row is one blog post with rich HTML content, metadata, tags,
    SEO fields, and publish state. The slug is unique and used for
    public-facing URLs (/blog/{slug}).

    Doc Ref: Blog System §1 — Database Schema
             Migration 0051_blog_posts
    """

    __tablename__ = "blog_posts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    title = Column(String(300), nullable=False)
    slug = Column(String(300), nullable=False, unique=True)
    excerpt = Column(Text)
    content = Column(Text)
    featured_image_url = Column(Text)
    author_name = Column(String(150))
    author_avatar_url = Column(Text)
    tags = Column(JSONB, nullable=False, default=list)
    is_published = Column(Boolean, nullable=False, default=False)
    published_at = Column(DateTime(timezone=True))
    seo_title = Column(String(300))
    seo_description = Column(String(500))
    seo_keywords = Column(String(500))
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AuditLog(Base):
    """
    Platform-wide audit trail of administrative actions.
    Doc Ref: Docs/05_Database/09_DATABASE_SCHEMA_PART_8_AUDIT_NOTIFICATION.md §76-104
             Docs/04_API_Documentation/12_ADMIN_API.md §24
             BRD Part 8 §213 (Audit Compliance — 7-year retention)

    Append-only at the application layer. New rows are inserted by
    app.modules.admin.services.audit_logger; existing rows must not be
    updated or deleted. The DB schema does not enforce immutability (no
    triggers) — that's the responsibility of the audit_logger contract and
    of the operator-level audit review.

    Fields:
        module_name: AUTH | PARTNER | DRIVER | VEHICLE | HOTEL | TOUR |
                     BOOKING | PAYMENT | WALLET | SETTLEMENT | COUPON |
                     NOTIFICATION | ROLE | CONFIG | STAFF
        action_type: free-form verb (e.g. STATUS_UPDATED, PASSWORD_RESET,
                     LOGIN_SUCCESS, ROLE_CREATED, PERMISSION_ASSIGNED)
    """

    __tablename__ = "audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)  # NULL = system event
    module_name = Column(String(100), nullable=False)
    entity_name = Column(String(100))  # e.g. "partner", "hotel"
    entity_id = Column(BigInteger)  # id of the affected entity
    action_type = Column(String(100), nullable=False)
    old_values = Column(JSONB)  # snapshot before the change
    new_values = Column(JSONB)  # snapshot after the change
    ip_address = Column(String(100))
    user_agent = Column(Text)
    request_id = Column(String(100))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Indexes are created in the migration alongside the table. Declaring
    # them here too keeps them in sync if anyone ever runs Base.metadata
    # create_all in a dev environment.
    __table_args__ = (
        Index("idx_audit_user", "user_id"),
        Index("idx_audit_module", "module_name"),
        Index("idx_audit_action_type", "action_type"),
        Index("idx_audit_entity", "entity_name", "entity_id"),
        Index("idx_audit_created_at", "created_at"),
    )
