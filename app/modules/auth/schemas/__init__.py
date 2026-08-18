# ============================================================
# WAY TERO - AUTH SCHEMAS
# File: app/modules/auth/schemas/__init__.py
# Phase: 1 - Authentication Module
# ============================================================

import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.shared.enums.user_types import UserType


def _normalize_mobile(value: str) -> str:
    value = value.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^[6-9]\d{9}$", value):
        raise ValueError("Enter a valid 10-digit Indian mobile number")
    return value


def _validate_password(value: str) -> str:
    if len(value) < 12:
        raise ValueError("Password must be at least 12 characters long")
    if not re.search(r"[A-Z]", value):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", value):
        raise ValueError("Password must contain at least one lowercase letter")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain at least one digit")
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;':\",./<>?]", value):
        raise ValueError("Password must contain at least one special character")
    return value


class SendOTPRequest(BaseModel):
    """POST /api/v1/auth/send-otp"""

    mobile: str
    # Part of the OTP scope: LOGIN (default) | REGISTER | PASSWORD_RESET |
    # MOBILE_ATTACH. MOBILE_ATTACH is used when the OTP verifies a NEW mobile
    # that gets linked to an existing (Google) account — keeping it in a
    # separate scope means it can never consume or collide with a login OTP.
    purpose: str = "LOGIN"

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        return _normalize_mobile(value)


class OTPLoginRequest(BaseModel):
    """OTP verification based login."""

    mobile: str
    otp: str
    user_type: Optional[UserType] = None
    device_name: Optional[str] = None
    device_os: Optional[str] = None

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        return _normalize_mobile(value)

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^\d{6}$", value):
            raise ValueError("OTP must be a 6-digit number")
        return value


class VerifyOTPRequest(OTPLoginRequest):
    """POST /api/v1/auth/verify-otp"""


class PartnerLoginRequest(BaseModel):
    """POST /api/v1/auth/partner/login — accepts mobile OR email + password"""

    mobile: Optional[str] = None  # 10-digit mobile number
    email: Optional[str] = None  # email address
    password: str
    device_name: Optional[str] = None
    device_os: Optional[str] = None

    @model_validator(mode="after")
    def validate_identifier(self) -> "PartnerLoginRequest":
        if not self.mobile and not self.email:
            raise ValueError("Provide either mobile number or email address.")
        if self.mobile:
            self.mobile = _normalize_mobile(self.mobile)
        if self.email:
            self.email = self.email.strip().lower()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", self.email):
                raise ValueError("Enter a valid email address.")
        return self


def _validate_admin_username(value: str) -> str:
    """Accept an email address OR a 10-digit Indian mobile number."""
    value = value.strip()
    if "@" in value:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise ValueError("Enter a valid email address")
        return value.lower()
    cleaned = value.replace(" ", "").replace("-", "")
    if not re.match(r"^[6-9]\d{9}$", cleaned):
        raise ValueError("Enter a valid email or 10-digit Indian mobile number")
    return cleaned


class AdminSendOTPRequest(BaseModel):
    """POST /api/v1/auth/admin/send-otp — OTP login method.

    Sends a login OTP to the admin's registered mobile number. The mobile
    must belong to an ADMIN / SUPER_ADMIN user (verified server-side).
    """

    mobile: str

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        return _normalize_mobile(value)


class AdminPasswordLoginRequest(BaseModel):
    """POST /api/v1/auth/admin/login/password — password login method.

    Admin signs in with their email address OR 10-digit mobile number plus
    password. No OTP required.
    """

    username: str  # email address or 10-digit mobile number
    password: str
    device_name: Optional[str] = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return _validate_admin_username(value)


class AdminOTPLoginRequest(BaseModel):
    """POST /api/v1/auth/admin/login/otp — OTP login method.

    Admin signs in with mobile number + the OTP sent via
    /auth/admin/send-otp.
    """

    mobile: str
    otp: str
    device_name: Optional[str] = None

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        return _normalize_mobile(value)

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^\d{6}$", value):
            raise ValueError("OTP must be a 6-digit number")
        return value


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None
    logout_all_devices: bool = False


class RevokeSessionRequest(BaseModel):
    session_id: UUID


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password(value)

    @model_validator(mode="after")
    def passwords_match(self) -> "ChangePasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class ForgotPasswordRequest(BaseModel):
    """Initiate OTP-based password reset.

    Doc Ref: Auth Flow — OTP-based password reset.
    Accepts an email address OR a 10-digit Indian mobile number.
    """

    identifier: str  # email address OR 10-digit mobile number

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip().replace(" ", "")
        if not value:
            raise ValueError("Provide an email address or mobile number")
        return value


class ResetPasswordRequest(BaseModel):
    """Reset password after verifying OTP.

    Doc Ref: Auth Flow — OTP-based password reset.
    reset_token: returned by /auth/forgot-password in the response.
    otp: the 6-digit OTP sent to the user's email or mobile.
    """

    reset_token: str
    otp: str
    new_password: str
    confirm_password: str

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^\d{6}$", value):
            raise ValueError("OTP must be exactly 6 digits")
        return value

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password(value)

    @model_validator(mode="after")
    def passwords_match(self) -> "ResetPasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class PartnerChangeFirstPasswordRequest(BaseModel):
    """Partner sets new password replacing default Waytero@15 — Doc Ref: Auth Flow §6"""

    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        v = v.strip()
        if v == "Waytero@15":
            raise ValueError(
                "Cannot reuse the default password. Please choose a new secure password."
            )
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one number.")
        if not re.search(r"[^A-Za-z0-9]", v):
            raise ValueError("Password must contain at least one special character.")
        return v


class FirebaseSignInRequest(BaseModel):
    """Doc Ref: Customer web Google sign-in — POST /auth/firebase-sign-in

    Body carries the Firebase ID token (obtained client-side via
    `signInWithPopup(GoogleAuthProvider).then(user => user.getIdToken())`).
    The backend verifies the token with the Admin SDK, finds-or-creates the
    local customer user, and returns the same envelope as the OTP login
    flow (WayTero JWT pair + user profile).
    """

    id_token: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    device_os: Optional[str] = None

    @field_validator("id_token")
    @classmethod
    def validate_id_token(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 20:
            raise ValueError("Firebase ID token looks too short")
        return v


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class AuthUserResponse(BaseModel):
    id: UUID
    mobile: str
    email: Optional[str]
    full_name: Optional[str]
    user_type: str
    status: str
    roles: list[str]
    permissions: list[str]
    is_mobile_verified: bool
    is_email_verified: bool
    must_change_password: bool = False  # True when force_password_change flag is set
    partner_id: Optional[int] = (
        None  # Integer partners.id — populated for PARTNER user_type
    )

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    user: AuthUserResponse
    tokens: TokenResponse
    session_id: UUID


class OTPSentResponse(BaseModel):
    mobile_number: str
    message: str = "OTP sent successfully"
    expires_in_seconds: int = 300
    dev_otp: Optional[str] = (
        None  # Only populated in non-production for dev convenience
    )


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    permissions: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class SessionInfoResponse(BaseModel):
    id: UUID
    device_name: Optional[str]
    ip_address: Optional[str]
    user_agent: Optional[str]
    is_active: bool
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime

    model_config = {"from_attributes": True}


class SessionsResponse(BaseModel):
    sessions: list[SessionInfoResponse]


class MessageResponse(BaseModel):
    success: bool = True
    message: str
