#!/usr/bin/env python3
"""
WayTero — CMS Default Data Seeder
===================================
Seeds default values into site_header_config and site_footer_config
singleton rows so the customer-web always renders properly even before
the admin configures anything in the CMS panel.

Idempotent — only updates NULL/empty fields; never overwrites data the
admin has already set. Safe to run on every backend restart.

Run via lifespan (app/core/lifespan.py) after alembic migrations.
"""

import asyncio
import sys
import os

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.config import settings

# ── Default values ─────────────────────────────────────────────────────────────
DEFAULT_NAV_LINKS = json.dumps([
    {"label": "Cabs",   "href": "/cabs",   "icon": "car"},
    {"label": "Hotels", "href": "/hotels", "icon": "hotel"},
    {"label": "Tours",  "href": "/tours",  "icon": "compass"},
    {"label": "Track",  "href": "/track",  "icon": "navigation"},
])

DEFAULT_SOCIAL_LINKS = json.dumps({
    "facebook":  "https://facebook.com/waytero",
    "twitter":   "https://twitter.com/waytero",
    "instagram": "https://instagram.com/waytero",
    "linkedin":  "https://linkedin.com/company/waytero",
    "youtube":   "https://youtube.com/@waytero",
})

DEFAULT_QUICK_LINKS = json.dumps([
    {"label": "Book a Cab",    "href": "/cabs"},
    {"label": "Find Hotels",   "href": "/hotels"},
    {"label": "Tour Packages", "href": "/tours"},
    {"label": "Live Tracking", "href": "/track"},
    {"label": "Travel Wallet", "href": "/wallet"},
])

DEFAULT_LEGAL_LINKS = json.dumps([
    {"label": "Privacy Policy",   "href": "/privacy"},
    {"label": "Terms of Service", "href": "/terms"},
    {"label": "Refund Policy",    "href": "/refund"},
    {"label": "Cookie Policy",    "href": "/cookies"},
])

COPYRIGHT_TEXT = (
    "© 2025 WayTero Travel Technologies Pvt Ltd · "
    "CIN U63090OR2023PTC · Built in Bhubaneswar, Odisha"
)

FOOTER_DESCRIPTION = (
    "India's Travel Operating System — connecting customers, partners, "
    "drivers, hotels, and tour operators on one seamless platform."
)


async def seed(session: AsyncSession) -> None:
    # ── Header ────────────────────────────────────────────────────
    # Use COALESCE so we never overwrite values the admin already set.
    await session.execute(
        text("""
            UPDATE site_header_config
            SET
                logo_alt_text     = COALESCE(NULLIF(logo_alt_text, ''),     'WayTero'),
                tagline           = COALESCE(NULLIF(tagline, ''),            'India''s Travel OS'),
                cta_text          = COALESCE(NULLIF(cta_text, ''),          'Book a Cab'),
                cta_link          = COALESCE(NULLIF(cta_link, ''),          '/cabs'),
                support_phone     = COALESCE(NULLIF(support_phone, ''),     '1800-WAYTERO'),
                contact_email     = COALESCE(NULLIF(contact_email, ''),     'support@waytero.com'),
                nav_links         = CASE
                                      WHEN nav_links  = '[]'::jsonb OR nav_links  IS NULL
                                      THEN :nav_links::jsonb
                                      ELSE nav_links
                                    END,
                social_links      = CASE
                                      WHEN social_links = '{}'::jsonb OR social_links IS NULL
                                      THEN :social_links::jsonb
                                      ELSE social_links
                                    END,
                show_search_bar   = COALESCE(show_search_bar,   TRUE),
                show_login_button = COALESCE(show_login_button, TRUE),
                is_active         = COALESCE(is_active, TRUE)
            WHERE singleton = TRUE
        """),
        {
            "nav_links":    DEFAULT_NAV_LINKS,
            "social_links": DEFAULT_SOCIAL_LINKS,
        },
    )
    print("[cms_seeder] ✅ site_header_config defaults applied")

    # ── Footer ────────────────────────────────────────────────────
    await session.execute(
        text("""
            UPDATE site_footer_config
            SET
                description     = COALESCE(NULLIF(description, ''),     :description),
                copyright_text  = COALESCE(NULLIF(copyright_text, ''), :copyright_text),
                company_address = COALESCE(NULLIF(company_address, ''), 'Bhubaneswar, Odisha, India'),
                support_phone   = COALESCE(NULLIF(support_phone, ''),   '1800-WAYTERO'),
                contact_email   = COALESCE(NULLIF(contact_email, ''),   'support@waytero.com'),
                quick_links     = CASE
                                    WHEN quick_links = '[]'::jsonb OR quick_links IS NULL
                                    THEN :quick_links::jsonb
                                    ELSE quick_links
                                  END,
                legal_links     = CASE
                                    WHEN legal_links = '[]'::jsonb OR legal_links IS NULL
                                    THEN :legal_links::jsonb
                                    ELSE legal_links
                                  END,
                social_links    = CASE
                                    WHEN social_links = '{}'::jsonb OR social_links IS NULL
                                    THEN :social_links::jsonb
                                    ELSE social_links
                                  END,
                is_active       = COALESCE(is_active, TRUE)
            WHERE singleton = TRUE
        """),
        {
            "description":    FOOTER_DESCRIPTION,
            "copyright_text": COPYRIGHT_TEXT,
            "quick_links":    DEFAULT_QUICK_LINKS,
            "legal_links":    DEFAULT_LEGAL_LINKS,
            "social_links":   DEFAULT_SOCIAL_LINKS,
        },
    )
    print("[cms_seeder] ✅ site_footer_config defaults applied")

    await session.commit()
    print("[cms_seeder] ✅ CMS defaults committed")


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with async_session() as session:
            await seed(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
