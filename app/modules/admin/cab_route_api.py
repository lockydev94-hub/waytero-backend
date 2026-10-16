# ============================================================
# WAYTERO — ADMIN CAB ROUTES API
# File: app/modules/admin/cab_route_api.py
# Doc Ref: migration 0079_cab_routes
# Mounted at /admin/cab-routes (require_roles SUPER_ADMIN/ADMIN via api/router.py)
#
# Bare payloads, like the offers slice. A route owns its endpoints and the
# distance between them only — vehicle prices come from the fare engine at read
# time, so the admin never has a stale fare to keep in sync.
# ============================================================

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.admin import cab_route_distance, cab_route_service
from app.modules.admin.cab_route_models import CabRoute

router = APIRouter()

_MAX_DISTANCE_KM = Decimal("5000")

# Only fields the admin explicitly sent are written, so a partial save can
# never blank an image or clear the vehicle list by omission.
_MUTABLE = (
    "origin_label",
    "destination_label",
    "origin_city_id",
    "destination_city_id",
    "pricing_city_id",
    "description",
    "terms_and_conditions",
    "image_url",
    "is_active",
    "sort_order",
)


class RouteCreate(BaseModel):
    origin_label: str = Field(min_length=1, max_length=200)
    destination_label: str = Field(min_length=1, max_length=200)
    origin_city_id: int | None = Field(None, gt=0)
    destination_city_id: int | None = Field(None, gt=0)
    pricing_city_id: int | None = Field(None, gt=0)
    # Omitted, the distance is resolved from Google Maps on save.
    distance_km: Decimal | None = Field(None, gt=0, le=_MAX_DISTANCE_KM)
    description: str = ""
    terms_and_conditions: str = ""
    image_url: str | None = None
    is_active: bool = False
    sort_order: int = Field(0, ge=-32768, le=32767)
    vehicle_category_ids: list[int] = Field(default_factory=list)
    slug: str | None = Field(None, max_length=160)


class RouteUpdate(BaseModel):
    origin_label: str | None = Field(None, min_length=1, max_length=200)
    destination_label: str | None = Field(None, min_length=1, max_length=200)
    origin_city_id: int | None = Field(None, gt=0)
    destination_city_id: int | None = Field(None, gt=0)
    pricing_city_id: int | None = Field(None, gt=0)
    distance_km: Decimal | None = Field(None, gt=0, le=_MAX_DISTANCE_KM)
    description: str | None = None
    terms_and_conditions: str | None = None
    image_url: str | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(None, ge=-32768, le=32767)
    vehicle_category_ids: list[int] | None = None
    slug: str | None = Field(None, max_length=160)


class FetchDistanceIn(BaseModel):
    origin_label: str = Field(min_length=1, max_length=200)
    destination_label: str = Field(min_length=1, max_length=200)
    origin_city_id: int | None = Field(None, gt=0)
    destination_city_id: int | None = Field(None, gt=0)


async def _city_hint(db: AsyncSession, city_id: int | None) -> str | None:
    """An endpoint often names a town inside a district, so the linked city
    makes geocoding and the Distance Matrix markedly more accurate."""
    if city_id is None:
        return None
    return (
        await db.execute(text("SELECT name FROM cities WHERE id = :c"), {"c": city_id})
    ).scalar()


async def _save_route(
    db: AsyncSession, payload, current_user: dict, route_id: int | None
) -> int:
    creating = route_id is None
    provided = payload.model_fields_set

    if creating:
        route = CabRoute(created_by=str(current_user["sub"]))
        for field in _MUTABLE:
            setattr(route, field, getattr(payload, field))
        old = None
    else:
        route = (
            await db.execute(
                select(CabRoute)
                .where(CabRoute.id == route_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if route is None:
            raise ResourceNotFoundException("Cab route", route_id)
        old = {
            "origin_label": route.origin_label,
            "destination_label": route.destination_label,
            "distance_km": route.distance_km,
            "distance_source": route.distance_source,
            "pricing_city_id": route.pricing_city_id,
        }
        for field in _MUTABLE:
            if field in provided:
                setattr(route, field, getattr(payload, field))

    route.origin_label = (route.origin_label or "").strip()
    route.destination_label = (route.destination_label or "").strip()
    if not route.origin_label or not route.destination_label:
        raise ValidationException("Origin and destination are both required")
    if not cab_route_service.endpoints_differ(
        route.origin_label, route.destination_label
    ):
        raise ValidationException("Origin and destination must be different places")
    await cab_route_service.assert_endpoints_free(
        db, route.origin_label, route.destination_label, route_id
    )

    for cid, label in (
        (route.origin_city_id, "Origin city"),
        (route.destination_city_id, "Destination city"),
        (route.pricing_city_id, "Pricing city"),
    ):
        if cid is not None:
            await cab_route_service.assert_city_active(db, cid, label)

    if route.pricing_city_id is None:
        route.pricing_city_id = await cab_route_service.resolve_pricing_city(
            db, route.origin_city_id, route.destination_city_id
        )

    typed = payload.distance_km if "distance_km" in provided else None
    labels_changed = old is None or (
        route.origin_label != old["origin_label"]
        or route.destination_label != old["destination_label"]
    )

    if typed is not None and (creating or typed != old["distance_km"]):
        # The admin overrode the number by hand — that is authoritative.
        route.distance_km, route.distance_source = typed, "MANUAL"
    elif not creating and old["distance_source"] != "MANUAL" and labels_changed:
        route.distance_km, route.distance_source = await _resolve(db, route)
    elif creating:
        route.distance_km, route.distance_source = await _resolve(db, route)
    else:
        route.distance_km, route.distance_source = (
            old["distance_km"],
            old["distance_source"],
        )
    if route.distance_km <= 0 or route.distance_km > _MAX_DISTANCE_KM:
        raise ValidationException("Distance must be between 0 and 5000 km")

    if "vehicle_category_ids" in provided or creating:
        vehicle_ids = list(payload.vehicle_category_ids or [])
        await cab_route_service.assert_vehicle_categories_exist(db, vehicle_ids)
    else:
        vehicle_ids = await cab_route_service.linked_vehicle_category_ids(db, route_id)

    if route.is_active:
        await cab_route_service.assert_publishable(route, len(vehicle_ids))

    if creating:
        route.slug = await cab_route_service.unique_slug(
            db,
            payload.slug or f"{route.origin_label} to {route.destination_label}",
            None,
        )
        db.add(route)
        await db.flush()
    else:
        if "slug" in provided and payload.slug and payload.slug != route.slug:
            route.slug = await cab_route_service.unique_slug(db, payload.slug, route.id)
        await db.flush()

    await cab_route_service.set_route_vehicles(db, route.id, vehicle_ids)
    return route.id


async def _resolve(db: AsyncSession, route: CabRoute) -> tuple[Decimal, str]:
    return await cab_route_distance.resolve_route_distance(
        db,
        route.origin_label,
        route.destination_label,
        origin_hint=await _city_hint(db, route.origin_city_id),
        destination_hint=await _city_hint(db, route.destination_city_id),
    )


@router.get("", tags=["Cab Routes"])
async def list_routes(
    is_active: bool | None = None,
    q: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_db),
):
    where = []
    params: dict = {}
    if is_active is not None:
        where.append("r.is_active = :active")
        params["active"] = is_active
    if q:
        where.append("(r.origin_label ILIKE :q OR r.destination_label ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = (
        (
            await db.execute(
                text(
                    f"""
                    SELECT r.id, r.slug, r.origin_label, r.destination_label,
                           r.origin_city_id, r.destination_city_id, r.pricing_city_id,
                           pc.name AS pricing_city_name,
                           r.distance_km, r.distance_source, r.description,
                           r.terms_and_conditions, r.image_url, r.is_active, r.sort_order,
                           (SELECT COUNT(*) FROM cab_route_vehicle_categories l
                              JOIN vehicle_categories vc ON vc.id = l.vehicle_category_id
                             WHERE l.route_id = r.id AND vc.is_active) AS vehicle_count
                    FROM cab_routes r
                    LEFT JOIN cities pc ON pc.id = r.pricing_city_id
                    {wsql}
                    ORDER BY r.sort_order, r.id DESC
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.post("/fetch-distance", tags=["Cab Routes"])
async def fetch_distance(
    payload: FetchDistanceIn,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    """Backs the editor's "Fetch distance" button — reads Google, writes nothing."""
    origin = payload.origin_label.strip()
    destination = payload.destination_label.strip()
    if not cab_route_service.endpoints_differ(origin, destination):
        raise ValidationException("Origin and destination must be different places")
    distance_km, source = await cab_route_distance.resolve_route_distance(
        db,
        origin,
        destination,
        origin_hint=await _city_hint(db, payload.origin_city_id),
        destination_hint=await _city_hint(db, payload.destination_city_id),
    )
    return {"distance_km": distance_km, "distance_source": source}


@router.post("", status_code=201, tags=["Cab Routes"])
async def create_route(
    payload: RouteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    route_id = await _save_route(db, payload, current_user, None)
    return await get_route(route_id, db)


@router.get("/{route_id}", tags=["Cab Routes"])
async def get_route(route_id: int, db: AsyncSession = Depends(get_db)):
    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT r.*, oc.name AS origin_city_name,
                           dc.name AS destination_city_name,
                           pc.name AS pricing_city_name
                    FROM cab_routes r
                    LEFT JOIN cities oc ON oc.id = r.origin_city_id
                    LEFT JOIN cities dc ON dc.id = r.destination_city_id
                    LEFT JOIN cities pc ON pc.id = r.pricing_city_id
                    WHERE r.id = :r
                    """
                ),
                {"r": route_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ResourceNotFoundException("Cab route", route_id)
    vehicles = (
        (
            await db.execute(
                text(
                    """
                    SELECT vc.id, vc.category_name
                    FROM cab_route_vehicle_categories l
                    JOIN vehicle_categories vc ON vc.id = l.vehicle_category_id
                    WHERE l.route_id = :r
                    ORDER BY vc.display_order, vc.id
                    """
                ),
                {"r": route_id},
            )
        )
        .mappings()
        .all()
    )
    d = dict(row)
    d["vehicle_category_ids"] = [v["id"] for v in vehicles]
    d["vehicle_category_names"] = [v["category_name"] for v in vehicles]
    return d


@router.put("/{route_id}", tags=["Cab Routes"])
async def update_route(
    route_id: int,
    payload: RouteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    # Row lock first: two admins editing the same corridor must not race on
    # the distance re-resolution or the vehicle list.
    await db.execute(
        text("SELECT 1 FROM cab_routes WHERE id = :r FOR UPDATE"), {"r": route_id}
    )
    saved_id = await _save_route(db, payload, current_user, route_id)
    return await get_route(saved_id, db)


@router.delete("/{route_id}", status_code=204, tags=["Cab Routes"])
async def delete_route(
    route_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN")),
):
    deleted = (
        await db.execute(
            text("DELETE FROM cab_routes WHERE id = :r RETURNING id"), {"r": route_id}
        )
    ).scalar()
    if deleted is None:
        raise ResourceNotFoundException("Cab route", route_id)
    await db.flush()
