# ============================================================
# WAYTERO — SETTLEMENT MATH (single source of monetary truth)
# File: app/modules/booking/services/settlement_math.py
# Doc Ref: BRD Part 3 §45 (trip close), §155 (GST), BRD Part 5 (settlement)
#
# Trip close is where the money for a cab booking is decided, and the same
# arithmetic lived in four places — twice in `admin/trip_assistance_api.py`,
# once in its commission endpoint, once in `partner/booking_api.py` — each
# written independently and each in floats. Copies of a formula drift, and a
# drift here changes what a partner is paid. Every site now calls
# `compute_close_economics` with a rule resolved by `CommissionService`.
#
# Why Decimal and not float: float rounding is half-to-even on a binary
# approximation, so the same fare can produce a commission one paisa apart
# depending on how the number reached the calculator. Money here is quantised
# with ROUND_HALF_UP, which is what the invoice a customer receives and the
# statement a partner reconciles against both say.
#
# Why FLAT is a first-class type: `commission_rules.commission_type` allows
# PERCENTAGE and FLAT, and a city can be configured with a flat amount only.
# Reading only PERCENTAGE silently substituted the platform default 10% for a
# rule the admin had deliberately set — an adjustment nobody approved.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
ONE_HUNDRED = Decimal("100")

PERCENTAGE = "PERCENTAGE"
FLAT = "FLAT"

# The commission to apply when no rule is configured at all. Kept here so the
# admin and partner paths cannot disagree about the fallback.
DEFAULT_COMMISSION_VALUE = Decimal("10")

# GST rate to use when the switch is on but the configured value is not a
# number. Mirrors the pre-existing behaviour so a bad config value cannot
# silently under-collect tax by charging nothing.
FALLBACK_GST_RATE = Decimal("5")


def money(value: Any) -> Decimal:
    """Coerce any sane input (Decimal, int, str, float) to a 2-dp Decimal."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        dec = value
    else:
        # str() first: Decimal(float) would take the exact binary expansion.
        dec = Decimal(str(value))
    return dec.quantize(CENT, rounding=ROUND_HALF_UP)


def percent_of(amount: Any, percent: Any) -> Decimal:
    """`amount * percent / 100`, rounded once at the end, to the cent."""
    base = Decimal(str(amount or 0))
    rate = Decimal(str(percent or 0))
    return money(base * rate / ONE_HUNDRED)


@dataclass(frozen=True)
class CommissionRuleSpec:
    """The commission rule that was in force, as configured — not as guessed."""

    commission_type: str = PERCENTAGE
    value: Decimal = DEFAULT_COMMISSION_VALUE
    source: str = "default"
    rule_id: Optional[int] = None

    @staticmethod
    def default() -> "CommissionRuleSpec":
        return CommissionRuleSpec(
            commission_type=PERCENTAGE,
            value=DEFAULT_COMMISSION_VALUE,
            source="default_10pct",
            rule_id=None,
        )

    def apply_to(self, fare: Decimal) -> Decimal:
        """The platform's share of one fare, under this rule."""
        if self.commission_type == FLAT:
            return money(self.value)
        return percent_of(fare, self.value)


@dataclass(frozen=True)
class CloseEconomics:
    """Every figure a completed trip produces. Derived, never guessed."""

    final_amount: Decimal
    commission: CommissionRuleSpec
    platform_commission: Decimal
    partner_payout: Decimal
    gst_rate_pct: Decimal
    gst_amount: Decimal
    is_tax_invoice: bool

    @property
    def commission_percent(self) -> Decimal:
        """The percentage, for PERCENTAGE rules. Zero for a FLAT rule.

        Kept as its own field because the trip-close response and the desk UI
        already show "Commission ({percent}%)"; a flat rule shows the amount
        and the type instead rather than a percentage it does not have.
        """
        return self.commission.value if self.commission.commission_type == PERCENTAGE else ZERO

    @property
    def gross_payable(self) -> Decimal:
        """What the customer owes for the trip before advance and coupon.

        GST rides on top only when the booking is a tax invoice — the same rule
        `compute_balance_due` and the invoice PDF follow, so the cash figure
        stored at collection cannot disagree with the printed bill.
        """
        return self.final_amount + (self.gst_amount if self.is_tax_invoice else ZERO)


def gst_rate(enabled: bool, configured_rate: Any) -> Decimal:
    """Resolve the GST percentage, tolerating a non-numeric config value."""
    if not enabled:
        return ZERO
    try:
        return money(configured_rate)
    except Exception:
        return money(FALLBACK_GST_RATE)


def compute_close_economics(
    *,
    final_amount: Any,
    commission: Optional[CommissionRuleSpec] = None,
    gst_enabled: bool = False,
    gst_rate_percent: Any = 0,
) -> CloseEconomics:
    """Commission, payout and GST for a finished trip. Pure — no DB, no writes.

    Commission is taken on the fare the platform agreed (`final_amount`), not
    on the cash the partner happens to be holding and not after the coupon:
    the coupon is a discount on the customer's bill whose cost is split
    separately (`coupon_partner_share` at settlement). Folding it in here would
    charge the partner commission on money the customer never owed.
    """
    fare = money(final_amount)
    rule = commission or CommissionRuleSpec.default()
    gst = gst_rate(gst_enabled, gst_rate_percent)
    commission_amount = rule.apply_to(fare)
    return CloseEconomics(
        final_amount=fare,
        commission=rule,
        platform_commission=commission_amount,
        partner_payout=money(fare - commission_amount),
        gst_rate_pct=gst if gst_enabled else ZERO,
        gst_amount=percent_of(fare, gst) if gst_enabled else ZERO,
        is_tax_invoice=bool(gst_enabled),
    )


def commission_snapshot(
    econ: CloseEconomics,
    *,
    gst_config_source: str = "system_configurations",
    fare_close_check: Optional[dict] = None,
) -> dict:
    """The JSONB body stored on the booking at close.

    `cab_bookings.commission_config_snapshot` exists so a settlement months
    later can be proved against the rule that was live when the trip closed,
    after the admin has edited or deactivated that rule. The rule id alone is
    not enough — the row can change and can be deleted — so the type, the
    value and the GST inputs are copied in as they were applied.

    `rate_snapshot` is deliberately NOT touched here: it is the rule the
    customer was *quoted* at booking time, and overwriting it at close would
    destroy the very figure a price dispute is about. The fare the admin
    accepted at close, and the rule the sanity check compared it against, are
    recorded alongside as `fare_close_check`.
    """
    rule = econ.commission
    body: dict = {
        "commission_type": rule.commission_type,
        "commission_value": str(rule.value),
        "commission_source": rule.source,
        "commission_rule_id": rule.rule_id,
        "gst_enabled": econ.is_tax_invoice,
        "gst_rate_pct": str(econ.gst_rate_pct),
        "gst_config_source": gst_config_source,
        "final_amount": str(econ.final_amount),
        "platform_commission": str(econ.platform_commission),
        "partner_payout": str(econ.partner_payout),
    }
    if fare_close_check:
        body["fare_close_check"] = fare_close_check
    return body
