# ============================================================
# WAY TERO — HOTEL SERVICES
# File: app/modules/hotel/services/__init__.py
# Doc Ref: BRD Part 4 §57-92, SRS Part 5 §153-194, API Doc 09_HOTEL_API
#
# HotelService             — the five build stages an admin edits
# HotelReadinessService    — one computation behind both the submit gate and the
#                            UI completeness meter, so the two cannot disagree
# HotelVerificationService — the status machine and its audit trail
#
# Nothing here commits: get_db owns the transaction.
# ============================================================

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    DuplicateResourceException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.hotel.constants import (
    ACTION_COMMISSION_UPDATED,
    ACTION_DOCUMENT_REJECTED,
    ACTION_DOCUMENT_VERIFIED,
    ACTION_DOCUMENTS_REQUESTED,
    ACTION_HOTEL_ACTIVATED,
    ACTION_HOTEL_APPROVED,
    ACTION_HOTEL_APPROVED_OWN_RISK,
    ACTION_HOTEL_BLOCKED,
    ACTION_HOTEL_CREATED,
    ACTION_HOTEL_DEACTIVATED,
    ACTION_HOTEL_REJECTED,
    ACTION_HOTEL_SUBMITTED,
    ACTION_HOTEL_SUSPENDED,
    ACTION_HOTEL_UPDATED,
    ACTION_OFFICER_ASSIGNED,
    ACTION_OFFICER_UNASSIGNED,
    ACTION_REVIEW_STARTED,
    ACTION_TAX_UPDATED,
    COMMISSION_SOURCE_CITY_RULE,
    COMMISSION_SOURCE_GLOBAL_RULE,
    COMMISSION_SOURCE_HOTEL_OVERRIDE,
    COMMISSION_SOURCE_SYSTEM_DEFAULT,
    COMMISSION_TYPE_FLAT,
    COMMISSION_TYPE_PERCENTAGE,
    CONFIG_GST_ENABLED,
    CONFIG_HOTEL_AUTO_APPROVE_ENABLED,
    CONFIG_HOTEL_CODE_PREFIX,
    CONFIG_HOTEL_DEFAULT_COMMISSION,
    CONFIG_HOTEL_MIN_IMAGES_REQUIRED,
    CONFIRMATION_MODES,
    DOC_GST_CERTIFICATE,
    DOC_STATUS_PENDING,
    DOC_STATUS_REJECTED,
    DOC_STATUS_VERIFIED,
    ERR_INVALID_TRANSITION,
    ERR_NOT_READY_FOR_SUBMISSION,
    ERR_PARTNER_SERVICE_MISSING,
    HOTEL_DOCUMENT_TYPES,
    HOTEL_IMAGE_TYPES,
    HOTEL_PIPELINE_STATUSES,
    HOTEL_REQUIRED_DOCUMENT_TYPES,
    HOTEL_STATUS_ACTIVE,
    HOTEL_STATUS_APPROVED,
    HOTEL_STATUS_BLOCKED,
    HOTEL_STATUS_DOCUMENT_PENDING,
    HOTEL_STATUS_DRAFT,
    HOTEL_STATUS_INACTIVE,
    HOTEL_STATUS_PENDING,
    HOTEL_STATUS_REJECTED,
    HOTEL_STATUS_SUSPENDED,
    HOTEL_STATUS_UNDER_REVIEW,
    HOTEL_TAX_MODES,
    ROOM_ALLOCATION_MODES,
    VALID_HOTEL_TRANSITIONS,
)
from app.modules.hotel.models import (
    Hotel,
    HotelCommissionConfig,
    HotelDocument,
    HotelImage,
    HotelPerformanceSummary,
    HotelPolicy,
    HotelRating,
    HotelVerificationAssignment,
    HotelVerificationLog,
)
from app.modules.hotel.repositories import (
    HotelMasterRepository,
    HotelRepository,
    HotelVerificationRepository,
    InventoryRepository,
    RoomCategoryRepository,
)
from app.modules.hotel.services.pricing import CommissionConfigInput, compute_commission
from app.modules.hotel.validators import (
    slugify,
    validate_commission_values,
    validate_coordinates,
    validate_gst_number,
    validate_mobile,
    validate_pan_number,
    validate_pincode,
    validate_refund_ladder,
    validate_slug,
    validate_star_rating,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# CONFIG
# ============================================================


async def get_config(
    db: AsyncSession, key: str, default: Optional[str] = None
) -> Optional[str]:
    """Read one system_configurations value.

    Tax rates, commission defaults and the inventory horizon are seeded rows,
    editable at runtime — never constants in code.
    """
    value = (
        await db.execute(
            text(
                "SELECT config_value FROM system_configurations WHERE config_key = :k"
            ),
            {"k": key},
        )
    ).scalar_one_or_none()
    return default if value is None else str(value)


async def get_config_bool(db: AsyncSession, key: str, default: bool = False) -> bool:
    raw = await get_config(db, key)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


async def get_config_int(db: AsyncSession, key: str, default: int) -> int:
    raw = await get_config(db, key)
    try:
        return int(str(raw).strip()) if raw is not None else default
    except ValueError:
        return default


async def get_config_decimal(db: AsyncSession, key: str, default: Decimal) -> Decimal:
    raw = await get_config(db, key)
    try:
        return Decimal(str(raw).strip()) if raw is not None else default
    except (ArithmeticError, ValueError):
        return default


async def platform_gst_enabled(db: AsyncSession) -> bool:
    """The global switch. While it is off the platform raises no tax invoice and
    every per-hotel tax_mode is inert."""
    return await get_config_bool(db, CONFIG_GST_ENABLED, False)


# ============================================================
# COMMISSION RESOLUTION
# ============================================================


async def resolve_hotel_commission(
    db: AsyncSession, hotel: Hotel, on_date: Optional[date] = None
) -> CommissionConfigInput:
    """Resolve the commission in force for a hotel, most specific first:

        1. this hotel's override      (the only tier that can express HYBRID)
        2. commission_rules HOTEL for the hotel's city
        3. commission_rules HOTEL with no city
        4. HOTEL_DEFAULT_COMMISSION_PERCENT

    A parallel path to the cab resolver rather than a refactor of it — that code
    hardcodes service_type CAB and drives live payouts.
    """
    on_date = on_date or date.today()

    override = await HotelRepository(db).get_active_commission(hotel.id, on_date)
    if override is not None:
        return CommissionConfigInput(
            commission_type=str(override.commission_type),
            commission_percent=Decimal(override.commission_percent or 0),
            commission_flat=Decimal(override.commission_flat or 0),
            min_commission=(
                Decimal(override.min_commission)
                if override.min_commission is not None
                else None
            ),
            max_commission=(
                Decimal(override.max_commission)
                if override.max_commission is not None
                else None
            ),
            applies_to=str(override.applies_to),
            source=COMMISSION_SOURCE_HOTEL_OVERRIDE,
            config_id=int(override.id),
        )

    # commission_rules carries one type and one value, so tiers 2 and 3 can never
    # be HYBRID — precisely why the override table exists.
    for city_clause, source in (
        ("city_id = :city_id", COMMISSION_SOURCE_CITY_RULE),
        ("city_id IS NULL", COMMISSION_SOURCE_GLOBAL_RULE),
    ):
        row = (
            (
                await db.execute(
                    text(
                        "SELECT id, commission_type, commission_value "
                        "FROM commission_rules "
                        "WHERE service_type = 'HOTEL' AND is_active = TRUE "
                        f"  AND {city_clause} "
                        "  AND (effective_from IS NULL OR effective_from <= :d) "
                        "  AND (effective_to IS NULL OR effective_to >= :d) "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"city_id": hotel.city_id, "d": on_date},
                )
            )
            .mappings()
            .first()
        )
        if row:
            is_flat = str(row["commission_type"] or "").upper() == COMMISSION_TYPE_FLAT
            value = Decimal(row["commission_value"] or 0)
            return CommissionConfigInput(
                commission_type=(
                    COMMISSION_TYPE_FLAT if is_flat else COMMISSION_TYPE_PERCENTAGE
                ),
                commission_percent=Decimal("0") if is_flat else value,
                commission_flat=value if is_flat else Decimal("0"),
                source=source,
                config_id=int(row["id"]),
            )

    return CommissionConfigInput(
        commission_type=COMMISSION_TYPE_PERCENTAGE,
        commission_percent=await get_config_decimal(
            db, CONFIG_HOTEL_DEFAULT_COMMISSION, Decimal("12")
        ),
        commission_flat=Decimal("0"),
        source=COMMISSION_SOURCE_SYSTEM_DEFAULT,
    )


def commission_worked_example(
    config: CommissionConfigInput, sample_amount: Decimal = Decimal("10000")
) -> dict[str, Any]:
    """A concrete number beside the configuration. "HYBRID, 5%, Rs.200" is far
    harder to sanity-check than "on Rs.10,000 the partner keeps Rs.9,300"."""
    result = compute_commission(sample_amount, config, room_nights=1)
    return {
        "sample_amount": str(sample_amount),
        "commission_amount": str(result.commission_amount),
        "partner_payout": str(result.partner_payout),
        "was_clamped": result.was_clamped,
    }


def resolved_commission_payload(config: CommissionConfigInput) -> dict[str, Any]:
    return {
        "commission_type": config.commission_type,
        "commission_percent": config.commission_percent,
        "commission_flat": config.commission_flat,
        "min_commission": config.min_commission,
        "max_commission": config.max_commission,
        "applies_to": config.applies_to,
        "source": config.source,
        "config_id": config.config_id,
        "example": commission_worked_example(config),
    }


# ============================================================
# HOTEL SERVICE
# ============================================================


class HotelService:
    """Stages 1-5: everything an admin edits before verification."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = HotelRepository(db)
        self.masters = HotelMasterRepository(db)
        self.verification = HotelVerificationRepository(db)

    # --- helpers ---

    async def get_or_404(self, hotel_id: int) -> Hotel:
        hotel = await self.repo.get_by_id(hotel_id)
        if hotel is None:
            raise ResourceNotFoundException("Hotel", hotel_id)
        return hotel

    async def _log(
        self,
        hotel_id: int,
        action: str,
        performed_by: Optional[UUID],
        remarks: Optional[str] = None,
    ) -> None:
        await self.verification.add_log(
            HotelVerificationLog(
                hotel_id=hotel_id,
                action=action,
                remarks=remarks,
                performed_by=performed_by,
            )
        )

    async def _assert_partner_offers_hotels(self, partner_id: int) -> None:
        """A partner may own hotels only while holding an active HOTEL service.

        Not granted silently here: the partner-services endpoint writes its own
        audit record, and an implicit insert would create the grant with none.
        """
        exists = (
            await self.db.execute(
                text(
                    "SELECT 1 FROM partners p "
                    "WHERE p.id = :pid AND p.deleted_at IS NULL "
                    "  AND EXISTS (SELECT 1 FROM partner_services ps "
                    "              WHERE ps.partner_id = p.id "
                    "                AND ps.service_type = 'HOTEL' "
                    "                AND ps.is_active = TRUE)"
                ),
                {"pid": partner_id},
            )
        ).scalar_one_or_none()

        if exists is None:
            raise BusinessException(
                "This partner does not offer the HOTEL service. Assign the HOTEL "
                "service to the partner before adding a hotel.",
                code=ERR_PARTNER_SERVICE_MISSING,
                details={"partner_id": partner_id},
            )

    async def _city_name(self, city_id: int) -> Optional[str]:
        return (
            await self.db.execute(
                text("SELECT name FROM cities WHERE id = :cid"), {"cid": city_id}
            )
        ).scalar_one_or_none()

    async def _state_of_city(self, city_id: int) -> Optional[int]:
        return (
            await self.db.execute(
                text("SELECT state_id FROM cities WHERE id = :cid"), {"cid": city_id}
            )
        ).scalar_one_or_none()

    async def _unique_slug(self, base: str) -> str:
        candidate = base or "hotel"
        suffix = 1
        while await self.repo.slug_exists(candidate):
            suffix += 1
            candidate = f"{base}-{suffix}"
        return candidate

    # --- Stage 1: create ---

    async def create_hotel(self, payload: Any, actor_id: Optional[UUID]) -> Hotel:
        await self._assert_partner_offers_hotels(payload.partner_id)

        if await self.repo.name_exists_in_city(payload.hotel_name, payload.city_id):
            raise DuplicateResourceException("Hotel", "name in this city")

        city_name = await self._city_name(payload.city_id)
        if city_name is None:
            raise ResourceNotFoundException("City", payload.city_id)

        if (
            payload.hotel_category_id is not None
            and await self.masters.get_category(payload.hotel_category_id) is None
        ):
            raise ResourceNotFoundException("Hotel category", payload.hotel_category_id)

        prefix = await get_config(self.db, CONFIG_HOTEL_CODE_PREFIX, "HTL") or "HTL"

        hotel = Hotel(
            partner_id=payload.partner_id,
            hotel_code=await self.repo.next_hotel_code(prefix),
            hotel_name=payload.hotel_name.strip(),
            hotel_type=payload.hotel_type,
            hotel_category_id=payload.hotel_category_id,
            star_rating=validate_star_rating(payload.star_rating),
            city_id=payload.city_id,
            state_id=payload.state_id or await self._state_of_city(payload.city_id),
            address=payload.address,
            contact_person=payload.contact_person,
            contact_number=validate_mobile(payload.contact_number),
            email=payload.email,
            slug=await self._unique_slug(slugify(f"{payload.hotel_name} {city_name}")),
            status=HOTEL_STATUS_DRAFT,
            created_by=actor_id,
        )
        await self.repo.add(hotel)

        # Default children so later tabs edit a row instead of creating one —
        # the point of a resumable builder.
        await self.repo.add_policy(HotelPolicy(hotel_id=hotel.id))
        self.db.add(HotelRating(hotel_id=hotel.id))
        self.db.add(HotelPerformanceSummary(hotel_id=hotel.id))
        await self.db.flush()

        await self.verification.add_log(
            HotelVerificationLog(
                hotel_id=hotel.id,
                action=ACTION_HOTEL_CREATED,
                to_status=HOTEL_STATUS_DRAFT,
                remarks=f"Hotel {hotel.hotel_code} created",
                performed_by=actor_id,
            )
        )
        return hotel

    # --- Stage 2: profile ---

    async def update_profile(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> Hotel:
        hotel = await self.get_or_404(hotel_id)
        data = payload.model_dump(exclude_unset=True)

        if "gst_number" in data:
            data["gst_number"] = validate_gst_number(data["gst_number"])
        if "pan_number" in data:
            data["pan_number"] = validate_pan_number(data["pan_number"])
        if "postal_code" in data:
            data["postal_code"] = validate_pincode(data["postal_code"])
        if "contact_number" in data:
            data["contact_number"] = validate_mobile(data["contact_number"])
        if "alternate_number" in data:
            data["alternate_number"] = validate_mobile(
                data["alternate_number"], field="alternate_number"
            )
        if "star_rating" in data:
            data["star_rating"] = validate_star_rating(data["star_rating"])

        if "latitude" in data or "longitude" in data:
            validate_coordinates(
                data.get("latitude", hotel.latitude),
                data.get("longitude", hotel.longitude),
            )

        mode = data.get("confirmation_mode")
        if mode is not None and mode not in CONFIRMATION_MODES:
            raise ValidationException(
                f"Confirmation mode must be one of {', '.join(CONFIRMATION_MODES)}.",
                details={"field": "confirmation_mode"},
            )
        allocation = data.get("room_allocation_mode")
        if allocation is not None and allocation not in ROOM_ALLOCATION_MODES:
            raise ValidationException(
                "Room allocation mode must be one of "
                f"{', '.join(ROOM_ALLOCATION_MODES)}.",
                details={"field": "room_allocation_mode"},
            )

        if "city_id" in data and data["city_id"] != hotel.city_id:
            if await self._city_name(data["city_id"]) is None:
                raise ResourceNotFoundException("City", data["city_id"])
            if "state_id" not in data:
                data["state_id"] = await self._state_of_city(data["city_id"])

        if (
            data.get("hotel_category_id") is not None
            and await self.masters.get_category(data["hotel_category_id"]) is None
        ):
            raise ResourceNotFoundException("Hotel category", data["hotel_category_id"])

        if data.get("hotel_name") and await self.repo.name_exists_in_city(
            data["hotel_name"], data.get("city_id", hotel.city_id), hotel.id
        ):
            raise DuplicateResourceException("Hotel", "name in this city")

        for field, value in data.items():
            setattr(hotel, field, value)
        await self.db.flush()

        await self._log(hotel.id, ACTION_HOTEL_UPDATED, actor_id, "Profile updated")
        return hotel

    async def update_seo(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> tuple[Hotel, Optional[str]]:
        """Returns the hotel plus an optional warning.

        Changing a live hotel's slug is warned about rather than blocked —
        breaking an indexed URL is the admin's call to make knowingly.
        """
        hotel = await self.get_or_404(hotel_id)
        data = payload.model_dump(exclude_unset=True)
        warning: Optional[str] = None

        if data.get("slug"):
            slug = validate_slug(data["slug"])
            if slug != hotel.slug:
                if await self.repo.slug_exists(str(slug), exclude_hotel_id=hotel.id):
                    raise DuplicateResourceException("Hotel", "slug")
                if hotel.status == HOTEL_STATUS_ACTIVE:
                    warning = (
                        "This hotel is live. Changing its slug breaks existing links "
                        "and the search-engine indexing of the old URL."
                    )
            data["slug"] = slug

        for field, value in data.items():
            setattr(hotel, field, value)
        await self.db.flush()

        await self._log(hotel.id, ACTION_HOTEL_UPDATED, actor_id, "SEO updated")
        return hotel, warning

    async def update_tax(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> tuple[Hotel, bool]:
        """Per-hotel tax mode. Returns the global switch alongside it so the UI
        can say the setting is inert rather than silently ignoring it."""
        hotel = await self.get_or_404(hotel_id)

        if payload.tax_mode not in HOTEL_TAX_MODES:
            raise ValidationException(
                f"Tax mode must be one of {', '.join(HOTEL_TAX_MODES)}.",
                details={"field": "tax_mode"},
            )

        if payload.gst_number is not None:
            hotel.gst_number = validate_gst_number(payload.gst_number)
        if payload.is_gst_registered and not hotel.gst_number:
            raise ValidationException(
                "A GST-registered hotel must have a GST number.",
                details={"field": "gst_number"},
            )

        hotel.tax_mode = payload.tax_mode
        hotel.is_gst_registered = payload.is_gst_registered
        await self.db.flush()

        await self._log(
            hotel.id,
            ACTION_TAX_UPDATED,
            actor_id,
            f"Tax mode set to {payload.tax_mode}",
        )
        return hotel, await platform_gst_enabled(self.db)

    async def replace_amenities(
        self, hotel_id: int, amenity_ids: list[int], actor_id: Optional[UUID]
    ) -> list[int]:
        await self.get_or_404(hotel_id)

        valid = await self.masters.filter_existing_amenity_ids(amenity_ids)
        unknown = set(amenity_ids) - valid
        if unknown:
            raise ValidationException(
                f"Unknown amenity ids: {sorted(unknown)}",
                details={"field": "amenity_ids"},
            )

        await self.repo.replace_amenities(hotel_id, sorted(valid))
        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Amenities updated")
        return sorted(valid)

    async def delete_hotel(self, hotel_id: int, actor_id: Optional[UUID]) -> None:
        hotel = await self.get_or_404(hotel_id)

        live = int(
            (
                await self.db.execute(
                    text(
                        "SELECT COUNT(*) FROM hotel_reservations "
                        "WHERE hotel_id = :hid AND reservation_status NOT IN "
                        "  ('CANCELLED', 'CHECKED_OUT', 'NO_SHOW', 'EXPIRED')"
                    ),
                    {"hid": hotel_id},
                )
            ).scalar_one()
            or 0
        )
        if live:
            raise BusinessException(
                f"This hotel has {live} active reservation(s) and cannot be deleted. "
                "Deactivate it instead so existing guests are unaffected."
            )

        await self.repo.soft_delete(hotel)
        await self._log(hotel.id, ACTION_HOTEL_UPDATED, actor_id, "Hotel deleted")

    # --- images ---

    async def add_image(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelImage:
        await self.get_or_404(hotel_id)

        if payload.image_type not in HOTEL_IMAGE_TYPES:
            raise ValidationException(
                f"Image type must be one of {', '.join(HOTEL_IMAGE_TYPES)}.",
                details={"field": "image_type"},
            )

        # The first image becomes primary automatically — a gallery with no
        # primary renders nothing on the customer site.
        make_primary = payload.is_primary or await self.repo.count_images(hotel_id) == 0
        if make_primary:
            await self.repo.clear_primary_image(hotel_id)

        image = await self.repo.add_image(
            HotelImage(
                hotel_id=hotel_id,
                image_url=payload.image_url,
                thumbnail_url=payload.thumbnail_url,
                caption=payload.caption,
                image_type=payload.image_type,
                display_order=payload.display_order,
                is_primary=make_primary,
            )
        )
        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Image added")
        return image

    async def _get_image_or_404(self, hotel_id: int, image_id: int) -> HotelImage:
        image = await self.repo.get_image(image_id)
        if image is None or int(image.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Hotel image", image_id)
        return image

    async def update_image(
        self, hotel_id: int, image_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelImage:
        await self.get_or_404(hotel_id)
        image = await self._get_image_or_404(hotel_id, image_id)
        data = payload.model_dump(exclude_unset=True)

        if data.get("image_type") and data["image_type"] not in HOTEL_IMAGE_TYPES:
            raise ValidationException(
                f"Image type must be one of {', '.join(HOTEL_IMAGE_TYPES)}.",
                details={"field": "image_type"},
            )
        if data.get("is_primary"):
            await self.repo.clear_primary_image(hotel_id, except_image_id=image_id)

        for field, value in data.items():
            setattr(image, field, value)
        await self.db.flush()

        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Image updated")
        return image

    async def set_primary_image(
        self, hotel_id: int, image_id: int, actor_id: Optional[UUID]
    ) -> HotelImage:
        await self.get_or_404(hotel_id)
        image = await self._get_image_or_404(hotel_id, image_id)

        await self.repo.clear_primary_image(hotel_id, except_image_id=image_id)
        image.is_primary = True
        await self.db.flush()

        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Primary image set")
        return image

    async def reorder_images(
        self, hotel_id: int, ordered_ids: list[int], actor_id: Optional[UUID]
    ) -> list[HotelImage]:
        await self.get_or_404(hotel_id)
        images = {int(i.id): i for i in await self.repo.list_images(hotel_id)}

        unknown = set(ordered_ids) - set(images)
        if unknown:
            raise ValidationException(
                f"Unknown image ids: {sorted(unknown)}",
                details={"field": "ordered_ids"},
            )

        for position, image_id in enumerate(ordered_ids):
            images[image_id].display_order = position
        await self.db.flush()

        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Images reordered")
        return await self.repo.list_images(hotel_id)

    async def delete_image(
        self, hotel_id: int, image_id: int, actor_id: Optional[UUID]
    ) -> None:
        await self.get_or_404(hotel_id)
        image = await self._get_image_or_404(hotel_id, image_id)
        was_primary = bool(image.is_primary)

        await self.repo.delete_image(image)

        # Promote the next image rather than leaving the gallery headless.
        if was_primary:
            remaining = await self.repo.list_images(hotel_id)
            if remaining:
                remaining[0].is_primary = True
                await self.db.flush()

        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Image deleted")

    # --- documents ---

    async def add_document(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelDocument:
        await self.get_or_404(hotel_id)

        if payload.document_type not in HOTEL_DOCUMENT_TYPES:
            raise ValidationException(
                f"Document type must be one of {', '.join(HOTEL_DOCUMENT_TYPES)}.",
                details={"field": "document_type"},
            )
        if (
            payload.issue_date
            and payload.expiry_date
            and payload.expiry_date < payload.issue_date
        ):
            raise ValidationException(
                "Expiry date cannot be earlier than the issue date.",
                details={"field": "expiry_date"},
            )

        document = await self.repo.add_document(
            HotelDocument(
                hotel_id=hotel_id,
                document_type=payload.document_type,
                document_number=payload.document_number,
                file_url=payload.file_url,
                issue_date=payload.issue_date,
                expiry_date=payload.expiry_date,
                verification_status=DOC_STATUS_PENDING,
            )
        )
        await self._log(
            hotel_id,
            ACTION_HOTEL_UPDATED,
            actor_id,
            f"Document uploaded: {payload.document_type}",
        )
        return document

    async def _get_document_or_404(
        self, hotel_id: int, document_id: int
    ) -> HotelDocument:
        document = await self.repo.get_document(document_id)
        if document is None or int(document.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Hotel document", document_id)
        return document

    async def verify_document(
        self, hotel_id: int, document_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelDocument:
        await self.get_or_404(hotel_id)
        document = await self._get_document_or_404(hotel_id, document_id)

        if payload.verification_status not in (
            DOC_STATUS_VERIFIED,
            DOC_STATUS_REJECTED,
        ):
            raise ValidationException(
                "Verification status must be VERIFIED or REJECTED.",
                details={"field": "verification_status"},
            )
        # A rejection the partner cannot act on just loops the queue.
        if (
            payload.verification_status == DOC_STATUS_REJECTED
            and not (payload.remarks or "").strip()
        ):
            raise ValidationException(
                "Remarks are required when rejecting a document — the partner needs "
                "to know what to correct.",
                details={"field": "remarks"},
            )

        document.verification_status = payload.verification_status
        document.remarks = payload.remarks
        document.verified_at = _utcnow()
        document.verified_by = actor_id
        await self.db.flush()

        await self._log(
            hotel_id,
            (
                ACTION_DOCUMENT_VERIFIED
                if payload.verification_status == DOC_STATUS_VERIFIED
                else ACTION_DOCUMENT_REJECTED
            ),
            actor_id,
            f"{document.document_type}: "
            f"{payload.remarks or payload.verification_status}",
        )
        return document

    async def delete_document(
        self, hotel_id: int, document_id: int, actor_id: Optional[UUID]
    ) -> None:
        await self.get_or_404(hotel_id)
        document = await self._get_document_or_404(hotel_id, document_id)

        if document.verification_status == DOC_STATUS_VERIFIED:
            raise BusinessException(
                "A verified document cannot be deleted. It is the evidence behind "
                "this hotel's approval."
            )

        doc_type = document.document_type
        await self.repo.delete_document(document)
        await self._log(
            hotel_id, ACTION_HOTEL_UPDATED, actor_id, f"Document deleted: {doc_type}"
        )

    # --- policy ---

    async def get_policy(self, hotel_id: int) -> HotelPolicy:
        await self.get_or_404(hotel_id)
        policy = await self.repo.get_policy(hotel_id)
        if policy is None:
            policy = await self.repo.add_policy(HotelPolicy(hotel_id=hotel_id))
        return policy

    async def update_policy(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelPolicy:
        policy = await self.get_policy(hotel_id)
        data = payload.model_dump(exclude_unset=True)

        merged = {
            field: data.get(field, getattr(policy, field))
            for field in (
                "cancellation_free_hours",
                "cancellation_tier_2_hours",
                "cancellation_tier_3_hours",
                "refund_percent_tier_1",
                "refund_percent_tier_2",
                "refund_percent_tier_3",
                "refund_percent_same_day",
            )
        }

        validate_refund_ladder(
            merged["refund_percent_tier_1"],
            merged["refund_percent_tier_2"],
            merged["refund_percent_tier_3"],
            merged["refund_percent_same_day"],
        )

        # Hours count down towards check-in, so each tier must be tighter than the
        # one before. A non-monotonic ladder refunds more the later a guest cancels.
        hours = [
            h
            for h in (
                merged["cancellation_free_hours"],
                merged["cancellation_tier_2_hours"],
                merged["cancellation_tier_3_hours"],
            )
            if h is not None
        ]
        if any(h < 0 for h in hours):
            raise ValidationException(
                "Cancellation hours cannot be negative.",
                details={"field": "cancellation_free_hours"},
            )
        for earlier, later in zip(hours, hours[1:]):
            if later >= earlier:
                raise ValidationException(
                    "Each cancellation tier must be closer to check-in than the one "
                    "before it.",
                    details={"field": "cancellation_tier_2_hours"},
                )

        for field, value in data.items():
            setattr(policy, field, value)
        await self.db.flush()

        await self._log(hotel_id, ACTION_HOTEL_UPDATED, actor_id, "Policies updated")
        return policy

    # --- commission ---

    async def get_commission(self, hotel_id: int) -> dict[str, Any]:
        hotel = await self.get_or_404(hotel_id)
        return {
            "override": await self.repo.get_active_commission(hotel_id),
            "history": await self.repo.list_commission_history(hotel_id),
            "resolved": resolved_commission_payload(
                await resolve_hotel_commission(self.db, hotel)
            ),
        }

    async def set_commission(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelCommissionConfig:
        """Supersede rather than edit in place — a confirmed booking's commission
        must stay explainable after the rate changes."""
        await self.get_or_404(hotel_id)

        validate_commission_values(
            payload.commission_type,
            payload.commission_percent,
            payload.commission_flat,
            payload.min_commission,
            payload.max_commission,
        )
        if (
            payload.effective_from
            and payload.effective_to
            and payload.effective_to < payload.effective_from
        ):
            raise ValidationException(
                "Effective-to cannot be earlier than effective-from.",
                details={"field": "effective_to"},
            )

        await self.repo.deactivate_commissions(hotel_id)
        config = await self.repo.add_commission(
            HotelCommissionConfig(
                hotel_id=hotel_id,
                commission_type=payload.commission_type,
                commission_percent=payload.commission_percent,
                commission_flat=payload.commission_flat,
                min_commission=payload.min_commission,
                max_commission=payload.max_commission,
                applies_to=payload.applies_to,
                effective_from=payload.effective_from,
                effective_to=payload.effective_to,
                is_active=True,
                remarks=payload.remarks,
                created_by=actor_id,
            )
        )
        await self._log(
            hotel_id,
            ACTION_COMMISSION_UPDATED,
            actor_id,
            f"Commission set to {payload.commission_type}",
        )
        return config

    async def clear_commission(
        self, hotel_id: int, actor_id: Optional[UUID]
    ) -> dict[str, Any]:
        hotel = await self.get_or_404(hotel_id)
        await self.repo.deactivate_commissions(hotel_id)
        await self._log(
            hotel_id,
            ACTION_COMMISSION_UPDATED,
            actor_id,
            "Commission override removed; falling back to platform rules",
        )
        return resolved_commission_payload(
            await resolve_hotel_commission(self.db, hotel)
        )


# ============================================================
# READINESS
# ============================================================


class ReadinessFacts:
    """Everything the checklist rules need, already gathered.

    Separating the facts from the rules is what lets the list endpoint compute a
    completeness badge for 20 hotels with batched queries while running the
    exact same rules as the detail meter. Two expressions of the rules would
    drift, and the badge would then promise a submit the gate refuses.
    """

    def __init__(
        self,
        *,
        amenity_count: int,
        image_count: int,
        primary_image_count: int,
        min_images: int,
        policy_set: bool,
        priced_category_count: int,
        inventory_ok: bool,
        document_status: dict[str, str],
        commission_resolved: bool,
        bank_verified: bool,
        platform_gst_enabled: bool,
    ):
        self.amenity_count = amenity_count
        self.image_count = image_count
        self.primary_image_count = primary_image_count
        self.min_images = min_images
        self.policy_set = policy_set
        self.priced_category_count = priced_category_count
        self.inventory_ok = inventory_ok
        self.document_status = document_status
        self.commission_resolved = commission_resolved
        self.bank_verified = bank_verified
        self.platform_gst_enabled = platform_gst_enabled


def build_readiness_report(hotel: Any, facts: ReadinessFacts) -> dict[str, Any]:
    """The one place the readiness rules live. Pure — no session, no queries."""
    checks: list[dict[str, Any]] = []

    def check(
        key: str,
        label: str,
        passed: bool,
        blocks_submit: bool,
        blocks_approve: bool,
        hint: Optional[str] = None,
    ) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "passed": passed,
                "blocks_submit": blocks_submit,
                "blocks_approve": blocks_approve,
                "hint": None if passed else hint,
            }
        )

    # --- profile: BRD §59 mandatory fields ---
    missing = [
        label
        for field, label in (
            ("hotel_name", "name"),
            ("hotel_type", "type"),
            ("address", "address"),
            ("city_id", "city"),
            ("contact_person", "contact person"),
            ("contact_number", "contact number"),
            ("email", "email"),
        )
        if not getattr(hotel, field, None)
    ]
    check(
        "profile_complete",
        "Mandatory profile fields",
        not missing,
        True,
        True,
        f"Missing: {', '.join(missing)}",
    )
    check(
        "category_assigned",
        "Property category assigned",
        hotel.hotel_category_id is not None,
        True,
        True,
        "Pick a property category on the Profile tab.",
    )
    check(
        "location_set",
        "Map location set",
        hotel.latitude is not None and hotel.longitude is not None,
        False,
        False,
        "Without coordinates this hotel will not appear in map or "
        "distance-based search.",
    )
    check(
        "amenities_selected",
        "At least 3 amenities",
        facts.amenity_count >= 3,
        False,
        False,
        f"{facts.amenity_count} selected; 3 or more is recommended.",
    )

    # --- media ---
    check(
        "images_uploaded",
        f"At least {facts.min_images} images with one primary",
        facts.image_count >= facts.min_images and facts.primary_image_count == 1,
        True,
        True,
        f"{facts.image_count} image(s), {facts.primary_image_count} marked primary.",
    )

    # --- policies ---
    check(
        "policies_set",
        "Check-in and cancellation policy set",
        facts.policy_set,
        True,
        True,
        "Set the cancellation ladder on the Policies tab.",
    )

    # --- rooms ---
    check(
        "room_categories",
        "At least one priced room category",
        facts.priced_category_count > 0,
        True,
        True,
        "Add a room category with a base price above zero on the Rooms tab.",
    )
    check(
        "inventory_ready",
        "Inventory generated for every room category",
        facts.inventory_ok,
        True,
        True,
        "Each category needs a room count above zero and generated inventory.",
    )

    # --- documents ---
    required = list(HOTEL_REQUIRED_DOCUMENT_TYPES)
    # GST certificate is conditional — a property below the registration
    # threshold legitimately has none.
    if hotel.is_gst_registered:
        required.append(DOC_GST_CERTIFICATE)

    missing_docs = [d for d in required if d not in facts.document_status]
    check(
        "documents_mandatory",
        "All mandatory documents uploaded",
        not missing_docs,
        True,
        True,
        f"Missing: {', '.join(missing_docs)}",
    )

    unverified = [
        d for d in required if facts.document_status.get(d) != DOC_STATUS_VERIFIED
    ]
    # Blocks approve but NOT submit: submitting is what asks an officer to
    # verify, so requiring verification before submission would deadlock.
    check(
        "documents_verified",
        "All mandatory documents verified",
        not unverified,
        False,
        True,
        f"Awaiting verification: {', '.join(unverified)}",
    )

    # --- commercials ---
    check(
        "commission_resolvable",
        "Commission resolvable",
        facts.commission_resolved,
        True,
        True,
        "No hotel override, city rule, global rule or system default matched.",
    )
    check(
        "tax_configured",
        "Tax mode configured",
        bool(hotel.tax_mode),
        False,
        False,
        (
            "Platform GST is off, so this setting is currently inert."
            if not facts.platform_gst_enabled
            else "Set the tax mode on the Commercials tab."
        ),
    )
    check(
        "bank_account",
        "Partner has a verified bank account",
        facts.bank_verified,
        True,
        True,
        "The owning partner needs a verified bank account before this hotel can "
        "be paid.",
    )
    check(
        "seo_complete",
        "SEO fields complete",
        bool(hotel.slug and hotel.seo_title and hotel.seo_description),
        False,
        False,
        "Fill the SEO tab so the property page ranks.",
    )

    passed_count = sum(1 for c in checks if c["passed"])
    return {
        "hotel_id": int(hotel.id),
        "status": hotel.status,
        "completeness_percent": (
            int(round(passed_count * 100 / len(checks))) if checks else 0
        ),
        "can_submit": all(c["passed"] for c in checks if c["blocks_submit"]),
        "can_approve": all(c["passed"] for c in checks if c["blocks_approve"]),
        "checks": checks,
    }


class HotelReadinessService:
    """The single completeness computation.

    Both the submit gate and the UI meter read this, so an admin can never be
    rejected by a rule the meter did not show them.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = HotelRepository(db)
        self.categories = RoomCategoryRepository(db)
        self.inventory = InventoryRepository(db)

    async def compute(self, hotel_id: int) -> dict[str, Any]:
        hotel = await self.repo.get_by_id(hotel_id)
        if hotel is None:
            raise ResourceNotFoundException("Hotel", hotel_id)

        min_images = await get_config_int(self.db, CONFIG_HOTEL_MIN_IMAGES_REQUIRED, 3)
        gst_on = await platform_gst_enabled(self.db)

        images = await self.repo.list_images(hotel_id)
        room_categories = await self.categories.list_for_hotel(
            hotel_id, include_inactive=False
        )
        priced = [c for c in room_categories if Decimal(c.base_price or 0) > 0]
        inventory_ok = bool(priced)
        for category in priced:
            if (
                int(category.total_rooms or 0) <= 0
                or await self.inventory.count_for_category(category.id) == 0
            ):
                inventory_ok = False
                break

        # list_documents is newest-first, so the latest upload of a type wins.
        latest_status: dict[str, str] = {}
        for doc in await self.repo.list_documents(hotel_id):
            latest_status.setdefault(
                str(doc.document_type), str(doc.verification_status)
            )

        # A hotel with no payout path should not go live — far cheaper to surface
        # here than at the first settlement run.
        bank_ok = (
            await self.db.execute(
                text(
                    "SELECT 1 FROM partner_bank_accounts "
                    "WHERE partner_id = :pid AND verification_status = 'VERIFIED' "
                    "LIMIT 1"
                ),
                {"pid": hotel.partner_id},
            )
        ).scalar_one_or_none() is not None

        policy = await self.repo.get_policy(hotel_id)
        facts = ReadinessFacts(
            amenity_count=len(await self.repo.get_amenity_ids(hotel_id)),
            image_count=len(images),
            primary_image_count=sum(1 for i in images if i.is_primary),
            min_images=min_images,
            policy_set=policy is not None
            and policy.cancellation_free_hours is not None,
            priced_category_count=len(priced),
            inventory_ok=inventory_ok,
            document_status=latest_status,
            commission_resolved=await resolve_hotel_commission(self.db, hotel)
            is not None,
            bank_verified=bank_ok,
            platform_gst_enabled=gst_on,
        )
        return build_readiness_report(hotel, facts)

    async def compute_many(self, hotels: list[Hotel]) -> dict[int, dict[str, Any]]:
        """The same rules over a page of hotels, with the facts gathered in one
        query per fact rather than one per hotel — the list badge would
        otherwise cost 14 round-trips per row."""
        if not hotels:
            return {}

        ids = [int(h.id) for h in hotels]
        min_images = await get_config_int(self.db, CONFIG_HOTEL_MIN_IMAGES_REQUIRED, 3)
        gst_on = await platform_gst_enabled(self.db)

        amenity_counts = await self.repo.amenity_counts(ids)
        image_counts, primary_counts = await self.repo.image_counts(ids)
        policy_ready = await self.repo.hotels_with_valid_policy(ids)
        priced_counts, inventory_ready = await self.categories.readiness_counts(ids)
        doc_status = await self.repo.latest_document_status(ids)
        banked = await self.repo.partners_with_verified_bank(
            [int(h.partner_id) for h in hotels]
        )

        report: dict[int, dict[str, Any]] = {}
        for hotel in hotels:
            hid = int(hotel.id)
            facts = ReadinessFacts(
                amenity_count=amenity_counts.get(hid, 0),
                image_count=image_counts.get(hid, 0),
                primary_image_count=primary_counts.get(hid, 0),
                min_images=min_images,
                policy_set=hid in policy_ready,
                priced_category_count=priced_counts.get(hid, 0),
                inventory_ok=hid in inventory_ready,
                document_status=doc_status.get(hid, {}),
                # Resolution always terminates at the system default, so on the
                # list it is never the reason a row shows as incomplete.
                commission_resolved=True,
                bank_verified=int(hotel.partner_id) in banked,
                platform_gst_enabled=gst_on,
            )
            report[hid] = build_readiness_report(hotel, facts)
        return report


# ============================================================
# VERIFICATION
# ============================================================


class HotelVerificationService:
    """Stage 6. Every status change goes through _transition_hotel, so neither
    the transition map nor the audit trail can be bypassed."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = HotelRepository(db)
        self.verification = HotelVerificationRepository(db)
        self.readiness = HotelReadinessService(db)

    async def get_or_404(self, hotel_id: int) -> Hotel:
        hotel = await self.repo.get_by_id(hotel_id)
        if hotel is None:
            raise ResourceNotFoundException("Hotel", hotel_id)
        return hotel

    async def _transition_hotel(
        self,
        hotel: Hotel,
        to_status: str,
        action: str,
        actor_id: Optional[UUID],
        remarks: Optional[str] = None,
    ) -> Hotel:
        from_status = str(hotel.status)
        allowed = VALID_HOTEL_TRANSITIONS.get(from_status, [])
        if to_status not in allowed:
            raise BusinessException(
                f"A hotel in {from_status} cannot move to {to_status}. Allowed: "
                f"{', '.join(allowed) if allowed else 'none'}.",
                code=ERR_INVALID_TRANSITION,
                details={"from_status": from_status, "to_status": to_status},
            )

        hotel.status = to_status
        await self.db.flush()
        await self.verification.add_log(
            HotelVerificationLog(
                hotel_id=hotel.id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                remarks=remarks,
                performed_by=actor_id,
            )
        )
        return hotel

    @staticmethod
    def _require_remarks(remarks: Optional[str], why: str) -> str:
        if not (remarks or "").strip():
            raise ValidationException(
                f"Remarks are required: {why}", details={"field": "remarks"}
            )
        return str(remarks).strip()

    def _blocking(self, report: dict[str, Any], gate: str) -> str:
        return "; ".join(
            c["label"] for c in report["checks"] if c[gate] and not c["passed"]
        )

    # --- submit ---

    async def submit(self, hotel_id: int, actor_id: Optional[UUID]) -> Hotel:
        hotel = await self.get_or_404(hotel_id)
        report = await self.readiness.compute(hotel_id)

        if not report["can_submit"]:
            raise BusinessException(
                "This hotel is not ready for verification. Outstanding: "
                + self._blocking(report, "blocks_submit"),
                code=ERR_NOT_READY_FOR_SUBMISSION,
                details={"checks": report["checks"]},
            )

        await self._transition_hotel(
            hotel,
            HOTEL_STATUS_PENDING,
            ACTION_HOTEL_SUBMITTED,
            actor_id,
            "Submitted for verification",
        )
        hotel.submitted_at = _utcnow()
        hotel.rejection_reason = None
        await self.db.flush()
        return hotel

    # --- officer assignment ---

    async def assign_officer(
        self,
        hotel_id: int,
        officer_id: UUID,
        actor_id: Optional[UUID],
        notes: Optional[str] = None,
    ) -> HotelVerificationAssignment:
        hotel = await self.get_or_404(hotel_id)

        # Verification officers are the primary assignees. Admins and
        # super-admins may also be assigned so they can verify a hotel at their
        # own risk when no officer is free (the admin UI flags this on select).
        officer = (
            await self.db.execute(
                text(
                    "SELECT 1 FROM users WHERE id = :oid "
                    "  AND user_type IN "
                    "      ('VERIFICATION_OFFICER', 'ADMIN', 'SUPER_ADMIN') "
                    "  AND is_active = TRUE AND deleted_at IS NULL"
                ),
                {"oid": str(officer_id)},
            )
        ).scalar_one_or_none()
        if officer is None:
            raise ResourceNotFoundException("Verification officer", officer_id)

        # Reassignment deactivates the old row instead of editing it, so the
        # handover stays visible in the history.
        await self.verification.deactivate_assignments(hotel_id)
        assignment = await self.verification.add_assignment(
            HotelVerificationAssignment(
                hotel_id=hotel_id,
                officer_id=officer_id,
                assigned_by=actor_id,
                notes=notes,
                is_active=True,
            )
        )

        if hotel.status == HOTEL_STATUS_PENDING:
            await self._transition_hotel(
                hotel,
                HOTEL_STATUS_UNDER_REVIEW,
                ACTION_OFFICER_ASSIGNED,
                actor_id,
                notes or "Officer assigned",
            )
        else:
            await self.verification.add_log(
                HotelVerificationLog(
                    hotel_id=hotel_id,
                    action=ACTION_OFFICER_ASSIGNED,
                    from_status=hotel.status,
                    to_status=hotel.status,
                    remarks=notes or "Officer assigned",
                    performed_by=actor_id,
                )
            )
        return assignment

    async def unassign_officer(self, hotel_id: int, actor_id: Optional[UUID]) -> None:
        hotel = await self.get_or_404(hotel_id)
        await self.verification.deactivate_assignments(hotel_id)
        await self.verification.add_log(
            HotelVerificationLog(
                hotel_id=hotel_id,
                action=ACTION_OFFICER_UNASSIGNED,
                from_status=hotel.status,
                to_status=hotel.status,
                performed_by=actor_id,
            )
        )

    # --- review ---

    async def start_review(self, hotel_id: int, actor_id: Optional[UUID]) -> Hotel:
        return await self._transition_hotel(
            await self.get_or_404(hotel_id),
            HOTEL_STATUS_UNDER_REVIEW,
            ACTION_REVIEW_STARTED,
            actor_id,
            "Review started",
        )

    async def request_documents(
        self, hotel_id: int, remarks: Optional[str], actor_id: Optional[UUID]
    ) -> Hotel:
        return await self._transition_hotel(
            await self.get_or_404(hotel_id),
            HOTEL_STATUS_DOCUMENT_PENDING,
            ACTION_DOCUMENTS_REQUESTED,
            actor_id,
            self._require_remarks(
                remarks, "state which document is missing or unacceptable"
            ),
        )

    # --- approve / reject ---

    async def approve(
        self, hotel_id: int, actor_id: Optional[UUID], remarks: Optional[str] = None
    ) -> Hotel:
        hotel = await self.get_or_404(hotel_id)
        report = await self.readiness.compute(hotel_id)

        if not report["can_approve"]:
            raise BusinessException(
                "This hotel cannot be approved yet. Outstanding: "
                + self._blocking(report, "blocks_approve")
                + ". Use own-risk approval if you intend to override this.",
                code=ERR_NOT_READY_FOR_SUBMISSION,
                details={"checks": report["checks"]},
            )

        await self._transition_hotel(
            hotel, HOTEL_STATUS_APPROVED, ACTION_HOTEL_APPROVED, actor_id, remarks
        )
        hotel.approved_at = _utcnow()
        hotel.approved_by = actor_id
        hotel.rejection_reason = None
        await self.db.flush()
        return hotel

    async def approve_own_risk(
        self, hotel_id: int, actor_id: Optional[UUID], remarks: Optional[str]
    ) -> Hotel:
        """Skips the document checks. The justification is mandatory and
        permanent — an unexplained control bypass is indistinguishable from a
        mistake six months later."""
        hotel = await self.get_or_404(hotel_id)

        if not await get_config_bool(self.db, CONFIG_HOTEL_AUTO_APPROVE_ENABLED, False):
            raise BusinessException(
                "Own-risk approval is disabled. Enable HOTEL_AUTO_APPROVE_ENABLED in "
                "system settings first."
            )

        justification = self._require_remarks(
            remarks, "own-risk approval bypasses document verification"
        )
        await self._transition_hotel(
            hotel,
            HOTEL_STATUS_APPROVED,
            ACTION_HOTEL_APPROVED_OWN_RISK,
            actor_id,
            justification,
        )
        hotel.approved_at = _utcnow()
        hotel.approved_by = actor_id
        hotel.is_own_risk_approved = True
        hotel.rejection_reason = None
        await self.db.flush()
        return hotel

    async def reject(
        self, hotel_id: int, reason: str, actor_id: Optional[UUID]
    ) -> Hotel:
        hotel = await self.get_or_404(hotel_id)
        detail = self._require_remarks(reason, "the partner needs a reason to act on")

        await self._transition_hotel(
            hotel, HOTEL_STATUS_REJECTED, ACTION_HOTEL_REJECTED, actor_id, detail
        )
        hotel.rejection_reason = detail
        await self.db.flush()
        return hotel

    # --- go live ---

    async def activate(self, hotel_id: int, actor_id: Optional[UUID]) -> Hotel:
        """Approval says the paperwork is sound; activation says start selling.
        Kept separate so a seasonal property can be approved in advance."""
        hotel = await self.get_or_404(hotel_id)
        report = await self.readiness.compute(hotel_id)

        if not report["can_submit"]:
            raise BusinessException(
                "This hotel cannot go live. Outstanding: "
                + self._blocking(report, "blocks_submit"),
                code=ERR_NOT_READY_FOR_SUBMISSION,
                details={"checks": report["checks"]},
            )

        await self._transition_hotel(
            hotel,
            HOTEL_STATUS_ACTIVE,
            ACTION_HOTEL_ACTIVATED,
            actor_id,
            "Hotel is live",
        )
        if hotel.activated_at is None:
            hotel.activated_at = _utcnow()
        await self.db.flush()
        return hotel

    async def deactivate(
        self, hotel_id: int, actor_id: Optional[UUID], remarks: Optional[str] = None
    ) -> Hotel:
        return await self._transition_hotel(
            await self.get_or_404(hotel_id),
            HOTEL_STATUS_INACTIVE,
            ACTION_HOTEL_DEACTIVATED,
            actor_id,
            remarks,
        )

    async def suspend(
        self, hotel_id: int, actor_id: Optional[UUID], remarks: Optional[str]
    ) -> Hotel:
        return await self._transition_hotel(
            await self.get_or_404(hotel_id),
            HOTEL_STATUS_SUSPENDED,
            ACTION_HOTEL_SUSPENDED,
            actor_id,
            self._require_remarks(remarks, "a suspension must be explainable"),
        )

    async def block(
        self, hotel_id: int, actor_id: Optional[UUID], remarks: Optional[str]
    ) -> Hotel:
        return await self._transition_hotel(
            await self.get_or_404(hotel_id),
            HOTEL_STATUS_BLOCKED,
            ACTION_HOTEL_BLOCKED,
            actor_id,
            self._require_remarks(remarks, "blocking is terminal"),
        )

    # --- reads ---

    async def get_assignment(self, hotel_id: int) -> Optional[dict[str, Any]]:
        assignment = await self.verification.get_active_assignment(hotel_id)
        if assignment is None:
            return None

        officer = (
            (
                await self.db.execute(
                    text(
                        "SELECT TRIM(CONCAT(first_name, ' ', COALESCE(last_name, ''))) "
                        "AS name, mobile_number FROM users WHERE id = :oid"
                    ),
                    {"oid": str(assignment.officer_id)},
                )
            )
            .mappings()
            .first()
        )
        return {
            "id": int(assignment.id),
            "hotel_id": int(assignment.hotel_id),
            "officer_id": assignment.officer_id,
            "assigned_by": assignment.assigned_by,
            "assigned_at": assignment.assigned_at,
            "unassigned_at": assignment.unassigned_at,
            "is_active": bool(assignment.is_active),
            "notes": assignment.notes,
            "officer_name": officer["name"] if officer else None,
            "officer_mobile": officer["mobile_number"] if officer else None,
        }

    async def list_logs(self, hotel_id: int, limit: int = 200) -> list[dict[str, Any]]:
        await self.get_or_404(hotel_id)
        logs = await self.verification.list_logs(hotel_id, limit)

        actor_ids = [str(log.performed_by) for log in logs if log.performed_by]
        names: dict[str, str] = {}
        if actor_ids:
            rows = await self.db.execute(
                text(
                    "SELECT id, TRIM(CONCAT(first_name, ' ', COALESCE(last_name, ''))) "
                    "AS name FROM users WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": sorted(set(actor_ids))},
            )
            names = {str(r[0]): r[1] for r in rows.all()}

        return [
            {
                "id": int(log.id),
                "hotel_id": int(log.hotel_id),
                "action": log.action,
                "from_status": log.from_status,
                "to_status": log.to_status,
                "remarks": log.remarks,
                "performed_by": log.performed_by,
                "performed_by_name": names.get(str(log.performed_by)),
                "created_at": log.created_at,
            }
            for log in logs
        ]

    async def stats(self, officer_id: Optional[UUID] = None) -> dict[str, Any]:
        counts = await self.repo.status_counts()
        result: dict[str, Any] = {
            "total": sum(counts.values()),
            "draft": counts.get(HOTEL_STATUS_DRAFT, 0),
            "pending": counts.get(HOTEL_STATUS_PENDING, 0),
            "under_review": counts.get(HOTEL_STATUS_UNDER_REVIEW, 0),
            "document_pending": counts.get(HOTEL_STATUS_DOCUMENT_PENDING, 0),
            "approved": counts.get(HOTEL_STATUS_APPROVED, 0),
            "active": counts.get(HOTEL_STATUS_ACTIVE, 0),
            "rejected": counts.get(HOTEL_STATUS_REJECTED, 0),
            "inactive": counts.get(HOTEL_STATUS_INACTIVE, 0),
            "suspended": counts.get(HOTEL_STATUS_SUSPENDED, 0),
            "blocked": counts.get(HOTEL_STATUS_BLOCKED, 0),
            "own_risk_approved": await self.repo.count_own_risk_approved(),
            "unassigned_in_pipeline": await self.repo.count_unassigned_in_pipeline(
                HOTEL_PIPELINE_STATUSES
            ),
        }

        # An officer's landing view should show their own queue, not a global count.
        if officer_id is not None:
            mine = (
                await self.db.execute(
                    select(func.count(Hotel.id))
                    .join(
                        HotelVerificationAssignment,
                        HotelVerificationAssignment.hotel_id == Hotel.id,
                    )
                    .where(
                        Hotel.deleted_at.is_(None),
                        Hotel.status.in_(HOTEL_PIPELINE_STATUSES),
                        HotelVerificationAssignment.officer_id == officer_id,
                        HotelVerificationAssignment.is_active.is_(True),
                    )
                )
            ).scalar_one()
            result["awaiting_my_review"] = int(mine)

        return result


__all__ = [
    "get_config",
    "get_config_bool",
    "get_config_int",
    "get_config_decimal",
    "platform_gst_enabled",
    "resolve_hotel_commission",
    "commission_worked_example",
    "resolved_commission_payload",
    "HotelService",
    "HotelReadinessService",
    "HotelVerificationService",
]
