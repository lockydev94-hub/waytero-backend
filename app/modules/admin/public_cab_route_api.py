# ============================================================
# WAYTERO — PUBLIC CAB ROUTES API (no auth)
# File: app/modules/admin/public_cab_route_api.py
# Doc Ref: migration 0079_cab_routes; BRD Part 3 §35-§36 (fare engine)
#
# Endpoints:
#   GET /public/routes                 — active routes for the homepage + hub
#   GET /public/routes/{slug}          — route with live per-vehicle fares
#   GET /public/routes/{slug}/related  — sibling corridors for the switcher
#
# Raw JSON, like every other /public route. Prices are computed here, never
# stored: the same rule lookup and breakdown the booking engine uses.
# ============================================================

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.admin.cab_route_service import TRIP_TYPE, quote_vehicles
from app.modules.admin.public_cab_api import FareBreakdownItem

router = APIRouter()


async def _active_route_by_slug(db: AsyncSession, slug: str) -> dict:
    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT r.id, r.slug, r.origin_label, r.destination_label,
                           r.origin_city_id, r.destination_city_id,
                           r.pricing_city_id, pc.name AS pricing_city_name,
                           r.distance_km, r.distance_source, r.description,
                           r.terms_and_conditions, r.image_url
                    FROM cab_routes r
                    LEFT JOIN cities pc ON pc.id = r.pricing_city_id
                    WHERE r.slug = :s AND r.is_active
                    """
                ),
                {"s": slug},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(404, "Route not found")
    return dict(row)


# ── GET /public/routes ────────────────────────────────────────────────────────


@router.get("/routes")
async def list_routes(
    limit: int = Query(12, ge=1, le=60),
    offset: int = Query(0, ge=0, le=100000),
    q: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_db),
):
    """Active routes, no fare math — `total` drives "View More (12 of 14)"."""
    where = ["r.is_active"]
    params: dict = {"limit": limit, "offset": offset}
    if q and q.strip():
        where.append("(r.origin_label ILIKE :q OR r.destination_label ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    wsql = "WHERE " + " AND ".join(where)
    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT r.id, r.slug, r.origin_label, r.destination_label,
                           r.distance_km, r.distance_source, r.image_url,
                           (SELECT COUNT(*) FROM cab_route_vehicle_categories l
                              JOIN vehicle_categories vc ON vc.id = l.vehicle_category_id
                             WHERE l.route_id = r.id AND vc.is_active) AS vehicle_count
                    FROM cab_routes r
                    {wsql}
                    ORDER BY r.sort_order, r.id
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    total_params = {"q": params["q"]} if "q" in params else {}
    total = (
        await db.execute(
            text(f"SELECT COUNT(*) FROM cab_routes r {wsql}"),
            total_params,
        )
    ).scalar_one()
    return {"items": [dict(r) for r in rows], "total": int(total)}


# ── GET /public/routes/{slug} ─────────────────────────────────────────────────


@router.get("/routes/{slug}")
async def get_route(slug: str, db: AsyncSession = Depends(get_db)):
    route = await _active_route_by_slug(db, slug)
    vehicle_ids = (
        (
            await db.execute(
                text(
                    """
                    SELECT l.vehicle_category_id FROM cab_route_vehicle_categories l
                    JOIN vehicle_categories vc ON vc.id = l.vehicle_category_id
                    WHERE l.route_id = :r AND vc.is_active
                    ORDER BY vc.display_order, vc.id
                    """
                ),
                {"r": route["id"]},
            )
        )
        .scalars()
        .all()
    )
    quoted = await quote_vehicles(
        db,
        pricing_city_id=route["pricing_city_id"],
        distance_km=route["distance_km"],
        vehicle_category_ids=list(vehicle_ids),
    )
    route["trip_type"] = TRIP_TYPE
    route["vehicles"] = [FareBreakdownItem(**q) for q in quoted]
    return route


# ── GET /public/routes/{slug}/related ─────────────────────────────────────────


@router.get("/routes/{slug}/related")
async def list_related(slug: str, db: AsyncSession = Depends(get_db)):
    """Other corridors that touch this route's endpoints, then its pricing city.

    Endpoints are free text, so two of them is the strongest link available;
    the pricing-city top-up keeps the switcher useful for a one-of-a-kind route.
    """
    route = await _active_route_by_slug(db, slug)
    # Every branch is added only when it has a value: an untyped NULL bind
    # parameter has no type for asyncpg to infer, and this query would fail.
    clauses = []
    params: dict = {"id": route["id"]}
    if route["origin_city_id"] is not None:
        clauses.append("(origin_city_id = :oc OR destination_city_id = :oc)")
        params["oc"] = route["origin_city_id"]
    if route["destination_city_id"] is not None:
        clauses.append("(origin_city_id = :dc OR destination_city_id = :dc)")
        params["dc"] = route["destination_city_id"]
    params["origin"] = route["origin_label"].lower()
    params["destination"] = route["destination_label"].lower()
    clauses.append(
        "(lower(origin_label) IN (:origin, :destination) "
        "OR lower(destination_label) IN (:origin, :destination))"
    )
    shared = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT id, slug, origin_label, destination_label, distance_km
                    FROM cab_routes
                    WHERE is_active AND id <> :id
                      AND ({" OR ".join(clauses)})
                    ORDER BY sort_order, id
                    LIMIT 6
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    items = [dict(r) for r in shared]
    if len(items) < 6:
        seen = {route["id"], *(i["id"] for i in items)}
        more = (
            (
                await db.execute(
                    text(
                        """
                        SELECT id, slug, origin_label, destination_label, distance_km
                        FROM cab_routes
                        WHERE is_active AND pricing_city_id = :city
                          AND NOT (id = ANY(CAST(:seen AS BIGINT[])))
                        ORDER BY sort_order, id
                        LIMIT :lim
                        """
                    ),
                    {
                        "city": route["pricing_city_id"],
                        "seen": list(seen),
                        "lim": 6 - len(items),
                    },
                )
            )
            .mappings()
            .all()
        )
        items.extend(dict(r) for r in more)
    return {"items": items}
