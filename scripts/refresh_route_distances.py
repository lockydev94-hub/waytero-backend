# ============================================================
# WAYTERO — REFRESH CAB ROUTE DISTANCES
# File: scripts/refresh_route_distances.py
# Run: python -m scripts.refresh_route_distances  (from Backend/)
#
# Re-measures every active corridor with the Google Distance Matrix API and
# writes the result back (distance_km + distance_source). The seeded routes
# were MANUAL numbers typed into a migration; a route page that quotes a fare
# off an unverified distance is quoting the wrong journey.
#
# A manual distance is replaced — that is the point of running this — but a
# route whose distance cannot be measured is left exactly as it is, so a
# missing key or a bad place name never zeroes a published route.
# ============================================================

import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.modules.admin.cab_route_distance import resolve_route_distance


async def refresh() -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    changed = 0
    failed = 0
    async with session() as db:
        routes = (
            (
                await db.execute(
                    text(
                        """
                        SELECT id, slug, origin_label, destination_label,
                               distance_km, distance_source
                        FROM cab_routes
                        WHERE is_active
                        ORDER BY id
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        for route in routes:
            try:
                km, source = await resolve_route_distance(
                    db,
                    route["origin_label"],
                    route["destination_label"],
                    origin_hint=None,
                    destination_hint=None,
                )
            except Exception as exc:  # noqa: BLE001 — report, never crash the sweep
                failed += 1
                print(f"  {route['slug']}: LEFT AS IS ({exc})")
                continue
            old = Decimal(str(route["distance_km"]))
            if source == "HAVERSINE":
                # An estimate is not an upgrade over a number someone checked.
                print(f"  {route['slug']}: kept {old} km (Google unavailable)")
                continue
            await db.execute(
                text(
                    """
                    UPDATE cab_routes
                       SET distance_km = :km, distance_source = :src,
                           updated_at = NOW()
                     WHERE id = :id
                    """
                ),
                {"km": km, "src": source, "id": route["id"]},
            )
            changed += 1
            print(
                f"  {route['slug']}: {old} km {route['distance_source']} -> "
                f"{km} km {source}"
            )
        await db.commit()
    await engine.dispose()
    print(f"Done — {changed} updated, {failed} could not be measured.")


if __name__ == "__main__":
    asyncio.run(refresh())
