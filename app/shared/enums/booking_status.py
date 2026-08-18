# WAY TERO — BOOKING STATUS ENUMS
# Doc Ref: BRD Part 3 — Cab Booking Management
from enum import Enum


class BookingStatus(str, Enum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    ASSIGNED = "ASSIGNED"
    # Awaiting partner sign-off after admin assignment. Partner has a
    # deadline (cab_bookings.acceptance_deadline) to accept or reject;
    # a Celery sweeper reverts to PENDING_ASSIGNMENT on timeout.
    PENDING_PARTNER_ACCEPTANCE = "PENDING_PARTNER_ACCEPTANCE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    # Mid-trip breakdown (cab-specific — not used on master bookings):
    # BREAKDOWN_REPORTED — driver/partner has raised a vehicle issue; trip
    #   is paused and admin cab-ops dashboard surfaces an alert.
    # AWAITING_SWAP — admin is actively finding a replacement vehicle
    #   (same partner or new partner). The cab is on hold.
    BREAKDOWN_REPORTED = "BREAKDOWN_REPORTED"
    AWAITING_SWAP = "AWAITING_SWAP"


# Cab-booking-only breakdown status values. Master bookings never use these;
# guard helpers in booking.services.breakdown ensure the boundary.
CAB_BREAKDOWN_STATUSES = frozenset(
    {BookingStatus.BREAKDOWN_REPORTED.value, BookingStatus.AWAITING_SWAP.value}
)


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIAL = "PARTIAL"


class ServiceType(str, Enum):
    CAB_LOCAL = "CAB_LOCAL"
    CAB_AIRPORT = "CAB_AIRPORT"
    CAB_OUTSTATION_ONE_WAY = "CAB_OUTSTATION_ONE_WAY"
    CAB_OUTSTATION_ROUND = "CAB_OUTSTATION_ROUND"
    CAB_RENTAL = "CAB_RENTAL"
    HOTEL = "HOTEL"
    TOUR = "TOUR"
