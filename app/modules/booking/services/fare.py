# ============================================================
# WAYTERO — CAB FARE CALCULATOR
# File: app/modules/booking/services/fare.py
# Doc Ref: BRD Part 3 §35 — Fare engine formula
#
# Single source of truth for cab fare math. Every place that prices a cab
# trip — admin close-trip, partner close-trip, partner pricing preview,
# admin cab-add, both frontends — calls into this module so the rules
# can never drift between them.
#
# Formula (migration 0049):
#
#     For LOCAL / AIRPORT (package-style):
#         distance_charge = max(0, actual_km - minimum_km) * per_km_rate
#         (first ``minimum_km`` km are included in base_fare)
#
#     For OUTSTATION / ONE_WAY / ROUND_TRIP (billable minimum):
#         billable_km    = max(actual_km, minimum_km)
#         distance_charge = billable_km * per_km_rate
#         (a 65 km trip on a 100 km minimum still bills 100 km)
#
#     Driver allowance by ``driver_allowance_type``:
#         PER_TRIP  → driver_allowance                          (legacy flat)
#         PER_DAY   → driver_allowance * trip_days              (multi-day)
#         PER_KM    → driver_allowance * billable_km            (variable)
#         NONE      → 0
#         (default PER_TRIP if the rule doesn't carry the new key — preserves
#          legacy rows after migration 0049)
#
#     base_fare
#   + distance_charge
#   + (driver_allowance evaluated by type)
#   + night_charge              (only if any part of the trip falls in the
#                                night window)
#
# The rule shape is the dict returned by
# app.modules.admin.services.DefaultPricingService.get_effective_rule.
#
# Pure functions — no DB, no exceptions. Callers handle missing fields by
# treating them as zero (matches what get_effective_rule already does).
# ============================================================

from datetime import datetime, time, timezone, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Mapping, Optional

# Default night window (22:00 – 06:00 local). Mirrors the BRD's convention
# for "night hours" in Indian road transport; can be overridden per tenant
# via the NIGHT_CHARGE_START_HOUR / NIGHT_CHARGE_END_HOUR system_configurations
# rows if those exist.
DEFAULT_NIGHT_START = time(22, 0)
DEFAULT_NIGHT_END = time(6, 0)
DEFAULT_NIGHT_TZ = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata

TWO_PLACES = Decimal("0.01")

# Trip types where ``minimum_km`` is a billable minimum (always charge at
# least that many km). Mirrors migration 0049.
_BILLABLE_MIN_TRIP_TYPES = frozenset({"OUTSTATION", "ONE_WAY", "ROUND_TRIP"})

# Recognised driver_allowance_type values. Anything else falls back to
# PER_TRIP for backcompat with rules that predate migration 0049.
_VALID_DA_TYPES = frozenset({"PER_TRIP", "PER_DAY", "PER_KM", "NONE"})

# Recognised night_charge_type values. Anything else falls back to FIXED.
_NIGHT_CT_TYPES = frozenset({"FIXED", "PERCENTAGE", "PER_KM"})


def _rule_decimal(rule: Mapping[str, Any], key: str) -> Decimal:
    """Read a numeric field from a rule dict, defaulting to 0."""
    val = rule.get(key)
    if val is None or val == "":
        return Decimal("0")
    return Decimal(str(val))


def _billable_km(
    actual_km: Decimal, minimum_km: Decimal, trip_type: Optional[str]
) -> Decimal:
    """Compute the km figure used for the per-km charge.

    Outstation/one-way/round-trip use ``MAX(actual, minimum)`` — the customer
    pays for at least the minimum even if they travelled less. Package-style
    trips (LOCAL/AIRPORT) charge only km above the included minimum.
    """
    if trip_type in _BILLABLE_MIN_TRIP_TYPES:
        return max(actual_km, minimum_km)
    return max(Decimal("0"), actual_km - minimum_km)


def _driver_allowance(
    da_amount: Decimal,
    da_type: str,
    billable_km_: Decimal,
    trip_days: int,
) -> Decimal:
    """Resolve the driver-allowance line for the given rule.

    See module docstring for the dispatch. ``da_type`` is normalised to
    PER_TRIP if the rule carries an unknown value (forward-compatible with
    future enum members).
    """
    if da_type == "NONE":
        return Decimal("0")
    if da_type == "PER_DAY":
        # Single-day round trip → 1 day; 2-day round trip → 2 days. Multi-day
        # billing is the entire reason this enum exists.
        days = max(1, int(trip_days or 1))
        return da_amount * days
    if da_type == "PER_KM":
        return da_amount * billable_km_
    # PER_TRIP (default) — legacy flat behaviour.
    return da_amount


def _waiting_charge(
    free_waiting_minutes: Decimal,
    actual_waiting_minutes: Decimal,
    waiting_rate_per_hour: Decimal,
    granularity: str = "PER_MINUTE",
) -> Decimal:
    """
    Compute waiting charge based on free period and billing granularity.

    Granularity options:
        PER_MINUTE    — bill every actual minute after free period
        PER_15_MINUTES — bill in 15-minute blocks after free period
        PER_30_MINUTES — bill in 30-minute blocks after free period
        PER_HOUR      — bill in 60-minute blocks after free period
    """
    if actual_waiting_minutes <= free_waiting_minutes:
        return Decimal("0")

    chargeable = actual_waiting_minutes - free_waiting_minutes

    if granularity == "PER_MINUTE":
        # Bill per exact minute (rare; usually 15m/30m/hour)
        rate_per_min = waiting_rate_per_hour / Decimal("60")
        return (chargeable * rate_per_min).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if granularity == "PER_15_MINUTES":
        blocks = (chargeable + Decimal("14")) // Decimal("15")  # ceiling divide
        return (blocks * waiting_rate_per_hour / Decimal("4")).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )
    if granularity == "PER_30_MINUTES":
        blocks = (chargeable + Decimal("29")) // Decimal("30")  # ceiling divide
        return (blocks * waiting_rate_per_hour / Decimal("2")).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )
    if granularity == "PER_HOUR":
        blocks = (chargeable + Decimal("59")) // Decimal("60")  # ceiling divide
        return (blocks * waiting_rate_per_hour).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

    # fallback to PER_MINUTE
    rate_per_min = waiting_rate_per_hour / Decimal("60")
    return (chargeable * rate_per_min).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _apply_night_charge(
    night_charge: Decimal,
    night_charge_type: str,
    billable_km: Decimal,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    subtotal: Decimal = Decimal("0"),
) -> Decimal:
    """Apply night charge based on its type.

    Types:
        FIXED      — flat ``night_charge`` when any part of the trip falls in
                     the night window.
        PERCENTAGE — ``night_charge``% of ``subtotal`` (base + distance +
                     allowance + toll + waiting) when the night window is hit.
                     The value is a plain number, e.g. 10 → 10%.
        PER_KM     — ``night_charge`` per billable km when the night window is
                     hit. Stored as an amount, e.g. 5 → ₹5/km.
    """
    in_night = (
        start_dt is not None and end_dt is not None and is_night_trip(start_dt, end_dt)
    )
    if not in_night:
        return Decimal("0")

    if night_charge_type == "PERCENTAGE":
        return (subtotal * night_charge / Decimal("100")).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

    if night_charge_type == "PER_KM":
        return (night_charge * billable_km).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    # FIXED (default)
    return night_charge


def is_night_trip(
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    *,
    night_start: time = DEFAULT_NIGHT_START,
    night_end: time = DEFAULT_NIGHT_END,
    tz: timezone = DEFAULT_NIGHT_TZ,
) -> bool:
    """
    True if any part of `[start_dt, end_dt]` falls inside the night window
    in the given timezone. Night is `[night_start, 24:00) ∪ [00:00, night_end)`
    — a single window that wraps midnight.

    If either datetime is naive it's assumed to already be in ``tz``. Returns
    False if either datetime is missing — the caller is the one that knows
    whether a night charge applies.
    """
    if start_dt is None or end_dt is None:
        return False

    def local(d: datetime) -> datetime:
        if d.tzinfo is None:
            return d.replace(tzinfo=tz)
        return d.astimezone(tz)

    a = local(start_dt)
    b = local(end_dt)
    if b < a:
        a, b = b, a

    # Windowed night check: for each whole day the trip touches, see whether
    # the [a, b) interval overlaps [night_start, midnight) ∪ [midnight, night_end).
    # Step day-by-day so a multi-day trip is handled too.
    cursor_day = a.date()
    end_day = b.date()
    while cursor_day <= end_day:
        if night_start <= night_end:
            # Non-wrapping window (e.g. 09:00–17:00). Kept for completeness.
            window_start = datetime.combine(cursor_day, night_start, tzinfo=tz)
            window_end = datetime.combine(cursor_day, night_end, tzinfo=tz)
            if a < window_end and window_start <= b:
                return True
        else:
            # Wrapping window — split into two halves.
            first_start = datetime.combine(cursor_day, night_start, tzinfo=tz)
            first_end = datetime.combine(
                cursor_day, time(23, 59, 59, 999999), tzinfo=tz
            )
            second_start = datetime.combine(cursor_day, time(0, 0), tzinfo=tz)
            second_end = datetime.combine(cursor_day, night_end, tzinfo=tz)
            if a < first_end and first_start <= b:
                return True
            if a < second_end and second_start <= b:
                return True
        cursor_day = cursor_day + timedelta(days=1)

    return False


def calculate_fare(
    rule: Mapping[str, Any],
    actual_distance_km: float,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    *,
    trip_type: Optional[str] = None,
    trip_days: Optional[int] = None,
) -> Decimal:
    """
    Compute the cab fare using the full rule.

    Behaviour depends on ``trip_type``:

    - LOCAL / AIRPORT (package-style): charge per km beyond ``minimum_km``
      (the first ``minimum_km`` are included in the base fare).
    - OUTSTATION / ONE_WAY / ROUND_TRIP (billable-minimum style): charge
      ``MAX(actual_km, minimum_km) * per_km_rate`` so a 65 km trip on a
      100 km minimum still bills 100 km.

    ``trip_type=None`` (legacy callers) defaults to the package-style
    behaviour — preserves existing call sites until they're updated to pass
    the booking's trip_type.

    Driver allowance is dispatched on ``rule['driver_allowance_type']``
    (PER_TRIP / PER_DAY / PER_KM / NONE). Unknown values fall back to
    PER_TRIP for forward-compat.

    ``actual_distance_km`` may be negative or zero — guarded by max(0, …).

    Toll and waiting charges are read from the rule if present; they are
    added after the base components (before night charge).
    """
    base_fare = _rule_decimal(rule, "base_fare")
    minimum_km = _rule_decimal(rule, "minimum_km")
    per_km_rate = _rule_decimal(rule, "per_km_rate")
    da_amount = _rule_decimal(rule, "driver_allowance")
    da_type = rule.get("driver_allowance_type") or "PER_TRIP"
    if da_type not in _VALID_DA_TYPES:
        da_type = "PER_TRIP"
    night_charge = _rule_decimal(rule, "night_charge")
    night_charge_type = rule.get("night_charge_type") or "FIXED"
    if night_charge_type not in _NIGHT_CT_TYPES:
        night_charge_type = "FIXED"

    distance = max(Decimal("0"), Decimal(str(actual_distance_km or 0)))
    billable = _billable_km(distance, minimum_km, trip_type)
    distance_charge = billable * per_km_rate
    allowance = _driver_allowance(da_amount, da_type, billable, int(trip_days or 1))

    # Toll charge (flat amount from rule)
    toll = _rule_decimal(rule, "toll")

    # Waiting charge: free_waiting_minutes, actual_waiting_minutes, rate_per_hour, granularity
    free_waiting = _rule_decimal(rule, "free_waiting_minutes")
    actual_waiting = _rule_decimal(rule, "actual_waiting_minutes")
    waiting_rate = _rule_decimal(rule, "waiting_rate_per_hour")
    waiting_granularity = rule.get("waiting_granularity") or "PER_15_MINUTES"
    waiting = _waiting_charge(
        free_waiting, actual_waiting, waiting_rate, waiting_granularity
    )

    pre_night_subtotal = base_fare + distance_charge + allowance + toll + waiting
    applied_night = _apply_night_charge(
        night_charge,
        night_charge_type,
        billable,
        start_dt,
        end_dt,
        subtotal=pre_night_subtotal,
    )

    return (pre_night_subtotal + applied_night).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


def calculate_fare_breakdown(
    rule: Mapping[str, Any],
    actual_distance_km: float,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    *,
    trip_type: Optional[str] = None,
    trip_days: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Same calculation as :func:`calculate_fare`, but returns the line items so
    the UI / invoice can show the components. All money values are Decimal
    rounded to 2 places; ``total`` is the same value :func:`calculate_fare`
    returns.

    Keys:
        base_fare               — Decimal (₹2 places)
        minimum_km              — int (rule)
        actual_distance_km      — float (input, clamped to ≥0)
        billable_km             — float (km used for the per-km line; for
                                   OUTSTATION/ONE_WAY/ROUND_TRIP this is
                                   MAX(actual, minimum))
        extra_km                — float (max(0, actual - minimum_km) — the
                                   "package overage" figure, useful for
                                   LOCAL/AIRPORT UI)
        per_km_rate             — Decimal
        distance_charge         — Decimal (₹2 places)
        driver_allowance        — Decimal (₹2 places, evaluated value)
        driver_allowance_type   — str (PER_TRIP / PER_DAY / PER_KM / NONE)
        trip_days               — int (used only when DA type = PER_DAY;
                                   minimum 1, even for single-day bookings)
        night_charge_applied    — Decimal (₹2 places, 0 if not a night trip)
        night_charge_type       — str (FIXED / PERCENTAGE / PER_KM)
        is_night                — bool
        toll                    — Decimal (₹2 places, flat amount on the rule)
        waiting                 — Decimal (₹2 places, evaluated waiting charge)
        total                   — Decimal (₹2 places)
    """
    base_fare = _rule_decimal(rule, "base_fare").quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    minimum_km = _rule_decimal(rule, "minimum_km")
    per_km_rate = _rule_decimal(rule, "per_km_rate")
    da_amount = _rule_decimal(rule, "driver_allowance").quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    da_type = rule.get("driver_allowance_type") or "PER_TRIP"
    if da_type not in _VALID_DA_TYPES:
        da_type = "PER_TRIP"
    night_charge = _rule_decimal(rule, "night_charge").quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    night_charge_type = rule.get("night_charge_type") or "FIXED"
    if night_charge_type not in _NIGHT_CT_TYPES:
        night_charge_type = "FIXED"

    distance = max(Decimal("0"), Decimal(str(actual_distance_km or 0)))
    billable = _billable_km(distance, minimum_km, trip_type)
    extra_km = max(Decimal("0"), distance - minimum_km)
    distance_charge = (billable * per_km_rate).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    driver_allowance_line = _driver_allowance(
        da_amount, da_type, billable, int(trip_days or 1)
    ).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    is_night = is_night_trip(start_dt, end_dt)

    # Toll charge (from rule)
    toll = _rule_decimal(rule, "toll").quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    # Waiting charge (from rule)
    free_waiting = _rule_decimal(rule, "free_waiting_minutes")
    actual_waiting = _rule_decimal(rule, "actual_waiting_minutes")
    waiting_rate = _rule_decimal(rule, "waiting_rate_per_hour")
    waiting_granularity = rule.get("waiting_granularity") or "PER_15_MINUTES"
    waiting = _waiting_charge(
        free_waiting, actual_waiting, waiting_rate, waiting_granularity
    ).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    pre_night_subtotal = (
        base_fare + distance_charge + driver_allowance_line + toll + waiting
    )
    applied_night = _apply_night_charge(
        night_charge,
        night_charge_type,
        billable,
        start_dt,
        end_dt,
        subtotal=pre_night_subtotal,
    )

    total = (pre_night_subtotal + applied_night).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )

    return {
        "base_fare": base_fare,
        "minimum_km": int(minimum_km),
        "actual_distance_km": float(distance),
        "billable_km": float(billable),
        "extra_km": float(extra_km),
        "per_km_rate": per_km_rate,
        "distance_charge": distance_charge,
        "driver_allowance": driver_allowance_line,
        "driver_allowance_type": da_type,
        "trip_days": max(1, int(trip_days or 1)),
        "night_charge_applied": applied_night,
        "night_charge_type": night_charge_type,
        "is_night": is_night,
        "toll": toll,
        "waiting": waiting,
        "total": total,
    }


def trip_days_from(
    pickup_dt: Optional[datetime],
    return_dt: Optional[datetime],
) -> int:
    """Compute the trip-day count for the PER_DAY driver-allowance formula.

    A same-day pickup-and-return is 1 day. A pickup on Aug 13 with return on
    Aug 15 is 3 days (13→14→15). Either input being missing collapses to 1 —
    the safe default for single-day bookings where PER_DAY isn't used.
    """
    if pickup_dt is None or return_dt is None:
        return 1
    delta = (return_dt.date() - pickup_dt.date()).days
    return max(1, delta + 1)
