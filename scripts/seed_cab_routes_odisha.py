# ============================================================
# WAYTERO — SEED POPULAR CAB ROUTES (ODISHA / BHUBANESWAR)
# File: scripts/seed_cab_routes_odisha.py
# Run: python -m scripts.seed_cab_routes_odisha   (from Backend/ dir)
#
# Gives the Popular Routes section and the /cabs/routes hub real corridors to
# show on day one. Distances are resolved through Google's Distance Matrix (or
# its straight-line fallback) — never typed by hand, because a wrong distance
# quotes a wrong fare.
#
# Safe to re-run: an existing slug is left alone.
# ============================================================

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.modules.admin import cab_route_distance

PRICING_CITY = "Bhubaneswar"

# `note` is the one place a route may state a fact about the places themselves;
# anything about price, duration or road conditions belongs to the fare engine.
ROUTES = [
    {
        "origin": "Bhubaneswar",
        "destination": "Puri",
        "origin_city": "Bhubaneswar",
        "destination_city": "Puri",
        "note": "The coast road to the Jagannath temple town.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Konark",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "Sun Temple side, via the Puri road.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Cuttack",
        "origin_city": "Bhubaneswar",
        "destination_city": "Cuttack",
        "note": "The twin city, across the Mahanadi.",
    },
    {
        "origin": "Cuttack",
        "destination": "Puri",
        "origin_city": "Cuttack",
        "destination_city": "Puri",
        "note": "Cuttack to the coast without going back through Bhubaneswar.",
    },
    {
        "origin": "Puri",
        "destination": "Konark",
        "origin_city": "Puri",
        "destination_city": None,
        "note": "The short temple-to-temple hop on the coast road.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Chandipur",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "Balarama-side beach, where the tide retreats far out.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Dhenkanal",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "North-west on NH55 towards Angul.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Angul",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "The industrial town on the Hirakud side.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Kendrapara",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "North-east towards the Baitarani delta.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Jajpur",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "The old town on the main road north.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Balasore",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "North on NH16 towards West Bengal.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Rourkela",
        "origin_city": "Bhubaneswar",
        "destination_city": "Rourkela",
        "note": "The steel city in the north-west.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Berhampur",
        "origin_city": "Bhubaneswar",
        "destination_city": "Berhampur",
        "note": "South on NH16, the silk and fishing town.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Gopalpur",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "Beach town past Berhampur, near the fishing harbour.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Koraput",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "note": "Eastern Ghats, on the way to the Indravati hills.",
    },
    {
        "origin": "Bhubaneswar",
        "destination": "Visakhapatnam",
        "origin_city": "Bhubaneswar",
        "destination_city": None,
        "state": "Andhra Pradesh",
        "note": "Coastal drive south into Andhra Pradesh.",
    },
]

TERMS = """This fare is calculated live from our published cab fare rules for the distance shown on this page, so it matches what the booking flow charges.

Where the fare rules set a minimum distance for the vehicle, the trip is billed at that minimum even if the road distance is shorter. The vehicle card states the billed distance when that happens.

A night surcharge is added automatically when your pickup falls inside the night window defined by the fare rules.

The estimate covers a one-way drop between the two places named on this page. Waiting time, tolls, parking and permit charges are not included in it.

Prices change when the fare rules are updated; a quote you save while booking is held for that booking."""


async def seed() -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session() as db:
        pricing_city_id = (
            await db.execute(
                text(
                    "SELECT id FROM cities WHERE name = :n AND COALESCE(is_active, TRUE)"
                ),
                {"n": PRICING_CITY},
            )
        ).scalar()
        if pricing_city_id is None:
            print(
                f"[seed_cab_routes] No active city named {PRICING_CITY}; nothing to price against."
            )
            await engine.dispose()
            return

        city_ids = dict(
            (
                await db.execute(
                    text("SELECT name, id FROM cities WHERE COALESCE(is_active, TRUE)")
                )
            )
            .mappings()
            .all()
        )
        vehicles = list(
            (
                await db.execute(
                    text(
                        """
                        SELECT id FROM vehicle_categories
                        WHERE is_active ORDER BY display_order, id
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        if not vehicles:
            print(
                "[seed_cab_routes] No active vehicle categories — a route needs at least one."
            )
            await engine.dispose()
            return

        created = skipped = failed = 0
        for order, r in enumerate(ROUTES, start=1):
            origin, destination = r["origin"], r["destination"]
            slug = f"{origin.lower()}-to-{destination.lower()}"
            exists = (
                await db.execute(
                    text("SELECT 1 FROM cab_routes WHERE slug = :s"), {"s": slug}
                )
            ).first()
            if exists:
                skipped += 1
                continue

            origin_city_id = city_ids.get(r.get("origin_city") or "")
            destination_city_id = city_ids.get(r.get("destination_city") or "")
            hint = r.get("state", "Odisha")
            try:
                distance_km, source = await cab_route_distance.resolve_route_distance(
                    db,
                    origin,
                    destination,
                    origin_hint=hint,
                    destination_hint=hint,
                )
            except Exception as exc:
                print(
                    f"[seed_cab_routes] {slug}: distance unresolved ({exc}) — skipped"
                )
                failed += 1
                continue

            description = f"One-way cab from {origin} to {destination}. {r.get('note', '')}".strip()
            await db.execute(
                text(
                    """
                    INSERT INTO cab_routes (slug, origin_label, destination_label,
                        origin_city_id, destination_city_id, pricing_city_id,
                        distance_km, distance_source, description, terms_and_conditions,
                        is_active, sort_order, created_by)
                    VALUES (:slug, :origin, :destination, :origin_city, :destination_city,
                        :pricing_city, :distance_km, :source, :description, :terms,
                        TRUE, :sort_order, 'seed_cab_routes_odisha')
                    ON CONFLICT (slug) DO NOTHING
                    """
                ),
                {
                    "slug": slug,
                    "origin": origin,
                    "destination": destination,
                    "origin_city": origin_city_id,
                    "destination_city": destination_city_id,
                    "pricing_city": pricing_city_id,
                    "distance_km": distance_km,
                    "source": source,
                    "description": description,
                    "terms": TERMS,
                    "sort_order": order,
                },
            )
            route_id = (
                await db.execute(
                    text("SELECT id FROM cab_routes WHERE slug = :s"), {"s": slug}
                )
            ).scalar_one()
            for vc in vehicles:
                await db.execute(
                    text(
                        """
                        INSERT INTO cab_route_vehicle_categories (route_id, vehicle_category_id)
                        VALUES (:r, :v) ON CONFLICT DO NOTHING
                        """
                    ),
                    {"r": route_id, "v": vc},
                )
            print(f"[seed_cab_routes] {slug}: {distance_km} km ({source})")
            created += 1

        await db.commit()
        print(
            f"[seed_cab_routes] created={created} already_present={skipped} "
            f"unresolved={failed}"
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
