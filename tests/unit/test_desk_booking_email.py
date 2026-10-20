# ============================================================
# WAYTERO — PHONE-DESK CAB CONFIRMATION EMAIL TESTS
# File: tests/unit/test_desk_booking_email.py
# Doc Ref: Round 15 plan C2 (the desk emails the booking it created).
#
# The phone desk quotes a fare out loud and the customer agrees to it on the
# call. The email is what the customer keeps, so the claim under test is narrow:
# the figures in that email are the figures on the booking, they add up, and
# nothing secret rides along with them.
#
# The context builder is tested directly rather than through `POST /cab-booking`
# because the alternative is filing a real booking into the database to read one
# dict. The money is the part worth pinning, and it is pure arithmetic — this
# runs the real fare engine, not a stand-in for it.
# ============================================================

from datetime import datetime
from decimal import Decimal

from app.modules.admin.customer_care_api import _cab_confirmation_context
from app.modules.booking.services.distance import TripDistance
from app.modules.booking.services.fare import calculate_fare_breakdown
from app.modules.booking.services.trip_pricing import TripQuote

# An afternoon pickup: no night charge, so a zero line is available to test.
PICKUP = datetime(2026, 11, 4, 14, 30)


def _rule(**overrides):
    rule = dict(
        base_fare=Decimal("500"),
        minimum_km=Decimal("30"),
        per_km_rate=Decimal("20"),
        driver_allowance=Decimal("0"),
        driver_allowance_type="PER_TRIP",
        night_charge=Decimal("0"),
        night_charge_type="FIXED",
        toll=Decimal("0"),
        free_waiting_minutes=Decimal("0"),
        actual_waiting_minutes=Decimal("0"),
        waiting_rate_per_hour=Decimal("0"),
        waiting_granularity="PER_15_MINUTES",
    )
    rule.update(overrides)
    return rule


def _quote(*, km: str, rule=None) -> TripQuote:
    rule = rule or _rule()
    breakdown = calculate_fare_breakdown(
        rule=rule,
        actual_distance_km=float(km),
        start_dt=PICKUP,
        end_dt=PICKUP,
        trip_type="ONE_WAY",
    )
    return TripQuote(
        distance=TripDistance(km=Decimal(km), source="GOOGLE", pricing_city_id=5),
        rule=rule,
        breakdown=breakdown,
        total=Decimal(str(breakdown["total"])),
    )


def _context(quote: TripQuote, **overrides) -> dict:
    args = dict(
        booking_number="WT-CAB-2026-000123",
        trip_type="ONE_WAY",
        pickup_location="Bhubaneswar Railway Station",
        drop_location="Puri",
        pickup_datetime=PICKUP,
        coupon_code=None,
        coupon_discount=Decimal("0.00"),
    )
    args.update(overrides)
    return _cab_confirmation_context(quote, **args)


def _money_lines(details) -> list:
    """Only the rupee lines — the route and the time carry no ₹ sign."""
    return [
        (label, value)
        for label, value in details
        if value.startswith("₹") or value.startswith("−₹")
    ]


def _rupees(value: str) -> Decimal:
    return Decimal(value.replace("₹", "").replace("−", "").strip())


# ── the arithmetic the customer is told ────────────────────────────────────


def test_the_lines_shown_add_up_to_the_fare_shown() -> None:
    quote = _quote(km="47")
    ctx = _context(quote)

    assert sum(_rupees(v) for _l, v in _money_lines(ctx["details"])) == ctx["amount"]
    assert ctx["amount"] == quote.total


def test_a_charge_that_did_not_happen_is_not_printed_as_zero() -> None:
    """An afternoon trip with no toll and no waiting has no ₹0.00 to show.

    A zero reads as a charge that happened, and a customer who is not paying for
    a night ride should not be told about one in their confirmation mail.
    """
    ctx = _context(_quote(km="47"))
    labels = [label for label, _v in _money_lines(ctx["details"])]

    assert "Night charge" not in labels
    assert "Toll" not in labels
    assert "Waiting" not in labels
    assert "Driver allowance" not in labels


def test_the_coupon_appears_once_and_the_total_is_what_they_pay() -> None:
    quote = _quote(km="47")
    ctx = _context(quote, coupon_code="DIWALI150", coupon_discount=Decimal("150.00"))
    lines = _money_lines(ctx["details"])

    coupons = [v for label, v in lines if label.startswith("Coupon")]
    assert len(coupons) == 1
    assert coupons[0].startswith("−₹")
    assert ctx["amount"] == quote.total - Decimal("150.00")
    charged = sum(_rupees(v) for label, v in lines if not label.startswith("Coupon"))
    assert charged - _rupees(coupons[0]) == ctx["amount"]


def test_the_distance_billed_and_the_distance_driven_are_both_named() -> None:
    """A 23 km ONE_WAY against a 30 km minimum: the fare is on 30 km.

    Only one figure can sit next to the distance charge, and the driven one there
    is the arithmetic that does not add up. The journey keeps its own line, so
    the customer is not told they rode 30 km.
    """
    ctx = _context(_quote(km="23"))
    details = dict(ctx["details"])

    assert details["Distance"] == "23 km"
    charge_label = next(
        label
        for label, _v in _money_lines(ctx["details"])
        if label.startswith("Distance charge")
    )
    assert charge_label == "Distance charge (30 km)"


def test_a_toll_in_force_is_a_line_the_customer_sees() -> None:
    ctx = _context(_quote(km="47", rule=_rule(toll=Decimal("250"))))

    assert dict(ctx["details"])["Toll"] == "₹250.00"


# ── what must not ride along ───────────────────────────────────────────────


def test_the_email_carries_the_booking_route_and_fare_and_no_other_field() -> None:
    ctx = _context(_quote(km="47"))

    assert ctx["service"] == "Cab"
    assert ctx["booking_number"] == "WT-CAB-2026-000123"
    # Estimated, not "Fare": the trip is billed on the kilometres actually driven.
    assert ctx["amount_label"] == "Estimated fare"
    assert set(ctx) <= {
        "service",
        "booking_number",
        "message",
        "details",
        "amount",
        "amount_label",
        "amount_note",
    }


def test_no_credential_shape_rides_in_the_email() -> None:
    """A booking confirmation is the easiest place to leak a secret by accident.

    Checked against the rendered content rather than the source, because the
    content is what leaves the building.
    """
    rendered = str(_context(_quote(km="47"))).lower()

    for forbidden in ("mpin", "token", "password", "secret", "otp"):
        assert forbidden not in rendered
