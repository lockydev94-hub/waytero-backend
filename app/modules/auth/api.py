# ============================================================
# WAY TERO - AUTH API ROUTER
# File: app/modules/auth/api.py
# Phase: 1 - Authentication Module
# ============================================================

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.infrastructure.cache.redis_client import RedisCache, get_redis
from app.modules.auth.schemas import (
    AdminOTPLoginRequest,
    AdminPasswordLoginRequest,
    AdminSendOTPRequest,
    ChangePasswordRequest,
    FirebaseSignInRequest,
    ForgotPasswordRequest,
    LoginResponse,
    LogoutRequest,
    MessageResponse,
    OTPSentResponse,
    OTPLoginRequest,
    PartnerLoginRequest,
    PartnerChangeFirstPasswordRequest,
    RefreshResponse,
    RefreshTokenRequest,
    ResetPasswordRequest,
    RevokeSessionRequest,
    SendOTPRequest,
    SessionsResponse,
    VerifyOTPRequest,
)
from app.modules.auth.services import AuthService, OTPService
from app.shared.enums.user_types import UserType

router = APIRouter()


async def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    redis = await get_redis()
    return AuthService(db=db, cache=RedisCache(redis))


async def get_otp_service() -> OTPService:
    redis = await get_redis()
    return OTPService(cache=RedisCache(redis))


async def _otp_login_by_user_type(
    request: Request,
    body: OTPLoginRequest,
    auth_service: AuthService,
) -> LoginResponse:
    ip_address = request.client.host if request.client else None
    user_type = body.user_type or UserType.CUSTOMER

    if user_type == UserType.DRIVER:
        return await auth_service.driver_otp_login(
            mobile=body.mobile,
            otp=body.otp,
            device_name=body.device_name,
            device_os=body.device_os,
            ip_address=ip_address,
        )
    if user_type == UserType.PARTNER:
        return await auth_service.partner_otp_login(
            mobile=body.mobile,
            otp=body.otp,
            device_name=body.device_name,
            device_os=body.device_os,
            ip_address=ip_address,
        )
    return await auth_service.customer_otp_login(
        mobile=body.mobile,
        otp=body.otp,
        device_name=body.device_name,
        device_os=body.device_os,
        ip_address=ip_address,
    )


@router.get(
    "/config",
    status_code=status.HTTP_200_OK,
    summary="Auth capabilities — is mobile-OTP login available?",
    description=(
        "OTP delivery needs an active SMS provider (MSG91) under Settings → "
        "API Integrations. Without one, web clients should hide the mobile/OTP "
        "auth UI and rely on Google sign-in, saving mobiles as UNVERIFIED. "
        "When the admin activates an SMS provider this flips back on "
        "automatically."
    ),
)
async def auth_config(
    db: AsyncSession = Depends(get_db),
):
    sms_rows = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM api_integrations "
                "WHERE service_type IN ('MSG91') AND is_active = TRUE "
                "AND configuration IS NOT NULL "
                "AND configuration::text NOT IN ('{}', 'null')"
            )
        )
    ).scalar() or 0
    sms_configured = int(sms_rows) > 0
    return {
        "success": True,
        "data": {
            "sms_configured": sms_configured,
            "mobile_login_enabled": sms_configured,
        },
    }


@router.post(
    "/send-otp",
    response_model=OTPSentResponse,
    status_code=status.HTTP_200_OK,
    summary="Send OTP to mobile number",
)
async def send_otp(
    body: SendOTPRequest,
    otp_service: OTPService = Depends(get_otp_service),
):
    # purpose is part of the OTP scope — LOGIN / REGISTER / PASSWORD_RESET /
    # MOBILE_ATTACH. Passing it keeps attach OTPs separate from login OTPs so
    # verifying one never consumes the other.
    return await otp_service.send_otp(mobile=body.mobile, purpose=body.purpose)


@router.post(
    "/verify-otp",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify OTP and login",
)
async def verify_otp(
    request: Request,
    body: VerifyOTPRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    return await _otp_login_by_user_type(request, body, auth_service)


@router.post(
    "/firebase-sign-in",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in with Firebase ID token (e.g. Google sign-in)",
    description=(
        "Verifies a Firebase ID token with the Admin SDK, finds-or-creates the "
        "local customer user, and returns the same envelope as /auth/verify-otp "
        "(WayTero access + refresh JWTs and user profile)."
    ),
)
async def firebase_sign_in(
    request: Request,
    body: FirebaseSignInRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.customer_firebase_login(
        id_token=body.id_token,
        device_name=body.device_name,
        device_os=body.device_os,
        ip_address=ip_address,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="OTP login for customer or driver",
)
async def login(
    request: Request,
    body: OTPLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    return await _otp_login_by_user_type(request, body, auth_service)


@router.post(
    "/driver/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Driver OTP login",
)
async def driver_login(
    request: Request,
    body: OTPLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    payload = body.model_copy(update={"user_type": UserType.DRIVER})
    return await _otp_login_by_user_type(request, payload, auth_service)


@router.post(
    "/partner/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Partner login with password",
)
async def partner_login(
    request: Request,
    body: PartnerLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.partner_password_login(
        mobile=body.mobile,
        email=body.email,
        password=body.password,
        device_name=body.device_name,
        device_os=body.device_os,
        ip_address=ip_address,
    )


@router.post(
    "/partner/login/otp",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Partner login with OTP",
)
async def partner_otp_login(
    request: Request,
    body: OTPLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    payload = body.model_copy(update={"user_type": UserType.PARTNER})
    return await _otp_login_by_user_type(request, payload, auth_service)


@router.post(
    "/admin/send-otp",
    response_model=OTPSentResponse,
    status_code=status.HTTP_200_OK,
    summary="Send OTP for admin OTP login — accepts registered admin mobile",
)
async def admin_send_otp(
    body: AdminSendOTPRequest,
    db: AsyncSession = Depends(get_db),
    otp_service: OTPService = Depends(get_otp_service),
):
    """Send a login OTP to an admin's registered mobile number.

    Only sends when the mobile belongs to an ADMIN / SUPER_ADMIN user
    (checked without revealing whether the number is registered).
    """
    from sqlalchemy import select
    from app.modules.auth.models.user import User
    from app.shared.enums.user_types import UserType as _UserType

    result = await db.execute(
        select(User.id).where(
            User.mobile_number == body.mobile,
            User.user_type.in_([_UserType.ADMIN, _UserType.SUPER_ADMIN]),
        )
    )
    if result.scalar_one_or_none() is None:
        from app.core.exceptions import AuthenticationException

        raise AuthenticationException(
            message="Admin account not found for this mobile number.",
            code="ADMIN_NOT_FOUND",
        )

    return await otp_service.send_otp(mobile=body.mobile)


@router.post(
    "/admin/login/password",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin login with email/mobile + password (no OTP)",
)
async def admin_password_login(
    request: Request,
    body: AdminPasswordLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.admin_password_login(
        username=body.username,
        password=body.password,
        device_name=body.device_name,
        ip_address=ip_address,
    )


@router.post(
    "/admin/login/otp",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Admin login with mobile + OTP",
)
async def admin_otp_login(
    request: Request,
    body: AdminOTPLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.admin_otp_login(
        mobile=body.mobile,
        otp=body.otp,
        device_name=body.device_name,
        ip_address=ip_address,
    )


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh access token using refresh token",
)
async def refresh_token(
    body: RefreshTokenRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    return await auth_service.refresh_access_token(refresh_token=body.refresh_token)


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Logout from current device or all devices",
)
async def logout(
    body: LogoutRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.logout(
        user_id=UUID(current_user["sub"]),
        refresh_token=body.refresh_token,
        logout_all=body.logout_all_devices,
    )
    return MessageResponse(message="Logged out successfully")


@router.get(
    "/sessions",
    response_model=SessionsResponse,
    status_code=status.HTTP_200_OK,
    summary="List active sessions for the current user",
)
async def list_sessions(
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    sessions = await auth_service.list_sessions(UUID(current_user["sub"]))
    return SessionsResponse(sessions=sessions)


@router.post(
    "/sessions/revoke",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke one active session for the current user",
)
async def revoke_session(
    body: RevokeSessionRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.revoke_session(UUID(current_user["sub"]), body.session_id)
    return MessageResponse(message="Session revoked successfully")


@router.post(
    "/change-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Change account password",
)
async def change_password(
    body: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.change_password(
        user_id=UUID(current_user["sub"]),
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return MessageResponse(message="Password changed successfully. Please login again.")


@router.post(
    "/forgot-password",
    status_code=status.HTTP_200_OK,
    summary="Initiate password reset via OTP",
    description=(
        "Send a 6-digit OTP to the user's email (preferred) or mobile. "
        "Returns a reset_token the client must pass to /reset-password. "
        "Resolution: email first (if SMTP configured), then SMS (if MSG91 "
        "configured). Raises 400 if neither channel is available."
    ),
)
async def forgot_password(
    body: ForgotPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    result = await auth_service.initiate_password_reset(body.identifier)
    return {
        "success": True,
        "message": result["message"],
        "data": {
            "channel": result.get("channel"),
            "reset_token": result.get("reset_token"),
            "masked_identifier": result.get("masked_identifier"),
            **({"dev_otp": result["dev_otp"]} if result.get("dev_otp") else {}),
        },
    }


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Reset password using OTP + reset token",
)
async def reset_password(
    body: ResetPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.reset_password(
        reset_token=body.reset_token,
        otp=body.otp,
        new_password=body.new_password,
    )
    return MessageResponse(message="Password reset successfully. Please login again.")


@router.post(
    "/partner/change-first-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Partner: Set new password replacing the default Waytero@15 password",
    description=(
        "Must be called when must_change_password=True is returned from login. "
        "The new password cannot be Waytero@15. Clears force_password_change flag "
        "and revokes all existing sessions so partner must log in again with new password. "
        "Doc Ref: Partner Portal — first-login force password change"
    ),
)
async def partner_change_first_password(
    body: PartnerChangeFirstPasswordRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.partner_change_first_password(
        user_id=UUID(current_user["sub"]),
        new_password=body.new_password,
    )
    return MessageResponse(
        message="Password updated successfully. Please login with your new password."
    )


@router.get(
    "/me",
    status_code=status.HTTP_200_OK,
    summary="Get current authenticated user info",
)
async def get_me(
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    profile = await auth_service.get_profile(UUID(current_user["sub"]))
    return {"success": True, "data": profile.model_dump(mode="json")}
