# ============================================================
# WAYTERO — ADMIN SETTINGS API ROUTER (ASYNC)
# File: app/modules/admin/api.py
# Doc Ref:
#   Backend Architecture §2 FastAPI Structure, §3 Module Structure
#   Frontend Architecture §31 System Configuration
#   DB Schema Part 1 §12-14 | Part 2 §14-15 | Part 3 §18 | Part 8 §11
# Prefix: /admin/settings  (registered in api/router.py)
# All routes are async — uses AsyncSession (project standard)
# ============================================================

import hashlib
import time
from typing import List, Optional
import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_roles
from app.modules.admin.models import ApiIntegration
from app.modules.admin.schemas import (
    SystemConfigOut,
    SystemConfigUpdate,
    AppVersionOut,
    AppVersionCreate,
    AppVersionUpdate,
    CommissionGroupOut,
    CommissionGroupCreate,
    CommissionGroupUpdate,
    CommissionRuleOut,
    CommissionRuleCreate,
    CommissionRuleUpdate,
    VehiclePricingRuleOut,
    VehiclePricingRuleCreate,
    VehiclePricingRuleUpdate,
    NotificationTemplateOut,
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
    ApiIntegrationOut,
    ApiIntegrationCreate,
    ApiIntegrationUpdate,
    SettingsResponse,
    CityOut,
    CityCreate,
    CityUpdate,
    StateOut,
    VehicleCategoryOut,
    VehicleCategoryCreate,
    VehicleCategoryUpdate,
    ServiceTypeOut,
    ServiceTypeUpdate,
    ServiceTypeCreate,
    DefaultPricingRuleOut,
    DefaultPricingRuleUpdate,
    EffectivePricingRuleOut,
)
from app.modules.admin.services import (
    SystemConfigService,
    AppVersionService,
    CommissionService,
    PricingService,
    NotificationTemplateService,
    ApiIntegrationService,
    CityService,
    VehicleCategoryService,
    ServiceTypeService,
    CmsService,
    DefaultPricingService,
)
from app.core.dependencies import require_permission

from app.modules.admin.schemas import (
    PageSectionOut,
    PageSectionUpdate,
    SectionVariantOut,
    SectionVariantCreate,
    SectionVariantUpdate,
    SiteHeaderOut,
    SiteHeaderUpdate,
    SiteFooterOut,
    SiteFooterUpdate,
)

router = APIRouter()


# ════════════════════════════════════════════════════════════════
#  SYSTEM CONFIGURATIONS
#  Doc Ref: DB Schema Part 1 §12 — system_configurations
# ════════════════════════════════════════════════════════════════


@router.get(
    "/configurations",
    response_model=List[SystemConfigOut],
    tags=["Settings – System Config"],
)
async def list_configurations(db: AsyncSession = Depends(get_db)):
    """Return all platform configuration key-value pairs."""
    return await SystemConfigService.list_all(db)


@router.get(
    "/configurations/{key}",
    response_model=SystemConfigOut,
    tags=["Settings – System Config"],
)
async def get_configuration(key: str, db: AsyncSession = Depends(get_db)):
    cfg = await SystemConfigService.get_by_key(db, key)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Config key '{key}' not found")
    return cfg


@router.put(
    "/configurations/{key}",
    response_model=SystemConfigOut,
    tags=["Settings – System Config"],
)
async def upsert_configuration(
    key: str, payload: SystemConfigUpdate, db: AsyncSession = Depends(get_db)
):
    """Create or update a configuration key."""
    return await SystemConfigService.upsert(db, key, payload)


@router.delete(
    "/configurations/{key}",
    response_model=SettingsResponse,
    tags=["Settings – System Config"],
)
async def delete_configuration(key: str, db: AsyncSession = Depends(get_db)):
    deleted = await SystemConfigService.delete(db, key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Config key '{key}' not found")
    return SettingsResponse(message=f"Config key '{key}' deleted")


# ════════════════════════════════════════════════════════════════
#  API INTEGRATIONS
#  Doc Ref: DB Schema Part 1 §14 — api_integrations
#  service_type: CLOUDINARY | FIREBASE | GOOGLE_MAPS | RAZORPAY | MSG91 | WHATSAPP | FCM | MINIO | SMTP
# ════════════════════════════════════════════════════════════════

# Roles allowed to READ integration configs (other staff pages — CabBooking,
# AddHotelWizard — read the GOOGLE_MAPS key presence from here).
INTEGRATION_READ_ROLES = (
    "SUPER_ADMIN",
    "ADMIN",
    "CCO",
    "FINANCE_MANAGER",
    "VERIFICATION_OFFICER",
)

# Credential fields that must never be returned to non-SUPER_ADMIN callers.
INTEGRATION_SECRET_KEYS = (
    "api_secret",
    "secret_key",
    "private_key",
    "auth_key",
    "password",
    "key_secret",
    "secret",
    "access_key",
    "token",
)


def _mask_configuration(
    configuration: Optional[dict], is_super_admin: bool
) -> Optional[dict]:
    """Mask credential values in an integration configuration (S5).

    SUPER_ADMIN sees full values (the admin Settings page round-trips the
    configuration object on save). Everyone else gets masked placeholders —
    which stay truthy, so UI code checking "is the key configured?" still works.
    """
    if configuration is None or is_super_admin:
        return configuration
    masked = dict(configuration)
    for key in list(masked.keys()):
        lower = key.lower()
        if any(secret in lower for secret in INTEGRATION_SECRET_KEYS):
            masked[key] = "••••••••"
    return masked


def _mask_integrations(integrations: list, current_user: dict) -> list:
    is_super_admin = (
        current_user.get("user_type") == "SUPER_ADMIN"
        or "SUPER_ADMIN" in (current_user.get("roles") or [])
        or current_user.get("role") == "SUPER_ADMIN"
    )
    out = []
    for integration in integrations:
        if hasattr(integration, "configuration"):
            integration.configuration = _mask_configuration(
                integration.configuration, is_super_admin
            )
        out.append(integration)
    return out


@router.get(
    "/api-integrations",
    response_model=List[ApiIntegrationOut],
    tags=["Settings – API Integrations"],
)
async def list_api_integrations(
    current_user: dict = Depends(require_roles(*INTEGRATION_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Return all third-party API integration configurations.

    Credential values (api_secret, private_key, auth_key, etc.) are masked
    for everyone except SUPER_ADMIN (Doc Ref: Security Hardening — S5).
    """
    integrations = await ApiIntegrationService.list_all(db)
    return _mask_integrations(integrations, current_user)


@router.get(
    "/api-integrations/{integration_id}",
    response_model=ApiIntegrationOut,
    tags=["Settings – API Integrations"],
)
async def get_api_integration(
    integration_id: int,
    current_user: dict = Depends(require_roles(*INTEGRATION_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    ai = await ApiIntegrationService.get_by_id(db, integration_id)
    if not ai:
        raise HTTPException(status_code=404, detail="API integration not found")
    masked = _mask_integrations([ai], current_user)
    return masked[0]


@router.post(
    "/api-integrations",
    response_model=ApiIntegrationOut,
    status_code=201,
    tags=["Settings – API Integrations"],
)
async def create_api_integration(
    payload: ApiIntegrationCreate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    return await ApiIntegrationService.create(db, payload)


@router.patch(
    "/api-integrations/{integration_id}",
    response_model=ApiIntegrationOut,
    tags=["Settings – API Integrations"],
)
async def update_api_integration(
    integration_id: int,
    payload: ApiIntegrationUpdate,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    ai = await ApiIntegrationService.update(db, integration_id, payload)
    if not ai:
        raise HTTPException(status_code=404, detail="API integration not found")
    return ai


@router.delete(
    "/api-integrations/{integration_id}",
    response_model=SettingsResponse,
    tags=["Settings – API Integrations"],
)
async def delete_api_integration(
    integration_id: int,
    current_user: dict = Depends(require_roles("SUPER_ADMIN", "ADMIN")),
    db: AsyncSession = Depends(get_db),
):
    deleted = await ApiIntegrationService.delete(db, integration_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="API integration not found")
    return SettingsResponse(message="API integration deleted")


# ════════════════════════════════════════════════════════════════
#  APP VERSIONS
#  Doc Ref: DB Schema Part 1 §13 — app_versions
# ════════════════════════════════════════════════════════════════


@router.get(
    "/app-versions",
    response_model=List[AppVersionOut],
    tags=["Settings – App Versions"],
)
async def list_app_versions(db: AsyncSession = Depends(get_db)):
    return await AppVersionService.list_all(db)


@router.post(
    "/app-versions",
    response_model=AppVersionOut,
    status_code=201,
    tags=["Settings – App Versions"],
)
async def create_app_version(
    payload: AppVersionCreate, db: AsyncSession = Depends(get_db)
):
    return await AppVersionService.create(db, payload)


@router.patch(
    "/app-versions/{version_id}",
    response_model=AppVersionOut,
    tags=["Settings – App Versions"],
)
async def update_app_version(
    version_id: int, payload: AppVersionUpdate, db: AsyncSession = Depends(get_db)
):
    av = await AppVersionService.update(db, version_id, payload)
    if not av:
        raise HTTPException(status_code=404, detail="App version not found")
    return av


@router.delete(
    "/app-versions/{version_id}",
    response_model=SettingsResponse,
    tags=["Settings – App Versions"],
)
async def delete_app_version(version_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await AppVersionService.delete(db, version_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="App version not found")
    return SettingsResponse(message="App version deleted")


# ════════════════════════════════════════════════════════════════
#  COMMISSION GROUPS & RULES
#  Doc Ref: DB Schema Part 2 §14-15 — commission_groups, commission_rules
# ════════════════════════════════════════════════════════════════


@router.get(
    "/commission-groups",
    response_model=List[CommissionGroupOut],
    tags=["Settings – Commission"],
)
async def list_commission_groups(db: AsyncSession = Depends(get_db)):
    return await CommissionService.list_groups(db)


@router.get(
    "/commission-groups/{group_id}",
    response_model=CommissionGroupOut,
    tags=["Settings – Commission"],
)
async def get_commission_group(group_id: int, db: AsyncSession = Depends(get_db)):
    grp = await CommissionService.get_group(db, group_id)
    if not grp:
        raise HTTPException(status_code=404, detail="Commission group not found")
    return grp


@router.post(
    "/commission-groups",
    response_model=CommissionGroupOut,
    status_code=201,
    tags=["Settings – Commission"],
)
async def create_commission_group(
    payload: CommissionGroupCreate, db: AsyncSession = Depends(get_db)
):
    return await CommissionService.create_group(db, payload)


@router.patch(
    "/commission-groups/{group_id}",
    response_model=CommissionGroupOut,
    tags=["Settings – Commission"],
)
async def update_commission_group(
    group_id: int, payload: CommissionGroupUpdate, db: AsyncSession = Depends(get_db)
):
    grp = await CommissionService.update_group(db, group_id, payload)
    if not grp:
        raise HTTPException(status_code=404, detail="Commission group not found")
    return grp


@router.delete(
    "/commission-groups/{group_id}",
    response_model=SettingsResponse,
    tags=["Settings – Commission"],
)
async def delete_commission_group(group_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await CommissionService.delete_group(db, group_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Commission group not found")
    return SettingsResponse(message="Commission group deleted")


@router.post(
    "/commission-groups/{group_id}/rules",
    response_model=CommissionRuleOut,
    status_code=201,
    tags=["Settings – Commission"],
)
async def add_commission_rule(
    group_id: int, payload: CommissionRuleCreate, db: AsyncSession = Depends(get_db)
):
    rule = await CommissionService.add_rule(db, group_id, payload)
    if not rule:
        raise HTTPException(status_code=404, detail="Commission group not found")
    return rule


@router.patch(
    "/commission-rules/{rule_id}",
    response_model=CommissionRuleOut,
    tags=["Settings – Commission"],
)
async def update_commission_rule(
    rule_id: int, payload: CommissionRuleUpdate, db: AsyncSession = Depends(get_db)
):
    rule = await CommissionService.update_rule(db, rule_id, payload)
    if not rule:
        raise HTTPException(status_code=404, detail="Commission rule not found")
    return rule


@router.delete(
    "/commission-rules/{rule_id}",
    response_model=SettingsResponse,
    tags=["Settings – Commission"],
)
async def delete_commission_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await CommissionService.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Commission rule not found")
    return SettingsResponse(message="Commission rule deleted")


# ════════════════════════════════════════════════════════════════
#  VEHICLE PRICING RULES
#  Doc Ref: DB Schema Part 3 §18 — vehicle_pricing_rules
# ════════════════════════════════════════════════════════════════


@router.get(
    "/pricing-rules",
    response_model=List[VehiclePricingRuleOut],
    tags=["Settings – Pricing"],
)
async def list_pricing_rules(
    city_id: Optional[int] = Query(None),
    vehicle_category_id: Optional[int] = Query(None),
    trip_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    return await PricingService.list_rules(db, city_id, vehicle_category_id, trip_type)


@router.post(
    "/pricing-rules",
    response_model=VehiclePricingRuleOut,
    status_code=201,
    tags=["Settings – Pricing"],
)
async def create_pricing_rule(
    payload: VehiclePricingRuleCreate, db: AsyncSession = Depends(get_db)
):
    return await PricingService.create_rule(db, payload)


@router.patch(
    "/pricing-rules/{rule_id}",
    response_model=VehiclePricingRuleOut,
    tags=["Settings – Pricing"],
)
async def update_pricing_rule(
    rule_id: int, payload: VehiclePricingRuleUpdate, db: AsyncSession = Depends(get_db)
):
    rule = await PricingService.update_rule(db, rule_id, payload)
    if not rule:
        raise HTTPException(status_code=404, detail="Pricing rule not found")
    return rule


@router.delete(
    "/pricing-rules/{rule_id}",
    response_model=SettingsResponse,
    tags=["Settings – Pricing"],
)
async def delete_pricing_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await PricingService.delete_rule(db, rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Pricing rule not found")
    return SettingsResponse(message="Pricing rule deleted")


# ════════════════════════════════════════════════════════════════
#  NOTIFICATION TEMPLATES
#  Doc Ref: DB Schema Part 8 §11 — notification_templates
# ════════════════════════════════════════════════════════════════


@router.get(
    "/notification-templates",
    response_model=List[NotificationTemplateOut],
    tags=["Settings – Notification Templates"],
)
async def list_notification_templates(
    channel: Optional[str] = Query(
        None, description="Filter by channel: SMS|EMAIL|PUSH|IN_APP"
    ),
    db: AsyncSession = Depends(get_db),
):
    return await NotificationTemplateService.list_all(db, channel)


@router.get(
    "/notification-templates/{template_id}",
    response_model=NotificationTemplateOut,
    tags=["Settings – Notification Templates"],
)
async def get_notification_template(
    template_id: int, db: AsyncSession = Depends(get_db)
):
    tmpl = await NotificationTemplateService.get_by_id(db, template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Notification template not found")
    return tmpl


@router.post(
    "/notification-templates",
    response_model=NotificationTemplateOut,
    status_code=201,
    tags=["Settings – Notification Templates"],
)
async def create_notification_template(
    payload: NotificationTemplateCreate, db: AsyncSession = Depends(get_db)
):
    return await NotificationTemplateService.create(db, payload)


@router.patch(
    "/notification-templates/{template_id}",
    response_model=NotificationTemplateOut,
    tags=["Settings – Notification Templates"],
)
async def update_notification_template(
    template_id: int,
    payload: NotificationTemplateUpdate,
    db: AsyncSession = Depends(get_db),
):
    tmpl = await NotificationTemplateService.update(db, template_id, payload)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Notification template not found")
    return tmpl


@router.delete(
    "/notification-templates/{template_id}",
    response_model=SettingsResponse,
    tags=["Settings – Notification Templates"],
)
async def delete_notification_template(
    template_id: int, db: AsyncSession = Depends(get_db)
):
    deleted = await NotificationTemplateService.delete(db, template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Notification template not found")
    return SettingsResponse(message="Notification template deleted")


# ── City Management ───────────────────────────────────────────────────────────
#  Doc Ref: Admin API §17 | DB Schema Part 1 §11
#  cities table seeded in 0003_phase2_master_data


@router.get("/master/states", response_model=List[StateOut], tags=["Master Data"])
async def list_states(db: AsyncSession = Depends(get_db)):
    """List all states — used to populate city create form."""
    return await CityService.list_states(db)


@router.get("/master/cities", response_model=List[CityOut], tags=["Master Data"])
async def list_cities(
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """List all cities with state name. Used in commission rules & pricing rules city pickers."""
    return await CityService.list_all(db, active_only=active_only)


@router.post(
    "/master/cities", response_model=CityOut, status_code=201, tags=["Master Data"]
)
async def create_city(payload: CityCreate, db: AsyncSession = Depends(get_db)):
    return await CityService.create(db, payload)


@router.patch("/master/cities/{city_id}", response_model=CityOut, tags=["Master Data"])
async def update_city(
    city_id: int, payload: CityUpdate, db: AsyncSession = Depends(get_db)
):
    city = await CityService.update(db, city_id, payload)
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    return city


@router.delete(
    "/master/cities/{city_id}", response_model=SettingsResponse, tags=["Master Data"]
)
async def delete_city(city_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await CityService.delete(db, city_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="City not found")
    return SettingsResponse(message="City deleted")


# ── Geocode helper for the City editor ───────────────────────────────────────
# Doc Ref: customer-web filters Google Places predictions to active cities.
# When admin adds a new city we don't yet have lat/lng, so we hit Google's
# Geocoding API using the admin-configured key from api_integrations.
# Cost: $5/1000 calls, charged to the configured project.


class GeocodeOut(BaseModel):
    latitude: float
    longitude: float
    formatted_address: str


async def _get_active_google_maps_key(db: AsyncSession) -> Optional[str]:
    """Read the GOOGLE_MAPS api_integration row. Returns None when not configured."""
    try:
        row = (
            (
                await db.execute(
                    sa.text(
                        """
                SELECT configuration, is_active
                FROM api_integrations
                WHERE service_type = 'GOOGLE_MAPS'
                LIMIT 1
            """
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    except Exception:
        return None
    if not row or not row.get("is_active"):
        return None
    cfg = row.get("configuration") or {}
    if not isinstance(cfg, dict):
        return None
    return (cfg.get("api_key") or "").strip() or None


@router.post(
    "/master/cities/geocode",
    response_model=GeocodeOut,
    tags=["Master Data"],
    summary="Geocode a city name to lat/lng via the admin-configured Google Maps key",
)
async def geocode_city(payload: dict, db: AsyncSession = Depends(get_db)):
    """Used by the City editor's "Find coordinates" button.

    Body: ``{"name": "...", "state_name": "..."}`` (state_name is optional,
    used to disambiguate when two cities share a name — e.g. multiple
    Bhubaneswar towns exist across states).
    """
    import httpx

    name = (payload or {}).get("name", "").strip()
    state_name = (payload or {}).get("state_name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    api_key = await _get_active_google_maps_key(db)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Maps API key not configured. "
                "Add one in Settings → API Integrations → GOOGLE_MAPS."
            ),
        )

    address = f"{name}, {state_name}, India" if state_name else f"{name}, India"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": address, "key": api_key, "components": "country:IN"},
            )
            data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Geocoding request failed: {e}")

    results = (data or {}).get("results") or []
    if not results or data.get("status") not in ("OK",):
        err = (data or {}).get("status") or "UNKNOWN"
        raise HTTPException(
            status_code=404,
            detail=f"No geocoding result for '{address}' (Google status: {err})",
        )

    loc = results[0].get("geometry", {}).get("location") or {}
    lat = loc.get("lat")
    lng = loc.get("lng")
    if lat is None or lng is None:
        raise HTTPException(status_code=404, detail="Geocoder returned no location")

    return GeocodeOut(
        latitude=float(lat),
        longitude=float(lng),
        formatted_address=results[0].get("formatted_address", address),
    )


# ── Vehicle Category Management ───────────────────────────────────────────────
#  Doc Ref: DB Schema Part 3 §10-11
#  vehicle_categories seeded in 0006_phase2_driver_vehicle
#  These are the "CAB booking assets" — HATCHBACK, SEDAN, SUV, etc.


@router.get(
    "/master/vehicle-categories",
    response_model=List[VehicleCategoryOut],
    tags=["Master Data"],
)
async def list_vehicle_categories(
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """List vehicle categories. Used in commission rules (CAB service) & pricing rules."""
    return await VehicleCategoryService.list_all(db, active_only=active_only)


@router.post(
    "/master/vehicle-categories",
    response_model=VehicleCategoryOut,
    status_code=201,
    tags=["Master Data"],
)
async def create_vehicle_category(
    payload: VehicleCategoryCreate, db: AsyncSession = Depends(get_db)
):
    return await VehicleCategoryService.create(db, payload)


@router.patch(
    "/master/vehicle-categories/{cat_id}",
    response_model=VehicleCategoryOut,
    tags=["Master Data"],
)
async def update_vehicle_category(
    cat_id: int, payload: VehicleCategoryUpdate, db: AsyncSession = Depends(get_db)
):
    cat = await VehicleCategoryService.update(db, cat_id, payload)
    if not cat:
        raise HTTPException(status_code=404, detail="Vehicle category not found")
    return cat


@router.delete(
    "/master/vehicle-categories/{cat_id}",
    response_model=SettingsResponse,
    tags=["Master Data"],
)
async def delete_vehicle_category(cat_id: int, db: AsyncSession = Depends(get_db)):
    deleted = await VehicleCategoryService.delete(db, cat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Vehicle category not found")
    return SettingsResponse(message="Vehicle category deleted")


# ════════════════════════════════════════════════════════════════
#  SERVICE TYPES  (Dynamic — Migration 0019)
#  Doc Ref: DB Schema Part 2 §8 — CAB | HOTEL | TOUR
#  type_code is immutable; labels / media / SEO are admin-editable.
# ════════════════════════════════════════════════════════════════


@router.get(
    "/master/service-types",
    response_model=List[ServiceTypeOut],
    tags=["Master Data"],
    summary="List all platform service types (CAB, HOTEL, TOUR)",
)
async def list_service_types(
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    return await ServiceTypeService.list_all(db, active_only=active_only)


@router.patch(
    "/master/service-types/{st_id}",
    response_model=ServiceTypeOut,
    tags=["Master Data"],
    summary="Update a service type's label, description, media, or SEO metadata",
)
async def update_service_type(
    st_id: int, payload: ServiceTypeUpdate, db: AsyncSession = Depends(get_db)
):
    obj = await ServiceTypeService.update(db, st_id, payload)
    if not obj:
        raise HTTPException(status_code=404, detail="Service type not found")
    return obj


@router.post(
    "/master/service-types",
    response_model=ServiceTypeOut,
    status_code=201,
    tags=["Master Data"],
    summary="Create a new platform service type",
)
async def create_service_type(
    payload: ServiceTypeCreate, db: AsyncSession = Depends(get_db)
):
    try:
        obj = await ServiceTypeService.create(db, payload)
        await db.commit()
        await db.refresh(obj)
        return obj
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete(
    "/master/service-types/{st_id}",
    response_model=SettingsResponse,
    tags=["Master Data"],
    summary="Delete a service type (hard-delete; guarded if partners reference it)",
)
async def delete_service_type(st_id: int, db: AsyncSession = Depends(get_db)):
    try:
        deleted = await ServiceTypeService.delete(db, st_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Service type not found")
        await db.commit()
        return SettingsResponse(message="Service type deleted")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ════════════════════════════════════════════════════════════════
#  DEFAULT VEHICLE PRICING RULES  (Platform Fallback Defaults)
#  Doc Ref:
#    DB Schema Part 3 §18 — vehicle_pricing_rules
#    BRD Part 3 §35 — Fare engine must not hardcode values
#    BRD Part 3 §36 — City-based pricing with fallback to defaults
#  Table: default_vehicle_pricing_rules (migration 0011)
#  Logic: city-specific rule takes priority; default used as fallback
# ════════════════════════════════════════════════════════════════


@router.get(
    "/default-pricing-rules",
    response_model=List[DefaultPricingRuleOut],
    tags=["Settings – Default Pricing"],
    summary="List all platform-level default pricing rules",
    description=(
        "Returns the full default_vehicle_pricing_rules table. "
        "These values are used as fallback when no city-specific rule is configured "
        "for a given (vehicle_category, trip_type) combination. "
        "Ordered by vehicle_category_id then trip_type."
    ),
)
async def list_default_pricing_rules(db: AsyncSession = Depends(get_db)):
    return await DefaultPricingService.list_all(db)


@router.patch(
    "/default-pricing-rules/{rule_id}",
    response_model=DefaultPricingRuleOut,
    tags=["Settings – Default Pricing"],
    summary="Update a single default pricing rule",
    description=(
        "Admin can update any numeric field on a default pricing row. "
        "Only supplied fields are changed (partial update). "
        "The updated_at timestamp is set automatically."
    ),
)
async def update_default_pricing_rule(
    rule_id: int,
    payload: DefaultPricingRuleUpdate,
    db: AsyncSession = Depends(get_db),
):
    rule = await DefaultPricingService.update_rule(db, rule_id, payload)
    if not rule:
        raise HTTPException(status_code=404, detail="Default pricing rule not found")
    return rule


@router.get(
    "/pricing-rules/effective",
    response_model=EffectivePricingRuleOut,
    tags=["Settings – Default Pricing"],
    summary="Get effective pricing for a booking (city-specific → default fallback)",
    description=(
        "Core fare-engine helper used by the booking system. "
        "Returns the city-specific rule if one exists for the given "
        "(city_id, vehicle_category_id, trip_type) combination. "
        "Falls back to the platform default if not. "
        "Response includes a 'source' field: 'city_specific' | 'default'. "
        "BRD Part 3 §35: pricing must never be hardcoded — always call this endpoint."
    ),
)
async def get_effective_pricing_rule(
    city_id: int = Query(..., description="City ID for the booking"),
    vehicle_category_id: int = Query(..., description="Vehicle category ID"),
    trip_type: str = Query(
        ..., description="LOCAL | AIRPORT | OUTSTATION | ONE_WAY | ROUND_TRIP"
    ),
    db: AsyncSession = Depends(get_db),
):
    rule = await DefaultPricingService.get_effective_rule(
        db, city_id, vehicle_category_id, trip_type
    )
    if not rule:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No pricing rule found for city_id={city_id}, "
                f"vehicle_category_id={vehicle_category_id}, trip_type={trip_type}. "
                "Please configure a city-specific rule or ensure the migration 0011 was applied."
            ),
        )
    return rule


# ════════════════════════════════════════════════════════════════
#  MEDIA UPLOAD — Cloudinary
#  POST /admin/settings/upload-media
#  Doc Ref: DB Schema Part 1 §14 — api_integrations (CLOUDINARY row)
#           Migration 0013_platform_profile — PLATFORM_LOGO_URL etc.
#
#  Reads cloud_name / api_key / api_secret from the CLOUDINARY
#  api_integrations row (is_active must be TRUE).
#
#  Upload spec per asset_type:
#    logo     → crop=fill  w=400  h=120  folder=waytero/platform
#    favicon  → crop=fill  w=32   h=32   folder=waytero/platform
#    og_image → crop=fill  w=1200 h=630  folder=waytero/platform
#    general  → no forced dimensions   folder=waytero/platform
#
#  The frontend performs client-side crop (canvas) and sends the
#  already-cropped image as multipart/form-data.
#  This endpoint signs the upload and saves it to Cloudinary,
#  then returns the secure_url so the caller can persist it via
#  PUT /admin/settings/configurations/{key}.
# ════════════════════════════════════════════════════════════════

ASSET_SPECS: dict[str, dict] = {
    "logo": {"width": 400, "height": 120, "crop": "fill"},
    "favicon": {"width": 32, "height": 32, "crop": "fill"},
    "og_image": {"width": 1200, "height": 630, "crop": "fill"},
    "general": {},
}


def _cloudinary_sign(params: dict, api_secret: str) -> str:
    """
    Compute Cloudinary v2 signature.
    Signature = SHA1 of sorted key=value pairs (excluding file/api_key/resource_type)
    concatenated with the api_secret.
    """
    excluded = {"file", "api_key", "resource_type", "cloud_name"}
    sorted_str = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k not in excluded
    )
    to_sign = sorted_str + api_secret
    return hashlib.sha1(to_sign.encode("utf-8")).hexdigest()


@router.post(
    "/upload-media",
    tags=["Settings – Media Upload"],
    summary="Upload platform media (logo / favicon / OG image) to Cloudinary",
    description=(
        "Reads Cloudinary credentials from the active CLOUDINARY api_integration row. "
        "Accepts a multipart upload with asset_type in [logo, favicon, og_image, general]. "
        "Returns the Cloudinary secure_url for persisting in system_configurations."
    ),
)
async def upload_platform_media(
    file: UploadFile = File(
        ..., description="Image file (PNG, JPG, ICO, SVG, WebP) or PDF document"
    ),
    asset_type: str = Form(
        "general", description="logo | favicon | og_image | general | document | photo"
    ),
    folder_override: str = Form(
        "",
        description="Optional Cloudinary folder override (e.g. waytero/vehicles/documents)",
    ),
    db: AsyncSession = Depends(get_db),
):
    # 1. Resolve Cloudinary credentials from api_integrations
    result = await db.execute(
        sa_select(ApiIntegration).where(
            ApiIntegration.service_type == "CLOUDINARY",
            ApiIntegration.is_active == True,  # noqa: E712
        )
    )
    integration = result.scalar_one_or_none()
    if not integration or not integration.configuration:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cloudinary integration is not configured or not active. "
                "Go to Settings → API Integrations → Cloudinary and set credentials."
            ),
        )

    cfg: dict = integration.configuration
    cloud_name = cfg.get("cloud_name", "").strip()
    api_key = cfg.get("api_key", "").strip()
    api_secret = cfg.get("api_secret", "").strip()

    if not cloud_name or not api_key or not api_secret:
        raise HTTPException(
            status_code=400,
            detail="Cloudinary cloud_name, api_key, and api_secret must all be set.",
        )

    # 2. Build upload params
    spec = ASSET_SPECS.get(asset_type, {})
    ts = int(time.time())
    folder = "waytero/platform"

    # Determine folder: honour override if provided, else default per asset_type
    if folder_override and folder_override.strip():
        actual_folder = folder_override.strip()
    elif asset_type in ("document",):
        actual_folder = "waytero/vehicles/documents"
    elif asset_type in ("photo",):
        actual_folder = "waytero/vehicles/photos"
    else:
        actual_folder = folder

    upload_params: dict = {
        "timestamp": ts,
        "folder": actual_folder,
        "public_id": f"{asset_type}_{ts}",
    }

    if spec.get("width"):
        upload_params["transformation"] = (
            f"c_{spec['crop']},w_{spec['width']},h_{spec['height']}"
        )

    upload_params["signature"] = _cloudinary_sign(upload_params, api_secret)
    upload_params["api_key"] = api_key

    # 3. Read file bytes
    content = await file.read()
    max_bytes = 20 * 1024 * 1024  # 20 MB (docs can be large PDFs)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="File too large — maximum 20 MB.")

    # 4. Determine resource_type (raw for PDF/documents, image for everything else)
    ct = (file.content_type or "").lower()
    filename_lower = (file.filename or "").lower()
    is_raw = ct == "application/pdf" or filename_lower.endswith(".pdf")
    resource_type = "raw" if is_raw else "image"

    # 4b. POST to Cloudinary upload API
    upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                upload_url,
                data=upload_params,
                files={
                    "file": (
                        file.filename or "upload",
                        content,
                        file.content_type or "image/png",
                    )
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cloudinary request failed: {exc}")

    if resp.status_code not in (200, 201):
        # Attempt to extract Cloudinary error message
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=502, detail=f"Cloudinary error: {detail}")

    data = resp.json()
    return {
        "secure_url": data.get("secure_url"),
        "public_id": data.get("public_id"),
        "width": data.get("width"),
        "height": data.get("height"),
        "format": data.get("format"),
        "asset_type": asset_type,
        "resource_type": resource_type,
    }


# ════════════════════════════════════════════════════════════════
#  WEBSITE CMS — Homepage Sections, Variants, Header, Footer
#  Doc Ref: Migration 0044_website_cms
#
#  Admin endpoints (require cms.section.manage / cms.header_footer.manage):
#    GET    /admin/settings/cms/sections              — registry of section templates
#    PATCH  /admin/settings/cms/sections/{key}        — toggle visibility / rename
#    GET    /admin/settings/cms/sections/{key}/variants
#    POST   /admin/settings/cms/sections/{key}/variants
#    GET    /admin/settings/cms/variants/{id}
#    PATCH  /admin/settings/cms/variants/{id}
#    DELETE /admin/settings/cms/variants/{id}
#    POST   /admin/settings/cms/variants/{id}/activate
#    GET    /admin/settings/cms/header                 — singleton
#    PATCH  /admin/settings/cms/header                 — singleton update
#    GET    /admin/settings/cms/footer                 — singleton
#    PATCH  /admin/settings/cms/footer                 — singleton update
#
#  Public (no auth — used by customer-web):
#    GET    /public/homepage                            — flattened homepage bundle
# ════════════════════════════════════════════════════════════════


@router.get(
    "/cms/sections",
    response_model=List[PageSectionOut],
    tags=["CMS – Sections"],
    summary="List all homepage section templates (with variant counts)",
)
async def list_cms_sections(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_permission("cms.section.manage")),
):
    return await CmsService.list_sections(db)


@router.get(
    "/cms/picker-items",
    tags=["CMS – Content Picker"],
    summary="Candidate DB records to populate a section's content JSON",
)
async def cms_picker_items(
    picker_type: str = Query(
        ...,
        alias="type",
        description="HOTELS | COUPONS | DESTINATIONS | SERVICES",
    ),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_permission("cms.section.manage")),
):
    return await CmsService.picker_items(db, picker_type)


@router.patch(
    "/cms/sections/{section_key}",
    response_model=PageSectionOut,
    tags=["CMS – Sections"],
    summary="Update section metadata (name, visibility, order)",
)
async def update_cms_section(
    section_key: str,
    payload: PageSectionUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.section.manage")),
):
    section = await CmsService.update_section(
        db, section_key, payload, actor_id=user.get("sub")
    )
    if not section:
        raise HTTPException(
            status_code=404, detail=f"Section '{section_key}' not found"
        )
    return section


@router.get(
    "/cms/sections/{section_key}/variants",
    response_model=List[SectionVariantOut],
    tags=["CMS – Variants"],
    summary="List all variants of a section, ordered by display_order",
)
async def list_cms_variants(
    section_key: str,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_permission("cms.section.manage")),
):
    variants = await CmsService.list_variants(db, section_key)
    if not variants and not await CmsService.get_section_by_key(db, section_key):
        raise HTTPException(
            status_code=404, detail=f"Section '{section_key}' not found"
        )
    return variants


@router.post(
    "/cms/sections/{section_key}/variants",
    response_model=SectionVariantOut,
    status_code=201,
    tags=["CMS – Variants"],
    summary="Create a new variant design for a section",
)
async def create_cms_variant(
    section_key: str,
    payload: SectionVariantCreate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.section.manage")),
):
    variant = await CmsService.create_variant(
        db, section_key, payload, actor_id=user.get("sub")
    )
    if not variant:
        raise HTTPException(
            status_code=404, detail=f"Section '{section_key}' not found"
        )
    return variant


@router.get(
    "/cms/variants/{variant_id}",
    response_model=SectionVariantOut,
    tags=["CMS – Variants"],
)
async def get_cms_variant(
    variant_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_permission("cms.section.manage")),
):
    variant = await CmsService.get_variant(db, variant_id)
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    return variant


@router.patch(
    "/cms/variants/{variant_id}",
    response_model=SectionVariantOut,
    tags=["CMS – Variants"],
    summary="Edit a variant's content / media / scheduling",
)
async def update_cms_variant(
    variant_id: int,
    payload: SectionVariantUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.section.manage")),
):
    variant = await CmsService.update_variant(
        db, variant_id, payload, actor_id=user.get("sub")
    )
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    return variant


@router.delete(
    "/cms/variants/{variant_id}",
    response_model=SettingsResponse,
    tags=["CMS – Variants"],
    summary="Delete a variant (cannot delete the currently-active variant)",
)
async def delete_cms_variant(
    variant_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.section.manage")),
):
    variant = await CmsService.get_variant(db, variant_id)
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    if variant.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the active variant. Activate another variant first.",
        )
    await CmsService.delete_variant(db, variant_id, actor_id=user.get("sub"))
    return SettingsResponse(message=f"Variant {variant_id} deleted")


@router.post(
    "/cms/variants/{variant_id}/activate",
    response_model=SectionVariantOut,
    tags=["CMS – Variants"],
    summary="Mark this variant as the active one for its section",
)
async def activate_cms_variant(
    variant_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.section.manage")),
):
    variant = await CmsService.activate_variant(
        db, variant_id, actor_id=user.get("sub")
    )
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    return variant


@router.get(
    "/cms/header",
    response_model=SiteHeaderOut,
    tags=["CMS – Header & Footer"],
    summary="Get the global website header config (singleton)",
)
async def get_cms_header(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_permission("cms.header_footer.manage")),
):
    return await CmsService.get_header(db)


@router.patch(
    "/cms/header",
    response_model=SiteHeaderOut,
    tags=["CMS – Header & Footer"],
    summary="Update the global website header config",
)
async def update_cms_header(
    payload: SiteHeaderUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.header_footer.manage")),
):
    return await CmsService.update_header(db, payload, actor_id=user.get("sub"))


@router.get(
    "/cms/footer",
    response_model=SiteFooterOut,
    tags=["CMS – Header & Footer"],
    summary="Get the global website footer config (singleton)",
)
async def get_cms_footer(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_permission("cms.header_footer.manage")),
):
    return await CmsService.get_footer(db)


@router.patch(
    "/cms/footer",
    response_model=SiteFooterOut,
    tags=["CMS – Header & Footer"],
    summary="Update the global website footer config",
)
async def update_cms_footer(
    payload: SiteFooterUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_permission("cms.header_footer.manage")),
):
    return await CmsService.update_footer(db, payload, actor_id=user.get("sub"))
