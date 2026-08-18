"""Phase 2 - Partner tables

Revision ID: 0005_phase2_partner
Revises: 0004_phase2_customer
Create Date: 2026-06-02

Doc Ref: DB Schema Part 2 — Partner (Sections 4-19)
Phase: 2 — Partner Module
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_phase2_partner"
down_revision = "0004_phase2_customer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ============================================================
    # PARTNERS — Doc Ref: Section 4
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partners (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            partner_code VARCHAR(50) NOT NULL UNIQUE,
            partner_type VARCHAR(50) NOT NULL,
            business_name VARCHAR(255),
            owner_name VARCHAR(255) NOT NULL,
            mobile VARCHAR(15) NOT NULL,
            email VARCHAR(255),
            city_id BIGINT NOT NULL REFERENCES cities(id),
            status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            onboarding_source VARCHAR(100),
            approved_at TIMESTAMP WITH TIME ZONE,
            approved_by UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMP WITH TIME ZONE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_city ON partners(city_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_status ON partners(status)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_type ON partners(partner_type)"))

    # ============================================================
    # PARTNER SERVICES — Doc Ref: Section 7
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_services (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            service_type VARCHAR(50) NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_service ON partner_services(service_type)"))
    conn.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS idx_partner_service_unique ON partner_services(partner_id, service_type)"))

    # ============================================================
    # PARTNER DOCUMENTS — Doc Ref: Section 9
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_documents (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            document_type VARCHAR(100) NOT NULL,
            file_url TEXT NOT NULL,
            file_hash VARCHAR(255),
            verification_status VARCHAR(50) DEFAULT 'PENDING',
            remarks TEXT,
            uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            verified_at TIMESTAMP WITH TIME ZONE,
            verified_by UUID,
            expiry_date DATE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_doc_status ON partner_documents(verification_status)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_doc_partner ON partner_documents(partner_id)"))

    # ============================================================
    # PARTNER GST DETAILS — Doc Ref: Section 11
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_gst_details (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL UNIQUE REFERENCES partners(id) ON DELETE CASCADE,
            gst_number VARCHAR(20) UNIQUE,
            legal_name VARCHAR(255),
            trade_name VARCHAR(255),
            registration_date DATE,
            gst_status VARCHAR(50),
            verified_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # PARTNER BANK ACCOUNTS — Doc Ref: Section 12
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_bank_accounts (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            account_holder_name VARCHAR(255),
            account_number_encrypted TEXT,
            ifsc_code VARCHAR(20),
            bank_name VARCHAR(255),
            branch_name VARCHAR(255),
            is_primary BOOLEAN DEFAULT FALSE,
            verification_status VARCHAR(50) DEFAULT 'PENDING',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_partner_bank ON partner_bank_accounts(partner_id)"))

    # ============================================================
    # PARTNER VERIFICATION LOGS — Doc Ref: Section 13
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_verification_logs (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
            action VARCHAR(100),
            remarks TEXT,
            performed_by UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # COMMISSION GROUPS — Doc Ref: Section 14
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS commission_groups (
            id BIGSERIAL PRIMARY KEY,
            group_name VARCHAR(150) NOT NULL UNIQUE,
            description TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # COMMISSION RULES — Doc Ref: Section 15
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS commission_rules (
            id BIGSERIAL PRIMARY KEY,
            commission_group_id BIGINT NOT NULL REFERENCES commission_groups(id),
            service_type VARCHAR(50),
            commission_type VARCHAR(50),
            commission_value NUMERIC(12,2),
            city_id BIGINT REFERENCES cities(id),
            effective_from DATE,
            effective_to DATE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_commission_city ON commission_rules(city_id)"))

    # ============================================================
    # PARTNER COMMISSION MAPPING — Doc Ref: Section 17
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_commission_groups (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL REFERENCES partners(id),
            commission_group_id BIGINT NOT NULL REFERENCES commission_groups(id),
            assigned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            assigned_by UUID
        )
    """))

    # ============================================================
    # PARTNER RATINGS — Doc Ref: Section 18
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_ratings (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL UNIQUE REFERENCES partners(id),
            average_rating NUMERIC(3,2) DEFAULT 0.00,
            total_reviews INTEGER DEFAULT 0,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # PARTNER PERFORMANCE SUMMARY — Doc Ref: Section 19
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS partner_performance_summary (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL UNIQUE REFERENCES partners(id),
            completed_bookings INTEGER DEFAULT 0,
            cancelled_bookings INTEGER DEFAULT 0,
            acceptance_rate NUMERIC(5,2) DEFAULT 0.00,
            completion_rate NUMERIC(5,2) DEFAULT 0.00,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # SEED — default commission group
    # ============================================================
    conn.execute(sa.text("""
        INSERT INTO commission_groups (group_name, description, is_active)
        VALUES ('DEFAULT', 'Default commission group for all new partners', TRUE)
        ON CONFLICT (group_name) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()
    for t in [
        "partner_performance_summary", "partner_ratings", "partner_commission_groups",
        "commission_rules", "commission_groups", "partner_verification_logs",
        "partner_bank_accounts", "partner_gst_details", "partner_documents",
        "partner_services", "partners"
    ]:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {t} CASCADE"))
