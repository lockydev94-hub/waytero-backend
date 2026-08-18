# ============================================================
# WAY TERO — HOTEL VALIDATORS
# File: app/modules/hotel/validators/__init__.py
# Doc Ref: BRD Part 4 §58-70, SRS Part 5 §155-162
#
# Pure functions. They raise ValidationException rather than returning bools so
# the caller cannot forget to check a result.
# ============================================================

import re
from datetime import date
from decimal import Decimal
from typing import Optional

from app.core.exceptions import ValidationException
from app.modules.hotel.constants import MAX_STAR_RATING, MIN_STAR_RATING

# Statutory formats, not preferences.
GST_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")
PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")
SLUG_REGEX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PINCODE_REGEX = re.compile(r"^[1-9][0-9]{5}$")
MOBILE_REGEX = re.compile(r"^[6-9][0-9]{9}$")


def validate_gst_number(gst: Optional[str]) -> Optional[str]:
    """15-char GSTIN. Optional — a hotel below the threshold has none."""
    if not gst:
        return None
    value = gst.strip().upper()
    if not GST_REGEX.match(value):
        raise ValidationException(
            "Invalid GST number. Expected 15-character GSTIN, e.g. 27AAPFU0939F1ZV.",
            details={"field": "gst_number"},
        )
    return value


def validate_pan_number(pan: Optional[str]) -> Optional[str]:
    if not pan:
        return None
    value = pan.strip().upper()
    if not PAN_REGEX.match(value):
        raise ValidationException(
            "Invalid PAN. Expected 10 characters, e.g. AAPFU0939F.",
            details={"field": "pan_number"},
        )
    return value


def validate_pincode(pincode: Optional[str]) -> Optional[str]:
    if not pincode:
        return None
    value = pincode.strip()
    if not PINCODE_REGEX.match(value):
        raise ValidationException(
            "Invalid postal code. Expected a 6-digit Indian PIN code.",
            details={"field": "postal_code"},
        )
    return value


def validate_mobile(
    mobile: Optional[str], field: str = "contact_number"
) -> Optional[str]:
    if not mobile:
        return None
    value = re.sub(r"[^0-9]", "", mobile.strip())
    if value.startswith("91") and len(value) == 12:
        value = value[2:]
    if not MOBILE_REGEX.match(value):
        raise ValidationException(
            "Invalid mobile number. Expected 10 digits starting with 6-9.",
            details={"field": field},
        )
    return value


def validate_star_rating(rating: Optional[int]) -> Optional[int]:
    if rating is None:
        return None
    if rating < MIN_STAR_RATING or rating > MAX_STAR_RATING:
        raise ValidationException(
            f"Star rating must be between {MIN_STAR_RATING} and {MAX_STAR_RATING}.",
            details={"field": "star_rating"},
        )
    return rating


def validate_coordinates(
    latitude: Optional[Decimal], longitude: Optional[Decimal]
) -> None:
    """Both or neither — a lone coordinate cannot place a pin and silently
    produces a hotel that never appears in a map search."""
    if latitude is None and longitude is None:
        return
    if latitude is None or longitude is None:
        raise ValidationException(
            "Latitude and longitude must be provided together.",
            details={"field": "latitude,longitude"},
        )
    if not (Decimal("-90") <= latitude <= Decimal("90")):
        raise ValidationException(
            "Latitude must be between -90 and 90.", details={"field": "latitude"}
        )
    if not (Decimal("-180") <= longitude <= Decimal("180")):
        raise ValidationException(
            "Longitude must be between -180 and 180.", details={"field": "longitude"}
        )


def slugify(value: str) -> str:
    """Lowercase, hyphen-separated, ASCII-only. Used for the customer-site URL."""
    slug = value.strip().lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug.strip("-")


def validate_slug(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    value = slug.strip().lower()
    if not SLUG_REGEX.match(value):
        raise ValidationException(
            "Invalid slug. Use lowercase letters, numbers and single hyphens.",
            details={"field": "slug"},
        )
    return value


def validate_date_range(
    date_from: date, date_to: date, field: str = "date_range"
) -> None:
    if date_to < date_from:
        raise ValidationException(
            "End date cannot be earlier than start date.", details={"field": field}
        )


def validate_occupancy(
    base_occupancy: int,
    max_adults: int,
    max_children: int,
    max_occupancy: int,
) -> None:
    """Occupancy numbers that contradict each other produce rooms that can be
    booked for more guests than they hold."""
    if base_occupancy < 1:
        raise ValidationException(
            "Base occupancy must be at least 1.", details={"field": "base_occupancy"}
        )
    if max_occupancy < base_occupancy:
        raise ValidationException(
            "Maximum occupancy cannot be less than base occupancy.",
            details={"field": "max_occupancy"},
        )
    if max_adults < 1:
        raise ValidationException(
            "A room must allow at least one adult.", details={"field": "max_adults"}
        )
    if max_children < 0:
        raise ValidationException(
            "Maximum children cannot be negative.", details={"field": "max_children"}
        )
    if max_adults + max_children < max_occupancy:
        raise ValidationException(
            "Maximum occupancy exceeds the sum of maximum adults and children.",
            details={"field": "max_occupancy"},
        )


def validate_price_structure(
    base_price: Decimal,
    published_price: Optional[Decimal],
    min_sellable_price: Optional[Decimal],
) -> None:
    """Guards the floor price. Selling below min_sellable_price after a discount
    is how a property ends up losing money on a booking."""
    if base_price < 0:
        raise ValidationException(
            "Base price cannot be negative.", details={"field": "base_price"}
        )
    if published_price is not None and published_price < base_price:
        raise ValidationException(
            "Published price cannot be lower than the base price.",
            details={"field": "published_price"},
        )
    if min_sellable_price is not None and min_sellable_price > base_price:
        raise ValidationException(
            "Minimum sellable price cannot exceed the base price.",
            details={"field": "min_sellable_price"},
        )


def validate_day_of_week_mask(mask: Optional[str]) -> Optional[str]:
    """7 chars of 0/1, Monday first. None means every day."""
    if not mask:
        return None
    value = mask.strip()
    if len(value) != 7 or any(ch not in "01" for ch in value):
        raise ValidationException(
            "Day-of-week mask must be 7 characters of 0 or 1, starting Monday.",
            details={"field": "day_of_week_mask"},
        )
    if "1" not in value:
        raise ValidationException(
            "Day-of-week mask must include at least one active day.",
            details={"field": "day_of_week_mask"},
        )
    return value


def validate_commission_values(
    commission_type: str,
    commission_percent: Decimal,
    commission_flat: Decimal,
    min_commission: Optional[Decimal],
    max_commission: Optional[Decimal],
) -> None:
    if commission_percent < 0 or commission_percent > 100:
        raise ValidationException(
            "Commission percentage must be between 0 and 100.",
            details={"field": "commission_percent"},
        )
    if commission_flat < 0:
        raise ValidationException(
            "Flat commission cannot be negative.",
            details={"field": "commission_flat"},
        )
    if commission_type == "PERCENTAGE" and commission_percent <= 0:
        raise ValidationException(
            "A percentage commission requires a percentage greater than 0.",
            details={"field": "commission_percent"},
        )
    if commission_type == "FLAT" and commission_flat <= 0:
        raise ValidationException(
            "A flat commission requires an amount greater than 0.",
            details={"field": "commission_flat"},
        )
    if commission_type == "HYBRID" and commission_percent <= 0 and commission_flat <= 0:
        raise ValidationException(
            "A hybrid commission requires a percentage or a flat amount.",
            details={"field": "commission_type"},
        )
    if (
        min_commission is not None
        and max_commission is not None
        and min_commission > max_commission
    ):
        raise ValidationException(
            "Minimum commission cannot exceed maximum commission.",
            details={"field": "min_commission"},
        )


def validate_refund_ladder(
    tier_1: Optional[Decimal],
    tier_2: Optional[Decimal],
    tier_3: Optional[Decimal],
    same_day: Optional[Decimal],
) -> None:
    """Refund percentages must not increase as the stay approaches — an
    inverted ladder rewards late cancellation."""
    for label, value in (
        ("refund_percent_tier_1", tier_1),
        ("refund_percent_tier_2", tier_2),
        ("refund_percent_tier_3", tier_3),
        ("refund_percent_same_day", same_day),
    ):
        if value is not None and (value < 0 or value > 100):
            raise ValidationException(
                "Refund percentages must be between 0 and 100.",
                details={"field": label},
            )

    ladder = [v for v in (tier_1, tier_2, tier_3, same_day) if v is not None]
    for earlier, later in zip(ladder, ladder[1:]):
        if later > earlier:
            raise ValidationException(
                "Refund percentages must not increase as the check-in date approaches.",
                details={"field": "refund_ladder"},
            )
