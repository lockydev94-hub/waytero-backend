# ============================================================
# WAYTERO — ADMIN / SETTINGS SERVICE (ASYNC)
# File: app/modules/admin/services/__init__.py
# Doc Ref:
#   Backend Architecture §3 Module Structure
#   Frontend Architecture §31 System Configuration
#   DB Schema Part 1 §12-14 | Part 2 §14-15 | Part 8 §11
#   api_integrations examples: RAZORPAY, MSG91, WHATSAPP, FCM, MINIO
# ALL methods are async — uses AsyncSession (project standard)
# ============================================================

from datetime import date, datetime, timezone
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, update as sa_update

from app.core.exceptions import (
    BusinessException,
    PermissionDeniedException,
    ValidationException,
)
from app.modules.admin.models import (
    SystemConfiguration,
    AppVersion,
    CommissionGroup,
    CommissionRule,
    NotificationTemplate,
    ApiIntegration,
    PageSection,
    SectionVariant,
    SiteHeaderConfig,
    SiteFooterConfig,
    CmsAuditVersion,
    BlogPost,
)
from app.modules.master.models import City, State
from app.modules.vehicle.models import VehicleCategory, VehiclePricingRule
from app.modules.admin.schemas import (
    SystemConfigUpdate,
    AppVersionCreate,
    AppVersionUpdate,
    CommissionGroupCreate,
    CommissionGroupUpdate,
    CommissionRuleCreate,
    CommissionRuleUpdate,
    VehiclePricingRuleCreate,
    VehiclePricingRuleUpdate,
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
    ApiIntegrationCreate,
    ApiIntegrationUpdate,
    PageSectionUpdate,
    SectionVariantCreate,
    SectionVariantUpdate,
    SiteHeaderUpdate,
    SiteHeaderOut,
    SiteFooterUpdate,
    SiteFooterOut,
    PublicHomepageOut,
    BlogPostCreate,
    BlogPostUpdate,
)
from app.modules.admin.schemas import (
    CityOut,
    CityCreate,
    CityUpdate,
    StateOut,
    VehicleCategoryCreate,
    VehicleCategoryUpdate,
)
from app.shared.utilities.seo import ensure_slug_unique, unique_slug


# ── System Configuration ──────────────────────────────────────────────────────


class SystemConfigService:

    # Keys the admin session policy reads: idle-lock default, forced-logout
    # hour, MPIN strength and lockout. /admin/settings is gated ALL_STAFF, so
    # without this a CCO could move the hour everyone gets logged out at or
    # weaken the MPIN rule — those belong to the super admin alone.
    SUPER_ADMIN_ONLY_PREFIX = "ADMIN_"

    @staticmethod
    def _reject_admin_policy_write(key: str, current_user: Optional[dict]) -> None:
        if not key.startswith(SystemConfigService.SUPER_ADMIN_ONLY_PREFIX):
            return
        user = current_user or {}
        roles = user.get("roles") or [user.get("role", "")]
        if "SUPER_ADMIN" not in roles:
            raise PermissionDeniedException(
                f"'{key}' is an admin session-policy key and can only be "
                "changed by a super admin"
            )

    @staticmethod
    async def list_all(db: AsyncSession) -> List[SystemConfiguration]:
        result = await db.execute(
            select(SystemConfiguration).order_by(SystemConfiguration.config_key)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_key(db: AsyncSession, key: str) -> Optional[SystemConfiguration]:
        result = await db.execute(
            select(SystemConfiguration).where(SystemConfiguration.config_key == key)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def upsert(
        db: AsyncSession,
        key: str,
        payload: SystemConfigUpdate,
        current_user: Optional[dict] = None,
    ) -> SystemConfiguration:
        SystemConfigService._reject_admin_policy_write(key, current_user)
        result = await db.execute(
            select(SystemConfiguration).where(SystemConfiguration.config_key == key)
        )
        cfg = result.scalar_one_or_none()
        if cfg:
            cfg.config_value = payload.config_value
            if payload.description is not None:
                cfg.description = payload.description
            cfg.updated_at = datetime.now(timezone.utc)
        else:
            cfg = SystemConfiguration(
                config_key=key,
                config_value=payload.config_value,
                description=payload.description,
                updated_at=datetime.now(timezone.utc),
            )
            db.add(cfg)
        await db.flush()
        await db.refresh(cfg)
        return cfg

    @staticmethod
    async def delete(
        db: AsyncSession,
        key: str,
        current_user: Optional[dict] = None,
    ) -> bool:
        SystemConfigService._reject_admin_policy_write(key, current_user)
        result = await db.execute(
            select(SystemConfiguration).where(SystemConfiguration.config_key == key)
        )
        cfg = result.scalar_one_or_none()
        if not cfg:
            return False
        await db.delete(cfg)
        return True


# ── API Integrations ──────────────────────────────────────────────────────────


class ApiIntegrationService:
    """
    Manages third-party service configs.
    Doc Ref: DB Schema Part 1 §14 — api_integrations
    service_type examples: RAZORPAY | MSG91 | WHATSAPP | FCM | MINIO | CLOUDINARY | GOOGLE_MAPS | SMTP
    """

    @staticmethod
    async def list_all(db: AsyncSession) -> List[ApiIntegration]:
        result = await db.execute(
            select(ApiIntegration).order_by(ApiIntegration.service_type)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(
        db: AsyncSession, integration_id: int
    ) -> Optional[ApiIntegration]:
        result = await db.execute(
            select(ApiIntegration).where(ApiIntegration.id == integration_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(db: AsyncSession, payload: ApiIntegrationCreate) -> ApiIntegration:
        ai = ApiIntegration(
            service_name=payload.service_name,
            service_type=payload.service_type,
            configuration=payload.configuration,  # JSONB column accepts dict directly
            is_active=payload.is_active,
        )
        db.add(ai)
        await db.flush()
        await db.refresh(ai)
        return ai

    @staticmethod
    async def update(
        db: AsyncSession, integration_id: int, payload: ApiIntegrationUpdate
    ) -> Optional[ApiIntegration]:
        result = await db.execute(
            select(ApiIntegration).where(ApiIntegration.id == integration_id)
        )
        ai = result.scalar_one_or_none()
        if not ai:
            return None
        if payload.service_name is not None:
            ai.service_name = payload.service_name
        if payload.configuration is not None:
            ai.configuration = (
                payload.configuration
            )  # JSONB column accepts dict directly
        if payload.is_active is not None:
            ai.is_active = payload.is_active
        await db.flush()
        await db.refresh(ai)
        return ai

    @staticmethod
    async def delete(db: AsyncSession, integration_id: int) -> bool:
        result = await db.execute(
            select(ApiIntegration).where(ApiIntegration.id == integration_id)
        )
        ai = result.scalar_one_or_none()
        if not ai:
            return False
        await db.delete(ai)
        return True


# ── App Version ───────────────────────────────────────────────────────────────


class AppVersionService:

    @staticmethod
    async def list_all(db: AsyncSession) -> List[AppVersion]:
        result = await db.execute(
            select(AppVersion).order_by(
                AppVersion.platform, AppVersion.created_at.desc()
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def create(db: AsyncSession, payload: AppVersionCreate) -> AppVersion:
        av = AppVersion(**payload.model_dump())
        db.add(av)
        await db.flush()
        await db.refresh(av)
        return av

    @staticmethod
    async def update(
        db: AsyncSession, version_id: int, payload: AppVersionUpdate
    ) -> Optional[AppVersion]:
        result = await db.execute(select(AppVersion).where(AppVersion.id == version_id))
        av = result.scalar_one_or_none()
        if not av:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(av, field, val)
        await db.flush()
        await db.refresh(av)
        return av

    @staticmethod
    async def delete(db: AsyncSession, version_id: int) -> bool:
        result = await db.execute(select(AppVersion).where(AppVersion.id == version_id))
        av = result.scalar_one_or_none()
        if not av:
            return False
        await db.delete(av)
        return True


# ── Commission Group & Rules ──────────────────────────────────────────────────


class CommissionService:

    @staticmethod
    async def list_groups(db: AsyncSession) -> List[CommissionGroup]:
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(CommissionGroup)
            .options(selectinload(CommissionGroup.rules))
            .order_by(CommissionGroup.group_name)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_group(db: AsyncSession, group_id: int) -> Optional[CommissionGroup]:
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(CommissionGroup)
            .options(selectinload(CommissionGroup.rules))
            .where(CommissionGroup.id == group_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create_group(
        db: AsyncSession, payload: CommissionGroupCreate
    ) -> CommissionGroup:
        from sqlalchemy.orm import selectinload

        grp = CommissionGroup(**payload.model_dump())
        db.add(grp)
        await db.flush()  # get the generated id; get_db commits on success
        # Re-fetch with rules eagerly loaded (bare refresh cannot load relationships in async)
        result = await db.execute(
            select(CommissionGroup)
            .options(selectinload(CommissionGroup.rules))
            .where(CommissionGroup.id == grp.id)
        )
        return result.scalar_one()

    @staticmethod
    async def update_group(
        db: AsyncSession, group_id: int, payload: CommissionGroupUpdate
    ) -> Optional[CommissionGroup]:
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(CommissionGroup).where(CommissionGroup.id == group_id)
        )
        grp = result.scalar_one_or_none()
        if not grp:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(grp, field, val)
        await db.flush()
        # Re-fetch with rules eagerly loaded
        result2 = await db.execute(
            select(CommissionGroup)
            .options(selectinload(CommissionGroup.rules))
            .where(CommissionGroup.id == group_id)
        )
        return result2.scalar_one_or_none()

    @staticmethod
    async def delete_group(db: AsyncSession, group_id: int) -> bool:
        result = await db.execute(
            select(CommissionGroup).where(CommissionGroup.id == group_id)
        )
        grp = result.scalar_one_or_none()
        if not grp:
            return False
        await db.delete(grp)
        return True

    @staticmethod
    async def add_rule(
        db: AsyncSession, group_id: int, payload: CommissionRuleCreate
    ) -> Optional[CommissionRule]:
        result = await db.execute(
            select(CommissionGroup).where(CommissionGroup.id == group_id)
        )
        if not result.scalar_one_or_none():
            return None
        rule = CommissionRule(commission_group_id=group_id, **payload.model_dump())
        db.add(rule)
        await db.flush()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def update_rule(
        db: AsyncSession, rule_id: int, payload: CommissionRuleUpdate
    ) -> Optional[CommissionRule]:
        result = await db.execute(
            select(CommissionRule).where(CommissionRule.id == rule_id)
        )
        rule = result.scalar_one_or_none()
        if not rule:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(rule, field, val)
        await db.flush()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def delete_rule(db: AsyncSession, rule_id: int) -> bool:
        result = await db.execute(
            select(CommissionRule).where(CommissionRule.id == rule_id)
        )
        rule = result.scalar_one_or_none()
        if not rule:
            return False
        await db.delete(rule)
        return True


# ── Vehicle Pricing Rules ─────────────────────────────────────────────────────


class PricingService:

    @staticmethod
    async def list_rules(
        db: AsyncSession,
        city_id: Optional[int] = None,
        vehicle_category_id: Optional[int] = None,
        trip_type: Optional[str] = None,
    ) -> List[VehiclePricingRule]:
        q = select(VehiclePricingRule)
        if city_id:
            q = q.where(VehiclePricingRule.city_id == city_id)
        if vehicle_category_id:
            q = q.where(VehiclePricingRule.vehicle_category_id == vehicle_category_id)
        if trip_type:
            q = q.where(VehiclePricingRule.trip_type == trip_type)
        q = q.order_by(VehiclePricingRule.city_id, VehiclePricingRule.trip_type)
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def create_rule(
        db: AsyncSession, payload: VehiclePricingRuleCreate
    ) -> VehiclePricingRule:
        rule = VehiclePricingRule(**payload.model_dump())
        db.add(rule)
        await db.flush()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def update_rule(
        db: AsyncSession, rule_id: int, payload: VehiclePricingRuleUpdate
    ) -> Optional[VehiclePricingRule]:
        result = await db.execute(
            select(VehiclePricingRule).where(VehiclePricingRule.id == rule_id)
        )
        rule = result.scalar_one_or_none()
        if not rule:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(rule, field, val)
        await db.flush()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def delete_rule(db: AsyncSession, rule_id: int) -> bool:
        result = await db.execute(
            select(VehiclePricingRule).where(VehiclePricingRule.id == rule_id)
        )
        rule = result.scalar_one_or_none()
        if not rule:
            return False
        await db.delete(rule)
        return True


# ── Notification Templates ────────────────────────────────────────────────────


class NotificationTemplateService:

    @staticmethod
    async def list_all(
        db: AsyncSession, channel: Optional[str] = None
    ) -> List[NotificationTemplate]:
        q = select(NotificationTemplate)
        if channel:
            q = q.where(NotificationTemplate.channel == channel)
        q = q.order_by(NotificationTemplate.channel, NotificationTemplate.template_code)
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(
        db: AsyncSession, template_id: int
    ) -> Optional[NotificationTemplate]:
        result = await db.execute(
            select(NotificationTemplate).where(NotificationTemplate.id == template_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(
        db: AsyncSession, payload: NotificationTemplateCreate
    ) -> NotificationTemplate:
        tmpl = NotificationTemplate(**payload.model_dump())
        db.add(tmpl)
        await db.flush()
        await db.refresh(tmpl)
        return tmpl

    @staticmethod
    async def update(
        db: AsyncSession, template_id: int, payload: NotificationTemplateUpdate
    ) -> Optional[NotificationTemplate]:
        result = await db.execute(
            select(NotificationTemplate).where(NotificationTemplate.id == template_id)
        )
        tmpl = result.scalar_one_or_none()
        if not tmpl:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(tmpl, field, val)
        await db.flush()
        await db.refresh(tmpl)
        return tmpl

    @staticmethod
    async def delete(db: AsyncSession, template_id: int) -> bool:
        result = await db.execute(
            select(NotificationTemplate).where(NotificationTemplate.id == template_id)
        )
        tmpl = result.scalar_one_or_none()
        if not tmpl:
            return False
        await db.delete(tmpl)
        return True


# ── City Service ──────────────────────────────────────────────────────────────
# Doc Ref: Admin API §17 — City Management
# cities table is seeded in migration 0003_phase2_master_data


class CityService:

    @staticmethod
    async def list_all(db: AsyncSession, active_only: bool = False) -> List[CityOut]:
        q = (
            select(City, State.name.label("state_name"))
            .join(State, City.state_id == State.id)
            .order_by(State.name, City.name)
        )
        if active_only:
            q = q.where(City.is_active.is_(True))
        result = await db.execute(q)
        rows = result.all()
        out = []
        for city, state_name in rows:
            o = CityOut.model_validate(city)
            o.state_name = state_name
            out.append(o)
        return out

    @staticmethod
    async def list_states(db: AsyncSession) -> List[StateOut]:
        result = await db.execute(select(State).order_by(State.name))
        return [StateOut.model_validate(s) for s in result.scalars().all()]

    @staticmethod
    async def get_by_id(db: AsyncSession, city_id: int) -> Optional[CityOut]:
        result = await db.execute(
            select(City, State.name.label("state_name"))
            .join(State, City.state_id == State.id)
            .where(City.id == city_id)
        )
        row = result.first()
        if not row:
            return None
        city, state_name = row
        o = CityOut.model_validate(city)
        o.state_name = state_name
        return o

    @staticmethod
    async def create(db: AsyncSession, payload: CityCreate) -> CityOut:
        city = City(**payload.model_dump())
        db.add(city)
        await db.flush()
        await db.refresh(city)
        return await CityService.get_by_id(db, city.id)

    @staticmethod
    async def update(
        db: AsyncSession, city_id: int, payload: CityUpdate
    ) -> Optional[CityOut]:
        result = await db.execute(select(City).where(City.id == city_id))
        city = result.scalar_one_or_none()
        if not city:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(city, field, val)
        await db.flush()
        return await CityService.get_by_id(db, city.id)

    @staticmethod
    async def delete(db: AsyncSession, city_id: int) -> bool:
        result = await db.execute(select(City).where(City.id == city_id))
        city = result.scalar_one_or_none()
        if not city:
            return False
        await db.delete(city)
        return True


# ── Vehicle Category Service ──────────────────────────────────────────────────
# Doc Ref: DB Schema Part 3 §10-11 — vehicle_categories
# Seeded in migration 0006_phase2_driver_vehicle


class VehicleCategoryService:

    @staticmethod
    async def list_all(
        db: AsyncSession, active_only: bool = False
    ) -> List[VehicleCategory]:
        q = select(VehicleCategory).order_by(VehicleCategory.category_name)
        if active_only:
            q = q.where(VehicleCategory.is_active.is_(True))
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(db: AsyncSession, cat_id: int) -> Optional[VehicleCategory]:
        result = await db.execute(
            select(VehicleCategory).where(VehicleCategory.id == cat_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(
        db: AsyncSession, payload: VehicleCategoryCreate
    ) -> VehicleCategory:
        data = payload.model_dump()
        if data.get("slug"):
            await ensure_slug_unique(db, VehicleCategory, data["slug"])
        else:
            data["slug"] = await unique_slug(
                db, VehicleCategory, data.get("category_name") or ""
            )
        cat = VehicleCategory(**data)
        db.add(cat)
        await db.flush()
        await db.refresh(cat)
        return cat

    @staticmethod
    async def update(
        db: AsyncSession, cat_id: int, payload: VehicleCategoryUpdate
    ) -> Optional[VehicleCategory]:
        result = await db.execute(
            select(VehicleCategory).where(VehicleCategory.id == cat_id)
        )
        cat = result.scalar_one_or_none()
        if not cat:
            return None
        data = payload.model_dump(exclude_unset=True)
        if data.get("slug"):
            await ensure_slug_unique(
                db, VehicleCategory, data["slug"], exclude_id=cat_id
            )
        for field, val in data.items():
            setattr(cat, field, val)
        await db.flush()
        await db.refresh(cat)
        return cat

    @staticmethod
    async def delete(db: AsyncSession, cat_id: int) -> bool:
        result = await db.execute(
            select(VehicleCategory).where(VehicleCategory.id == cat_id)
        )
        cat = result.scalar_one_or_none()
        if not cat:
            return False
        await db.delete(cat)
        return True


# ── Default Vehicle Pricing Rules ─────────────────────────────────────────────


class DefaultPricingService:
    """
    Manages the platform-level fallback pricing table.
    BRD Part 3 §35 — fare engine must not hardcode values.
    Booking engine calls get_effective_rule() to always get a usable price.
    """

    @staticmethod
    async def list_all(db: AsyncSession):
        """Return all default pricing rules ordered by category then trip_type."""
        from app.modules.vehicle.models import DefaultVehiclePricingRule

        q = select(DefaultVehiclePricingRule).order_by(
            DefaultVehiclePricingRule.vehicle_category_id,
            DefaultVehiclePricingRule.trip_type,
        )
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def update_rule(db: AsyncSession, rule_id: int, payload) -> Optional[object]:
        """Admin updates a single default pricing row."""
        from app.modules.vehicle.models import DefaultVehiclePricingRule
        from datetime import datetime, timezone

        result = await db.execute(
            select(DefaultVehiclePricingRule).where(
                DefaultVehiclePricingRule.id == rule_id
            )
        )
        rule = result.scalar_one_or_none()
        if not rule:
            return None
        for field, val in payload.model_dump(exclude_unset=True).items():
            setattr(rule, field, val)
        rule.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await db.refresh(rule)
        return rule

    @staticmethod
    async def get_effective_rule(
        db: AsyncSession,
        city_id: int,
        vehicle_category_id: int,
        trip_type: str,
        for_date: Optional[date] = None,
    ) -> Optional[dict]:
        """
        Core fallback logic used by the booking engine:
        1. Look for a city-specific rule (vehicle_pricing_rules).
        2. If not found, fall back to default_vehicle_pricing_rules.
        3. Return None only if neither exists (should not happen after migration).

        Returns a dict with all pricing fields + a 'source' key, plus the
        identity of the rule row that answered (`rule_id`, `rule_table`,
        `effective_from`, `effective_to`) so a booking can snapshot the rule it
        was actually priced under. Settlement and dispute both read that snapshot
        back, which is what makes "the rate changed since" a question with an
        answer instead of an argument.

        `for_date` is the day the trip happens, and defaults to today. A rule
        sheet carries `effective_from` / `effective_to` precisely so a fare can be
        announced now for a trip next month and a superseded rate stops applying
        on the day it was superseded — neither of which works if the columns are
        ignored, which is how they were read until 0086. A future-dated booking
        must pass its own pickup date or it will be quoted at the current rate.
        """
        from app.modules.vehicle.models import (
            VehiclePricingRule,
            DefaultVehiclePricingRule,
        )

        on_day = for_date or datetime.now(timezone.utc).date()

        # Step 1: city-specific rule, in force on the trip's date.
        city_q = select(VehiclePricingRule).where(
            VehiclePricingRule.city_id == city_id,
            VehiclePricingRule.vehicle_category_id == vehicle_category_id,
            VehiclePricingRule.trip_type == trip_type,
            or_(
                VehiclePricingRule.effective_from.is_(None),
                VehiclePricingRule.effective_from <= on_day,
            ),
            or_(
                VehiclePricingRule.effective_to.is_(None),
                VehiclePricingRule.effective_to >= on_day,
            ),
        )
        city_rows = (await db.execute(city_q)).scalars().all()
        if len(city_rows) > 1:
            # Two sheets in force at once is a data conflict, not a booking
            # problem, and guessing between them by row order is how a customer
            # is quoted one price and invoiced another. Say which pair is
            # duplicated and let the owner fix the sheet.
            raise BusinessException(
                f"More than one fare sheet is in force for this city, cab "
                f"category and trip type on {on_day}. Remove or re-date one of "
                f"them before booking."
            )
        city_rule = city_rows[0] if city_rows else None

        if city_rule:
            return {
                "rule_id": city_rule.id,
                "rule_table": "vehicle_pricing_rules",
                "city_id": city_rule.city_id,
                "effective_from": city_rule.effective_from,
                "effective_to": city_rule.effective_to,
                "vehicle_category_id": city_rule.vehicle_category_id,
                "trip_type": city_rule.trip_type,
                "base_fare": city_rule.base_fare or 0,
                "minimum_km": city_rule.minimum_km or 0,
                "per_km_rate": city_rule.per_km_rate or 0,
                "driver_allowance": city_rule.driver_allowance or 0,
                "driver_allowance_type": city_rule.driver_allowance_type or "PER_TRIP",
                "night_charge": city_rule.night_charge or 0,
                "night_charge_type": city_rule.night_charge_type or "FIXED",
                "toll": city_rule.toll or 0,
                "free_waiting_minutes": city_rule.free_waiting_minutes or 0,
                "actual_waiting_minutes": city_rule.actual_waiting_minutes or 0,
                "waiting_rate_per_hour": city_rule.waiting_rate_per_hour or 0,
                "waiting_granularity": city_rule.waiting_granularity
                or "PER_15_MINUTES",
                "source": "city_specific",
            }

        # Step 2: default fallback
        def_q = select(DefaultVehiclePricingRule).where(
            DefaultVehiclePricingRule.vehicle_category_id == vehicle_category_id,
            DefaultVehiclePricingRule.trip_type == trip_type,
        )
        def_rows = (await db.execute(def_q)).scalars().all()
        if len(def_rows) > 1:
            raise BusinessException(
                "More than one platform fare sheet exists for this cab category "
                "and trip type. Remove one before booking."
            )
        def_rule = def_rows[0] if def_rows else None

        if def_rule:
            return {
                # The platform fallback sheet carries no effective window
                # (0011/0022 created it without one), so there is nothing to
                # date-filter here and nothing to claim about a start date.
                "rule_id": def_rule.id,
                "rule_table": "default_vehicle_pricing_rules",
                "city_id": None,
                "effective_from": None,
                "effective_to": None,
                "vehicle_category_id": def_rule.vehicle_category_id,
                "trip_type": def_rule.trip_type,
                "base_fare": def_rule.base_fare,
                "minimum_km": def_rule.minimum_km,
                "per_km_rate": def_rule.per_km_rate,
                "driver_allowance": def_rule.driver_allowance,
                "driver_allowance_type": def_rule.driver_allowance_type or "PER_TRIP",
                "night_charge": def_rule.night_charge,
                "night_charge_type": def_rule.night_charge_type or "FIXED",
                "toll": def_rule.toll or 0,
                "free_waiting_minutes": def_rule.free_waiting_minutes or 0,
                "actual_waiting_minutes": def_rule.actual_waiting_minutes or 0,
                "waiting_rate_per_hour": def_rule.waiting_rate_per_hour or 0,
                "waiting_granularity": def_rule.waiting_granularity or "PER_15_MINUTES",
                "source": "default",
            }

        return None


# ── Service Type Service (Migration 0019) ─────────────────────────────────────


class ServiceTypeService:
    """
    CRUD for the dynamic service_types master table.
    type_code (CAB | HOTEL | TOUR) is immutable — only metadata is editable.
    Doc Ref: DB Schema Part 2 §8 — service types are platform constants;
             Migration 0019 makes their metadata (labels, icons, SEO) editable.
    """

    @staticmethod
    async def list_all(db: AsyncSession, active_only: bool = False):
        from app.modules.master.models import ServiceType

        q = select(ServiceType).order_by(
            ServiceType.display_order, ServiceType.type_code
        )
        if active_only:
            q = q.where(ServiceType.is_active.is_(True))
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(db: AsyncSession, st_id: int):
        from app.modules.master.models import ServiceType

        result = await db.execute(select(ServiceType).where(ServiceType.id == st_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def update(db: AsyncSession, st_id: int, payload):
        from app.modules.master.models import ServiceType

        result = await db.execute(select(ServiceType).where(ServiceType.id == st_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return None
        data = payload.model_dump(exclude_unset=True)
        if data.get("slug"):
            await ensure_slug_unique(db, ServiceType, data["slug"], exclude_id=st_id)
        for field, val in data.items():
            setattr(obj, field, val)
        await db.flush()
        await db.refresh(obj)
        return obj

    @staticmethod
    async def create(db: AsyncSession, payload):
        """Create a new service type. type_code must be unique (UNIQUE constraint on DB)."""
        from app.modules.master.models import ServiceType
        from sqlalchemy import select as _sel

        # Normalise type_code to upper
        type_code = payload.type_code.strip().upper()
        # Check uniqueness upfront for a friendly error
        existing = (
            await db.execute(
                _sel(ServiceType).where(ServiceType.type_code == type_code)
            )
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"Service type '{type_code}' already exists")
        if payload.slug:
            await ensure_slug_unique(db, ServiceType, payload.slug)
            slug = payload.slug
        else:
            slug = await unique_slug(db, ServiceType, payload.label or type_code)
        obj = ServiceType(
            type_code=type_code,
            label=payload.label,
            slug=slug,
            description=payload.description,
            icon_url=payload.icon_url,
            image_url=payload.image_url,
            seo_title=payload.seo_title,
            seo_description=payload.seo_description,
            seo_keywords=payload.seo_keywords,
            display_order=payload.display_order,
            is_active=payload.is_active,
        )
        db.add(obj)
        await db.flush()
        await db.refresh(obj)
        return obj

    @staticmethod
    async def delete(db: AsyncSession, st_id: int) -> bool:
        """Hard-delete a service type. Guarded — cannot delete if partner_services rows reference this code."""
        from app.modules.master.models import ServiceType
        from sqlalchemy import text as _txt

        result = await db.execute(select(ServiceType).where(ServiceType.id == st_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return False
        # Guard: check if any partner_services rows use this type_code
        usage = (
            await db.execute(
                _txt(
                    "SELECT COUNT(*) FROM partner_services WHERE service_type = :code"
                ),
                {"code": obj.type_code},
            )
        ).scalar()
        if usage and usage > 0:
            raise ValueError(
                f"Cannot delete '{obj.type_code}' — {usage} partner service record(s) reference it. "
                "Deactivate (toggle is_active) instead."
            )
        await db.delete(obj)
        await db.flush()
        return True

    @staticmethod
    async def get_valid_codes(db: AsyncSession) -> set:
        """Return set of all active type_codes from DB — used for dynamic validation."""
        from app.modules.master.models import ServiceType

        rows = (
            (
                await db.execute(
                    select(ServiceType.type_code).where(ServiceType.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
        return set(rows)


# ════════════════════════════════════════════════════════════════
#  WEBSITE CMS — Homepage Sections, Variants, Header, Footer
#  Doc Ref: Migration 0044_website_cms
# ════════════════════════════════════════════════════════════════

# The customer-web listing route for each platform service type. The type_code
# is not the URL slug: /cab, /hotel and /tour do not exist, so building the
# link from the code hands the admin a card that 404s.
SERVICE_TYPE_ROUTES = {"CAB": "/cabs", "HOTEL": "/hotels", "TOUR": "/tours"}


class CmsService:
    """
    Manages the admin-controlled website homepage:
      • page_sections      — registry of section templates
      • section_variants   — concrete designs per section (one active)
      • site_header_config — singleton global header
      • site_footer_config — singleton global footer
      • cms_audit_versions — append-only audit

    Public read path (used by customer-web):
        build_public_homepage() → PublicHomepageOut
        Resolves only visible sections whose active variant is in-window.
    """

    # ── Content contract ─────────────────────────────────────────

    # `build_public_homepage` merges each variant's JSONB over the typed
    # columns (`variant_payload.update(active.content)`), and customer-web
    # reads the merged object back with `pick(variant, key)`. So the only
    # content keys with any effect are the ones a renderer literally picks,
    # and any key that names a column is a silent overwrite of the typed
    # field an admin can see and edit elsewhere in the same form.
    #
    # The live-data bands (hotels, destinations, tours, routes, cities, offers,
    # blog) fetch their cards from tables, so they accept copy keys only. Their
    # `content.hotels` / `content.destinations` entries used to be documented
    # and offered in the editor; nothing rendered them, so they are rejected now
    # rather than quietly filling a form that does nothing.
    SECTION_CONTENT_KEYS: dict[str, set[str]] = {
        "HERO": {"marquee_items"},
        "SERVICES": {"items", "eyebrow", "title", "subtitle"},
        "WHY_US": {"items", "eyebrow", "title", "subtitle"},
        "STATS": {"stats", "eyebrow", "title"},
        "HOW_IT_WORKS": {"steps"},
        "TESTIMONIALS": {"testimonials", "eyebrow", "title", "subtitle"},
        "PARTNERS": {"partners", "cta", "eyebrow", "title", "subtitle"},
        "CTA": {"title", "subtitle"},
        "FAQ": {"faqs"},
        "FEATURED_HOTELS": {"eyebrow", "title", "subtitle"},
        "POPULAR_DESTINATIONS": {"eyebrow", "title", "subtitle"},
        "TOUR_PACKAGES": {"eyebrow", "title", "subtitle"},
        "POPULAR_ROUTES": set(),
        "POPULAR_CITIES": set(),
        "OFFERS": set(),
        "BLOG": set(),
        "NEWSLETTER": set(),
        "APP_DOWNLOAD": set(),
    }

    # Derived from the ORM so a new column can never drift out of the guard.
    TYPED_SLOTS: frozenset[str] = frozenset(
        c.name for c in SectionVariant.__table__.columns if c.name != "content"
    )

    @classmethod
    def validate_content(cls, section_key: str, content: Optional[dict]) -> None:
        """Reject content that cannot render, before it is written.

        Called from create_variant / update_variant only. The read path
        deliberately does NOT validate, so rows already in the database keep
        rendering while the admin is made to fix them on their next save —
        same reject-on-write / tolerate-on-read split the landing-page CMS
        uses. An unknown section key fails open: a band added later must not
        be locked out of the editor by a stale whitelist here.
        """
        if not content:
            return
        allowed = cls.SECTION_CONTENT_KEYS.get(section_key.upper())
        if allowed is None:
            return
        for key in content:
            if key in cls.TYPED_SLOTS:
                raise ValidationException(
                    f"'{key}' is a field of this variant, not a content key — "
                    f"set it on the variant itself. Keys placed in content "
                    f"overwrite the matching field on the site.",
                    details={"field": f"content.{key}"},
                )
            if key not in allowed:
                if allowed:
                    hint = "Allowed keys: " + ", ".join(sorted(allowed)) + "."
                else:
                    hint = (
                        "This band builds its cards from live data, so it "
                        "takes no content keys — edit the records themselves "
                        "instead."
                    )
                raise ValidationException(
                    f"'{key}' is not read by the '{section_key}' section. {hint}",
                    details={"field": f"content.{key}"},
                )

    # ── Page Sections ────────────────────────────────────────────

    @staticmethod
    async def list_sections(db: AsyncSession) -> list[dict]:
        """Return every section template with variant count + active variant id."""
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(PageSection)
            .options(selectinload(PageSection.variants))
            .order_by(PageSection.display_order, PageSection.id)
        )
        sections = list(result.scalars().all())
        out = []
        for s in sections:
            active_id = next((v.id for v in s.variants if v.is_active), None)
            out.append(
                {
                    "id": s.id,
                    "section_key": s.section_key,
                    "display_name": s.display_name,
                    "description": s.description,
                    "icon": s.icon,
                    "display_order": s.display_order,
                    "is_visible": s.is_visible,
                    "is_active": s.is_active,
                    "variant_count": len(s.variants),
                    "active_variant_id": active_id,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
            )
        return out

    @staticmethod
    async def get_section_by_key(
        db: AsyncSession, section_key: str
    ) -> Optional[PageSection]:
        result = await db.execute(
            select(PageSection).where(PageSection.section_key == section_key)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _section_key_for(db: AsyncSession, page_section_id: int) -> Optional[str]:
        """Resolve the section_key (HERO, SERVICES, …) for a given page_section_id."""
        result = await db.execute(
            select(PageSection.section_key).where(PageSection.id == page_section_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _serialize_variant(variant: SectionVariant, section_key: str) -> dict:
        """
        Build a JSON-friendly dict that matches SectionVariantOut.

        SectionVariantOut requires `section_key` (the denormalized business
        key the frontend renders on), but the ORM model only carries
        `page_section_id`. We resolve section_key at write/read time and
        pass a plain dict into Pydantic so it validates cleanly.

        `overlay_opacity` is a `Numeric(3,2)` column on the ORM — SQLAlchemy
        hands it back as `Decimal`. The schema layer accepts Decimal, but
        callers that put this dict into a JSONB column (cms_audit_versions
        new_value / previous_value) crash with "Object of type Decimal is
        not JSON serializable". Coerce to float so the dict is safe for
        both Pydantic and JSONB.
        """
        return {
            "id": variant.id,
            "page_section_id": variant.page_section_id,
            "section_key": section_key,
            "variant_name": variant.variant_name,
            "variant_tag": variant.variant_tag,
            "display_order": variant.display_order,
            "is_active": variant.is_active,
            "headline": variant.headline,
            "subheadline": variant.subheadline,
            "body_text": variant.body_text,
            "cta_text": variant.cta_text,
            "cta_link": variant.cta_link,
            "background_image_url": variant.background_image_url,
            "background_video_url": variant.background_video_url,
            "mobile_image_url": variant.mobile_image_url,
            "icon_url": variant.icon_url,
            "accent_color": variant.accent_color,
            "animation_style": variant.animation_style,
            "text_alignment": variant.text_alignment,
            "overlay_opacity": (
                float(variant.overlay_opacity)
                if variant.overlay_opacity is not None
                else None
            ),
            "content": variant.content,
            "service_type": variant.service_type,
            "starts_at": variant.starts_at,
            "ends_at": variant.ends_at,
            "created_at": variant.created_at,
            "updated_at": variant.updated_at,
        }

    @staticmethod
    async def update_section(
        db: AsyncSession,
        section_key: str,
        payload: PageSectionUpdate,
        actor_id: Optional[str] = None,
    ) -> Optional[PageSection]:
        section = await CmsService.get_section_by_key(db, section_key)
        if not section:
            return None
        data = payload.model_dump(exclude_unset=True)
        for field, val in data.items():
            setattr(section, field, val)
        section.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await CmsService._audit(
            db,
            entity_type="PAGE_SECTION",
            entity_id=section.id,
            section_key=section.section_key,
            action_type="UPDATED",
            new_value=data,
            actor_id=actor_id,
        )
        await db.refresh(section)
        return section

    # ── Section Variants ─────────────────────────────────────────

    @staticmethod
    async def list_variants(db: AsyncSession, section_key: str) -> List[dict]:
        section = await CmsService.get_section_by_key(db, section_key)
        if not section:
            return []
        result = await db.execute(
            select(SectionVariant)
            .where(SectionVariant.page_section_id == section.id)
            .order_by(SectionVariant.display_order, SectionVariant.id)
        )
        return [
            CmsService._serialize_variant(v, section.section_key)
            for v in result.scalars().all()
        ]

    @staticmethod
    async def get_variant(db: AsyncSession, variant_id: int) -> Optional[dict]:
        result = await db.execute(
            select(SectionVariant).where(SectionVariant.id == variant_id)
        )
        variant = result.scalar_one_or_none()
        if not variant:
            return None
        section_key = await CmsService._section_key_for(db, variant.page_section_id)
        return CmsService._serialize_variant(variant, section_key or "")

    @staticmethod
    async def create_variant(
        db: AsyncSession,
        section_key: str,
        payload: SectionVariantCreate,
        actor_id: Optional[str] = None,
    ) -> Optional[dict]:
        section = await CmsService.get_section_by_key(db, section_key)
        if not section:
            return None
        CmsService.validate_content(section.section_key, payload.content)
        data = payload.model_dump()
        # section_variants carries a partial unique index
        # (uniq_variants_active_per_section) — one live variant per section — so
        # the incumbents have to be retired before this row is written as
        # active. It lands inactive first to earn an id, then switches on.
        want_active = bool(data["is_active"])
        variant = SectionVariant(
            page_section_id=section.id, **{**data, "is_active": False}
        )
        db.add(variant)
        await db.flush()
        if want_active:
            await CmsService._deactivate_other_variants(
                db, section.id, variant.id, actor_id=actor_id
            )
            variant.is_active = True
            await db.flush()
        await CmsService._audit(
            db,
            entity_type="SECTION_VARIANT",
            entity_id=variant.id,
            section_key=section.section_key,
            action_type="CREATED",
            new_value=data,
            actor_id=actor_id,
        )
        await db.refresh(variant)
        return CmsService._serialize_variant(variant, section.section_key)

    @staticmethod
    async def update_variant(
        db: AsyncSession,
        variant_id: int,
        payload: SectionVariantUpdate,
        actor_id: Optional[str] = None,
    ) -> Optional[dict]:
        variant_row = await db.execute(
            select(SectionVariant).where(SectionVariant.id == variant_id)
        )
        variant = variant_row.scalar_one_or_none()
        if not variant:
            return None
        section_key = await CmsService._section_key_for(db, variant.page_section_id)
        CmsService.validate_content(section_key or "", payload.content)
        old = {k: getattr(variant, k) for k in payload.model_fields_set}
        data = payload.model_dump(exclude_unset=True)
        for field, val in data.items():
            setattr(variant, field, val)
        variant.updated_at = datetime.now(timezone.utc)
        # Retire the incumbents before flushing, in the same transaction —
        # writing is_active=True first collides with the partial unique index
        # uniq_variants_active_per_section and the save 500s.
        if data.get("is_active") is True:
            await CmsService._deactivate_other_variants(
                db, variant.page_section_id, variant.id, actor_id=actor_id
            )
        await db.flush()
        await CmsService._audit(
            db,
            entity_type="SECTION_VARIANT",
            entity_id=variant.id,
            section_key=section_key,
            action_type="UPDATED",
            previous_value=old,
            new_value=data,
            actor_id=actor_id,
        )
        await db.refresh(variant)
        return CmsService._serialize_variant(variant, section_key or "")

    @staticmethod
    async def delete_variant(
        db: AsyncSession,
        variant_id: int,
        actor_id: Optional[str] = None,
    ) -> bool:
        variant_row = await db.execute(
            select(SectionVariant).where(SectionVariant.id == variant_id)
        )
        variant = variant_row.scalar_one_or_none()
        if not variant:
            return False
        section_key = await CmsService._section_key_for(db, variant.page_section_id)
        await db.delete(variant)
        await db.flush()
        await CmsService._audit(
            db,
            entity_type="SECTION_VARIANT",
            entity_id=variant_id,
            section_key=section_key,
            action_type="DELETED",
            actor_id=actor_id,
        )
        return True

    @staticmethod
    async def activate_variant(
        db: AsyncSession,
        variant_id: int,
        actor_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Make this variant the active one for its section."""
        variant_row = await db.execute(
            select(SectionVariant).where(SectionVariant.id == variant_id)
        )
        variant = variant_row.scalar_one_or_none()
        if not variant:
            return None
        variant.is_active = True
        variant.updated_at = datetime.now(timezone.utc)
        # Same ordering rule as update_variant: the partial unique index
        # permits one live variant per section, so the incumbent is retired
        # before this row's flip is flushed.
        await CmsService._deactivate_other_variants(
            db, variant.page_section_id, variant.id, actor_id=actor_id
        )
        await db.flush()
        section_key = await CmsService._section_key_for(db, variant.page_section_id)
        await CmsService._audit(
            db,
            entity_type="SECTION_VARIANT",
            entity_id=variant.id,
            section_key=section_key,
            action_type="ACTIVATED",
            actor_id=actor_id,
        )
        await db.refresh(variant)
        return CmsService._serialize_variant(variant, section_key or "")

    @staticmethod
    async def _deactivate_other_variants(
        db: AsyncSession,
        page_section_id: int,
        keep_id: int,
        actor_id: Optional[str] = None,
    ) -> None:
        """Turn is_active=False on every other variant of the same section."""
        await db.execute(
            sa_update(SectionVariant)
            .where(
                SectionVariant.page_section_id == page_section_id,
                SectionVariant.id != keep_id,
                SectionVariant.is_active == True,  # noqa: E712
            )
            .values(is_active=False, updated_at=datetime.now(timezone.utc))
        )

    # ── Site Header (singleton) ──────────────────────────────────

    @staticmethod
    async def get_header(db: AsyncSession) -> SiteHeaderConfig:
        result = await db.execute(
            select(SiteHeaderConfig).where(SiteHeaderConfig.singleton.is_(True))
        )
        header = result.scalar_one_or_none()
        if not header:
            header = SiteHeaderConfig(singleton=True, nav_links=[], social_links={})
            db.add(header)
            await db.flush()
            await db.refresh(header)
        return header

    @staticmethod
    async def update_header(
        db: AsyncSession,
        payload: SiteHeaderUpdate,
        actor_id: Optional[str] = None,
    ) -> SiteHeaderConfig:
        header = await CmsService.get_header(db)
        data = payload.model_dump(exclude_unset=True)
        for field, val in data.items():
            setattr(header, field, val)
        if actor_id:
            header.updated_by_user_id = actor_id
        header.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await CmsService._audit(
            db,
            entity_type="SITE_HEADER",
            entity_id=header.id,
            action_type="UPDATED",
            new_value=data,
            actor_id=actor_id,
        )
        await db.refresh(header)
        return header

    # ── Site Footer (singleton) ──────────────────────────────────

    @staticmethod
    async def get_footer(db: AsyncSession) -> SiteFooterConfig:
        result = await db.execute(
            select(SiteFooterConfig).where(SiteFooterConfig.singleton.is_(True))
        )
        footer = result.scalar_one_or_none()
        if not footer:
            footer = SiteFooterConfig(
                singleton=True,
                quick_links=[],
                legal_links=[],
                social_links={},
                payment_icons=[],
                app_store_links={},
            )
            db.add(footer)
            await db.flush()
            await db.refresh(footer)
        return footer

    @staticmethod
    async def update_footer(
        db: AsyncSession,
        payload: SiteFooterUpdate,
        actor_id: Optional[str] = None,
    ) -> SiteFooterConfig:
        footer = await CmsService.get_footer(db)
        data = payload.model_dump(exclude_unset=True)
        for field, val in data.items():
            setattr(footer, field, val)
        if actor_id:
            footer.updated_by_user_id = actor_id
        footer.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await CmsService._audit(
            db,
            entity_type="SITE_FOOTER",
            entity_id=footer.id,
            action_type="UPDATED",
            new_value=data,
            actor_id=actor_id,
        )
        await db.refresh(footer)
        return footer

    # ── Public Homepage Bundle ───────────────────────────────────

    # ── Default fallback values ──────────────────────────────────
    # Applied when the DB singleton row has null / empty values.
    # Admin can override any of these from the CMS panel.
    _HEADER_DEFAULTS: dict = {
        "logo_url": None,  # frontend uses SVG wordmark fallback
        "logo_alt_text": "WayTero",
        "tagline": "Explore More. Travel Better.",
        "show_search_bar": True,
        "show_login_button": True,
        "cta_text": "Book a Cab",
        "cta_link": "/cabs",
        # Empty on purpose: the real number lives in
        # system_configurations.SUPPORT_PHONE (migration 0072); the website
        # shows its brand-master fallback when nothing is configured.
        "support_phone": "",
        "contact_email": "support@waytero.com",
        "nav_links": [
            {"label": "Cabs", "href": "/cabs", "icon": "car"},
            {"label": "Hotels", "href": "/hotels", "icon": "hotel"},
            {"label": "Tours", "href": "/tours", "icon": "compass"},
            {"label": "Track", "href": "/track", "icon": "navigation"},
        ],
        # Owner-approved official profiles only (WAYTERO-PROJECT-MASTER).
        "social_links": {
            "youtube": "https://www.youtube.com/@WayTero",
            "instagram": "https://www.instagram.com/waytero.official/",
            "facebook": "https://www.facebook.com/WayTero",
        },
        "background_color": None,
        "text_color": None,
        "is_active": True,
    }

    _FOOTER_DEFAULTS: dict = {
        "logo_url": None,
        "description": (
            "India's Travel Operating System — connecting customers, partners, "
            "drivers, hotels, and tour operators on one seamless platform."
        ),
        # No invented company registration numbers — plain brand line.
        "copyright_text": "WayTero · waytero.com · All rights reserved.",
        "company_address": "Avinue Appartment, Gothapatana, Bhubaneswar - 751003",
        "support_phone": "",  # real value: system_configurations.SUPPORT_PHONE
        "contact_email": "support@waytero.com",
        "quick_links": [
            {"label": "Book a Cab", "href": "/cabs"},
            {"label": "Find Hotels", "href": "/hotels"},
            {"label": "Tour Packages", "href": "/tours"},
            {"label": "Live Tracking", "href": "/track"},
            {"label": "Travel Wallet", "href": "/wallet"},
        ],
        "legal_links": [
            {"label": "Privacy Policy", "href": "/privacy"},
            {"label": "Terms of Service", "href": "/terms"},
            {"label": "Refund Policy", "href": "/refund"},
            {"label": "Cookie Policy", "href": "/cookies"},
        ],
        "social_links": {
            "youtube": "https://www.youtube.com/@WayTero",
            "instagram": "https://www.instagram.com/waytero.official/",
            "facebook": "https://www.facebook.com/WayTero",
        },
        "payment_icons": [],
        "app_store_links": {},
        "background_color": None,
        "text_color": None,
        "is_active": True,
    }

    @staticmethod
    def _merge_header_defaults(header: "SiteHeaderConfig") -> dict:
        """
        Merge DB header values with hardcoded defaults.
        DB values take priority; null/empty fields fall back to defaults
        so the frontend always receives sensible data even before the admin
        configures anything in the CMS panel.
        """
        d = CmsService._HEADER_DEFAULTS
        return {
            "id": header.id,
            "logo_url": header.logo_url or d["logo_url"],
            "logo_alt_text": header.logo_alt_text or d["logo_alt_text"],
            "tagline": header.tagline or d["tagline"],
            "show_search_bar": (
                header.show_search_bar
                if header.show_search_bar is not None
                else d["show_search_bar"]
            ),
            "show_login_button": (
                header.show_login_button
                if header.show_login_button is not None
                else d["show_login_button"]
            ),
            "cta_text": header.cta_text or d["cta_text"],
            "cta_link": header.cta_link or d["cta_link"],
            "support_phone": header.support_phone or d["support_phone"],
            "contact_email": header.contact_email or d["contact_email"],
            "nav_links": header.nav_links if header.nav_links else d["nav_links"],
            "social_links": (
                header.social_links if header.social_links else d["social_links"]
            ),
            "background_color": header.background_color or d["background_color"],
            "text_color": header.text_color or d["text_color"],
            "is_active": (
                header.is_active if header.is_active is not None else d["is_active"]
            ),
            "updated_at": header.updated_at,
        }

    @staticmethod
    def _merge_footer_defaults(footer: "SiteFooterConfig") -> dict:
        """
        Merge DB footer values with hardcoded defaults.
        DB values take priority; null/empty fields fall back to defaults.
        """
        d = CmsService._FOOTER_DEFAULTS
        return {
            "id": footer.id,
            "logo_url": footer.logo_url or d["logo_url"],
            "description": footer.description or d["description"],
            "copyright_text": footer.copyright_text or d["copyright_text"],
            "company_address": footer.company_address or d["company_address"],
            "support_phone": footer.support_phone or d["support_phone"],
            "contact_email": footer.contact_email or d["contact_email"],
            "quick_links": (
                footer.quick_links if footer.quick_links else d["quick_links"]
            ),
            "legal_links": (
                footer.legal_links if footer.legal_links else d["legal_links"]
            ),
            "social_links": (
                footer.social_links if footer.social_links else d["social_links"]
            ),
            "payment_icons": (
                footer.payment_icons
                if footer.payment_icons is not None
                else d["payment_icons"]
            ),
            "app_store_links": (
                footer.app_store_links
                if footer.app_store_links is not None
                else d["app_store_links"]
            ),
            "background_color": footer.background_color or d["background_color"],
            "text_color": footer.text_color or d["text_color"],
            "is_active": (
                footer.is_active if footer.is_active is not None else d["is_active"]
            ),
            "updated_at": footer.updated_at,
        }

    @staticmethod
    def reviewer_display_name(
        source: Optional[str],
        guest_name: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> str:
        """
        Public name for a homepage review.

        A logged-out traveller typed `guest_name` themselves, so it can be
        shown as written. A registered customer's name was collected to
        identify a booking, not to headline a marketing band, so it is
        shortened — the platform publishes no more of a customer's identity
        than the review needs.
        """
        if (source or "").upper() == "GUEST_WEB":
            return (guest_name or "").strip() or "WayTero customer"
        first = (first_name or "").strip()
        last = (last_name or "").strip()
        if first and last:
            return f"{first} {last[0]}."
        return first or "WayTero customer"

    @staticmethod
    async def picker_items(db: AsyncSession, picker_type: str) -> dict:
        """
        Records that fill the admin CMS content editor from live data.
        Doc Ref: BRD Part 6 §155, Website CMS.

        `picker_type` maps a picker to the homepage band it feeds:
          SERVICES     → SERVICES        `{items:[…]}`
          REVIEWS      → TESTIMONIALS    `{testimonials:[…]}`
          PARTNERS     → PARTNERS        `{partners:[…]}`
          HOTELS / COUPONS / DESTINATIONS are still served, but the bands
          that used them now build their cards live, so `validate_content`
          rejects a content array for them and the editor offers no button.

        Item keys are the names the public renderer reads, so a picked
        record lands in a typed row without remapping. Keys that only the
        picker dialog shows are dropped when the editor projects the row.

        Every branch imports its own models: a branch-level import binds the
        name across the whole function, so reading `City` without importing it
        in that branch raises UnboundLocalError instead of using the
        module-level one.
        """
        from sqlalchemy import func, nullslast
        from datetime import date as _date

        pt = (picker_type or "").upper()
        items: list[dict] = []

        if pt == "HOTELS":
            from app.modules.hotel.models import Hotel, HotelImage
            from app.modules.master.models import City, State

            result = await db.execute(
                select(Hotel, City.name, State.name)
                .join(City, City.id == Hotel.city_id)
                .outerjoin(State, State.id == City.state_id)
                .where(Hotel.status.in_(["ACTIVE", "APPROVED"]))
                .order_by(
                    Hotel.is_featured.desc(),
                    Hotel.display_order,
                    Hotel.hotel_name,
                )
                .limit(50)
            )
            rows = result.all()

            primary_images: dict[int, str] = {}
            if rows:
                hotel_ids = [h.id for h, _, _ in rows]
                img_result = await db.execute(
                    select(HotelImage.hotel_id, HotelImage.image_url)
                    .where(
                        HotelImage.hotel_id.in_(hotel_ids),
                        HotelImage.is_primary == True,  # noqa: E712
                    )
                    .order_by(HotelImage.hotel_id, HotelImage.display_order)
                )
                for hid, url in img_result.all():
                    primary_images.setdefault(hid, url)

            for hotel, city, state in rows:
                items.append(
                    {
                        "name": hotel.hotel_name,
                        "city": city or "",
                        "state": state,
                        "rating": hotel.star_rating or 4.0,
                        "reviews": 0,
                        "price": 0,
                        "old_price": None,
                        "amenities": [],
                        "tag": "Featured" if hotel.is_featured else None,
                        "image_url": primary_images.get(hotel.id),
                        "link": "/hotels",
                    }
                )

        elif pt == "COUPONS":
            from app.modules.admin.coupon_models import Coupon

            today = _date.today()
            result = await db.execute(
                select(Coupon)
                .where(
                    Coupon.is_active == True,  # noqa: E712
                    Coupon.valid_from <= today,
                    Coupon.valid_to >= today,
                )
                .order_by(Coupon.valid_to, Coupon.id)
                .limit(50)
            )
            for c in result.scalars().all():
                raw = str(c.discount_value)
                value = raw.rstrip("0").rstrip(".") if "." in raw else raw
                badge = (
                    f"{value}% OFF"
                    if c.discount_type == "PERCENTAGE"
                    else f"₹{value} OFF"
                )
                items.append(
                    {
                        "title": c.title,
                        "description": c.description
                        or f"Use code {c.coupon_code} at checkout.",
                        "badge": badge,
                        "cta_text": f"Use code {c.coupon_code}",
                        "cta_link": "/cabs",
                        "image_url": None,
                    }
                )

        elif pt == "DESTINATIONS":
            from app.modules.master.models import City, State

            result = await db.execute(
                select(City, State.name)
                .join(State, State.id == City.state_id)
                .where(City.is_active == True)  # noqa: E712
                .order_by(City.name)
                .limit(60)
            )
            for city, state in result.all():
                items.append(
                    {
                        "name": city.name,
                        "state": state or "",
                        "starting_price": 0,
                        "trending": False,
                        "image_url": None,
                    }
                )

        elif pt == "SERVICES":
            from app.modules.master.models import ServiceType

            result = await db.execute(
                select(ServiceType)
                .where(ServiceType.is_active == True)  # noqa: E712
                .order_by(ServiceType.display_order, ServiceType.type_code)
            )
            for st in result.scalars().all():
                items.append(
                    {
                        "title": st.label,
                        "desc": st.description or "",
                        "link": SERVICE_TYPE_ROUTES.get(
                            st.type_code, f"/{st.type_code.lower()}"
                        ),
                    }
                )

        elif pt == "REVIEWS":
            from app.modules.booking.models import BookingReview, MasterBooking
            from app.modules.customer.models import Customer
            from app.modules.master.models import City

            result = await db.execute(
                select(
                    BookingReview.review,
                    BookingReview.rating,
                    BookingReview.source,
                    BookingReview.guest_name,
                    Customer.first_name,
                    Customer.last_name,
                    City.name,
                )
                .join(MasterBooking, MasterBooking.id == BookingReview.master_booking_id)
                .join(Customer, Customer.id == BookingReview.customer_id)
                .outerjoin(City, City.id == MasterBooking.city_id)
                .where(
                    BookingReview.status == "APPROVED",
                    BookingReview.review.isnot(None),
                    func.trim(BookingReview.review) != "",
                )
                .order_by(
                    nullslast(BookingReview.moderated_at.desc()),
                    BookingReview.id.desc(),
                )
                .limit(30)
            )
            # Only the columns above are read: guest_mobile, guest_email and the
            # booking reference stay in the table.
            for review, rating, source, guest_name, first, last, city in result.all():
                text = " ".join(str(review).split())
                items.append(
                    {
                        "name": CmsService.reviewer_display_name(
                            source, guest_name, first, last
                        ),
                        "city": city or None,
                        "text": text,
                        "rating": int(rating) if rating is not None else None,
                        "badge": f"{rating}/5" if rating is not None else None,
                    }
                )

        elif pt == "PARTNERS":
            from app.modules.partner.models import Partner
            from app.modules.master.models import City

            result = await db.execute(
                select(Partner.business_name, Partner.owner_name, City.name)
                .outerjoin(City, City.id == Partner.city_id)
                .where(
                    Partner.status.in_(["APPROVED", "ACTIVE"]),
                    Partner.deleted_at.is_(None),
                )
                .order_by(
                    nullslast(Partner.approved_at.desc()),
                    Partner.id,
                )
                .limit(40)
            )
            for business_name, owner_name, city in result.all():
                brand = (business_name or owner_name or "").strip()
                if not brand:
                    continue
                # `partners` stores plain strings, so the editor maps this
                # row's `name` into the list and ignores the rest.
                items.append({"name": brand, "city": city or ""})

        else:
            raise ValidationException(
                f"Unknown content picker type '{picker_type}'.",
                details={"picker_type": pt},
            )

        return {"type": pt, "items": items}

    @staticmethod
    async def build_public_homepage(db: AsyncSession) -> PublicHomepageOut:
        """
        What customer-web actually renders:
          - header + footer (always, with fallback defaults for null DB fields)
          - visible sections in display_order whose active variant is
            currently inside its scheduling window (or has no window).
        """
        header = await CmsService.get_header(db)
        footer = await CmsService.get_footer(db)

        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(PageSection)
            .options(selectinload(PageSection.variants))
            .where(
                PageSection.is_visible == True,  # noqa: E712
                PageSection.is_active == True,  # noqa: E712
            )
            .order_by(PageSection.display_order, PageSection.id)
        )
        sections = list(result.scalars().all())

        now = datetime.now(timezone.utc)
        out_sections: list[dict] = []
        for s in sections:
            active = next((v for v in s.variants if v.is_active), None)
            if not active:
                continue
            if (
                active.starts_at
                and active.ends_at
                and not (active.starts_at <= now <= active.ends_at)
            ):
                continue
            variant_payload = {
                "id": active.id,
                "variant_name": active.variant_name,
                "variant_tag": active.variant_tag,
                "headline": active.headline,
                "subheadline": active.subheadline,
                "body_text": active.body_text,
                "cta_text": active.cta_text,
                "cta_link": active.cta_link,
                "background_image_url": active.background_image_url,
                "background_video_url": active.background_video_url,
                "mobile_image_url": active.mobile_image_url,
                "icon_url": active.icon_url,
                "accent_color": active.accent_color,
                "animation_style": active.animation_style,
                "text_alignment": active.text_alignment,
                "overlay_opacity": (
                    float(active.overlay_opacity)
                    if active.overlay_opacity is not None
                    else None
                ),
                # Drives which search form the customer-web renders
                # inside the section (CAB | HOTEL | TOUR | ALL).
                "service_type": active.service_type,
            }
            # Structured content is spread at the top level of the
            # variant payload (plus kept under `content`) so renderers
            # read `pick(variant, "steps" | "offers" | "faqs" | …)`
            # exactly like the typed slots.
            if active.content:
                variant_payload["content"] = active.content
                variant_payload.update(active.content)
            out_sections.append(
                {
                    "id": s.id,
                    "section_key": s.section_key,
                    "display_name": s.display_name,
                    "icon": s.icon,
                    "display_order": s.display_order,
                    "variant": variant_payload,
                }
            )

        # Apply fallback defaults for any null/empty DB fields before serialising.
        header_data = CmsService._merge_header_defaults(header)
        footer_data = CmsService._merge_footer_defaults(footer)

        # The admin-saved contact number under Settings → Platform Details
        # (system_configurations.SUPPORT_PHONE) is the canonical support line.
        # Override the CMS header/footer phones so the entire public site
        # follows one number; CMS keeps its own value only as a fallback.
        from sqlalchemy import text as _text

        settings_row = (
            await db.execute(
                _text(
                    "SELECT config_value FROM system_configurations "
                    "WHERE config_key = 'SUPPORT_PHONE'"
                )
            )
        ).first()
        settings_phone = (settings_row[0] or "").strip() if settings_row else ""
        if settings_phone:
            header_data["support_phone"] = settings_phone
            footer_data["support_phone"] = settings_phone

        return PublicHomepageOut(
            header=SiteHeaderOut.model_validate(header_data),
            footer=SiteFooterOut.model_validate(footer_data),
            sections=out_sections,
        )

    # ── Audit helper ─────────────────────────────────────────────

    @staticmethod
    def _jsonify(value):
        """
        Recursively coerce a value into something stdlib `json.dumps`
        can handle. Used to scrub `previous_value` / `new_value` dicts
        before they hit a JSONB column — `overlay_opacity` (Numeric on
        the ORM) returns as `Decimal`, which json.dumps rejects.

        Coerces:
            Decimal → float   (lossless for opacity / pricing fields
                               bounded to 4 decimal places)
            datetime/date    → ISO-8601 string
            UUID             → string
            set/frozenset    → sorted list
            (dict | list | tuple) → recursed
        """
        from datetime import date, datetime
        from decimal import Decimal
        from uuid import UUID

        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, dict):
            return {k: CmsService._jsonify(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [CmsService._jsonify(v) for v in value]
        # Fallback — let json.dumps raise the explicit TypeError so the
        # error message names the offending type.
        return value

    @staticmethod
    async def _audit(
        db: AsyncSession,
        *,
        entity_type: str,
        entity_id: int,
        action_type: str,
        section_key: Optional[str] = None,
        previous_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        actor_id: Optional[str] = None,
    ) -> None:
        from uuid import UUID

        actor_uuid: Optional[UUID] = None
        if actor_id:
            try:
                actor_uuid = UUID(str(actor_id))
            except (ValueError, TypeError):
                actor_uuid = None
        entry = CmsAuditVersion(
            entity_type=entity_type,
            entity_id=entity_id,
            section_key=section_key,
            action_type=action_type,
            previous_value=(
                CmsService._jsonify(previous_value)
                if previous_value is not None
                else None
            ),
            new_value=(
                CmsService._jsonify(new_value) if new_value is not None else None
            ),
            changed_by_user_id=actor_uuid,
        )
        db.add(entry)


# ════════════════════════════════════════════════════════════════
# BLOG SYSTEM — Blog Post Service
# Doc Ref: Blog System §1 — Database Schema
#          Migration 0051_blog_posts
# ════════════════════════════════════════════════════════════════


class BlogService:
    """CRUD service for blog posts — used by both admin and public routes."""

    @staticmethod
    def _apply_filters(
        q,
        published_only: bool = False,
        status: Optional[str] = None,
        search: Optional[str] = None,
        tag: Optional[str] = None,
    ):
        """
        Shared WHERE-clause builder for list/count queries.

        ``status`` is the admin-facing tri-state filter:
          - "published" → only published posts
          - "draft"     → only unpublished posts
          - None/"all"  → both (honoured unless ``published_only`` is set,
                          which is the public-API signal)
        """
        if status == "draft":
            q = q.where(BlogPost.is_published.is_(False))
        elif status == "published" or published_only:
            q = q.where(BlogPost.is_published.is_(True))

        if search:
            q = q.where(
                BlogPost.title.ilike(f"%{search}%")
                | BlogPost.excerpt.ilike(f"%{search}%")
            )

        if tag:
            q = q.where(BlogPost.tags.contains([tag]))

        return q

    @staticmethod
    async def list_posts(
        db: AsyncSession,
        published_only: bool = False,
        status: Optional[str] = None,
        search: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BlogPost]:
        q = BlogService._apply_filters(
            select(BlogPost),
            published_only=published_only,
            status=status,
            search=search,
            tag=tag,
        )
        q = q.order_by(BlogPost.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def count_posts(
        db: AsyncSession,
        published_only: bool = False,
        status: Optional[str] = None,
        search: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> int:
        from sqlalchemy import func

        q = BlogService._apply_filters(
            select(func.count(BlogPost.id)),
            published_only=published_only,
            status=status,
            search=search,
            tag=tag,
        )
        result = await db.execute(q)
        return result.scalar() or 0

    @staticmethod
    async def list_tags(db: AsyncSession) -> list[str]:
        """Deduplicated, ordered list of every tag in use (any publish state)."""
        from sqlalchemy import text

        result = await db.execute(
            text(
                """
                SELECT DISTINCT tag
                FROM blog_posts, jsonb_array_elements_text(tags) AS tag
                ORDER BY tag
                """
            )
        )
        return [row[0] for row in result.all()]

    @staticmethod
    async def get_by_id(db: AsyncSession, post_id: int) -> Optional[BlogPost]:
        result = await db.execute(select(BlogPost).where(BlogPost.id == post_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_slug(db: AsyncSession, slug: str) -> Optional[BlogPost]:
        result = await db.execute(select(BlogPost).where(BlogPost.slug == slug))
        return result.scalar_one_or_none()

    @staticmethod
    async def create(
        db: AsyncSession, payload: BlogPostCreate, created_by: Optional[str] = None
    ) -> BlogPost:
        published_at = payload.published_at
        if payload.is_published and not published_at:
            published_at = datetime.now(timezone.utc)

        post = BlogPost(
            title=payload.title,
            slug=payload.slug,
            excerpt=payload.excerpt,
            content=payload.content,
            featured_image_url=payload.featured_image_url,
            author_name=payload.author_name,
            author_avatar_url=payload.author_avatar_url,
            tags=payload.tags or [],
            is_published=payload.is_published,
            published_at=published_at,
            seo_title=payload.seo_title,
            seo_description=payload.seo_description,
            seo_keywords=payload.seo_keywords,
            created_by=created_by,
        )
        db.add(post)
        await db.flush()
        await db.refresh(post)
        return post

    @staticmethod
    async def update(
        db: AsyncSession, post_id: int, payload: BlogPostUpdate
    ) -> Optional[BlogPost]:
        result = await db.execute(select(BlogPost).where(BlogPost.id == post_id))
        post = result.scalar_one_or_none()
        if not post:
            return None

        update_data = payload.model_dump(exclude_unset=True)
        for field, val in update_data.items():
            setattr(post, field, val)

        # Publishing via an update stamps published_at if it has never been set.
        if update_data.get("is_published") is True and not post.published_at:
            post.published_at = datetime.now(timezone.utc)

        post.updated_at = datetime.now(timezone.utc)
        await db.flush()
        await db.refresh(post)
        return post

    @staticmethod
    async def delete(db: AsyncSession, post_id: int) -> bool:
        result = await db.execute(select(BlogPost).where(BlogPost.id == post_id))
        post = result.scalar_one_or_none()
        if not post:
            return False
        await db.delete(post)
        return True
