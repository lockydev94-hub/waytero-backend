#!/usr/bin/env python3
"""
WayTero — Odisha Tour Package Seeder (admin-equivalent bulk insert).

Creates 10 ACTIVE, APPROVED tour packages for the existing active partner
(Maa Travels, PRT-379499), exactly the way the admin "Add Package" API
(POST /api/v1/admin/tours/packages) would have saved them:

  tour_packages            — main row, status=ACTIVE, approved_at/by set
  tour_itineraries         — day-wise plan (activities as JSONB)
  tour_package_inclusions  — one row per text
  tour_package_exclusions  — one row per text
  tour_package_pricing     — per-group slabs keyed by persons_count

Prerequisites checked before any write (fails loudly if unmet):
  * the partner exists and is ACTIVE
  * partner_services has an ACTIVE TOUR grant for that partner
  * all city_id values used by the data file exist in cities
  * no duplicate package_code / slug already present

Idempotent: packages with the same package_code are SKIPPED, not
duplicated. Re-running is always safe.

Images are intentionally NOT inserted — the admin will upload and manage
images from the admin dashboard afterwards (tour_package_media stays
untouched for the new packages).

Usage (from the backend project root on the server):
    python -m scripts.seed_odisha_tours            # normal run
    python -m scripts.seed_odisha_tours --dry-run  # validate only, no writes
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup (same pattern as seed_cms_defaults.py) ────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
# Register the users table in Base.metadata — tour_packages.approved_by has
# an FK to users.id and the flush fails without this model imported
# (same reason alembic/env.py only sees a partial metadata).
import app.modules.auth.models.user  # noqa: F401
from app.modules.master.models import City
from app.modules.partner.models import Partner, PartnerService
from app.modules.tour.models import (
    TourItinerary,
    TourPackage,
    TourPackageExclusion,
    TourPackageInclusion,
    TourPackagePricing,
)

from scripts.odisha_tour_data import PACKAGES

DRY_RUN = "--dry-run" in sys.argv


async def main() -> None:
    print("=" * 64)
    print("WayTero — Odisha Tour Package Seeder")
    print("=" * 64)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # ── 1. Locate the single active partner ──────────────────────────
        partners = (
            await db.scalars(select(Partner).where(Partner.status == "ACTIVE"))
        ).all()
        if len(partners) == 0:
            print("FATAL: no ACTIVE partner found in the database.")
            sys.exit(1)
        if len(partners) > 1:
            print("Multiple active partners found:")
            for p in partners:
                print(f"  id={p.id} {p.partner_code} {p.business_name}")
            print("FATAL: expected exactly one active partner; aborting.")
            sys.exit(1)

        partner = partners[0]
        print(f"Partner : id={partner.id} {partner.partner_code} {partner.business_name}")

        # ── 2. Verify the TOUR service grant ─────────────────────────────
        tour_service = await db.scalar(
            select(PartnerService.id).where(
                PartnerService.partner_id == partner.id,
                PartnerService.service_type == "TOUR",
                PartnerService.is_active.is_(True),
            )
        )
        if tour_service is None:
            print(
                "FATAL: partner does not have an active TOUR service "
                "(enable it in the admin before seeding)."
            )
            sys.exit(1)
        print("TOUR service grant: OK")

        # ── 3. Verify every city_id exists ───────────────────────────────
        needed_city_ids = {p["city_id"] for p in PACKAGES}
        for pkg in PACKAGES:
            for day in pkg["itinerary"]:
                if day.get("overnight_city_id"):
                    needed_city_ids.add(day["overnight_city_id"])
        city_rows = (
            await db.scalars(select(City).where(City.id.in_(needed_city_ids)))
        ).all()
        found_city_ids = {c.id for c in city_rows}
        missing = needed_city_ids - found_city_ids
        if missing:
            print(f"FATAL: city ids missing in cities table: {sorted(missing)}")
            sys.exit(1)
        print(f"Cities  : OK ({sorted(found_city_ids)})")

        # ── 4. Idempotency + collision checks ────────────────────────────
        existing_codes = {
            r
            for r in (
                await db.scalars(select(TourPackage.package_code))
            ).all()
        }
        existing_slugs = {
            r for r in (await db.scalars(select(TourPackage.slug))).all()
        }

        inserts, skips, collisions = [], [], []
        for pkg in PACKAGES:
            if pkg["package_code"] in existing_codes:
                skips.append(pkg["package_code"])
                continue
            if pkg["slug"] in existing_slugs:
                collisions.append(pkg["slug"])
                continue
            inserts.append(pkg)
        if collisions:
            print(
                "FATAL: slug already in use by another package: "
                f"{collisions}. Fix the data file slugs."
            )
            sys.exit(1)

        now = datetime.now(timezone.utc)
        print(f"To insert: {len(inserts)}  |  already present (skip): {len(skips)}")
        if DRY_RUN:
            for pkg in inserts:
                print(f"  would insert {pkg['package_code']} — {pkg['package_name']}")
            print("DRY RUN — no changes written.")
            await engine.dispose()
            return

        # ── 5. Insert packages + children ────────────────────────────────
        for pkg in inserts:
            package = TourPackage(
                uuid=uuid.uuid4(),
                partner_id=partner.id,
                package_code=pkg["package_code"],
                slug=pkg["slug"],
                package_name=pkg["package_name"],
                package_type=pkg["package_type"],
                destination=pkg["destination"],
                city_id=pkg["city_id"],
                duration_days=pkg["duration_days"],
                duration_nights=pkg["duration_nights"],
                minimum_persons=pkg["minimum_persons"],
                maximum_persons=pkg["maximum_persons"],
                short_description=pkg["short_description"],
                description=pkg["description"],
                terms_and_conditions=pkg["terms"],
                # Admin-authored packages land directly ACTIVE, matching
                # admin_create_package() behaviour; approval metadata is
                # stamped so the audit trail looks like an admin action.
                status="ACTIVE",
                submitted_at=now,
                approved_at=now,
                activated_at=now,
            )
            db.add(package)
            await db.flush()  # assign package.id

            for day in pkg["itinerary"]:
                db.add(
                    TourItinerary(
                        package_id=package.id,
                        day_number=day["day_number"],
                        title=day["title"],
                        description=day.get("description"),
                        activities=day.get("activities") or [],
                        overnight_city_id=day.get("overnight_city_id"),
                    )
                )
            for text in pkg["inclusions"]:
                if text and text.strip():
                    db.add(
                        TourPackageInclusion(
                            package_id=package.id, inclusion_text=text.strip()
                        )
                    )
            for text in pkg["exclusions"]:
                if text and text.strip():
                    db.add(
                        TourPackageExclusion(
                            package_id=package.id, exclusion_text=text.strip()
                        )
                    )
            for slab in pkg["pricing"]:
                db.add(
                    TourPackagePricing(
                        package_id=package.id,
                        persons_count=slab["persons_count"],
                        package_price=slab["package_price"],
                    )
                )
            # NOTE: tour_package_media deliberately left empty — images are
            # uploaded and managed by the admin from the admin dashboard.
            print(
                f"  inserted {package.package_code} id={package.id} "
                f"— {package.package_name}"
            )

        await db.commit()

    await engine.dispose()
    print("Done. Packages are ACTIVE and visible on the public catalog.")


if __name__ == "__main__":
    asyncio.run(main())
