"""Phase 2 - Driver and Vehicle tables

Revision ID: 0006_phase2_driver_vehicle
Revises: 0005_phase2_partner
Create Date: 2026-06-02

Doc Ref: DB Schema Part 3 — Driver + Vehicle (Sections 2-23)
Phase: 2 — Driver + Vehicle Module
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_phase2_driver_vehicle"
down_revision = "0005_phase2_partner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ============================================================
    # VEHICLE CATEGORIES — Doc Ref: Section 10 (must come before vehicles)
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_categories (
            id BIGSERIAL PRIMARY KEY,
            category_name VARCHAR(100) NOT NULL UNIQUE,
            seating_capacity INTEGER,
            luggage_capacity INTEGER,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # Seed default categories — Doc Ref: Section 11
    conn.execute(sa.text("""
        INSERT INTO vehicle_categories (category_name, seating_capacity, luggage_capacity, is_active)
        VALUES
            ('HATCHBACK', 4, 1, TRUE),
            ('SEDAN', 4, 2, TRUE),
            ('SUV', 6, 3, TRUE),
            ('MUV', 7, 3, TRUE),
            ('TEMPO_TRAVELLER', 12, 5, TRUE),
            ('MINI_BUS', 20, 8, TRUE),
            ('LUXURY', 4, 2, TRUE)
        ON CONFLICT (category_name) DO NOTHING
    """))

    # ============================================================
    # DRIVERS — Doc Ref: Section 2
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS drivers (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            user_id UUID UNIQUE REFERENCES users(id) ON DELETE SET NULL,
            partner_id BIGINT NOT NULL REFERENCES partners(id),
            driver_code VARCHAR(50) NOT NULL UNIQUE,
            full_name VARCHAR(255) NOT NULL,
            mobile VARCHAR(15) NOT NULL UNIQUE,
            email VARCHAR(255),
            license_number VARCHAR(100) NOT NULL,
            license_expiry_date DATE,
            date_of_birth DATE,
            joining_date DATE,
            status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            approved_at TIMESTAMP WITH TIME ZONE,
            approved_by UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMP WITH TIME ZONE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_driver_partner ON drivers(partner_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_driver_status ON drivers(status)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_driver_mobile ON drivers(mobile)"))

    # ============================================================
    # DRIVER DOCUMENTS — Doc Ref: Section 4
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS driver_documents (
            id BIGSERIAL PRIMARY KEY,
            driver_id BIGINT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
            document_type VARCHAR(100) NOT NULL,
            file_url TEXT NOT NULL,
            file_hash VARCHAR(255),
            verification_status VARCHAR(50) DEFAULT 'PENDING',
            expiry_date DATE,
            remarks TEXT,
            uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            verified_at TIMESTAMP WITH TIME ZONE,
            verified_by UUID
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_driver_doc_driver ON driver_documents(driver_id)"))

    # ============================================================
    # DRIVER AVAILABILITY — Doc Ref: Section 6
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS driver_availability (
            id BIGSERIAL PRIMARY KEY,
            driver_id BIGINT NOT NULL UNIQUE REFERENCES drivers(id) ON DELETE CASCADE,
            availability_status VARCHAR(50) DEFAULT 'OFFLINE',
            last_online_at TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # DRIVER PERFORMANCE SUMMARY — Doc Ref: Section 22
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS driver_performance_summary (
            id BIGSERIAL PRIMARY KEY,
            driver_id BIGINT NOT NULL UNIQUE REFERENCES drivers(id),
            completed_trips INTEGER DEFAULT 0,
            cancelled_trips INTEGER DEFAULT 0,
            acceptance_rate NUMERIC(5,2) DEFAULT 0.00,
            average_rating NUMERIC(3,2) DEFAULT 0.00,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # VEHICLES — Doc Ref: Section 12
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            partner_id BIGINT NOT NULL REFERENCES partners(id),
            vehicle_category_id BIGINT NOT NULL REFERENCES vehicle_categories(id),
            vehicle_code VARCHAR(50) UNIQUE,
            registration_number VARCHAR(50) NOT NULL UNIQUE,
            vehicle_brand VARCHAR(100),
            vehicle_model VARCHAR(100),
            manufacturing_year INTEGER,
            fuel_type VARCHAR(50),
            seating_capacity INTEGER,
            city_id BIGINT NOT NULL REFERENCES cities(id),
            status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            approved_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMP WITH TIME ZONE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_vehicle_partner ON vehicles(partner_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_vehicle_city ON vehicles(city_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_vehicle_status ON vehicles(status)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_vehicle_category ON vehicles(vehicle_category_id)"))

    # ============================================================
    # VEHICLE DOCUMENTS — Doc Ref: Section 14
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_documents (
            id BIGSERIAL PRIMARY KEY,
            vehicle_id BIGINT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            document_type VARCHAR(100),
            file_url TEXT,
            expiry_date DATE,
            verification_status VARCHAR(50) DEFAULT 'PENDING',
            uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            verified_at TIMESTAMP WITH TIME ZONE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_vehicle_doc_vehicle ON vehicle_documents(vehicle_id)"))

    # ============================================================
    # DRIVER-VEHICLE ASSIGNMENT — Doc Ref: Section 16
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS driver_vehicle_assignments (
            id BIGSERIAL PRIMARY KEY,
            driver_id BIGINT NOT NULL REFERENCES drivers(id),
            vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
            assigned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            unassigned_at TIMESTAMP WITH TIME ZONE,
            is_active BOOLEAN DEFAULT TRUE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_dva_driver ON driver_vehicle_assignments(driver_id)"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_dva_vehicle ON driver_vehicle_assignments(vehicle_id)"))

    # ============================================================
    # VEHICLE AVAILABILITY — Doc Ref: Section 17
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_availability (
            id BIGSERIAL PRIMARY KEY,
            vehicle_id BIGINT NOT NULL UNIQUE REFERENCES vehicles(id) ON DELETE CASCADE,
            availability_status VARCHAR(50) DEFAULT 'UNAVAILABLE',
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # VEHICLE PRICING RULES — Doc Ref: Section 18
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_pricing_rules (
            id BIGSERIAL PRIMARY KEY,
            city_id BIGINT NOT NULL REFERENCES cities(id),
            vehicle_category_id BIGINT NOT NULL REFERENCES vehicle_categories(id),
            trip_type VARCHAR(50),
            base_fare NUMERIC(12,2),
            minimum_km INTEGER,
            per_km_rate NUMERIC(12,2),
            driver_allowance NUMERIC(12,2),
            night_charge NUMERIC(12,2),
            effective_from DATE,
            effective_to DATE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_pricing_city_cat ON vehicle_pricing_rules(city_id, vehicle_category_id)"))

    # ============================================================
    # VEHICLE MAINTENANCE — Doc Ref: Section 20
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_maintenance (
            id BIGSERIAL PRIMARY KEY,
            vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
            maintenance_type VARCHAR(100),
            service_date DATE,
            next_due_date DATE,
            cost NUMERIC(12,2),
            remarks TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # VEHICLE PERFORMANCE SUMMARY — Doc Ref: Section 23
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_performance_summary (
            id BIGSERIAL PRIMARY KEY,
            vehicle_id BIGINT NOT NULL UNIQUE REFERENCES vehicles(id),
            total_trips INTEGER DEFAULT 0,
            total_distance NUMERIC(12,2) DEFAULT 0.00,
            average_rating NUMERIC(3,2) DEFAULT 0.00,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))


def downgrade() -> None:
    conn = op.get_bind()
    for t in [
        "vehicle_performance_summary", "vehicle_maintenance", "vehicle_pricing_rules",
        "vehicle_availability", "driver_vehicle_assignments", "vehicle_documents",
        "vehicles", "driver_performance_summary", "driver_availability",
        "driver_documents", "drivers", "vehicle_categories"
    ]:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {t} CASCADE"))
