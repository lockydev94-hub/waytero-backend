"""Razorpay order creation, signature verification and booking posting.

Why the gateway calls live here rather than in the booking module: booking owns
what a booking costs and how a receipt is recorded, while this module owns the
external money movement. The final write goes through the existing booking /
hotel / tour services so partner and admin screens see the same receipt they
would have seen for a cash collection.

The admin Settings page already stores Razorpay credentials in
``api_integrations`` (service_type ``RAZORPAY``, configuration keys ``key_id``,
``key_secret``, ``webhook_secret`` and optional ``advance_percent``). This
module is the only reader of the secret for customer payments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RAZORPAY_API = "https://api.razorpay.com/v1"
DEFAULT_ADVANCE_PERCENT = Decimal("30")
_RAZORPAY_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class PaymentGatewayError(Exception):
    """The gateway refused or could not be reached."""


# ── configuration ────────────────────────────────────────────────────────────


async def get_razorpay_config(
    db: AsyncSession, *, active_only: bool = True
) -> Optional[Dict[str, Any]]:
    """Return the Razorpay configuration, or None when unusable.

    A row with ``is_active = FALSE`` is a deliberate off switch for *new*
    checkouts. Missing keys also mean disabled: never show a checkout button the
    server cannot honour. Verification and webhooks pass ``active_only=False``
    so a payment captured just before an admin disables the row is still posted
    instead of leaving the customer's money unrecorded.
    """
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, configuration, is_active FROM api_integrations "
                    "WHERE service_type = 'RAZORPAY' "
                    "ORDER BY is_active DESC, id DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        return None
    if active_only and not row["is_active"]:
        return None

    configuration = row["configuration"] or {}
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except (TypeError, ValueError):
            configuration = {}
    key_id = str(configuration.get("key_id") or "").strip()
    key_secret = str(configuration.get("key_secret") or "").strip()
    if not key_id or not key_secret:
        return None

    raw_percent = configuration.get("advance_percent", DEFAULT_ADVANCE_PERCENT)
    try:
        percent = Decimal(str(raw_percent))
    except Exception:
        percent = DEFAULT_ADVANCE_PERCENT
    percent = max(Decimal("1"), min(Decimal("100"), percent))

    return {
        "key_id": key_id,
        "key_secret": key_secret,
        "webhook_secret": str(configuration.get("webhook_secret") or "").strip(),
        "advance_percent": percent,
        "currency": "INR",
    }


async def public_payment_config(db: AsyncSession) -> Dict[str, Any]:
    config = await get_razorpay_config(db)
    if not config:
        return {
            "enabled": False,
            "provider": "RAZORPAY",
            "currency": "INR",
            "advance_percent": 0,
        }
    return {
        "enabled": True,
        "provider": "RAZORPAY",
        "key_id": config["key_id"],
        "currency": config["currency"],
        "advance_percent": float(config["advance_percent"]),
    }


# ── helpers ──────────────────────────────────────────────────────────────────


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _booking_row(booking: str) -> str:
    return f"WT-{booking[:37]}" if booking else "WT-PAYMENT"


async def _booking_for_customer(
    db: AsyncSession, booking_number: str, customer_id: int
) -> Dict[str, Any]:
    row = (
        (
            await db.execute(
                text(
                    """
                    SELECT mb.id, mb.booking_number, mb.customer_id,
                           mb.total_amount, mb.total_paid_amount,
                           mb.payment_status, mb.booking_status,
                           bs.service_type, bs.service_reference_id
                      FROM master_bookings mb
                      LEFT JOIN booking_services bs
                        ON bs.master_booking_id = mb.id
                     WHERE mb.booking_number = :number
                       AND mb.customer_id = :customer
                     ORDER BY bs.id
                     LIMIT 1
                    """
                ),
                {"number": booking_number, "customer": customer_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(404, "Booking not found for this account.")
    return dict(row)


async def _customer_id_for_user(db: AsyncSession, user_id: str) -> int:
    customer_id = (
        await db.execute(
            text("SELECT id FROM customers WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
    ).scalar_one_or_none()
    if customer_id is None:
        raise HTTPException(404, "Customer profile not found.")
    return int(customer_id)


async def _razorpay_request(
    method: str,
    path: str,
    *,
    key_id: str,
    key_secret: str,
    payload: Optional[dict] = None,
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=_RAZORPAY_TIMEOUT) as client:
            response = await client.request(
                method,
                f"{RAZORPAY_API}{path}",
                auth=(key_id, key_secret),
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise PaymentGatewayError(
            "The payment gateway could not be reached. Try again."
        ) from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        message = (
            body.get("error", {}).get("description") if isinstance(body, dict) else None
        )
        raise PaymentGatewayError(message or "The payment gateway refused the request.")
    return body if isinstance(body, dict) else {}


def _verify_signature(
    order_id: str, payment_id: str, signature: str, secret: str
) -> bool:
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_webhook(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _order_row(db: AsyncSession, order_id: str) -> Optional[Dict[str, Any]]:
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, order_reference, master_booking_id, customer_id, "
                    "       booking_number, amount, currency, payment_type, status, "
                    "       gateway_payment_id, receipt_number, failure_reason "
                    "FROM payment_orders WHERE order_reference = :order"
                ),
                {"order": order_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


# ── order creation ───────────────────────────────────────────────────────────


async def create_order(
    db: AsyncSession,
    *,
    current_user: dict,
    booking_number: str,
    payment_type: str,
) -> Dict[str, Any]:
    config = await get_razorpay_config(db)
    if not config:
        raise HTTPException(409, "Online payment is not enabled right now.")

    customer_id = await _customer_id_for_user(db, current_user["sub"])
    booking = await _booking_for_customer(db, booking_number, customer_id)

    if not booking.get("service_type"):
        raise HTTPException(409, "This booking has no payable service.")

    total = _money(booking["total_amount"])
    paid = _money(booking["total_paid_amount"])
    balance = max(Decimal("0.00"), total - paid)
    if balance <= 0:
        raise HTTPException(409, "This booking is already paid in full.")

    # The app is a single-service checkout. Mixed masters are a future shape;
    # without a single service we cannot mirror the money into the right receipt
    # table and would leave partner screens disagreeing with the customer.
    service_count = (
        await db.execute(
            text("SELECT COUNT(*) FROM booking_services WHERE master_booking_id = :m"),
            {"m": booking["id"]},
        )
    ).scalar() or 0
    if int(service_count) != 1:
        raise HTTPException(
            409, "Online payment is available for single-service bookings only."
        )

    if (payment_type or "FULL").upper() == "ADVANCE":
        amount = _money(total * config["advance_percent"] / Decimal("100"))
        amount = min(amount, balance)
        if paid > 0:
            raise HTTPException(
                409,
                "An advance is already on this booking. Pay the remaining balance instead.",
            )
    else:
        amount = balance
        payment_type = "FULL"

    amount = _money(amount)
    if amount <= 0:
        raise HTTPException(409, "There is nothing to pay on this booking.")

    amount_paise = int((amount * 100).to_integral_value())
    receipt = _booking_row(booking["booking_number"])
    order = await _razorpay_request(
        "POST",
        "/orders",
        key_id=config["key_id"],
        key_secret=config["key_secret"],
        payload={
            "amount": amount_paise,
            "currency": config["currency"],
            "receipt": receipt,
            "notes": {
                "booking_number": booking["booking_number"],
                "service_type": booking["service_type"],
                "payment_type": payment_type,
            },
        },
    )
    order_id = str(order.get("id") or "").strip()
    if not order_id:
        raise PaymentGatewayError("The payment gateway did not return an order id.")

    await db.execute(
        text(
            """
            INSERT INTO payment_orders
                (order_reference, master_booking_id, customer_id, booking_number,
                 amount, currency, payment_type, status, created_at, updated_at)
            VALUES
                (:order, :master, :customer, :booking, :amount, :currency,
                 :payment_type, 'CREATED', NOW(), NOW())
            """
        ),
        {
            "order": order_id,
            "master": booking["id"],
            "customer": customer_id,
            "booking": booking["booking_number"],
            "amount": amount,
            "currency": config["currency"],
            "payment_type": payment_type,
        },
    )

    return {
        "booking_number": booking["booking_number"],
        "payment_type": payment_type,
        "amount": float(amount),
        "amount_paise": amount_paise,
        "currency": config["currency"],
        "key_id": config["key_id"],
        "order_id": order_id,
    }


# ── posting a captured payment ───────────────────────────────────────────────


async def _mirror_cab_payment(
    db: AsyncSession,
    *,
    master_id: int,
    service_id: int,
    amount: Decimal,
    payment_id: str,
    user_id: str,
) -> Dict[str, Any]:
    from app.modules.booking.services import create_advance

    return await create_advance(
        db,
        cab_booking_id=int(service_id),
        amount=float(amount),
        payment_mode="ONLINE",
        received_by="ADMIN",
        user_id=user_id,
        source_role="CUSTOMER",
        reference_note=f"Razorpay {payment_id}",
    )


async def _mirror_hotel_payment(
    db: AsyncSession,
    *,
    master_id: int,
    service_id: int,
    amount: Decimal,
    payment_id: str,
    user_id: str,
) -> Dict[str, Any]:
    from app.modules.booking.services import rollup_hotel_totals_into_master
    from app.modules.hotel.models import HotelAdvancePayment, HotelReservation
    from app.modules.hotel.services import billing

    reservation = (
        (
            await db.execute(
                text(
                    "SELECT id, reservation_status, total_amount, payment_collected_status "
                    "FROM hotel_reservations WHERE id = :rid AND master_booking_id = :m"
                ),
                {"rid": service_id, "m": master_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not reservation:
        raise HTTPException(404, "Hotel reservation not found.")
    if reservation["reservation_status"] in ("CANCELLED", "REJECTED", "NO_SHOW"):
        raise HTTPException(409, "Payment cannot be taken on a cancelled stay.")

    receipt = await billing.next_receipt_number(db)
    db.add(
        HotelAdvancePayment(
            hotel_reservation_id=int(service_id),
            master_booking_id=master_id,
            receipt_number=receipt,
            amount=amount,
            payment_mode="ONLINE",
            received_by="ADMIN",
            reference_number=payment_id,
            notes="Razorpay customer payment",
            status="ACTIVE",
        )
    )
    await db.flush()

    total = _money(reservation["total_amount"])
    paid_row = (
        await db.execute(
            text(
                "SELECT COALESCE(SUM(amount - COALESCE(refunded_amount, 0)), 0) "
                "FROM hotel_advance_payments "
                "WHERE hotel_reservation_id = :rid AND status = 'ACTIVE'"
            ),
            {"rid": service_id},
        )
    ).scalar() or 0
    paid = _money(paid_row)
    status = "PAID" if paid >= total else "PARTIAL"
    obj = await db.get(HotelReservation, int(service_id))
    if obj is not None:
        obj.payment_collected_status = status
        obj.payment_collected_by = "ADMIN"
        obj.payment_mode = "ONLINE"

    await rollup_hotel_totals_into_master(db, master_id)
    # The rollup updates the ORM master object; flush so the payment response's
    # follow-up read sees the same paid amount that will commit at request end.
    await db.flush()
    return {
        "receipt_number": receipt,
        "amount": float(amount),
        "payment_status": status,
        "balance_due": float(max(Decimal("0.00"), total - paid)),
    }


async def _mirror_tour_payment(
    db: AsyncSession,
    *,
    master_id: int,
    service_id: int,
    amount: Decimal,
    payment_id: str,
    user_id: str,
) -> Dict[str, Any]:
    from app.modules.tour.services import collect_advance

    return await collect_advance(
        db,
        tour_booking_id=int(service_id),
        amount=amount,
        payment_mode="ONLINE",
        received_by="ADMIN",
        user_id=user_id,
        source_role="CUSTOMER",
        reference_number=payment_id,
        notes="Razorpay customer payment",
    )


async def finalize_order(
    db: AsyncSession,
    *,
    order_id: str,
    gateway_payment_id: str,
    signature_payload: Optional[dict] = None,
    customer_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Mark a gateway order paid and post the money to the booking.

    Idempotent: Razorpay can deliver the checkout callback and the webhook for
    the same payment. The second call returns the first receipt rather than
    taking the money twice.
    """
    order = await _order_row(db, order_id)
    if not order:
        raise HTTPException(404, "Payment order not found.")
    if customer_id is not None and int(order["customer_id"]) != int(customer_id):
        raise HTTPException(404, "Payment order not found.")

    if order["status"] == "PAID":
        return {
            "booking_number": order["booking_number"],
            "payment_type": order["payment_type"],
            "amount": float(order["amount"]),
            "currency": order["currency"],
            "payment_status": "SUCCESS",
            "receipt_number": order["receipt_number"],
            "gateway_payment_id": order["gateway_payment_id"] or gateway_payment_id,
        }

    master_id = int(order["master_booking_id"])
    booking = (
        (
            await db.execute(
                text(
                    "SELECT bs.service_type, bs.service_reference_id "
                    "FROM booking_services bs WHERE bs.master_booking_id = :m "
                    "ORDER BY bs.id LIMIT 1"
                ),
                {"m": master_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not booking:
        raise HTTPException(409, "This booking has no service to apply the payment to.")

    amount = _money(order["amount"])
    # `payments.collected_by_user_id` is a users.id UUID, not the customer row
    # id. Resolve it so a gateway payment is attributed like any other customer
    # receipt while keeping the order table keyed on the customer. Webhooks have
    # no session, so this lookup is the only common source.
    user_id = (
        await db.execute(
            text("SELECT user_id FROM customers WHERE id = :cid"),
            {"cid": order["customer_id"]},
        )
    ).scalar_one_or_none()
    user_id = str(user_id) if user_id else ""
    service_type = str(booking["service_type"] or "").upper()
    service_id = int(booking["service_reference_id"] or 0)

    if service_type == "CAB":
        receipt = await _mirror_cab_payment(
            db,
            master_id=master_id,
            service_id=service_id,
            amount=amount,
            payment_id=gateway_payment_id,
            user_id=user_id,
        )
        receipt_number = receipt.get("receipt_number")
    elif service_type == "HOTEL":
        receipt = await _mirror_hotel_payment(
            db,
            master_id=master_id,
            service_id=service_id,
            amount=amount,
            payment_id=gateway_payment_id,
            user_id=user_id,
        )
        receipt_number = receipt.get("receipt_number")
    elif service_type == "TOUR":
        receipt = await _mirror_tour_payment(
            db,
            master_id=master_id,
            service_id=service_id,
            amount=amount,
            payment_id=gateway_payment_id,
            user_id=user_id,
        )
        receipt_number = receipt.get("receipt_number")
    else:
        raise HTTPException(409, "This service cannot take online payment yet.")

    master = (
        (
            await db.execute(
                text(
                    "SELECT payment_status, total_paid_amount, total_amount "
                    "FROM master_bookings WHERE id = :m"
                ),
                {"m": master_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    paid = _money(master["total_paid_amount"] if master else 0)
    total = _money(master["total_amount"] if master else 0)
    balance = max(Decimal("0.00"), total - paid)

    await db.execute(
        text(
            "UPDATE payment_orders SET status = 'PAID', "
            "gateway_payment_id = :payment, receipt_number = :receipt, "
            "updated_at = NOW() WHERE order_reference = :order"
        ),
        {
            "payment": gateway_payment_id,
            "receipt": receipt_number,
            "order": order_id,
        },
    )
    if signature_payload is not None:
        # Keep the gateway's own identifiers as evidence on the order row. The
        # legacy `payment_transactions` table requires a `payments.id`, which a
        # service-specific receipt does not always create; the order is the
        # durable record for this gateway attempt.
        await db.execute(
            text(
                "UPDATE payment_orders SET gateway_response = CAST(:response AS jsonb), "
                "updated_at = NOW() WHERE order_reference = :order"
            ),
            {
                "response": json.dumps(signature_payload),
                "order": order_id,
            },
        )

    return {
        "booking_number": order["booking_number"],
        "payment_type": order["payment_type"],
        "amount": float(amount),
        "currency": order["currency"],
        "payment_status": "SUCCESS",
        "booking_payment_status": master["payment_status"] if master else None,
        "balance_due": float(balance),
        "receipt_number": receipt_number,
        "gateway_payment_id": gateway_payment_id,
    }


async def verify_payment(
    db: AsyncSession,
    *,
    current_user: dict,
    order_id: str,
    payment_id: str,
    signature: str,
) -> Dict[str, Any]:
    config = await get_razorpay_config(db, active_only=False)
    if not config:
        raise HTTPException(409, "Online payment is not configured right now.")
    if not _verify_signature(order_id, payment_id, signature, config["key_secret"]):
        raise HTTPException(400, "Payment signature verification failed.")

    customer_id = await _customer_id_for_user(db, current_user["sub"])
    return await finalize_order(
        db,
        order_id=order_id,
        gateway_payment_id=payment_id,
        signature_payload={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "source": "checkout",
        },
        customer_id=customer_id,
    )


async def handle_webhook(
    db: AsyncSession,
    *,
    raw_body: bytes,
    signature: str,
    payload: dict,
) -> Dict[str, Any]:
    config = await get_razorpay_config(db, active_only=False)
    if not config or not config["webhook_secret"]:
        raise HTTPException(503, "Payment webhook is not configured.")
    if not signature or not _verify_webhook(
        raw_body, signature, config["webhook_secret"]
    ):
        raise HTTPException(400, "Invalid webhook signature.")

    event = str(payload.get("event") or "")
    entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    order_id = str(entity.get("order_id") or "").strip()
    payment_id = str(entity.get("id") or "").strip()

    if event == "payment.failed" and order_id:
        await db.execute(
            text(
                "UPDATE payment_orders SET status = 'FAILED', "
                "failure_reason = :reason, updated_at = NOW() "
                "WHERE order_reference = :order AND status = 'CREATED'"
            ),
            {
                "order": order_id,
                "reason": str(entity.get("error_description") or "Payment failed"),
            },
        )
        return {"received": True, "status": "FAILED"}

    if event not in {"payment.captured", "order.paid"} or not order_id:
        return {"received": True, "status": "IGNORED"}

    result = await finalize_order(
        db,
        order_id=order_id,
        gateway_payment_id=payment_id or "webhook",
        signature_payload={"source": "webhook", "event": event, "id": payment_id},
    )
    return {"received": True, "status": result["payment_status"]}


# ── driver balance QR ────────────────────────────────────────────────────────
#
# The driver app used to close an ONLINE trip without proving that any money
# had arrived. This QR is the proof: Razorpay owns the collection, and the close
# endpoint reads the QR back from Razorpay before it records the payment. A
# single-use, fixed-amount UPI QR means the amount cannot be edited by the
# driver and the same QR cannot collect twice.


async def cancel_open_booking_links(
    db: AsyncSession, cab_booking_id: int, *, keep: Optional[str] = None
) -> None:
    """Cancel unpaid links for this trip before another mode takes over.

    Without this a driver could generate a UPI QR, switch to cash, close the
    trip, and the customer could still pay the abandoned link — money arriving
    for a booking already settled in cash. Paid links are never cancelled here;
    the close endpoint refuses cash when one exists.
    """
    config = await get_razorpay_config(db, active_only=False)
    if not config:
        return
    # Keep the optional filter out of SQL when it is not used: asyncpg cannot
    # infer the type of a bare NULL bind (`$2 IS NULL`), and the close path
    # calls this without a keep value.
    keep_sql = " AND qr_reference <> :keep" if keep else ""
    rows = (
        (
            await db.execute(
                text(
                    "SELECT qr_reference, status FROM payment_qr_codes "
                    "WHERE cab_booking_id = :cab "
                    "  AND status NOT IN ('PAID','CANCELLED','EXPIRED')" + keep_sql
                ),
                {"cab": cab_booking_id, "keep": keep},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        reference = str(row["qr_reference"])
        try:
            await _razorpay_request(
                "POST",
                f"/payment_links/{reference}/cancel",
                key_id=config["key_id"],
                key_secret=config["key_secret"],
            )
            status = "CANCELLED"
        except PaymentGatewayError:
            # It may have just expired or been paid. Never label a paid link
            # cancelled: the close endpoint must see the money and refuse cash.
            try:
                checked = await fetch_booking_qr(db, reference)
                status = "PAID" if checked["is_paid"] else "CANCELLED"
            except Exception:
                status = str(row["status"] or "CANCELLED")
        await db.execute(
            text(
                "UPDATE payment_qr_codes SET status = :status, updated_at = NOW() "
                "WHERE qr_reference = :qr"
            ),
            {"status": status, "qr": reference},
        )


async def create_booking_qr(
    db: AsyncSession,
    *,
    master_booking_id: int,
    cab_booking_id: int,
    booking_number: str,
    amount: Decimal,
    description: str,
) -> Dict[str, Any]:
    config = await get_razorpay_config(db)
    if not config:
        raise HTTPException(409, "Online payment is not enabled right now.")

    amount = _money(amount)
    if amount <= 0:
        raise HTTPException(409, "There is no balance to collect online.")

    # One live link per trip: a second "Generate QR" replaces the first.
    await cancel_open_booking_links(db, cab_booking_id)

    # Razorpay's dedicated QR API is not enabled on every account. A payment
    # link is universally available, is still single-use for the exact amount,
    # and can be rendered as a UPI QR by the app. The link is the QR payload.
    link = await _razorpay_request(
        "POST",
        "/payment_links",
        key_id=config["key_id"],
        key_secret=config["key_secret"],
        payload={
            "amount": int((amount * 100).to_integral_value()),
            "currency": config["currency"],
            "accept_partial": False,
            "description": description[:255],
            "reference_id": f"{booking_number}-{int(time.time())}",
            "expire_by": int(time.time()) + 1800,
            "notify": {"sms": False, "email": False, "whatsapp": False},
            "reminder_enable": False,
            "notes": {
                "booking_number": booking_number,
                "purpose": "TRIP_BALANCE",
            },
        },
    )
    qr_reference = str(link.get("id") or "").strip()
    payment_url = str(link.get("short_url") or "").strip()
    if not qr_reference or not payment_url:
        raise PaymentGatewayError("The payment gateway did not return a payable link.")

    await db.execute(
        text(
            """
            INSERT INTO payment_qr_codes
                (qr_reference, master_booking_id, cab_booking_id, booking_number,
                 amount, currency, image_url, status, created_at, updated_at)
            VALUES
                (:qr, :master, :cab, :booking, :amount, :currency,
                 :url, 'ACTIVE', NOW(), NOW())
            ON CONFLICT (qr_reference) DO NOTHING
            """
        ),
        {
            "qr": qr_reference,
            "master": master_booking_id,
            "cab": cab_booking_id,
            "booking": booking_number,
            "amount": amount,
            "currency": config["currency"],
            "url": payment_url,
        },
    )
    return {
        "qr_reference": qr_reference,
        "payment_url": payment_url,
        "image_url": payment_url,
        "amount": float(amount),
        "currency": config["currency"],
        "status": str(link.get("status") or "created"),
    }


async def fetch_booking_qr(db: AsyncSession, qr_reference: str) -> Dict[str, Any]:
    config = await get_razorpay_config(db, active_only=False)
    if not config:
        raise HTTPException(409, "Online payment is not configured right now.")

    row = (
        (
            await db.execute(
                text(
                    "SELECT id, qr_reference, master_booking_id, cab_booking_id, "
                    "       booking_number, amount, status, image_url, "
                    "       gateway_payment_id, paid_amount "
                    "FROM payment_qr_codes WHERE qr_reference = :qr"
                ),
                {"qr": qr_reference},
            )
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(404, "Payment QR not found.")

    remote = await _razorpay_request(
        "GET",
        f"/payment_links/{qr_reference}",
        key_id=config["key_id"],
        key_secret=config["key_secret"],
    )
    paid = _money(remote.get("amount_paid")) / Decimal("100")
    gateway_status = str(remote.get("status") or "created").upper()
    local_status = "PAID" if paid >= _money(row["amount"]) else gateway_status

    await db.execute(
        text(
            """
            UPDATE payment_qr_codes
               SET paid_amount = :paid,
                   status = :status,
                   paid_at = CASE WHEN :is_paid THEN NOW() ELSE paid_at END,
                   updated_at = NOW()
             WHERE qr_reference = :qr
            """
        ),
        {
            "paid": paid,
            "status": local_status,
            "is_paid": paid >= _money(row["amount"]),
            "qr": qr_reference,
        },
    )
    return {
        "qr_reference": qr_reference,
        "master_booking_id": int(row["master_booking_id"]),
        "cab_booking_id": (
            int(row["cab_booking_id"]) if row["cab_booking_id"] is not None else None
        ),
        "amount": float(row["amount"]),
        "paid_amount": float(paid),
        "status": local_status,
        "is_paid": paid >= _money(row["amount"]),
        "payment_url": row["image_url"],
        "image_url": row["image_url"],
    }
