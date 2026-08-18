# ============================================================
# WAY TERO — AUTH SCHEMAS
# File: app/modules/auth/schemas.py
# Doc Ref: Auth Flow Part 4 — Sections 5,9,13,14,15,17
# Pydantic v2 models for all auth request/response payloads
# ============================================================

import re
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, field_validator


# ============================================================
# REQUEST SCHEMAS
# ============================================================


class SendOTPRequest(BaseModel):
    """Doc Ref: Section 9 — POST /auth/send-otp"""

    mobile: str
    purpose: str = "LOGIN"  # LOGIN | REGISTER | PASSWORD_RESET

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        v = v.strip().replace(" ", "").replace("-", "")
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v


class VerifyOTPRequest(BaseModel):
    """Doc Ref: Section 9 — POST /auth/verify-otp"""

    mobile: str
    otp: str

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^\d{6}$", v):
            raise ValueError("OTP must be exactly 6 digits")
        return v


class OTPLoginRequest(BaseModel):
    """Customer / Driver OTP login — Doc Ref: Section 5, 7"""

    mobile: str
    otp: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v


class PartnerLoginRequest(BaseModel):
    """Partner password login — Doc Ref: Section 6"""

    mobile: str
    password: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v


class AdminLoginRequest(BaseModel):
    """Admin login (password + OTP MFA) — Doc Ref: Section 8"""

    mobile: str
    password: str
    otp: Optional[str] = None
    device_id: Optional[str] = None
    device_name: Optional[str] = None

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not re.match(r"^[6-9]\d{9}$", v):
            raise ValueError("Enter a valid 10-digit Indian mobile number")
        return v


class RefreshTokenRequest(BaseModel):
    """Doc Ref: Section 15 — Refresh access token"""

    refresh_token: str


class LogoutRequest(BaseModel):
    """Logout current or specific session"""

    session_id: Optional[UUID] = None  # None = logout current session


class RevokeSessionRequest(BaseModel):
    """Revoke a specific session by ID — Doc Ref: Section 17"""

    session_id: UUID


class ChangePasswordRequest(BaseModel):
    """Doc Ref: Section 11 — Password Policy"""

    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 12:
            raise ValueError("Password must be at least 12 characters")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one number")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", v):
            raise ValueError("Password must contain at least one special character")
        return v


class ForgotPasswordRequest(BaseModel):
    """Initiate password reset — accepts email OR mobile.

    Doc Ref: Auth Flow — OTP-based password reset.
    The backend resolves the user by email first, then mobile, and
    sends an OTP to the first channel that is configured (email SMTP
    preferred, SMS fallback).
    """

    identifier: str  # email address OR 10-digit mobile number

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not v:
            raise ValueError("Provide an email address or mobile number")
        return v


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
    def validate_otp(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^\d{6}$", v):
            raise ValueError("OTP must be exactly 6 digits")
        return v

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 12:
            raise ValueError("Password must be at least 12 characters")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Must contain uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Must contain lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Must contain a number")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", v):
            raise ValueError("Must contain a special character")
        return v

    @field_validator("confirm_password")
    @classmethod
    def validate_confirm(cls, v: str) -> str:
        return v


# ============================================================
# RESPONSE SCHEMAS
# ============================================================


class UserPayload(BaseModel):
    """Minimal user info embedded in login responses"""

    id: UUID
    first_name: str
    last_name: Optional[str] = None
    mobile_number: str
    email: Optional[str] = None
    user_type: str
    status: str
    is_mobile_verified: bool
    profile_image_url: Optional[str] = None
    must_change_password: bool = False  # True for partners using default password

    class Config:
        from_attributes = True


class AuthUserResponse(BaseModel):
    """Auth user response including must_change_password flag"""

    id: UUID
    mobile: str
    email: Optional[str] = None
    full_name: str
    user_type: str
    status: str
    roles: List[str] = []
    permissions: List[str] = []
    is_mobile_verified: bool = False
    is_email_verified: bool = False
    must_change_password: bool = False  # Doc Ref: Partner first-login force change


class PartnerChangeFirstPasswordRequest(BaseModel):
    """Request for partner to set new password replacing Waytero@15"""

    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if v == "Waytero@15":
            raise ValueError(
                "You cannot use the default password. Please choose a new password."
            )
        return v


class TokenData(BaseModel):
    """Doc Ref: Section 14 — access token payload"""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds


class LoginResponse(BaseModel):
    """Full login response — tokens + user"""

    success: bool = True
    message: str
    data: Optional[dict] = None


class OTPSentResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[dict] = None


class RefreshResponse(BaseModel):
    success: bool = True
    message: str = "Token refreshed"
    data: Optional[dict] = None


class SessionInfo(BaseModel):
    """Single session info — Doc Ref: Section 17"""

    id: UUID
    device_name: Optional[str] = None
    device_id: Optional[str] = None
    ip_address: Optional[str] = None
    login_at: datetime
    expires_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class SessionsResponse(BaseModel):
    success: bool = True
    message: str = "Sessions retrieved"
    data: Optional[List[SessionInfo]] = None


class MessageResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[dict] = None
