# ============================================================
# WAY TERO — CUSTOMER API ROUTER
# File: app/modules/customer/api.py
# Phase: 2 — Customer Module
# Doc Ref: API Documentation — Customer endpoints
# ============================================================

import math
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles
from app.modules.customer.schemas import (
    CustomerProfileUpdate,
    CustomerResponse,
    MobileAttachRequest,
    MobileOtpVerifyRequest,
    EmailUpdateRequest,
    AddressCreate,
    AddressResponse,
)
from app.modules.customer.services import CustomerService

router = APIRouter()


def get_service(db: AsyncSession = Depends(get_db)) -> CustomerService:
    return CustomerService(db=db)


# ---- Customer Profile ----


@router.get(
    "/me",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Get my customer profile",
)
async def get_my_profile(
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
):
    return await service.get_profile(UUID(current_user["sub"]))


@router.patch(
    "/me",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Update my customer profile",
)
async def update_my_profile(
    body: CustomerProfileUpdate,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
):
    return await service.update_profile(UUID(current_user["sub"]), body)


@router.patch(
    "/me/mobile",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Attach mobile WITHOUT OTP — stored as UNVERIFIED (no-SMS fallback)",
    description=(
        "Direct mobile attach used when NO SMS provider is integrated "
        "(Settings → API Integrations has no active MSG91 row). The web login "
        "flow saves the number right away so the customer can register / book "
        "immediately; `is_mobile_verified` stays FALSE. When an SMS provider "
        "is active, use POST /customers/me/mobile/verify-otp instead, which "
        "verifies the OTP and marks the mobile verified."
    ),
)
async def update_my_mobile(
    payload: MobileAttachRequest,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text

    user_uuid = UUID(current_user["sub"])
    mobile = (payload.mobile_number or "").strip()
    if not mobile or not (10 <= len(mobile) <= 15) or not mobile.isdigit():
        from app.core.exceptions import ValidationException

        raise ValidationException(
            message="Enter a valid 10–15 digit mobile number.",
            code="INVALID_MOBILE",
        )
    # Reject if another user already owns this mobile.
    dup = (
        await db.execute(
            _text("SELECT id FROM users WHERE mobile_number = :m AND id <> :uid"),
            {"m": mobile, "uid": str(user_uuid)},
        )
    ).first()
    if dup:
        from app.core.exceptions import DuplicateResourceException

        raise DuplicateResourceException(
            message="This mobile number is already linked to another account.",
            resource="User.mobile_number",
        )
    await db.execute(
        _text(
            """
            UPDATE users
               SET mobile_number = :m,
                   is_mobile_verified = FALSE,
                   updated_at = NOW()
             WHERE id = :uid
        """
        ),
        {"m": mobile, "uid": str(user_uuid)},
    )
    await db.commit()
    return await service.get_profile(user_uuid)


@router.post(
    "/me/mobile/verify-otp",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify OTP and attach mobile to the current account (Google login flow)",
    description=(
        "Verifies the OTP sent with purpose=MOBILE_ATTACH and persists the "
        "verified mobile on the currently authenticated user. Unlike "
        "/auth/verify-otp it never creates or switches to another customer "
        "account — the Google user keeps their identity. Returns the updated "
        "customer profile."
    ),
)
async def verify_and_attach_mobile(
    payload: MobileOtpVerifyRequest,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text

    from app.core.exceptions import DuplicateResourceException, ValidationException
    from app.infrastructure.cache.redis_client import RedisCache, get_redis
    from app.modules.auth.services import OTPService

    user_uuid = UUID(current_user["sub"])
    mobile = payload.mobile_number.strip()
    if not (10 <= len(mobile) <= 15) or not mobile.isdigit():
        raise ValidationException(
            message="Enter a valid 10–15 digit mobile number.",
            code="INVALID_MOBILE",
        )
    # Reject if another user already owns this mobile.
    dup = (
        await db.execute(
            _text("SELECT id FROM users WHERE mobile_number = :m AND id <> :uid"),
            {"m": mobile, "uid": str(user_uuid)},
        )
    ).first()
    if dup:
        raise DuplicateResourceException(
            message="This mobile number is already linked to another account.",
            resource="User.mobile_number",
        )
    # Verify the attach OTP — purpose-scoped so it can't collide with a login OTP.
    redis = await get_redis()
    otp_service = OTPService(cache=RedisCache(redis))
    await otp_service.verify_otp(mobile, payload.otp, purpose="MOBILE_ATTACH")
    await db.execute(
        _text(
            """
            UPDATE users
               SET mobile_number = :m,
                   is_mobile_verified = TRUE,
                   updated_at = NOW()
             WHERE id = :uid
        """
        ),
        {"m": mobile, "uid": str(user_uuid)},
    )
    await db.commit()
    return await service.get_profile(user_uuid)


@router.patch(
    "/me/email",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="Attach / update my email (mobile-OTP login flow)",
    description=(
        "Sets the email on the currently authenticated customer account. Used "
        "when a mobile-OTP user has no email on file — the web login flow "
        "offers it as an optional step. Rejects emails already claimed by "
        "another account."
    ),
)
async def update_my_email(
    payload: EmailUpdateRequest,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text

    from app.core.exceptions import DuplicateResourceException

    user_uuid = UUID(current_user["sub"])
    email = payload.email.strip().lower()
    # Read the current email + name BEFORE updating — if the account had no
    # email yet, this attach is the registration completion and we send the
    # welcome email (the OTP login path can't send it at signup because the
    # customer has no email then).
    cur = (
        (
            await db.execute(
                _text("SELECT email, first_name, last_name FROM users WHERE id = :uid"),
                {"uid": str(user_uuid)},
            )
        )
        .mappings()
        .first()
    )
    had_email = bool(cur and (cur["email"] or "").strip())
    # Reject if another user already owns this email.
    dup = (
        await db.execute(
            _text("SELECT id FROM users WHERE email = :e AND id <> :uid"),
            {"e": email, "uid": str(user_uuid)},
        )
    ).first()
    if dup:
        raise DuplicateResourceException(
            message="This email is already linked to another account.",
            resource="User.email",
        )
    await db.execute(
        _text(
            """
            UPDATE users
               SET email = :e,
                   updated_at = NOW()
             WHERE id = :uid
        """
        ),
        {"e": email, "uid": str(user_uuid)},
    )
    await db.commit()
    # ── Email: welcome the customer the first time an email is attached ──
    if not had_email:
        try:
            from app.infrastructure.email import send_event_email

            first_name = cur["first_name"] if cur else None
            last_name = cur["last_name"] if cur else None
            name = " ".join(filter(None, [first_name, last_name]))
            await send_event_email(
                db,
                event_type="customer_registered",
                to_email=email,
                to_name=name or None,
                context={
                    "name": first_name or "there",
                    "message": (
                        "Welcome to WayTero! Your account is ready — book cabs, "
                        "hotels and tour packages in a few taps."
                    ),
                    "body": [
                        "Your WayTero Travel Wallet is ready. Explore trips, earn "
                        "wallet cashbacks and track every booking live."
                    ],
                },
                related_type="CUSTOMER",
                related_id=str(user_uuid),
            )
        except Exception:  # pragma: no cover — email must never break the flow
            pass
    return await service.get_profile(user_uuid)


# ---- Address Management ----


@router.post(
    "/me/addresses",
    response_model=AddressResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new address",
)
async def add_address(
    body: AddressCreate,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
):
    return await service.add_address(UUID(current_user["sub"]), body)


@router.patch(
    "/me/addresses/{address_id}/default",
    response_model=AddressResponse,
    status_code=status.HTTP_200_OK,
    summary="Set an address as default",
)
async def set_default_address(
    address_id: int,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
):
    return await service.set_default_address(UUID(current_user["sub"]), address_id)


@router.delete(
    "/me/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an address",
)
async def delete_address(
    address_id: int,
    current_user: dict = Depends(get_current_user),
    service: CustomerService = Depends(get_service),
):
    await service.delete_address(UUID(current_user["sub"]), address_id)


# ---- My Wallet ----
# Customer-facing wallet: balance + ledger. Money lives in customer_wallets
# (seeded for every customer on startup) and customer_wallet_ledger; the
# admin/partner wallets module writes those rows. These endpoints are the
# read side for the customer web (/wallet page). Doc Ref: BRD Part 3 §44-45


async def _my_customer_row(db: AsyncSession, user_uuid: UUID):
    from sqlalchemy import text as _text

    return (
        (
            await db.execute(
                _text(
                    """
                    SELECT c.id, u.mobile_number AS mobile, u.email,
                           COALESCE(NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), ''), 'Customer') AS full_name
                    FROM customers c
                    JOIN users u ON u.id = c.user_id
                    WHERE c.user_id = :uid
                    """
                ),
                {"uid": str(user_uuid)},
            )
        )
        .mappings()
        .one_or_none()
    )


async def _my_wallet_row(db: AsyncSession, customer_id: int):
    from sqlalchemy import text as _text

    return (
        (
            await db.execute(
                _text(
                    "SELECT id, wallet_status, available_balance, hold_balance "
                    "FROM customer_wallets WHERE customer_id = :cid"
                ),
                {"cid": customer_id},
            )
        )
        .mappings()
        .one_or_none()
    )


def _wallet_payload(wallet) -> dict:
    if not wallet:
        return {
            "id": None,
            "wallet_status": "ACTIVE",
            "available_balance": 0.0,
            "hold_balance": 0.0,
            "total_balance": 0.0,
        }
    available = float(wallet["available_balance"] or 0)
    hold = float(wallet["hold_balance"] or 0)
    return {
        "id": wallet["id"],
        "wallet_status": wallet["wallet_status"],
        "available_balance": available,
        "hold_balance": hold,
        "total_balance": available + hold,
    }


async def _wallet_ledger_page(
    db: AsyncSession,
    wallet_id: Optional[int],
    page: int,
    page_size: int,
) -> dict:
    from sqlalchemy import text as _text

    if wallet_id is None:
        return {
            "total": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 1,
            "entries": [],
        }

    total = (
        await db.execute(
            _text(
                "SELECT COUNT(*) FROM customer_wallet_ledger "
                "WHERE customer_wallet_id = :wid"
            ),
            {"wid": wallet_id},
        )
    ).scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                _text(
                    """
                    SELECT id, transaction_reference, reference_type,
                           debit_amount, credit_amount, balance_after,
                           narration, created_at
                    FROM customer_wallet_ledger
                    WHERE customer_wallet_id = :wid
                    ORDER BY created_at DESC, id DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                {"wid": wallet_id, "lim": page_size, "off": offset},
            )
        )
        .mappings()
        .all()
    )

    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(int(total) / page_size) if total > 0 else 1,
        "entries": [
            {
                "id": r["id"],
                "ref": r["transaction_reference"],
                "ref_type": r["reference_type"],
                "debit": float(r["debit_amount"] or 0),
                "credit": float(r["credit_amount"] or 0),
                "balance_after": float(r["balance_after"] or 0),
                "narration": r["narration"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get(
    "/me/wallet",
    status_code=status.HTTP_200_OK,
    summary="Get my wallet balance and recent activity",
)
async def get_my_wallet(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Wallet summary + the 10 most recent ledger entries for the current
    customer. Returns zeroed balances when no wallet row exists yet (first
    booking creates it) so the UI never 404s."""
    cust = await _my_customer_row(db, UUID(current_user["sub"]))
    wallet = await _my_wallet_row(db, cust["id"]) if cust else None
    ledger = await _wallet_ledger_page(db, wallet["id"] if wallet else None, 1, 10)

    return {
        "customer": (
            {
                "id": cust["id"],
                "name": cust["full_name"],
                "mobile": cust["mobile"],
                "email": cust["email"],
            }
            if cust
            else None
        ),
        "wallet": _wallet_payload(wallet),
        "ledger": ledger,
    }


@router.get(
    "/me/wallet/ledger",
    status_code=status.HTTP_200_OK,
    summary="Get my wallet transaction history (paginated)",
)
async def get_my_wallet_ledger(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cust = await _my_customer_row(db, UUID(current_user["sub"]))
    wallet = await _my_wallet_row(db, cust["id"]) if cust else None
    return await _wallet_ledger_page(
        db, wallet["id"] if wallet else None, page, page_size
    )


# ---- Admin endpoints ----


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="[Admin] List all customers",
)
async def list_customers(
    city_id: int = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
    service: CustomerService = Depends(get_service),
):
    return await service.list_customers(city_id=city_id, page=page, per_page=per_page)


@router.get(
    "/{customer_uuid}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
    summary="[Admin] Get customer by UUID",
)
async def get_customer(
    customer_uuid: UUID,
    current_user: dict = Depends(require_roles("ADMIN", "SUPER_ADMIN", "CCO")),
    service: CustomerService = Depends(get_service),
):
    return await service.get_by_uuid(customer_uuid)
