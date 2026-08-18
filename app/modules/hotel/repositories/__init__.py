# ============================================================
# WAY TERO — HOTEL REPOSITORIES
# File: app/modules/hotel/repositories/__init__.py
# Doc Ref: DB Schema Part 5 — Hotel Management
#
# All hotel SQL lives here. Services orchestrate; they do not query.
# Nothing in this file commits — get_db owns the transaction.
# ============================================================

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.hotel.models import (
    Hotel,
    HotelAmenity,
    HotelAmenityMapping,
    HotelCategory,
    HotelCheckin,
    HotelCommissionConfig,
    HotelDocument,
    HotelGstSlab,
    HotelImage,
    HotelInventory,
    HotelPolicy,
    HotelReservation,
    HotelReservationGuest,
    HotelRoom,
    HotelRoomCategory,
    HotelRoomCategoryAmenity,
    HotelRoomCategoryImage,
    HotelRoomRatePlan,
    HotelVerificationAssignment,
    HotelVerificationLog,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# MASTERS
# ============================================================


class HotelMasterRepository:
    """Categories, amenities and GST slabs — the lookup tables the admin
    settings page edits."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- categories ---

    async def list_categories(
        self, include_inactive: bool = False
    ) -> list[HotelCategory]:
        stmt = select(HotelCategory)
        if not include_inactive:
            stmt = stmt.where(HotelCategory.is_active.is_(True))
        stmt = stmt.order_by(HotelCategory.display_order, HotelCategory.label)
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_category(self, category_id: int) -> Optional[HotelCategory]:
        stmt = select(HotelCategory).where(HotelCategory.id == category_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_category_by_code(self, code: str) -> Optional[HotelCategory]:
        stmt = select(HotelCategory).where(HotelCategory.category_code == code)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def add_category(self, category: HotelCategory) -> HotelCategory:
        self.db.add(category)
        await self.db.flush()
        return category

    # --- amenities ---

    async def list_amenities(
        self, include_inactive: bool = False
    ) -> list[HotelAmenity]:
        stmt = select(HotelAmenity)
        if not include_inactive:
            stmt = stmt.where(HotelAmenity.is_active.is_(True))
        stmt = stmt.order_by(
            HotelAmenity.amenity_group,
            HotelAmenity.display_order,
            HotelAmenity.amenity_name,
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_amenity(self, amenity_id: int) -> Optional[HotelAmenity]:
        stmt = select(HotelAmenity).where(HotelAmenity.id == amenity_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_amenity_by_code(self, code: str) -> Optional[HotelAmenity]:
        stmt = select(HotelAmenity).where(HotelAmenity.amenity_code == code)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def filter_existing_amenity_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        stmt = select(HotelAmenity.id).where(HotelAmenity.id.in_(ids))
        return set((await self.db.execute(stmt)).scalars().all())

    async def add_amenity(self, amenity: HotelAmenity) -> HotelAmenity:
        self.db.add(amenity)
        await self.db.flush()
        return amenity

    # --- GST slabs ---

    async def list_gst_slabs(
        self, on_date: Optional[date] = None, include_inactive: bool = False
    ) -> list[HotelGstSlab]:
        """Slabs in force on a date. Effective-dating matters because a GST
        council revision must not retroactively re-price past bookings."""
        stmt = select(HotelGstSlab)
        if not include_inactive:
            stmt = stmt.where(HotelGstSlab.is_active.is_(True))
        if on_date is not None:
            stmt = stmt.where(
                HotelGstSlab.effective_from <= on_date,
                or_(
                    HotelGstSlab.effective_to.is_(None),
                    HotelGstSlab.effective_to >= on_date,
                ),
            )
        stmt = stmt.order_by(HotelGstSlab.tariff_from)
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_gst_slab(self, slab_id: int) -> Optional[HotelGstSlab]:
        stmt = select(HotelGstSlab).where(HotelGstSlab.id == slab_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def overlapping_gst_slabs(
        self,
        tariff_from: Decimal,
        tariff_to: Optional[Decimal],
        effective_from: date,
        effective_to: Optional[date],
        exclude_id: Optional[int] = None,
    ) -> list[HotelGstSlab]:
        """Slabs whose tariff band and effective window both overlap the given
        one. Two slabs matching the same tariff on the same date make the
        resolver's answer depend on row order, which is not a tax position
        anyone can defend.

        A NULL upper bound means open-ended, so it overlaps everything above it.
        """
        stmt = select(HotelGstSlab).where(
            HotelGstSlab.is_active.is_(True),
            or_(
                HotelGstSlab.tariff_to.is_(None),
                HotelGstSlab.tariff_to >= tariff_from,
            ),
            or_(
                HotelGstSlab.effective_to.is_(None),
                HotelGstSlab.effective_to >= effective_from,
            ),
        )
        if tariff_to is not None:
            stmt = stmt.where(HotelGstSlab.tariff_from <= tariff_to)
        if effective_to is not None:
            stmt = stmt.where(HotelGstSlab.effective_from <= effective_to)
        if exclude_id is not None:
            stmt = stmt.where(HotelGstSlab.id != exclude_id)
        return list((await self.db.execute(stmt)).scalars().all())

    async def add_gst_slab(self, slab: HotelGstSlab) -> HotelGstSlab:
        self.db.add(slab)
        await self.db.flush()
        return slab


# ============================================================
# HOTEL
# ============================================================


class HotelRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _detail_query(self) -> Select[Any]:
        return (
            select(Hotel)
            .options(
                selectinload(Hotel.category),
                selectinload(Hotel.amenity_mappings),
                selectinload(Hotel.images),
                selectinload(Hotel.documents),
                selectinload(Hotel.policy),
                selectinload(Hotel.commission_configs),
                selectinload(Hotel.room_categories),
            )
            .where(Hotel.deleted_at.is_(None))
        )

    async def next_hotel_code(self, prefix: str = "HTL") -> str:
        """A Postgres sequence, not MAX(id)+1 — two admins creating hotels at
        the same moment must not collide on hotel_code."""
        result = await self.db.execute(select(func.nextval("hotel_code_seq")))
        return f"{prefix}{int(result.scalar_one()):06d}"

    async def add(self, hotel: Hotel) -> Hotel:
        self.db.add(hotel)
        await self.db.flush()
        return hotel

    async def get_by_id(self, hotel_id: int) -> Optional[Hotel]:
        stmt = self._detail_query().where(Hotel.id == hotel_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_by_uuid(self, hotel_uuid: UUID) -> Optional[Hotel]:
        stmt = self._detail_query().where(Hotel.uuid == hotel_uuid)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_lite(self, hotel_id: int) -> Optional[Hotel]:
        """No eager loads — for status checks and existence probes where the
        relationship payload would be wasted work."""
        stmt = select(Hotel).where(Hotel.id == hotel_id, Hotel.deleted_at.is_(None))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def slug_exists(
        self, slug: str, exclude_hotel_id: Optional[int] = None
    ) -> bool:
        stmt = select(Hotel.id).where(Hotel.slug == slug)
        if exclude_hotel_id is not None:
            stmt = stmt.where(Hotel.id != exclude_hotel_id)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none() is not None

    async def name_exists_in_city(
        self, hotel_name: str, city_id: int, exclude_hotel_id: Optional[int] = None
    ) -> bool:
        stmt = select(Hotel.id).where(
            func.lower(Hotel.hotel_name) == hotel_name.strip().lower(),
            Hotel.city_id == city_id,
            Hotel.deleted_at.is_(None),
        )
        if exclude_hotel_id is not None:
            stmt = stmt.where(Hotel.id != exclude_hotel_id)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none() is not None

    async def list_paginated(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
        status: Optional[str] = None,
        city_id: Optional[int] = None,
        state_id: Optional[int] = None,
        partner_id: Optional[int] = None,
        category_id: Optional[int] = None,
        star_rating: Optional[int] = None,
        officer_id: Optional[UUID] = None,
        is_featured: Optional[bool] = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> tuple[list[Hotel], int]:
        filters = [Hotel.deleted_at.is_(None)]

        if search:
            like = f"%{search.strip()}%"
            filters.append(
                or_(
                    Hotel.hotel_name.ilike(like),
                    Hotel.hotel_code.ilike(like),
                    Hotel.contact_number.ilike(like),
                    Hotel.email.ilike(like),
                )
            )
        if status:
            filters.append(Hotel.status == status)
        if city_id:
            filters.append(Hotel.city_id == city_id)
        if state_id:
            filters.append(Hotel.state_id == state_id)
        if partner_id:
            filters.append(Hotel.partner_id == partner_id)
        if category_id:
            filters.append(Hotel.hotel_category_id == category_id)
        if star_rating:
            filters.append(Hotel.star_rating == star_rating)
        if is_featured is not None:
            filters.append(Hotel.is_featured.is_(is_featured))
        if officer_id is not None:
            assigned = (
                select(HotelVerificationAssignment.hotel_id)
                .where(
                    HotelVerificationAssignment.officer_id == officer_id,
                    HotelVerificationAssignment.is_active.is_(True),
                )
                .scalar_subquery()
            )
            filters.append(Hotel.id.in_(assigned))

        total = (
            await self.db.execute(select(func.count(Hotel.id)).where(and_(*filters)))
        ).scalar_one()

        sortable: dict[str, Any] = {
            "created_at": Hotel.created_at,
            "hotel_name": Hotel.hotel_name,
            "status": Hotel.status,
            "star_rating": Hotel.star_rating,
            "total_rooms": Hotel.total_rooms,
            "display_order": Hotel.display_order,
        }
        column = sortable.get(sort_by, Hotel.created_at)
        order = column.desc() if sort_dir.lower() == "desc" else column.asc()

        stmt = (
            select(Hotel)
            .options(
                selectinload(Hotel.category),
                selectinload(Hotel.images),
                selectinload(Hotel.room_categories),
            )
            .where(and_(*filters))
            .order_by(order)
            .offset(max(page - 1, 0) * page_size)
            .limit(page_size)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        return rows, int(total)

    async def status_counts(self) -> dict[str, int]:
        stmt = (
            select(Hotel.status, func.count(Hotel.id))
            .where(Hotel.deleted_at.is_(None))
            .group_by(Hotel.status)
        )
        return {row[0]: int(row[1]) for row in (await self.db.execute(stmt)).all()}

    async def count_own_risk_approved(self) -> int:
        stmt = select(func.count(Hotel.id)).where(
            Hotel.deleted_at.is_(None), Hotel.is_own_risk_approved.is_(True)
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def count_unassigned_in_pipeline(self, pipeline_statuses: list[str]) -> int:
        """Hotels waiting in the verification pipeline with no active officer —
        the queue that would otherwise silently stall."""
        assigned = (
            select(HotelVerificationAssignment.hotel_id)
            .where(HotelVerificationAssignment.is_active.is_(True))
            .scalar_subquery()
        )
        stmt = select(func.count(Hotel.id)).where(
            Hotel.deleted_at.is_(None),
            Hotel.status.in_(pipeline_statuses),
            Hotel.id.notin_(assigned),
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def recalculate_total_rooms(self, hotel_id: int) -> int:
        """hotels.total_rooms is a denormalised sum of the room categories.
        Recomputed rather than incremented so it cannot drift."""
        total = (
            await self.db.execute(
                select(func.coalesce(func.sum(HotelRoomCategory.total_rooms), 0)).where(
                    HotelRoomCategory.hotel_id == hotel_id,
                    HotelRoomCategory.is_active.is_(True),
                )
            )
        ).scalar_one()
        await self.db.execute(
            update(Hotel).where(Hotel.id == hotel_id).values(total_rooms=int(total))
        )
        return int(total)

    async def soft_delete(self, hotel: Hotel) -> None:
        hotel.deleted_at = _utcnow()
        await self.db.flush()

    # --- amenities ---

    async def get_amenity_ids(self, hotel_id: int) -> list[int]:
        stmt = select(HotelAmenityMapping.amenity_id).where(
            HotelAmenityMapping.hotel_id == hotel_id
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def replace_amenities(self, hotel_id: int, amenity_ids: list[int]) -> None:
        await self.db.execute(
            delete(HotelAmenityMapping).where(HotelAmenityMapping.hotel_id == hotel_id)
        )
        for amenity_id in dict.fromkeys(amenity_ids):
            self.db.add(HotelAmenityMapping(hotel_id=hotel_id, amenity_id=amenity_id))
        await self.db.flush()

    # --- images ---

    async def list_images(self, hotel_id: int) -> list[HotelImage]:
        stmt = (
            select(HotelImage)
            .where(HotelImage.hotel_id == hotel_id)
            .order_by(
                HotelImage.is_primary.desc(), HotelImage.display_order, HotelImage.id
            )
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_image(self, image_id: int) -> Optional[HotelImage]:
        stmt = select(HotelImage).where(HotelImage.id == image_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def add_image(self, image: HotelImage) -> HotelImage:
        self.db.add(image)
        await self.db.flush()
        return image

    async def clear_primary_image(
        self, hotel_id: int, except_image_id: Optional[int] = None
    ) -> None:
        """Must run before setting a new primary — uq_hotel_primary_image is a
        partial unique index and will reject a second true."""
        stmt = update(HotelImage).where(
            HotelImage.hotel_id == hotel_id, HotelImage.is_primary.is_(True)
        )
        if except_image_id is not None:
            stmt = stmt.where(HotelImage.id != except_image_id)
        await self.db.execute(stmt.values(is_primary=False))
        await self.db.flush()

    async def delete_image(self, image: HotelImage) -> None:
        await self.db.delete(image)
        await self.db.flush()

    async def count_images(self, hotel_id: int) -> int:
        stmt = select(func.count(HotelImage.id)).where(HotelImage.hotel_id == hotel_id)
        return int((await self.db.execute(stmt)).scalar_one())

    # --- batched readiness facts (list endpoint) ---

    async def amenity_counts(self, hotel_ids: list[int]) -> dict[int, int]:
        if not hotel_ids:
            return {}
        stmt = (
            select(
                HotelAmenityMapping.hotel_id,
                func.count(HotelAmenityMapping.amenity_id),
            )
            .where(HotelAmenityMapping.hotel_id.in_(hotel_ids))
            .group_by(HotelAmenityMapping.hotel_id)
        )
        return {int(r[0]): int(r[1]) for r in (await self.db.execute(stmt)).all()}

    async def image_counts(
        self, hotel_ids: list[int]
    ) -> tuple[dict[int, int], dict[int, int]]:
        """Total and primary counts in one pass — the readiness rule needs both."""
        if not hotel_ids:
            return {}, {}
        stmt = (
            select(
                HotelImage.hotel_id,
                func.count(HotelImage.id),
                func.count(HotelImage.id).filter(HotelImage.is_primary.is_(True)),
            )
            .where(HotelImage.hotel_id.in_(hotel_ids))
            .group_by(HotelImage.hotel_id)
        )
        rows = (await self.db.execute(stmt)).all()
        return (
            {int(r[0]): int(r[1]) for r in rows},
            {int(r[0]): int(r[2]) for r in rows},
        )

    async def hotels_with_valid_policy(self, hotel_ids: list[int]) -> set[int]:
        if not hotel_ids:
            return set()
        stmt = select(HotelPolicy.hotel_id).where(
            HotelPolicy.hotel_id.in_(hotel_ids),
            HotelPolicy.cancellation_free_hours.isnot(None),
        )
        return {int(r) for r in (await self.db.execute(stmt)).scalars().all()}

    async def latest_document_status(
        self, hotel_ids: list[int]
    ) -> dict[int, dict[str, str]]:
        """Newest upload per (hotel, type) wins, matching list_documents' ordering
        — a re-upload after a rejection must supersede the rejected row."""
        if not hotel_ids:
            return {}
        stmt = (
            select(
                HotelDocument.hotel_id,
                HotelDocument.document_type,
                HotelDocument.verification_status,
            )
            .where(HotelDocument.hotel_id.in_(hotel_ids))
            .order_by(HotelDocument.hotel_id, HotelDocument.uploaded_at.desc())
        )
        result: dict[int, dict[str, str]] = {}
        for hotel_id, doc_type, doc_status in (await self.db.execute(stmt)).all():
            result.setdefault(int(hotel_id), {}).setdefault(
                str(doc_type), str(doc_status)
            )
        return result

    async def partners_with_verified_bank(self, partner_ids: list[int]) -> set[int]:
        if not partner_ids:
            return set()
        rows = await self.db.execute(
            text(
                "SELECT DISTINCT partner_id FROM partner_bank_accounts "
                "WHERE partner_id = ANY(:ids) AND verification_status = 'VERIFIED'"
            ),
            {"ids": sorted(set(partner_ids))},
        )
        return {int(r[0]) for r in rows.all()}

    # --- documents ---

    async def list_documents(self, hotel_id: int) -> list[HotelDocument]:
        stmt = (
            select(HotelDocument)
            .where(HotelDocument.hotel_id == hotel_id)
            .order_by(HotelDocument.uploaded_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_document(self, document_id: int) -> Optional[HotelDocument]:
        stmt = select(HotelDocument).where(HotelDocument.id == document_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def add_document(self, document: HotelDocument) -> HotelDocument:
        self.db.add(document)
        await self.db.flush()
        return document

    async def delete_document(self, document: HotelDocument) -> None:
        await self.db.delete(document)
        await self.db.flush()

    # --- policy ---

    async def get_policy(self, hotel_id: int) -> Optional[HotelPolicy]:
        stmt = select(HotelPolicy).where(HotelPolicy.hotel_id == hotel_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def add_policy(self, policy: HotelPolicy) -> HotelPolicy:
        self.db.add(policy)
        await self.db.flush()
        return policy

    # --- commission ---

    async def get_active_commission(
        self, hotel_id: int, on_date: Optional[date] = None
    ) -> Optional[HotelCommissionConfig]:
        stmt = select(HotelCommissionConfig).where(
            HotelCommissionConfig.hotel_id == hotel_id,
            HotelCommissionConfig.is_active.is_(True),
        )
        if on_date is not None:
            stmt = stmt.where(
                or_(
                    HotelCommissionConfig.effective_from.is_(None),
                    HotelCommissionConfig.effective_from <= on_date,
                ),
                or_(
                    HotelCommissionConfig.effective_to.is_(None),
                    HotelCommissionConfig.effective_to >= on_date,
                ),
            )
        stmt = stmt.order_by(HotelCommissionConfig.id.desc()).limit(1)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_commission_history(
        self, hotel_id: int
    ) -> list[HotelCommissionConfig]:
        stmt = (
            select(HotelCommissionConfig)
            .where(HotelCommissionConfig.hotel_id == hotel_id)
            .order_by(HotelCommissionConfig.id.desc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def deactivate_commissions(self, hotel_id: int) -> None:
        await self.db.execute(
            update(HotelCommissionConfig)
            .where(
                HotelCommissionConfig.hotel_id == hotel_id,
                HotelCommissionConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self.db.flush()

    async def add_commission(
        self, config: HotelCommissionConfig
    ) -> HotelCommissionConfig:
        self.db.add(config)
        await self.db.flush()
        return config


# ============================================================
# ROOM CATEGORIES / ROOMS
# ============================================================


class RoomCategoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_hotel(
        self, hotel_id: int, include_inactive: bool = True
    ) -> list[HotelRoomCategory]:
        stmt = (
            select(HotelRoomCategory)
            .options(
                selectinload(HotelRoomCategory.images),
                selectinload(HotelRoomCategory.amenities),
            )
            .where(HotelRoomCategory.hotel_id == hotel_id)
        )
        if not include_inactive:
            stmt = stmt.where(HotelRoomCategory.is_active.is_(True))
        stmt = stmt.order_by(HotelRoomCategory.display_order, HotelRoomCategory.id)
        return list((await self.db.execute(stmt)).scalars().all())

    async def get(self, category_id: int) -> Optional[HotelRoomCategory]:
        stmt = (
            select(HotelRoomCategory)
            .options(
                selectinload(HotelRoomCategory.images),
                selectinload(HotelRoomCategory.amenities),
            )
            .where(HotelRoomCategory.id == category_id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def add(self, category: HotelRoomCategory) -> HotelRoomCategory:
        self.db.add(category)
        await self.db.flush()
        return category

    async def delete(self, category: HotelRoomCategory) -> None:
        await self.db.delete(category)
        await self.db.flush()

    async def name_exists(
        self, hotel_id: int, name: str, exclude_id: Optional[int] = None
    ) -> bool:
        stmt = select(HotelRoomCategory.id).where(
            HotelRoomCategory.hotel_id == hotel_id,
            func.lower(HotelRoomCategory.category_name) == name.strip().lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(HotelRoomCategory.id != exclude_id)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none() is not None

    async def count_for_hotel(self, hotel_id: int) -> int:
        stmt = select(func.count(HotelRoomCategory.id)).where(
            HotelRoomCategory.hotel_id == hotel_id,
            HotelRoomCategory.is_active.is_(True),
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def readiness_counts(
        self, hotel_ids: list[int]
    ) -> tuple[dict[int, int], set[int]]:
        """Batch equivalent of the priced-category / inventory facts that
        HotelReadinessService.compute derives per hotel. Returns the priced
        category count per hotel, and the set of hotels where every priced
        category is stocked (rooms declared and inventory materialised)."""
        if not hotel_ids:
            return {}, set()

        stmt = select(
            HotelRoomCategory.hotel_id,
            HotelRoomCategory.id,
            HotelRoomCategory.total_rooms,
        ).where(
            HotelRoomCategory.hotel_id.in_(hotel_ids),
            HotelRoomCategory.is_active.is_(True),
            HotelRoomCategory.base_price > 0,
        )
        priced_rows = (await self.db.execute(stmt)).all()

        priced_counts: dict[int, int] = {}
        for hotel_id, _, _ in priced_rows:
            priced_counts[int(hotel_id)] = priced_counts.get(int(hotel_id), 0) + 1

        category_ids = [int(row[1]) for row in priced_rows]
        stocked: set[int] = set()
        if category_ids:
            stocked = set(
                (
                    await self.db.execute(
                        select(HotelInventory.room_category_id)
                        .where(HotelInventory.room_category_id.in_(category_ids))
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )

        inventory_ready = {hid for hid in priced_counts}
        for hotel_id, category_id, total_rooms in priced_rows:
            if int(total_rooms or 0) <= 0 or int(category_id) not in stocked:
                inventory_ready.discard(int(hotel_id))
        return priced_counts, inventory_ready

    async def replace_amenities(self, category_id: int, amenity_ids: list[int]) -> None:
        await self.db.execute(
            delete(HotelRoomCategoryAmenity).where(
                HotelRoomCategoryAmenity.room_category_id == category_id
            )
        )
        for amenity_id in dict.fromkeys(amenity_ids):
            self.db.add(
                HotelRoomCategoryAmenity(
                    room_category_id=category_id, amenity_id=amenity_id
                )
            )
        await self.db.flush()

    # --- images ---

    async def add_image(self, image: HotelRoomCategoryImage) -> HotelRoomCategoryImage:
        self.db.add(image)
        await self.db.flush()
        return image

    async def get_image(self, image_id: int) -> Optional[HotelRoomCategoryImage]:
        stmt = select(HotelRoomCategoryImage).where(
            HotelRoomCategoryImage.id == image_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def clear_primary_image(self, category_id: int) -> None:
        await self.db.execute(
            update(HotelRoomCategoryImage)
            .where(
                HotelRoomCategoryImage.room_category_id == category_id,
                HotelRoomCategoryImage.is_primary.is_(True),
            )
            .values(is_primary=False)
        )
        await self.db.flush()

    async def delete_image(self, image: HotelRoomCategoryImage) -> None:
        await self.db.delete(image)
        await self.db.flush()


class RoomRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_hotel(
        self, hotel_id: int, room_category_id: Optional[int] = None
    ) -> list[HotelRoom]:
        stmt = select(HotelRoom).where(HotelRoom.hotel_id == hotel_id)
        if room_category_id is not None:
            stmt = stmt.where(HotelRoom.room_category_id == room_category_id)
        stmt = stmt.order_by(HotelRoom.room_category_id, HotelRoom.room_number)
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_for_partner(self, partner_id: int) -> list[HotelRoom]:
        """Every physical room across every hotel owned by ``partner_id``.

        Joins ``hotel_rooms`` with ``hotels`` so the partner filter is a single
        query. Used by the partner-side ``GET /admin/hotels/rooms/all`` endpoint
        to avoid the N+1 of fetching hotels then looping ``list_for_hotel``.
        """
        stmt = (
            select(HotelRoom)
            .join(Hotel, Hotel.id == HotelRoom.hotel_id)
            .where(Hotel.partner_id == partner_id)
            .order_by(Hotel.id, HotelRoom.room_category_id, HotelRoom.room_number)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get(self, room_id: int) -> Optional[HotelRoom]:
        stmt = select(HotelRoom).where(HotelRoom.id == room_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def existing_room_numbers(
        self, hotel_id: int, numbers: list[str]
    ) -> set[str]:
        """Checked in one query before a bulk insert so the whole batch can be
        rejected with a useful message instead of failing on a unique violation."""
        if not numbers:
            return set()
        stmt = select(HotelRoom.room_number).where(
            HotelRoom.hotel_id == hotel_id, HotelRoom.room_number.in_(numbers)
        )
        return set((await self.db.execute(stmt)).scalars().all())

    async def add_many(self, rooms: list[HotelRoom]) -> list[HotelRoom]:
        self.db.add_all(rooms)
        await self.db.flush()
        return rooms

    async def delete(self, room: HotelRoom) -> None:
        await self.db.delete(room)
        await self.db.flush()

    async def count_by_category(self, hotel_id: int) -> dict[int, int]:
        stmt = (
            select(HotelRoom.room_category_id, func.count(HotelRoom.id))
            .where(HotelRoom.hotel_id == hotel_id)
            .group_by(HotelRoom.room_category_id)
        )
        return {int(r[0]): int(r[1]) for r in (await self.db.execute(stmt)).all()}

    async def active_allocation_map(self, hotel_id: int) -> dict[str, dict[str, Any]]:
        """Room numbers currently held by a live stay, keyed by upper-cased number.

        A room is *actively allocated* when it appears in the ``allocated_rooms``
        JSON of a check-in whose reservation is CHECKED_IN or IN_HOUSE. This is the
        signal used to lock a room from manual status edits — it is released
        automatically when the booking checks out. Stronger than trusting
        ``room_status == 'OCCUPIED'``, which a partner could also set by hand.
        """
        stmt = (
            select(HotelReservation, HotelCheckin.allocated_rooms)
            .join(HotelCheckin, HotelCheckin.reservation_id == HotelReservation.id)
            .where(
                HotelReservation.hotel_id == hotel_id,
                HotelReservation.reservation_status.in_(("CHECKED_IN", "IN_HOUSE")),
            )
        )
        rows = (await self.db.execute(stmt)).all()
        if not rows:
            return {}

        # Primary guest name per reservation, in one query.
        res_ids = [int(res.id) for res, _ in rows]
        guest_stmt = select(
            HotelReservationGuest.reservation_id, HotelReservationGuest.guest_name
        ).where(
            HotelReservationGuest.reservation_id.in_(res_ids),
            HotelReservationGuest.is_primary.is_(True),
        )
        guest_map = {
            int(rid): name for rid, name in (await self.db.execute(guest_stmt)).all()
        }

        result: dict[str, dict[str, Any]] = {}
        for res, allocated_json in rows:
            if not allocated_json:
                continue
            try:
                numbers = json.loads(allocated_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(numbers, list):
                continue
            info = {
                "reservation_id": int(res.id),
                "reservation_number": res.reservation_number,
                "guest_name": guest_map.get(int(res.id)),
                "check_out_date": res.check_out_date,
                "status": res.reservation_status,
            }
            for num in numbers:
                if num is None:
                    continue
                key = str(num).strip().upper()
                if key:
                    result[key] = info
        return result


# ============================================================
# RATE PLANS
# ============================================================


class RatePlanRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_category(
        self,
        room_category_id: int,
        active_only: bool = False,
        overlapping: Optional[tuple[date, date]] = None,
    ) -> list[HotelRoomRatePlan]:
        stmt = select(HotelRoomRatePlan).where(
            HotelRoomRatePlan.room_category_id == room_category_id
        )
        if active_only:
            stmt = stmt.where(HotelRoomRatePlan.is_active.is_(True))
        if overlapping is not None:
            date_from, date_to = overlapping
            stmt = stmt.where(
                HotelRoomRatePlan.date_from <= date_to,
                HotelRoomRatePlan.date_to >= date_from,
            )
        stmt = stmt.order_by(
            HotelRoomRatePlan.priority.desc(), HotelRoomRatePlan.id.desc()
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_for_hotel(self, hotel_id: int) -> list[HotelRoomRatePlan]:
        stmt = (
            select(HotelRoomRatePlan)
            .where(HotelRoomRatePlan.hotel_id == hotel_id)
            .order_by(HotelRoomRatePlan.room_category_id, HotelRoomRatePlan.date_from)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def get(self, plan_id: int) -> Optional[HotelRoomRatePlan]:
        stmt = select(HotelRoomRatePlan).where(HotelRoomRatePlan.id == plan_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def add(self, plan: HotelRoomRatePlan) -> HotelRoomRatePlan:
        self.db.add(plan)
        await self.db.flush()
        return plan

    async def delete(self, plan: HotelRoomRatePlan) -> None:
        await self.db.delete(plan)
        await self.db.flush()

    async def count_for_hotel(self, hotel_id: int) -> dict[int, int]:
        stmt = (
            select(HotelRoomRatePlan.room_category_id, func.count(HotelRoomRatePlan.id))
            .where(
                HotelRoomRatePlan.hotel_id == hotel_id,
                HotelRoomRatePlan.is_active.is_(True),
            )
            .group_by(HotelRoomRatePlan.room_category_id)
        )
        return {int(r[0]): int(r[1]) for r in (await self.db.execute(stmt)).all()}


# ============================================================
# INVENTORY
# ============================================================


class InventoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_range(
        self,
        hotel_id: int,
        date_from: date,
        date_to: date,
        room_category_id: Optional[int] = None,
    ) -> list[HotelInventory]:
        stmt = select(HotelInventory).where(
            HotelInventory.hotel_id == hotel_id,
            HotelInventory.inventory_date >= date_from,
            HotelInventory.inventory_date <= date_to,
        )
        if room_category_id is not None:
            stmt = stmt.where(HotelInventory.room_category_id == room_category_id)
        stmt = stmt.order_by(
            HotelInventory.room_category_id, HotelInventory.inventory_date
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def existing_dates(
        self, room_category_id: int, date_from: date, date_to: date
    ) -> set[date]:
        stmt = select(HotelInventory.inventory_date).where(
            HotelInventory.room_category_id == room_category_id,
            HotelInventory.inventory_date >= date_from,
            HotelInventory.inventory_date <= date_to,
        )
        return set((await self.db.execute(stmt)).scalars().all())

    async def add_many(self, rows: list[HotelInventory]) -> int:
        if not rows:
            return 0
        self.db.add_all(rows)
        await self.db.flush()
        return len(rows)

    async def count_for_category(self, room_category_id: int) -> int:
        stmt = select(func.count(HotelInventory.id)).where(
            HotelInventory.room_category_id == room_category_id
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def rate_overrides(
        self, room_category_id: int, date_from: date, date_to: date
    ) -> dict[date, Any]:
        stmt = select(
            HotelInventory.inventory_date, HotelInventory.rate_override
        ).where(
            HotelInventory.room_category_id == room_category_id,
            HotelInventory.inventory_date >= date_from,
            HotelInventory.inventory_date <= date_to,
            HotelInventory.rate_override.isnot(None),
        )
        return {row[0]: row[1] for row in (await self.db.execute(stmt)).all()}


# ============================================================
# VERIFICATION
# ============================================================


class HotelVerificationRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_active_assignment(
        self, hotel_id: int
    ) -> Optional[HotelVerificationAssignment]:
        stmt = (
            select(HotelVerificationAssignment)
            .where(
                HotelVerificationAssignment.hotel_id == hotel_id,
                HotelVerificationAssignment.is_active.is_(True),
            )
            .order_by(HotelVerificationAssignment.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def deactivate_assignments(self, hotel_id: int) -> None:
        await self.db.execute(
            update(HotelVerificationAssignment)
            .where(
                HotelVerificationAssignment.hotel_id == hotel_id,
                HotelVerificationAssignment.is_active.is_(True),
            )
            .values(is_active=False, unassigned_at=_utcnow())
        )
        await self.db.flush()

    async def add_assignment(
        self, assignment: HotelVerificationAssignment
    ) -> HotelVerificationAssignment:
        self.db.add(assignment)
        await self.db.flush()
        return assignment

    async def active_assignments_for(
        self, hotel_ids: list[int]
    ) -> dict[int, HotelVerificationAssignment]:
        """Batched for the list view — one query instead of N."""
        if not hotel_ids:
            return {}
        stmt = select(HotelVerificationAssignment).where(
            HotelVerificationAssignment.hotel_id.in_(hotel_ids),
            HotelVerificationAssignment.is_active.is_(True),
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return {int(r.hotel_id): r for r in rows}

    async def add_log(self, log: HotelVerificationLog) -> HotelVerificationLog:
        self.db.add(log)
        await self.db.flush()
        return log

    async def list_logs(
        self, hotel_id: int, limit: int = 200
    ) -> list[HotelVerificationLog]:
        stmt = (
            select(HotelVerificationLog)
            .where(HotelVerificationLog.hotel_id == hotel_id)
            .order_by(HotelVerificationLog.id.desc())
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())


__all__ = [
    "HotelMasterRepository",
    "HotelRepository",
    "RoomCategoryRepository",
    "RoomRepository",
    "RatePlanRepository",
    "InventoryRepository",
    "HotelVerificationRepository",
]
