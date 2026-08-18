# ============================================================
# WAY TERO — HOTEL READINESS CHECKLIST TESTS
# File: tests/unit/test_hotel_readiness.py
# Doc Ref: BRD Part 4 §59, SRS Part 5 §157
#
# The readiness report drives BOTH the submit gate and the UI completeness
# meter, so a drift between them is a hotel an admin cannot submit for a reason
# the screen never showed. These tests pin that contract.
#
# The repositories are stubbed rather than hitting Postgres: the logic under
# test is the checklist, not the SQL.
# ============================================================

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.exceptions import ResourceNotFoundException
from app.modules.hotel.constants import (
    DOC_BANK_PROOF,
    DOC_GST_CERTIFICATE,
    DOC_PAN_CARD,
    DOC_STATUS_PENDING,
    DOC_STATUS_VERIFIED,
    DOC_TRADE_LICENSE,
    HOTEL_STATUS_DRAFT,
)
from app.modules.hotel.services import HotelReadinessService
from app.modules.hotel.services.pricing import CommissionConfigInput


def _hotel(**kw):
    defaults = dict(
        id=1,
        partner_id=7,
        status=HOTEL_STATUS_DRAFT,
        hotel_name="Hotel Sunrise",
        hotel_type="HOTEL",
        address="12 MG Road",
        city_id=3,
        contact_person="Asha Nair",
        contact_number="9876543210",
        email="stay@sunrise.test",
        hotel_category_id=2,
        latitude=Decimal("12.97"),
        longitude=Decimal("77.59"),
        is_gst_registered=False,
        tax_mode="EXCLUSIVE",
        slug="hotel-sunrise-bengaluru",
        seo_title="Hotel Sunrise",
        seo_description="Stay in Bengaluru",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _doc(document_type, status=DOC_STATUS_VERIFIED):
    return SimpleNamespace(document_type=document_type, verification_status=status)


def _category(base_price="4000", total_rooms=10, category_id=1):
    return SimpleNamespace(
        id=category_id, base_price=Decimal(base_price), total_rooms=total_rooms
    )


class _StubResult:
    """Stands in for the Result of the one raw bank-account probe."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StubDb:
    def __init__(self, bank_verified=True):
        self.bank_verified = bank_verified

    async def execute(self, _stmt, _params=None):
        return _StubResult(1 if self.bank_verified else None)


def _service(
    hotel=None,
    *,
    images=3,
    primary=1,
    amenities=4,
    policy_set=True,
    categories=None,
    inventory_rows=30,
    documents=None,
    bank_verified=True,
    commission=CommissionConfigInput(
        commission_type="PERCENTAGE",
        commission_percent=Decimal("12"),
        commission_flat=Decimal("0"),
        source="SYSTEM_DEFAULT",
    ),
    monkeypatch=None,
):
    hotel = hotel or _hotel()
    categories = [_category()] if categories is None else categories
    documents = (
        [_doc(DOC_PAN_CARD), _doc(DOC_TRADE_LICENSE), _doc(DOC_BANK_PROOF)]
        if documents is None
        else documents
    )

    service = HotelReadinessService(_StubDb(bank_verified))  # type: ignore[arg-type]

    async def get_by_id(_hid):
        return hotel

    async def get_amenity_ids(_hid):
        return list(range(amenities))

    async def list_images(_hid):
        return [SimpleNamespace(id=n, is_primary=(n < primary)) for n in range(images)]

    async def get_policy(_hid):
        return SimpleNamespace(cancellation_free_hours=48) if policy_set else None

    async def list_documents(_hid):
        return list(documents)

    async def list_for_hotel(_hid, include_inactive=False):
        return list(categories)

    async def count_for_category(_cid):
        return inventory_rows

    service.repo.get_by_id = get_by_id  # type: ignore[method-assign]
    service.repo.get_amenity_ids = get_amenity_ids  # type: ignore[method-assign]
    service.repo.list_images = list_images  # type: ignore[method-assign]
    service.repo.get_policy = get_policy  # type: ignore[method-assign]
    service.repo.list_documents = list_documents  # type: ignore[method-assign]
    service.categories.list_for_hotel = list_for_hotel  # type: ignore[method-assign]
    service.inventory.count_for_category = count_for_category  # type: ignore[method-assign]

    async def stub_config_int(_db, _key, default):
        return default

    async def stub_gst(_db):
        return False

    async def stub_commission(_db, _hotel, on_date=None):
        return commission

    monkeypatch.setattr("app.modules.hotel.services.get_config_int", stub_config_int)
    monkeypatch.setattr("app.modules.hotel.services.platform_gst_enabled", stub_gst)
    monkeypatch.setattr(
        "app.modules.hotel.services.resolve_hotel_commission", stub_commission
    )
    return service


def _by_key(report):
    return {c["key"]: c for c in report["checks"]}


class TestFullyReadyHotel:
    @pytest.mark.asyncio
    async def test_complete_hotel_can_submit_but_not_approve_until_verified(
        self, monkeypatch
    ):
        service = _service(monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert report["can_submit"] is True
        assert report["can_approve"] is True
        assert report["completeness_percent"] == 100

    @pytest.mark.asyncio
    async def test_every_check_reports_a_hint_only_when_failing(self, monkeypatch):
        service = _service(images=1, monkeypatch=monkeypatch)
        report = await service.compute(1)

        for check in report["checks"]:
            if check["passed"]:
                assert check["hint"] is None
            else:
                assert check["hint"]

    @pytest.mark.asyncio
    async def test_missing_hotel_raises_not_found(self, monkeypatch):
        service = _service(monkeypatch=monkeypatch)

        async def missing(_hid):
            return None

        service.repo.get_by_id = missing  # type: ignore[method-assign]
        with pytest.raises(ResourceNotFoundException):
            await service.compute(1)


class TestSubmitApproveSplit:
    @pytest.mark.asyncio
    async def test_unverified_documents_block_approve_not_submit(self, monkeypatch):
        """Submitting is what asks an officer to verify. Requiring verification
        before submission would deadlock the pipeline."""
        service = _service(
            documents=[
                _doc(DOC_PAN_CARD, DOC_STATUS_PENDING),
                _doc(DOC_TRADE_LICENSE, DOC_STATUS_PENDING),
                _doc(DOC_BANK_PROOF, DOC_STATUS_PENDING),
            ],
            monkeypatch=monkeypatch,
        )
        report = await service.compute(1)
        checks = _by_key(report)

        assert checks["documents_mandatory"]["passed"] is True
        assert checks["documents_verified"]["passed"] is False
        assert checks["documents_verified"]["blocks_submit"] is False
        assert checks["documents_verified"]["blocks_approve"] is True

        assert report["can_submit"] is True
        assert report["can_approve"] is False

    @pytest.mark.asyncio
    async def test_missing_mandatory_document_blocks_both(self, monkeypatch):
        service = _service(
            documents=[_doc(DOC_PAN_CARD), _doc(DOC_TRADE_LICENSE)],
            monkeypatch=monkeypatch,
        )
        report = await service.compute(1)

        assert _by_key(report)["documents_mandatory"]["passed"] is False
        assert report["can_submit"] is False
        assert report["can_approve"] is False


class TestConditionalGstDocument:
    @pytest.mark.asyncio
    async def test_gst_certificate_not_required_when_not_registered(self, monkeypatch):
        """A property below the registration threshold legitimately has none."""
        service = _service(
            hotel=_hotel(is_gst_registered=False), monkeypatch=monkeypatch
        )
        report = await service.compute(1)
        assert _by_key(report)["documents_mandatory"]["passed"] is True

    @pytest.mark.asyncio
    async def test_gst_certificate_required_when_registered(self, monkeypatch):
        service = _service(
            hotel=_hotel(is_gst_registered=True), monkeypatch=monkeypatch
        )
        report = await service.compute(1)

        check = _by_key(report)["documents_mandatory"]
        assert check["passed"] is False
        assert DOC_GST_CERTIFICATE in check["hint"]

    @pytest.mark.asyncio
    async def test_registered_hotel_passes_once_certificate_uploaded(self, monkeypatch):
        service = _service(
            hotel=_hotel(is_gst_registered=True),
            documents=[
                _doc(DOC_PAN_CARD),
                _doc(DOC_TRADE_LICENSE),
                _doc(DOC_BANK_PROOF),
                _doc(DOC_GST_CERTIFICATE),
            ],
            monkeypatch=monkeypatch,
        )
        report = await service.compute(1)
        assert _by_key(report)["documents_mandatory"]["passed"] is True


class TestLatestDocumentWins:
    @pytest.mark.asyncio
    async def test_newest_upload_of_a_type_supersedes_the_older_one(self, monkeypatch):
        """list_documents is newest-first. A re-upload after a rejection must
        clear the rejection, not be shadowed by it."""
        service = _service(
            documents=[
                _doc(DOC_PAN_CARD, DOC_STATUS_VERIFIED),
                _doc(DOC_PAN_CARD, "REJECTED"),
                _doc(DOC_TRADE_LICENSE),
                _doc(DOC_BANK_PROOF),
            ],
            monkeypatch=monkeypatch,
        )
        report = await service.compute(1)
        assert _by_key(report)["documents_verified"]["passed"] is True


class TestBlockingChecks:
    @pytest.mark.asyncio
    async def test_unpriced_room_category_blocks_submit(self, monkeypatch):
        service = _service(
            categories=[_category(base_price="0")], monkeypatch=monkeypatch
        )
        report = await service.compute(1)

        assert _by_key(report)["room_categories"]["passed"] is False
        assert report["can_submit"] is False

    @pytest.mark.asyncio
    async def test_no_inventory_blocks_submit(self, monkeypatch):
        service = _service(inventory_rows=0, monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert _by_key(report)["inventory_ready"]["passed"] is False
        assert report["can_submit"] is False

    @pytest.mark.asyncio
    async def test_zero_room_count_blocks_even_with_inventory_rows(self, monkeypatch):
        service = _service(
            categories=[_category(total_rooms=0)], monkeypatch=monkeypatch
        )
        report = await service.compute(1)
        assert _by_key(report)["inventory_ready"]["passed"] is False

    @pytest.mark.asyncio
    async def test_unverified_bank_account_blocks_submit(self, monkeypatch):
        """Cheaper to surface here than at the first settlement run."""
        service = _service(bank_verified=False, monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert _by_key(report)["bank_account"]["passed"] is False
        assert report["can_submit"] is False

    @pytest.mark.asyncio
    async def test_too_few_images_blocks_submit(self, monkeypatch):
        service = _service(images=2, monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert _by_key(report)["images_uploaded"]["passed"] is False
        assert report["can_submit"] is False

    @pytest.mark.asyncio
    async def test_images_without_exactly_one_primary_fail(self, monkeypatch):
        service = _service(images=4, primary=2, monkeypatch=monkeypatch)
        report = await service.compute(1)
        assert _by_key(report)["images_uploaded"]["passed"] is False

    @pytest.mark.asyncio
    async def test_missing_policy_blocks_submit(self, monkeypatch):
        service = _service(policy_set=False, monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert _by_key(report)["policies_set"]["passed"] is False
        assert report["can_submit"] is False

    @pytest.mark.asyncio
    async def test_missing_profile_field_names_it_in_the_hint(self, monkeypatch):
        service = _service(hotel=_hotel(email=None), monkeypatch=monkeypatch)
        report = await service.compute(1)

        check = _by_key(report)["profile_complete"]
        assert check["passed"] is False
        assert "email" in check["hint"]


class TestAdvisoryChecks:
    """These lower the completeness meter but must never block a submission."""

    @pytest.mark.asyncio
    async def test_missing_coordinates_are_advisory(self, monkeypatch):
        service = _service(
            hotel=_hotel(latitude=None, longitude=None), monkeypatch=monkeypatch
        )
        report = await service.compute(1)

        assert _by_key(report)["location_set"]["passed"] is False
        assert report["can_submit"] is True
        assert report["completeness_percent"] < 100

    @pytest.mark.asyncio
    async def test_thin_amenities_are_advisory(self, monkeypatch):
        service = _service(amenities=1, monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert _by_key(report)["amenities_selected"]["passed"] is False
        assert report["can_submit"] is True

    @pytest.mark.asyncio
    async def test_missing_seo_is_advisory(self, monkeypatch):
        service = _service(hotel=_hotel(seo_title=None), monkeypatch=monkeypatch)
        report = await service.compute(1)

        assert _by_key(report)["seo_complete"]["passed"] is False
        assert report["can_submit"] is True

    @pytest.mark.asyncio
    async def test_tax_hint_explains_that_platform_gst_is_off(self, monkeypatch):
        """The per-hotel tax_mode is inert while the global switch is off. The
        hint has to say so, or the setting looks broken."""
        service = _service(hotel=_hotel(tax_mode=None), monkeypatch=monkeypatch)
        report = await service.compute(1)

        check = _by_key(report)["tax_configured"]
        assert check["passed"] is False
        assert check["blocks_submit"] is False
        assert "Platform GST is off" in check["hint"]
