# ============================================================
# WAY TERO - AUTH SERVICES
# File: app/modules/auth/services/__init__.py
# Phase: 1 - Authentication Module
# ============================================================

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
    BusinessException,
    ResourceNotFoundException,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    generate_otp,
    generate_secure_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.infrastructure.cache.redis_client import RedisCache
from app.infrastructure.push import (
    FirebaseAuthUnavailableError,
    InvalidFirebaseIdTokenError,
    verify_firebase_id_token,
)
from app.modules.auth.constants import (
    AUTH_EVENT_ACCOUNT_LOCKED,
    AUTH_EVENT_LOGIN_SUCCESS,
    AUTH_EVENT_PASSWORD_CHANGED,
    AUTH_EVENT_TOKEN_REFRESHED,
    LOGIN_LOCK_MINUTES,
    LOGIN_MAX_FAILED_ATTEMPTS,
    LOGIN_RATE_LIMIT_COUNT,
    LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    MAX_SESSIONS,
    OTP_LOCK_MINUTES,
    OTP_MAX_ATTEMPTS,
    OTP_RATE_LIMIT_COUNT,
    OTP_RATE_LIMIT_WINDOW_SECONDS,
    OTP_RESEND_COOLDOWN_SECONDS,
    PASSWORD_RESET_EXPIRE_MINUTES,
    PASSWORD_RESET_RATE_LIMIT_COUNT,
    PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
    REDIS_LOGIN_RATE_KEY,
    REDIS_OTP_ATTEMPTS_KEY,
    REDIS_OTP_LOCK_KEY,
    REDIS_OTP_RATE_KEY,
    REDIS_OTP_RESEND_KEY,
    REDIS_PASSWORD_RESET_KEY,
    REDIS_PASSWORD_RESET_OTP_KEY,
    REDIS_PASSWORD_RESET_RATE_KEY,
    REDIS_REFRESH_RATE_KEY,
    REFRESH_RATE_LIMIT_COUNT,
    REFRESH_RATE_LIMIT_WINDOW_SECONDS,
)
from app.modules.auth.models.user import User
from app.modules.auth.repositories import (
    RoleRepository,
    SessionRepository,
    UserRepository,
)
from app.modules.auth.schemas import (
    AuthUserResponse,
    LoginResponse,
    OTPSentResponse,
    RefreshResponse,
    TokenResponse,
)
from app.shared.enums.user_types import UserType, UserStatus
from app.modules.partner.repositories import PartnerRepository

from app.modules.admin.services.audit_logger import AuditLogger


def _placeholder_mobile_for_firebase(uid_or_email: str) -> str:
    """User.mobile_number is NOT NULL UNIQUE and Firebase doesn't give us
    a phone number. Synthesise a deterministic, unique placeholder from the
    Firebase uid (or email if uid is missing) so the NOT NULL UNIQUE
    constraint holds until the customer attaches a real number.

    Format: `fb_<10 hex chars>` — fits in the 20-char VARCHAR column and
    starts with letters so it's obviously not a real Indian mobile number
    (which starts with 6/7/8/9)."""
    digest = hashlib.sha256(uid_or_email.encode("utf-8")).hexdigest()[:10]
    return f"fb_{digest}"


def _split_display_name(
    display_name: str, fallback_email: str
) -> tuple[str, Optional[str]]:
    """Split 'First Last' (or 'First Middle Last') into (first, last). Falls
    back to the local-part of the email if no display name was provided."""
    if display_name:
        parts = display_name.split()
        if len(parts) == 1:
            return parts[0], None
        return parts[0], " ".join(parts[1:])
    local = fallback_email.split("@", 1)[0]
    # Strip non-letters so 'r.k.singh42' becomes 'rksingh'
    cleaned = "".join(ch for ch in local if ch.isalpha()) or "Customer"
    return cleaned.capitalize(), None


def _user_audit_id(user_id) -> Optional[int]:
    """
    Cast a User.id (typically a UUID) into the BIGINT audit_logs.user_id
    column. UUIDs cannot be stored losslessly; in that case we drop the id
    and rely on user_agent / ip_address / mobile for forensics. Staff users
    sometimes have integer ids — those pass through.
    """
    if user_id is None:
        return None
    if isinstance(user_id, int):
        return user_id
    try:
        return int(user_id) if str(user_id).isdigit() else None
    except (TypeError, ValueError):
        return None


logger = get_logger(__name__)

# Default password assigned to all new/reset partners
# Partners MUST change this on first login
DEFAULT_PARTNER_PASSWORD = "Waytero@15"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OTPService:
    """Handles OTP generation, validation, and abuse controls."""

    def __init__(self, cache: RedisCache):
        self.cache = cache

    def _scope_mobile(self, mobile: str, purpose: str) -> str:
        return f"{purpose}:{mobile}"

    async def _enforce_request_rate_limit(self, mobile: str) -> None:
        key = REDIS_OTP_RATE_KEY.format(mobile=mobile)
        attempts = await self.cache.increment(key, OTP_RATE_LIMIT_WINDOW_SECONDS)
        if attempts > OTP_RATE_LIMIT_COUNT:
            retry_after = max(await self.cache.ttl(key), 0)
            raise BusinessException(
                message=(
                    "OTP request limit exceeded. Please wait "
                    f"{retry_after} seconds before trying again."
                ),
                code="OTP_RATE_LIMIT",
            )

    async def send_otp(self, mobile: str, purpose: str = "LOGIN") -> OTPSentResponse:
        await self._enforce_request_rate_limit(mobile)

        scoped_mobile = self._scope_mobile(mobile, purpose)
        resend_key = REDIS_OTP_RESEND_KEY.format(mobile=scoped_mobile)
        if await self.cache.exists(resend_key):
            raise BusinessException(
                message=(
                    "OTP already sent. Please wait "
                    f"{OTP_RESEND_COOLDOWN_SECONDS} seconds before requesting again."
                ),
                code="OTP_COOLDOWN",
            )

        lock_key = REDIS_OTP_LOCK_KEY.format(mobile=scoped_mobile)
        if await self.cache.exists(lock_key):
            raise AuthenticationException(
                message=(
                    "Too many failed attempts. Please try again after "
                    f"{OTP_LOCK_MINUTES} minutes."
                ),
                code="OTP_LOCKED",
            )

        otp = generate_otp()
        await self.cache.set_otp(scoped_mobile, _hash_token(otp))
        await self.cache.redis.setex(resend_key, OTP_RESEND_COOLDOWN_SECONDS, "1")
        await self.cache.redis.delete(
            REDIS_OTP_ATTEMPTS_KEY.format(mobile=scoped_mobile)
        )

        if not settings.is_production:
            # Never log the OTP value in production (H1) — dev only helper.
            logger.info(
                "otp_generated",
                mobile_number=mobile,
                purpose=purpose,
                otp=otp,
                note="DEV ONLY - replace with SMS/WhatsApp delivery in production",
            )

        return OTPSentResponse(
            mobile_number=mobile,
            expires_in_seconds=settings.OTP_EXPIRE_MINUTES * 60,
            # Expose OTP in response only in non-production (no SMS gateway configured)
            dev_otp=otp if not settings.is_production else None,
        )

    async def verify_otp(self, mobile: str, otp: str, purpose: str = "LOGIN") -> bool:
        scoped_mobile = self._scope_mobile(mobile, purpose)
        lock_key = REDIS_OTP_LOCK_KEY.format(mobile=scoped_mobile)
        if await self.cache.exists(lock_key):
            raise AuthenticationException(
                message=f"Too many failed attempts. Locked for {OTP_LOCK_MINUTES} minutes.",
                code="OTP_LOCKED",
            )

        stored_hash = await self.cache.get_otp(scoped_mobile)
        if not stored_hash:
            raise AuthenticationException(
                message="OTP expired or not found. Please request a new OTP.",
                code="OTP_EXPIRED",
            )

        if stored_hash != _hash_token(otp):
            attempts_key = REDIS_OTP_ATTEMPTS_KEY.format(mobile=scoped_mobile)
            attempts = await self.cache.redis.incr(attempts_key)
            await self.cache.redis.expire(attempts_key, OTP_LOCK_MINUTES * 60)

            if int(attempts) >= OTP_MAX_ATTEMPTS:
                await self.cache.redis.setex(lock_key, OTP_LOCK_MINUTES * 60, "1")
                await self.cache.delete_otp(scoped_mobile)
                logger.warning("otp_locked", mobile_number=mobile, purpose=purpose)
                raise AuthenticationException(
                    message=(
                        "Too many incorrect attempts. "
                        f"Locked for {OTP_LOCK_MINUTES} minutes."
                    ),
                    code="OTP_LOCKED",
                )

            remaining = OTP_MAX_ATTEMPTS - int(attempts)
            raise AuthenticationException(
                message=f"Incorrect OTP. {remaining} attempt(s) remaining.",
                code="OTP_INVALID",
            )

        await self.cache.delete_otp(scoped_mobile)
        await self.cache.redis.delete(
            REDIS_OTP_ATTEMPTS_KEY.format(mobile=scoped_mobile)
        )
        return True


class AuthService:
    """Authentication orchestration layer."""

    def __init__(self, db: AsyncSession, cache: RedisCache):
        self.db = db
        self.cache = cache
        self.user_repo = UserRepository(db)
        self.session_repo = SessionRepository(db)
        self.role_repo = RoleRepository(db)
        self.otp_service = OTPService(cache)

    async def _ensure_customer_profile(
        self, user_id, first_name: str = "Customer", last_name: Optional[str] = None
    ) -> None:
        """Create a `customers` row + wallet for a brand-new self-service
        customer user. Called right after `users` is created so every
        downstream customer endpoint (GET/PATCH /customers/me, etc.) has a
        row to read. Idempotent — does nothing if the profile already exists.
        Inline SQL to avoid a circular dependency with the customer module.
        """
        import uuid as _uuid
        from sqlalchemy import text as _text

        existing = (
            await self.db.execute(
                _text("SELECT id FROM customers WHERE user_id = :uid"),
                {"uid": str(user_id)},
            )
        ).first()
        if existing:
            return
        # Generate unique customer_code
        import random as _rand

        code = f"CUS-{_rand.randint(100000, 999999)}"
        for _ in range(5):
            clash = (
                await self.db.execute(
                    _text("SELECT id FROM customers WHERE customer_code = :c"),
                    {"c": code},
                )
            ).first()
            if not clash:
                break
            code = f"CUS-{_rand.randint(100000, 999999)}"
        await self.db.execute(
            _text(
                """
                INSERT INTO customers
                    (uuid, user_id, customer_code, first_name, last_name, is_active,
                     created_at, updated_at)
                VALUES (:uuid, :uid, :code, :fn, :ln, TRUE, NOW(), NOW())
            """
            ),
            {
                "uuid": str(_uuid.uuid4()),
                "uid": str(user_id),
                "code": code,
                "fn": first_name,
                "ln": last_name,
            },
        )
        await self.db.execute(
            _text(
                """
                INSERT INTO customer_wallets
                    (customer_id, available_balance, hold_balance,
                     wallet_status, created_at, updated_at)
                SELECT id, 0, 0, 'ACTIVE', NOW(), NOW()
                  FROM customers WHERE user_id = :uid
            """
            ),
            {"uid": str(user_id)},
        )
        await self.db.commit()
        logger.info(
            "customer_profile_created user_id=%s code=%s",
            str(user_id),
            code,
        )

    async def customer_otp_login(
        self,
        mobile: str,
        otp: str,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        await self._enforce_login_rate_limit(f"customer:{mobile}")
        await self.otp_service.verify_otp(mobile, otp)

        user = await self.user_repo.get_by_mobile_and_type(mobile, UserType.CUSTOMER)
        if not user:
            user = await self.user_repo.create(
                mobile_number=mobile,
                user_type=UserType.CUSTOMER,
                status=UserStatus.ACTIVE,
                is_mobile_verified=True,
                first_name="Customer",
                last_name=None,
            )
            await self.role_repo.ensure_default_role_for_user(user)
            await self._ensure_customer_profile(user.id, first_name="Customer")
            logger.info("customer_registered", user_id=str(user.id), mobile=mobile)
            # ── Email: welcome new customer (only when they have an email) ──
            try:
                from app.infrastructure.email import send_event_email

                await send_event_email(
                    self.db,
                    event_type="customer_registered",
                    to_email=user.email or "",
                    to_name=" ".join(
                        filter(
                            None,
                            [str(user.first_name or ""), str(user.last_name or "")],
                        )
                    )
                    or None,
                    context={
                        "name": user.first_name or "there",
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
                    related_id=str(user.id),
                )
            except Exception:  # pragma: no cover — email must never break auth
                pass
        else:
            await self.user_repo.mark_mobile_verified(user.id)
            user.is_mobile_verified = True

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        return await self._create_session_and_tokens(
            user, device_name, device_os, ip_address
        )

    # ------------------------------------------------------------------
    # Firebase / Google social sign-in
    # Doc Ref: Customer web Google sign-in flow
    # ------------------------------------------------------------------
    async def customer_firebase_login(
        self,
        id_token: str,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        """Verify a Firebase ID token, find-or-create the local customer user,
        and mint WayTero JWTs.

        The customer row requires a unique mobile_number; Google doesn't give
        us a phone, so for a brand-new account we seed it with a unique
        placeholder keyed on the Firebase uid (`fb_<uid>` truncated to fit
        the column). The customer can attach a real number later via the
        /auth/me/attach-mobile endpoint (or the booking flow can prompt for it).

        Existing accounts are matched by email (case-insensitive). If the email
        is claimed by a different user_type, we raise — prevents cross-portal
        collisions.
        """
        await self._enforce_login_rate_limit("customer:firebase")

        try:
            claims = await verify_firebase_id_token(self.db, id_token)
        except FirebaseAuthUnavailableError as exc:
            # Server-side Firebase not configured — surface as 503 so the
            # web client can fall back to OTP without a generic 500.
            raise BusinessException(
                message="Google sign-in is not available right now. Please use mobile OTP.",
                code="FIREBASE_UNAVAILABLE",
            ) from exc
        except InvalidFirebaseIdTokenError as exc:
            raise AuthenticationException(
                message="Google sign-in failed. Please try again.",
                code="INVALID_FIREBASE_TOKEN",
            ) from exc

        email = (claims.get("email") or "").strip().lower() or None
        if not email:
            # Firebase phone-only auth or weird claim shape — we won't create
            # an account without an email because the web flow is Google-only.
            raise AuthenticationException(
                message="Google account did not return an email address.",
                code="FIREBASE_NO_EMAIL",
            )

        display_name = (claims.get("name") or "").strip()
        first_name, last_name = _split_display_name(display_name, fallback_email=email)
        picture = claims.get("picture")

        user = await self.user_repo.get_by_email_and_type(email, UserType.CUSTOMER)

        if user is None:
            # New customer — synthesise a unique mobile_number placeholder so
            # the NOT NULL UNIQUE constraint is satisfied. Customer attaches
            # a real one before their first booking.
            placeholder_mobile = _placeholder_mobile_for_firebase(
                claims.get("uid") or email
            )
            # Avoid colliding with a row that already owns that placeholder
            # (extremely unlikely, but cheap to check).
            if await self.user_repo.exists_by_mobile(placeholder_mobile):
                placeholder_mobile = (
                    f"{placeholder_mobile[:18]}{int(time.time()*1000) % 1000:03d}"
                )
            user = await self.user_repo.create(
                mobile_number=placeholder_mobile,
                email=email,
                user_type=UserType.CUSTOMER,
                status=UserStatus.ACTIVE,
                first_name=first_name,
                last_name=last_name,
                is_email_verified=True,
            )
            await self.role_repo.ensure_default_role_for_user(user)
            await self._ensure_customer_profile(
                user.id, first_name=first_name, last_name=last_name
            )
            logger.info(
                "customer_registered_firebase user_id=%s email=%s",
                str(user.id),
                email,
            )
            # ── Email: welcome new Google/Firebase customer ──
            try:
                from app.infrastructure.email import send_event_email

                await send_event_email(
                    self.db,
                    event_type="customer_registered",
                    to_email=email,
                    to_name=display_name or first_name or None,
                    context={
                        "name": first_name or email.split("@")[0],
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
                    related_id=str(user.id),
                )
            except Exception:  # pragma: no cover — email must never break auth
                pass
        else:
            # Existing customer — mark email verified (no-op if already True),
            # refresh name/picture if the IdP has fresher values.
            await self.user_repo.mark_email_verified(user.id)
            user.is_email_verified = True
            await self.user_repo.update_profile(
                user.id,
                first_name=first_name or user.first_name,
                last_name=last_name if last_name is not None else user.last_name,
                profile_image_url=picture,
            )

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        return await self._create_session_and_tokens(
            user, device_name, device_os, ip_address
        )

    async def driver_otp_login(
        self,
        mobile: str,
        otp: str,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        await self._enforce_login_rate_limit(f"driver:{mobile}")
        await self.otp_service.verify_otp(mobile, otp)

        user = await self.user_repo.get_by_mobile_and_type(mobile, UserType.DRIVER)
        if not user:
            raise AuthenticationException(
                message="Driver account not found. Please contact support.",
                code="DRIVER_NOT_FOUND",
            )

        await self.user_repo.mark_mobile_verified(user.id)
        user.is_mobile_verified = True
        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        return await self._create_session_and_tokens(
            user, device_name, device_os, ip_address
        )

    async def partner_password_login(
        self,
        password: str,
        mobile: Optional[str] = None,
        email: Optional[str] = None,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        identifier = mobile or email or ""
        await self._enforce_login_rate_limit(f"partner-password:{identifier}")
        # Look up by mobile first, fall back to email
        user = None
        if mobile:
            user = await self.user_repo.get_by_mobile_and_type(mobile, UserType.PARTNER)
        if not user and email:
            user = await self.user_repo.get_by_email_and_type(email, UserType.PARTNER)
        if not user:
            raise AuthenticationException(
                message="Invalid credentials. Check your mobile/email and password.",
                code="INVALID_CREDENTIALS",
            )

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)

        if not user.password_hash:
            raise AuthenticationException(
                message="Password login not configured. Use OTP login.",
                code="PASSWORD_NOT_SET",
            )

        if not verify_password(password, user.password_hash):
            new_count = await self.user_repo.increment_failed_attempts(user.id)
            if new_count >= LOGIN_MAX_FAILED_ATTEMPTS:
                locked_until = datetime.now(timezone.utc) + timedelta(
                    minutes=LOGIN_LOCK_MINUTES
                )
                await self.user_repo.lock_account(user.id, locked_until)
                logger.warning(
                    AUTH_EVENT_ACCOUNT_LOCKED, user_id=str(user.id), mobile=mobile
                )
                raise AuthenticationException(
                    message=(
                        "Account locked for "
                        f"{LOGIN_LOCK_MINUTES} minutes due to too many failed attempts."
                    ),
                    code="ACCOUNT_LOCKED",
                )
            raise AuthenticationException(
                message="Invalid mobile number or password.",
                code="INVALID_CREDENTIALS",
            )

        if needs_rehash(user.password_hash):
            await self.user_repo.update_password(user.id, hash_password(password))

        return await self._create_session_and_tokens(
            user, device_name, device_os, ip_address
        )

    async def partner_change_first_password(
        self,
        user_id: UUID,
        new_password: str,
    ) -> None:
        """Partner sets a new password replacing the default. Clears force_password_change flag."""
        if new_password == DEFAULT_PARTNER_PASSWORD:
            raise BusinessException(
                message="You cannot use the default password. Please choose a new password.",
                code="PASSWORD_SAME_AS_DEFAULT",
            )
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ResourceNotFoundException("User", user_id)
        await self.user_repo.update_password(user_id, hash_password(new_password))
        await self.user_repo.clear_force_password_change(user_id)
        await self.session_repo.deactivate_all_user_sessions(user_id)
        logger.info("partner_first_password_changed", user_id=str(user_id))

    async def partner_otp_login(
        self,
        mobile: str,
        otp: str,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        await self._enforce_login_rate_limit(f"partner-otp:{mobile}")
        await self.otp_service.verify_otp(mobile, otp)

        user = await self.user_repo.get_by_mobile_and_type(mobile, UserType.PARTNER)
        if not user:
            raise AuthenticationException(
                message="Partner account not found. Please contact support.",
                code="PARTNER_NOT_FOUND",
            )

        await self.user_repo.mark_mobile_verified(user.id)
        user.is_mobile_verified = True
        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        return await self._create_session_and_tokens(
            user, device_name, device_os, ip_address
        )

    async def _resolve_admin_user(self, username: str) -> Optional["User"]:
        """Resolve an admin (ADMIN / SUPER_ADMIN) user by email or mobile.

        Accepts an email address or a 10-digit Indian mobile number as the
        username. Returns None when no admin matches, so callers can fail
        with a generic "invalid credentials" message.
        """
        if "@" in username:
            for utype in (UserType.ADMIN, UserType.SUPER_ADMIN):
                user = await self.user_repo.get_by_email_and_type(
                    username.lower(), utype
                )
                if user:
                    return user
        else:
            for utype in (UserType.ADMIN, UserType.SUPER_ADMIN):
                user = await self.user_repo.get_by_mobile_and_type(username, utype)
                if user:
                    return user
        return None

    async def admin_password_login(
        self,
        username: str,
        password: str,
        device_name: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        """Admin login with email/mobile + password (no OTP).

        Lookup: tries email first if "@" in username, else mobile — scoped
        to ADMIN / SUPER_ADMIN user types only.
        """
        await self._enforce_login_rate_limit(f"admin-password:{username}")

        user = await self._resolve_admin_user(username)
        if not user:
            raise AuthenticationException(
                message="Invalid credentials. Check your email/mobile and password.",
                code="INVALID_CREDENTIALS",
            )

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)

        if not user.password_hash:
            raise AuthenticationException(
                message="Password login not configured. Use OTP login.",
                code="PASSWORD_NOT_SET",
            )

        if not verify_password(password, user.password_hash):
            new_count = await self.user_repo.increment_failed_attempts(user.id)
            if new_count >= LOGIN_MAX_FAILED_ATTEMPTS:
                locked_until = datetime.now(timezone.utc) + timedelta(
                    minutes=LOGIN_LOCK_MINUTES
                )
                await self.user_repo.lock_account(user.id, locked_until)
                logger.warning(
                    AUTH_EVENT_ACCOUNT_LOCKED, user_id=str(user.id), mobile=username
                )
                raise AuthenticationException(
                    message=(
                        "Account locked for "
                        f"{LOGIN_LOCK_MINUTES} minutes due to too many failed attempts."
                    ),
                    code="ACCOUNT_LOCKED",
                )
            raise AuthenticationException(
                message="Invalid email/mobile or password.",
                code="INVALID_CREDENTIALS",
            )

        if needs_rehash(user.password_hash):
            await self.user_repo.update_password(user.id, hash_password(password))

        return await self._create_session_and_tokens(
            user, device_name, None, ip_address
        )

    async def admin_otp_login(
        self,
        mobile: str,
        otp: str,
        device_name: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        """Admin login with mobile + OTP (no password)."""
        await self._enforce_login_rate_limit(f"admin-otp:{mobile}")
        await self.otp_service.verify_otp(mobile, otp)

        user = await self.user_repo.get_by_mobile_and_type(mobile, UserType.ADMIN)
        if not user:
            user = await self.user_repo.get_by_mobile_and_type(
                mobile, UserType.SUPER_ADMIN
            )
        if not user:
            raise AuthenticationException(
                message="Admin account not found. Please contact support.",
                code="ADMIN_NOT_FOUND",
            )

        await self.user_repo.mark_mobile_verified(user.id)
        user.is_mobile_verified = True
        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        return await self._create_session_and_tokens(
            user, device_name, None, ip_address
        )

    async def refresh_access_token(self, refresh_token: str) -> RefreshResponse:
        await self._enforce_refresh_rate_limit(refresh_token)
        payload = decode_refresh_token(refresh_token)
        if not payload:
            raise AuthenticationException(
                message="Invalid or expired refresh token.",
                code="INVALID_REFRESH_TOKEN",
            )

        token_hash = _hash_token(refresh_token)
        session = await self.session_repo.get_by_refresh_token_hash(token_hash)
        if not session or not session.is_active or session.is_expired:
            raise AuthenticationException(
                message="Session expired. Please login again.",
                code="SESSION_EXPIRED",
            )

        user = await self.user_repo.get_by_id(UUID(payload["sub"]))
        if not user:
            raise AuthenticationException(
                message="User not found.", code="USER_NOT_FOUND"
            )

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        roles, permissions = await self._get_access_context(user.id)

        await self.session_repo.update_last_used(session.id)

        # Resolve integer partner_id for PARTNER users so the token carries it
        # (used by vehicle/booking endpoints instead of trusting query params).
        partner_int_id = await self._resolve_partner_int_id(user)

        access_payload = {
            "sub": str(user.id),
            "role": roles[0] if roles else user.user_type.value,
            "roles": roles,
            "permissions": permissions,
            "user_type": user.user_type.value,
            "session_id": str(session.id),
        }
        if partner_int_id is not None:
            access_payload["partner_id"] = partner_int_id

        new_access_token = create_access_token(access_payload)

        logger.info(
            AUTH_EVENT_TOKEN_REFRESHED, user_id=str(user.id), session_id=str(session.id)
        )

        # Platform audit log (audit_logs) — token refresh is a low-signal
        # event but high-volume; if this proves noisy we can downgrade.
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_TOKEN_REFRESHED,
            user_id=_user_audit_id(user.id),
        )

        return RefreshResponse(
            access_token=new_access_token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            permissions=permissions,
            roles=roles,
        )

    async def logout(
        self, user_id: UUID, refresh_token: Optional[str], logout_all: bool
    ) -> None:
        if logout_all:
            await self.session_repo.deactivate_all_user_sessions(user_id)
            logger.info("logout_all_devices", user_id=str(user_id))
            return

        if refresh_token:
            session = await self.session_repo.get_by_refresh_token_hash(
                _hash_token(refresh_token)
            )
            if session and session.user_id == user_id:
                await self.session_repo.deactivate_session(session.id)

        logger.info("logout", user_id=str(user_id))

    async def change_password(
        self, user_id: UUID, current_password: str, new_password: str
    ) -> None:
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ResourceNotFoundException("User", user_id)

        if not user.password_hash or not verify_password(
            current_password, user.password_hash
        ):
            raise AuthenticationException(
                message="Current password is incorrect.",
                code="WRONG_PASSWORD",
            )

        await self.user_repo.update_password(user_id, hash_password(new_password))
        await self.session_repo.deactivate_all_user_sessions(user_id)
        logger.info(AUTH_EVENT_PASSWORD_CHANGED, user_id=str(user_id))

        # Platform audit log (audit_logs)
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_PASSWORD_CHANGED,
            user_id=_user_audit_id(user_id),
        )

    async def _is_smtp_configured(self) -> bool:
        """Check whether SMTP is configured in system_configurations or
        api_integrations.  Used to decide whether password-reset OTP can
        be delivered via email."""
        from sqlalchemy import text as _text

        try:
            row = (
                await self.db.execute(
                    _text(
                        "SELECT 1 FROM api_integrations "
                        "WHERE service_type = 'SMTP' AND is_active = TRUE "
                        "AND configuration IS NOT NULL "
                        "AND configuration::text NOT IN ('{}', 'null') LIMIT 1"
                    )
                )
            ).scalar()
            if row:
                return True
        except Exception:
            pass
        try:
            row = (
                await self.db.execute(
                    _text(
                        "SELECT 1 FROM system_configurations "
                        "WHERE config_key = 'EMAIL_ENABLED' "
                        "AND LOWER(config_value) = 'true' LIMIT 1"
                    )
                )
            ).scalar()
            return bool(row)
        except Exception:
            return False

    async def _is_sms_configured(self) -> bool:
        """Check whether an SMS provider (MSG91) is active in api_integrations."""
        from sqlalchemy import text as _text

        try:
            row = (
                await self.db.execute(
                    _text(
                        "SELECT 1 FROM api_integrations "
                        "WHERE service_type IN ('MSG91') AND is_active = TRUE "
                        "AND configuration IS NOT NULL "
                        "AND configuration::text NOT IN ('{}', 'null') LIMIT 1"
                    )
                )
            ).scalar()
            return bool(row)
        except Exception:
            return False

    async def _send_password_reset_otp_email(
        self, email: str, name: str, otp: str
    ) -> None:
        """Send a password-reset OTP via the email engine."""
        from app.infrastructure.email import send_event_email

        await send_event_email(
            self.db,
            event_type="password_reset_otp",
            to_email=email,
            to_name=name or None,
            context={
                "name": name or "there",
                "otp": otp,
                "message": (
                    f"Your WayTero password reset OTP is {otp}. "
                    "It expires in 15 minutes. Do not share this code."
                ),
                "body": [
                    f"Use OTP <b>{otp}</b> to reset your password.",
                    "This OTP expires in 15 minutes.",
                    "If you did not request a password reset, please ignore this email.",
                ],
            },
            related_type="PASSWORD_RESET",
            related_id=email,
        )

    async def initiate_password_reset(self, identifier: str) -> dict:
        """Initiate OTP-based password reset.

        Resolution order:
          1. If *identifier* looks like an email → find user by email.
          2. Otherwise treat as a 10-digit mobile → find user by mobile.
          3. Determine delivery channel:
             a. Email OTP if the user has an email AND SMTP is configured.
             b. SMS OTP if the user has a mobile AND MSG91 is configured.
             c. Neither → raise BusinessException (password reset
                unavailable).
          4. Generate OTP, store in Redis keyed by a secure reset token.
          5. Return ``{reset_token, channel, ...}`` — the frontend uses
             reset_token in the follow-up /reset-password call.

        Doc Ref: Auth Flow — OTP-based password reset
        """
        rate_key = REDIS_PASSWORD_RESET_RATE_KEY.format(email=identifier.lower())
        attempts = await self.cache.increment(
            rate_key,
            PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS,
        )
        if attempts > PASSWORD_RESET_RATE_LIMIT_COUNT:
            raise BusinessException(
                message="Password reset request limit exceeded. Please try again later.",
                code="PASSWORD_RESET_RATE_LIMIT",
            )

        # ── Resolve user ──
        user = None
        is_email = "@" in identifier
        if is_email:
            user = await self.user_repo.get_by_email(identifier.lower())
        else:
            # Try as mobile — scan common user types
            for utype in (
                UserType.CUSTOMER,
                UserType.PARTNER,
                UserType.DRIVER,
                UserType.ADMIN,
                UserType.SUPER_ADMIN,
            ):
                user = await self.user_repo.get_by_mobile_and_type(identifier, utype)
                if user:
                    break

        if not user:
            # Deliberately vague — never reveal whether the account exists
            logger.info(
                "password_reset_ignored",
                identifier=identifier,
                reason="user_not_found",
            )
            return {
                "message": "If an account exists, an OTP has been sent.",
                "channel": None,
            }

        # ── Determine delivery channel ──
        email_configured = await self._is_smtp_configured()
        sms_configured = await self._is_sms_configured()

        channel = None
        otp = generate_otp()

        if user.email and email_configured:
            channel = "email"
        elif user.mobile_number and sms_configured:
            channel = "sms"
        else:
            raise BusinessException(
                message=(
                    "Password reset is not available. Neither email nor SMS "
                    "is configured for this account. Please contact support."
                ),
                code="PASSWORD_RESET_UNAVAILABLE",
            )

        # ── Store OTP keyed by reset token hash ──
        reset_token = generate_secure_token()
        token_hash = _hash_token(reset_token)
        reset_key = REDIS_PASSWORD_RESET_KEY.format(token_hash=token_hash)
        otp_key = REDIS_PASSWORD_RESET_OTP_KEY.format(token_hash=token_hash)

        await self.cache.set(
            reset_key,
            {"user_id": str(user.id), "channel": channel},
            ttl_seconds=PASSWORD_RESET_EXPIRE_MINUTES * 60,
        )
        await self.cache.set(
            otp_key,
            _hash_token(otp),
            ttl_seconds=PASSWORD_RESET_EXPIRE_MINUTES * 60,
        )

        # ── Deliver OTP ──
        display_name = user.full_name or user.first_name or ""
        if channel == "email":
            try:
                await self._send_password_reset_otp_email(user.email, display_name, otp)
            except Exception:
                logger.warning(
                    "password_reset_email_failed",
                    user_id=str(user.id),
                    email=user.email,
                    exc_info=True,
                )
                raise BusinessException(
                    message="Failed to send OTP email. Please try again later.",
                    code="PASSWORD_RESET_EMAIL_FAILED",
                )
        else:
            # SMS OTP — reuse existing OTPService for delivery
            if not settings.is_production:
                # Never log the OTP value in production (H1) — dev only helper.
                logger.info(
                    "password_reset_sms_otp",
                    user_id=str(user.id),
                    mobile=user.mobile_number,
                    otp=otp,
                    note="DEV ONLY - replace with SMS delivery in production",
                )

        masked = ""
        if channel == "email" and user.email:
            parts = user.email.split("@")
            masked = parts[0][:2] + "***@" + parts[1]
        elif channel == "sms" and user.mobile_number:
            masked = user.mobile_number[:2] + "****" + user.mobile_number[-2:]

        logger.info(
            "password_reset_initiated",
            user_id=str(user.id),
            channel=channel,
            identifier=identifier,
        )

        return {
            "message": f"OTP sent to {masked}",
            "channel": channel,
            "reset_token": reset_token,
            "masked_identifier": masked,
            # Dev-only: expose OTP in non-production
            **({"dev_otp": otp} if not settings.is_production else {}),
        }

    async def reset_password(
        self, reset_token: str, otp: str, new_password: str
    ) -> None:
        """Verify OTP and reset the user's password.

        Doc Ref: Auth Flow — OTP-based password reset
        """
        token_hash = _hash_token(reset_token)
        reset_key = REDIS_PASSWORD_RESET_KEY.format(token_hash=token_hash)
        otp_key = REDIS_PASSWORD_RESET_OTP_KEY.format(token_hash=token_hash)

        payload = await self.cache.get(reset_key)
        stored_otp_hash = await self.cache.get(otp_key)

        if not payload or "user_id" not in payload:
            raise AuthenticationException(
                message="Invalid or expired password reset token.",
                code="INVALID_RESET_TOKEN",
            )

        if not stored_otp_hash:
            raise AuthenticationException(
                message="OTP expired or not found. Please request a new one.",
                code="OTP_EXPIRED",
            )

        if stored_otp_hash != _hash_token(otp):
            raise AuthenticationException(
                message="Incorrect OTP. Please try again.",
                code="OTP_INVALID",
            )

        user_id = UUID(payload["user_id"])
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ResourceNotFoundException("User", user_id)

        await self.user_repo.update_password(user_id, hash_password(new_password))
        await self.session_repo.deactivate_all_user_sessions(user_id)
        await self.cache.delete(reset_key)
        await self.cache.delete(otp_key)
        logger.info("password_reset_completed", user_id=str(user_id))

    async def list_sessions(self, user_id: UUID) -> list:
        return await self.session_repo.get_active_sessions(user_id)

    async def revoke_session(self, user_id: UUID, session_id: UUID) -> None:
        session = await self.session_repo.get_by_id(session_id)
        if not session or session.user_id != user_id:
            raise ResourceNotFoundException("Session", session_id)
        await self.session_repo.deactivate_session(session_id)

    async def get_profile(self, user_id: UUID) -> AuthUserResponse:
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ResourceNotFoundException("User", user_id)
        await self.role_repo.ensure_default_role_for_user(user)
        roles, permissions = await self._get_access_context(user.id)
        return self._build_user_response(user, roles, permissions)

    async def _enforce_login_rate_limit(self, scope: str) -> None:
        rate_key = REDIS_LOGIN_RATE_KEY.format(scope=scope)
        attempts = await self.cache.increment(rate_key, LOGIN_RATE_LIMIT_WINDOW_SECONDS)
        if attempts > LOGIN_RATE_LIMIT_COUNT:
            raise AuthenticationException(
                message="Too many login attempts. Please wait a minute and try again.",
                code="LOGIN_RATE_LIMIT",
            )

    async def _enforce_refresh_rate_limit(self, refresh_token: str) -> None:
        rate_key = REDIS_REFRESH_RATE_KEY.format(scope=_hash_token(refresh_token)[:16])
        attempts = await self.cache.increment(
            rate_key, REFRESH_RATE_LIMIT_WINDOW_SECONDS
        )
        if attempts > REFRESH_RATE_LIMIT_COUNT:
            raise AuthenticationException(
                message="Refresh token rate limit exceeded. Please login again shortly.",
                code="REFRESH_RATE_LIMIT",
            )

    async def _check_account_active(self, user: User) -> None:
        if user.is_locked:
            raise AuthenticationException(
                message="Account is temporarily locked. Please try again later.",
                code="ACCOUNT_LOCKED",
            )
        if not user.is_active:
            raise AuthenticationException(
                message="Your account is inactive or suspended. Contact support.",
                code="ACCOUNT_INACTIVE",
            )

    async def _get_access_context(self, user_id: UUID) -> tuple[list[str], list[str]]:
        roles = await self.role_repo.list_roles_for_user(user_id)
        permissions = await self.role_repo.list_permissions_for_user(user_id)
        return roles, permissions

    def _build_user_response(
        self,
        user: User,
        roles: list[str],
        permissions: list[str],
        partner_id: Optional[int] = None,
    ) -> AuthUserResponse:
        return AuthUserResponse(
            id=user.id,
            mobile=user.mobile_number,
            email=user.email,
            full_name=user.full_name,
            user_type=user.user_type.value,
            status=user.status.value,
            roles=roles,
            permissions=permissions,
            is_mobile_verified=user.is_mobile_verified,
            is_email_verified=user.is_email_verified,
            must_change_password=getattr(user, "force_password_change", False),
            partner_id=partner_id,
        )

    async def _create_session_and_tokens(
        self,
        user: User,
        device_name: Optional[str],
        device_os: Optional[str],
        ip_address: Optional[str],
    ) -> LoginResponse:
        roles, permissions = await self._get_access_context(user.id)

        max_sessions = MAX_SESSIONS.get(user.user_type.value, 3)
        active_count = await self.session_repo.count_active_sessions(user.id)
        if active_count >= max_sessions:
            await self.session_repo.deactivate_oldest_session(user.id)

        provisional_hash = _hash_token(generate_secure_token())
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
        )
        session = await self.session_repo.create(
            user_id=user.id,
            refresh_token_hash=provisional_hash,
            device_name=device_name,
            # device_os is not a column in UserSession model — omitted
            ip_address=ip_address,
            expires_at=expires_at,
        )

        access_payload = {
            "sub": str(user.id),
            "role": roles[0] if roles else user.user_type.value,
            "roles": roles,
            "permissions": permissions,
            "user_type": user.user_type.value,
            "session_id": str(session.id),
        }
        access_token = create_access_token(access_payload)
        refresh_token = create_refresh_token(access_payload)
        session.refresh_token_hash = _hash_token(refresh_token)
        await self.db.flush()

        await self.user_repo.update_last_login(user.id)
        logger.info(
            AUTH_EVENT_LOGIN_SUCCESS,
            user_id=str(user.id),
            user_type=user.user_type.value,
            session_id=str(session.id),
        )

        # Platform audit log (audit_logs)
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_LOGIN_SUCCESS,
            user_id=_user_audit_id(user.id),
            ip_address=ip_address,
        )

        # Resolve integer partner_id for PARTNER user type (needed by vehicle/booking endpoints)
        partner_int_id = await self._resolve_partner_int_id(user)

        access_payload = {
            "sub": str(user.id),
            "role": roles[0] if roles else user.user_type.value,
            "roles": roles,
            "permissions": permissions,
            "user_type": user.user_type.value,
            "session_id": str(session.id),
        }
        if partner_int_id is not None:
            access_payload["partner_id"] = partner_int_id

        access_token = create_access_token(access_payload)
        refresh_token = create_refresh_token(access_payload)
        session.refresh_token_hash = _hash_token(refresh_token)
        await self.db.flush()

        await self.user_repo.update_last_login(user.id)
        logger.info(
            AUTH_EVENT_LOGIN_SUCCESS,
            user_id=str(user.id),
            user_type=user.user_type.value,
            session_id=str(session.id),
        )

        # Platform audit log (audit_logs)
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_LOGIN_SUCCESS,
            user_id=_user_audit_id(user.id),
            ip_address=ip_address,
        )

        return LoginResponse(
            user=self._build_user_response(
                user, roles, permissions, partner_id=partner_int_id
            ),
            tokens=TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            ),
            session_id=session.id,
        )

    async def _resolve_partner_int_id(self, user: User) -> Optional[int]:
        """Resolve the integer partner_id for a PARTNER user, else None.

        Doc Ref: Security Hardening — S6: partner_id from JWT, not query params.
        """
        if user.user_type.value != UserType.PARTNER.value:
            return None
        try:
            partner_repo = PartnerRepository(self.db)
            partner_obj = await partner_repo.get_by_user_id(user.id)
            if partner_obj:
                return partner_obj.id
        except Exception:
            pass  # Non-fatal — endpoints can still resolve it via DB when needed
        return None
