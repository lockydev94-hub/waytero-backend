"""0092_why_and_how_copy — "Why Choose WayTero?" and "How It Works" copy

Revision ID: 0092
Revises: 0091

Doc Ref:
  Migration 0055_homepage_sections — seeds WHY_US and HOW_IT_WORKS plus their
  default variants, both of which this updates;
  BRD Part 6 §155 — homepage admin control.

Why:
  The two bands under the hero were redesigned to the 2026-11 mock:

    WHY_US       — one pastel panel: claim on the left, five proof points on
                   the right. The panel carries its own heading, so the band
                   has no eyebrow, and the five reasons (with their icons and
                   tones) are seeded as `content.items` instead of living in
                   the renderer's DEFAULT.
    HOW_IT_WORKS — "How It Works" with the dashes either side of the title;
                   the four steps are re-worded to the mock's.

  Seeded rather than hardcoded so the admin can edit every word and card,
  and so the editor shows what the site renders.

  Idempotent: both statements are plain UPDATEs keyed on section_key, and the
  JSONB content is merged (`||`) so a re-run rewrites the same keys.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

_WHY_ITEMS = [
    {
        "icon": "Zap",
        "title": "Instant Booking",
        "desc": "Book in a few simple steps.",
        "tone": "accent",
    },
    {
        "icon": "ShieldCheck",
        "title": "Verified Partners",
        "desc": "Background-checked drivers & operators.",
        "tone": "success",
    },
    {
        "icon": "IndianRupee",
        "title": "Transparent Pricing",
        "desc": "Know your fare upfront.",
        "tone": "warning",
    },
    {
        "icon": "Lock",
        "title": "Secure Payments",
        "desc": "Multiple safe and reliable options.",
        "tone": "danger",
    },
    {
        "icon": "Headphones",
        "title": "24x7 Support",
        "desc": "Get help anytime, anywhere.",
        "tone": "primary",
    },
]

_HOW_STEPS = [
    {
        "icon": "Search",
        "title": "Choose Service",
        "description": "Select Cab or Tour Package.",
    },
    {
        "icon": "CalendarCheck",
        "title": "Enter Details",
        "description": "Add your trip details and preferences.",
    },
    {
        "icon": "Car",
        "title": "Confirm & Pay",
        "description": "Review, pay securely and get confirmation.",
    },
    {
        "icon": "CheckCircle2",
        "title": "Travel & Enjoy",
        "description": "Experience a safe, comfortable journey.",
    },
]

_OLD_HOW_STEPS = [
    {
        "icon": "Search",
        "title": "Search & Compare",
        "description": "Browse cabs, hotels and tour packages and compare the best options in one place.",
    },
    {
        "icon": "CalendarCheck",
        "title": "Choose Your Trip",
        "description": "Pick the ride, stay or package that fits your budget and dates — with transparent pricing upfront.",
    },
    {
        "icon": "ShieldCheck",
        "title": "Pay Securely",
        "description": "Pay online via wallet, UPI, cards or net-banking with 100% secure, GST-compliant billing.",
    },
    {
        "icon": "Plane",
        "title": "Travel with Ease",
        "description": "Track your cab live, check in with a tap and get 24/7 support from start to finish.",
    },
]

_UPDATE_WHY = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag,
        headline = :headline,
        subheadline = :subheadline,
        content = COALESCE(sv.content, '{}'::jsonb) || CAST(:content AS JSONB)
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'WHY_US'
"""

_UPDATE_HOW = """
    UPDATE section_variants sv
    SET variant_tag = :variant_tag,
        headline = :headline,
        subheadline = :subheadline,
        content = COALESCE(sv.content, '{}'::jsonb) || CAST(:content AS JSONB)
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'HOW_IT_WORKS'
"""

_DROP_WHY_ITEMS = """
    UPDATE section_variants sv
    SET content = COALESCE(sv.content, '{}'::jsonb) - 'items'
    FROM page_sections ps
    WHERE ps.id = sv.page_section_id
      AND ps.section_key = 'WHY_US'
"""


def _why(conn, variant_tag, headline, subheadline, content=None):
    conn.execute(
        sa.text(_UPDATE_WHY),
        {
            "variant_tag": variant_tag,
            "headline": headline,
            "subheadline": subheadline,
            "content": json.dumps(content or {}),
        },
    )


def _how(conn, variant_tag, headline, subheadline, steps):
    conn.execute(
        sa.text(_UPDATE_HOW),
        {
            "variant_tag": variant_tag,
            "headline": headline,
            "subheadline": subheadline,
            "content": json.dumps({"steps": steps}),
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    _why(
        conn,
        None,
        "Why Choose WayTero?",
        "A trusted travel platform for safe, comfortable and hassle-free journeys.",
        {"items": _WHY_ITEMS},
    )
    _how(
        conn,
        None,
        "How It Works",
        "Book your cab or tour package in just a few simple steps.",
        _HOW_STEPS,
    )


def downgrade() -> None:
    conn = op.get_bind()
    _why(
        conn,
        "Why choose us",
        "Built for Indian Travelers",
        "We understand the nuances of travel across India — and built WayTero to solve them.",
    )
    conn.execute(sa.text(_DROP_WHY_ITEMS))
    _how(
        conn,
        "Simple & transparent",
        "How WayTero Works",
        "Book your entire trip in four simple steps.",
        _OLD_HOW_STEPS,
    )
