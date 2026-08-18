# ============================================================
# WAY TERO — CORE CONFIGURATION
# File: app/core/config.py
# Doc Ref: Backend Architecture Part 2, Section 16
# Uses pydantic-settings for type-safe environment loading.
# ============================================================

from typing import List

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "WayTero"
    APP_ENV: str = "development"
    APP_DEBUG: bool = (
        False  # Set True only when debugging SQL — disables SQLAlchemy echo in production/dev
    )
    APP_VERSION: str = "1.0.0"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    SECRET_KEY: str = "change-this-secret-key"

    # --- Database (PostgreSQL 17) ---
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "waytero_db"
    DB_USER: str = "waytero_user"
    DB_PASSWORD: str = "password"
    DATABASE_URL: str = (
        "postgresql+asyncpg://waytero_user:password@localhost:5432/waytero_db"
    )

    # --- Redis ---
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- JWT (Doc Ref: Auth Architecture — JWT + Argon2) ---
    JWT_SECRET_KEY: str = "change-this-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # --- MinIO (Object Storage) ---
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_SECURE: bool = False
    MINIO_BUCKET_DOCUMENTS: str = "documents"
    MINIO_BUCKET_VEHICLES: str = "vehicles"
    MINIO_BUCKET_DRIVERS: str = "drivers"
    MINIO_BUCKET_PARTNERS: str = "partners"
    MINIO_BUCKET_HOTELS: str = "hotels"
    MINIO_BUCKET_INVOICES: str = "invoices"

    # --- Celery ---
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- OTP Config ---
    OTP_EXPIRE_MINUTES: int = 5
    OTP_LENGTH: int = 6

    # --- Session TTL (for KV-backed edge-cached sessions) ---
    SESSION_TTL_SECONDS: int = 3600

    # --- SMS ---
    SMS_PROVIDER: str = "msg91"
    MSG91_AUTH_KEY: str = ""
    MSG91_SENDER_ID: str = "WAYTRO"
    MSG91_TEMPLATE_ID_OTP: str = ""

    # --- Email ---
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_NAME: str = "WayTero"
    SMTP_FROM_EMAIL: str = "noreply@waytero.com"

    # --- Payment (Razorpay) ---
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    # --- Startup behaviour ---
    # Production runs migrations via a dedicated one-shot "migrate" service so
    # multiple uvicorn workers never race on alembic (H9). Set False in prod.
    RUN_MIGRATIONS_ON_STARTUP: bool = True

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @model_validator(mode="after")
    def _reject_weak_production_secrets(self):
        """Refuse to boot in production with default/weak signing secrets.

        Doc Ref: Security Hardening — S3: the JWT signing key was a public
        placeholder, so anyone could forge admin tokens. In production the
        secrets must come from the environment, be unique, and be strong.
        """
        if self.APP_ENV != "production":
            return self

        weak_jwt = (
            not self.JWT_SECRET_KEY
            or self.JWT_SECRET_KEY in {"change-this-jwt-secret"}
            or len(self.JWT_SECRET_KEY) < 32
        )
        if weak_jwt:
            raise ValueError(
                "JWT_SECRET_KEY must be set to a unique, strong value (>= 32 chars) "
                "in production — refusing to boot with the default placeholder."
            )

        weak_secret = (
            not self.SECRET_KEY
            or self.SECRET_KEY in {"change-this-secret-key"}
            or len(self.SECRET_KEY) < 32
        )
        if weak_secret:
            raise ValueError(
                "SECRET_KEY must be set to a unique, strong value (>= 32 chars) "
                "in production — refusing to boot with the default placeholder."
            )
        return self


# Single instance — import this everywhere
settings = Settings()
