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
