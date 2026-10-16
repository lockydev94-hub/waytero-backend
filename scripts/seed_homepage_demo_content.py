#!/usr/bin/env python3
"""
WayTero — homepage claim-band demo content (UPDATEs existing active variants).

Four homepage bands read their cards from `section_variants.content` JSON
instead of a table, and on this dev database three of them are empty (so the
band is hidden entirely) and the fourth holds one card with a broken link:

  SERVICES      content.items        — 1 card, link /cab, which 404s
  STATS         content.stats        — []    → band hidden
  TESTIMONIALS  content.testimonials — null  → band hidden
  PARTNERS      content.partners     — null  → band hidden

It also strips three legacy content keys that no longer render:

  FEATURED_HOTELS       content.hotels        — the band reads the hotels table
  POPULAR_DESTINATIONS  content.destinations  — the band reads cities + packages
  OFFERS                content.offers        — the band reads the offers table

Those keys are not merely dead weight: CmsService.validate_content rejects
them on write, so while they sit in the row the admin cannot save those
sections from the CMS at all. Removing them is what makes "I will edit it in
the dashboard" work.

Only the ACTIVE variant of each section is touched, and it is UPDATEd — never
inserted — because uniq_variants_active_per_section is a partial unique index,
so a second is_active=true variant for one section is a hard database error.

Every content dict is passed through CmsService.validate_content() before it
is written, so what lands in the row is guaranteed to be re-savable from the
CMS editor.

LOCAL DEMO DATA — two different rules apply, and the split is deliberate:

  * STATS and PARTNERS are DERIVED from this database (live counts, real
    partner and hotel names). Nothing is invented, so the homepage publishes
    only numbers the platform actually has.
  * TESTIMONIALS is written copy. The database does hold guest reviews, but
    they are unmoderated PENDING rows — including a 2-star complaint — so they
    are not publishable, and inventing praise is what the renderer comment
    rules out. These four entries exist so the layout can be judged. Clear
    them again with:
        UPDATE section_variants SET content = content - 'testimonials'
        WHERE id = <the active TESTIMONIALS variant id>;

Never run this against production.

Idempotent: STATS / TESTIMONIALS / PARTNERS are filled only when the key is
missing or the array is empty, so a re-run never overwrites copy the owner has
since written in the CMS. SERVICES repairs the broken links in place and
appends only the service types that still have no card. The legacy-key strip
is unconditional, because an empty key set is what the CMS wants.

Usage (from the backend project root):
    python -m scripts.seed_homepage_demo_content               # apply
    python -m scripts.seed_homepage_demo_content --dry-run     # show only
    python -m scripts.seed_homepage_demo_content --retire-test-rows
        additionally soft-deletes the ACTIVE hotels sitting on test master
        data (a name starting "Test", or a city/state named Test*), so the
        hotels band does not show a test artifact. Reversible: the script
        prints the exact UPDATE that restores them.
"""

import asyncio
import sys
from pathlib import Path

# ── Path setup (same pattern as seed_odisha_tours.py) ────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings

# Register the users table in Base.metadata — section_variants.created_by_user_id
# has an FK to users.id, so the flush fails without this model imported (same
# reason seed_odisha_tours.py imports it).
import app.modules.auth.models.user  # noqa: F401

# `app.modules.master.models` also registers cities/states in Base.metadata,
# which the raw count queries below join against.
from app.modules.admin.models import PageSection, SectionVariant
from app.modules.admin.services import SERVICE_TYPE_ROUTES, CmsService
from app.modules.master.models import ServiceType

DRY_RUN = "--dry-run" in sys.argv
RETIRE_TEST_ROWS = "--retire-test-rows" in sys.argv

# The CMS picker builds SERVICES cards from the service_types master and links
# them through SERVICE_TYPE_ROUTES, so the seed writes what the dashboard
# would have written. The renderer reads `icon` from its own four-name map and
# `tone` from its own four-colour list, so both are constrained here.
SERVICE_ICONS = {"CAB": "Car", "HOTEL": "Hotel", "TOUR": "Map"}
SERVICE_TONES = {"CAB": "primary", "HOTEL": "accent", "TOUR": "success"}
# Reverse of the picker's route map, to tell which service type a card
# already covers.
_LINK_TO_CODE = {url: code for code, url in SERVICE_TYPE_ROUTES.items()}

# content keys that stopped rendering when their band moved to live tables.
# validate_content rejects them, so they also block CMS saves.
LEGACY_KEYS = {
    "FEATURED_HOTELS": "hotels",
    "POPULAR_DESTINATIONS": "destinations",
    "OFFERS": "offers",
}

# Written copy — see the LOCAL DEMO note in the docstring. `name` is the React
# key in the renderer, so the four names are distinct; `avatar` is left out and
# the card falls back to initials.
DEMO_TESTIMONIALS = [
    {
        "name": "Ananya Sahu",
        "role": "Booked a cab",
        "city": "Bhubaneswar",
        "rating": 5,
        "text": (
            "Booked an airport pickup at eleven at night and the driver was "
            "already at the gate when I landed. The fare on the screen is the "
            "fare I paid, with toll and parking shown separately."
        ),
    },
    {
        "name": "Deepak Mohanty",
        "role": "Booked a hotel",
        "city": "Cuttack",
        "rating": 4,
        "text": (
            "The room was what the description said it would be, which is more "
            "than I expected after booking elsewhere. Check-in took a couple of "
            "minutes and the bill matched the platform quote."
        ),
    },
    {
        "name": "Lipika Panda",
        "role": "Booked a tour package",
        "city": "Rourkela",
        "rating": 5,
        "text": (
            "Took the Chilika weekend package with my parents. The vehicle was "
            "clean, the hotel was on the route rather than a detour, and the "
            "local team called the day before to confirm the schedule."
        ),
    },
    {
        "name": "Sanjay Barik",
        "role": "Booked a hotel",
        "city": "Berhampur",
        "rating": 4,
        "text": (
            "Value stay near the market end of town. Wallet balance paid for it "
            "in one tap, and the refund on my earlier cancellation had already "
            "landed by the time I rebooked."
        ),
    },
]


async def _active_variants(db) -> dict:
    """The one active variant per section, keyed by section_key."""
    rows = (
        await db.execute(
            select(SectionVariant, PageSection.section_key)
            .join(PageSection, PageSection.id == SectionVariant.page_section_id)
            .where(SectionVariant.is_active.is_(True))
        )
    ).all()
    return {section_key: variant for variant, section_key in rows}


def _repair_services(items: list, service_types: list) -> tuple[list, int, int]:
    """Fix the links the picker used to generate, then add the missing cards.

    Existing copy is kept: only the `link` is corrected and the icon/tone the
    renderer needs are filled in, so a title or description the owner wrote in
    the CMS survives. Returns (items, repaired, appended).
    """
    repaired = 0
    # The picker used to emit f"/{type_code.lower()}" — /cab, /hotel, /tour.
    # Those URLs do not exist; the listing routes are plural.
    broken = {
        f"/{st.type_code.lower()}": _expected_link(st)
        for st in service_types
        if _expected_link(st) != f"/{st.type_code.lower()}"
    }
    for item in items:
        link = item.get("link")
        if link in broken:
            item["link"] = broken[link]
            repaired += 1
        code = _code_for_link(item.get("link"))
        if code and not item.get("icon"):
            item["icon"] = SERVICE_ICONS[code]
        if code and not item.get("tone"):
            item["tone"] = SERVICE_TONES[code]

    present = {_code_for_link(item.get("link")) for item in items}
    appended = 0
    for st in service_types:
        if st.type_code in present:
            continue
        items.append(
            {
                "title": st.label,
                "desc": st.description or "",
                "link": _expected_link(st),
                "icon": SERVICE_ICONS.get(st.type_code, "MapPin"),
                "tone": SERVICE_TONES.get(st.type_code, "primary"),
            }
        )
        appended += 1
    return items, repaired, appended


def _expected_link(service_type) -> str:
    return SERVICE_TYPE_ROUTES.get(
        service_type.type_code, f"/{service_type.type_code.lower()}"
    )


def _code_for_link(link) -> str | None:
    return _LINK_TO_CODE.get(link)


async def main() -> None:
    print("=" * 64)
    print("WayTero — homepage demo content")
    print("=" * 64)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        variants = await _active_variants(db)
        print(f"Active variants : {len(variants)} sections")

        changes: list[str] = []

        # ── 1. SERVICES — repair the links, then fill the missing cards ───
        service_types = (
            await db.scalars(
                select(ServiceType)
                .where(ServiceType.is_active.is_(True))
                .order_by(ServiceType.display_order, ServiceType.type_code)
            )
        ).all()
        variant = variants.get("SERVICES")
        if variant is None:
            print("NOTE: no active SERVICES variant — band skipped.")
        else:
            content = dict(variant.content or {})
            items, repaired, appended = _repair_services(
                list(content.get("items") or []), service_types
            )
            if repaired or appended:
                content["items"] = items
                CmsService.validate_content("SERVICES", content)
                variant.content = content
                changes.append(
                    f"SERVICES variant {variant.id}: {repaired} link(s) fixed, "
                    f"{appended} card(s) added → {len(items)} items "
                    f"({[i.get('link') for i in items]})"
                )
            else:
                changes.append(
                    f"SERVICES variant {variant.id}: already correct "
                    f"({len(items)} items) — no change"
                )

        # ── 2. STATS — live counts, nothing invented ──────────────────────
        counts = (
            (
                await db.execute(
                    sql_text(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM cities
                            WHERE is_active = TRUE)                          AS cities,
                          (SELECT COUNT(*) FROM hotels
                            WHERE status = 'ACTIVE' AND deleted_at IS NULL)  AS hotels,
                          (SELECT COUNT(*) FROM tour_packages
                            WHERE status = 'ACTIVE')                        AS packages,
                          (SELECT COUNT(*) FROM cab_routes)                 AS routes
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
        stats = [
            {"value": int(counts["cities"]), "label": "Cities listed"},
            {"value": int(counts["hotels"]), "label": "Hotels live"},
            {"value": int(counts["packages"]), "label": "Tour packages"},
            {"value": int(counts["routes"]), "label": "Cab routes"},
        ]
        variant = variants.get("STATS")
        if variant is None:
            print("NOTE: no active STATS variant — band skipped.")
        else:
            content = dict(variant.content or {})
            current = content.get("stats") or []
            if current:
                changes.append(
                    f"STATS variant {variant.id}: {len(current)} stat(s) "
                    "already present — no change"
                )
            else:
                content["stats"] = stats
                CmsService.validate_content("STATS", content)
                variant.content = content
                changes.append(
                    f"STATS variant {variant.id}: filled {len(stats)} from live "
                    "counts → " + ", ".join(f"{s['value']} {s['label']}" for s in stats)
                )

        # ── 3. TESTIMONIALS — written demo copy, local only ───────────────
        variant = variants.get("TESTIMONIALS")
        if variant is None:
            print("NOTE: no active TESTIMONIALS variant — band skipped.")
        else:
            content = dict(variant.content or {})
            current = content.get("testimonials") or []
            if current:
                changes.append(
                    f"TESTIMONIALS variant {variant.id}: {len(current)} entr"
                    f"{'y' if len(current) == 1 else 'ies'} already present "
                    "— no change"
                )
            else:
                content["testimonials"] = DEMO_TESTIMONIALS
                CmsService.validate_content("TESTIMONIALS", content)
                variant.content = content
                changes.append(
                    f"TESTIMONIALS variant {variant.id}: filled "
                    f"{len(DEMO_TESTIMONIALS)} written entries (demo copy)"
                )

        # ── 4. PARTNERS — the real supply roster in this database ─────────
        partner_names = (
            (
                await db.execute(
                    sql_text(
                        """
                        SELECT business_name FROM partners
                         WHERE status = 'ACTIVE' AND deleted_at IS NULL
                           AND business_name IS NOT NULL
                        UNION
                        SELECT h.hotel_name FROM hotels h
                          JOIN cities c ON c.id = h.city_id
                         WHERE h.status = 'ACTIVE' AND h.deleted_at IS NULL
                           AND h.hotel_name IS NOT NULL
                        ORDER BY 1
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        # Test master data must not appear on a "trusted partners" strip.
        partners = sorted(
            {
                name.strip()
                for name in partner_names
                if name and not name.strip().lower().startswith("test")
            }
        )
        variant = variants.get("PARTNERS")
        if variant is None:
            print("NOTE: no active PARTNERS variant — band skipped.")
        elif not partners:
            print("NOTE: no ACTIVE partner or hotel names — PARTNERS skipped.")
        else:
            content = dict(variant.content or {})
            current = content.get("partners") or []
            if current:
                changes.append(
                    f"PARTNERS variant {variant.id}: {len(current)} name(s) "
                    "already present — no change"
                )
            else:
                content["partners"] = partners
                CmsService.validate_content("PARTNERS", content)
                variant.content = content
                changes.append(
                    f"PARTNERS variant {variant.id}: filled {len(partners)} "
                    f"live names → {partners}"
                )

        # ── 5. Strip the legacy keys that block CMS saves ─────────────────
        for section_key, legacy_key in LEGACY_KEYS.items():
            variant = variants.get(section_key)
            if variant is None:
                continue
            content = variant.content or {}
            if legacy_key not in content:
                continue
            stripped = {k: v for k, v in content.items() if k != legacy_key}
            CmsService.validate_content(section_key, stripped)
            variant.content = stripped
            changes.append(
                f"{section_key} variant {variant.id}: dropped content."
                f"{legacy_key} ({len(content.get(legacy_key) or [])} entries) "
                "— the band reads live data, and the key blocked CMS saves"
            )

        # ── 6. Optional: retire the test hotel rows ──────────────────────
        if RETIRE_TEST_ROWS:
            test_hotels = (
                (
                    await db.execute(
                        sql_text(
                            """
                            SELECT h.id, h.hotel_name FROM hotels h
                              JOIN cities c ON c.id = h.city_id
                              LEFT JOIN states s ON s.id = h.state_id
                             WHERE h.deleted_at IS NULL AND h.status = 'ACTIVE'
                               AND (h.hotel_name ILIKE 'Test%'
                                    OR c.name ILIKE 'Test%'
                                    OR s.name ILIKE 'Test%')
                             ORDER BY h.id
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
            if test_hotels:
                ids = [row["id"] for row in test_hotels]
                changes.append(
                    "Retire test hotels "
                    f"{ids} ({', '.join(row['hotel_name'] for row in test_hotels)})"
                    f" — revert with: UPDATE hotels SET deleted_at = NULL "
                    f"WHERE id IN ({', '.join(str(i) for i in ids)});"
                )
                if not DRY_RUN:
                    await db.execute(
                        sql_text(
                            "UPDATE hotels SET deleted_at = now() "
                            "WHERE id = ANY(:ids)"
                        ),
                        {"ids": ids},
                    )

        # ── 7. Report ────────────────────────────────────────────────────
        print()
        for line in changes:
            print(f"  {'would: ' if DRY_RUN else ''}{line}")
        if not changes:
            print("  nothing to do — every band is already filled.")

        if DRY_RUN:
            await db.rollback()
            print("\nDRY RUN — no changes written.")
        else:
            await db.commit()
            print("\nDone.")

    await engine.dispose()
    print(
        "Next: restart the :3002 dev server — /public/homepage revalidates for "
        "60s and the band counts for 300s."
    )


if __name__ == "__main__":
    asyncio.run(main())
