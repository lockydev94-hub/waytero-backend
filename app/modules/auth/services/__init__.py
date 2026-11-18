# ============================================================
# WAY TERO - AUTH SERVICES
# File: app/modules/auth/services/__init__.py
# Phase: 1 - Authentication Module
# ============================================================

import hashlib
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
    BusinessException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ValidationException,
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
from app.core.timezone import get_platform_tz, get_platform_tz_name
from app.infrastructure.cache.redis_client import RedisCache
from app.infrastructure.push import (
    FirebaseAuthUnavailableError,
    InvalidFirebaseIdTokenError,
    verify_firebase_id_token,
)
from app.modules.auth.constants import (
    AUTH_EVENT_ACCOUNT_LOCKED,
    AUTH_EVENT_LOGIN_SUCCESS,
    AUTH_EVENT_MPIN_CHANGED,
    AUTH_EVENT_MPIN_FAILED,
    AUTH_EVENT_MPIN_LOCKED,
    AUTH_EVENT_MPIN_REMOVED,
    AUTH_EVENT_MPIN_SET,
    AUTH_EVENT_MPIN_VERIFIED,
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
    # Admin session policy (migration 0084)
    CONFIG_ADMIN_DAILY_LOGOUT_HOUR,
    CONFIG_ADMIN_IDLE_DEFAULT_MINUTES,
    CONFIG_ADMIN_MPIN_LOCKOUT_MINUTES,
    CONFIG_ADMIN_MPIN_MAX_ATTEMPTS,
    CONFIG_ADMIN_MPIN_MIN_LENGTH,
    DAILY_LOGOUT_HOUR_DEFAULT,
    DAILY_LOGOUT_HOUR_MAX,
    DAILY_LOGOUT_HOUR_MIN,
    IDLE_LOCK_MINUTES_DEFAULT,
    IDLE_LOCK_MINUTES_MAX,
    IDLE_LOCK_MINUTES_MIN,
    MPIN_LOCKOUT_MINUTES_DEFAULT,
    MPIN_MAX_ATTEMPTS_DEFAULT,
    MPIN_MIN_LENGTH_DEFAULT,
    STAFF_USER_TYPES,
    DASHBOARD_USER_TYPES,
    DASHBOARD_SESSION_HOURS,
    HEARTBEAT_MAX_SECONDS,
    OTP_PURPOSE_MPIN_SET,
)
from app.modules.auth.models.user import User
from app.modules.auth.repositories import (
    RoleRepository,
    SessionRepository,
    StaffWorkDailyRepository,
    UserRepository,
)
from app.modules.auth.schemas import (
    AuthUserResponse,
    BranchSummary,
    LoginResponse,
    OTPSentResponse,
    PartnerSelfRegisterRequest,
    RefreshResponse,
    SessionConfigResponse,
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
        self.work_repo = StaffWorkDailyRepository(db)
        self.otp_service = OTPService(cache)

    async def _platform_today(self) -> date:
        """Today in the platform zone — the bucket every work-day row uses.

        Server-side on purpose: a browser in another zone must not be able to
        file its working minutes against somebody else's Tuesday, and the
        daily-logout hour is compared in the same zone.
        """
        zone = await get_platform_tz(self.db)
        return datetime.now(zone).date()

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

    async def send_partner_registration_otp(self, mobile: str) -> OTPSentResponse:
        """Verify ownership of the applicant mobile before creating anything."""
        if await self.user_repo.exists_by_mobile(mobile):
            raise BusinessException(
                message=(
                    "An account already exists for this mobile number. "
                    "Sign in instead, or use a different number."
                ),
                code="MOBILE_ALREADY_REGISTERED",
            )
        return await self.otp_service.send_otp(
            mobile=mobile, purpose="PARTNER_REGISTER"
        )

    async def partner_self_register(
        self,
        data: PartnerSelfRegisterRequest,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        """Create a PARTNER user and a PENDING partner application.

        This is the app's self-service path. The account is usable immediately
        so the applicant can log in and watch the approval state, but the
        partner record stays PENDING until an admin activates it. Service rows
        are written now because they are what the API gates enforce later.
        """
        import random as _random
        import uuid as _uuid
        from sqlalchemy import text as _text

        await self.otp_service.verify_otp(
            data.mobile, data.otp, purpose="PARTNER_REGISTER"
        )

        if await self.user_repo.exists_by_mobile(data.mobile):
            raise BusinessException(
                message="An account already exists for this mobile number.",
                code="MOBILE_ALREADY_REGISTERED",
            )
        if await self.user_repo.get_by_email(data.email):
            raise BusinessException(
                message="An account already exists for this email address.",
                code="EMAIL_ALREADY_REGISTERED",
            )

        city = (
            await self.db.execute(
                _text("SELECT id FROM cities WHERE id = :id AND is_active = TRUE"),
                {"id": data.city_id},
            )
        ).first()
        if city is None:
            raise ValidationException("Select a valid operating city")

        valid_rows = (
            (
                await self.db.execute(
                    _text(
                        "SELECT type_code FROM service_types "
                        "WHERE is_active = TRUE AND type_code = ANY(:services)"
                    ),
                    {"services": data.services},
                )
            )
            .scalars()
            .all()
        )
        valid_services = {str(row) for row in valid_rows}
        invalid = sorted(set(data.services) - valid_services)
        if invalid:
            raise ValidationException(
                f"Unknown or inactive service type(s): {', '.join(invalid)}"
            )

        name_parts = data.owner_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else None

        user = await self.user_repo.create(
            mobile_number=data.mobile,
            email=data.email,
            password_hash=hash_password(data.password),
            user_type=UserType.PARTNER,
            status=UserStatus.ACTIVE,
            is_mobile_verified=True,
            is_email_verified=False,
            first_name=first_name,
            last_name=last_name,
        )
        await self.role_repo.ensure_default_role_for_user(user)

        partner_code = f"PRT-{_random.randint(100000, 999999)}"
        while (
            await self.db.execute(
                _text("SELECT id FROM partners WHERE partner_code = :code"),
                {"code": partner_code},
            )
        ).first():
            partner_code = f"PRT-{_random.randint(100000, 999999)}"

        partner_id = (
            await self.db.execute(
                _text(
                    """
                    INSERT INTO partners
                        (uuid, user_id, partner_code, partner_type, owner_name,
                         business_name, mobile, email, city_id, status,
                         onboarding_source, office_city_id, created_at, updated_at)
                    VALUES
                        (:uuid, :user_id, :code, :partner_type, :owner_name,
                         :business_name, :mobile, :email, :city_id, 'PENDING',
                         'MOBILE_APP', :city_id, NOW(), NOW())
                    RETURNING id
                    """
                ),
                {
                    "uuid": _uuid.uuid4(),
                    "user_id": user.id,
                    "code": partner_code,
                    "partner_type": data.partner_type,
                    "owner_name": data.owner_name.strip(),
                    "business_name": data.business_name.strip(),
                    "mobile": data.mobile,
                    "email": data.email,
                    "city_id": data.city_id,
                },
            )
        ).scalar_one()

        await self.db.execute(
            _text(
                """
                INSERT INTO partner_ratings
                    (partner_id, average_rating, total_reviews, updated_at)
                VALUES (:pid, 0, 0, NOW())
                """
            ),
            {"pid": partner_id},
        )
        await self.db.execute(
            _text(
                """
                INSERT INTO wallets
                    (partner_id, wallet_type, available_balance, hold_balance,
                     credit_limit, wallet_status, created_at)
                VALUES (:pid, 'PREPAID', 0, 0, 0, 'ACTIVE', NOW())
                ON CONFLICT (partner_id) DO NOTHING
                """
            ),
            {"pid": partner_id},
        )
        for service_type in data.services:
            await self.db.execute(
                _text(
                    """
                    INSERT INTO partner_services
                        (partner_id, service_type, is_active, created_at)
                    VALUES (:pid, :service_type, TRUE, NOW())
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"pid": partner_id, "service_type": service_type},
            )
        await self.db.execute(
            _text(
                """
                INSERT INTO partner_verification_logs
                    (partner_id, action, remarks, created_at)
                VALUES
                    (:pid, 'PARTNER_APPLIED',
                     'Self-registration submitted from the partner app', NOW())
                """
            ),
            {"pid": partner_id},
        )

        await self.db.flush()
        logger.info(
            "partner_self_registered",
            user_id=str(user.id),
            partner_id=partner_id,
            partner_code=partner_code,
            services=data.services,
        )

        try:
            from app.infrastructure.email import send_event_email

            await send_event_email(
                self.db,
                event_type="partner_registered",
                to_email=data.email,
                to_name=data.owner_name,
                context={
                    "name": data.owner_name or data.business_name or "partner",
                    "message": (
                        f"Thank you for applying to WayTero. Your application "
                        f"({partner_code}) is pending review."
                    ),
                    "body": [
                        "Our verification team will review your business details. "
                        "You can track the status in the partner app.",
                    ],
                    "details": [
                        ("Partner code", partner_code),
                        ("Business name", data.business_name),
                        ("Services", ", ".join(data.services)),
                        ("Status", "Pending review"),
                    ],
                },
                related_type="PARTNER",
                related_id=partner_id,
            )
        except Exception:  # pragma: no cover — email must never break signup
            pass

        return await self._create_session_and_tokens(
            user, device_name, device_os, ip_address
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
        the column). The customer can attach a real number later via
        PATCH /customers/me/mobile (or the unverified no-SMS variant), and the
        booking flows prompt for one when it is still a placeholder.

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

    async def partner_firebase_login(
        self,
        id_token: str,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        """Google sign-in for an existing partner account.

        Google is a login method, not a second registration path: a partner
        must already have applied (or been created by an admin) and the Google
        email must match users.email. Creating a partner from a social login
        would skip city, services and KYC, so that deliberately does not happen.
        """
        await self._enforce_login_rate_limit("partner:firebase")

        try:
            claims = await verify_firebase_id_token(self.db, id_token)
        except FirebaseAuthUnavailableError as exc:
            raise BusinessException(
                message=(
                    "Google sign-in is not available right now. "
                    "Please use your mobile number and password."
                ),
                code="FIREBASE_UNAVAILABLE",
            ) from exc
        except InvalidFirebaseIdTokenError as exc:
            raise AuthenticationException(
                message="Google sign-in failed. Please try again.",
                code="INVALID_FIREBASE_TOKEN",
            ) from exc

        email = (claims.get("email") or "").strip().lower()
        if not email:
            raise AuthenticationException(
                message="Google account did not return an email address.",
                code="FIREBASE_NO_EMAIL",
            )
        if not claims.get("email_verified", False):
            raise AuthenticationException(
                message="Google account email is not verified.",
                code="FIREBASE_EMAIL_UNVERIFIED",
            )

        user = await self.user_repo.get_by_email_and_type(email, UserType.PARTNER)
        if user is None:
            raise AuthenticationException(
                message=(
                    "No partner account uses this Google email. "
                    "Apply as a partner first or sign in with your registered mobile."
                ),
                code="PARTNER_NOT_FOUND",
            )

        partner = await PartnerRepository(self.db).get_by_user_id(user.id)
        if partner is None:
            raise AuthenticationException(
                message="Partner application not found. Please contact support.",
                code="PARTNER_NOT_FOUND",
            )

        await self.user_repo.mark_email_verified(user.id)
        user.is_email_verified = True
        display_name = (claims.get("name") or "").strip()
        if display_name:
            first_name, last_name = _split_display_name(
                display_name, fallback_email=email
            )
            await self.user_repo.update_profile(
                user.id,
                first_name=first_name or user.first_name,
                last_name=last_name if last_name is not None else user.last_name,
                profile_image_url=claims.get("picture"),
            )

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        logger.info("partner_google_login", user_id=str(user.id), partner_id=partner.id)
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

    async def driver_firebase_login(
        self,
        id_token: str,
        device_name: Optional[str] = None,
        device_os: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> LoginResponse:
        """Google sign-in for a driver created by a partner.

        Google is a login method, not a registration path: the email must match
        a driver record (or the DRIVER user already linked to it). A driver
        without an email is told to use mobile OTP instead of being given an
        empty account that no partner can recognise.
        """
        await self._enforce_login_rate_limit("driver:firebase")

        try:
            claims = await verify_firebase_id_token(self.db, id_token)
        except FirebaseAuthUnavailableError as exc:
            raise BusinessException(
                message=(
                    "Google sign-in is not available right now. "
                    "Please use mobile OTP."
                ),
                code="FIREBASE_UNAVAILABLE",
            ) from exc
        except InvalidFirebaseIdTokenError as exc:
            raise AuthenticationException(
                message="Google sign-in failed. Please try again.",
                code="INVALID_FIREBASE_TOKEN",
            ) from exc

        email = (claims.get("email") or "").strip().lower()
        if not email:
            raise AuthenticationException(
                message="Google account did not return an email address.",
                code="FIREBASE_NO_EMAIL",
            )
        if not claims.get("email_verified", False):
            raise AuthenticationException(
                message="Google account email is not verified.",
                code="FIREBASE_EMAIL_UNVERIFIED",
            )

        from sqlalchemy import func, select

        from app.modules.driver.models import Driver

        user = await self.user_repo.get_by_email_and_type(email, UserType.DRIVER)
        driver = None

        if user is not None:
            driver = (
                await self.db.execute(
                    select(Driver).where(
                        Driver.user_id == user.id, Driver.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()

        if driver is None:
            driver = (
                await self.db.execute(
                    select(Driver).where(
                        func.lower(Driver.email) == email,
                        Driver.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

        if driver is None:
            raise AuthenticationException(
                message=(
                    "No driver account uses this Google email. Ask your partner "
                    "to add the email to your driver profile, or sign in with "
                    "your mobile number."
                ),
                code="DRIVER_NOT_FOUND",
            )

        if user is None and driver.user_id is not None:
            candidate = await self.user_repo.get_by_id(driver.user_id)
            if candidate is not None:
                if candidate.user_type != UserType.DRIVER:
                    raise AuthenticationException(
                        message="This Google email belongs to a non-driver account.",
                        code="DRIVER_ACCOUNT_CONFLICT",
                    )
                user = candidate

        if user is None:
            # A partner-created driver may predate the user backfill. Reuse the
            # DRIVER user with this mobile when one exists; never move a
            # customer/partner account into the driver portal.
            user = await self.user_repo.get_by_mobile_and_type(
                driver.mobile, UserType.DRIVER
            )
            if user is None:
                mobile_owner = await self.user_repo.get_by_mobile(driver.mobile)
                if mobile_owner is not None:
                    raise AuthenticationException(
                        message=(
                            "This driver mobile is linked to another WayTero "
                            "account. Please contact support."
                        ),
                        code="DRIVER_ACCOUNT_CONFLICT",
                    )
                parts = (driver.full_name or "Driver").split()
                first_name = parts[0] if parts else "Driver"
                last_name = " ".join(parts[1:]) if len(parts) > 1 else None
                user = await self.user_repo.create(
                    mobile_number=driver.mobile,
                    email=email,
                    user_type=UserType.DRIVER,
                    status=UserStatus.ACTIVE,
                    first_name=first_name,
                    last_name=last_name,
                    is_email_verified=True,
                    is_mobile_verified=True,
                )

        if driver.user_id != user.id:
            driver.user_id = user.id
        if not user.email:
            owner = await self.user_repo.get_by_email(email)
            if owner is None:
                user.email = email

        await self.user_repo.mark_email_verified(user.id)
        user.is_email_verified = True
        display_name = (claims.get("name") or "").strip()
        if display_name:
            first_name, last_name = _split_display_name(
                display_name, fallback_email=email
            )
            await self.user_repo.update_profile(
                user.id,
                first_name=first_name or user.first_name,
                last_name=last_name if last_name is not None else user.last_name,
                profile_image_url=claims.get("picture"),
            )

        await self.role_repo.ensure_default_role_for_user(user)
        await self._check_account_active(user)
        logger.info("driver_google_login", user_id=str(user.id), driver_id=driver.id)
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

    async def _close_work_day(self, user_id: UUID) -> None:
        """Stamp the end of today's work row for a staff account.

        Customers and partners sign out through the same method, so the staff
        check belongs here rather than in each route.
        """
        user = await self.user_repo.get_by_id(user_id)
        if user is None or user.user_type.value not in STAFF_USER_TYPES:
            return
        await self.work_repo.close_day(
            user_id=user_id, work_date=await self._platform_today()
        )

    async def record_heartbeat(self, user_id: UUID, delta_seconds: int) -> int:
        """Add actively-worked seconds to today's row; returns what was kept.

        The portal only counts real input and sends at most once a minute, but
        the clamp is server-side because anything holding the token can call
        this: a client claiming 3600 records 60.
        """
        await self._staff_user(user_id)
        recorded = max(1, min(HEARTBEAT_MAX_SECONDS, int(delta_seconds)))
        await self.work_repo.record_activity(
            user_id=user_id,
            work_date=await self._platform_today(),
            active_seconds=recorded,
        )
        return recorded

    async def get_my_work_hours(self, user_id: UUID, days: int = 30) -> dict:
        """This admin's own recent days.

        The working-hours report is super-admin only; the profile tab still has
        to show somebody their own attendance, and "your own rows" is the one
        filter that needs no extra permission.
        """
        await self._staff_user(user_id)
        zone = await get_platform_tz(self.db)
        today = datetime.now(zone).date()
        date_from = today - timedelta(days=max(1, min(days, 366)) - 1)
        items = await self.work_repo.list_days(
            tz_name=str(zone), date_from=date_from, date_to=today, user_id=user_id
        )
        totals = await self.work_repo.summarize_days(
            date_from=date_from, date_to=today, user_id=user_id
        )
        return {
            "items": items,
            "period_totals": totals,
            "date_from": date_from.isoformat(),
            "date_to": today.isoformat(),
        }

    async def logout(
        self, user_id: UUID, refresh_token: Optional[str], logout_all: bool
    ) -> None:
        if logout_all:
            await self.session_repo.deactivate_all_user_sessions(
                user_id, logout_at=datetime.now(timezone.utc)
            )
            await self._close_work_day(user_id)
            logger.info("logout_all_devices", user_id=str(user_id))
            return

        if refresh_token:
            session = await self.session_repo.get_by_refresh_token_hash(
                _hash_token(refresh_token)
            )
            if session and session.user_id == user_id:
                await self.session_repo.deactivate_session(
                    session.id, logout_at=datetime.now(timezone.utc)
                )

        await self._close_work_day(user_id)
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
        partner_id, partner_status, active_services = (
            await self._resolve_partner_access(user)
        )
        return self._build_user_response(
            user,
            roles,
            permissions,
            partner_id=partner_id,
            partner_status=partner_status,
            active_services=active_services,
            branch=await self._resolve_branch(user),
        )

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
        partner_status: Optional[str] = None,
        active_services: Optional[list[str]] = None,
        branch: Optional[BranchSummary] = None,
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
            # No PIN means the shell can only log the admin out on idle, so the
            # profile asks for one. Staff-only: a customer has no dashboard.
            must_set_mpin=(
                user.user_type.value in DASHBOARD_USER_TYPES and user.mpin_hash is None
            ),
            user_code=user.user_code,
            last_login_at=user.last_login_at,
            partner_id=partner_id,
            partner_status=partner_status,
            active_services=active_services or [],
            branch=branch,
            is_super=user.user_type == UserType.SUPER_ADMIN,
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
        # Dashboard sessions get the absolute 12-hour cap; customers keep the
        # normal refresh-token lifetime. The DB row is the gate: /auth/refresh
        # refuses an expired session even if the JWT itself has not expired.
        session_lifetime = (
            timedelta(hours=DASHBOARD_SESSION_HOURS)
            if user.user_type.value in DASHBOARD_USER_TYPES
            else timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        )
        expires_at = datetime.now(timezone.utc) + session_lifetime
        session = await self.session_repo.create(
            user_id=user.id,
            refresh_token_hash=provisional_hash,
            device_name=device_name,
            # device_os is not a column in UserSession model — omitted
            ip_address=ip_address,
            expires_at=expires_at,
        )

        # Resolve the partner's access context once: the integer id goes into
        # the token, while status + active services go into the user envelope
        # so the app can gate routes without a second request.
        partner_int_id, partner_status, active_services = (
            await self._resolve_partner_access(user)
        )
        branch = await self._resolve_branch(user)

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
        if user.user_type.value in STAFF_USER_TYPES:
            # Attendance: this is the row the working-hours report reads. After
            # the token block because W0 made this path run exactly once per
            # login — two calls here would double `session_count`.
            await self.work_repo.open_day(
                user_id=user.id, work_date=await self._platform_today()
            )
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
                user,
                roles,
                permissions,
                partner_id=partner_int_id,
                partner_status=partner_status,
                active_services=active_services,
                branch=branch,
            ),
            tokens=TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            ),
            session_id=session.id,
        )

    async def _resolve_partner_access(
        self, user: User
    ) -> tuple[Optional[int], Optional[str], list[str]]:
        """Return (partner_id, status, active services) for a partner user.

        The app routes on this context before rendering any operational screen.
        Doc Ref: Security Hardening — S6: partner_id from JWT, not query params.
        """
        if user.user_type.value != UserType.PARTNER.value:
            return None, None, []
        try:
            partner_repo = PartnerRepository(self.db)
            partner_obj = await partner_repo.get_by_user_id(user.id)
            if partner_obj:
                services = sorted(
                    {
                        str(service.service_type)
                        for service in (partner_obj.services or [])
                        if service.is_active
                    }
                )
                return partner_obj.id, str(partner_obj.status), services
        except Exception:
            pass  # Non-fatal — endpoints can still resolve it via DB when needed
        return None, None, []

    async def _resolve_partner_int_id(self, user: User) -> Optional[int]:
        partner_id, _, _ = await self._resolve_partner_access(user)
        return partner_id

    async def _resolve_branch(self, user: User) -> Optional[BranchSummary]:
        """The office this staff account answers to, for the portal's topbar.

        Display only — nothing enforces on it. get_current_user re-reads
        users.branch_id on every request, so a super admin moving someone takes
        effect immediately even if the client still shows the old chip.

        City name comes along because the chip reads better with "Cuttack" than
        with a code, and this is the one query that already has the branch row.
        """
        if user.branch_id is None:
            return None
        row = (
            await self.db.execute(
                text(
                    "SELECT b.name, b.code, b.city_id, b.is_headquarters, c.name "
                    "FROM branches b "
                    "LEFT JOIN cities c ON c.id = b.city_id "
                    "WHERE b.id = :branch_id"
                ),
                {"branch_id": user.branch_id},
            )
        ).first()
        if row is None:
            # The FK is RESTRICT, so this is unreachable while a row exists;
            # returning None keeps a stale id from crashing the login that a
            # branch-less account would otherwise have got.
            return None
        return BranchSummary(
            id=user.branch_id,
            name=row[0],
            code=row[1],
            city_id=row[2],
            city_name=row[4],
            is_headquarters=bool(row[3]),
        )

    # ── Admin session policy: unlock PIN + idle lock ───────────────────────────
    # Doc Ref: Admin Portal §39 Security; migration 0084.
    #
    # The dashboard no longer logs an idle admin out — it locks behind a PIN, so
    # a half-filled booking form survives a phone call. Tokens deliberately stay
    # alive through the lock; the freeze happens in the client (and the PIN is
    # checked against this DB row, not against the JWT).

    POLICY_CONFIG_KEYS = (
        CONFIG_ADMIN_IDLE_DEFAULT_MINUTES,
        CONFIG_ADMIN_DAILY_LOGOUT_HOUR,
        CONFIG_ADMIN_MPIN_MIN_LENGTH,
        CONFIG_ADMIN_MPIN_MAX_ATTEMPTS,
        CONFIG_ADMIN_MPIN_LOCKOUT_MINUTES,
    )

    async def _dashboard_user(self, user_id: UUID) -> User:
        """Load a signed-in dashboard user (admin staff or partner).

        mpin_hash and idle_lock_minutes sit on the shared `users` row, so these
        routes check the user type themselves — the /auth/admin/* prefix is a
        URL, not a gate, and a CUSTOMER or DRIVER token must not reach them.
        Partners share the same gate; they have their own portal and the same
        operational data at stake.
        """
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ResourceNotFoundException("User", user_id)
        if user.user_type.value not in DASHBOARD_USER_TYPES:
            raise PermissionDeniedException(
                "The dashboard unlock PIN is only available to dashboard accounts."
            )
        return user

    async def _staff_user(self, user_id: UUID) -> User:
        """Load a signed-in staff user for attendance-only operations."""
        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise ResourceNotFoundException("User", user_id)
        if user.user_type.value not in STAFF_USER_TYPES:
            raise PermissionDeniedException(
                "This operation is only available to admin staff."
            )
        return user

    async def _admin_policy(self) -> dict:
        """Read the five ADMIN_* configuration rows in one query.

        A missing or unparseable row falls back to the 0084 seed value, and any
        value is clamped into its safe range: a hand-edited row can make the
        lock stricter, never switch it off.
        """
        rows = (
            await self.db.execute(
                text(
                    "SELECT config_key, config_value FROM system_configurations "
                    "WHERE config_key = ANY(:keys)"
                ),
                {"keys": list(self.POLICY_CONFIG_KEYS)},
            )
        ).all()
        raw = dict(rows)

        def as_int(key: str, fallback: int, low: int, high: int) -> int:
            try:
                value = int(str(raw[key]).strip())
            except (KeyError, TypeError, ValueError):
                return fallback
            return max(low, min(high, value))

        return {
            "idle_default_minutes": as_int(
                CONFIG_ADMIN_IDLE_DEFAULT_MINUTES,
                IDLE_LOCK_MINUTES_DEFAULT,
                IDLE_LOCK_MINUTES_MIN,
                IDLE_LOCK_MINUTES_MAX,
            ),
            "daily_logout_hour": as_int(
                CONFIG_ADMIN_DAILY_LOGOUT_HOUR,
                DAILY_LOGOUT_HOUR_DEFAULT,
                DAILY_LOGOUT_HOUR_MIN,
                DAILY_LOGOUT_HOUR_MAX,
            ),
            "mpin_min_length": as_int(
                CONFIG_ADMIN_MPIN_MIN_LENGTH, MPIN_MIN_LENGTH_DEFAULT, 4, 12
            ),
            "mpin_max_attempts": as_int(
                CONFIG_ADMIN_MPIN_MAX_ATTEMPTS, MPIN_MAX_ATTEMPTS_DEFAULT, 1, 10
            ),
            "mpin_lockout_minutes": as_int(
                CONFIG_ADMIN_MPIN_LOCKOUT_MINUTES, MPIN_LOCKOUT_MINUTES_DEFAULT, 1, 240
            ),
        }

    @staticmethod
    def _validate_mpin_length(mpin: str, min_length: int) -> None:
        if len(mpin) < min_length:
            raise ValidationException(
                message=f"Unlock PIN must be at least {min_length} digits.",
                code="MPIN_TOO_SHORT",
            )

    async def _confirm_password(
        self, user: User, current_password: Optional[str]
    ) -> None:
        """Password re-entry for any change that touches the gate itself.

        Without it, a hijacked live session token could install, rotate or strip
        the PIN that locks the real admin out. Success also clears an MPIN
        lockout — re-proving the password is the only way back in before the
        lockout window expires.
        """
        if not user.password_hash:
            raise BusinessException(
                message="This account signs in with an OTP, so confirm the unlock PIN instead.",
                code="PASSWORD_NOT_SET",
            )
        if not current_password or not verify_password(
            current_password, user.password_hash
        ):
            raise AuthenticationException(
                message="Password confirmation is required.",
                code="WRONG_PASSWORD",
            )
        user.mpin_failed_attempts = 0
        user.mpin_locked_until = None

    async def _confirm_mpin_otp(self, user: User, otp: Optional[str]) -> None:
        """OTP re-entry for an account with no password.

        The same ownership proof, reached a different way: installing the
        unlock PIN is the one action a hijacked live token must not be able to
        do, and an OTP-only admin has no password to type. The OTP goes to the
        number on this user's own row, scoped to a purpose no login will accept.
        """
        if not otp:
            raise ValidationException(
                message="Enter the OTP sent to your registered mobile.",
                code="OTP_REQUIRED",
            )
        await self.otp_service.verify_otp(
            user.mobile_number, otp, purpose=OTP_PURPOSE_MPIN_SET
        )

    def _require_unlockable(self, user: User) -> None:
        """Guard the PIN paths: set, and not currently in a lockout window."""
        if user.mpin_hash is None:
            raise BusinessException(
                message="No unlock PIN is set for this account.",
                code="MPIN_NOT_SET",
            )
        if user.mpin_locked_until is None:
            return
        if user.is_mpin_locked:
            raise self._lockout_error(user)
        # Window expired — start the next five attempts fresh.
        user.mpin_locked_until = None
        user.mpin_failed_attempts = 0

    @staticmethod
    def _lockout_error(user: User) -> BusinessException:
        return BusinessException(
            message=(
                "Too many incorrect PIN attempts. Wait for the lock to pass, "
                "or confirm your password."
            ),
            code="MPIN_LOCKED",
            details={"locked_until": user.mpin_locked_until.isoformat()},
        )

    async def _check_mpin(
        self, user: User, mpin: Optional[str], max_attempts: int, lockout_minutes: int
    ) -> None:
        """Verify a submitted PIN, counting failures against the lockout."""
        if not mpin:
            raise ValidationException(
                message="Enter the current unlock PIN.", code="MPIN_REQUIRED"
            )
        if verify_password(mpin, user.mpin_hash):
            user.mpin_failed_attempts = 0
            user.mpin_locked_until = None
            return

        logger.warning(AUTH_EVENT_MPIN_FAILED, user_id=str(user.id))
        remaining = max(0, max_attempts - (user.mpin_failed_attempts + 1))
        if remaining:
            user.mpin_failed_attempts += 1
            await self.db.flush()
            raise BusinessException(
                message="Unlock PIN is incorrect.",
                code="MPIN_INVALID",
                details={"attempts_remaining": remaining},
            )

        # The attempt that trips the lock answers "locked", not "wrong, 0 left":
        # what the admin needs next is the wait time, and the password re-entry
        # that ends it early.
        user.mpin_failed_attempts = max_attempts
        user.mpin_locked_until = datetime.now(timezone.utc) + timedelta(
            minutes=lockout_minutes
        )
        await self.db.flush()
        logger.warning(AUTH_EVENT_MPIN_LOCKED, user_id=str(user.id))
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_MPIN_LOCKED,
            user_id=_user_audit_id(user.id),
        )
        raise self._lockout_error(user)

    async def get_session_config(
        self, user_id: UUID, session_id: Optional[str] = None
    ) -> SessionConfigResponse:
        """Everything the dashboard shell needs about this session, in one call.

        `server_now` is what the client counts the daily-logout hour against, so
        a wrong laptop clock cannot skip the forced logout. `session_expires_at`
        is the absolute 12-hour bound; the client signs out at that instant even
        if the tab has been idle-locked and never refreshed a token.
        """
        user = await self._dashboard_user(user_id)
        policy = await self._admin_policy()
        has_mpin = user.mpin_hash is not None
        session = None
        if session_id:
            try:
                session = await self.session_repo.get_by_id(UUID(session_id))
            except (TypeError, ValueError):
                session = None
        return SessionConfigResponse(
            has_mpin=has_mpin,
            has_password=user.password_hash is not None,
            idle_minutes=user.idle_lock_minutes or policy["idle_default_minutes"],
            idle_lock_minutes=user.idle_lock_minutes,
            idle_default_minutes=policy["idle_default_minutes"],
            daily_logout_hour=policy["daily_logout_hour"],
            timezone=await get_platform_tz_name(self.db),
            server_now=datetime.now(timezone.utc),
            mpin_locked_until=user.mpin_locked_until if user.is_mpin_locked else None,
            mpin_attempts_remaining=(
                max(0, policy["mpin_max_attempts"] - user.mpin_failed_attempts)
                if has_mpin
                else None
            ),
            mpin_min_length=policy["mpin_min_length"],
            access_token_expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            session_started_at=session.login_at if session else None,
            session_expires_at=session.expires_at if session else None,
            branch=await self._resolve_branch(user),
            is_super=user.user_type == UserType.SUPER_ADMIN,
        )

    async def send_mpin_otp(self, user_id: UUID) -> OTPSentResponse:
        """OTP to this admin's own registered mobile, to prove they own the
        account before installing an unlock PIN.

        Subject-scoped on purpose: `/auth/send-otp` takes any mobile from the
        body, and this route must only ever reach the caller's own number.
        """
        user = await self._dashboard_user(user_id)
        return await self.otp_service.send_otp(
            user.mobile_number, purpose=OTP_PURPOSE_MPIN_SET
        )

    async def set_mpin(
        self,
        user_id: UUID,
        mpin: str,
        current_password: Optional[str],
        otp: Optional[str] = None,
    ) -> None:
        user = await self._dashboard_user(user_id)
        policy = await self._admin_policy()
        if user.mpin_hash is not None:
            raise BusinessException(
                message="An unlock PIN is already set — change or remove it instead.",
                code="MPIN_ALREADY_SET",
            )
        if user.password_hash:
            await self._confirm_password(user, current_password)
        else:
            # An OTP-only admin has no password to re-enter, so the OTP to their
            # own mobile is the ownership proof. Without one of the two, any
            # holder of a live token could install the gate that locks the real
            # admin out.
            await self._confirm_mpin_otp(user, otp)
        self._validate_mpin_length(mpin, policy["mpin_min_length"])
        user.mpin_hash = hash_password(mpin)
        await self.db.flush()
        logger.info(AUTH_EVENT_MPIN_SET, user_id=str(user_id))
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_MPIN_SET,
            user_id=_user_audit_id(user_id),
        )

    async def change_mpin(
        self, user_id: UUID, current_mpin: Optional[str], new_mpin: str
    ) -> None:
        user = await self._dashboard_user(user_id)
        policy = await self._admin_policy()
        self._require_unlockable(user)
        await self._check_mpin(
            user,
            current_mpin,
            policy["mpin_max_attempts"],
            policy["mpin_lockout_minutes"],
        )
        self._validate_mpin_length(new_mpin, policy["mpin_min_length"])
        if new_mpin == current_mpin:
            raise ValidationException(
                message="Choose a PIN different from the current one.",
                code="MPIN_UNCHANGED",
            )
        user.mpin_hash = hash_password(new_mpin)
        await self.db.flush()
        logger.info(AUTH_EVENT_MPIN_CHANGED, user_id=str(user_id))
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_MPIN_CHANGED,
            user_id=_user_audit_id(user_id),
        )

    async def verify_mpin(self, user_id: UUID, mpin: Optional[str]) -> None:
        """Unlock the dashboard after an idle lock. Never issues tokens."""
        user = await self._dashboard_user(user_id)
        policy = await self._admin_policy()
        self._require_unlockable(user)
        await self._check_mpin(
            user, mpin, policy["mpin_max_attempts"], policy["mpin_lockout_minutes"]
        )
        logger.info(AUTH_EVENT_MPIN_VERIFIED, user_id=str(user_id))

    async def remove_mpin(
        self,
        user_id: UUID,
        current_password: Optional[str],
        current_mpin: Optional[str] = None,
    ) -> None:
        user = await self._dashboard_user(user_id)
        policy = await self._admin_policy()
        if user.mpin_hash is None:
            raise BusinessException(
                message="No unlock PIN is set for this account.",
                code="MPIN_NOT_SET",
            )
        if user.password_hash:
            await self._confirm_password(user, current_password)
        else:
            # An OTP-only admin has no password to re-enter, so the PIN itself is
            # the only proof available.
            self._require_unlockable(user)
            await self._check_mpin(
                user,
                current_mpin,
                policy["mpin_max_attempts"],
                policy["mpin_lockout_minutes"],
            )
        user.mpin_hash = None
        user.mpin_failed_attempts = 0
        user.mpin_locked_until = None
        await self.db.flush()
        logger.info(AUTH_EVENT_MPIN_REMOVED, user_id=str(user_id))
        await AuditLogger.log_auth_event(
            self.db,
            action_type=AUTH_EVENT_MPIN_REMOVED,
            user_id=_user_audit_id(user_id),
        )

    async def set_idle_lock_minutes(
        self, user_id: UUID, idle_lock_minutes: Optional[int]
    ) -> int:
        """Set how soon this admin's screen locks; None inherits the default.

        Reduce-only: an admin may lock their own dashboard sooner than the
        platform default, never later — otherwise the idle lock becomes opt-out.
        """
        user = await self._dashboard_user(user_id)
        policy = await self._admin_policy()
        if (
            idle_lock_minutes is not None
            and idle_lock_minutes > policy["idle_default_minutes"]
        ):
            raise ValidationException(
                message=(
                    f"Your lock time can only be shorter than the platform default "
                    f"of {policy['idle_default_minutes']} minutes."
                ),
                code="IDLE_LOCK_TOO_LONG",
                details={"idle_default_minutes": policy["idle_default_minutes"]},
            )
        user.idle_lock_minutes = idle_lock_minutes
        await self.db.flush()
        return idle_lock_minutes or policy["idle_default_minutes"]
