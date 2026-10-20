# ============================================================
# WAYTERO — UNIT TESTS: settlement_math + compute_balance_due
# File: tests/unit/test_settlement_math.py
#
# The money rules a completed cab trip produces, proved without a database:
# rounding, the two commission rule types, GST on top of the fare, and the
# balance the customer is asked for at the desk. These numbers go on an
# invoice and into a partner's wallet, so the tests are about the cents.
# ============================================================

from decimal import Decimal

import pytest

from app.modules.booking.services import compute_balance_due
from app.modules.booking.services.settlement_math import (
    CommissionRuleSpec,
    commission_snapshot,
    compute_close_economics,
    money,
    percent_of,
)


def _d(value) -> Decimal:
    return Decimal(str(value))


# ── money / percent_of ─────────────────────────────────────────


def test_money_rounds_half_up_not_to_even():
    """₹2.675 becomes ₹2.68.

    Python's built-in round() on a float gives 2.67 here (half-to-even on a
    number that is not exactly 2.675 in binary). Invoice rounding is half-up,
    and the paisa a customer is charged is decided by which one the code runs.
    """
    assert money("2.675") == Decimal("2.68")
    assert round(2.675, 2) == 2.67  # the behaviour being replaced
    assert money(2.675) == Decimal("2.68")


def test_money_accepts_the_types_that_show_up_and_none_means_zero():
    assert money(Decimal("10.005")) == Decimal("10.01")
    assert money(10) == Decimal("10.00")
    assert money("10.4") == Decimal("10.40")
    assert money(None) == Decimal("0.00")


def test_percent_of_rounds_once_at_the_end():
    # 33.33 * 10% = 3.333 → 3.33. Rounding the rate first would not change it,
    # but rounding the fare first would; the single rounding is the point.
    assert percent_of("33.33", "10") == Decimal("3.33")
    assert percent_of(1000, 10) == Decimal("100.00")
    assert percent_of(1000, 0) == Decimal("0.00")


# ── commission rules ───────────────────────────────────────────


def test_percentage_rule_takes_its_share_of_the_fare():
    rule = CommissionRuleSpec(commission_type="PERCENTAGE", value=Decimal("12"))
    assert rule.apply_to(Decimal("1000.00")) == Decimal("120.00")


def test_flat_rule_charges_the_configured_amount_whatever_the_fare_is():
    """Bhubaneswar's CAB rule is FLAT ₹900. It is honoured, not skipped.

    The old lookups filtered `commission_type = 'PERCENTAGE'`, so this rule was
    invisible and the platform default of 10% was applied instead — ₹100 on a
    ₹1000 fare the admin had priced at ₹900.
    """
    rule = CommissionRuleSpec(commission_type="FLAT", value=Decimal("900"))
    assert rule.apply_to(Decimal("1000.00")) == Decimal("900.00")
    assert rule.apply_to(Decimal("5000.00")) == Decimal("900.00")


def test_a_flat_rule_bigger_than_the_fare_leaves_a_negative_payout():
    """No silent cap. The ledger owes the platform the difference.

    Clamping commission to the fare would hide a misconfigured rule and make the
    settlement balance sheet wrong; a negative payout is settled as a DEBIT,
    which is the honest outcome.
    """
    econ = compute_close_economics(
        final_amount=500,
        commission=CommissionRuleSpec(commission_type="FLAT", value=Decimal("900")),
    )
    assert econ.platform_commission == Decimal("900.00")
    assert econ.partner_payout == Decimal("-400.00")


# ── close economics ────────────────────────────────────────────


def test_close_splits_fare_into_commission_and_payout_that_add_up():
    econ = compute_close_economics(
        final_amount="1234.56",
        commission=CommissionRuleSpec(value=Decimal("15")),
    )
    assert econ.final_amount == Decimal("1234.56")
    assert econ.platform_commission == Decimal("185.18")
    assert econ.partner_payout == Decimal("1049.38")
    assert econ.platform_commission + econ.partner_payout == econ.final_amount


def test_gst_is_added_on_top_only_for_a_tax_invoice():
    econ = compute_close_economics(
        final_amount=1000, commission=CommissionRuleSpec(value=Decimal("10")),
        gst_enabled=True, gst_rate_percent="5",
    )
    assert econ.gst_amount == Decimal("50.00")
    assert econ.is_tax_invoice is True
    assert econ.gst_rate_pct == Decimal("5.00")
    assert econ.gross_payable == Decimal("1050.00")

    plain = compute_close_economics(
        final_amount=1000, commission=CommissionRuleSpec(value=Decimal("10")),
        gst_enabled=False, gst_rate_percent="5",
    )
    assert plain.gst_amount == Decimal("0.00")
    assert plain.gst_rate_pct == Decimal("0.00")
    assert plain.gross_payable == Decimal("1000.00")


def test_gst_is_never_taken_on_the_commission():
    """GST is charged on the fare; the platform's cut comes out of that fare."""
    econ = compute_close_economics(
        final_amount=1000, commission=CommissionRuleSpec(value=Decimal("10")),
        gst_enabled=True, gst_rate_percent="18",
    )
    assert econ.platform_commission == Decimal("100.00")
    assert econ.gst_amount == Decimal("180.00")
    assert econ.partner_payout == Decimal("900.00")


def test_an_unusable_gst_rate_config_falls_back_to_five_percent():
    econ = compute_close_economics(
        final_amount=1000, gst_enabled=True, gst_rate_percent="not-a-number"
    )
    assert econ.gst_rate_pct == Decimal("5.00")


def test_no_rule_at_all_means_the_documented_ten_percent():
    econ = compute_close_economics(final_amount=1000)
    assert econ.commission.source == "default_10pct"
    assert econ.commission.rule_id is None
    assert econ.platform_commission == Decimal("100.00")


def test_commission_percent_field_is_zero_for_a_flat_rule():
    """The response cannot show "900%" for a flat ₹900 charge.

    `commission_percent` stays on the payload for existing callers; on a flat
    rule it reads zero and `commission_value`/`commission_type` carry the truth.
    """
    flat = compute_close_economics(
        final_amount=1000,
        commission=CommissionRuleSpec(commission_type="FLAT", value=Decimal("900")),
    )
    assert flat.commission_percent == Decimal("0.00")
    assert flat.commission.value == Decimal("900")


# ── the snapshot that gets stored ─────────────────────────────


def test_snapshot_records_the_rule_as_applied_including_its_id():
    econ = compute_close_economics(
        final_amount=1000,
        commission=CommissionRuleSpec(
            commission_type="FLAT", value=Decimal("900"),
            source="city", rule_id=10,
        ),
        gst_enabled=True, gst_rate_percent="12",
    )
    snap = commission_snapshot(econ)
    assert snap["commission_type"] == "FLAT"
    assert snap["commission_value"] == str(econ.commission.value)
    assert snap["commission_rule_id"] == 10
    assert snap["commission_source"] == "city"
    assert snap["gst_enabled"] is True
    assert snap["gst_rate_pct"] == "12.00"
    assert snap["platform_commission"] == "900.00"
    assert snap["partner_payout"] == "100.00"
    # Decimals are stored as strings so a JSON reader cannot lose the scale.
    assert all(isinstance(v, str) for v in (
        snap["final_amount"], snap["platform_commission"], snap["partner_payout"]
    ))


def test_snapshot_keeps_the_close_time_fare_check_alongside_the_commission():
    econ = compute_close_economics(final_amount=1000)
    snap = commission_snapshot(
        econ, fare_close_check={"rule_id": 42, "calculated_fare": "980.00"}
    )
    assert snap["fare_close_check"]["rule_id"] == 42
    assert "rate_snapshot" not in snap  # the booking-time quote is not touched


def test_snapshot_without_a_fare_check_omits_the_key_rather_than_storing_null():
    snap = commission_snapshot(compute_close_economics(final_amount=1000))
    assert "fare_close_check" not in snap


# ── what the customer owes ────────────────────────────────────


def test_balance_is_gross_of_gst_when_it_is_a_tax_invoice():
    """The desk must ask for what the PDF prints: fare + GST − coupon − advance."""
    assert compute_balance_due(
        final_amount=1000, coupon_discount=100, advance_paid=200,
        gst_amount=50, is_tax_invoice=True,
    ) == Decimal("750.00")
    assert compute_balance_due(
        final_amount=1000, coupon_discount=100, advance_paid=200,
        gst_amount=50, is_tax_invoice=False,
    ) == Decimal("700.00")


def test_balance_never_goes_negative():
    assert compute_balance_due(final_amount=500, advance_paid=800) == Decimal("0.00")


def test_balance_returns_decimal_and_still_takes_legacy_floats():
    out = compute_balance_due(final_amount=99.99, advance_paid=33.33)
    assert isinstance(out, Decimal)
    assert out == Decimal("66.66")


def test_balance_quantises_a_fractional_paisa_input():
    # A 1/3-rupee discount must not leave the ledger holding 333.3333333.
    assert compute_balance_due(
        final_amount=1000, coupon_discount=Decimal("333.333333")
    ) == Decimal("666.67")


@pytest.mark.parametrize(
    "final,coupon,advance,expected",
    [
        (1000, 0, 0, "1000.00"),
        (1000, 1000, 0, "0.00"),
        (1000, 0, 1000, "0.00"),
        (0.01, 0, 0, "0.01"),
    ],
)
def test_balance_table(final, coupon, advance, expected):
    assert compute_balance_due(
        final_amount=final, coupon_discount=coupon, advance_paid=advance
    ) == _d(expected)
