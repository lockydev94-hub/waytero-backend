"""0055_homepage_sections — richer homepage section catalog + structured content

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-13

Doc Ref:
  BRD Part 6 §155 — homepage admin control
  Migration 0044_website_cms (page_sections / section_variants model)

Why:
  The homepage only knew 8 sections (HERO, SERVICES, WHY_US, TOUR_PACKAGES,
  TESTIMONIALS, STATS, PARTNERS, CTA). Real travel sites also want How-It-Works
  steps, Offers & Deals, FAQ, Newsletter, App Download and a Travel-Stories
  (blog) band — and the existing Popular Destinations / Featured Hotels
  renderers were never registered as CMS sections at all.

  This migration:

    1. Adds a `content` JSONB column to section_variants so structured
       sections (steps, offers, faqs, destinations, hotels, stats) can be
       authored by admins without another migration. The admin editor shows
       a JSON editor for sections that define a content schema; the public
       homepage spreads `content` into the variant payload so renderers read
       it via the usual `pick(variant, key, fallback)`.

    2. Seeds 8 new page_sections rows (6 new + the 2 existing-but-unregistered
       renderers: POPULAR_DESTINATIONS, FEATURED_HOTELS).

    3. Seeds a default active variant for EVERY section that has none yet
       (idempotent — NOT EXISTS guard), mirroring the previous hardcoded
       homepage copy, so the homepage keeps rendering all sections from CMS
       data the moment this migration runs. Admin-created variants are never
       clobbered.

Everything is idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING / NOT EXISTS).
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

# ── New section templates ─────────────────────────────────────
NEW_SECTIONS = [
    (
        "OFFERS",
        "Offers & Deals",
        22,
        "Promotional banners and limited-time deals.",
        "offers",
    ),
    (
        "POPULAR_DESTINATIONS",
        "Popular Destinations",
        25,
        "Trending destinations grid.",
        "destinations",
    ),
    (
        "HOW_IT_WORKS",
        "How It Works",
        35,
        "Step-by-step guide to booking with WayTero.",
        "howitworks",
    ),
    ("FEATURED_HOTELS", "Featured Hotels", 45, "Handpicked partner hotels.", "hotels"),
    (
        "FAQ",
        "Frequently Asked Questions",
        85,
        "Answers to common customer questions.",
        "faq",
    ),
    ("NEWSLETTER", "Newsletter Signup", 90, "Email capture band.", "newsletter"),
    ("APP_DOWNLOAD", "App Download", 92, "Mobile app promo band.", "app"),
    ("BLOG", "Travel Stories", 95, "Latest blog posts band.", "blog"),
]

# ── Default content for content-driven sections ───────────────
_STEPS = {
    "steps": [
        {
            "title": "Search & Compare",
            "description": "Browse cabs, hotels and tour packages across 30+ cities and compare the best options in one place.",
            "icon": "Search",
        },
        {
            "title": "Choose Your Trip",
            "description": "Pick the ride, stay or package that fits your budget and dates — with transparent pricing upfront.",
            "icon": "CalendarCheck",
        },
        {
            "title": "Pay Securely",
            "description": "Pay online via wallet, UPI, cards or net-banking with 100% secure, GST-compliant billing.",
            "icon": "ShieldCheck",
        },
        {
            "title": "Travel with Ease",
            "description": "Track your cab live, check in with a tap and get 24/7 support from start to finish.",
            "icon": "Plane",
        },
    ]
}
_OFFERS = {
    "offers": [
        {
            "title": "Flat ₹300 Off on Airport Rides",
            "description": "Book any outstation airport cab this week and save ₹300 instantly.",
            "badge": "Limited time",
            "cta_text": "Book a cab",
            "cta_link": "/cabs",
            "image_url": None,
        },
        {
            "title": "Up to 25% Off on Weekend Stays",
            "description": "Enjoy discounted rates at 1,200+ partner hotels across India.",
            "badge": "Weekend special",
            "cta_text": "Explore hotels",
            "cta_link": "/hotels",
            "image_url": None,
        },
        {
            "title": "Free Pickup on Tour Packages",
            "description": "Book any 5-day tour package and get complimentary hotel pickup.",
            "badge": "Popular",
            "cta_text": "See packages",
            "cta_link": "/tours",
            "image_url": None,
        },
    ]
}
_FAQS = {
    "faqs": [
        {
            "question": "How do I book a cab with WayTero?",
            "answer": "Choose your trip type, enter pickup and drop locations and a date-time, review the fare breakdown and confirm. You can pay online or use wallet balance.",
        },
        {
            "question": "Can I get a refund if I cancel my booking?",
            "answer": "Yes — cancellations follow the policy shown at checkout. Refunds are processed back to your source or wallet within 3-5 business days.",
        },
        {
            "question": "Are the hotels verified?",
            "answer": "Every partner hotel goes through document verification, live audits and guest-rating reviews before it is listed on WayTero.",
        },
        {
            "question": "How do I track my cab in real time?",
            "answer": "Once your driver is assigned, open the Track page and enter your booking number + mobile to follow the live route and driver location.",
        },
        {
            "question": "What payment methods are accepted?",
            "answer": "UPI, debit/credit cards, net-banking and WayTero Wallet. Wallet recharges can be used for instant checkouts with wallet rewards.",
        },
        {
            "question": "Is my data safe with WayTero?",
            "answer": "Yes. All payments are processed over encrypted channels and we follow strict data-security practices. See our Privacy & Security pages for details.",
        },
    ]
}
_DESTINATIONS = {
    "destinations": [
        {
            "name": "Goa",
            "state": "Goa",
            "starting_price": 4999,
            "trending": True,
            "image_url": None,
        },
        {
            "name": "Manali",
            "state": "Himachal Pradesh",
            "starting_price": 6999,
            "trending": True,
            "image_url": None,
        },
        {
            "name": "Jaipur",
            "state": "Rajasthan",
            "starting_price": 4499,
            "trending": False,
            "image_url": None,
        },
        {
            "name": "Leh",
            "state": "Ladakh",
            "starting_price": 12999,
            "trending": True,
            "image_url": None,
        },
        {
            "name": "Andaman",
            "state": "Andaman & Nicobar",
            "starting_price": 14999,
            "trending": False,
            "image_url": None,
        },
        {
            "name": "Kerala",
            "state": "Kerala",
            "starting_price": 8999,
            "trending": True,
            "image_url": None,
        },
        {
            "name": "Rishikesh",
            "state": "Uttarakhand",
            "starting_price": 3499,
            "trending": False,
            "image_url": None,
        },
        {
            "name": "Udaipur",
            "state": "Rajasthan",
            "starting_price": 5499,
            "trending": False,
            "image_url": None,
        },
    ]
}
_HOTELS = {
    "hotels": [
        {
            "name": "The Leela Palace",
            "city": "New Delhi",
            "rating": 4.9,
            "reviews": 2103,
            "price": 18500,
            "old_price": 22000,
            "amenities": ["Wifi", "Pool", "Breakfast"],
            "tag": "Editor's Pick",
            "image_url": None,
        },
        {
            "name": "Taj Lake Palace",
            "city": "Udaipur",
            "rating": 4.9,
            "reviews": 1542,
            "price": 32500,
            "amenities": ["Wifi", "Lake View", "Spa"],
            "image_url": None,
        },
        {
            "name": "Radisson Blu",
            "city": "Bengaluru",
            "rating": 4.6,
            "reviews": 982,
            "price": 9500,
            "amenities": ["Wifi", "Breakfast", "Gym"],
            "image_url": None,
        },
        {
            "name": "Marriott Resort",
            "city": "Goa",
            "rating": 4.7,
            "reviews": 1847,
            "price": 14999,
            "old_price": 17999,
            "amenities": ["Beach", "Pool", "Wifi"],
            "tag": "Beachfront",
            "image_url": None,
        },
    ]
}
_STATS = {
    "stats": [
        {"value": 50000, "suffix": "+", "label": "Happy Travelers"},
        {"value": 1200, "suffix": "+", "label": "Partner Hotels"},
        {"value": 800, "suffix": "+", "label": "Verified Drivers"},
        {"value": 30, "suffix": "+", "label": "Cities"},
    ]
}

# ── Default variant per section (only inserted when none exists) ─
DEFAULT_VARIANTS = [
    {
        "key": "HERO",
        "name": "Default Hero",
        "tag": "India's Travel Operating System",
        "headline": "Book cabs, hotels & tours|across India in seconds",
        "subheadline": "Your travel companion for every journey — instant cab booking, verified hotels and curated tour packages.",
        "cta_text": "Explore Tour Packages",
        "cta_link": "/tours",
        "content": None,
    },
    {
        "key": "SERVICES",
        "name": "Default Services",
        "tag": "What we offer",
        "headline": "Everything for Your Journey",
        "subheadline": "One platform for every travel need — from your first mile to your last check-out.",
        "cta_text": "Explore services",
        "cta_link": "/cabs",
        "content": None,
    },
    {
        "key": "WHY_US",
        "name": "Default Why Us",
        "tag": "Why choose us",
        "headline": "Built for Indian Travelers",
        "subheadline": "We understand the nuances of travel across India — and built WayTero to solve them.",
        "cta_text": None,
        "cta_link": None,
        "content": None,
    },
    {
        "key": "TOUR_PACKAGES",
        "name": "Default Tour Packages",
        "tag": "Curated experiences",
        "headline": "Tour Packages Loved by Travelers",
        "subheadline": "Handpicked itineraries with verified partners and transparent pricing.",
        "cta_text": "View all packages",
        "cta_link": "/tours",
        "content": None,
    },
    {
        "key": "TESTIMONIALS",
        "name": "Default Testimonials",
        "tag": "Loved by travelers",
        "headline": "What Our Customers Say",
        "subheadline": "4.8 / 5 average rating across 50,000+ verified reviews.",
        "cta_text": None,
        "cta_link": None,
        "content": None,
    },
    {
        "key": "STATS",
        "name": "Default Stats",
        "tag": "Trusted across India",
        "headline": "Numbers that speak",
        "subheadline": None,
        "cta_text": None,
        "cta_link": None,
        "content": _STATS,
    },
    {
        "key": "PARTNERS",
        "name": "Default Partners",
        "tag": "Trusted partners",
        "headline": "Backed by India's Best",
        "subheadline": "We work with the brands you trust — and the partners who power every journey.",
        "cta_text": "Become a Partner",
        "cta_link": "/partner",
        "content": None,
    },
    {
        "key": "CTA",
        "name": "Default CTA",
        "tag": None,
        "headline": "Ready for your next journey?",
        "subheadline": "Join 50,000+ Indian travelers who book smarter with WayTero.",
        "cta_text": "Start Booking",
        "cta_link": "/cabs",
        "content": None,
    },
    {
        "key": "POPULAR_DESTINATIONS",
        "name": "Default Destinations",
        "tag": "Where to next",
        "headline": "Popular Destinations",
        "subheadline": "Trending spots this season — handpicked for every kind of traveler.",
        "cta_text": None,
        "cta_link": None,
        "content": _DESTINATIONS,
    },
    {
        "key": "FEATURED_HOTELS",
        "name": "Default Featured Hotels",
        "tag": "Where to stay",
        "headline": "Featured Hotels",
        "subheadline": "Top-rated stays handpicked by our travel team.",
        "cta_text": None,
        "cta_link": None,
        "content": _HOTELS,
    },
    {
        "key": "HOW_IT_WORKS",
        "name": "Default How It Works",
        "tag": "Simple & transparent",
        "headline": "How WayTero Works",
        "subheadline": "Book your entire trip in four simple steps.",
        "cta_text": None,
        "cta_link": None,
        "content": _STEPS,
    },
    {
        "key": "OFFERS",
        "name": "Default Offers",
        "tag": "Limited-time deals",
        "headline": "Offers & Deals",
        "subheadline": "Grab these limited-time deals on cabs, hotels and tour packages.",
        "cta_text": None,
        "cta_link": None,
        "content": _OFFERS,
    },
    {
        "key": "FAQ",
        "name": "Default FAQ",
        "tag": "Help centre",
        "headline": "Frequently Asked Questions",
        "subheadline": "Everything you need to know before you book.",
        "cta_text": "Visit support",
        "cta_link": "/support",
        "content": _FAQS,
    },
    {
        "key": "NEWSLETTER",
        "name": "Default Newsletter",
        "tag": "Stay in the loop",
        "headline": "Get travel deals in your inbox",
        "subheadline": "Subscribe for weekly offers, new destinations and booking tips. No spam — unsubscribe anytime.",
        "cta_text": "Subscribe",
        "cta_link": None,
        "content": None,
    },
    {
        "key": "APP_DOWNLOAD",
        "name": "Default App Download",
        "tag": "On the go",
        "headline": "Travel Smarter with the WayTero App",
        "subheadline": "Book, track and manage trips on the go — available on iOS and Android.",
        "cta_text": "Download the app",
        "cta_link": None,
        "content": None,
    },
    {
        "key": "BLOG",
        "name": "Default Blog",
        "tag": "Travel stories",
        "headline": "Travel Stories & Tips",
        "subheadline": "Guides, itineraries and insider tips from our team.",
        "cta_text": "Read the blog",
        "cta_link": "/blog",
        "content": None,
    },
]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Structured content column on variants.
    conn.execute(
        sa.text("ALTER TABLE section_variants ADD COLUMN IF NOT EXISTS content JSONB")
    )

    # 2. Seed the new section templates.
    for key, name, order, desc, icon in NEW_SECTIONS:
        conn.execute(
            sa.text(
                """
                INSERT INTO page_sections
                    (section_key, display_name, description, icon, display_order, is_visible, is_active)
                VALUES (:k, :n, :d, :ic, :o, TRUE, TRUE)
                ON CONFLICT (section_key) DO NOTHING
                """
            ),
            {"k": key, "n": name, "d": desc, "ic": icon, "o": order},
        )

    # 3. Default active variant for every section that has none yet —
    #    so the homepage renders the full catalog from CMS data. Existing
    #    admin-authored variants are left untouched.
    for v in DEFAULT_VARIANTS:
        content = json.dumps(v["content"]) if v["content"] is not None else None
        conn.execute(
            sa.text(
                """
                INSERT INTO section_variants
                    (page_section_id, variant_name, variant_tag, display_order, is_active,
                     headline, subheadline, cta_text, cta_link, content)
                SELECT ps.id, :name, :tag, 0, TRUE, :headline, :subheadline, :cta_text, :cta_link, CAST(:content AS JSONB)
                FROM page_sections ps
                WHERE ps.section_key = :key
                  AND NOT EXISTS (
                      SELECT 1 FROM section_variants sv WHERE sv.page_section_id = ps.id
                  )
                """
            ),
            {
                "key": v["key"],
                "name": v["name"],
                "tag": v["tag"],
                "headline": v["headline"],
                "subheadline": v["subheadline"],
                "cta_text": v["cta_text"],
                "cta_link": v["cta_link"],
                "content": content,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Remove the newly-seeded default variants + section templates.
    conn.execute(
        sa.text(
            """
            DELETE FROM section_variants
            WHERE page_section_id IN (
                SELECT ps.id FROM page_sections ps
                WHERE ps.section_key IN (
                    'OFFERS', 'POPULAR_DESTINATIONS', 'HOW_IT_WORKS',
                    'FEATURED_HOTELS', 'FAQ', 'NEWSLETTER', 'APP_DOWNLOAD', 'BLOG'
                )
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            DELETE FROM page_sections
            WHERE section_key IN (
                'OFFERS', 'POPULAR_DESTINATIONS', 'HOW_IT_WORKS',
                'FEATURED_HOTELS', 'FAQ', 'NEWSLETTER', 'APP_DOWNLOAD', 'BLOG'
            )
            """
        )
    )
    conn.execute(sa.text("ALTER TABLE section_variants DROP COLUMN IF EXISTS content"))
