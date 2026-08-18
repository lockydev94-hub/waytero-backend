"""Staff profiles — extended fields for office staff members

Revision ID: 0014_staff_profiles
Revises: 0013_platform_profile
Create Date: 2026-07-29

Doc Ref:
  DB Schema Part 1 §2 — users table (user_type: ADMIN | CCO | VERIFICATION_OFFICER | FINANCE_MANAGER)
  BRD Part 2 §12 — Internal Staff Management
  19_Company_Operations §06 EMPLOYEE_LIFECYCLE — Onboarding, Document collection

Creates:
  staff_profiles          — extended profile per staff member (1-to-1 with users)
  staff_documents         — uploaded documents (ID card, PAN card, etc.)

Seeding:
  No default rows — staff are created by SUPER_ADMIN at runtime.

ON CONFLICT DO NOTHING ensures safe re-runs on backend restart.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_staff_profiles"
down_revision = "0013_platform_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── ENUMS ────────────────────────────────────────────────────────────────
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE gender_enum AS ENUM ('MALE', 'FEMALE', 'OTHER', 'PREFER_NOT_TO_SAY');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE blood_group_enum AS ENUM ('A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE employment_type_enum AS ENUM ('FULL_TIME', 'PART_TIME', 'CONTRACT', 'INTERN');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE id_card_status_enum AS ENUM ('NOT_GENERATED', 'GENERATED', 'PRINTED', 'REVOKED');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    # ── staff_profiles ───────────────────────────────────────────────────────
    # One-to-one extension of users table for internal office staff.
    # Doc Ref: BRD Part 2 §12, Employee Lifecycle §13 — Pre-Boarding / Documentation
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS staff_profiles (
            id                      UUID PRIMARY KEY,
            user_id                 UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,

            -- Personal
            date_of_birth           DATE,
            gender                  gender_enum,
            blood_group             blood_group_enum,
            personal_email          VARCHAR(255),
            emergency_contact_name  VARCHAR(150),
            emergency_contact_phone VARCHAR(20),

            -- Employment
            designation             VARCHAR(150),
            department              VARCHAR(150),
            employment_type         employment_type_enum NOT NULL DEFAULT 'FULL_TIME',
            joining_date            DATE,
            employee_id             VARCHAR(50) UNIQUE,    -- Internal employee number e.g. EMP-000001

            -- Address (current residence)
            address_line1           TEXT,
            address_line2           TEXT,
            city                    VARCHAR(100),
            state                   VARCHAR(100),
            pincode                 VARCHAR(10),
            country                 VARCHAR(100) DEFAULT 'India',

            -- Identity Documents
            pan_number              VARCHAR(20),           -- PAN card number
            aadhar_number           VARCHAR(20),           -- Aadhaar (last 4 visible)
            driving_license_number  VARCHAR(30),

            -- Bank Details (for salary disbursement)
            bank_name               VARCHAR(150),
            bank_account_number     VARCHAR(30),
            bank_ifsc_code          VARCHAR(20),
            bank_branch             VARCHAR(150),
            bank_account_type       VARCHAR(30),           -- SAVINGS | CURRENT

            -- Nominee
            nominee_name            VARCHAR(150),
            nominee_relation        VARCHAR(50),
            nominee_phone           VARCHAR(20),
            nominee_address         TEXT,

            -- ID Card
            id_card_status          id_card_status_enum NOT NULL DEFAULT 'NOT_GENERATED',
            id_card_generated_at    TIMESTAMPTZ,
            id_card_notes           TEXT,                  -- Admin instructions shown in backend view

            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_staff_profiles_user ON staff_profiles(user_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_staff_profiles_employee_id ON staff_profiles(employee_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_staff_profiles_joining ON staff_profiles(joining_date)"))

    # ── staff_documents ──────────────────────────────────────────────────────
    # Uploaded document files per staff member (Cloudinary URLs)
    # Doc Ref: Employee Lifecycle §13 — Pre-Boarding Documentation
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS staff_documents (
            id              UUID PRIMARY KEY,
            staff_profile_id UUID NOT NULL REFERENCES staff_profiles(id) ON DELETE CASCADE,

            document_type   VARCHAR(50) NOT NULL,    -- ID_CARD_FRONT | ID_CARD_BACK | PAN_CARD | OFFER_LETTER | PHOTO | OTHER
            document_name   VARCHAR(255),
            file_url        TEXT NOT NULL,            -- Cloudinary URL
            file_type       VARCHAR(20),              -- image/jpeg | application/pdf etc.
            is_verified     BOOLEAN NOT NULL DEFAULT FALSE,
            verified_by     UUID REFERENCES users(id),
            verified_at     TIMESTAMPTZ,
            notes           TEXT,

            uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_staff_docs_profile ON staff_documents(staff_profile_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_staff_docs_type ON staff_documents(document_type)"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS staff_documents"))
    conn.execute(sa.text("DROP TABLE IF EXISTS staff_profiles"))
    conn.execute(sa.text("DROP TYPE IF EXISTS id_card_status_enum"))
    conn.execute(sa.text("DROP TYPE IF EXISTS employment_type_enum"))
    conn.execute(sa.text("DROP TYPE IF EXISTS blood_group_enum"))
    conn.execute(sa.text("DROP TYPE IF EXISTS gender_enum"))
