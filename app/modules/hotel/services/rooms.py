# ============================================================
# WAY TERO — HOTEL ROOM, RATE PLAN AND INVENTORY SERVICES
# File: app/modules/hotel/services/rooms.py
# Doc Ref: BRD Part 4 §64-70, SRS Part 5 §160-166
#
# Four distinct levels, deliberately separate:
#   room category  — the sellable unit and its base price
#   rate plan      — date-ranged overrides on that base price
#   physical room  — an actual numbered room (optional; BRD §66)
#   inventory      — one row per category per date, materialised
#
# Nothing here commits: get_db owns the transaction.
# ============================================================

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessException,
    DuplicateResourceException,
    ResourceNotFoundException,
    ValidationException,
)
from app.modules.hotel.constants import (
    ACTION_HOTEL_UPDATED,
    BED_TYPES,
    CONFIG_HOTEL_INVENTORY_HORIZON_DAYS,
    ERR_INVALID_RATE_PLAN,
    ERR_ROOM_CATEGORY_NOT_FOUND,
    HOTEL_ROOM_TYPES,
    MEAL_PLANS,
    PHYSICAL_ROOM_STATUSES,
    RATE_MODES,
    RATE_PLAN_TYPES,
    VIEW_TYPES,
)
from app.modules.hotel.models import (
    HotelInventory,
    HotelRoom,
    HotelRoomCategory,
    HotelRoomCategoryImage,
    HotelRoomRatePlan,
    HotelVerificationLog,
)
from app.modules.hotel.repositories import (
    HotelMasterRepository,
    HotelRepository,
    HotelVerificationRepository,
    InventoryRepository,
    RatePlanRepository,
    RoomCategoryRepository,
    RoomRepository,
)
from app.modules.hotel.services import (
    get_config_int,
    platform_gst_enabled,
)
from app.modules.hotel.services.pricing import (
    GstSlabInput,
    RatePlanInput,
    compute_tax,
    money,
    resolve_stay_rates,
)
from app.modules.hotel.validators import (
    validate_date_range,
    validate_day_of_week_mask,
    validate_occupancy,
    validate_price_structure,
)

# An inventory row per category per date is cheap, but a request for a decade is
# not. The horizon is configurable; this is the hard ceiling on one call.
MAX_GENERATE_DAYS = 730


class _Base:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.hotels = HotelRepository(db)
        self.categories = RoomCategoryRepository(db)
        self.rooms = RoomRepository(db)
        self.rate_plans = RatePlanRepository(db)
        self.inventory = InventoryRepository(db)
        self.verification = HotelVerificationRepository(db)

    async def _hotel_or_404(self, hotel_id: int) -> Any:
        hotel = await self.hotels.get_lite(hotel_id)
        if hotel is None:
            raise ResourceNotFoundException("Hotel", hotel_id)
        return hotel

    async def _category_or_404(
        self, hotel_id: int, category_id: int
    ) -> HotelRoomCategory:
        category = await self.categories.get(category_id)
        if category is None or int(category.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Room category", category_id)
        return category

    async def _log(self, hotel_id: int, actor_id: Optional[UUID], remarks: str) -> None:
        await self.verification.add_log(
            HotelVerificationLog(
                hotel_id=hotel_id,
                action=ACTION_HOTEL_UPDATED,
                remarks=remarks,
                performed_by=actor_id,
            )
        )


# ============================================================
# ROOM CATEGORIES
# ============================================================


class RoomCategoryService(_Base):
    async def list_categories(self, hotel_id: int) -> list[HotelRoomCategory]:
        await self._hotel_or_404(hotel_id)
        categories = await self.categories.list_for_hotel(hotel_id)

        # Batched counts so the list view stays two queries, not two per category.
        plan_counts = await self.rate_plans.count_for_hotel(hotel_id)
        room_counts = await self.rooms.count_by_category(hotel_id)

        # Decorated onto the instances so RoomCategoryResponse.model_validate
        # picks them up as ordinary attributes.
        for category in categories:
            self._decorate(category, plan_counts, room_counts)
        return categories

    @staticmethod
    def _decorate(
        category: HotelRoomCategory,
        plan_counts: Optional[dict[int, int]] = None,
        room_counts: Optional[dict[int, int]] = None,
    ) -> HotelRoomCategory:
        category.amenity_ids = [int(a.amenity_id) for a in category.amenities]  # type: ignore[attr-defined]
        category.rate_plan_count = (plan_counts or {}).get(int(category.id), 0)  # type: ignore[attr-defined]
        category.physical_room_count = (room_counts or {}).get(int(category.id), 0)  # type: ignore[attr-defined]
        return category

    async def get_category(self, hotel_id: int, category_id: int) -> HotelRoomCategory:
        category = await self._category_or_404(hotel_id, category_id)
        return self._decorate(
            category,
            await self.rate_plans.count_for_hotel(hotel_id),
            await self.rooms.count_by_category(hotel_id),
        )

    def _validate(self, data: dict[str, Any], current: Any = None) -> None:
        def value(field: str, default: Any = None) -> Any:
            if field in data:
                return data[field]
            return getattr(current, field, default) if current is not None else default

        room_type = value("room_type")
        if room_type and room_type not in HOTEL_ROOM_TYPES:
            raise ValidationException(
                f"Room type must be one of {', '.join(HOTEL_ROOM_TYPES)}.",
                details={"field": "room_type"},
            )
        bed_type = value("bed_type")
        if bed_type and bed_type not in BED_TYPES:
            raise ValidationException(
                f"Bed type must be one of {', '.join(BED_TYPES)}.",
                details={"field": "bed_type"},
            )
        view_type = value("view_type")
        if view_type and view_type not in VIEW_TYPES:
            raise ValidationException(
                f"View type must be one of {', '.join(VIEW_TYPES)}.",
                details={"field": "view_type"},
            )
        meal_plan = value("meal_plan")
        if meal_plan and meal_plan not in MEAL_PLANS:
            raise ValidationException(
                f"Meal plan must be one of {', '.join(MEAL_PLANS)}.",
                details={"field": "meal_plan"},
            )

        validate_occupancy(
            int(value("base_occupancy", 2)),
            int(value("max_adults", 2)),
            int(value("max_children", 1)),
            int(value("max_occupancy", 3)),
        )
        validate_price_structure(
            Decimal(value("base_price", 0) or 0),
            (
                Decimal(value("published_price"))
                if value("published_price") is not None
                else None
            ),
            (
                Decimal(value("min_sellable_price"))
                if value("min_sellable_price") is not None
                else None
            ),
        )

        if int(value("total_rooms", 0) or 0) < 0:
            raise ValidationException(
                "Total rooms cannot be negative.", details={"field": "total_rooms"}
            )

    async def create_category(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoomCategory:
        await self._hotel_or_404(hotel_id)

        if await self.categories.name_exists(hotel_id, payload.category_name):
            raise DuplicateResourceException("Room category", "category_name")

        data = payload.model_dump(exclude={"amenity_ids"})
        self._validate(data)

        category = await self.categories.add(
            HotelRoomCategory(hotel_id=hotel_id, **data)
        )

        if payload.amenity_ids:
            await self._set_amenities(category.id, payload.amenity_ids)

        await self.hotels.recalculate_total_rooms(hotel_id)
        await self._log(
            hotel_id, actor_id, f"Room category added: {payload.category_name}"
        )
        return await self.get_category(hotel_id, int(category.id))

    async def _set_amenities(self, category_id: int, amenity_ids: list[int]) -> None:
        valid = await HotelMasterRepository(self.db).filter_existing_amenity_ids(
            amenity_ids
        )
        unknown = set(amenity_ids) - valid
        if unknown:
            raise ValidationException(
                f"Unknown amenity ids: {sorted(unknown)}",
                details={"field": "amenity_ids"},
            )
        await self.categories.replace_amenities(category_id, sorted(valid))

    async def update_category(
        self, hotel_id: int, category_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoomCategory:
        category = await self._category_or_404(hotel_id, category_id)
        data = payload.model_dump(exclude_unset=True, exclude={"amenity_ids"})

        if data.get("category_name") and await self.categories.name_exists(
            hotel_id, data["category_name"], exclude_id=category_id
        ):
            raise DuplicateResourceException("Room category", "category_name")

        self._validate(data, current=category)

        # Shrinking the room count below what is already sold would make
        # available_rooms negative on those dates; the DB CHECK would reject the
        # write with a constraint error nobody can act on.
        if "total_rooms" in data and data["total_rooms"] is not None:
            peak = await self._peak_committed(category_id)
            if int(data["total_rooms"]) < peak:
                raise BusinessException(
                    f"This category already has {peak} room(s) booked or blocked on "
                    f"at least one date. Total rooms cannot drop below {peak}."
                )

        for field, value in data.items():
            setattr(category, field, value)
        await self.db.flush()

        if payload.amenity_ids is not None:
            await self._set_amenities(category_id, payload.amenity_ids)

        if "total_rooms" in data:
            await self._resync_inventory_totals(category)
        await self.hotels.recalculate_total_rooms(hotel_id)

        await self._log(
            hotel_id, actor_id, f"Room category updated: {category.category_name}"
        )
        return await self.get_category(hotel_id, category_id)

    async def _peak_committed(self, category_id: int) -> int:
        """The busiest future date's booked + blocked count — the floor below
        which total_rooms cannot be reduced."""
        category = await self.categories.get(category_id)
        if category is None:
            return 0
        inventory = await self.inventory.list_range(
            int(category.hotel_id),
            date.today(),
            date.today() + timedelta(days=MAX_GENERATE_DAYS),
            room_category_id=category_id,
        )
        return max(
            (int(i.booked_rooms or 0) + int(i.blocked_rooms or 0) for i in inventory),
            default=0,
        )

    async def _resync_inventory_totals(self, category: HotelRoomCategory) -> None:
        """Push a changed room count onto future inventory, from today forward.

        Past dates are left alone — they are a record of what was sellable then,
        and rewriting them would corrupt occupancy reporting.
        """
        inventory = await self.inventory.list_range(
            int(category.hotel_id),
            date.today(),
            date.today() + timedelta(days=MAX_GENERATE_DAYS),
            room_category_id=int(category.id),
        )
        total = int(category.total_rooms or 0)
        for row in inventory:
            row.total_rooms = total
            row.available_rooms = max(
                total
                - int(row.booked_rooms or 0)
                - int(row.blocked_rooms or 0)
                - int(row.held_rooms or 0),
                0,
            )
        await self.db.flush()

    async def delete_category(
        self, hotel_id: int, category_id: int, actor_id: Optional[UUID]
    ) -> None:
        category = await self._category_or_404(hotel_id, category_id)

        committed = await self._peak_committed(category_id)
        if committed:
            raise BusinessException(
                "This room category has booked or blocked rooms and cannot be "
                "deleted. Deactivate it instead so it stops being sold."
            )

        name = category.category_name
        await self.categories.delete(category)
        await self.hotels.recalculate_total_rooms(hotel_id)
        await self._log(hotel_id, actor_id, f"Room category deleted: {name}")

    # --- images ---

    async def add_image(
        self, hotel_id: int, category_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoomCategoryImage:
        category = await self._category_or_404(hotel_id, category_id)

        make_primary = payload.is_primary or not category.images
        if make_primary:
            await self.categories.clear_primary_image(category_id)

        image = await self.categories.add_image(
            HotelRoomCategoryImage(
                room_category_id=category_id,
                image_url=payload.image_url,
                caption=payload.caption,
                display_order=payload.display_order,
                is_primary=make_primary,
            )
        )
        await self._log(hotel_id, actor_id, "Room category image added")
        return image

    async def delete_image(
        self, hotel_id: int, category_id: int, image_id: int, actor_id: Optional[UUID]
    ) -> None:
        await self._category_or_404(hotel_id, category_id)
        image = await self.categories.get_image(image_id)
        if image is None or int(image.room_category_id) != category_id:
            raise ResourceNotFoundException("Room category image", image_id)

        await self.categories.delete_image(image)
        await self._log(hotel_id, actor_id, "Room category image deleted")

    # --- physical rooms ---

    async def list_rooms(
        self, hotel_id: int, category_id: Optional[int] = None
    ) -> list[HotelRoom]:
        await self._hotel_or_404(hotel_id)
        return await self.rooms.list_for_hotel(hotel_id, category_id)

    async def room_allocation_map(self, hotel_id: int) -> dict[str, Any]:
        """Room numbers held by a live (CHECKED_IN / IN_HOUSE) booking, keyed by
        upper-cased room number. Used to surface and lock allocated rooms."""
        return await self.rooms.active_allocation_map(hotel_id)

    async def create_room(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoom:
        await self._category_or_404(hotel_id, payload.room_category_id)

        if await self.rooms.existing_room_numbers(hotel_id, [payload.room_number]):
            raise DuplicateResourceException("Room", "room_number")

        rooms = await self.rooms.add_many(
            [
                HotelRoom(
                    hotel_id=hotel_id,
                    room_category_id=payload.room_category_id,
                    room_number=payload.room_number,
                    floor_number=payload.floor_number,
                    remarks=payload.remarks,
                )
            ]
        )
        await self._log(hotel_id, actor_id, f"Room added: {payload.room_number}")
        return rooms[0]

    async def bulk_create_rooms(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> list[HotelRoom]:
        """Generate prefix + a padded counter, e.g. 101..120.

        The whole batch is checked for collisions first so it fails with a list
        of the offending numbers rather than a unique-violation on row 14.
        """
        await self._category_or_404(hotel_id, payload.room_category_id)

        numbers = [
            f"{payload.prefix}{str(payload.start_number + n).zfill(payload.pad_width)}"
            for n in range(payload.count)
        ]
        clashes = await self.rooms.existing_room_numbers(hotel_id, numbers)
        if clashes:
            raise BusinessException(
                "These room numbers are already in use: "
                + ", ".join(sorted(clashes)[:10])
            )

        rooms = await self.rooms.add_many(
            [
                HotelRoom(
                    hotel_id=hotel_id,
                    room_category_id=payload.room_category_id,
                    room_number=number,
                    floor_number=payload.floor_number,
                )
                for number in numbers
            ]
        )
        await self._log(hotel_id, actor_id, f"{len(rooms)} rooms generated")
        return rooms

    async def update_room(
        self, hotel_id: int, room_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoom:
        room = await self.rooms.get(room_id)
        if room is None or int(room.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Room", room_id)

        data = payload.model_dump(exclude_unset=True)
        if (
            data.get("room_status")
            and data["room_status"] not in PHYSICAL_ROOM_STATUSES
        ):
            raise ValidationException(
                f"Room status must be one of {', '.join(PHYSICAL_ROOM_STATUSES)}.",
                details={"field": "room_status"},
            )

        # OCCUPIED is booking-driven only: it is set at check-in and cleared at
        # check-out. Blocking the manual path here stops phantom-occupied rooms
        # that no booking will ever release. Check-in/out mutate room_status by
        # direct attribute assignment and never reach this service, so the
        # booking lifecycle is unaffected.
        if data.get("room_status") == "OCCUPIED":
            raise BusinessException(
                "Rooms are marked occupied automatically when a booking checks "
                "in — this status cannot be set manually."
            )

        # A room held by a live stay must not be hand-edited: the check-out relies
        # on the allocation, and changing status/number/is_active mid-stay would
        # corrupt it. It is released automatically at check-out.
        alloc = await self.rooms.active_allocation_map(hotel_id)
        active = alloc.get(str(room.room_number).strip().upper())
        if active:
            changing_status = (
                "room_status" in data and data["room_status"] != room.room_status
            )
            changing_number = (
                "room_number" in data and data["room_number"] != room.room_number
            )
            changing_active = (
                "is_active" in data and data["is_active"] != room.is_active
            )
            if changing_status or changing_number or changing_active:
                res_label = (
                    active.get("reservation_number") or f"#{active['reservation_id']}"
                )
                raise BusinessException(
                    f"Room {room.room_number} is occupied by an active booking "
                    f"({res_label}). It is released automatically at check-out and "
                    f"cannot be edited until then."
                )

        if data.get("room_number") and data["room_number"] != room.room_number:
            if await self.rooms.existing_room_numbers(hotel_id, [data["room_number"]]):
                raise DuplicateResourceException("Room", "room_number")

        for field, value in data.items():
            setattr(room, field, value)
        await self.db.flush()

        await self._log(hotel_id, actor_id, f"Room updated: {room.room_number}")
        return room

    async def delete_room(
        self, hotel_id: int, room_id: int, actor_id: Optional[UUID]
    ) -> None:
        room = await self.rooms.get(room_id)
        if room is None or int(room.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Room", room_id)

        number = room.room_number
        await self.rooms.delete(room)
        await self._log(hotel_id, actor_id, f"Room deleted: {number}")


# ============================================================
# RATE PLANS
# ============================================================


class RatePlanService(_Base):
    async def list_plans(
        self, hotel_id: int, category_id: Optional[int] = None
    ) -> list[HotelRoomRatePlan]:
        await self._hotel_or_404(hotel_id)
        if category_id is not None:
            await self._category_or_404(hotel_id, category_id)
            return await self.rate_plans.list_for_category(category_id)
        return await self.rate_plans.list_for_hotel(hotel_id)

    def _validate(self, data: dict[str, Any], current: Any = None) -> None:
        def value(field: str, default: Any = None) -> Any:
            if field in data:
                return data[field]
            return getattr(current, field, default) if current is not None else default

        plan_type = value("plan_type")
        if plan_type not in RATE_PLAN_TYPES:
            raise ValidationException(
                f"Plan type must be one of {', '.join(RATE_PLAN_TYPES)}.",
                code=ERR_INVALID_RATE_PLAN,
                details={"field": "plan_type"},
            )
        rate_mode = value("rate_mode")
        if rate_mode not in RATE_MODES:
            raise ValidationException(
                f"Rate mode must be one of {', '.join(RATE_MODES)}.",
                code=ERR_INVALID_RATE_PLAN,
                details={"field": "rate_mode"},
            )

        validate_date_range(value("date_from"), value("date_to"), field="date_from")
        if "day_of_week_mask" in data:
            data["day_of_week_mask"] = validate_day_of_week_mask(
                data["day_of_week_mask"]
            )

        if int(value("min_nights", 1) or 1) < 1:
            raise ValidationException(
                "Minimum nights must be at least 1.", details={"field": "min_nights"}
            )

        # ABSOLUTE and PERCENT rates cannot be negative; DELTA legitimately can,
        # since a discount is expressed as a negative delta.
        rate_value = Decimal(value("rate_value", 0) or 0)
        if rate_mode != "DELTA" and rate_value < 0:
            raise ValidationException(
                "Rate value cannot be negative for this rate mode.",
                details={"field": "rate_value"},
            )
        if rate_mode == "PERCENT" and rate_value <= Decimal("-100"):
            raise ValidationException(
                "A percentage rate of -100% or lower would make the room free.",
                details={"field": "rate_value"},
            )

    async def create_plan(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoomRatePlan:
        await self._category_or_404(hotel_id, payload.room_category_id)

        data = payload.model_dump()
        self._validate(data)

        plan = await self.rate_plans.add(
            HotelRoomRatePlan(hotel_id=hotel_id, created_by=actor_id, **data)
        )
        await self._log(hotel_id, actor_id, f"Rate plan added: {payload.plan_name}")
        return plan

    async def update_plan(
        self, hotel_id: int, plan_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> HotelRoomRatePlan:
        plan = await self.rate_plans.get(plan_id)
        if plan is None or int(plan.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Rate plan", plan_id)

        data = payload.model_dump(exclude_unset=True)
        self._validate(data, current=plan)

        for field, value in data.items():
            setattr(plan, field, value)
        await self.db.flush()

        await self._log(hotel_id, actor_id, f"Rate plan updated: {plan.plan_name}")
        return plan

    async def delete_plan(
        self, hotel_id: int, plan_id: int, actor_id: Optional[UUID]
    ) -> None:
        plan = await self.rate_plans.get(plan_id)
        if plan is None or int(plan.hotel_id) != hotel_id:
            raise ResourceNotFoundException("Rate plan", plan_id)

        name = plan.plan_name
        await self.rate_plans.delete(plan)
        await self._log(hotel_id, actor_id, f"Rate plan deleted: {name}")

    async def preview(
        self, hotel_id: int, category_id: int, date_from: date, date_to: date
    ) -> dict[str, Any]:
        """A night-by-night calendar naming the winning plan for each date.

        The plan name is the point: overlapping plans resolved by precedence are
        otherwise impossible to reason about from the configuration alone.
        """
        hotel = await self._hotel_or_404(hotel_id)
        category = await self._category_or_404(hotel_id, category_id)
        validate_date_range(date_from, date_to, field="date_from")

        if (date_to - date_from).days > MAX_GENERATE_DAYS:
            raise ValidationException(
                f"Preview range cannot exceed {MAX_GENERATE_DAYS} days.",
                details={"field": "date_to"},
            )

        plans = [
            RatePlanInput(
                id=int(p.id),
                plan_name=str(p.plan_name),
                plan_type=str(p.plan_type),
                priority=int(p.priority or 0),
                date_from=p.date_from,
                date_to=p.date_to,
                day_of_week_mask=p.day_of_week_mask,
                rate_mode=str(p.rate_mode),
                rate_value=Decimal(p.rate_value or 0),
                min_nights=int(p.min_nights or 1),
                is_active=bool(p.is_active),
            )
            for p in await self.rate_plans.list_for_category(
                category_id, active_only=True, overlapping=(date_from, date_to)
            )
        ]

        overrides = {
            day: Decimal(value)
            for day, value in (
                await self.inventory.rate_overrides(category_id, date_from, date_to)
            ).items()
        }

        quote = resolve_stay_rates(
            check_in=date_from,
            check_out=date_to,
            base_price=Decimal(category.base_price or 0),
            rate_plans=plans,
            inventory_overrides=overrides,
        )

        gst_on = await platform_gst_enabled(self.db)
        slabs = [
            GstSlabInput(
                id=int(s.id),
                slab_name=str(s.slab_name),
                tariff_from=Decimal(s.tariff_from),
                tariff_to=Decimal(s.tariff_to) if s.tariff_to is not None else None,
                gst_percent=Decimal(s.gst_percent),
                hsn_code=s.hsn_code,
            )
            for s in await HotelMasterRepository(self.db).list_gst_slabs(date_from)
        ]

        nights: list[dict[str, Any]] = []
        total_with_tax = Decimal("0.00")
        for night in quote.nights:
            # The slab keys on the nightly tariff, so it is resolved per night —
            # a festival rate can legitimately cross into a higher GST slab.
            tax = compute_tax(
                amount=night.rate,
                nightly_tariff=night.rate,
                tax_mode=str(hotel.tax_mode),
                slabs=slabs,
                platform_gst_enabled=gst_on,
            )
            total_with_tax = money(total_with_tax + tax.total_amount)
            nights.append(
                {
                    "date": night.stay_date,
                    "rate": night.rate,
                    "source": night.source,
                    "plan_id": night.plan_id,
                    "plan_name": night.plan_name,
                    "gst_percent": tax.gst_percent,
                    "gst_amount": tax.gst_amount,
                    "total_with_tax": tax.total_amount,
                }
            )

        return {
            "room_category_id": category_id,
            "date_from": date_from,
            "date_to": date_to,
            "base_price": Decimal(category.base_price or 0),
            "tax_mode": hotel.tax_mode,
            "platform_gst_enabled": gst_on,
            "nights": nights,
            "total": quote.total,
            "total_with_tax": total_with_tax,
        }


# ============================================================
# INVENTORY
# ============================================================


class InventoryService(_Base):
    async def list_inventory(
        self,
        hotel_id: int,
        date_from: date,
        date_to: date,
        category_id: Optional[int] = None,
    ) -> list[HotelInventory]:
        await self._hotel_or_404(hotel_id)
        validate_date_range(date_from, date_to, field="date_from")
        return await self.inventory.list_range(
            hotel_id, date_from, date_to, category_id
        )

    async def generate(
        self, hotel_id: int, payload: Any, actor_id: Optional[UUID]
    ) -> dict[str, Any]:
        """Materialise inventory rows across a date range.

        Existing dates are skipped, never overwritten: a row may already carry
        bookings, blocks or a rate override, and regenerating would silently
        destroy them.
        """
        await self._hotel_or_404(hotel_id)
        validate_date_range(payload.date_from, payload.date_to, field="date_from")

        span = (payload.date_to - payload.date_from).days + 1
        horizon = await get_config_int(
            self.db, CONFIG_HOTEL_INVENTORY_HORIZON_DAYS, 365
        )
        limit = min(max(horizon, 1), MAX_GENERATE_DAYS)
        if span > limit:
            raise ValidationException(
                f"Inventory can be generated for at most {limit} days at a time "
                f"(requested {span}).",
                details={"field": "date_to"},
            )

        if payload.room_category_id is not None:
            categories = [
                await self._category_or_404(hotel_id, payload.room_category_id)
            ]
        else:
            categories = await self.categories.list_for_hotel(
                hotel_id, include_inactive=False
            )

        if not categories:
            raise BusinessException(
                "This hotel has no active room categories, so there is nothing to "
                "generate inventory for.",
                code=ERR_ROOM_CATEGORY_NOT_FOUND,
            )

        created = 0
        skipped = 0
        for category in categories:
            existing = await self.inventory.existing_dates(
                int(category.id), payload.date_from, payload.date_to
            )
            total = int(category.total_rooms or 0)
            rows = []
            for offset in range(span):
                day = payload.date_from + timedelta(days=offset)
                if day in existing:
                    skipped += 1
                    continue
                rows.append(
                    HotelInventory(
                        hotel_id=hotel_id,
                        room_category_id=int(category.id),
                        inventory_date=day,
                        total_rooms=total,
                        booked_rooms=0,
                        blocked_rooms=0,
                        held_rooms=0,
                        available_rooms=total,
                    )
                )
            created += await self.inventory.add_many(rows)

        await self._log(
            hotel_id,
            actor_id,
            f"Inventory generated: {created} row(s) created, {skipped} skipped",
        )
        return {
            "created": created,
            "skipped_existing": skipped,
            "categories": len(categories),
            "date_from": payload.date_from,
            "date_to": payload.date_to,
        }

    async def bulk_update(
        self,
        hotel_id: int,
        payload: Any,
        actor_id: Optional[UUID],
        days_of_week: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        """Apply a change across a date range, optionally only on given weekdays
        (0 = Monday) — the shape of "block every Sunday in December".

        booked_rooms and held_rooms are never touched here: they belong to the
        reservation flow, and an admin edit must not silently release a sold room.
        """
        await self._hotel_or_404(hotel_id)
        await self._category_or_404(hotel_id, payload.room_category_id)
        validate_date_range(payload.date_from, payload.date_to, field="date_from")

        if days_of_week and any(d < 0 or d > 6 for d in days_of_week):
            raise ValidationException(
                "Days of week must be 0 (Monday) through 6 (Sunday).",
                details={"field": "days_of_week"},
            )

        rows = await self.inventory.list_range(
            hotel_id,
            payload.date_from,
            payload.date_to,
            room_category_id=payload.room_category_id,
        )
        if not rows:
            raise BusinessException(
                "No inventory exists for that range. Generate inventory first."
            )

        updated = 0
        for row in rows:
            if days_of_week and row.inventory_date.weekday() not in days_of_week:
                continue

            if payload.total_rooms is not None:
                committed = int(row.booked_rooms or 0) + int(row.blocked_rooms or 0)
                if payload.total_rooms < committed:
                    raise BusinessException(
                        f"{row.inventory_date} already has {committed} room(s) booked "
                        f"or blocked; total rooms cannot be set to "
                        f"{payload.total_rooms}."
                    )
                row.total_rooms = payload.total_rooms

            if payload.blocked_rooms is not None:
                if payload.blocked_rooms < 0:
                    raise ValidationException(
                        "Blocked rooms cannot be negative.",
                        details={"field": "blocked_rooms"},
                    )
                if payload.blocked_rooms + int(row.booked_rooms or 0) > int(
                    row.total_rooms or 0
                ):
                    raise BusinessException(
                        f"{row.inventory_date}: blocking {payload.blocked_rooms} "
                        f"room(s) exceeds what is left after "
                        f"{int(row.booked_rooms or 0)} booking(s)."
                    )
                row.blocked_rooms = payload.blocked_rooms

            if payload.rate_override is not None:
                if payload.rate_override < 0:
                    raise ValidationException(
                        "Rate override cannot be negative.",
                        details={"field": "rate_override"},
                    )
                row.rate_override = payload.rate_override

            if payload.is_stop_sell is not None:
                row.is_stop_sell = payload.is_stop_sell

            row.available_rooms = max(
                int(row.total_rooms or 0)
                - int(row.booked_rooms or 0)
                - int(row.blocked_rooms or 0)
                - int(row.held_rooms or 0),
                0,
            )
            updated += 1

        await self.db.flush()
        await self._log(hotel_id, actor_id, f"Inventory updated: {updated} row(s)")
        return {"updated": updated}

    async def clear_rate_override(
        self,
        hotel_id: int,
        category_id: int,
        date_from: date,
        date_to: date,
        actor_id: Optional[UUID],
    ) -> dict[str, Any]:
        """Drop per-date overrides so those dates fall back to rate plans."""
        await self._hotel_or_404(hotel_id)
        await self._category_or_404(hotel_id, category_id)
        validate_date_range(date_from, date_to, field="date_from")

        rows = await self.inventory.list_range(
            hotel_id, date_from, date_to, room_category_id=category_id
        )
        cleared = 0
        for row in rows:
            if row.rate_override is not None:
                row.rate_override = None
                cleared += 1
        await self.db.flush()

        await self._log(hotel_id, actor_id, f"{cleared} rate override(s) cleared")
        return {"cleared": cleared}


__all__ = [
    "MAX_GENERATE_DAYS",
    "RoomCategoryService",
    "RatePlanService",
    "InventoryService",
]
