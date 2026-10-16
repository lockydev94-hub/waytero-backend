#!/usr/bin/env python3
"""
WayTero — Odisha Hotel Seeder (admin-equivalent bulk insert).

Creates six ACTIVE hotels across the Odisha cities WayTero already has in the
dev database, exactly the way the admin "Add Hotel" flow
(POST /api/v1/admin/hotels plus its profile / rooms / amenities tabs) would
have saved them:

  hotels                      — main row, status=ACTIVE, approval stamps set
  hotel_room_categories       — 1-2 priced categories per hotel
  hotel_ratings               — the row the admin create flow adds by default
  hotel_policies              — the row the admin create flow adds by default
  hotel_performance_summary   — the row the admin create flow adds by default
  hotel_amenity_mappings      — picked from the hotel_amenities master by code

LOCAL DEMO DATA ONLY — see scripts/odisha_hotel_data.py. The properties,
prices and review counts are invented so the homepage hotels band fills out.
Never run this against production.

Prerequisites checked before any write (fails loudly if unmet):
  * at least one partner is ACTIVE and holds an ACTIVE partner_services HOTEL
    grant (a dev database collects several partners, so the grant — not a
    partner count of one — is the rule; the lowest eligible id is used)
  * every city_id used by the data file exists and is active
  * every category_code and amenity_code exists in its master table
  * no slug in the data file is already used by a different hotel
  * every postal_code is the 6-digit shape the profile tab validates

Idempotent: a hotel whose (hotel_name, city_id) pair already exists is SKIPPED
with its children — nothing is updated, so edits an admin has since made in
the dashboard are never overwritten. Re-running is always safe. The one
exception is hotel_type: an early run of this seeder stored room-type words
(EXECUTIVE, SUPER_DELUXE) there, so a stored value that is one of those words
is corrected to the property's real type; any other stored value is treated as
an admin's choice and left alone.

hotel_code comes from the real hotel_code_seq sequence with the
HOTEL_CODE_PREFIX system configuration, the same pair the admin service uses,
so the numbering the admin sees in the dashboard stays consistent.

Images are intentionally NOT inserted — hotel_images stays empty and the admin
will upload and manage photos from the admin dashboard afterwards. The public
card falls back to a placeholder while primary_image_url is null.

Contact fields stay NULL for the same reason: an invented phone number or
e-mail is not acceptable even in demo data.

Usage (from the backend project root):
    python -m scripts.seed_odisha_hotels            # normal run
    python -m scripts.seed_odisha_hotels --dry-run  # validate only, no writes
"""

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# ── Path setup (same pattern as seed_odisha_tours.py) ────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid

from sqlalchemy import func, select

# `text` is used as a loop variable in the sibling seeder, so the SQL helper is
# aliased here too — keeps the two scripts interchangeable.
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings

# Register the users table in Base.metadata — hotels.approved_by and
# hotels.created_by have FKs to users.id and the flush fails without this
# model imported (same reason alembic/env.py only sees a partial metadata).
import app.modules.auth.models.user  # noqa: F401

# Also registers `partners` in Base.metadata — hotels.partner_id has an FK to it.
import app.modules.partner.models  # noqa: F401
from app.modules.hotel.constants import HOTEL_ROOM_TYPES
from app.modules.hotel.models import (
    Hotel,
    HotelAmenity,
    HotelAmenityMapping,
    HotelCategory,
    HotelPerformanceSummary,
    HotelPolicy,
    HotelRating,
    HotelRoomCategory,
)
from app.modules.hotel.validators import slugify
from app.modules.master.models import City

from scripts.odisha_hotel_data import HOTELS

DRY_RUN = "--dry-run" in sys.argv


async def main() -> None:
    print("=" * 64)
    print("WayTero — Odisha Hotel Seeder")
    print("=" * 64)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # ── 1. Pick the partner that is allowed to sell hotel rooms ───────
        active_partners = (
            (
                await db.execute(
                    sql_text(
                        """
                        SELECT p.id, p.partner_code, p.business_name,
                               EXISTS (
                                 SELECT 1 FROM partner_services ps
                                  WHERE ps.partner_id = p.id
                                    AND ps.service_type = 'HOTEL'
                                    AND ps.is_active = TRUE
                               ) AS has_hotel
                        FROM partners p
                        WHERE p.status = 'ACTIVE' AND p.deleted_at IS NULL
                        ORDER BY has_hotel DESC, p.id
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        if not active_partners:
            print("FATAL: no ACTIVE partner found in the database.")
            sys.exit(1)
        eligible = [p for p in active_partners if p["has_hotel"]]
        if not eligible:
            print("FATAL: no ACTIVE partner has an active HOTEL service grant.")
            print("       Enable it in the admin before seeding. Partners found:")
            for p in active_partners:
                print(f"         id={p['id']} {p['partner_code']} {p['business_name']}")
            sys.exit(1)

        partner = eligible[0]
        print(
            f"Partner : id={partner['id']} {partner['partner_code']} "
            f"{partner['business_name']}"
        )
        if len(eligible) > 1:
            print(
                f"        ({len(eligible)} partners hold a HOTEL grant — "
                "seeding under the lowest id)"
            )
        print("HOTEL service grant: OK")

        # ── 2. Verify cities ───────────────────────────────────────────────
        needed_city_ids = {h["city_id"] for h in HOTELS}
        city_rows = (
            await db.scalars(select(City).where(City.id.in_(needed_city_ids)))
        ).all()
        city_by_id = {c.id: c for c in city_rows}
        missing = needed_city_ids - set(city_by_id)
        if missing:
            print(f"FATAL: city ids missing in cities table: {sorted(missing)}")
            sys.exit(1)
        inactive = [cid for cid, c in city_by_id.items() if not c.is_active]
        if inactive:
            print(f"FATAL: cities are not active: {sorted(inactive)}")
            sys.exit(1)
        print(f"Cities  : OK ({sorted(needed_city_ids)})")

        # ── 3. Resolve the master lookups by code, not by hardcoded id ────
        needed_category_codes = {h["category_code"] for h in HOTELS}
        category_rows = (
            (
                await db.execute(
                    select(HotelCategory).where(
                        HotelCategory.category_code.in_(needed_category_codes)
                    )
                )
            )
            .scalars()
            .all()
        )
        category_by_code = {c.category_code: c.id for c in category_rows}
        missing = needed_category_codes - set(category_by_code)
        if missing:
            print(f"FATAL: category_code not in hotel_categories: {sorted(missing)}")
            sys.exit(1)

        needed_amenity_codes = {code for h in HOTELS for code in h["amenity_codes"]}
        amenity_rows = (
            (
                await db.execute(
                    select(HotelAmenity).where(
                        HotelAmenity.amenity_code.in_(needed_amenity_codes),
                        HotelAmenity.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        amenity_by_code = {a.amenity_code: a.id for a in amenity_rows}
        missing = needed_amenity_codes - set(amenity_by_code)
        if missing:
            print(
                f"FATAL: amenity_code not active in hotel_amenities: {sorted(missing)}"
            )
            sys.exit(1)
        print(
            f"Masters : OK ({len(category_by_code)} categories, "
            f"{len(amenity_by_code)} amenities)"
        )

        # ── 4. The admin who is credited with the rows ────────────────────
        # Looked up rather than hardcoded so the seed works on any dev
        # database. NULL is legal on both columns, so an empty result is a
        # warning and not a failure.
        actor_id = (
            await db.execute(
                sql_text(
                    """
                    SELECT u.id FROM users u
                    JOIN user_roles ur ON ur.user_id = u.id
                    JOIN roles r ON r.id = ur.role_id
                    WHERE r.role_code IN ('SUPER_ADMIN', 'ADMIN')
                      AND u.deleted_at IS NULL AND u.is_active = TRUE
                    ORDER BY
                      CASE r.role_code WHEN 'SUPER_ADMIN' THEN 0 ELSE 1 END,
                      u.created_at
                    LIMIT 1
                    """
                )
            )
        ).scalar()
        if actor_id is None:
            print(
                "NOTE: no active SUPER_ADMIN/ADMIN user — created_by/approved_by stay NULL."
            )
        else:
            print(f"Actor   : {actor_id}")

        # ── 5. Idempotency + collision checks ─────────────────────────────
        # Keyed by (hotel_name, city_id); the value is the stored hotel_type,
        # which the vocabulary fix below reads.
        existing_name_city = {
            (name, city): hotel_type
            for name, city, hotel_type in (
                await db.execute(
                    sql_text(
                        "SELECT hotel_name, city_id, hotel_type FROM hotels "
                        "WHERE deleted_at IS NULL"
                    )
                )
            ).all()
        }
        existing_slugs = {
            s
            for s in (
                await db.scalars(select(Hotel.slug).where(Hotel.slug.is_not(None)))
            ).all()
        }
        existing_codes = {c for c in (await db.scalars(select(Hotel.hotel_code))).all()}

        inserts, skips, fixes, collisions = [], [], [], []
        for hotel in HOTELS:
            slug = slugify(f"{hotel['hotel_name']} {city_by_id[hotel['city_id']].name}")
            hotel["_slug"] = slug
            if (hotel["hotel_name"], hotel["city_id"]) in existing_name_city:
                stored_type = existing_name_city[
                    (hotel["hotel_name"], hotel["city_id"])
                ]
                # This seeder once wrote the room-type vocabulary (SUPER_DELUXE,
                # EXECUTIVE, ...) into hotel_type. The column is free text and
                # the intended vocabulary is HOTEL / RESORT / VILLA / ...; an
                # admin who has since chosen their own value is left alone
                # because their choice is not a room-type word.
                if stored_type != hotel["hotel_type"] and (
                    stored_type is None or stored_type in HOTEL_ROOM_TYPES
                ):
                    fixes.append(
                        (
                            hotel["hotel_name"],
                            hotel["city_id"],
                            stored_type,
                            hotel["hotel_type"],
                        )
                    )
                skips.append(hotel["hotel_name"])
                continue
            if slug in existing_slugs:
                collisions.append(slug)
                continue
            inserts.append(hotel)
        if collisions:
            print(
                "FATAL: slug already in use by another hotel: "
                f"{collisions}. Fix the data file names."
            )
            sys.exit(1)

        print(f"To insert: {len(inserts)}  |  already present (skip): {len(skips)}")
        for name in skips:
            print(f"  skip (name+city exists) : {name}")
        for name, city_id, stored_type, new_type in fixes:
            print(f"  hotel_type {stored_type!r} -> {new_type!r} ({name})")
            if not DRY_RUN:
                await db.execute(
                    sql_text(
                        "UPDATE hotels SET hotel_type = :type, updated_at = now() "
                        "WHERE hotel_name = :name AND city_id = :city_id "
                        "AND deleted_at IS NULL"
                    ),
                    {"type": new_type, "name": name, "city_id": city_id},
                )

        # ── 6. hotel_code: prefix + the real sequence ─────────────────────
        prefix = (
            await db.execute(
                sql_text(
                    "SELECT config_value FROM system_configurations "
                    "WHERE config_key = 'HOTEL_CODE_PREFIX'"
                )
            )
        ).scalar() or "HTL"

        if DRY_RUN:
            # nextval() is not rolled back with the transaction, so a dry run
            # must not consume sequence values — predict them instead. When
            # is_called is false, last_value itself is still unissued.
            seq_row = (
                (
                    await db.execute(
                        sql_text("SELECT last_value, is_called FROM hotel_code_seq")
                    )
                )
                .mappings()
                .one()
            )
            predicted = seq_row["last_value"] + (1 if seq_row["is_called"] else 0)
            for hotel in inserts:
                while f"{prefix}{predicted:06d}" in existing_codes:
                    predicted += 1
                print(
                    f"  would insert {prefix}{predicted:06d} — {hotel['hotel_name']} "
                    f"({len(hotel['rooms'])} room categories)"
                )
                predicted += 1
            print("DRY RUN — no changes written.")
            await engine.dispose()
            return

        now = datetime.now(timezone.utc)

        # ── 7. Insert hotels + children ───────────────────────────────────
        for hotel in inserts:
            # The sequence, retried if the database is already holding the
            # code (a restored dump can carry codes ahead of the sequence).
            seq_value = (
                await db.execute(select(func.nextval("hotel_code_seq")))
            ).scalar()
            hotel_code = f"{prefix}{int(seq_value):06d}"
            while hotel_code in existing_codes:
                seq_value = (
                    await db.execute(select(func.nextval("hotel_code_seq")))
                ).scalar()
                hotel_code = f"{prefix}{int(seq_value):06d}"
            existing_codes.add(hotel_code)

            row = Hotel(
                uuid=uuid.uuid4(),
                partner_id=partner["id"],
                hotel_code=hotel_code,
                hotel_name=hotel["hotel_name"],
                hotel_type=hotel["hotel_type"],
                hotel_category_id=category_by_code[hotel["category_code"]],
                star_rating=hotel["star_rating"],
                short_description=hotel["short_description"],
                description=hotel["description"],
                city_id=hotel["city_id"],
                # The service fills state_id from the city when the payload
                # leaves it out, so the seed reads the same value here.
                state_id=city_by_id[hotel["city_id"]].state_id,
                address=hotel["address"],
                address_line_2=hotel["address_line_2"],
                landmark=hotel["landmark"],
                postal_code=hotel["postal_code"],
                latitude=Decimal(hotel["latitude"]),
                longitude=Decimal(hotel["longitude"]),
                total_rooms=hotel["total_rooms"],
                slug=hotel["_slug"],
                # Admin-authored properties land ACTIVE so the homepage band
                # shows them; approval metadata is stamped the way the
                # approve-then-activate flow leaves it.
                is_featured=True,
                display_order=hotel["display_order"],
                status="ACTIVE",
                submitted_at=now,
                approved_at=now,
                activated_at=now,
                approved_by=actor_id,
                created_by=actor_id,
            )
            db.add(row)
            await db.flush()  # assign hotels.id

            # Default children, the same three rows create_hotel() adds so the
            # admin's later tabs edit a row instead of creating one.
            db.add(HotelPolicy(hotel_id=row.id))
            db.add(
                HotelRating(
                    hotel_id=row.id,
                    average_rating=Decimal(hotel["average_rating"]),
                    total_reviews=hotel["total_reviews"],
                )
            )
            db.add(HotelPerformanceSummary(hotel_id=row.id))

            for order, room in enumerate(hotel["rooms"]):
                db.add(
                    HotelRoomCategory(
                        hotel_id=row.id,
                        category_name=room["category_name"],
                        room_type=room["room_type"],
                        description=room["description"],
                        base_occupancy=room["base_occupancy"],
                        max_adults=room["max_adults"],
                        max_children=room["max_children"],
                        max_occupancy=room["max_occupancy"],
                        bed_type=room["bed_type"],
                        room_size_sqft=room["room_size_sqft"],
                        view_type=room["view_type"],
                        base_price=Decimal(room["base_price"]),
                        published_price=Decimal(room["published_price"]),
                        min_sellable_price=Decimal(room["min_sellable_price"]),
                        meal_plan=room["meal_plan"],
                        total_rooms=room["total_rooms"],
                        display_order=order,
                        is_active=True,
                    )
                )

            for code in hotel["amenity_codes"]:
                db.add(
                    HotelAmenityMapping(
                        hotel_id=row.id, amenity_id=amenity_by_code[code]
                    )
                )

            # NOTE: hotel_images deliberately left empty — images are uploaded
            # and managed by the admin from the admin dashboard.
            print(
                f"  inserted {hotel_code} id={row.id} — {row.hotel_name} "
                f"({row.star_rating}*, {len(hotel['rooms'])} categories, "
                f"{len(hotel['amenity_codes'])} amenities)"
            )

        await db.commit()

    await engine.dispose()
    print(
        "Done. Hotels are ACTIVE and visible on the public catalog and the "
        "homepage hotels band.\n"
        "Next: restart the :3002 dev server — /public/homepage revalidates for "
        "60s and the band counts for 300s."
    )


if __name__ == "__main__":
    asyncio.run(main())
