# ============================================================
# WAY TERO — SECURITY LAYER
# File: app/core/security.py
# Doc Ref: Backend Architecture Part 2, Section 20
# Doc Ref: Auth Flow — JWT + Argon2 + Refresh Tokens
# Handles: JWT generation/validation, password hashing, OTP
# ============================================================

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import secrets
import string

from jose import JWTError, jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.core.config import settings

# --- Argon2 Password Hasher (Doc: use Argon2)
ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


# ============================================================
# PASSWORD HASHING
# ============================================================


def hash_password(plain_password: str) -> str:
    """Hash a plain text password using Argon2."""
    return ph.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against an Argon2 hash."""
    try:
        return ph.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed_password: str) -> bool:
    """Check if a hash needs to be upgraded."""
    return ph.check_needs_rehash(hashed_password)


# ============================================================
# JWT TOKEN GENERATION & VALIDATION
# Doc Ref: Auth Flow Section 3 — JWT Access + Refresh Tokens
# ============================================================


def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Generate a JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update(
        {
            "exp": expire,
            "iat": datetime.now(timezone.utc),
            "type": "access",
        }
    )
    return jwt.encode(
        to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


def create_refresh_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Generate a JWT refresh token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    )
    to_encode.update(
        {
            "exp": expire,
            "iat": datetime.now(timezone.utc),
            "type": "refresh",
        }
    )
    return jwt.encode(
        to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT token.
    Raises JWTError if invalid or expired.
    """
    return jwt.decode(
        token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
    )


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode access token and return payload or None."""
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def decode_refresh_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode refresh token and return payload or None."""
    try:
        payload = decode_token(token)
        if payload.get("type") != "refresh":
            return None
        return payload
    except JWTError:
        return None


# ============================================================
# OTP GENERATION
# Doc Ref: Auth Flow — OTP Login (6-digit numeric)
# Stored in Redis with TTL of OTP_EXPIRE_MINUTES
# ============================================================


def generate_otp(length: Optional[int] = None) -> str:
    """Generate a numeric OTP of configured length."""
    otp_length = length or settings.OTP_LENGTH
    return "".join(secrets.choice(string.digits) for _ in range(otp_length))


def generate_secure_token(length: int = 32) -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(length)
