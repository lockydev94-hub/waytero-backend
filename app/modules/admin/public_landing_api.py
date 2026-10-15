# ============================================================
# WAYTERO — PUBLIC LANDING PAGES API
# File: app/modules/admin/public_landing_api.py
# Doc Ref: City + Service-Type Landing Pages §Phase 1
# Prefix: /public  (registered in api/router.py)
#
#   GET /public/landing-pages          — slim refs for every published page
#   GET /public/landing-pages/{slug}   — the full bundle for one page
#
# No auth: customer-web calls these from server components at build/revalidate
# time. Payloads are returned bare (no {success, data} envelope), matching
# every other /public endpoint the website consumes.
#
# "/landing-pages" is declared before "/landing-pages/{slug}" — the path param
# is a string, so a reversed order would send the literal list request into the
# single-page handler and 404 on a slug that happens to be empty.
# ============================================================

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ResourceNotFoundException
from app.modules.admin.models import LandingPage
from app.modules.admin.services.landing_pages import LandingPageService
from app.modules.master.models import City, Place

router = APIRouter()

# The website caches these for 300s; the header just keeps CDN behaviour
# aligned with the ISR window so a revalidated page is never served stale.
_CACHE_CONTROL = "public, max-age=60, s-maxage=300, stale-while-revalidate=600"


@router.get(
    "/landing-pages",
    response_model=List[Dict[str, Any]],
    summary="Published landing pages — slug, type, canonical path (no auth)",
    description=(
        "The single feed behind the website's generateStaticParams, sitemap.xml "
        "and llms.txt. Slim on purpose: a crawl should cost one cheap query."
    ),
)
async def list_public_landing_pages(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return await LandingPageService.list_public_refs(db)


@router.get(
    "/landing-pages/{slug}",
    response_model=Dict[str, Any],
    summary="Full bundle for one published landing page (no auth)",
    description=(
        "Copy, hero (with image_source so the client can fall back to a gradient), "
        "blocks, derived prices and live hotel/tour/vehicle data — one request "
        "renders the whole page."
    ),
)
async def get_public_landing_page(
    slug: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    page = await LandingPageService.resolve_public(db, slug)
    if not page:
        raise ResourceNotFoundException("LandingPage", slug)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return await LandingPageService.build_public_payload(db, page)


@router.get(
    "/places",
    response_model=List[Dict[str, Any]],
    summary="Places with a published landing page — airport/station matching (no auth)",
    description=(
        "Active airports and railway stations whose PLACE_CABS page is live. "
        "The website matches a geocoded pickup against this list to record "
        "booking intent and deep-link to the right place page."
    ),
)
async def list_public_places(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    rows = (
        (
            await db.execute(
                select(
                    Place.id,
                    Place.name,
                    Place.kind,
                    Place.code,
                    Place.latitude,
                    Place.longitude,
                    Place.city_id,
                    City.name.label("city_name"),
                    LandingPage.slug,
                )
                .join(City, City.id == Place.city_id)
                .join(
                    LandingPage,
                    (LandingPage.place_id == Place.id)
                    & (LandingPage.page_type == "PLACE_CABS"),
                )
                .where(
                    Place.is_active.is_(True),
                    await LandingPageService.public_filter(),
                )
                .order_by(Place.name)
            )
        )
        .mappings()
        .all()
    )
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "kind": r["kind"],
            "code": r["code"],
            "city_id": int(r["city_id"]),
            "city_name": r["city_name"],
            "slug": r["slug"],
            "latitude": float(r["latitude"]) if r["latitude"] is not None else None,
            "longitude": float(r["longitude"]) if r["longitude"] is not None else None,
        }
        for r in rows
    ]
