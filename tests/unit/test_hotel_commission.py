# ============================================================
# WAY TERO — HOTEL COMMISSION TESTS
# File: tests/unit/test_hotel_commission.py
# Doc Ref: BRD Part 4 §87
# ============================================================

from decimal import Decimal

from app.modules.hotel.services.pricing import (
    CommissionConfigInput,
    commission_snapshot,
    compute_commission,
)


def _config(**kw) -> CommissionConfigInput:
    defaults = dict(
        commission_type="PERCENTAGE",
        commission_percent=Decimal("12.000"),
        commission_flat=Decimal("0.00"),
        min_commission=None,
        max_commission=None,
        applies_to="PER_BOOKING",
        source="HOTEL_OVERRIDE",
        config_id=7,
    )
    defaults.update(kw)
    return CommissionConfigInput(**defaults)


class TestPercentage:
    def test_basic_percentage(self):
        result = compute_commission(Decimal("10000.00"), _config())
        assert result.commission_amount == Decimal("1200.00")
        assert result.partner_payout == Decimal("8800.00")

    def test_fractional_percent_rounds_to_paise(self):
        result = compute_commission(
            Decimal("7118.64"), _config(commission_percent=Decimal("12.5"))
        )
        assert result.commission_amount == Decimal("889.83")

    def test_commission_and_payout_reconcile(self):
        result = compute_commission(Decimal("9999.99"), _config())
        assert result.commission_amount + result.partner_payout == Decimal("9999.99")


class TestFlat:
    def test_flat_ignores_the_amount(self):
        cfg = _config(commission_type="FLAT", commission_flat=Decimal("500.00"))
        assert compute_commission(
            Decimal("10000.00"), cfg
        ).commission_amount == Decimal("500.00")
        assert compute_commission(
            Decimal("20000.00"), cfg
        ).commission_amount == Decimal("500.00")


class TestHybrid:
    def test_hybrid_charges_percentage_and_flat_together(self):
        """The reason commission_rules cannot be reused: it carries one type and
        one value, so percentage-plus-flat is unrepresentable there."""
        cfg = _config(
            commission_type="HYBRID",
            commission_percent=Decimal("10"),
            commission_flat=Decimal("250.00"),
        )
        result = compute_commission(Decimal("10000.00"), cfg)
        assert result.commission_amount == Decimal("1250.00")
        assert result.partner_payout == Decimal("8750.00")


class TestPerRoomNight:
    def test_flat_multiplies_by_room_nights(self):
        cfg = _config(
            commission_type="FLAT",
            commission_flat=Decimal("200.00"),
            applies_to="PER_ROOM_NIGHT",
        )
        result = compute_commission(Decimal("12000.00"), cfg, room_nights=6)
        assert result.commission_amount == Decimal("1200.00")

    def test_hybrid_multiplies_only_the_flat_component(self):
        """The percentage component is already proportional to the amount, so
        scaling it by nights too would charge it twice."""
        cfg = _config(
            commission_type="HYBRID",
            commission_percent=Decimal("10"),
            commission_flat=Decimal("100.00"),
            applies_to="PER_ROOM_NIGHT",
        )
        result = compute_commission(Decimal("12000.00"), cfg, room_nights=3)
        assert result.commission_amount == Decimal("1500.00")

    def test_percentage_is_unaffected_by_room_nights(self):
        cfg = _config(applies_to="PER_ROOM_NIGHT")
        one = compute_commission(Decimal("10000.00"), cfg, room_nights=1)
        five = compute_commission(Decimal("10000.00"), cfg, room_nights=5)
        assert one.commission_amount == five.commission_amount

    def test_zero_room_nights_is_treated_as_one(self):
        cfg = _config(
            commission_type="FLAT",
            commission_flat=Decimal("200.00"),
            applies_to="PER_ROOM_NIGHT",
        )
        assert compute_commission(
            Decimal("5000"), cfg, room_nights=0
        ).commission_amount == Decimal("200.00")


class TestClamps:
    def test_minimum_lifts_a_small_commission(self):
        cfg = _config(commission_percent=Decimal("5"), min_commission=Decimal("500.00"))
        result = compute_commission(Decimal("4000.00"), cfg)
        assert result.commission_amount == Decimal("500.00")
        assert result.was_clamped is True

    def test_maximum_caps_a_large_commission(self):
        cfg = _config(
            commission_percent=Decimal("20"), max_commission=Decimal("2000.00")
        )
        result = compute_commission(Decimal("50000.00"), cfg)
        assert result.commission_amount == Decimal("2000.00")
        assert result.was_clamped is True

    def test_unclamped_commission_reports_so(self):
        cfg = _config(min_commission=Decimal("100"), max_commission=Decimal("5000"))
        assert compute_commission(Decimal("10000.00"), cfg).was_clamped is False


class TestGuards:
    def test_commission_never_exceeds_the_amount_it_is_charged_on(self):
        cfg = _config(commission_type="FLAT", commission_flat=Decimal("9000.00"))
        result = compute_commission(Decimal("3000.00"), cfg)
        assert result.commission_amount == Decimal("3000.00")
        assert result.partner_payout == Decimal("0.00")
        assert result.was_clamped is True

    def test_payout_is_never_negative(self):
        cfg = _config(commission_type="FLAT", commission_flat=Decimal("50000.00"))
        assert compute_commission(Decimal("1000.00"), cfg).partner_payout == Decimal(
            "0.00"
        )

    def test_zero_amount(self):
        result = compute_commission(Decimal("0.00"), _config())
        assert result.commission_amount == Decimal("0.00")
        assert result.partner_payout == Decimal("0.00")

    def test_unknown_type_charges_nothing(self):
        result = compute_commission(Decimal("10000.00"), _config(commission_type="WAT"))
        assert result.commission_amount == Decimal("0.00")
        assert result.partner_payout == Decimal("10000.00")


class TestSource:
    def test_source_is_carried_through_so_the_ui_can_explain_inheritance(self):
        for src in ("HOTEL_OVERRIDE", "CITY_RULE", "GLOBAL_RULE", "SYSTEM_DEFAULT"):
            assert (
                compute_commission(Decimal("10000"), _config(source=src)).source == src
            )


class TestSnapshot:
    def test_snapshot_is_json_safe_and_records_the_resolved_numbers(self):
        """Frozen at confirmation per BRD Rule 25 — current config cannot
        reconstruct what was in force when the booking was made."""
        result = compute_commission(Decimal("10000.00"), _config())
        snap = commission_snapshot(result)
        assert snap["commission_amount"] == "1200.00"
        assert snap["source"] == "HOTEL_OVERRIDE"
        assert snap["config_id"] == 7
        assert all(not isinstance(v, Decimal) for v in snap.values())
