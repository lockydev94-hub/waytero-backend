# ============================================================
# WAYTERO — PUBLIC SERVICE AVAILABILITY API
# File: app/modules/admin/public_availability.py
# Doc Ref: Hard Service Availability (plan: rosy-vent-shrew)
# Prefix: /public  (registered in api/router.py)
#
#   GET /public/availability — per service: is it switched on AND is there
#   real supply, plus the city ids where that supply lives.
#
# The website hides a vertical ENTIRELY (nav, footer, sections, pages→404,
# sitemap, llms.txt) when active is false or supply_count is 0, and uses
# city_ids to answer "we don't serve that area" honestly instead of showing
# an empty result list. One endpoint rather than composing the three search
# feeds: a page needs one consistent snapshot, and three partial-failure
# modes across nav + sitemap + homepage is how half-hidden services appear.
#
# Supply means BOOKABLE supply, counted from the same tables the booking
# flows read — never the marketing copy.
# ============================================================

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

router = APIRouter()

_CACHE_CONTROL = "public, max-age=60, s-maxage=300, stale-while-revalidate=600"


@router.get(
    "/availability",
    response_model=Dict[str, Any],
    summary="Service switch + real supply counts per vertical (no auth)",
)
async def get_availability(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    switches = (
        (
            await db.execute(
                text(
                    "SELECT type_code, is_active FROM service_types "
                    "WHERE type_code IN ('CAB', 'HOTEL', 'TOUR')"
                )
            )
        )
        .mappings()
        .all()
    )
    active: Dict[str, bool] = {
        "CAB": False,
        "HOTEL": False,
        "TOUR": False,
    }
    for row in switches:
        active[row["type_code"]] = bool(row["is_active"])

    # A cab city is in supply only where a fare can actually be quoted —
    # an active vehicle category with a city rule or a platform-default rule
    # (same signal /public/cab/cities publishes as has_pricing).
    cab_cities: List[int] = [
        int(r["id"])
        for r in (
            (
                await db.execute(
                    text(
                        """
                SELECT c.id
                  FROM cities c
                 WHERE c.is_active = TRUE
                   AND EXISTS (
                        SELECT 1
                          FROM vehicle_categories vc
                         WHERE vc.is_active = TRUE
                           AND (
                             EXISTS (SELECT 1 FROM vehicle_pricing_rules r
                                      WHERE r.city_id = c.id
                                        AND r.vehicle_category_id = vc.id)
                             OR EXISTS (SELECT 1 FROM default_vehicle_pricing_rules d
                                         WHERE d.vehicle_category_id = vc.id)
                           )
                   )
                 ORDER BY c.id
                """
                    )
                )
            )
            .mappings()
            .all()
        )
    ]

    hotel_cities = (
        (
            await db.execute(
                text(
                    """
                SELECT h.city_id, COUNT(DISTINCT h.id) AS n
                  FROM hotels h
                 WHERE h.status = 'ACTIVE' AND h.deleted_at IS NULL
                   AND h.city_id IS NOT NULL
                 GROUP BY h.city_id
                 ORDER BY h.city_id
                """
                )
            )
        )
        .mappings()
        .all()
    )
    tour_cities = (
        (
            await db.execute(
                text(
                    """
                SELECT tp.city_id, COUNT(DISTINCT tp.id) AS n
                  FROM tour_packages tp
                 WHERE tp.status = 'ACTIVE' AND tp.city_id IS NOT NULL
                 GROUP BY tp.city_id
                 ORDER BY tp.city_id
                """
                )
            )
        )
        .mappings()
        .all()
    )

    def _pack(rows):
        ids = [int(r["city_id"]) for r in rows if r["city_id"] is not None]
        total = sum(int(r["n"] or 0) for r in rows)
        return ids, total

    hotel_ids, hotel_total = _pack(hotel_cities)
    tour_ids, tour_total = _pack(tour_cities)

    response.headers["Cache-Control"] = _CACHE_CONTROL
    # ``supply_count`` semantics differ per vertical on purpose (cities with a
    # quotable fare vs. live listings); every consumer only branches on > 0.
    return {
        "services": {
            "CAB": {
                "active": active["CAB"],
                "supply_count": len(cab_cities),
                "city_ids": cab_cities,
            },
            "HOTEL": {
                "active": active["HOTEL"],
                "supply_count": hotel_total,
                "city_ids": hotel_ids,
            },
            "TOUR": {
                "active": active["TOUR"],
                "supply_count": tour_total,
                "city_ids": tour_ids,
            },
        }
    }
