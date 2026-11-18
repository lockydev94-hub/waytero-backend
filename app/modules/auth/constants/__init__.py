# ============================================================
# WAY TERO — AUTH CONSTANTS
# File: app/modules/auth/constants/__init__.py
# Doc Ref: Auth Flow — OTP Security Rules (Section 10)
# Doc Ref: Auth Flow — Rate Limiting (Section 30)
# Doc Ref: Auth Flow — Multi Device Support (Section 18)
# Phase: 1 — Authentication Module
# ============================================================

# --- OTP
OTP_MAX_ATTEMPTS = 5
OTP_LOCK_MINUTES = 15
OTP_RESEND_COOLDOWN_SECONDS = 60  # min gap between resend requests
OTP_RATE_LIMIT_COUNT = 5
OTP_RATE_LIMIT_WINDOW_SECONDS = 15 * 60

# --- Redis Key Prefixes
REDIS_OTP_KEY = "otp:{mobile}"  # stores hashed OTP
REDIS_OTP_ATTEMPTS_KEY = "otp_attempts:{mobile}"  # failed attempt counter
REDIS_OTP_LOCK_KEY = "otp_lock:{mobile}"  # lock flag
REDIS_OTP_RESEND_KEY = "otp_resend:{mobile}"  # resend cooldown flag
REDIS_OTP_RATE_KEY = "otp_rate:{mobile}"  # OTP request rate limit
REDIS_LOGIN_RATE_KEY = "login_rate:{scope}"  # login request rate limit
REDIS_REFRESH_RATE_KEY = "refresh_rate:{scope}"  # refresh request rate limit
REDIS_PASSWORD_RESET_KEY = "password_reset:{token_hash}"
REDIS_PASSWORD_RESET_RATE_KEY = "password_reset_rate:{email}"
REDIS_PASSWORD_RESET_OTP_KEY = (
    "password_reset_otp:{token_hash}"  # OTP for password reset
)

# --- Account Lockout (Doc: Section 24)
LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 30
LOGIN_RATE_LIMIT_COUNT = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60
REFRESH_RATE_LIMIT_COUNT = 30
REFRESH_RATE_LIMIT_WINDOW_SECONDS = 60 * 60
PASSWORD_RESET_EXPIRE_MINUTES = 15
PASSWORD_RESET_RATE_LIMIT_COUNT = 5
PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS = 60 * 60

# --- Multi-Device Session Limits (Doc: Section 18)
MAX_SESSIONS = {
    "CUSTOMER": 5,
    "PARTNER": 3,
    "DRIVER": 2,
    "ADMIN": 1,
    "SUPER_ADMIN": 1,
    "CCO": 2,
    "VERIFICATION_OFFICER": 2,
    "FINANCE_MANAGER": 2,
}

# --- Audit Event Types (Doc: Section 29)
AUTH_EVENT_LOGIN_SUCCESS = "LOGIN_SUCCESS"
AUTH_EVENT_LOGIN_FAILED = "LOGIN_FAILED"
AUTH_EVENT_LOGOUT = "LOGOUT"
AUTH_EVENT_OTP_SENT = "OTP_SENT"
AUTH_EVENT_OTP_VERIFIED = "OTP_VERIFIED"
AUTH_EVENT_OTP_FAILED = "OTP_FAILED"
AUTH_EVENT_PASSWORD_CHANGED = "PASSWORD_CHANGED"
AUTH_EVENT_TOKEN_REFRESHED = "TOKEN_REFRESHED"
AUTH_EVENT_ACCOUNT_LOCKED = "ACCOUNT_LOCKED"

DEFAULT_ROLE_CODE_BY_USER_TYPE = {
    "CUSTOMER": "CUSTOMER",
    "PARTNER": "PARTNER",
    "DRIVER": "DRIVER",
    "CCO": "CCO",
    "VERIFICATION_OFFICER": "VERIFICATION_OFFICER",
    "FINANCE_MANAGER": "FINANCE_MANAGER",
    "ADMIN": "ADMIN",
    "SUPER_ADMIN": "SUPER_ADMIN",
}

# ── Admin dashboard session policy ────────────────────────────────────────────
# Doc: Admin Portal §39 Security — idle lock, daily forced logout, MPIN unlock.
# Migration: 0084.

# Who signs in to the admin portal. The MPIN and idle columns live on the
# shared `users` row, so the endpoints must reject CUSTOMER / PARTNER / DRIVER
# explicitly rather than assume the route name is a gate.
STAFF_USER_TYPES = (
    "SUPER_ADMIN",
    "ADMIN",
    "CCO",
    "FINANCE_MANAGER",
    "VERIFICATION_OFFICER",
)

# The portals protected by the screen lock. Partners get the same MPIN gate as
# staff because their dashboard holds operational and payout data too; drivers
# and customers do not have a dashboard to lock.
DASHBOARD_USER_TYPES = STAFF_USER_TYPES + ("PARTNER",)

# Absolute session lifetime for a dashboard sign-in. The refresh token row is
# capped at this even though customer sessions may live for 30 days: after 12
# hours the portal must sign in again, so a borrowed laptop cannot stay signed
# in indefinitely. Idle locking is a separate, shorter rule.
DASHBOARD_SESSION_HOURS = 12

CONFIG_ADMIN_IDLE_DEFAULT_MINUTES = "ADMIN_IDLE_DEFAULT_MINUTES"
CONFIG_ADMIN_DAILY_LOGOUT_HOUR = "ADMIN_DAILY_LOGOUT_HOUR"
CONFIG_ADMIN_MPIN_MIN_LENGTH = "ADMIN_MPIN_MIN_LENGTH"
CONFIG_ADMIN_MPIN_MAX_ATTEMPTS = "ADMIN_MPIN_MAX_ATTEMPTS"
CONFIG_ADMIN_MPIN_LOCKOUT_MINUTES = "ADMIN_MPIN_LOCKOUT_MINUTES"

# Code-side fallbacks for when a configuration row is missing (fresh database,
# or a seed deleted from /settings). They match what 0084 seeds.
MPIN_MIN_LENGTH_DEFAULT = 6
MPIN_MAX_LENGTH = 12
MPIN_MAX_ATTEMPTS_DEFAULT = 5
MPIN_LOCKOUT_MINUTES_DEFAULT = 15
IDLE_LOCK_MINUTES_DEFAULT = 5
DAILY_LOGOUT_HOUR_DEFAULT = 23

# The same range the users.idle_lock_minutes CHECK enforces — an admin may make
# the screen lock sooner, never later.
IDLE_LOCK_MINUTES_MIN = 1
IDLE_LOCK_MINUTES_MAX = 240
# A day is 24 hours; the heartbeat clamp keeps one report from exceeding it.
HEARTBEAT_MAX_SECONDS = 60
DAILY_LOGOUT_HOUR_MIN = 0
DAILY_LOGOUT_HOUR_MAX = 23

# OTP scope for installing the unlock PIN on an account that has no password.
# `set_mpin` demands proof of ownership first — a hijacked live token must not
# be able to install the gate that locks out the real admin — and an OTP-only
# admin has no password to re-enter, so the code goes to their own registered
# mobile. Scoping it here keeps that OTP unusable for a login.
OTP_PURPOSE_MPIN_SET = "MPIN_SET"

AUTH_EVENT_MPIN_SET = "MPIN_SET"
AUTH_EVENT_MPIN_CHANGED = "MPIN_CHANGED"
AUTH_EVENT_MPIN_VERIFIED = "MPIN_VERIFIED"
AUTH_EVENT_MPIN_FAILED = "MPIN_FAILED"
AUTH_EVENT_MPIN_LOCKED = "MPIN_LOCKED"
AUTH_EVENT_MPIN_REMOVED = "MPIN_REMOVED"
