from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.payment.schemas import (
    CreatePaymentOrderIn,
    VerifyPaymentIn,
)
from app.modules.payment.services import (
    PaymentGatewayError,
    create_order,
    handle_webhook,
    public_payment_config,
    verify_payment,
)
from app.shared.responses.base import success_response

public_router = APIRouter()
router = APIRouter()


@public_router.get(
    "/config",
    summary="Whether customer online payment is enabled (no auth)",
)
async def payment_config(db: AsyncSession = Depends(get_db)):
    """Expose only what checkout needs: the public key id and advance percent.

    Secrets stay in admin Settings and are never returned by this endpoint.
    """
    return success_response("Payment configuration", await public_payment_config(db))


@router.post("/orders", summary="Create a Razorpay order for a booking")
async def create_booking_payment_order(
    body: CreatePaymentOrderIn,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        order = await create_order(
            db,
            current_user=current_user,
            booking_number=body.booking_number.strip(),
            payment_type=body.payment_type,
        )
    except PaymentGatewayError as exc:
        raise HTTPException(502, str(exc)) from exc
    return success_response("Payment order created", order)


@router.post("/verify", summary="Verify a Razorpay checkout signature")
async def verify_booking_payment(
    body: VerifyPaymentIn,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await verify_payment(
        db,
        current_user=current_user,
        order_id=body.razorpay_order_id.strip(),
        payment_id=body.razorpay_payment_id.strip(),
        signature=body.razorpay_signature.strip(),
    )
    message = (
        "Payment received"
        if result.get("payment_status") == "SUCCESS"
        else "Payment verified"
    )
    return success_response(message, result)


@router.post("/webhook", summary="Razorpay webhook (signature protected)")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid webhook payload.") from exc
    result = await handle_webhook(
        db,
        raw_body=raw_body,
        signature=signature,
        payload=payload if isinstance(payload, dict) else {},
    )
    return result
