# ============================================================
# WAY TERO — ENUMS PACKAGE
# File: app/shared/enums/__init__.py
# Doc Ref: Backend Architecture Part 2, Section 14 (Shared enums)
#
# Re-export every shared enum from its sibling module so callers can
# `from app.shared.enums import ServiceType, BookingStatus, …` instead of
# having to know which file each enum lives in. Direct imports
# (`from app.shared.enums.booking_status import ServiceType`) keep working.
# ============================================================

from app.shared.enums.booking_status import (
    BookingStatus,
    PaymentStatus,
    ServiceType,
)
from app.shared.enums.user_types import (
    UserType,
    UserStatus,
    VerificationStatus,
)

__all__ = [
    "BookingStatus",
    "PaymentStatus",
    "ServiceType",
    "UserType",
    "UserStatus",
    "VerificationStatus",
]
