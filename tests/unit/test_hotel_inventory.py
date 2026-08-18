# ============================================================
# WAY TERO — HOTEL INVENTORY TESTS
# File: tests/unit/test_hotel_inventory.py
# Doc Ref: SRS Part 5 §161-162, §165
#
# Inventory is the one place where an admin edit can destroy a sold booking.
# These tests pin the two invariants that prevent it: materialisation never
# overwrites an existing row, and no edit path can push available_rooms below
# zero or release a committed room.
# ============================================================

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.exceptions import BusinessException, ValidationException
from app.modules.hotel.services.rooms import MAX_GENERATE_DAYS, InventoryService


def _payload(**kw):
    defaults = dict(
        room_category_id=1,
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 7),
        total_rooms=None,
        blocked_rooms=None,
        rate_override=None,
        is_stop_sell=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _row(day, total=10, booked=0, blocked=0, held=0):
    return SimpleNamespace(
        inventory_date=day,
        total_rooms=total,
        booked_rooms=booked,
        blocked_rooms=blocked,
        held_rooms=held,
        available_rooms=max(total - booked - blocked - held, 0),
        rate_override=None,
        is_stop_sell=False,
    )


class _StubDb:
    async def flush(self):
        return None


def _service(
    *,
    categories=None,
    existing_dates=None,
    rows=None,
    horizon=365,
    monkeypatch=None,
):
    categories = (
        [SimpleNamespace(id=1, hotel_id=1, total_rooms=10)]
        if categories is None
        else categories
    )
    service = InventoryService(_StubDb())  # type: ignore[arg-type]
    added: list = []
    logs: list = []

    async def get_lite(_hid):
        return SimpleNamespace(id=1, status="DRAFT")

    async def get_category(cid):
        for c in categories:
            if int(c.id) == cid:
                return c
        return None

    async def list_for_hotel(_hid, include_inactive=False):
        return list(categories)

    async def get_existing(_cid, _df, _dt):
        return set(existing_dates or [])

    async def add_many(new_rows):
        added.extend(new_rows)
        return len(new_rows)

    async def list_range(_hid, _df, _dt, room_category_id=None):
        return list(rows or [])

    async def add_log(log):
        logs.append(log)
        return log

    service.hotels.get_lite = get_lite  # type: ignore[method-assign]
    service.categories.get = get_category  # type: ignore[method-assign]
    service.categories.list_for_hotel = list_for_hotel  # type: ignore[method-assign]
    service.inventory.existing_dates = get_existing  # type: ignore[method-assign]
    service.inventory.add_many = add_many  # type: ignore[method-assign]
    service.inventory.list_range = list_range  # type: ignore[method-assign]
    service.verification.add_log = add_log  # type: ignore[method-assign]

    async def stub_horizon(_db, _key, default):
        return horizon

    monkeypatch.setattr("app.modules.hotel.services.rooms.get_config_int", stub_horizon)
    return service, added


class TestGenerate:
    @pytest.mark.asyncio
    async def test_generates_one_row_per_date_inclusive(self, monkeypatch):
        service, added = _service(monkeypatch=monkeypatch)

        result = await service.generate(
            1, _payload(date_from=date(2026, 9, 1), date_to=date(2026, 9, 7)), None
        )

        assert result["created"] == 7
        assert len(added) == 7
        assert added[0].inventory_date == date(2026, 9, 1)
        assert added[-1].inventory_date == date(2026, 9, 7)

    @pytest.mark.asyncio
    async def test_new_rows_start_fully_available(self, monkeypatch):
        service, added = _service(monkeypatch=monkeypatch)
        await service.generate(1, _payload(), None)

        for row in added:
            assert row.total_rooms == 10
            assert row.available_rooms == 10
            assert row.booked_rooms == 0
            assert row.blocked_rooms == 0

    @pytest.mark.asyncio
    async def test_existing_dates_are_skipped_not_overwritten(self, monkeypatch):
        """A row may already carry bookings, blocks or a rate override.
        Regenerating over it would silently destroy them."""
        already = {date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)}
        service, added = _service(existing_dates=already, monkeypatch=monkeypatch)

        result = await service.generate(1, _payload(), None)

        assert result["created"] == 4
        assert result["skipped_existing"] == 3
        assert not [r for r in added if r.inventory_date in already]

    @pytest.mark.asyncio
    async def test_regenerating_the_same_range_is_a_no_op(self, monkeypatch):
        whole = {date(2026, 9, 1) + timedelta(days=n) for n in range(7)}
        service, added = _service(existing_dates=whole, monkeypatch=monkeypatch)

        result = await service.generate(1, _payload(), None)

        assert result["created"] == 0
        assert result["skipped_existing"] == 7
        assert added == []

    @pytest.mark.asyncio
    async def test_generates_for_every_category_when_none_is_named(self, monkeypatch):
        service, added = _service(
            categories=[
                SimpleNamespace(id=1, hotel_id=1, total_rooms=10),
                SimpleNamespace(id=2, hotel_id=1, total_rooms=4),
            ],
            monkeypatch=monkeypatch,
        )

        result = await service.generate(1, _payload(room_category_id=None), None)

        assert result["categories"] == 2
        assert result["created"] == 14
        assert {r.total_rooms for r in added} == {10, 4}

    @pytest.mark.asyncio
    async def test_refuses_a_hotel_with_no_active_categories(self, monkeypatch):
        service, _ = _service(categories=[], monkeypatch=monkeypatch)

        with pytest.raises(BusinessException):
            await service.generate(1, _payload(room_category_id=None), None)

    @pytest.mark.asyncio
    async def test_range_beyond_the_configured_horizon_is_refused(self, monkeypatch):
        service, added = _service(horizon=30, monkeypatch=monkeypatch)

        with pytest.raises(ValidationException) as exc:
            await service.generate(
                1,
                _payload(date_from=date(2026, 9, 1), date_to=date(2026, 12, 31)),
                None,
            )

        assert "30 days" in exc.value.message
        assert added == []

    @pytest.mark.asyncio
    async def test_horizon_cannot_exceed_the_hard_ceiling(self, monkeypatch):
        """The config is editable at runtime; the ceiling is not."""
        service, _ = _service(horizon=100_000, monkeypatch=monkeypatch)

        with pytest.raises(ValidationException):
            await service.generate(
                1,
                _payload(
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 1) + timedelta(days=MAX_GENERATE_DAYS + 5),
                ),
                None,
            )

    @pytest.mark.asyncio
    async def test_inverted_date_range_is_refused(self, monkeypatch):
        service, _ = _service(monkeypatch=monkeypatch)

        with pytest.raises(ValidationException):
            await service.generate(
                1,
                _payload(date_from=date(2026, 9, 7), date_to=date(2026, 9, 1)),
                None,
            )


class TestBulkUpdate:
    @pytest.mark.asyncio
    async def test_blocking_rooms_reduces_availability(self, monkeypatch):
        rows = [_row(date(2026, 9, 1) + timedelta(days=n)) for n in range(3)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        result = await service.bulk_update(1, _payload(blocked_rooms=4), None)

        assert result["updated"] == 3
        for row in rows:
            assert row.blocked_rooms == 4
            assert row.available_rooms == 6

    @pytest.mark.asyncio
    async def test_booked_rooms_are_never_touched(self, monkeypatch):
        """They belong to the reservation flow. An admin edit must not silently
        release a sold room."""
        rows = [_row(date(2026, 9, 1), total=10, booked=3)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        await service.bulk_update(1, _payload(blocked_rooms=2), None)

        assert rows[0].booked_rooms == 3
        assert rows[0].available_rooms == 5

    @pytest.mark.asyncio
    async def test_held_rooms_count_against_availability(self, monkeypatch):
        rows = [_row(date(2026, 9, 1), total=10, booked=2, held=3)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        await service.bulk_update(1, _payload(blocked_rooms=1), None)

        assert rows[0].held_rooms == 3
        assert rows[0].available_rooms == 4

    @pytest.mark.asyncio
    async def test_cannot_block_more_rooms_than_remain_after_bookings(
        self, monkeypatch
    ):
        rows = [_row(date(2026, 9, 1), total=10, booked=8)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        with pytest.raises(BusinessException):
            await service.bulk_update(1, _payload(blocked_rooms=5), None)

        assert rows[0].blocked_rooms == 0

    @pytest.mark.asyncio
    async def test_cannot_shrink_total_below_what_is_committed(self, monkeypatch):
        """The DB CHECK would reject this with a constraint error nobody can act
        on, so it is caught here with the date named."""
        rows = [_row(date(2026, 9, 4), total=10, booked=6, blocked=1)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        with pytest.raises(BusinessException) as exc:
            await service.bulk_update(1, _payload(total_rooms=5), None)

        assert "2026-09-04" in exc.value.message
        assert rows[0].total_rooms == 10

    @pytest.mark.asyncio
    async def test_availability_never_goes_negative(self, monkeypatch):
        rows = [_row(date(2026, 9, 1), total=5, booked=5)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        await service.bulk_update(1, _payload(is_stop_sell=True), None)

        assert rows[0].available_rooms == 0

    @pytest.mark.asyncio
    async def test_day_of_week_filter_touches_only_matching_dates(self, monkeypatch):
        # 2026-09-01 is a Tuesday, so Sundays fall on the 6th.
        rows = [_row(date(2026, 9, 1) + timedelta(days=n)) for n in range(7)]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        result = await service.bulk_update(
            1, _payload(is_stop_sell=True), None, days_of_week=[6]
        )

        assert result["updated"] == 1
        stopped = [r for r in rows if r.is_stop_sell]
        assert len(stopped) == 1
        assert stopped[0].inventory_date == date(2026, 9, 6)

    @pytest.mark.asyncio
    async def test_invalid_day_of_week_is_refused(self, monkeypatch):
        rows = [_row(date(2026, 9, 1))]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        with pytest.raises(ValidationException):
            await service.bulk_update(
                1, _payload(is_stop_sell=True), None, days_of_week=[7]
            )

    @pytest.mark.asyncio
    async def test_rate_override_is_applied(self, monkeypatch):
        rows = [_row(date(2026, 9, 1))]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        await service.bulk_update(1, _payload(rate_override=Decimal("7500.00")), None)

        assert rows[0].rate_override == Decimal("7500.00")

    @pytest.mark.asyncio
    async def test_negative_rate_override_is_refused(self, monkeypatch):
        rows = [_row(date(2026, 9, 1))]
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        with pytest.raises(ValidationException):
            await service.bulk_update(1, _payload(rate_override=Decimal("-100")), None)

    @pytest.mark.asyncio
    async def test_updating_a_range_with_no_inventory_says_so(self, monkeypatch):
        service, _ = _service(rows=[], monkeypatch=monkeypatch)

        with pytest.raises(BusinessException) as exc:
            await service.bulk_update(1, _payload(blocked_rooms=1), None)

        assert "Generate inventory first" in exc.value.message


class TestClearRateOverride:
    @pytest.mark.asyncio
    async def test_clearing_falls_dates_back_to_rate_plans(self, monkeypatch):
        rows = [_row(date(2026, 9, 1)), _row(date(2026, 9, 2))]
        rows[0].rate_override = Decimal("9000")
        service, _ = _service(rows=rows, monkeypatch=monkeypatch)

        result = await service.clear_rate_override(
            1, 1, date(2026, 9, 1), date(2026, 9, 2), None
        )

        assert result["cleared"] == 1
        assert rows[0].rate_override is None
