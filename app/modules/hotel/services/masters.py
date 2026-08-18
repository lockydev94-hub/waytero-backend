# ============================================================
# WAY TERO — HOTEL MASTER DATA SERVICES
# File: app/modules/hotel/services/masters.py
# Doc Ref: BRD Part 4 §64, §88 · SRS Part 5 §157
#
# Property categories, the amenity list, and the tariff-based GST slabs.
# Seeded by migration and editable at runtime — nothing here is hardcoded, and
# nothing here commits: get_db owns the transaction.
# ============================================================

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    DuplicateResourceException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.hotel.models import HotelAmenity, HotelCategory, HotelGstSlab
from app.modules.hotel.repositories import HotelMasterRepository
from app.modules.hotel.validators import slugify


class HotelMasterService:
    """The lookup tables the admin Master Data tab edits."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = HotelMasterRepository(db)

    # ------------------------------------------------------------
    # PROPERTY CATEGORIES
    # ------------------------------------------------------------

    async def list_categories(
        self, include_inactive: bool = False
    ) -> list[HotelCategory]:
        return await self.repo.list_categories(include_inactive=include_inactive)

    async def create_category(self, payload: Any) -> HotelCategory:
        code = (payload.category_code or "").strip().upper().replace("-", "_")
        if not code:
            raise ValidationException(
                "Category code is required.", details={"field": "category_code"}
            )
        if await self.repo.get_category_by_code(code) is not None:
            raise DuplicateResourceException("Property category", "code")

        return await self.repo.add_category(
            HotelCategory(
                category_code=code,
                label=payload.label.strip(),
                description=payload.description,
                image_url=payload.image_url,
                icon_url=payload.icon_url,
                seo_title=payload.seo_title,
                seo_description=payload.seo_description,
                seo_keywords=payload.seo_keywords,
                display_order=payload.display_order,
                is_active=payload.is_active,
            )
        )

    async def update_category(self, category_id: int, payload: Any) -> HotelCategory:
        category = await self.repo.get_category(category_id)
        if category is None:
            raise ResourceNotFoundException("Property category", category_id)

        data = payload.model_dump(exclude_unset=True)
        # Deactivating a category that hotels still point at would leave those
        # hotels with a category the admin can no longer see or reassign from.
        if data.get("is_active") is False and await self._category_in_use(category_id):
            raise BusinessException(
                "This category is assigned to one or more hotels. Reassign them "
                "before deactivating it."
            )
        for field, value in data.items():
            setattr(category, field, value)
        await self.db.flush()
        return category

    async def _category_in_use(self, category_id: int) -> bool:
        return (
            await self.db.execute(
                text(
                    "SELECT 1 FROM hotels WHERE hotel_category_id = :cid "
                    "AND deleted_at IS NULL LIMIT 1"
                ),
                {"cid": category_id},
            )
        ).scalar_one_or_none() is not None

    # ------------------------------------------------------------
    # AMENITIES
    # ------------------------------------------------------------

    async def list_amenities(
        self, include_inactive: bool = False
    ) -> list[HotelAmenity]:
        return await self.repo.list_amenities(include_inactive=include_inactive)

    async def create_amenity(self, payload: Any) -> HotelAmenity:
        code = (payload.amenity_code or "").strip().upper().replace("-", "_")
        if not code:
            code = slugify(payload.amenity_name).upper().replace("-", "_")
        if not code:
            raise ValidationException(
                "Amenity code is required.", details={"field": "amenity_code"}
            )
        if await self.repo.get_amenity_by_code(code) is not None:
            raise DuplicateResourceException("Amenity", "code")

        return await self.repo.add_amenity(
            HotelAmenity(
                amenity_code=code,
                amenity_name=payload.amenity_name.strip(),
                icon_name=payload.icon_name,
                amenity_group=payload.amenity_group,
                display_order=payload.display_order,
                is_active=payload.is_active,
            )
        )

    async def update_amenity(self, amenity_id: int, payload: Any) -> HotelAmenity:
        amenity = await self.repo.get_amenity(amenity_id)
        if amenity is None:
            raise ResourceNotFoundException("Amenity", amenity_id)

        # Deactivation is allowed even while in use: an amenity that stops being
        # offered should disappear from the picker without rewriting history.
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(amenity, field, value)
        await self.db.flush()
        return amenity

    # ------------------------------------------------------------
    # GST SLABS
    # ------------------------------------------------------------

    async def list_gst_slabs(
        self, on_date: Optional[date] = None, include_inactive: bool = False
    ) -> list[HotelGstSlab]:
        return await self.repo.list_gst_slabs(
            on_date=on_date, include_inactive=include_inactive
        )

    def _validate_slab(self, data: dict[str, Any]) -> None:
        tariff_from = Decimal(data.get("tariff_from") or 0)
        tariff_to = data.get("tariff_to")
        percent = Decimal(data.get("gst_percent") or 0)

        if tariff_from < 0:
            raise ValidationException(
                "Tariff-from cannot be negative.", details={"field": "tariff_from"}
            )
        if tariff_to is not None and Decimal(tariff_to) <= tariff_from:
            raise ValidationException(
                "Tariff-to must be greater than tariff-from. Leave it empty for "
                "the open-ended top slab.",
                details={"field": "tariff_to"},
            )
        if not (Decimal("0") <= percent <= Decimal("100")):
            raise ValidationException(
                "GST percent must be between 0 and 100.",
                details={"field": "gst_percent"},
            )

        effective_from = data.get("effective_from")
        effective_to = data.get("effective_to")
        if effective_to is not None and effective_from is not None:
            if effective_to < effective_from:
                raise ValidationException(
                    "Effective-to cannot be earlier than effective-from.",
                    details={"field": "effective_to"},
                )

    async def _assert_no_overlap(
        self, data: dict[str, Any], exclude_id: Optional[int] = None
    ) -> None:
        clashes = await self.repo.overlapping_gst_slabs(
            tariff_from=Decimal(data["tariff_from"]),
            tariff_to=(
                Decimal(data["tariff_to"])
                if data.get("tariff_to") is not None
                else None
            ),
            effective_from=data["effective_from"],
            effective_to=data.get("effective_to"),
            exclude_id=exclude_id,
        )
        if clashes:
            names = ", ".join(str(slab.slab_name) for slab in clashes[:5])
            raise BusinessException(
                "This tariff band and date window overlap an existing slab "
                f"({names}). Close the old slab with an effective-to date first, "
                "so each tariff resolves to exactly one rate."
            )

    async def create_gst_slab(self, payload: Any) -> HotelGstSlab:
        data = payload.model_dump()
        self._validate_slab(data)
        await self._assert_no_overlap(data)
        return await self.repo.add_gst_slab(HotelGstSlab(**data, is_active=True))

    async def update_gst_slab(self, slab_id: int, payload: Any) -> HotelGstSlab:
        """Slabs are versioned by effective_from, so an edit here is for fixing a
        data-entry error. A rate change should be a new slab plus an
        effective_to on the old one, which is what the overlap check enforces."""
        slab = await self.repo.get_gst_slab(slab_id)
        if slab is None:
            raise ResourceNotFoundException("GST slab", slab_id)

        merged = {
            "slab_name": slab.slab_name,
            "tariff_from": slab.tariff_from,
            "tariff_to": slab.tariff_to,
            "gst_percent": slab.gst_percent,
            "has_input_credit": slab.has_input_credit,
            "hsn_code": slab.hsn_code,
            "effective_from": slab.effective_from,
            "effective_to": slab.effective_to,
            "notes": slab.notes,
        }
        merged.update(payload.model_dump(exclude_unset=True))
        self._validate_slab(merged)
        await self._assert_no_overlap(merged, exclude_id=slab_id)

        for field, value in merged.items():
            setattr(slab, field, value)
        await self.db.flush()
        return slab

    async def deactivate_gst_slab(self, slab_id: int) -> HotelGstSlab:
        """Deactivate, never delete. A past booking's tax snapshot has to stay
        explainable against the slab that produced it."""
        slab = await self.repo.get_gst_slab(slab_id)
        if slab is None:
            raise ResourceNotFoundException("GST slab", slab_id)
        slab.is_active = False
        await self.db.flush()
        return slab


__all__ = ["HotelMasterService"]
