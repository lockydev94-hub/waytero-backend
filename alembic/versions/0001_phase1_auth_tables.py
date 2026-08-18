"""Phase 1 - Auth tables: users, sessions, otp, refresh_tokens, login_history

Revision ID: 0001_phase1_auth
Revises:
Create Date: 2026-06-01

Doc Ref: DB Architecture Part 3 — Auth Tables (Sections 5, 16, 19, 23, 26)
Phase: 1 — Authentication Module

NOTE: roles, permissions, role_permissions, user_roles are created in 0002_phase1_rbac
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_phase1_auth"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ============================================================
    # ENUMS — raw SQL, bypasses SQLAlchemy type event system
    # Doc Ref: DB Architecture Part 3, Section 2
    # ============================================================
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE user_type_enum AS ENUM (
                'CUSTOMER', 'PARTNER', 'DRIVER', 'CCO',
                'VERIFICATION_OFFICER', 'FINANCE_MANAGER', 'ADMIN', 'SUPER_ADMIN'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE user_status_enum AS ENUM (
                'ACTIVE', 'INACTIVE', 'SUSPENDED', 'PENDING_VERIFICATION', 'BLOCKED'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    # ============================================================
    # USERS TABLE — Doc Ref: Section 5
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY,
            user_code VARCHAR(30) UNIQUE,
            first_name VARCHAR(100) NOT NULL,
            last_name VARCHAR(100),
            mobile_number VARCHAR(20) NOT NULL UNIQUE,
            email VARCHAR(255) UNIQUE,
            password_hash TEXT,
            profile_image_url TEXT,
            user_type user_type_enum NOT NULL,
            status user_status_enum NOT NULL DEFAULT 'ACTIVE',
            is_mobile_verified BOOLEAN NOT NULL DEFAULT FALSE,
            is_email_verified BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            failed_login_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TIMESTAMPTZ,
            last_login_at TIMESTAMPTZ,
            deleted_at TIMESTAMPTZ,
            created_by UUID,
            updated_by UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    # Indexes — Doc Ref: Section 6
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_users_mobile_number ON users(mobile_number)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_users_user_type ON users(user_type)"))

    # ============================================================
    # SESSIONS TABLE — Doc Ref: Section 16
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS sessions (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_token TEXT NOT NULL,
            device_id VARCHAR(255),
            device_name VARCHAR(255),
            ip_address VARCHAR(100),
            user_agent TEXT,
            login_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            logout_at TIMESTAMPTZ,
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        )
    """))

    # Indexes — Doc Ref: Section 17
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_active)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)"))

    # ============================================================
    # OTP_VERIFICATIONS TABLE — Doc Ref: Section 19
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS otp_verifications (
            id UUID PRIMARY KEY,
            mobile_number VARCHAR(20) NOT NULL,
            otp_hash TEXT NOT NULL,
            purpose VARCHAR(50) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            verified_at TIMESTAMPTZ,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            is_verified BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    # Indexes — Doc Ref: Section 21
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_otp_mobile ON otp_verifications(mobile_number)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_otp_expiry ON otp_verifications(expires_at)"))

    # ============================================================
    # REFRESH_TOKENS TABLE — Doc Ref: Section 23
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL,
            device_id VARCHAR(255),
            issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            is_revoked BOOLEAN NOT NULL DEFAULT FALSE
        )
    """))

    # Indexes — Doc Ref: Section 24
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_refresh_expiry ON refresh_tokens(expires_at)"))

    # ============================================================
    # LOGIN_HISTORY TABLE — Doc Ref: Section 26
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS login_history (
            id UUID PRIMARY KEY,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            mobile_number VARCHAR(20),
            ip_address VARCHAR(100),
            device_name VARCHAR(255),
            user_agent TEXT,
            login_status VARCHAR(30),
            failure_reason TEXT,
            login_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS login_history"))
    conn.execute(sa.text("DROP TABLE IF EXISTS refresh_tokens"))
    conn.execute(sa.text("DROP TABLE IF EXISTS otp_verifications"))
    conn.execute(sa.text("DROP TABLE IF EXISTS sessions"))
    conn.execute(sa.text("DROP TABLE IF EXISTS users"))
    conn.execute(sa.text("DROP TYPE IF EXISTS user_status_enum"))
    conn.execute(sa.text("DROP TYPE IF EXISTS user_type_enum"))
