#!/usr/bin/env python3
"""
WayTero — Offers demo seeder (hero slider / offer pages / booking ride-through).

Idempotent raw-SQL seed for the coupon-backed Offers feature (migration 0077):
  * 3 demo coupons — one per cost-bearer type (PLATFORM, PARTNER_ONLY,
    PLATFORM_PARTNER) with the service/partner rules each type needs
  * 4 published offers (CAB x2, HOTEL, TOUR) linked to real supply:
      CAB   — SUV (category 3) in Bhubaneswar, partner 2 Maa Travels +
              partner 3 Utkal Cab both have ACTIVE vehicles + pricing
      HOTEL — hotel 1 Grand Imperial, room category 1 Deluxe King
      TOUR  — tour package 1 Magical Odisha Heritage (partner 2)
  * refreshes the old smoke-test offer's placeholder image
    (https://example.com/smoke.png) with that hotel's real primary photo

Images are existing Cloudinary assets of this project, re-served with the
same circular 512x512 webp crop the admin MediaUploader produces
(g__face→no crop needed here: crop="fill" keeps composition; the storefront
also applies its own round mask).

Direct-DB caveat (project convention): partners normally come via the portal
flow; this script only ADDS marketing rows over supply that already exists —
pre-checks fail loudly when a prerequisite is missing, and re-running never
duplicates or clobbers admin edits (COALESCE keeps manually changed titles /
descriptions / images).

Usage (from the backend project root):
    python -m scripts.seed_offer_demo
"""

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

VALID_FROM = date(2026, 10, 12)
VALID_TO = date(2026, 11, 10)

CLOUDINARY_BASE = "https://res.cloudinary.com/jsrlg7ye/image/upload"
CIRCLE = "w_512,h_512,c_fill,g_auto,f_webp"


def circle(url: str) -> str:
    """Inject the crop transformation into an existing project Cloudinary URL."""
    marker = "/upload/"
    head, tail = url.split(marker, 1)
    return f"{head}{marker}{CIRCLE}/{tail}"


IMG_SUV = circle(
    f"{CLOUDINARY_BASE}/v1785401416/vehicle-categories/image/general_1785401411.png"
)
IMG_CITY_SUV = circle(
    f"{CLOUDINARY_BASE}/v1785401751/vehicle-categories/image/general_1785401746.png"
)
IMG_HOTEL = circle(
    f"{CLOUDINARY_BASE}/v1785961951/waytero/hotels/images/photo_1785961949.jpg"
)
IMG_TOUR = circle(f"{CLOUDINARY_BASE}/v1786709862/waytero/tours/general_1786709856.png")
SMOKE_PLACEHOLDER = "https://example.com/smoke.png"

TERMS_GENERIC = """1. Discount is applied only when the coupon code is applied at booking and the server confirms it is still valid.
2. Valid for bookings made between {frm} and {to} (system date), subject to the coupon's own validity window.
3. Cannot be combined with any other coupon or promotional discount on the same booking.
4. The listed discount is the maximum benefit; the actual amount is computed by the coupon engine at booking time and may be lower for small bookings.
5. Cancellations follow the cancellation policy of the booked service; any refunded discount returns to the coupon's usage pool per platform policy.
6. WayTero may end or change this offer; the coupon's live terms at booking time always prevail.""".format(
    frm=VALID_FROM.strftime("%d %b %Y"), to=VALID_TO.strftime("%d %b %Y")
)

# coupon_code, title, type, apply_to, dtype, dvalue, max_disc, min_book,
# platform_share_pct, service/partner rule spec
COUPONS = [
    {
        "code": "HERO10",
        "title": "Hero Week SUV Discount",
        "type": "PLATFORM",
        "apply_to": "CAB",
        "discount_type": "PERCENTAGE",
        "discount_value": "10.00",
        "max_discount": "150.00",
        "min_booking": "800.00",
        "platform_share_percent": None,
        "service_rules": [("CAB", 3)],
        "partner_ids": [],
    },
    {
        "code": "CABSURPRISE",
        "title": "Maa Travels SUV Flat Saver",
        "type": "PARTNER_ONLY",
        "apply_to": "CAB",
        "discount_type": "FLAT",
        "discount_value": "200.00",
        "max_discount": None,
        "min_booking": "900.00",
        "platform_share_percent": None,
        "service_rules": [("CAB", 3)],
        # PARTNER_ONLY: cohort must contain exactly the funding partner
        "partner_ids": [2],
    },
    {
        "code": "STAY20",
        "title": "Stay & Tour Share Offer",
        "type": "PLATFORM_PARTNER",
        "apply_to": "ALL",
        "discount_type": "PERCENTAGE",
        "discount_value": "20.00",
        "max_discount": "500.00",
        "min_booking": "1500.00",
        "platform_share_percent": 40,
        "service_rules": [],
        # Both hotel 1 and tour 1 belong to partner 2 (Maa Travels)
        "partner_ids": [2],
    },
]

# slug, coupon_code, service_type, vehicle_category_id, title, short_desc, image
OFFERS = [
    {
        "slug": "weekend-suv-deal",
        "coupon": "HERO10",
        "service_type": "CAB",
        "vehicle_category_id": 3,
        "title": "Weekend SUV Deal",
        "short": "10% off SUV trips in Bhubaneswar, up to ₹150 saved.",
        "image": IMG_SUV,
        "sort": 1,
    },
    {
        "slug": "city-suv-flat-saver",
        "coupon": "CABSURPRISE",
        "service_type": "CAB",
        "vehicle_category_id": 3,
        "title": "City SUV Flat Saver",
        "short": "Flat ₹200 off on Maa Travels SUV bookings.",
        "image": IMG_CITY_SUV,
        "sort": 2,
    },
    {
        "slug": "grand-imperial-deluxe-escape",
        "coupon": "STAY20",
        "service_type": "HOTEL",
        "vehicle_category_id": None,
        "title": "Grand Imperial Deluxe Escape",
        "short": "20% off Deluxe King stays at Grand Imperial, up to ₹500.",
        "image": IMG_HOTEL,
        "sort": 3,
        "hotel_links": [{"hotel_id": 1, "room_category_ids": [1]}],
    },
    {
        "slug": "magical-odisha-tour-deal",
        "coupon": "STAY20",
        "service_type": "TOUR",
        "vehicle_category_id": None,
        "title": "Magical Odisha Tour Deal",
        "short": "20% off the Magical Odisha Heritage Tour, up to ₹500.",
        "image": IMG_TOUR,
        "sort": 4,
        "tour_ids": [1],
    },
]


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.begin() as c:

            async def one(sql: str, **params):
                return (await c.execute(text(sql), params)).mappings().first()

            # ── Pre-checks: fail loudly, write nothing ──────────────────────
            checks = [
                (
                    "partner 2 (Maa Travels) APPROVED/ACTIVE with CAB grant",
                    """SELECT 1 ok FROM partners p JOIN partner_services ps
                       ON ps.partner_id = p.id AND ps.service_type='CAB' AND ps.is_active
                       WHERE p.id=2 AND p.status IN ('APPROVED','ACTIVE')""",
                ),
                (
                    "partner 2 active SUV vehicles in Bhubaneswar (city 5, cat 3)",
                    """SELECT 1 ok FROM vehicles WHERE partner_id=2 AND city_id=5
                       AND vehicle_category_id=3 AND status='ACTIVE' AND deleted_at IS NULL""",
                ),
                (
                    "cab pricing exists for city 5 / category 3",
                    """SELECT 1 ok FROM vehicle_pricing_rules
                       WHERE city_id=5 AND vehicle_category_id=3""",
                ),
                (
                    "hotel 1 ACTIVE, partner 2, with active room category 1",
                    """SELECT 1 ok FROM hotels h JOIN hotel_room_categories rc
                       ON rc.hotel_id=h.id AND rc.id=1 AND rc.is_active
                       WHERE h.id=1 AND h.status='ACTIVE' AND h.partner_id=2
                         AND h.deleted_at IS NULL""",
                ),
                (
                    "tour package 1 ACTIVE, partner 2 with TOUR grant",
                    """SELECT 1 ok FROM tour_packages tp JOIN partner_services ps
                       ON ps.partner_id=tp.partner_id AND ps.service_type='TOUR' AND ps.is_active
                       WHERE tp.id=1 AND tp.status='ACTIVE' AND tp.partner_id=2""",
                ),
                (
                    "vehicle category 3 (SUV) active",
                    "SELECT 1 ok FROM vehicle_categories WHERE id=3 AND is_active",
                ),
            ]
            for label, sql in checks:
                if not await one(sql):
                    raise SystemExit(f"PRE-CHECK FAILED — missing: {label}")
            print(
                "pre-checks OK: partners, supply, pricing, rooms, packages all present"
            )

            # ── Coupons ─────────────────────────────────────────────────────
            coupon_ids: dict[str, int] = {}
            for cp in COUPONS:
                await c.execute(
                    text(
                        """
                        INSERT INTO coupons (coupon_code, title, description, apply_to,
                          discount_type, discount_value, max_discount_amount, min_booking_amount,
                          max_usage_per_customer, valid_from, valid_to, is_active,
                          coupon_type, platform_share_percent, created_by)
                        VALUES (:code, :title, :desc, :apply_to, :dtype, CAST(:dval AS NUMERIC),
                          CAST(:maxd AS NUMERIC), CAST(:minb AS NUMERIC),
                          1, :vfrom, :vto, TRUE, :ctype, :pct, 'seed_offer_demo')
                        ON CONFLICT (coupon_code) DO NOTHING
                        """
                    ),
                    {
                        "code": cp["code"],
                        "title": cp["title"],
                        "desc": cp["title"],
                        "apply_to": cp["apply_to"],
                        "dtype": cp["discount_type"],
                        "dval": cp["discount_value"],
                        "maxd": cp["max_discount"],
                        "minb": cp["min_booking"],
                        "vfrom": VALID_FROM,
                        "vto": VALID_TO,
                        "ctype": cp["type"],
                        "pct": cp["platform_share_percent"],
                    },
                )
                row = await one(
                    "SELECT id FROM coupons WHERE coupon_code=:code", code=cp["code"]
                )
                coupon_ids[cp["code"]] = row["id"]
                for svc, vc in cp["service_rules"]:
                    exists = await one(
                        """SELECT 1 ok FROM coupon_service_rules
                           WHERE coupon_id=:cid AND service_type=:svc
                             AND vehicle_category_id IS NOT DISTINCT FROM :vc""",
                        cid=coupon_ids[cp["code"]],
                        svc=svc,
                        vc=vc,
                    )
                    if not exists:
                        await c.execute(
                            text(
                                "INSERT INTO coupon_service_rules (coupon_id, service_type, vehicle_category_id) VALUES (:cid, :svc, :vc)"
                            ),
                            {"cid": coupon_ids[cp["code"]], "svc": svc, "vc": vc},
                        )
                for pid in cp["partner_ids"]:
                    await c.execute(
                        text(
                            """
                            INSERT INTO coupon_partner_rules (coupon_id, partner_id, joined_at, created_by, is_active)
                            VALUES (:cid, :pid, NOW(), 'seed_offer_demo', TRUE)
                            ON CONFLICT (coupon_id, partner_id) DO UPDATE SET is_active=TRUE, removed_at=NULL
                            """
                        ),
                        {"cid": coupon_ids[cp["code"]], "pid": pid},
                    )
            print("coupons ready:", {k: v for k, v in coupon_ids.items()})

            # ── Offers + links ──────────────────────────────────────────────
            for of in OFFERS:
                await c.execute(
                    text(
                        """
                        INSERT INTO offers (slug, coupon_id, service_type, vehicle_category_id,
                          title, short_description, terms_and_conditions, image_url,
                          valid_from, valid_to, is_published, sort_order, created_by)
                        VALUES (:slug, :cid, :svc, :vc, :title, :short, :terms, :image,
                          :vfrom, :vto, TRUE, :sort, 'seed_offer_demo')
                        ON CONFLICT (slug) DO UPDATE SET
                          coupon_id = EXCLUDED.coupon_id,
                          service_type = EXCLUDED.service_type,
                          vehicle_category_id = EXCLUDED.vehicle_category_id,
                          valid_from = EXCLUDED.valid_from,
                          valid_to = EXCLUDED.valid_to,
                          is_published = TRUE,
                          sort_order = EXCLUDED.sort_order,
                          updated_at = NOW()
                        """
                    ),
                    {
                        "slug": of["slug"],
                        "cid": coupon_ids[of["coupon"]],
                        "svc": of["service_type"],
                        "vc": of["vehicle_category_id"],
                        "title": of["title"],
                        "short": of["short"],
                        "terms": TERMS_GENERIC,
                        "image": of["image"],
                        "vfrom": VALID_FROM,
                        "vto": VALID_TO,
                        "sort": of["sort"],
                    },
                )
                row = await one(
                    "SELECT id FROM offers WHERE slug=:slug", slug=of["slug"]
                )
                oid = row["id"]
                # Repair images this seed wrote badly (non-Cloudinary or the
                # undefined named transformation) — admin-uploaded URLs survive.
                await c.execute(
                    text(
                        """
                        UPDATE offers SET image_url=:img, updated_at=NOW()
                        WHERE id=:oid
                          AND (image_url IS NULL
                               OR image_url NOT LIKE 'https://res.cloudinary.com/%'
                               OR image_url LIKE '%/q_auto/offers-circle/%')
                        """
                    ),
                    {"img": of["image"], "oid": oid},
                )
                for link in of.get("hotel_links", []):
                    await c.execute(
                        text(
                            """
                            INSERT INTO offer_hotel_links (offer_id, hotel_id, status)
                            VALUES (:oid, :hid, 'ACTIVE')
                            ON CONFLICT (offer_id, hotel_id) DO NOTHING
                            """
                        ),
                        {"oid": oid, "hid": link["hotel_id"]},
                    )
                    for rc in link["room_category_ids"]:
                        await c.execute(
                            text(
                                "INSERT INTO offer_hotel_room_types (offer_id, room_category_id) VALUES (:oid, :rc) ON CONFLICT DO NOTHING"
                            ),
                            {"oid": oid, "rc": rc},
                        )
                for tp in of.get("tour_ids", []):
                    await c.execute(
                        text(
                            """
                            INSERT INTO offer_tour_packages (offer_id, tour_package_id, status)
                            VALUES (:oid, :tp, 'ACTIVE')
                            ON CONFLICT (offer_id, tour_package_id) DO NOTHING
                            """
                        ),
                        {"oid": oid, "tp": tp},
                    )
                print(f"offer OK: {of['slug']} (id {oid}) -> coupon {of['coupon']}")

            # ── Refresh the stale smoke-test placeholder image ──────────────
            await c.execute(
                text(
                    "UPDATE offers SET image_url=:real, updated_at=NOW() WHERE slug='smoke-grand-imperial-deluxe' AND image_url=:ph"
                ),
                {"real": IMG_HOTEL, "ph": SMOKE_PLACEHOLDER},
            )

            live = (
                (
                    await c.execute(
                        text(
                            """
                        SELECT o.slug, o.title FROM offers o JOIN coupons cp ON cp.id=o.coupon_id
                        WHERE o.is_published AND o.valid_from <= CURRENT_DATE AND o.valid_to >= CURRENT_DATE
                          AND cp.is_active AND cp.valid_from <= CURRENT_DATE AND cp.valid_to >= CURRENT_DATE
                        ORDER BY o.sort_order, o.id
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
            print(f"\nLIVE OFFERS the hero will show ({len(live)}):")
            for row in live:
                print(f"  /offers/{row['slug']} — {row['title']}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
