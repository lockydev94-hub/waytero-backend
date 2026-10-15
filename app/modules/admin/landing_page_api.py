# ============================================================
# WAYTERO — ADMIN LANDING PAGES API
# File: app/modules/admin/landing_page_api.py
# Doc Ref: City + Service-Type Landing Pages §Phase 1
# Prefix: /admin/settings/landing-pages  (registered in api/router.py)
#
#   GET    /landing-pages                    — list (filterable, paginated)
#   GET    /landing-pages/reserved-slugs     — slugs the website already owns
#   GET    /landing-pages/slot-specs         — per-slot crop dimensions
#   GET    /landing-pages/suggestions        — derived slug + default blocks
#   GET    /landing-pages/specs              — page_type / block vocabulary
#   POST   /landing-pages/sync               — provision every missing page
#   POST   /landing-pages                    — create
#   GET    /landing-pages/{id}               — read
#   GET    /landing-pages/{id}/preview       — the public bundle, draft included
#   PATCH  /landing-pages/{id}               — update
#   DELETE /landing-pages/{id}               — delete
#   POST   /landing-pages/{id}/publish       — publish / unpublish
#
# Literal paths are declared before /{id} — FastAPI matches in order, so a
# later "{id}" route would swallow "sync" as an integer and 422 the request.
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.modules.admin.constants.landing_pages import (
    BLOCK_TYPES,
    MAX_BLOCKS,
    MAX_FAQS,
    MAX_GALLERY,
    MAX_HIGHLIGHTS,
    MAX_STATS,
    PAGE_TYPES,
    PAGE_TYPE_PREFIX,
    PAGE_TYPE_SERVICE,
    RESERVED_LANDING_SLUGS,
    SLOT_SPECS,
    blocks_allowed_for,
)
from app.modules.admin.schemas.landing_pages import (
    LandingPageCreate,
    LandingPagePublishIn,
    LandingPageUpdate,
)
from app.modules.admin.services.landing_pages import LandingPageService

router = APIRouter()

_TAG = "Landing Pages"


# ── List ─────────────────────────────────────────────────────────────────────


@router.get(
    "/landing-pages",
    response_model=dict,
    tags=[_TAG],
    summary="List landing pages (filter by type, scope, state, text)",
)
async def list_landing_pages(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    page_type: Optional[str] = Query(None, description="Filter by page_type"),
    city_id: Optional[int] = Query(None, ge=1),
    state_id: Optional[int] = Query(None, ge=1),
    search: Optional[str] = Query(None, description="Matches slug or city/state"),
    status: Optional[str] = Query(None, description="all | published | draft"),
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("cms.section.manage")),
):
    items, total = await LandingPageService.list_pages(
        db,
        page_type=page_type,
        city_id=city_id,
        state_id=state_id,
        search=search,
        status=None if status in (None, "all") else status,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    total_pages = (total + per_page - 1) // per_page if per_page else 0
    return {
        "success": True,
        "data": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


# ── Contract feeds for the admin editor ──────────────────────────────────────


@router.get(
    "/landing-pages/reserved-slugs",
    response_model=dict,
    tags=[_TAG],
    summary="Slugs that would collide with an existing website route",
)
async def get_reserved_slugs(
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("cms.section.manage")),
):
    return {
        "success": True,
        "data": {
            "reserved": sorted(RESERVED_LANDING_SLUGS),
            # Published slugs are not "reserved" but a rename still breaks
            # live URLs, so the editor wants to know about them too.
            "published": await LandingPageService.published_slugs(db),
        },
    }


@router.get(
    "/landing-pages/slot-specs",
    response_model=dict,
    tags=[_TAG],
    summary="Per-slot crop dimensions the upload modal must enforce",
)
async def get_slot_specs(
    _user=Depends(require_permission("cms.section.manage")),
):
    return {"success": True, "data": SLOT_SPECS}


@router.get(
    "/landing-pages/specs",
    response_model=dict,
    tags=[_TAG],
    summary="Page types, block vocabulary and content limits",
)
async def get_specs(
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("cms.section.manage")),
):
    return {
        "success": True,
        "data": {
            "page_types": list(PAGE_TYPES),
            "page_type_prefix": PAGE_TYPE_PREFIX,
            "page_type_service": PAGE_TYPE_SERVICE,
            "block_types": sorted(BLOCK_TYPES),
            # Per page type, so the editor's "add block" menu can only ever
            # offer what validate_write accepts. One predicate on the backend
            # (constants.block_allowed) feeds both sides.
            "blocks_by_page_type": {pt: blocks_allowed_for(pt) for pt in PAGE_TYPES},
            "limits": {
                "blocks": MAX_BLOCKS,
                "gallery": MAX_GALLERY,
                "highlights": MAX_HIGHLIGHTS,
                "stats": MAX_STATS,
                "faqs": MAX_FAQS,
            },
            "slot_specs": SLOT_SPECS,
            "options": await LandingPageService.editor_options(db),
        },
    }


@router.get(
    "/landing-pages/suggestions",
    response_model=dict,
    tags=[_TAG],
    summary="Derived slug, copy seed and default blocks for a scope",
)
async def get_suggestions(
    page_type: str = Query(..., description="One of PAGE_TYPES"),
    city_id: Optional[int] = Query(None, ge=1),
    state_id: Optional[int] = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("cms.section.manage")),
):
    data = await LandingPageService.suggest(db, page_type, city_id, state_id)
    return {"success": True, "data": data}


# ── Provisioning ─────────────────────────────────────────────────────────────


@router.post(
    "/landing-pages/sync",
    response_model=dict,
    tags=[_TAG],
    summary="Provision any missing landing page for active cities and states",
)
async def sync_landing_pages(
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission("cms.section.manage")),
):
    result = await LandingPageService.sync_all(db, actor_id=str(user.get("sub")))
    return {
        "success": True,
        "message": (
            f"Provisioned {len(result['created'])} page(s)"
            if result["created"]
            else "All active cities and states already have pages"
        ),
        "data": result,
    }


# ── Create ───────────────────────────────────────────────────────────────────


@router.post(
    "/landing-pages",
    response_model=dict,
    tags=[_TAG],
    status_code=201,
    summary="Create a landing page",
)
async def create_landing_page(
    payload: LandingPageCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission("cms.section.manage")),
):
    data = await LandingPageService.create(db, payload, actor_id=str(user.get("sub")))
    return {"success": True, "data": data}


# ── Read / preview / update / delete / publish ───────────────────────────────


@router.get(
    "/landing-pages/{page_id}",
    response_model=dict,
    tags=[_TAG],
    summary="Read one landing page",
)
async def get_landing_page(
    page_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("cms.section.manage")),
):
    data = await LandingPageService.get_page_dict(db, page_id)
    return {"success": True, "data": data}


@router.get(
    "/landing-pages/{page_id}/preview",
    response_model=dict,
    tags=[_TAG],
    summary="The exact bundle the website would render, drafts included",
)
async def preview_landing_page(
    page_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_permission("cms.section.manage")),
):
    page = await LandingPageService.get_page(db, page_id)
    if not page:
        from app.core.exceptions import ResourceNotFoundException

        raise ResourceNotFoundException("LandingPage", page_id)
    return {
        "success": True,
        "data": await LandingPageService.build_public_payload(db, page),
    }


@router.patch(
    "/landing-pages/{page_id}",
    response_model=dict,
    tags=[_TAG],
    summary="Update a landing page",
)
async def update_landing_page(
    page_id: int,
    payload: LandingPageUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission("cms.section.manage")),
):
    data = await LandingPageService.update(
        db, page_id, payload, actor_id=str(user.get("sub"))
    )
    return {"success": True, "data": data}


@router.delete(
    "/landing-pages/{page_id}",
    response_model=dict,
    tags=[_TAG],
    summary="Delete a landing page",
)
async def delete_landing_page(
    page_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission("cms.section.manage")),
):
    await LandingPageService.delete(db, page_id, actor_id=str(user.get("sub")))
    return {"success": True, "message": "Landing page deleted"}


@router.post(
    "/landing-pages/{page_id}/publish",
    response_model=dict,
    tags=[_TAG],
    summary="Publish or unpublish a landing page",
)
async def publish_landing_page(
    page_id: int,
    payload: LandingPagePublishIn,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission("cms.section.manage")),
):
    data = await LandingPageService.set_published(
        db,
        page_id,
        payload.is_published,
        confirmed=payload.confirmed,
        actor_id=str(user.get("sub")),
    )
    return {
        "success": True,
        "message": "Published" if payload.is_published else "Unpublished",
        "data": data,
    }
