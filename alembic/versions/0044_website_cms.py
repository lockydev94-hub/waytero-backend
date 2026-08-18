"""0044_website_cms — admin-managed homepage sections + header/footer config

Revision ID: 0044_website_cms
Revises: 0043_cancellation_engine
Create Date: 2026-08-10

Doc Ref:
  BRD Part 6 §155 — homepage admin control
  Docs/04_API_Documentation/12_ADMIN_API.md §25 (Settings)
  Docs/05_Database/09_DATABASE_SCHEMA_PART_8_AUDIT_NOTIFICATION.md §76-104

Why:
  Today the customer-web homepage is hardcoded. Operations need the
  ability to swap hero banners, run festival / outstation promotions,
  toggle the visibility of every homepage section, and update the global
  header and footer without a code deploy.

  This migration lays the data-model foundation:

    1. page_sections — registry of every homepage section the platform
       knows about (HERO, SERVICES, TOURS, TESTIMONIALS, WHY_US, CTA,
       STATS, …). Each row is a template with a fixed section_key and
       a display_name. The frontend renders known sections by key and
       looks up the active variant from section_variants.

    2. section_variants — every concrete design of a section.
       A section may have N variants (Default Hero, Festival Hero,
       Outstation Hero …). Exactly one variant per section has
       is_active = TRUE at any time (enforced by a partial unique
       index). Variant content is stored in a typed set of columns
       per the section's template; section_key drives which columns
       the UI shows. All uploads go to Cloudinary under
       waytero/cms/{section_key}/ and we only persist the URL.

    3. site_header_config / site_footer_config — single-row tables
       (UNIQUE on a constant 'singleton' key) for global header and
       footer settings. Logo, contact info, navigation links, social
       URLs, copyright text, etc.

    4. cms_audit_versions — append-only log of every variant
       activate/deactivate so we can answer "which hero was live on
       2026-11-12". Same shape as cancellation_policy_versions.

Notes:
  Every write is idempotent. ON CONFLICT DO NOTHING for seed rows,
  CREATE TABLE IF NOT EXISTS everywhere, partial unique index guards
  the "exactly one active variant per section" invariant.
"""

import sqlalchemy as sa
from alembic import op


revision = "0044_website_cms"
down_revision = "0043_cancellation_engine"
branch_labels = None
depends_on = None


# Sections seeded by the platform. Adding more in future migrations is fine —
# the unique section_key lets us extend the catalog without code changes.
SEED_SECTIONS = [
    ("HERO", "Hero Banner", 10, True),
    ("SERVICES", "Services Grid", 20, True),
    ("WHY_US", "Why Choose WayTero", 30, True),
    ("TOUR_PACKAGES", "Tour Packages", 40, True),
    ("TESTIMONIALS", "Testimonials", 50, True),
    ("STATS", "Platform Stats", 60, True),
    ("PARTNERS", "Partners Showcase", 70, True),
    ("CTA", "Call To Action", 80, True),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ════════════════════════════════════════════════════════════════
    # 1. page_sections — registry of homepage section templates
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS page_sections (
                id                BIGSERIAL PRIMARY KEY,
                section_key       VARCHAR(50)  NOT NULL UNIQUE,
                display_name      VARCHAR(150) NOT NULL,
                description       TEXT,
                icon              VARCHAR(50),
                display_order     INTEGER      NOT NULL DEFAULT 0,
                is_visible        BOOLEAN      NOT NULL DEFAULT TRUE,
                is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_page_sections_order
                ON page_sections (display_order)
                WHERE is_active = TRUE
            """
        )
    )

    for section_key, display_name, order, active in SEED_SECTIONS:
        conn.execute(
            sa.text(
                """
                INSERT INTO page_sections
                    (section_key, display_name, description, icon, display_order, is_visible, is_active)
                VALUES (:k, :n, NULL, :icon, :o, TRUE, :a)
                ON CONFLICT (section_key) DO NOTHING
                """
            ),
            {
                "k": section_key,
                "n": display_name,
                "icon": section_key.lower(),
                "o": order,
                "a": active,
            },
        )

    # ════════════════════════════════════════════════════════════════
    # 2. section_variants — concrete designs per section
    #    One row per design. Exactly one is_active = TRUE per section.
    #    Common fields (name, is_active, display_order, scheduling)
    #    plus typed content columns specific to each section_key.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS section_variants (
                id                BIGSERIAL PRIMARY KEY,
                page_section_id   BIGINT      NOT NULL REFERENCES page_sections(id) ON DELETE CASCADE,
                variant_name      VARCHAR(150) NOT NULL,
                variant_tag       VARCHAR(50),
                display_order     INTEGER      NOT NULL DEFAULT 0,
                is_active         BOOLEAN      NOT NULL DEFAULT FALSE,

                -- ── Common typed slots ─────────────────────────────────
                headline          VARCHAR(300),
                subheadline       VARCHAR(500),
                body_text         TEXT,
                cta_text          VARCHAR(100),
                cta_link          VARCHAR(500),
                background_image_url  TEXT,
                background_video_url  TEXT,
                mobile_image_url      TEXT,
                icon_url              TEXT,
                accent_color          VARCHAR(20),

                -- ── Animation / display ───────────────────────────────
                animation_style   VARCHAR(50),
                text_alignment    VARCHAR(20),
                overlay_opacity   NUMERIC(3,2),

                -- ── Scheduling ────────────────────────────────────────
                starts_at         TIMESTAMPTZ,
                ends_at           TIMESTAMPTZ,

                -- ── Audit ─────────────────────────────────────────────
                created_by_user_id UUID REFERENCES users(id),
                updated_by_user_id UUID REFERENCES users(id),
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT chk_animation_style CHECK (
                    animation_style IS NULL OR animation_style IN
                    ('NONE','FADE','SLIDE_LEFT','SLIDE_RIGHT','SLIDE_UP','ZOOM','PARALLAX')
                ),
                CONSTRAINT chk_text_alignment CHECK (
                    text_alignment IS NULL OR text_alignment IN
                    ('LEFT','CENTER','RIGHT')
                )
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_variants_section
                ON section_variants (page_section_id, display_order)
            """
        )
    )
    # At most ONE active variant per section.
    conn.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_variants_active_per_section
                ON section_variants (page_section_id)
                WHERE is_active = TRUE
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_variants_scheduling
                ON section_variants (starts_at, ends_at)
                WHERE is_active = TRUE
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 3. site_header_config — single-row global header
    #    Singleton = TRUE constraint guarantees one row.
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS site_header_config (
                id                BIGSERIAL PRIMARY KEY,
                singleton         BOOLEAN      NOT NULL DEFAULT TRUE UNIQUE,
                logo_url          TEXT,
                logo_alt_text     VARCHAR(150),
                tagline           VARCHAR(300),
                show_search_bar   BOOLEAN      NOT NULL DEFAULT TRUE,
                show_login_button BOOLEAN      NOT NULL DEFAULT TRUE,
                cta_text          VARCHAR(100),
                cta_link          VARCHAR(500),
                support_phone     VARCHAR(20),
                contact_email     VARCHAR(150),
                nav_links         JSONB        NOT NULL DEFAULT '[]'::jsonb,
                social_links      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                background_color  VARCHAR(20),
                text_color        VARCHAR(20),
                is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
                updated_by_user_id UUID REFERENCES users(id),
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    # Seed the singleton row so the front-end never has to deal with 404s.
    conn.execute(
        sa.text(
            """
            INSERT INTO site_header_config
                (singleton, show_search_bar, show_login_button, nav_links)
            VALUES (TRUE, TRUE, TRUE, '[]'::jsonb)
            ON CONFLICT (singleton) DO NOTHING
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 4. site_footer_config — single-row global footer
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS site_footer_config (
                id                BIGSERIAL PRIMARY KEY,
                singleton         BOOLEAN      NOT NULL DEFAULT TRUE UNIQUE,
                logo_url          TEXT,
                description       TEXT,
                copyright_text    VARCHAR(300),
                company_address   TEXT,
                support_phone     VARCHAR(20),
                contact_email     VARCHAR(150),
                quick_links       JSONB        NOT NULL DEFAULT '[]'::jsonb,
                legal_links       JSONB        NOT NULL DEFAULT '[]'::jsonb,
                social_links      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                payment_icons     JSONB        NOT NULL DEFAULT '[]'::jsonb,
                app_store_links   JSONB        NOT NULL DEFAULT '{}'::jsonb,
                background_color  VARCHAR(20),
                text_color        VARCHAR(20),
                is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
                updated_by_user_id UUID REFERENCES users(id),
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO site_footer_config
                (singleton, quick_links, legal_links, social_links, payment_icons, app_store_links)
            VALUES (TRUE, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb)
            ON CONFLICT (singleton) DO NOTHING
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 5. cms_audit_versions — append-only log of activate / deactivate
    # ════════════════════════════════════════════════════════════════
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS cms_audit_versions (
                id                BIGSERIAL PRIMARY KEY,
                entity_type       VARCHAR(50)  NOT NULL,
                entity_id         BIGINT       NOT NULL,
                section_key       VARCHAR(50),
                action_type       VARCHAR(50)  NOT NULL,
                previous_value    JSONB,
                new_value         JSONB,
                changed_by_user_id UUID REFERENCES users(id),
                change_reason     TEXT,
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_cms_audit_entity
                ON cms_audit_versions (entity_type, entity_id, created_at DESC)
            """
        )
    )

    # ════════════════════════════════════════════════════════════════
    # 6. New permission codes — gated to SUPER_ADMIN + ADMIN
    # ════════════════════════════════════════════════════════════════
    for code, desc in (
        ("cms.section.manage", "Create / edit / activate homepage section variants"),
        ("cms.header_footer.manage", "Edit the global header & footer configuration"),
    ):
        conn.execute(
            sa.text(
                """
                INSERT INTO permissions (id, permission_code, permission_name, description)
                VALUES (gen_random_uuid(), :c, :c, :d)
                ON CONFLICT (permission_code) DO NOTHING
                """
            ),
            {"c": code, "d": desc},
        )

    for perm_code in ("cms.section.manage", "cms.header_footer.manage"):
        for role_code in ("SUPER_ADMIN", "ADMIN"):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                    SELECT gen_random_uuid(), r.id, p.id, NOW()
                    FROM roles r, permissions p
                    WHERE r.role_code = :rc AND p.permission_code = :pc
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"rc": role_code, "pc": perm_code},
            )


def downgrade() -> None:
    conn = op.get_bind()

    # 6. revoke perms
    for code in ("cms.section.manage", "cms.header_footer.manage"):
        conn.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id IN "
                "(SELECT id FROM permissions WHERE permission_code = :c)"
            ),
            {"c": code},
        )
        conn.execute(
            sa.text("DELETE FROM permissions WHERE permission_code = :c"),
            {"c": code},
        )

    # 5. audit
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_cms_audit_entity"))
    conn.execute(sa.text("DROP TABLE IF EXISTS cms_audit_versions"))

    # 4. footer
    conn.execute(sa.text("DROP TABLE IF EXISTS site_footer_config"))

    # 3. header
    conn.execute(sa.text("DROP TABLE IF EXISTS site_header_config"))

    # 2. variants
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_variants_scheduling"))
    conn.execute(sa.text("DROP INDEX IF EXISTS uniq_variants_active_per_section"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_variants_section"))
    conn.execute(sa.text("DROP TABLE IF EXISTS section_variants"))

    # 1. page_sections
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_page_sections_order"))
    conn.execute(sa.text("DROP TABLE IF EXISTS page_sections"))
