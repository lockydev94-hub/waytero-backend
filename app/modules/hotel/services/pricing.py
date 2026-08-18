# ============================================================
# WAY TERO — HOTEL PRICING, TAX AND COMMISSION RESOLVERS
# File: app/modules/hotel/services/pricing.py
# Doc Ref: BRD Part 4 §70 (rate plans), §87 (commission), §88 (GST)
#          SRS Part 5 §163-166
#
# Deliberately ORM-free: every function takes plain dataclasses and returns
# plain dataclasses. This is where the money is calculated, so it must be
# testable without a database.
#
# Every resolver reports the SOURCE of its answer. An admin looking at a
# commission of 12% needs to know whether that is this hotel's override, a city
# rule, or the system default — otherwise "why is this number what it is"
# becomes a support conversation.
# ============================================================

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from app.modules.hotel.constants import (
    COMMISSION_APPLIES_PER_ROOM_NIGHT,
    COMMISSION_TYPE_FLAT,
    COMMISSION_TYPE_HYBRID,
    COMMISSION_TYPE_PERCENTAGE,
    RATE_MODE_DELTA,
    RATE_MODE_PERCENT,
    RATE_PLAN_PRECEDENCE,
    TAX_MODE_EXEMPT,
    TAX_MODE_INCLUSIVE,
    TAX_SOURCE_HOTEL_EXEMPT,
    TAX_SOURCE_PLATFORM_DISABLED,
    TAX_SOURCE_SLAB,
)

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value: Decimal) -> Decimal:
    """Round half-up to paise. Banker's rounding would systematically shave
    receipts against the partner on .005 boundaries."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# ============================================================
# NIGHTLY RATE
# ============================================================


@dataclass(frozen=True)
class RatePlanInput:
    """A rate plan flattened out of the ORM."""

    id: int
    plan_name: str
    plan_type: str
    priority: int
    date_from: date
    date_to: date
    day_of_week_mask: Optional[str]
    rate_mode: str
    rate_value: Decimal
    min_nights: int = 1
    is_active: bool = True


@dataclass(frozen=True)
class NightlyRate:
    stay_date: date
    rate: Decimal
    source: str  # BASE_PRICE | INVENTORY_OVERRIDE | RATE_PLAN
    plan_id: Optional[int] = None
    plan_name: Optional[str] = None


def _plan_applies(plan: RatePlanInput, stay_date: date, nights: int) -> bool:
    if not plan.is_active:
        return False
    if not (plan.date_from <= stay_date <= plan.date_to):
        return False
    if plan.min_nights > nights:
        return False
    if plan.day_of_week_mask:
        # Monday-first, matching date.weekday()
        if plan.day_of_week_mask[stay_date.weekday()] != "1":
            return False
    return True


def _plan_sort_key(plan: RatePlanInput) -> tuple[int, int, int]:
    return (RATE_PLAN_PRECEDENCE.get(plan.plan_type, 0), plan.priority, plan.id)


def _apply_rate_mode(base_price: Decimal, plan: RatePlanInput) -> Decimal:
    if plan.rate_mode == RATE_MODE_PERCENT:
        return money(base_price * (Decimal("100") + plan.rate_value) / Decimal("100"))
    if plan.rate_mode == RATE_MODE_DELTA:
        return money(base_price + plan.rate_value)
    return money(plan.rate_value)


def resolve_nightly_rate(
    stay_date: date,
    base_price: Decimal,
    rate_plans: Sequence[RatePlanInput],
    nights: int = 1,
    inventory_override: Optional[Decimal] = None,
) -> NightlyRate:
    """Resolve the rate for one night.

    Precedence: a per-date inventory override beats every rate plan, because an
    admin who typed a number onto a specific date meant that number. Otherwise
    the highest-precedence applicable plan wins
    (FESTIVAL > SEASONAL > WEEKEND > PROMOTIONAL, then priority, then id).
    """
    if inventory_override is not None:
        return NightlyRate(
            stay_date=stay_date,
            rate=money(inventory_override),
            source="INVENTORY_OVERRIDE",
        )

    applicable = [p for p in rate_plans if _plan_applies(p, stay_date, nights)]
    if not applicable:
        return NightlyRate(
            stay_date=stay_date, rate=money(base_price), source="BASE_PRICE"
        )

    winner = max(applicable, key=_plan_sort_key)
    return NightlyRate(
        stay_date=stay_date,
        rate=_apply_rate_mode(base_price, winner),
        source="RATE_PLAN",
        plan_id=winner.id,
        plan_name=winner.plan_name,
    )


@dataclass
class StayQuote:
    nights: list[NightlyRate] = field(default_factory=list)
    total: Decimal = ZERO

    @property
    def average_nightly_rate(self) -> Decimal:
        if not self.nights:
            return ZERO
        return money(self.total / Decimal(len(self.nights)))


def resolve_stay_rates(
    check_in: date,
    check_out: date,
    base_price: Decimal,
    rate_plans: Sequence[RatePlanInput],
    inventory_overrides: Optional[dict[date, Decimal]] = None,
) -> StayQuote:
    """Rate per night across a stay. check_out is exclusive — the guest does not
    pay for the night they leave."""
    overrides = inventory_overrides or {}
    nights_count = (check_out - check_in).days
    quote = StayQuote()
    for offset in range(max(nights_count, 0)):
        stay_date = check_in + timedelta(days=offset)
        night = resolve_nightly_rate(
            stay_date=stay_date,
            base_price=base_price,
            rate_plans=rate_plans,
            nights=nights_count,
            inventory_override=overrides.get(stay_date),
        )
        quote.nights.append(night)
        quote.total = money(quote.total + night.rate)
    return quote


# ============================================================
# TAX
# ============================================================


@dataclass(frozen=True)
class GstSlabInput:
    id: int
    slab_name: str
    tariff_from: Decimal
    tariff_to: Optional[Decimal]  # None = open-ended top slab
    gst_percent: Decimal
    hsn_code: Optional[str] = None


@dataclass(frozen=True)
class TaxResult:
    gst_percent: Decimal
    taxable_amount: Decimal
    gst_amount: Decimal
    total_amount: Decimal
    is_tax_invoice: bool
    source: str
    slab_id: Optional[int] = None
    slab_name: Optional[str] = None
    hsn_code: Optional[str] = None


def resolve_room_gst(
    nightly_tariff: Decimal, slabs: Sequence[GstSlabInput]
) -> Optional[GstSlabInput]:
    """Pick the slab for a nightly tariff.

    The slab is a function of the per-room per-night tariff, not of the hotel,
    so one property can legitimately have a 5% Standard room and an 18% Suite.
    """
    for slab in sorted(slabs, key=lambda s: s.tariff_from):
        upper_ok = slab.tariff_to is None or nightly_tariff <= slab.tariff_to
        if nightly_tariff >= slab.tariff_from and upper_ok:
            return slab
    return None


def compute_tax(
    amount: Decimal,
    nightly_tariff: Decimal,
    tax_mode: str,
    slabs: Sequence[GstSlabInput],
    platform_gst_enabled: bool,
) -> TaxResult:
    """Apply GST to a room amount.

    Two switches, checked in this order:

      1. platform_gst_enabled — the global GST_ENABLED config. When off, the
         platform raises no tax invoice at all and this overrides everything
         below. A hotel marked INCLUSIVE does NOT get its rate backed out.
      2. tax_mode — the per-hotel setting, meaningful only once (1) is on.

    EXCLUSIVE: `amount` is pre-tax; GST is added on top.
    INCLUSIVE: `amount` already contains GST; it is backed out, so the guest
               total is unchanged. The arithmetic is amount - amount/(1+r),
               NOT amount * r — the latter over-charges by r² of the base.
               On Rs.8,400 at 18% that is Rs.1,281.36, not Rs.1,512.00.
    """
    amount = money(amount)

    if not platform_gst_enabled:
        return TaxResult(
            gst_percent=ZERO,
            taxable_amount=amount,
            gst_amount=ZERO,
            total_amount=amount,
            is_tax_invoice=False,
            source=TAX_SOURCE_PLATFORM_DISABLED,
        )

    if tax_mode == TAX_MODE_EXEMPT:
        return TaxResult(
            gst_percent=ZERO,
            taxable_amount=amount,
            gst_amount=ZERO,
            total_amount=amount,
            is_tax_invoice=False,
            source=TAX_SOURCE_HOTEL_EXEMPT,
        )

    slab = resolve_room_gst(nightly_tariff, slabs)
    if slab is None:
        return TaxResult(
            gst_percent=ZERO,
            taxable_amount=amount,
            gst_amount=ZERO,
            total_amount=amount,
            is_tax_invoice=False,
            source=TAX_SOURCE_SLAB,
        )

    rate = slab.gst_percent / Decimal("100")

    if tax_mode == TAX_MODE_INCLUSIVE:
        taxable = money(amount / (Decimal("1") + rate))
        gst_amount = money(amount - taxable)
        total = amount
    else:
        taxable = amount
        gst_amount = money(amount * rate)
        total = money(amount + gst_amount)

    return TaxResult(
        gst_percent=slab.gst_percent,
        taxable_amount=taxable,
        gst_amount=gst_amount,
        total_amount=total,
        is_tax_invoice=True,
        source=TAX_SOURCE_SLAB,
        slab_id=slab.id,
        slab_name=slab.slab_name,
        hsn_code=slab.hsn_code,
    )


# ============================================================
# COMMISSION
# ============================================================


@dataclass(frozen=True)
class CommissionConfigInput:
    commission_type: str
    commission_percent: Decimal
    commission_flat: Decimal
    min_commission: Optional[Decimal] = None
    max_commission: Optional[Decimal] = None
    applies_to: str = "PER_BOOKING"
    source: str = "HOTEL_OVERRIDE"
    config_id: Optional[int] = None


@dataclass(frozen=True)
class CommissionResult:
    commission_amount: Decimal
    partner_payout: Decimal
    commission_type: str
    commission_percent: Decimal
    commission_flat: Decimal
    applies_to: str
    source: str
    was_clamped: bool = False
    config_id: Optional[int] = None


def compute_commission(
    taxable_amount: Decimal,
    config: CommissionConfigInput,
    room_nights: int = 1,
) -> CommissionResult:
    """Platform commission on the pre-tax room amount.

    Commission is charged on the taxable amount, never on the GST-inclusive
    total — taking a cut of the government's tax would overstate platform
    revenue and understate the partner payout.

    HYBRID is percentage AND flat together, which is why this cannot reuse the
    existing commission_rules table (one type, one value).
    """
    taxable_amount = money(taxable_amount)
    nights = max(room_nights, 1)

    if config.commission_type == COMMISSION_TYPE_PERCENTAGE:
        raw = taxable_amount * config.commission_percent / Decimal("100")
    elif config.commission_type == COMMISSION_TYPE_FLAT:
        raw = config.commission_flat
    elif config.commission_type == COMMISSION_TYPE_HYBRID:
        raw = (
            taxable_amount * config.commission_percent / Decimal("100")
        ) + config.commission_flat
    else:
        raw = ZERO

    # A flat component is per room-night when configured that way; the
    # percentage component is already proportional to the amount.
    if config.applies_to == COMMISSION_APPLIES_PER_ROOM_NIGHT:
        if config.commission_type == COMMISSION_TYPE_FLAT:
            raw = config.commission_flat * Decimal(nights)
        elif config.commission_type == COMMISSION_TYPE_HYBRID:
            raw = (
                taxable_amount * config.commission_percent / Decimal("100")
            ) + config.commission_flat * Decimal(nights)

    commission = money(raw)
    was_clamped = False

    if config.min_commission is not None and commission < config.min_commission:
        commission = money(config.min_commission)
        was_clamped = True
    if config.max_commission is not None and commission > config.max_commission:
        commission = money(config.max_commission)
        was_clamped = True

    # Commission can never exceed the amount it is charged on.
    if commission > taxable_amount:
        commission = taxable_amount
        was_clamped = True
    if commission < ZERO:
        commission = ZERO

    return CommissionResult(
        commission_amount=commission,
        partner_payout=money(taxable_amount - commission),
        commission_type=config.commission_type,
        commission_percent=config.commission_percent,
        commission_flat=config.commission_flat,
        applies_to=config.applies_to,
        source=config.source,
        was_clamped=was_clamped,
        config_id=config.config_id,
    )


def commission_snapshot(result: CommissionResult) -> dict:
    """Frozen onto hotel_reservations.commission_config_snapshot at
    confirmation. BRD Rule 25 forbids a later config edit from changing a
    confirmed booking, and current config cannot reconstruct what was in force.
    """
    return {
        "commission_type": result.commission_type,
        "commission_percent": str(result.commission_percent),
        "commission_flat": str(result.commission_flat),
        "applies_to": result.applies_to,
        "source": result.source,
        "config_id": result.config_id,
        "commission_amount": str(result.commission_amount),
        "partner_payout": str(result.partner_payout),
        "was_clamped": result.was_clamped,
    }


def rate_snapshot(quote: StayQuote) -> dict:
    """Frozen onto hotel_reservations.rate_snapshot at confirmation."""
    return {
        "total": str(quote.total),
        "average_nightly_rate": str(quote.average_nightly_rate),
        "nights": [
            {
                "date": n.stay_date.isoformat(),
                "rate": str(n.rate),
                "source": n.source,
                "plan_id": n.plan_id,
                "plan_name": n.plan_name,
            }
            for n in quote.nights
        ],
    }
