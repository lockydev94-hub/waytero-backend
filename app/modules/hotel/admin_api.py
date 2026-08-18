# ============================================================
# WAY TERO — HOTEL ADMIN API
# File: app/modules/hotel/admin_api.py
# Doc Ref: BRD Part 4 §57-92, SRS Part 5 §153-194, API Doc 09_HOTEL_API
#          Docs/21_Hotel_Module_Implementation/02_BACKEND_API.md
#
# Mounted at /api/v1/admin/hotels.
#
# Every endpoint declares a role dependency. Some existing partner/vehicle
# endpoints omit one; that is a defect this module does not copy.
#
# Reads return the bare payload the admin portal expects; writes return the
# standard envelope. Nothing here commits — get_db owns the transaction.
# ============================================================

from datetime import date
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.modules.hotel.constants import (
    BED_TYPES,
    COMMISSION_APPLIES_TO,
    CONFIRMATION_MODES,
    HOTEL_COMMISSION_TYPES,
    HOTEL_DOCUMENT_TYPES,
    HOTEL_IMAGE_TYPES,
    HOTEL_REQUIRED_DOCUMENT_TYPES,
    HOTEL_ROOM_TYPES,
    HOTEL_STATUSES,
    HOTEL_TAX_MODES,
    MEAL_PLANS,
    PHYSICAL_ROOM_STATUSES,
    RATE_MODES,
    RATE_PLAN_TYPES,
    ROOM_ALLOCATION_MODES,
    VALID_HOTEL_TRANSITIONS,
    VIEW_TYPES,
)
from app.modules.hotel.schemas import (
    ApproveHotelRequest,
    AssignOfficerRequest,
    BulkRoomCreate,
    GstSlabCreate,
    GstSlabResponse,
    GstSlabUpdate,
    HotelAmenitiesUpdate,
    HotelAmenityCreate,
    HotelAmenityResponse,
    HotelAmenityUpdate,
    HotelCategoryCreate,
    HotelCategoryResponse,
    HotelCategoryUpdate,
    HotelCommissionCreate,
    HotelCreate,
    HotelDetailResponse,
    HotelDocumentCreate,
    HotelDocumentResponse,
    HotelDocumentVerify,
    HotelImageCreate,
    HotelImageResponse,
    HotelImagesReorder,
    HotelImageUpdate,
    HotelPolicyResponse,
    HotelPolicyUpdate,
    HotelProfileUpdate,
    HotelSeoUpdate,
    HotelStatsResponse,
    HotelTaxUpdate,
    InventoryGenerate,
    InventoryResponse,
    InventoryUpdate,
    RatePlanCreate,
    RatePlanResponse,
    RatePlanUpdate,
    ReadinessResponse,
    RejectHotelRequest,
    RoomCategoryCreate,
    RoomCategoryImageCreate,
    RoomCategoryImageResponse,
    RoomCategoryResponse,
    RoomCategoryUpdate,
    RoomActiveReservation,
    RoomCreate,
    RoomResponse,
    RoomUpdate,
    StatusChangeRequest,
)
from app.modules.hotel.services import (
    HotelReadinessService,
    HotelService,
    HotelVerificationService,
    platform_gst_enabled,
)
from app.modules.hotel.services.masters import HotelMasterService
from app.modules.hotel.services.rooms import (
    InventoryService,
    RatePlanService,
    RoomCategoryService,
)
from app.shared.responses.base import success_response

router = APIRouter()

# Role sets, per 02_BACKEND_API.md §3. Verification officers can read the
# pipeline and act on documents; they cannot create or configure commercials.
ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
ADMIN_OR_OFFICER = ("SUPER_ADMIN", "ADMIN", "VERIFICATION_OFFICER")
# The paginated list is also reached by the partner portal ("My Hotels"), which
# calls this same route. A partner is only ever shown their own properties —
# list_hotels forces the partner_id filter to the caller's own partner, so the
# extra role here cannot be used to read another partner's hotels.
ADMIN_OFFICER_OR_PARTNER = ("SUPER_ADMIN", "ADMIN", "VERIFICATION_OFFICER", "PARTNER")


def _actor(current_user: dict) -> Optional[UUID]:
    raw = current_user.get("sub")
    return UUID(str(raw)) if raw else None


def _is_partner_only(roles: list[str]) -> bool:
    """True when the caller holds PARTNER but none of the admin/officer roles."""
    return "PARTNER" in roles and not any(
        r in roles for r in ("SUPER_ADMIN", "ADMIN", "VERIFICATION_OFFICER")
    )


async def _own_partner_id(db: AsyncSession, current_user: dict) -> Optional[int]:
    """Resolve the caller's Partner row id from their user UUID (JWT ``sub``).

    Mirrors ``_resolve_partner_id`` in the partner booking API. Returns None when
    the user has no partner profile, so the caller can render an empty list
    rather than leaking every hotel.
    """
    from sqlalchemy import select as _select

    from app.modules.partner.models import Partner

    raw = current_user.get("sub")
    if not raw:
        return None
    pid = (
        await db.execute(_select(Partner.id).where(Partner.user_id == UUID(str(raw))))
    ).scalar_one_or_none()
    return int(pid) if pid is not None else None


async def _assert_partner_owns(
    db: AsyncSession, current_user: dict, hotel: Any
) -> None:
    """Raise PermissionDeniedException if a PARTNER-only caller doesn't own this hotel."""
    roles = current_user.get("roles") or [current_user.get("role", "")]
    if not _is_partner_only(roles):
        return
    own_id = await _own_partner_id(db, current_user)
    if own_id is None or int(hotel.partner_id) != own_id:
        from app.core.exceptions import PermissionDeniedException

        raise PermissionDeniedException("You do not have access to this hotel")


# ============================================================
# DEPENDENCIES
# ============================================================


def get_hotel_service(db: AsyncSession = Depends(get_db)) -> HotelService:
    return HotelService(db)


def get_verification_service(
    db: AsyncSession = Depends(get_db),
) -> HotelVerificationService:
    return HotelVerificationService(db)


def get_readiness_service(db: AsyncSession = Depends(get_db)) -> HotelReadinessService:
    return HotelReadinessService(db)


def get_master_service(db: AsyncSession = Depends(get_db)) -> HotelMasterService:
    return HotelMasterService(db)


def get_category_service(db: AsyncSession = Depends(get_db)) -> RoomCategoryService:
    return RoomCategoryService(db)


def get_rate_plan_service(db: AsyncSession = Depends(get_db)) -> RatePlanService:
    return RatePlanService(db)


def get_inventory_service(db: AsyncSession = Depends(get_db)) -> InventoryService:
    return InventoryService(db)


# ============================================================
# STATIC ROUTES
#
# Declared before /{hotel_id} so a literal segment is never swallowed by the
# int path parameter. FastAPI matches in declaration order.
# ============================================================


@router.get("/meta", summary="Enum values the hotel forms need")
async def hotel_meta(
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
) -> dict[str, Any]:
    """Served rather than duplicated in the frontend, so the picker options and
    the server-side validators cannot drift apart."""
    return {
        "statuses": HOTEL_STATUSES,
        "transitions": VALID_HOTEL_TRANSITIONS,
        "document_types": HOTEL_DOCUMENT_TYPES,
        "required_document_types": HOTEL_REQUIRED_DOCUMENT_TYPES,
        "image_types": HOTEL_IMAGE_TYPES,
        "room_types": HOTEL_ROOM_TYPES,
        "bed_types": BED_TYPES,
        "view_types": VIEW_TYPES,
        "meal_plans": MEAL_PLANS,
        "physical_room_statuses": PHYSICAL_ROOM_STATUSES,
        "rate_plan_types": RATE_PLAN_TYPES,
        "rate_modes": RATE_MODES,
        "commission_types": HOTEL_COMMISSION_TYPES,
        "commission_applies_to": COMMISSION_APPLIES_TO,
        "tax_modes": HOTEL_TAX_MODES,
        "confirmation_modes": CONFIRMATION_MODES,
        "room_allocation_modes": ROOM_ALLOCATION_MODES,
    }


@router.get(
    "/stats", response_model=HotelStatsResponse, summary="Hotel pipeline KPI counts"
)
async def hotel_stats(
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> Any:
    # An officer's landing view should be their own queue, not a global count.
    roles = current_user.get("roles") or [current_user.get("role", "")]
    officer_id = (
        _actor(current_user)
        if "VERIFICATION_OFFICER" in roles and "ADMIN" not in roles
        else None
    )
    return await service.stats(officer_id=officer_id)


@router.get("/categories", summary="Hotel property categories")
async def list_hotel_categories(
    include_inactive: bool = Query(False),
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelMasterService = Depends(get_master_service),
) -> list[HotelCategoryResponse]:
    rows = await service.list_categories(include_inactive=include_inactive)
    return [HotelCategoryResponse.model_validate(r) for r in rows]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_hotel_category(
    payload: HotelCategoryCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    category = await service.create_category(payload)
    return success_response(
        "Category created successfully", HotelCategoryResponse.model_validate(category)
    )


@router.patch("/categories/{category_id}")
async def update_hotel_category(
    category_id: int,
    payload: HotelCategoryUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    category = await service.update_category(category_id, payload)
    return success_response(
        "Category updated successfully", HotelCategoryResponse.model_validate(category)
    )


@router.get("/amenities", summary="Hotel amenity master list")
async def list_hotel_amenities(
    include_inactive: bool = Query(False),
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelMasterService = Depends(get_master_service),
) -> list[HotelAmenityResponse]:
    rows = await service.list_amenities(include_inactive=include_inactive)
    return [HotelAmenityResponse.model_validate(r) for r in rows]


@router.post("/amenities", status_code=status.HTTP_201_CREATED)
async def create_hotel_amenity(
    payload: HotelAmenityCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    amenity = await service.create_amenity(payload)
    return success_response(
        "Amenity created successfully", HotelAmenityResponse.model_validate(amenity)
    )


@router.patch("/amenities/{amenity_id}")
async def update_hotel_amenity(
    amenity_id: int,
    payload: HotelAmenityUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    amenity = await service.update_amenity(amenity_id, payload)
    return success_response(
        "Amenity updated successfully", HotelAmenityResponse.model_validate(amenity)
    )


@router.get("/gst-slabs", summary="GST slabs keyed on nightly tariff")
async def list_gst_slabs(
    on_date: Optional[date] = Query(None),
    include_inactive: bool = Query(False),
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelMasterService = Depends(get_master_service),
) -> list[GstSlabResponse]:
    rows = await service.list_gst_slabs(
        on_date=on_date, include_inactive=include_inactive
    )
    return [GstSlabResponse.model_validate(r) for r in rows]


# Editing a tax rate is a higher-privilege act than editing a hotel: a wrong
# slab silently mis-invoices every booking in its tariff band.
@router.post("/gst-slabs", status_code=status.HTTP_201_CREATED)
async def create_gst_slab(
    payload: GstSlabCreate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN")),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    slab = await service.create_gst_slab(payload)
    return success_response(
        "GST slab created successfully", GstSlabResponse.model_validate(slab)
    )


@router.patch("/gst-slabs/{slab_id}")
async def update_gst_slab(
    slab_id: int,
    payload: GstSlabUpdate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN")),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    slab = await service.update_gst_slab(slab_id, payload)
    return success_response(
        "GST slab updated successfully", GstSlabResponse.model_validate(slab)
    )


@router.delete("/gst-slabs/{slab_id}")
async def deactivate_gst_slab(
    slab_id: int,
    current_user: dict = Depends(require_roles("SUPER_ADMIN")),
    service: HotelMasterService = Depends(get_master_service),
) -> dict[str, Any]:
    slab = await service.deactivate_gst_slab(slab_id)
    return success_response(
        "GST slab deactivated successfully", GstSlabResponse.model_validate(slab)
    )


@router.get(
    "/partners-with-hotel",
    summary="Partners for the add-hotel dropdown, flagged by HOTEL service",
)
async def partners_with_hotel_service(
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Every active partner, with a has_hotel flag — deliberately not filtered
    to HOTEL holders only. The wizard offers to enable the service inline, and
    hiding the partner would leave the admin with nothing to click.

    Built the same way as /admin/vehicles/partners-with-cab.
    """
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    search_clause = ""
    if search and search.strip():
        search_clause = (
            " AND (p.owner_name ILIKE :q OR p.business_name ILIKE :q "
            "OR p.mobile ILIKE :q OR p.partner_code ILIKE :q)"
        )
        params["q"] = f"%{search.strip()}%"

    where = f"WHERE p.deleted_at IS NULL AND p.status = 'ACTIVE'{search_clause}"

    total = (
        await db.execute(text(f"SELECT COUNT(*) FROM partners p {where}"), params)
    ).scalar() or 0

    rows = await db.execute(
        text(
            "SELECT p.id, p.owner_name, p.business_name, p.mobile, p.partner_code, "
            "  p.city_id, p.status, "
            "  EXISTS(SELECT 1 FROM partner_services ps "
            "         WHERE ps.partner_id = p.id AND ps.service_type = 'HOTEL' "
            "           AND ps.is_active = TRUE) AS has_hotel, "
            "  COALESCE((SELECT string_agg(ps2.service_type, ',' "
            "                              ORDER BY ps2.service_type) "
            "            FROM partner_services ps2 "
            "            WHERE ps2.partner_id = p.id AND ps2.is_active = TRUE), '') "
            "    AS active_services, "
            "  (SELECT COUNT(*) FROM hotels h "
            "   WHERE h.partner_id = p.id AND h.deleted_at IS NULL) AS hotel_count "
            f"FROM partners p {where} "
            "ORDER BY has_hotel DESC, p.owner_name ASC LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    return {
        "items": [dict(r) for r in rows.mappings().all()],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.get(
    "/verification-officers",
    summary="Active verification officers with their current hotel load",
)
async def list_verification_officers(
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """The load count is what makes assignment a decision rather than a guess.

    Verification officers are the intended assignees. Admins and super-admins are
    also returned so they can self-assign and verify a hotel at their own risk
    (the UI flags this) when no officer is available. Officers are ordered first.
    """
    rows = await db.execute(
        text(
            "SELECT u.id, "
            "  TRIM(CONCAT(u.first_name, ' ', COALESCE(u.last_name, ''))) AS name, "
            "  u.mobile_number, u.email, u.user_type, "
            "  (SELECT COUNT(*) FROM hotel_verification_assignments a "
            "     JOIN hotels h ON h.id = a.hotel_id "
            "    WHERE a.officer_id = u.id AND a.is_active = TRUE "
            "      AND h.deleted_at IS NULL "
            "      AND h.status IN ('PENDING', 'UNDER_REVIEW', 'DOCUMENT_PENDING')"
            "  ) AS active_hotel_count "
            "FROM users u "
            "WHERE u.user_type IN ('VERIFICATION_OFFICER', 'ADMIN', 'SUPER_ADMIN') "
            "  AND u.is_active = TRUE AND u.deleted_at IS NULL "
            "ORDER BY "
            "  CASE u.user_type WHEN 'VERIFICATION_OFFICER' THEN 0 ELSE 1 END, "
            "  active_hotel_count ASC, name ASC"
        )
    )
    return [dict(r) for r in rows.mappings().all()]


@router.post(
    "/partners/{partner_id}/enable-hotel-service",
    summary="Grant a partner the HOTEL service without leaving the hotel wizard",
)
async def enable_hotel_service(
    partner_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Adds HOTEL to the partner's existing services and writes the same
    ADMIN_SERVICES_UPDATED log the partner-services endpoint writes.

    Additive on purpose: the partner-services PATCH is replace-set semantics, so
    reusing it from here would silently drop the partner's CAB and TOUR grants.
    """
    partner = (
        await db.execute(
            text(
                "SELECT id FROM partners WHERE id = :pid AND deleted_at IS NULL "
                "  AND status = 'ACTIVE'"
            ),
            {"pid": partner_id},
        )
    ).scalar_one_or_none()
    if partner is None:
        from app.core.exceptions import ResourceNotFoundException

        raise ResourceNotFoundException("Active partner", partner_id)

    await db.execute(
        text(
            "INSERT INTO partner_services (partner_id, service_type, is_active, "
            "                              created_at) "
            "VALUES (:pid, 'HOTEL', TRUE, NOW()) "
            "ON CONFLICT (partner_id, service_type) DO UPDATE SET is_active = TRUE"
        ),
        {"pid": partner_id},
    )
    await db.execute(
        text(
            "INSERT INTO partner_verification_logs (partner_id, action, remarks, "
            "                                       created_at) "
            "VALUES (:pid, 'ADMIN_SERVICES_UPDATED', :remarks, NOW())"
        ),
        {
            "pid": partner_id,
            "remarks": "HOTEL service enabled from the hotel onboarding screen",
        },
    )
    return success_response(
        "HOTEL service enabled for this partner", {"partner_id": partner_id}
    )


# ============================================================
# LIST & CREATE
# ============================================================


@router.get("", summary="Paginated hotel list with completeness badges")
async def list_hotels(
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    city_id: Optional[int] = Query(None),
    state_id: Optional[int] = Query(None),
    partner_id: Optional[int] = Query(None),
    hotel_category_id: Optional[int] = Query(None),
    star_rating: Optional[int] = Query(None, ge=1, le=7),
    officer_id: Optional[UUID] = Query(None),
    is_featured: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc"),
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    readiness: HotelReadinessService = Depends(get_readiness_service),
) -> dict[str, Any]:
    from app.modules.hotel.repositories import HotelRepository

    roles = current_user.get("roles") or [current_user.get("role", "")]
    # A partner ("My Hotels" in the partner portal) may list only their own
    # properties. Force the partner_id filter to the caller's own partner,
    # overriding any client-supplied value, and ignore officer scoping. No
    # partner profile → return an empty page instead of leaking every hotel.
    if _is_partner_only(roles):
        own_id = await _own_partner_id(db, current_user)
        if own_id is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        partner_id = own_id
        officer_id = None
    # An officer sees their own queue by default, but may still be pointed at a
    # specific officer's list if an admin role is also present.
    elif (
        "VERIFICATION_OFFICER" in roles
        and "ADMIN" not in roles
        and "SUPER_ADMIN" not in roles
    ):
        officer_id = _actor(current_user)

    hotels, total = await HotelRepository(db).list_paginated(
        page=page,
        page_size=page_size,
        search=search,
        status=status_filter,
        city_id=city_id,
        state_id=state_id,
        partner_id=partner_id,
        category_id=hotel_category_id,
        star_rating=star_rating,
        officer_id=officer_id,
        is_featured=is_featured,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )

    reports = await readiness.compute_many(hotels)
    names = await _related_names(db, hotels)

    items = []
    for hotel in hotels:
        hid = int(hotel.id)
        primary = next(
            (i.image_url for i in hotel.images if i.is_primary),
            hotel.images[0].image_url if hotel.images else None,
        )
        items.append(
            {
                "id": hid,
                "uuid": hotel.uuid,
                "hotel_code": hotel.hotel_code,
                "hotel_name": hotel.hotel_name,
                "status": hotel.status,
                "star_rating": hotel.star_rating,
                "city_id": hotel.city_id,
                "city_name": names["cities"].get(int(hotel.city_id)),
                "state_name": (
                    names["states"].get(int(hotel.state_id)) if hotel.state_id else None
                ),
                "partner_id": hotel.partner_id,
                "partner_name": names["partners"].get(int(hotel.partner_id)),
                "hotel_category_id": hotel.hotel_category_id,
                "category_label": hotel.category.label if hotel.category else None,
                "total_rooms": hotel.total_rooms,
                "room_category_count": len(
                    [c for c in hotel.room_categories if c.is_active]
                ),
                "primary_image_url": primary,
                "is_own_risk_approved": hotel.is_own_risk_approved,
                "is_featured": hotel.is_featured,
                "completeness_percent": reports.get(hid, {}).get(
                    "completeness_percent", 0
                ),
                "assigned_officer_name": names["officers"].get(hid),
                "created_at": hotel.created_at,
            }
        )

    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def _related_names(db: AsyncSession, hotels: list[Any]) -> dict[str, dict]:
    """City, state, partner and assigned-officer labels for a page of hotels.

    Batched: these live outside the hotel module, so relationship loading is not
    available and one query per row would make the list N+1.
    """
    empty: dict[str, dict] = {
        "cities": {},
        "states": {},
        "partners": {},
        "officers": {},
    }
    if not hotels:
        return empty

    city_ids = sorted({int(h.city_id) for h in hotels if h.city_id})
    state_ids = sorted({int(h.state_id) for h in hotels if h.state_id})
    partner_ids = sorted({int(h.partner_id) for h in hotels if h.partner_id})
    hotel_ids = sorted({int(h.id) for h in hotels})

    if city_ids:
        rows = await db.execute(
            text("SELECT id, name FROM cities WHERE id = ANY(:ids)"), {"ids": city_ids}
        )
        empty["cities"] = {int(r[0]): r[1] for r in rows.all()}
    if state_ids:
        rows = await db.execute(
            text("SELECT id, name FROM states WHERE id = ANY(:ids)"),
            {"ids": state_ids},
        )
        empty["states"] = {int(r[0]): r[1] for r in rows.all()}
    if partner_ids:
        rows = await db.execute(
            text(
                "SELECT id, COALESCE(NULLIF(business_name, ''), owner_name) "
                "FROM partners WHERE id = ANY(:ids)"
            ),
            {"ids": partner_ids},
        )
        empty["partners"] = {int(r[0]): r[1] for r in rows.all()}

    rows = await db.execute(
        text(
            "SELECT a.hotel_id, "
            "  TRIM(CONCAT(u.first_name, ' ', COALESCE(u.last_name, ''))) AS name "
            "FROM hotel_verification_assignments a "
            "  JOIN users u ON u.id = a.officer_id "
            "WHERE a.hotel_id = ANY(:ids) AND a.is_active = TRUE"
        ),
        {"ids": hotel_ids},
    )
    empty["officers"] = {int(r[0]): r[1] for r in rows.all()}
    return empty


@router.post("", status_code=status.HTTP_201_CREATED, summary="Stage 1 — create hotel")
async def create_hotel(
    payload: HotelCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    hotel = await service.create_hotel(payload, _actor(current_user))
    return success_response(
        f"Hotel {hotel.hotel_code} created as DRAFT",
        {
            "id": int(hotel.id),
            "uuid": hotel.uuid,
            "hotel_code": hotel.hotel_code,
            "hotel_name": hotel.hotel_name,
            "slug": hotel.slug,
            "status": hotel.status,
        },
    )


# ============================================================
# DETAIL
# ============================================================


@router.get(
    "/{hotel_id}", summary="Hotel header — deliberately not the whole aggregate"
)
async def get_hotel(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: HotelService = Depends(get_hotel_service),
    readiness: HotelReadinessService = Depends(get_readiness_service),
) -> dict[str, Any]:
    """The detail dialog opens on this and lazy-loads its tabs. One fat endpoint
    would make checking a status as slow as opening every tab."""
    hotel = await service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    names = await _related_names(db, [hotel])
    report = await readiness.compute(hotel_id)

    return {
        "id": int(hotel.id),
        "uuid": hotel.uuid,
        "hotel_code": hotel.hotel_code,
        "hotel_name": hotel.hotel_name,
        "hotel_type": hotel.hotel_type,
        "hotel_category_id": hotel.hotel_category_id,
        "category_label": hotel.category.label if hotel.category else None,
        "star_rating": hotel.star_rating,
        "status": hotel.status,
        "partner_id": hotel.partner_id,
        "partner_name": names["partners"].get(int(hotel.partner_id)),
        "city_id": hotel.city_id,
        "city_name": names["cities"].get(int(hotel.city_id)),
        "state_id": hotel.state_id,
        "state_name": (
            names["states"].get(int(hotel.state_id)) if hotel.state_id else None
        ),
        "total_rooms": hotel.total_rooms,
        "room_category_count": len([c for c in hotel.room_categories if c.is_active]),
        "image_count": len(hotel.images),
        "document_count": len(hotel.documents),
        "is_own_risk_approved": hotel.is_own_risk_approved,
        "is_featured": hotel.is_featured,
        "rejection_reason": hotel.rejection_reason,
        "submitted_at": hotel.submitted_at,
        "approved_at": hotel.approved_at,
        "activated_at": hotel.activated_at,
        "created_at": hotel.created_at,
        "updated_at": hotel.updated_at,
        "readiness": report,
        "allowed_transitions": VALID_HOTEL_TRANSITIONS.get(str(hotel.status), []),
    }


@router.get(
    "/{hotel_id}/full",
    response_model=HotelDetailResponse,
    summary="Everything about a hotel in one payload",
)
async def get_hotel_full(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    db: AsyncSession = Depends(get_db),
    service: HotelService = Depends(get_hotel_service),
    verification: HotelVerificationService = Depends(get_verification_service),
    readiness: HotelReadinessService = Depends(get_readiness_service),
) -> Any:
    hotel = await service.get_or_404(hotel_id)
    names = await _related_names(db, [hotel])
    commission = await service.get_commission(hotel_id)

    payload = {
        column.name: getattr(hotel, column.name)
        for column in hotel.__table__.columns
        if hasattr(hotel, column.name)
    }
    payload.update(
        {
            "category_label": hotel.category.label if hotel.category else None,
            "partner_name": names["partners"].get(int(hotel.partner_id)),
            "city_name": names["cities"].get(int(hotel.city_id)),
            "state_name": (
                names["states"].get(int(hotel.state_id)) if hotel.state_id else None
            ),
            "platform_gst_enabled": await platform_gst_enabled(db),
            "amenity_ids": sorted(int(m.amenity_id) for m in hotel.amenity_mappings),
            "images": [HotelImageResponse.model_validate(i) for i in hotel.images],
            "documents": [
                HotelDocumentResponse.model_validate(d) for d in hotel.documents
            ],
            "policy": (
                HotelPolicyResponse.model_validate(hotel.policy)
                if hotel.policy
                else None
            ),
            "commission": commission["resolved"],
            "assigned_officer": await verification.get_assignment(hotel_id),
            "readiness": await readiness.compute(hotel_id),
            "allowed_transitions": VALID_HOTEL_TRANSITIONS.get(str(hotel.status), []),
        }
    )
    return payload


@router.get(
    "/{hotel_id}/readiness",
    response_model=ReadinessResponse,
    summary="The submit/approve checklist behind the completeness meter",
)
async def get_readiness(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    readiness: HotelReadinessService = Depends(get_readiness_service),
) -> Any:
    return await readiness.compute(hotel_id)


# ============================================================
# PROFILE, SEO, TAX, AMENITIES
# ============================================================


@router.patch("/{hotel_id}", summary="Partial profile update")
async def update_hotel_profile(
    hotel_id: int,
    payload: HotelProfileUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    hotel = await service.update_profile(hotel_id, payload, _actor(current_user))
    return success_response(
        "Hotel profile updated successfully",
        {"id": int(hotel.id), "status": hotel.status},
    )


@router.patch("/{hotel_id}/seo", summary="Slug, meta tags and featured placement")
async def update_hotel_seo(
    hotel_id: int,
    payload: HotelSeoUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    hotel, warning = await service.update_seo(hotel_id, payload, _actor(current_user))
    return success_response(
        "SEO settings updated successfully",
        {"id": int(hotel.id), "slug": hotel.slug, "warning": warning},
    )


@router.patch("/{hotel_id}/tax", summary="Per-hotel tax mode")
async def update_hotel_tax(
    hotel_id: int,
    payload: HotelTaxUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    hotel, gst_on = await service.update_tax(hotel_id, payload, _actor(current_user))
    # Echo the global switch so the UI can say the setting is inert rather than
    # appearing to have saved something with no effect.
    return success_response(
        "Tax settings updated successfully",
        {
            "id": int(hotel.id),
            "tax_mode": hotel.tax_mode,
            "is_gst_registered": hotel.is_gst_registered,
            "platform_gst_enabled": gst_on,
        },
    )


@router.patch("/{hotel_id}/amenities", summary="Replace the hotel's amenity set")
async def update_hotel_amenities(
    hotel_id: int,
    payload: HotelAmenitiesUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    ids = await service.replace_amenities(
        hotel_id, payload.amenity_ids, _actor(current_user)
    )
    return success_response("Amenities updated successfully", {"amenity_ids": ids})


@router.delete("/{hotel_id}", summary="Soft-delete a hotel")
async def delete_hotel(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    await service.delete_hotel(hotel_id, _actor(current_user))
    return success_response("Hotel deleted successfully")


# ============================================================
# IMAGES
#
# Files are uploaded through the existing POST /admin/settings/upload-media
# with folder_override; these endpoints only persist the returned secure_url.
# A second upload path would mean two places to keep Cloudinary handling right.
# ============================================================


@router.get("/{hotel_id}/images")
async def list_hotel_images(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: HotelService = Depends(get_hotel_service),
) -> list[HotelImageResponse]:
    hotel = await service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    rows = await service.repo.list_images(hotel_id)
    return [HotelImageResponse.model_validate(r) for r in rows]


@router.post("/{hotel_id}/images", status_code=status.HTTP_201_CREATED)
async def add_hotel_image(
    hotel_id: int,
    payload: HotelImageCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    image = await service.add_image(hotel_id, payload, _actor(current_user))
    return success_response(
        "Image added successfully", HotelImageResponse.model_validate(image)
    )


@router.patch("/{hotel_id}/images/reorder")
async def reorder_hotel_images(
    hotel_id: int,
    payload: HotelImagesReorder,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    images = await service.reorder_images(
        hotel_id, payload.ordered_ids, _actor(current_user)
    )
    return success_response(
        "Images reordered successfully",
        [HotelImageResponse.model_validate(i) for i in images],
    )


@router.patch("/{hotel_id}/images/{image_id}/primary")
async def set_primary_hotel_image(
    hotel_id: int,
    image_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    image = await service.set_primary_image(hotel_id, image_id, _actor(current_user))
    return success_response(
        "Primary image set successfully", HotelImageResponse.model_validate(image)
    )


@router.patch("/{hotel_id}/images/{image_id}")
async def update_hotel_image(
    hotel_id: int,
    image_id: int,
    payload: HotelImageUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    image = await service.update_image(
        hotel_id, image_id, payload, _actor(current_user)
    )
    return success_response(
        "Image updated successfully", HotelImageResponse.model_validate(image)
    )


@router.delete("/{hotel_id}/images/{image_id}")
async def delete_hotel_image(
    hotel_id: int,
    image_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    await service.delete_image(hotel_id, image_id, _actor(current_user))
    return success_response("Image deleted successfully")


# ============================================================
# DOCUMENTS
# ============================================================


@router.get("/{hotel_id}/documents")
async def list_hotel_documents(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: HotelService = Depends(get_hotel_service),
) -> list[HotelDocumentResponse]:
    hotel = await service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    rows = await service.repo.list_documents(hotel_id)
    return [HotelDocumentResponse.model_validate(r) for r in rows]


@router.post("/{hotel_id}/documents", status_code=status.HTTP_201_CREATED)
async def add_hotel_document(
    hotel_id: int,
    payload: HotelDocumentCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    document = await service.add_document(hotel_id, payload, _actor(current_user))
    return success_response(
        "Document uploaded successfully",
        HotelDocumentResponse.model_validate(document),
    )


@router.patch("/{hotel_id}/documents/{document_id}/verify")
async def verify_hotel_document(
    hotel_id: int,
    document_id: int,
    payload: HotelDocumentVerify,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    document = await service.verify_document(
        hotel_id, document_id, payload, _actor(current_user)
    )
    return success_response(
        f"Document {str(document.verification_status).lower()}",
        HotelDocumentResponse.model_validate(document),
    )


@router.delete("/{hotel_id}/documents/{document_id}")
async def delete_hotel_document(
    hotel_id: int,
    document_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    await service.delete_document(hotel_id, document_id, _actor(current_user))
    return success_response("Document deleted successfully")


# ============================================================
# POLICIES
# ============================================================


@router.get("/{hotel_id}/policies", response_model=HotelPolicyResponse)
async def get_hotel_policy(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: HotelService = Depends(get_hotel_service),
) -> Any:
    hotel = await service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    return await service.get_policy(hotel_id)


@router.put("/{hotel_id}/policies")
async def update_hotel_policy(
    hotel_id: int,
    payload: HotelPolicyUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    policy = await service.update_policy(hotel_id, payload, _actor(current_user))
    return success_response(
        "Policies updated successfully", HotelPolicyResponse.model_validate(policy)
    )


# ============================================================
# COMMISSION
# ============================================================


@router.get(
    "/{hotel_id}/commission",
    summary="Active override, history, and the resolved effective commission",
)
async def get_hotel_commission(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    """The resolved block names its source, so the UI can distinguish an
    override from a value inherited from a city or global rule."""
    result = await service.get_commission(hotel_id)
    return {
        "override": (
            {
                column.name: getattr(result["override"], column.name)
                for column in result["override"].__table__.columns
            }
            if result["override"] is not None
            else None
        ),
        "history": [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in result["history"]
        ],
        "resolved": result["resolved"],
    }


@router.put("/{hotel_id}/commission", summary="Set a per-hotel commission override")
async def set_hotel_commission(
    hotel_id: int,
    payload: HotelCommissionCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    config = await service.set_commission(hotel_id, payload, _actor(current_user))
    return success_response(
        "Commission updated successfully",
        {"id": int(config.id), "commission_type": config.commission_type},
    )


@router.delete(
    "/{hotel_id}/commission", summary="Drop the override, fall back to platform rules"
)
async def clear_hotel_commission(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    resolved = await service.clear_commission(hotel_id, _actor(current_user))
    return success_response("Commission override removed", resolved)


# ============================================================
# ROOM CATEGORIES
# ============================================================


@router.get("/{hotel_id}/room-categories")
async def list_room_categories(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: RoomCategoryService = Depends(get_category_service),
    hotel_service: HotelService = Depends(get_hotel_service),
) -> list[RoomCategoryResponse]:
    hotel = await hotel_service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    rows = await service.list_categories(hotel_id)
    return [RoomCategoryResponse.model_validate(r) for r in rows]


@router.post("/{hotel_id}/room-categories", status_code=status.HTTP_201_CREATED)
async def create_room_category(
    hotel_id: int,
    payload: RoomCategoryCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    category = await service.create_category(hotel_id, payload, _actor(current_user))
    return success_response(
        "Room category created successfully",
        RoomCategoryResponse.model_validate(category),
    )


@router.patch("/{hotel_id}/room-categories/{category_id}")
async def update_room_category(
    hotel_id: int,
    category_id: int,
    payload: RoomCategoryUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    category = await service.update_category(
        hotel_id, category_id, payload, _actor(current_user)
    )
    return success_response(
        "Room category updated successfully",
        RoomCategoryResponse.model_validate(category),
    )


@router.delete("/{hotel_id}/room-categories/{category_id}")
async def delete_room_category(
    hotel_id: int,
    category_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    await service.delete_category(hotel_id, category_id, _actor(current_user))
    return success_response("Room category deleted successfully")


@router.post(
    "/{hotel_id}/room-categories/{category_id}/images",
    status_code=status.HTTP_201_CREATED,
)
async def add_room_category_image(
    hotel_id: int,
    category_id: int,
    payload: RoomCategoryImageCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    image = await service.add_image(
        hotel_id, category_id, payload, _actor(current_user)
    )
    return success_response(
        "Room image added successfully",
        RoomCategoryImageResponse.model_validate(image),
    )


@router.delete("/{hotel_id}/room-categories/{category_id}/images/{image_id}")
async def delete_room_category_image(
    hotel_id: int,
    category_id: int,
    image_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    await service.delete_image(hotel_id, category_id, image_id, _actor(current_user))
    return success_response("Room image deleted successfully")


# ============================================================
# PHYSICAL ROOMS
# ============================================================


# Declared before /{hotel_id}/rooms so the literal segment "all" is not
# captured as a hotel_id by FastAPI's path matcher.
@router.get("/rooms/all")
async def list_all_rooms(
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: RoomCategoryService = Depends(get_category_service),
) -> list[RoomResponse]:
    """Partner-scoped list of every physical room across hotels the caller owns.

    Partner-only callers receive only rooms from hotels whose ``partner_id``
    matches the caller's own partner row. Admins and verification officers
    receive every room. Each row carries the ``active_reservation`` payload
    (CHECKED_IN/IN_HOUSE) so the partner portal can render the lock state
    without an N+1 of allocation lookups.
    """
    from app.modules.hotel.repositories import RoomRepository

    roles = current_user.get("roles") or [current_user.get("role", "")]
    if _is_partner_only(roles):
        own_id = await _own_partner_id(db, current_user)
        if own_id is None:
            return []
        rows = await RoomRepository(db).list_for_partner(own_id)
    else:
        from sqlalchemy import select as _select

        from app.modules.hotel.models import HotelRoom as _HR

        rows = list(
            (
                await db.execute(
                    _select(_HR).order_by(
                        _HR.hotel_id, _HR.room_category_id, _HR.room_number
                    )
                )
            )
            .scalars()
            .all()
        )

    # Group allocation maps by hotel so a single round-trip populates every
    # row's active_reservation. active_allocation_map joins reservations to
    # checkins; running it once per hotel keeps the cost O(hotels) not O(rooms).
    hotel_ids = sorted({int(r.hotel_id) for r in rows})
    allocs_by_hotel: dict[int, dict[str, dict[str, Any]]] = {}
    for hid in hotel_ids:
        allocs_by_hotel[hid] = await service.room_allocation_map(hid)

    result: list[RoomResponse] = []
    for r in rows:
        resp = RoomResponse.model_validate(r)
        info = allocs_by_hotel.get(int(r.hotel_id), {}).get(
            str(r.room_number).strip().upper()
        )
        if info:
            resp.active_reservation = RoomActiveReservation(**info)
        result.append(resp)
    return result


@router.get("/{hotel_id}/rooms")
async def list_rooms(
    hotel_id: int,
    room_category_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: RoomCategoryService = Depends(get_category_service),
    hotel_service: HotelService = Depends(get_hotel_service),
) -> list[RoomResponse]:
    hotel = await hotel_service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    rows = await service.list_rooms(hotel_id, room_category_id)
    alloc = await service.room_allocation_map(hotel_id)
    result: list[RoomResponse] = []
    for r in rows:
        resp = RoomResponse.model_validate(r)
        info = alloc.get(str(r.room_number).strip().upper())
        if info:
            resp.active_reservation = RoomActiveReservation(**info)
        result.append(resp)
    return result


@router.post("/{hotel_id}/rooms", status_code=status.HTTP_201_CREATED)
async def create_room(
    hotel_id: int,
    payload: RoomCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    room = await service.create_room(hotel_id, payload, _actor(current_user))
    return success_response(
        "Room added successfully", RoomResponse.model_validate(room)
    )


@router.post("/{hotel_id}/rooms/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_create_rooms(
    hotel_id: int,
    payload: BulkRoomCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    rooms = await service.bulk_create_rooms(hotel_id, payload, _actor(current_user))
    return success_response(
        f"{len(rooms)} room(s) generated successfully",
        [RoomResponse.model_validate(r) for r in rooms],
    )


@router.patch("/{hotel_id}/rooms/{room_id}/status")
async def update_room_status(
    hotel_id: int,
    room_id: int,
    payload: RoomUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_OFFICER_OR_PARTNER)),
    db: AsyncSession = Depends(get_db),
    service: RoomCategoryService = Depends(get_category_service),
    hotel_service: HotelService = Depends(get_hotel_service),
) -> dict[str, Any]:
    """Partner-accessible: update only room_status and remarks on their own hotel."""
    hotel = await hotel_service.get_or_404(hotel_id)
    await _assert_partner_owns(db, current_user, hotel)
    roles = current_user.get("roles") or [current_user.get("role", "")]
    if _is_partner_only(roles):
        # Partners may only change status and remarks — strip everything else
        payload = RoomUpdate(room_status=payload.room_status, remarks=payload.remarks)
    room = await service.update_room(hotel_id, room_id, payload, _actor(current_user))
    return success_response(
        "Room status updated successfully", RoomResponse.model_validate(room)
    )


@router.patch("/{hotel_id}/rooms/{room_id}")
async def update_room(
    hotel_id: int,
    room_id: int,
    payload: RoomUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    room = await service.update_room(hotel_id, room_id, payload, _actor(current_user))
    return success_response(
        "Room updated successfully", RoomResponse.model_validate(room)
    )


@router.delete("/{hotel_id}/rooms/{room_id}")
async def delete_room(
    hotel_id: int,
    room_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RoomCategoryService = Depends(get_category_service),
) -> dict[str, Any]:
    await service.delete_room(hotel_id, room_id, _actor(current_user))
    return success_response("Room deleted successfully")


# ============================================================
# RATE PLANS
# ============================================================


@router.get("/{hotel_id}/rate-plans")
async def list_rate_plans(
    hotel_id: int,
    room_category_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RatePlanService = Depends(get_rate_plan_service),
) -> list[RatePlanResponse]:
    rows = await service.list_plans(hotel_id, room_category_id)
    return [RatePlanResponse.model_validate(r) for r in rows]


@router.post("/{hotel_id}/rate-plans", status_code=status.HTTP_201_CREATED)
async def create_rate_plan(
    hotel_id: int,
    payload: RatePlanCreate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RatePlanService = Depends(get_rate_plan_service),
) -> dict[str, Any]:
    plan = await service.create_plan(hotel_id, payload, _actor(current_user))
    return success_response(
        "Rate plan created successfully", RatePlanResponse.model_validate(plan)
    )


@router.patch("/{hotel_id}/rate-plans/{plan_id}")
async def update_rate_plan(
    hotel_id: int,
    plan_id: int,
    payload: RatePlanUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RatePlanService = Depends(get_rate_plan_service),
) -> dict[str, Any]:
    plan = await service.update_plan(hotel_id, plan_id, payload, _actor(current_user))
    return success_response(
        "Rate plan updated successfully", RatePlanResponse.model_validate(plan)
    )


@router.delete("/{hotel_id}/rate-plans/{plan_id}")
async def delete_rate_plan(
    hotel_id: int,
    plan_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RatePlanService = Depends(get_rate_plan_service),
) -> dict[str, Any]:
    await service.delete_plan(hotel_id, plan_id, _actor(current_user))
    return success_response("Rate plan deleted successfully")


@router.get(
    "/{hotel_id}/room-categories/{category_id}/rate-preview",
    summary="Nightly rates with the winning plan named per date",
)
async def rate_preview(
    hotel_id: int,
    category_id: int,
    date_from: date = Query(...),
    date_to: date = Query(...),
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: RatePlanService = Depends(get_rate_plan_service),
) -> dict[str, Any]:
    """Naming the winning plan is the point: overlapping plans resolved by
    precedence are otherwise impossible to reason about from the config alone."""
    return await service.preview(hotel_id, category_id, date_from, date_to)


# ============================================================
# INVENTORY
# ============================================================


@router.get("/{hotel_id}/inventory")
async def list_inventory(
    hotel_id: int,
    date_from: date = Query(...),
    date_to: date = Query(...),
    room_category_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: InventoryService = Depends(get_inventory_service),
) -> list[InventoryResponse]:
    rows = await service.list_inventory(hotel_id, date_from, date_to, room_category_id)
    return [InventoryResponse.model_validate(r) for r in rows]


@router.post("/{hotel_id}/inventory/generate")
async def generate_inventory(
    hotel_id: int,
    payload: InventoryGenerate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: InventoryService = Depends(get_inventory_service),
) -> dict[str, Any]:
    result = await service.generate(hotel_id, payload, _actor(current_user))
    return success_response("Inventory generated successfully", result)


@router.patch("/{hotel_id}/inventory/bulk")
async def bulk_update_inventory(
    hotel_id: int,
    payload: InventoryUpdate,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: InventoryService = Depends(get_inventory_service),
) -> dict[str, Any]:
    result = await service.bulk_update(
        hotel_id, payload, _actor(current_user), days_of_week=payload.days_of_week
    )
    return success_response("Inventory updated successfully", result)


@router.delete("/{hotel_id}/inventory/rate-override")
async def clear_inventory_rate_override(
    hotel_id: int,
    room_category_id: int = Query(...),
    date_from: date = Query(...),
    date_to: date = Query(...),
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: InventoryService = Depends(get_inventory_service),
) -> dict[str, Any]:
    result = await service.clear_rate_override(
        hotel_id, room_category_id, date_from, date_to, _actor(current_user)
    )
    return success_response("Rate overrides cleared successfully", result)


# ============================================================
# VERIFICATION & LIFECYCLE
# ============================================================


def _status_payload(hotel: Any) -> dict[str, Any]:
    return {
        "id": int(hotel.id),
        "status": hotel.status,
        "allowed_transitions": VALID_HOTEL_TRANSITIONS.get(str(hotel.status), []),
    }


@router.post("/{hotel_id}/submit", summary="DRAFT → PENDING")
async def submit_hotel(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.submit(hotel_id, _actor(current_user))
    return success_response("Hotel submitted for verification", _status_payload(hotel))


@router.post("/{hotel_id}/assign-officer")
async def assign_officer(
    hotel_id: int,
    payload: AssignOfficerRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    assignment = await service.assign_officer(
        hotel_id, payload.officer_id, _actor(current_user), payload.notes
    )
    return success_response(
        "Verification officer assigned successfully",
        {"assignment_id": int(assignment.id), "officer_id": assignment.officer_id},
    )


@router.post("/{hotel_id}/unassign-officer")
async def unassign_officer(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    await service.unassign_officer(hotel_id, _actor(current_user))
    return success_response("Verification officer unassigned successfully")


@router.get("/{hotel_id}/assignment")
async def get_assignment(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> Optional[dict[str, Any]]:
    return await service.get_assignment(hotel_id)


@router.patch("/{hotel_id}/review", summary="→ UNDER_REVIEW")
async def start_review(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.start_review(hotel_id, _actor(current_user))
    return success_response("Review started", _status_payload(hotel))


@router.patch("/{hotel_id}/document-pending", summary="→ DOCUMENT_PENDING")
async def request_documents(
    hotel_id: int,
    payload: StatusChangeRequest,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.request_documents(
        hotel_id, payload.remarks, _actor(current_user)
    )
    return success_response("Documents requested from partner", _status_payload(hotel))


@router.patch("/{hotel_id}/approve", summary="→ APPROVED")
async def approve_hotel(
    hotel_id: int,
    payload: ApproveHotelRequest,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    """own_risk skips the document check and is therefore admin-only — an
    officer must not be able to waive the control they exist to apply."""
    if payload.own_risk:
        roles = current_user.get("roles") or [current_user.get("role", "")]
        if not any(role in ADMIN_ROLES for role in roles):
            from app.core.exceptions import PermissionDeniedException

            raise PermissionDeniedException(
                "Own-risk approval is restricted to administrators."
            )
        hotel = await service.approve_own_risk(
            hotel_id, _actor(current_user), payload.remarks
        )
        return success_response(
            "Hotel approved at admin's own risk", _status_payload(hotel)
        )

    hotel = await service.approve(hotel_id, _actor(current_user), payload.remarks)
    return success_response("Hotel approved successfully", _status_payload(hotel))


@router.patch("/{hotel_id}/reject", summary="→ REJECTED")
async def reject_hotel(
    hotel_id: int,
    payload: RejectHotelRequest,
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.reject(hotel_id, payload.reason, _actor(current_user))
    return success_response("Hotel rejected", _status_payload(hotel))


@router.patch("/{hotel_id}/activate", summary="APPROVED → ACTIVE")
async def activate_hotel(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.activate(hotel_id, _actor(current_user))
    return success_response("Hotel is now live", _status_payload(hotel))


@router.patch("/{hotel_id}/deactivate", summary="→ INACTIVE")
async def deactivate_hotel(
    hotel_id: int,
    payload: StatusChangeRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.deactivate(hotel_id, _actor(current_user), payload.remarks)
    return success_response("Hotel deactivated", _status_payload(hotel))


@router.patch("/{hotel_id}/suspend", summary="→ SUSPENDED")
async def suspend_hotel(
    hotel_id: int,
    payload: StatusChangeRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.suspend(hotel_id, _actor(current_user), payload.remarks)
    return success_response("Hotel suspended", _status_payload(hotel))


@router.patch("/{hotel_id}/unsuspend", summary="SUSPENDED → ACTIVE")
async def unsuspend_hotel(
    hotel_id: int,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.activate(hotel_id, _actor(current_user))
    return success_response("Hotel reinstated", _status_payload(hotel))


@router.patch("/{hotel_id}/block", summary="→ BLOCKED (terminal)")
async def block_hotel(
    hotel_id: int,
    payload: StatusChangeRequest,
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> dict[str, Any]:
    hotel = await service.block(hotel_id, _actor(current_user), payload.remarks)
    return success_response("Hotel blocked", _status_payload(hotel))


@router.get("/{hotel_id}/logs", summary="Audit trail")
async def list_hotel_logs(
    hotel_id: int,
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(require_roles(*ADMIN_OR_OFFICER)),
    service: HotelVerificationService = Depends(get_verification_service),
) -> list[dict[str, Any]]:
    return await service.list_logs(hotel_id, limit)


# ============================================================
# COMMISSION AUDIT — platform-wide view of every hotel's
# resolved commission. Lets finance spot hotels that have
# drifted away from the city / global rule.
# ============================================================


@router.get(
    "/commission-audit",
    summary="Every hotel with its resolved commission source",
)
async def commission_audit(
    source_filter: Optional[str] = Query(
        None, description="HOTEL_OVERRIDE | CITY_RULE | GLOBAL_RULE | SYSTEM_DEFAULT"
    ),
    city_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_roles(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One row per hotel with the active commission override (if any),
    the city/global/system-default fallback, and the source the resolver
    would actually use at booking time. Source filter is the finance
    auditor's primary lever: 'show me every hotel that's not on the
    platform default'.
    """
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    where = ["h.deleted_at IS NULL"]
    if city_id is not None:
        where.append("h.city_id = :city_id")
        params["city_id"] = city_id
    if search and search.strip():
        where.append(
            "(h.hotel_name ILIKE :q OR h.hotel_code ILIKE :q "
            "OR p.business_name ILIKE :q OR p.partner_code ILIKE :q)"
        )
        params["q"] = f"%{search.strip()}%"
    where_clause = " AND ".join(where)

    # Get the page first, then resolve commission per hotel (the resolver
    # is per-hotel logic; calling it N times in a loop is the only way to
    # give a faithful "source" column without re-implementing it in SQL).
    list_sql = text(
        f"""
        SELECT h.id, h.uuid, h.hotel_code, h.hotel_name, h.status,
               h.city_id, h.partner_id,
               p.partner_code, COALESCE(NULLIF(p.business_name,''), p.owner_name) AS partner_name,
               c.name AS city_name
        FROM hotels h
        LEFT JOIN partners p ON p.id = h.partner_id
        LEFT JOIN cities   c ON c.id = h.city_id
        WHERE {where_clause}
        ORDER BY h.hotel_name ASC
        LIMIT :limit OFFSET :offset
        """
    )
    hotel_rows = (await db.execute(list_sql, params)).mappings().all()

    total_sql = text(
        f"SELECT COUNT(*) FROM hotels h LEFT JOIN partners p ON p.id = h.partner_id WHERE {where_clause}"
    )
    total = (await db.execute(total_sql, params)).scalar() or 0

    items: list[dict[str, Any]] = []
    hotel_service = HotelService(db)
    for r in hotel_rows:
        hid = int(r["id"])
        try:
            resolved = await hotel_service.get_commission(hid)
            source = (resolved.get("resolved") or {}).get("source")
        except Exception:
            source = None
        if source_filter and source != source_filter:
            continue
        override = resolved.get("override") if "resolved" in resolved else None
        items.append(
            {
                "hotel_id": hid,
                "hotel_code": r["hotel_code"],
                "hotel_name": r["hotel_name"],
                "status": r["status"],
                "city_id": int(r["city_id"]) if r["city_id"] is not None else None,
                "city_name": r["city_name"],
                "partner_id": (
                    int(r["partner_id"]) if r["partner_id"] is not None else None
                ),
                "partner_name": r["partner_name"],
                "partner_code": r["partner_code"],
                "has_override": override is not None,
                "override_commission_type": (
                    override.commission_type if override is not None else None
                ),
                "override_commission_percent": (
                    float(override.commission_percent)
                    if override is not None and override.commission_percent is not None
                    else None
                ),
                "override_commission_flat": (
                    float(override.commission_flat)
                    if override is not None and override.commission_flat is not None
                    else None
                ),
                "resolved_source": source,
            }
        )

    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


__all__ = ["router"]
