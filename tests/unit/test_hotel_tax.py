# ============================================================
# WAY TERO — HOTEL TAX TESTS
# File: tests/unit/test_hotel_tax.py
# Doc Ref: BRD Part 4 §88
#
# The two-switch design is the thing under test: the global GST_ENABLED config
# overrides everything, and per-hotel tax_mode only matters once it is on.
# ============================================================

from decimal import Decimal

import pytest

from app.modules.hotel.services.pricing import (
    GstSlabInput,
    compute_tax,
    resolve_room_gst,
)

SLAB_5 = GstSlabInput(
    id=1,
    slab_name="Room tariff up to Rs.7,500",
    tariff_from=Decimal("0.00"),
    tariff_to=Decimal("7500.00"),
    gst_percent=Decimal("5.00"),
    hsn_code="996311",
)
SLAB_18 = GstSlabInput(
    id=2,
    slab_name="Room tariff above Rs.7,500",
    tariff_from=Decimal("7500.01"),
    tariff_to=None,
    gst_percent=Decimal("18.00"),
    hsn_code="996311",
)
SLABS = [SLAB_5, SLAB_18]


class TestSlabResolution:
    @pytest.mark.parametrize(
        "tariff,expected_id",
        [
            (Decimal("0.00"), 1),
            (Decimal("999.00"), 1),
            (Decimal("7499.99"), 1),
            (Decimal("7500.00"), 1),  # boundary is inclusive on the lower slab
            (Decimal("7500.01"), 2),
            (Decimal("12000.00"), 2),
            (Decimal("250000.00"), 2),
        ],
    )
    def test_slab_boundaries(self, tariff, expected_id):
        assert resolve_room_gst(tariff, SLABS).id == expected_id

    def test_no_slab_matches_a_gap(self):
        gapped = [
            GstSlabInput(1, "Low", Decimal("0"), Decimal("1000"), Decimal("5")),
            GstSlabInput(2, "High", Decimal("5000"), None, Decimal("18")),
        ]
        assert resolve_room_gst(Decimal("2500"), gapped) is None

    def test_resolution_is_order_independent(self):
        assert resolve_room_gst(Decimal("9000"), [SLAB_18, SLAB_5]).id == 2

    def test_empty_slab_list(self):
        assert resolve_room_gst(Decimal("4000"), []) is None


class TestPlatformSwitch:
    def test_disabled_platform_gst_raises_no_tax(self):
        result = compute_tax(
            amount=Decimal("8400.00"),
            nightly_tariff=Decimal("8400.00"),
            tax_mode="EXCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=False,
        )
        assert result.gst_amount == Decimal("0.00")
        assert result.total_amount == Decimal("8400.00")
        assert result.is_tax_invoice is False
        assert result.source == "PLATFORM_GST_DISABLED"

    def test_platform_switch_overrides_inclusive_hotels(self):
        """An INCLUSIVE hotel must NOT have its rate backed out when the
        platform is not raising tax invoices — that would silently reduce the
        partner payout while the guest pays the same."""
        result = compute_tax(
            amount=Decimal("8400.00"),
            nightly_tariff=Decimal("8400.00"),
            tax_mode="INCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=False,
        )
        assert result.taxable_amount == Decimal("8400.00")
        assert result.gst_amount == Decimal("0.00")
        assert result.source == "PLATFORM_GST_DISABLED"

    def test_platform_switch_overrides_exempt_ordering(self):
        result = compute_tax(
            amount=Decimal("4000.00"),
            nightly_tariff=Decimal("4000.00"),
            tax_mode="EXEMPT",
            slabs=SLABS,
            platform_gst_enabled=False,
        )
        assert result.source == "PLATFORM_GST_DISABLED"


class TestExemptHotel:
    def test_exempt_hotel_charges_no_gst(self):
        result = compute_tax(
            amount=Decimal("4000.00"),
            nightly_tariff=Decimal("4000.00"),
            tax_mode="EXEMPT",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.gst_amount == Decimal("0.00")
        assert result.total_amount == Decimal("4000.00")
        assert result.is_tax_invoice is False
        assert result.source == "HOTEL_EXEMPT"


class TestExclusive:
    def test_gst_is_added_on_top_at_5_percent(self):
        result = compute_tax(
            amount=Decimal("4000.00"),
            nightly_tariff=Decimal("4000.00"),
            tax_mode="EXCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.gst_percent == Decimal("5.00")
        assert result.taxable_amount == Decimal("4000.00")
        assert result.gst_amount == Decimal("200.00")
        assert result.total_amount == Decimal("4200.00")
        assert result.is_tax_invoice is True
        assert result.hsn_code == "996311"

    def test_gst_is_added_on_top_at_18_percent(self):
        result = compute_tax(
            amount=Decimal("8400.00"),
            nightly_tariff=Decimal("8400.00"),
            tax_mode="EXCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.gst_percent == Decimal("18.00")
        assert result.gst_amount == Decimal("1512.00")
        assert result.total_amount == Decimal("9912.00")


class TestInclusive:
    def test_gst_is_backed_out_not_added(self):
        """The documented trap: Rs.8,400 inclusive of 18% contains Rs.1,281.36
        of GST, not Rs.1,512.00. Multiplying instead of dividing over-charges by
        r-squared of the base."""
        result = compute_tax(
            amount=Decimal("8400.00"),
            nightly_tariff=Decimal("8400.00"),
            tax_mode="INCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.gst_amount == Decimal("1281.36")
        assert result.gst_amount != Decimal("1512.00")
        assert result.taxable_amount == Decimal("7118.64")
        assert result.total_amount == Decimal("8400.00")

    def test_guest_total_is_unchanged_by_inclusive_mode(self):
        result = compute_tax(
            amount=Decimal("5000.00"),
            nightly_tariff=Decimal("5000.00"),
            tax_mode="INCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.total_amount == Decimal("5000.00")

    def test_components_reconcile_to_the_total(self):
        result = compute_tax(
            amount=Decimal("7333.33"),
            nightly_tariff=Decimal("7333.33"),
            tax_mode="INCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.taxable_amount + result.gst_amount == result.total_amount


class TestSlabIsPerRoomNotPerHotel:
    def test_two_rooms_in_one_hotel_can_land_in_different_slabs(self):
        """A property may have a 5% Standard and an 18% Suite. The slab follows
        the nightly tariff, which is why GST cannot live on the hotel row."""
        standard = compute_tax(
            Decimal("4000.00"), Decimal("4000.00"), "EXCLUSIVE", SLABS, True
        )
        suite = compute_tax(
            Decimal("12000.00"), Decimal("12000.00"), "EXCLUSIVE", SLABS, True
        )
        assert standard.gst_percent == Decimal("5.00")
        assert suite.gst_percent == Decimal("18.00")

    def test_slab_follows_nightly_tariff_not_stay_total(self):
        """A 3-night stay at Rs.4,000 totals Rs.12,000 but stays in the 5% slab —
        using the total would push cheap rooms into the luxury slab."""
        result = compute_tax(
            amount=Decimal("12000.00"),
            nightly_tariff=Decimal("4000.00"),
            tax_mode="EXCLUSIVE",
            slabs=SLABS,
            platform_gst_enabled=True,
        )
        assert result.gst_percent == Decimal("5.00")
        assert result.gst_amount == Decimal("600.00")


class TestNoMatchingSlab:
    def test_missing_slab_charges_nothing_and_raises_no_invoice(self):
        result = compute_tax(
            amount=Decimal("4000.00"),
            nightly_tariff=Decimal("4000.00"),
            tax_mode="EXCLUSIVE",
            slabs=[],
            platform_gst_enabled=True,
        )
        assert result.gst_amount == Decimal("0.00")
        assert result.is_tax_invoice is False
