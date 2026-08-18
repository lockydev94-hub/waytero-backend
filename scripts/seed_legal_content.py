#!/usr/bin/env python3
"""
WayTero — Legal Content Seeder
================================
Seeds the customer website's legal pages into `system_configurations`
(keys `LEGAL_PAGE_*`) as JSON so the pages render the same text the
platform actually enforces:

  * LEGAL_PAGE_PRIVACY              — privacy policy (incl. account deletion)
  * LEGAL_PAGE_TERMS                — terms of service (incl. booking instructions)
  * LEGAL_PAGE_REFUND               — refund & cancellation policy framework
  * LEGAL_PAGE_COOKIES              — cookie policy
  * LEGAL_PAGE_BOOKING_INSTRUCTIONS — how to book cab / hotel / tour

The refund page additionally renders LIVE ladder numbers from
`system_configurations` via `GET /public/cancellation-policy`, so the
numeric tiers always match the cancellation engine even after an admin
edits them. This seeder only writes the prose framework.

Idempotent — `ON CONFLICT (config_key) DO NOTHING`; it never overwrites
values an admin has already set. Safe to run on every backend restart.

Run via lifespan (app/core/lifespan.py) after alembic migrations, and
manually: `python -m scripts.seed_legal_content`
"""

import asyncio
import json
import os
import sys

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402

LEGAL_PAGES = {
    "LEGAL_PAGE_PRIVACY": {
        "slug": "privacy",
        "title": "Privacy Policy",
        "eyebrow": "Legal",
        "description": "WayTero's privacy policy — how we collect, use, and protect your data.",
        "effective_date": "2026-01-01",
        "updated_at": "2026-08-15",
        "sections": [
            {
                "heading": "1. Data we collect",
                "body": (
                    "WayTero Travel Technologies Pvt Ltd (\"WayTero\", \"we\", \"us\") collects only the "
                    "data needed to deliver your travel services: account information (name, email, "
                    "mobile number), trip information (pickup, drop, dates, passengers), payment "
                    "metadata (processed by our payment partners — card numbers are never stored by us), "
                    "and device/usage logs used for fraud prevention and product improvement."
                ),
            },
            {
                "heading": "2. How we use your data",
                "body": (
                    "We use your data to operate the platform, match you with verified partners "
                    "(drivers, hotels, tour operators), process payments and refunds, send booking "
                    "updates, and prevent abuse. We do not sell your personal data."
                ),
            },
            {
                "heading": "3. Sharing with partners",
                "body": (
                    "We share only the minimum trip information required for the relevant partner to "
                    "deliver your booking. All partners are bound by data-processing agreements."
                ),
            },
            {
                "heading": "4. Refunds & wallet",
                "body": (
                    "Refunds are computed by our cancellation engine from the applicable policy (cab, "
                    "hotel or tour) and credited to your WayTero Travel Wallet. See the Refund Policy "
                    "page for the current tiers."
                ),
            },
            {
                "heading": "5. Account deletion",
                "body": (
                    "You may request deletion of your account at any time from the dedicated "
                    "Account Deletion page. Requests are reviewed by our team and processed within "
                    "30 days. On deletion, your login is disabled and personal identifiers are "
                    "removed; booking and financial records are retained in anonymised form as "
                    "required by law."
                ),
            },
            {
                "heading": "6. Your rights",
                "body": (
                    "You can request an export of your data, correction, or deletion by raising an "
                    "account deletion request or emailing privacy@waytero.com. We respond within 30 days."
                ),
            },
            {
                "heading": "7. Security",
                "body": (
                    "All data is encrypted in transit (TLS 1.2+) and at rest. Authentication uses "
                    "email (Firebase) or mobile OTP with platform-managed JWTs. See our Security page "
                    "for details."
                ),
            },
        ],
    },
    "LEGAL_PAGE_TERMS": {
        "slug": "terms",
        "title": "Terms of Service",
        "eyebrow": "Legal",
        "description": "WayTero's terms of service — please read before using the platform.",
        "effective_date": "2026-01-01",
        "updated_at": "2026-08-15",
        "sections": [
            {
                "heading": "1. Service description",
                "body": (
                    "WayTero is a travel marketplace connecting customers with verified cab drivers, "
                    "hotels, and tour operators. We facilitate bookings, payments and support; the "
                    "underlying service is delivered by our partners."
                ),
            },
            {
                "heading": "2. Bookings & payments",
                "body": (
                    "Bookings are confirmed only after payment confirmation (full payment or the "
                    "advance requested by the partner). Cancellation refunds are governed by our "
                    "Refund Policy and computed automatically by the cancellation engine based on "
                    "the tier in effect at the time of the request."
                ),
            },
            {
                "heading": "3. Booking instructions",
                "body": (
                    "Before you book: (1) verify the pickup/drop, dates and passenger count; "
                    "(2) check the applicable cancellation policy and any advance required; "
                    "(3) keep your registered mobile reachable for driver/partner updates. "
                    "After you book you can track the trip, view invoices, and cancel from the "
                    "Bookings page. See the Booking Instructions page for the full guide."
                ),
            },
            {
                "heading": "4. User conduct",
                "body": (
                    "You agree to provide accurate booking information, treat partners respectfully, "
                    "and not misuse the platform for fraudulent activity. WayTero reserves the right "
                    "to suspend accounts that violate these terms."
                ),
            },
            {
                "heading": "5. Liability",
                "body": (
                    "WayTero acts as an intermediary. Service delivery is the partner's "
                    "responsibility; we facilitate dispute resolution in good faith."
                ),
            },
            {
                "heading": "6. Account deletion",
                "body": (
                    "You may delete your account via the Account Deletion page. Deletion is "
                    "irreversible: your login is disabled, wallet balance and loyalty value are "
                    "forfeited, and personal identifiers are removed."
                ),
            },
            {
                "heading": "7. Governing law",
                "body": (
                    "These terms are governed by the laws of India. Disputes are subject to the "
                    "jurisdiction of courts in Bhubaneswar, Odisha."
                ),
            },
        ],
    },
    "LEGAL_PAGE_REFUND": {
        "slug": "refund",
        "title": "Refund & Cancellation Policy",
        "eyebrow": "Policy",
        "description": "WayTero's tiered refund policy for cab, hotel, and tour bookings.",
        "effective_date": "2026-01-01",
        "updated_at": "2026-08-15",
        "sections": [
            {
                "heading": "How refunds work",
                "body": (
                    "WayTero uses a tiered, time-based refund engine. The closer to your pickup, "
                    "check-in or travel date, the higher the cancellation charge. Refunds are "
                    "computed automatically at cancellation time and credited to your WayTero "
                    "Travel Wallet — you are never charged more than the advance you actually paid."
                ),
            },
            {
                "heading": "Cab bookings",
                "body": (
                    "Cab refunds follow a global time-based ladder measured against pickup time. "
                    "The current tiers are displayed live on this page below. Once a partner has "
                    "been assigned, a post-assignment refund percent applies; after pickup there "
                    "is no refund."
                ),
            },
            {
                "heading": "Hotel bookings",
                "body": (
                    "Each hotel sets its own cancellation ladder (typically free up to 72 hours, "
                    "75% up to 48 hours, 50% up to 24 hours, none on the day). The applicable "
                    "policy is shown on the hotel page before you book and in the refund preview "
                    "before you cancel."
                ),
            },
            {
                "heading": "Tour packages",
                "body": (
                    "Tour refunds follow a day-based ladder measured against the travel start "
                    "date (default: free up to 30 days, 75% up to 15 days, 50% up to 7 days, "
                    "none after). Some packages carry their own policy, shown on the package "
                    "page — the current tiers are displayed live on this page below."
                ),
            },
            {
                "heading": "Wallet credits",
                "body": (
                    "All refunds credit your wallet instantly and can be used toward any future "
                    "booking. Wallet refunds are never deducted later for the same cancellation."
                ),
            },
        ],
    },
    "LEGAL_PAGE_COOKIES": {
        "slug": "cookies",
        "title": "Cookie Policy",
        "eyebrow": "Legal",
        "description": "How WayTero uses cookies and similar technologies.",
        "effective_date": "2026-01-01",
        "updated_at": "2026-08-15",
        "sections": [
            {
                "heading": "1. What we use",
                "body": (
                    "We use essential cookies to keep you signed in and secure, analytics cookies "
                    "to understand usage, and preference cookies to remember your choices. We do "
                    "not use cookies to sell your data."
                ),
            },
            {
                "heading": "2. Managing cookies",
                "body": (
                    "You can block or delete cookies in your browser settings at any time. Blocking "
                    "essential cookies may prevent sign-in and booking from working."
                ),
            },
        ],
    },
    "LEGAL_PAGE_BOOKING_INSTRUCTIONS": {
        "slug": "booking-instructions",
        "title": "Booking Instructions",
        "eyebrow": "Guide",
        "description": "How to book a cab, hotel, or tour package on WayTero.",
        "effective_date": "2026-01-01",
        "updated_at": "2026-08-15",
        "sections": [
            {
                "heading": "1. Book a cab",
                "body": (
                    "Enter pickup and drop, choose the trip type (one-way, round trip, outstation), "
                    "and pick the vehicle category. A fare estimate is shown before you confirm. "
                    "If the partner requests an advance, pay it to confirm; the driver receives "
                    "your trip automatically once assigned. Track your cab live from the Track page."
                ),
            },
            {
                "heading": "2. Book a hotel",
                "body": (
                    "Search by city and dates, filter by price/amenities, and review the hotel's "
                    "cancellation policy before booking. Confirm with the room category and guest "
                    "count. Some properties require an advance; the balance is collected at check-in "
                    "or per the property's terms."
                ),
            },
            {
                "heading": "3. Book a tour package",
                "body": (
                    "Browse tour packages by destination and duration. Check the package's "
                    "cancellation policy and what is included (transport, stay, meals, activities). "
                    "Pay the required advance to confirm; the tour operator confirms the booking "
                    "and the itinerary is available to download."
                ),
            },
            {
                "heading": "4. Payments & advances",
                "body": (
                    "Payments are processed securely by our payment partners. Advances are shown "
                    "on the booking detail with receipts. The balance due is displayed on every "
                    "booking; collect and settle through the partner at the time of service."
                ),
            },
            {
                "heading": "5. Cancelling a booking",
                "body": (
                    "You can preview the exact refund before you cancel from the Bookings page. "
                    "Cancellation rules differ per service (cab ladder, hotel policy, tour policy) "
                    "and are computed by the engine at the moment you cancel. Refunds credit your "
                    "wallet instantly."
                ),
            },
            {
                "heading": "6. Invoices & support",
                "body": (
                    "Tax invoices are available for completed trips from the booking detail and "
                    "customer app. For any issue, raise it from the Support page or contact our "
                    "customer care team."
                ),
            },
        ],
    },
}


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        for key, page in LEGAL_PAGES.items():
            await db.execute(
                text(
                    """
                    INSERT INTO system_configurations (config_key, config_value, description, updated_at)
                    VALUES (:k, :v, :d, NOW())
                    ON CONFLICT (config_key) DO NOTHING
                    """
                ),
                {
                    "k": key,
                    "v": json.dumps(page, ensure_ascii=False),
                    "d": f"Legal page: /{page['slug']} (seeded)",
                },
            )
        await db.commit()
    await engine.dispose()
    print("legal_content_seeded: 5 pages (privacy, terms, refund, cookies, booking-instructions)")


if __name__ == "__main__":
    asyncio.run(main())
