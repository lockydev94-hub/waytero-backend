# ============================================================
# WAYTERO — HOTEL FULL LIFECYCLE INTEGRATION TEST
# File: tests/integration/test_hotel_full_lifecycle.py
# Doc Ref: Hotel Hardening & UX Upgrade Plan — Step 12 (integration)
#
# DB-backed end-to-end run against the local Postgres: seeds a hotel booking
# with a real partner + customer + wallet, drives the partner-side lifecycle
# handlers directly (check-in → in-house → add-charges → record-advance →
# check-out → generate-invoice → collect-payment → complete), then the shared
# settlement engine, and asserts the partner wallet moves exactly once.
# All seeded rows are removed FK-safely on teardown.
# ============================================================

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.modules.auth.models.user import User
from app.modules.booking.models import BookingService, BookingTimeline, MasterBooking
from app.modules.customer.models import Customer
from app.modules.hotel.models import Hotel, HotelReservation, HotelRoomCategory
from app.modules.master.models import City, Country, State
from app.modules.partner.models import Partner
from app.shared.enums.user_types import UserStatus, UserType

from app.modules.partner.hotel_booking_api import (
    PartnerAddChargesRequest,
    PartnerCheckInRequest,
    PartnerCheckOutRequest,
    PartnerCollectPaymentRequest,
    PartnerRecordAdvanceRequest,
    partner_hotel_add_charges,
    partner_hotel_check_in,
    partner_hotel_check_out,
    partner_hotel_collect_payment,
    partner_hotel_complete,
    partner_hotel_generate_invoice,
    partner_hotel_in_house,
    partner_hotel_record_advance,
)
from app.modules.admin.settlement_api import _settle_hotel_reservation

CHECK_IN_DATE = date(2026, 8, 20)
CHECK_OUT_DATE = date(2026, 8, 22)
# 14:00 IST on the booked check-in date (no date mismatch) and 10:00 IST on the
# booked check-out date (before the 11:00 default checkout time → no overtime).
CHECK_IN_DT = datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc)
CHECK_OUT_DT = datetime(2026, 8, 22, 4, 30, tzinfo=timezone.utc)

STARTING_WALLET = Decimal("100000.00")
NIGHTLY_RATE = Decimal("5000.00")
EXTRA_CHARGE = Decimal("500.00")
ROOM_CHARGE = NIGHTLY_RATE * Decimal("2")  # 10000.00
GRAND_TOTAL = ROOM_CHARGE + EXTRA_CHARGE  # 10500.00 (GST disabled)
COMMISSION = GRAND_TOTAL * Decimal("0.12")  # 1260.00 (system default 12%)
PAYOUT = GRAND_TOTAL - COMMISSION  # 9240.00
NET_DEBIT = COMMISSION  # payout − partner_held − coupon − tds = −1260.00
FINAL_WALLET = STARTING_WALLET - NET_DEBIT  # 98740.00


@pytest.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


async def _cleanup(
    db,
    *,
    wallet_id,
    master_booking_id,
    reservation_id,
    hotel_id,
    room_category_id,
    partner_id,
    customer_id,
    partner_user_id,
    customer_user_id,
    city_id,
    state_id,
    country_id,
):
    await db.execute(
        text("DELETE FROM wallet_ledger WHERE wallet_id = :wid"),
        {"wid": wallet_id},
    )
    await db.execute(text("DELETE FROM wallets WHERE id = :wid"), {"wid": wallet_id})
    await db.execute(
        text("DELETE FROM booking_timelines WHERE master_booking_id = :m"),
        {"m": master_booking_id},
    )
    await db.execute(
        text(
            "DELETE FROM hotel_advance_payments "
            "WHERE master_booking_id = :m OR hotel_reservation_id = :r"
        ),
        {"m": master_booking_id, "r": reservation_id},
    )
    await db.execute(
        text("DELETE FROM hotel_checkins WHERE reservation_id = :r"),
        {"r": reservation_id},
    )
    await db.execute(
        text("DELETE FROM hotel_checkouts WHERE reservation_id = :r"),
        {"r": reservation_id},
    )
    await db.execute(
        text("DELETE FROM hotel_reservations WHERE id = :r"),
        {"r": reservation_id},
    )
    await db.execute(
        text("DELETE FROM booking_services WHERE master_booking_id = :m"),
        {"m": master_booking_id},
    )
    await db.execute(
        text("DELETE FROM master_bookings WHERE id = :m"),
        {"m": master_booking_id},
    )
    await db.execute(
        text("DELETE FROM hotel_room_categories WHERE id = :rc"),
        {"rc": room_category_id},
    )
    await db.execute(text("DELETE FROM hotels WHERE id = :h"), {"h": hotel_id})
    await db.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer_id})
    await db.execute(text("DELETE FROM partners WHERE id = :p"), {"p": partner_id})
    await db.execute(
        text("DELETE FROM users WHERE id IN (:pu, :cu)"),
        {"pu": partner_user_id, "cu": customer_user_id},
    )
    await db.execute(text("DELETE FROM cities WHERE id = :c"), {"c": city_id})
    await db.execute(text("DELETE FROM states WHERE id = :s"), {"s": state_id})
    await db.execute(text("DELETE FROM countries WHERE id = :c"), {"c": country_id})
    await db.commit()


async def test_hotel_full_lifecycle_partner_drives_and_admin_settles_once(db):
    suffix = uuid.uuid4().hex[:10]

    # ── Seed masters, users, partner, customer, hotel, booking, wallet ──
    country = Country(name="Testland", iso_code="TL", phone_code="+91", is_active=True)
    db.add(country)
    await db.flush()
    state = State(
        country_id=country.id, name="TestState", state_code="TS", is_active=True
    )
    db.add(state)
    await db.flush()
    city = City(state_id=state.id, name="TestCity", city_code="TC", is_active=True)
    db.add(city)
    await db.flush()

    partner_user = User(
        first_name="Partner",
        last_name="One",
        mobile_number=f"9{suffix[:9]}",
        user_type=UserType.PARTNER,
        status=UserStatus.ACTIVE,
    )
    db.add(partner_user)
    await db.flush()
    partner = Partner(
        user_id=partner_user.id,
        partner_code=f"TSTP{suffix[:8]}",
        partner_type="INDIVIDUAL",
        owner_name="Partner One",
        mobile=f"9{suffix[:9]}",
        city_id=city.id,
        status="ACTIVE",
    )
    db.add(partner)
    await db.flush()

    customer_user = User(
        first_name="Guest",
        mobile_number=f"7{suffix[:9]}",
        user_type=UserType.CUSTOMER,
        status=UserStatus.ACTIVE,
    )
    db.add(customer_user)
    await db.flush()
    customer = Customer(
        user_id=customer_user.id,
        customer_code=f"TSTC{suffix[:8]}",
        first_name="Guest",
        is_active=True,
    )
    db.add(customer)
    await db.flush()

    hotel = Hotel(
        partner_id=partner.id,
        hotel_code=f"TSTH{suffix[:8]}",
        hotel_name="Test Resort",
        city_id=city.id,
        state_id=state.id,
        tax_mode="EXCLUSIVE",
        is_gst_registered=False,
        status="ACTIVE",
        total_rooms=5,
    )
    db.add(hotel)
    await db.flush()
    category = HotelRoomCategory(
        hotel_id=hotel.id,
        category_name="Standard",
        base_price=NIGHTLY_RATE,
        total_rooms=5,
        is_active=True,
    )
    db.add(category)
    await db.flush()

    master = MasterBooking(
        booking_number=f"TSTB{suffix[:8]}",
        customer_id=customer.id,
        city_id=city.id,
        booking_status="CONFIRMED",
        total_amount=ROOM_CHARGE,
        total_paid_amount=Decimal("0.00"),
    )
    db.add(master)
    await db.flush()
    service = BookingService(
        master_booking_id=master.id,
        service_type="HOTEL",
        service_reference_id=1,
        service_status="CONFIRMED",
        service_amount=ROOM_CHARGE,
    )
    db.add(service)
    await db.flush()

    reservation = HotelReservation(
        master_booking_id=master.id,
        booking_service_id=service.id,
        hotel_id=hotel.id,
        room_category_id=category.id,
        customer_id=customer.id,
        reservation_number=f"TSTHR{suffix[:8]}",
        check_in_date=CHECK_IN_DATE,
        check_out_date=CHECK_OUT_DATE,
        nights=2,
        rooms_count=1,
        room_nights=2,
        adults_count=2,
        children_count=0,
        base_amount=ROOM_CHARGE,
        extra_charges=Decimal("0.00"),
        taxable_amount=ROOM_CHARGE,
        gst_percent=Decimal("0.00"),
        gst_amount=Decimal("0.00"),
        total_amount=ROOM_CHARGE,
        rate_snapshot={"nightly_rate": str(NIGHTLY_RATE)},
        reservation_status="CONFIRMED",
    )
    db.add(reservation)
    await db.flush()

    wallet_id = (
        await db.execute(
            text(
                "INSERT INTO wallets "
                "(partner_id, wallet_type, available_balance, hold_balance, "
                " credit_limit, wallet_status, created_at) "
                "VALUES (:pid, 'HOTEL', :bal, 0, 0, 'ACTIVE', NOW()) "
                "RETURNING id"
            ),
            {"pid": partner.id, "bal": STARTING_WALLET},
        )
    ).scalar_one()
    await db.commit()

    partner_user_id = partner_user.id
    customer_user_id = customer_user.id
    reservation_id = reservation.id
    master_id = master.id
    hotel_id = hotel.id
    category_id = category.id
    partner_id = partner.id
    customer_id = customer.id

    try:
        current_user = {
            "sub": str(partner_user_id),
            "role": "PARTNER",
            "roles": ["PARTNER"],
        }

        # ── Partner drives the stay ──────────────────────────────────────
        await partner_hotel_check_in(
            reservation_id,
            PartnerCheckInRequest(
                id_proof="AADHAAR",
                id_number="999988887777",
                actual_check_in_at=CHECK_IN_DT,
            ),
            current_user,
            db,
        )
        await partner_hotel_in_house(reservation_id, current_user, db)
        await partner_hotel_add_charges(
            reservation_id,
            PartnerAddChargesRequest(
                amount=float(EXTRA_CHARGE), description="Mini bar"
            ),
            current_user,
            db,
        )

        reservation = (
            await db.execute(
                select(HotelReservation).where(HotelReservation.id == reservation_id)
            )
        ).scalar_one()
        assert Decimal(str(reservation.total_amount)) == GRAND_TOTAL
        advance_amount = round(float(reservation.total_amount) * 0.4, 2)
        await partner_hotel_record_advance(
            reservation_id,
            PartnerRecordAdvanceRequest(amount=advance_amount, payment_mode="CASH"),
            current_user,
            db,
        )

        await partner_hotel_check_out(
            reservation_id,
            PartnerCheckOutRequest(
                additional_charges=0.0,
                check_out_notes="Great stay",
                actual_check_out_at=CHECK_OUT_DT,
            ),
            current_user,
            db,
        )
        await partner_hotel_generate_invoice(reservation_id, current_user, db)

        reservation = (
            await db.execute(
                select(HotelReservation).where(HotelReservation.id == reservation_id)
            )
        ).scalar_one()
        assert reservation.invoice_number
        balance_due = round(float(reservation.total_amount) - advance_amount, 2)
        await partner_hotel_collect_payment(
            reservation_id,
            PartnerCollectPaymentRequest(amount=balance_due, payment_mode="CASH"),
            current_user,
            db,
        )
        await partner_hotel_complete(reservation_id, current_user, db)

        reservation = (
            await db.execute(
                select(HotelReservation).where(HotelReservation.id == reservation_id)
            )
        ).scalar_one()
        assert reservation.reservation_status == "COMPLETED"
        assert reservation.payment_collected_status == "PAID"

        # ── Admin settles ────────────────────────────────────────────────
        master = (
            await db.execute(select(MasterBooking).where(MasterBooking.id == master_id))
        ).scalar_one()
        hotel = (
            await db.execute(select(Hotel).where(Hotel.id == hotel_id))
        ).scalar_one()
        result = await _settle_hotel_reservation(db, master, reservation, hotel)
        await db.commit()

        # ── Assertions ───────────────────────────────────────────────────
        assert result["hotel_status"] == "SETTLED"
        assert result["platform_commission"] == pytest.approx(
            float(COMMISSION), abs=0.01
        )
        assert result["partner_payout"] == pytest.approx(float(PAYOUT), abs=0.01)
        assert result["tds_deducted"] == 0.0
        assert result["position"]["net_settlement"] == pytest.approx(
            -float(NET_DEBIT), abs=0.01
        )
        assert result["partner_wallet_balance"] == pytest.approx(
            float(FINAL_WALLET), abs=0.01
        )
        assert result["coupon_disbursement_id"] is None

        reservation = (
            await db.execute(
                select(HotelReservation).where(HotelReservation.id == reservation_id)
            )
        ).scalar_one()
        assert reservation.reservation_status == "SETTLED"
        assert reservation.platform_commission == COMMISSION
        assert reservation.partner_payout == PAYOUT

        master = (
            await db.execute(select(MasterBooking).where(MasterBooking.id == master_id))
        ).scalar_one()
        assert master.booking_status == "CLOSED"

        # Wallet moved exactly once — a single SETTLEMENT_DEBIT ledger row.
        ledger = (
            (
                await db.execute(
                    text(
                        "SELECT reference_type, debit_amount, credit_amount, "
                        "transaction_reference "
                        "FROM wallet_ledger WHERE wallet_id = :wid "
                        "AND transaction_reference = :ref"
                    ),
                    {
                        "wid": wallet_id,
                        "ref": reservation.reservation_number,
                    },
                )
            )
            .mappings()
            .all()
        )
        assert len(ledger) == 1
        assert ledger[0]["reference_type"] == "SETTLEMENT_DEBIT"
        assert float(ledger[0]["debit_amount"]) == pytest.approx(
            float(NET_DEBIT), abs=0.01
        )
        assert float(ledger[0]["credit_amount"]) == 0.0

        balance = (
            await db.execute(
                text("SELECT available_balance FROM wallets WHERE id = :wid"),
                {"wid": wallet_id},
            )
        ).scalar_one()
        assert Decimal(str(balance)) == FINAL_WALLET

        settled_timeline = await db.execute(
            select(BookingTimeline).where(
                BookingTimeline.master_booking_id == master_id,
                BookingTimeline.event_type == "HOTEL_SETTLED_BY_ADMIN",
            )
        )
        assert settled_timeline.scalar_one_or_none() is not None
    finally:
        await _cleanup(
            db,
            wallet_id=wallet_id,
            master_booking_id=master_id,
            reservation_id=reservation_id,
            hotel_id=hotel_id,
            room_category_id=category_id,
            partner_id=partner_id,
            customer_id=customer_id,
            partner_user_id=partner_user_id,
            customer_user_id=customer_user_id,
            city_id=city.id,
            state_id=state.id,
            country_id=country.id,
        )
