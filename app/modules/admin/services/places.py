# ============================================================
# WAYTERO — ADMIN PLACES SERVICE
# File: app/modules/admin/services/places.py
# Doc Ref: Place-Level Landing Pages §Phase 1 (plan: rosy-vent-shrew)
#          Migration 0070_places
#
# Places (airports / railway stations) are master data, but unlike cities
# they are never inert: creating one provisions its PLACE_CABS landing page
# (unpublished), and the page's slug is derived from the place name at that
# moment. A later rename therefore does NOT re-derive the slug — live URLs
# must not move under the owner's feet (same rule the landing-page CMS
# enforces).
#
# Never commits: get_db owns the transaction. Raises WayTeroException only.
# ============================================================

from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ResourceNotFoundException, ValidationException
from app.modules.admin.models import LandingPage
from app.modules.admin.schemas import PlaceCreate, PlaceUpdate
from app.modules.master.models import City, Place


class PlaceService:
    @staticmethod
    async def list_all(
        db: AsyncSession,
        *,
        city_id: Optional[int] = None,
        active_only: bool = False,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        q = (
            select(
                Place,
                City.name.label("city_name"),
                LandingPage.slug.label("page_slug"),
                LandingPage.is_published.label("page_published"),
            )
            .join(City, City.id == Place.city_id)
            .outerjoin(
                LandingPage,
                (LandingPage.place_id == Place.id)
                & (LandingPage.page_type == "PLACE_CABS"),
            )
            .order_by(City.name, Place.kind, Place.name)
        )
        if city_id is not None:
            q = q.where(Place.city_id == city_id)
        if active_only:
            q = q.where(Place.is_active.is_(True))
        if search and search.strip():
            q = q.where(Place.name.ilike(f"%{search.strip()}%"))
        rows = (await db.execute(q)).all()
        out: List[Dict[str, Any]] = []
        for place, city_name, page_slug, page_published in rows:
            out.append(
                {
                    "id": int(place.id),
                    "city_id": int(place.city_id),
                    "city_name": city_name,
                    "kind": place.kind,
                    "name": place.name,
                    "code": place.code,
                    "address": place.address,
                    "latitude": (
                        float(place.latitude) if place.latitude is not None else None
                    ),
                    "longitude": (
                        float(place.longitude) if place.longitude is not None else None
                    ),
                    "is_active": bool(place.is_active),
                    "seo_title": place.seo_title,
                    "seo_description": place.seo_description,
                    "seo_keywords": place.seo_keywords,
                    "page_slug": page_slug,
                    "page_published": (
                        bool(page_published) if page_published is not None else None
                    ),
                    "created_at": place.created_at,
                    "updated_at": place.updated_at,
                }
            )
        return out

    @staticmethod
    async def get(db: AsyncSession, place_id: int) -> Place:
        place = await db.get(Place, place_id)
        if not place:
            raise ResourceNotFoundException("Place", place_id)
        return place

    @staticmethod
    async def create(
        db: AsyncSession, payload: PlaceCreate, actor_id: Optional[str] = None
    ) -> Dict[str, Any]:
        city = await db.get(City, payload.city_id)
        if not city:
            raise ResourceNotFoundException("City", payload.city_id)

        name = (payload.name or "").strip()
        if not name:
            raise ValidationException(
                "place name is required", details={"field": "name"}
            )
        dupe = (
            await db.execute(
                select(func.count(Place.id)).where(
                    Place.city_id == payload.city_id,
                    func.lower(Place.name) == name.lower(),
                )
            )
        ).scalar()
        if dupe:
            raise ValidationException(
                f"a place named {name!r} already exists in {city.name}",
                details={"field": "name"},
            )
        code = (payload.code or "").strip() or None
        if code:
            code_dupe = (
                await db.execute(
                    select(func.count(Place.id)).where(
                        func.upper(Place.code) == code.upper(),
                        Place.kind == payload.kind,
                    )
                )
            ).scalar()
            if code_dupe:
                raise ValidationException(
                    f"code {code!r} is already used for a {payload.kind}",
                    details={"field": "code"},
                )

        place = Place(
            city_id=payload.city_id,
            kind=payload.kind,
            name=name,
            code=code,
            address=(payload.address or "").strip() or None,
            latitude=payload.latitude,
            longitude=payload.longitude,
            is_active=bool(payload.is_active),
            seo_title=payload.seo_title,
            seo_description=payload.seo_description,
            seo_keywords=payload.seo_keywords,
        )
        db.add(place)
        await db.flush()
        await db.refresh(place)

        # The page is provisioned straight away but stays UNPUBLISHED: the
        # owner reviews the derived copy, adds any art and publishes.
        from app.modules.admin.services.landing_pages import LandingPageService

        await LandingPageService.sync_for_place(db, place.id, actor_id=actor_id)
        await db.flush()
        return await PlaceService._one(db, place.id)

    @staticmethod
    async def update(
        db: AsyncSession, place_id: int, payload: PlaceUpdate
    ) -> Dict[str, Any]:
        place = await PlaceService.get(db, place_id)
        data = payload.model_dump(exclude_unset=True)
        if "name" in data:
            name = (data["name"] or "").strip()
            if not name:
                raise ValidationException(
                    "place name cannot be empty", details={"field": "name"}
                )
            dupe = (
                await db.execute(
                    select(func.count(Place.id)).where(
                        Place.city_id == place.city_id,
                        func.lower(Place.name) == name.lower(),
                        Place.id != place.id,
                    )
                )
            ).scalar()
            if dupe:
                raise ValidationException(
                    f"a place named {name!r} already exists in this city",
                    details={"field": "name"},
                )
            data["name"] = name
        if "code" in data:
            data["code"] = (data["code"] or "").strip() or None
        if "address" in data:
            data["address"] = (data["address"] or "").strip() or None
        for field, value in data.items():
            setattr(place, field, value)
        await db.flush()
        return await PlaceService._one(db, place.id)

    @staticmethod
    async def delete(db: AsyncSession, place_id: int) -> None:
        place = await PlaceService.get(db, place_id)
        # ON DELETE CASCADE removes the PLACE_CABS page too — a page about a
        # place that no longer exists has nothing honest left to say.
        await db.delete(place)
        await db.flush()

    @staticmethod
    async def _one(db: AsyncSession, place_id: int) -> Dict[str, Any]:
        rows = await PlaceService.list_all(db)
        for row in rows:
            if row["id"] == place_id:
                return row
        raise ResourceNotFoundException("Place", place_id)
