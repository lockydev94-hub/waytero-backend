# ============================================================
# WAY TERO - AUTH API ROUTER
# File: app/modules/auth/api.py
# Phase: 1 - Authentication Module
# ============================================================

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
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
    HeartbeatRequest,
    IdleLockMinutesRequest,
    LoginResponse,
    LogoutRequest,
    MessageResponse,
    MpinChangeRequest,
    MpinRemoveRequest,
    MpinSetRequest,
    MpinVerifyRequest,
    OTPSentResponse,
    OTPLoginRequest,
    PartnerLoginRequest,
    PartnerChangeFirstPasswordRequest,
    PartnerRegisterStartRequest,
    PartnerSelfRegisterRequest,
    RefreshResponse,
    RefreshTokenRequest,
    ResetPasswordRequest,
    RevokeSessionRequest,
    SendOTPRequest,
    SessionConfigResponse,
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
    "/driver/google",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Driver Google sign-in (existing driver email)",
)
async def driver_google_login(
    request: Request,
    body: FirebaseSignInRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.driver_firebase_login(
        id_token=body.id_token,
        device_name=body.device_name or "WayTero Partner Driver Android",
        device_os=body.device_os or "Android",
        ip_address=ip_address,
    )


@router.post(
    "/partner/register/otp",
    response_model=OTPSentResponse,
    status_code=status.HTTP_200_OK,
    summary="Send OTP for partner self-registration",
)
async def partner_register_otp(
    body: PartnerRegisterStartRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    return await auth_service.send_partner_registration_otp(body.mobile)


@router.post(
    "/partner/register",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Apply as a partner and create a pending partner account",
)
async def partner_register(
    request: Request,
    body: PartnerSelfRegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.partner_self_register(
        data=body,
        device_name="WayTero Partner Android",
        device_os="Android",
        ip_address=ip_address,
    )


@router.post(
    "/partner/google",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Partner Google sign-in (existing partner email)",
)
async def partner_google_login(
    request: Request,
    body: FirebaseSignInRequest,
    auth_service: AuthService = Depends(get_auth_service),
):
    ip_address = request.client.host if request.client else None
    return await auth_service.partner_firebase_login(
        id_token=body.id_token,
        device_name=body.device_name,
        device_os=body.device_os,
        ip_address=ip_address,
    )


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


# ════════════════════════════════════════════════════════════════
#  ADMIN DASHBOARD SESSION POLICY — unlock PIN + idle lock
#  Doc Ref: Admin Portal §39 Security; migration 0084.
#
#  The dashboard locks on idle instead of logging the admin out, and the PIN is
#  checked against the users row, not the JWT. Tokens stay alive through a lock
#  on purpose (W4 freezes calls in the client); /auth/refresh is allow-listed so
#  the 15-minute token can still roll over while the screen is locked.
#  ════════════════════════════════════════════════════════════════


@router.get(
    "/admin/session-config",
    response_model=SessionConfigResponse,
    status_code=status.HTTP_200_OK,
    summary="Session policy for the signed-in admin — idle lock, daily logout hour, PIN state",
    description=(
        "One round trip for the whole shell. The client derives the "
        "forced-logout deadline from `server_now`, never from the laptop clock, "
        "so a wrong local time cannot skip the daily logout."
    ),
)
async def admin_session_config(
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    return await auth_service.get_session_config(
        UUID(current_user["sub"]), current_user.get("session_id")
    )


@router.post(
    "/admin/mpin/otp",
    response_model=OTPSentResponse,
    status_code=status.HTTP_200_OK,
    summary="OTP to this admin's own mobile, to prove ownership before installing a PIN",
    description=(
        "Password accounts confirm the password instead; this exists for admins "
        "who only ever sign in with OTP. The number comes from the session "
        "subject, never from the request body."
    ),
)
async def admin_mpin_otp(
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    return await auth_service.send_mpin_otp(UUID(current_user["sub"]))


@router.post(
    "/admin/mpin/set",
    status_code=status.HTTP_200_OK,
    summary="Install the dashboard unlock PIN",
    description=(
        "Requires the account password whenever one is set: without re-entry a "
        "hijacked live session token could install the gate that locks the real "
        "admin out. An OTP-only admin confirms the OTP from "
        "POST /auth/admin/mpin/otp. Returns the refreshed policy."
    ),
)
async def admin_mpin_set(
    body: MpinSetRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user_id = UUID(current_user["sub"])
    await auth_service.set_mpin(
        user_id=user_id,
        mpin=body.mpin,
        current_password=body.current_password,
        otp=body.otp,
    )
    return {
        "success": True,
        "data": (
            await auth_service.get_session_config(
                user_id, current_user.get("session_id")
            )
        ).model_dump(mode="json"),
    }


@router.post(
    "/admin/mpin/change",
    status_code=status.HTTP_200_OK,
    summary="Replace the dashboard unlock PIN",
)
async def admin_mpin_change(
    body: MpinChangeRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user_id = UUID(current_user["sub"])
    await auth_service.change_mpin(
        user_id=user_id, current_mpin=body.current_mpin, new_mpin=body.new_mpin
    )
    return {
        "success": True,
        "data": (
            await auth_service.get_session_config(
                user_id, current_user.get("session_id")
            )
        ).model_dump(mode="json"),
    }


@router.post(
    "/admin/mpin/verify",
    status_code=status.HTTP_200_OK,
    summary="Unlock the dashboard after an idle lock",
    description=(
        "Five wrong attempts lock the PIN for the configured window; that is "
        "cleared by password re-entry, not by waiting it out. Issues no tokens — "
        "the session was never dropped."
    ),
)
async def admin_mpin_verify(
    body: MpinVerifyRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    await auth_service.verify_mpin(user_id=UUID(current_user["sub"]), mpin=body.mpin)
    return {"success": True, "data": {"unlocked": True}}


@router.delete(
    "/admin/mpin",
    status_code=status.HTTP_200_OK,
    summary="Remove the dashboard unlock PIN",
    description=(
        "Back to the old behaviour: with no PIN the dashboard logs out on idle, "
        "because there is nothing to unlock with."
    ),
)
async def admin_mpin_remove(
    body: MpinRemoveRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user_id = UUID(current_user["sub"])
    await auth_service.remove_mpin(
        user_id=user_id,
        current_password=body.current_password,
        current_mpin=body.current_mpin,
    )
    return {
        "success": True,
        "data": (
            await auth_service.get_session_config(
                user_id, current_user.get("session_id")
            )
        ).model_dump(mode="json"),
    }


@router.put(
    "/admin/idle-lock-minutes",
    status_code=status.HTTP_200_OK,
    summary="Set how soon this admin's dashboard locks",
    description=(
        "Reduce-only against the platform default, so the idle lock is not "
        "opt-out. Send null to inherit the default again."
    ),
)
async def admin_idle_lock_minutes(
    body: IdleLockMinutesRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user_id = UUID(current_user["sub"])
    await auth_service.set_idle_lock_minutes(
        user_id=user_id, idle_lock_minutes=body.idle_lock_minutes
    )
    # Same shape as the PIN mutations: the shell re-arms its idle timer from the
    # refreshed policy rather than guessing what the effective minute count is.
    return {
        "success": True,
        "data": (
            await auth_service.get_session_config(
                user_id, current_user.get("session_id")
            )
        ).model_dump(mode="json"),
    }


@router.post(
    "/admin/heartbeat",
    status_code=status.HTTP_200_OK,
    summary="Record actively-worked seconds for today",
    description=(
        "Called by the portal while the admin is actually interacting with it. "
        "Idle gaps, locked screens and closed laptops are not work time, so the "
        "seconds arrive here instead of being derived from login and logout. "
        "The server keeps at most a minute per call."
    ),
)
async def admin_heartbeat(
    body: HeartbeatRequest,
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user_id = UUID(current_user["sub"])
    recorded = await auth_service.record_heartbeat(
        user_id=user_id, delta_seconds=body.delta_seconds
    )
    return {"success": True, "data": {"recorded_seconds": recorded}}


@router.get(
    "/admin/work-hours",
    status_code=status.HTTP_200_OK,
    summary="This admin's own recent working days",
    description=(
        "The profile tab shows the signed-in admin their own attendance. The "
        "all-staff report lives at GET /admin/staff-work-hours and is "
        "super-admin only."
    ),
)
async def admin_my_work_hours(
    days: int = Query(30, ge=1, le=366),
    current_user: dict = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    user_id = UUID(current_user["sub"])
    data = await auth_service.get_my_work_hours(user_id=user_id, days=days)
    return {"success": True, "data": data}


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
