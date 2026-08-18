"""Phase 3 - Booking Engine tables

Revision ID: 0007_phase3_booking
Revises: 0006_phase2_driver_vehicle
Create Date: 2026-06-02

Doc Ref: DB Schema Part 4 — Booking Engine (Sections 3-19)
Phase: 3 — Booking Module
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_phase3_booking"
down_revision = "0006_phase2_driver_vehicle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS master_bookings (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE,
            booking_number VARCHAR(100) UNIQUE NOT NULL,
            customer_id BIGINT NOT NULL REFERENCES customers(id),
            city_id BIGINT NOT NULL REFERENCES cities(id),
            booking_status VARCHAR(50) NOT NULL DEFAULT 'DRAFT',
            payment_status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            total_amount NUMERIC(12,2) DEFAULT 0,
            total_paid_amount NUMERIC(12,2) DEFAULT 0,
            total_refund_amount NUMERIC(12,2) DEFAULT 0,
            journey_start_date DATE,
            journey_end_date DATE,
            remarks TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_customer ON master_bookings(customer_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_status ON master_bookings(booking_status)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_city ON master_bookings(city_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_created ON master_bookings(created_at)
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_services (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            service_type VARCHAR(50) NOT NULL,
            service_reference_id BIGINT NOT NULL,
            service_status VARCHAR(50),
            service_amount NUMERIC(12,2),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_service_type ON booking_services(service_type)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_service_mbid ON booking_services(master_booking_id)
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS cab_bookings (
            id BIGSERIAL PRIMARY KEY,
            uuid UUID NOT NULL UNIQUE,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            booking_number VARCHAR(100) UNIQUE NOT NULL,
            trip_type VARCHAR(50),
            vehicle_category_id BIGINT REFERENCES vehicle_categories(id),
            pickup_location TEXT,
            pickup_latitude NUMERIC(10,7),
            pickup_longitude NUMERIC(10,7),
            drop_location TEXT,
            drop_latitude NUMERIC(10,7),
            drop_longitude NUMERIC(10,7),
            pickup_datetime TIMESTAMP WITH TIME ZONE,
            estimated_distance NUMERIC(12,2),
            estimated_amount NUMERIC(12,2),
            final_amount NUMERIC(12,2),
            booking_status VARCHAR(50) DEFAULT 'PENDING_ASSIGNMENT',
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cab_booking_status ON cab_bookings(booking_status)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cab_booking_mbid ON cab_bookings(master_booking_id)
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS cab_booking_assignments (
            id BIGSERIAL PRIMARY KEY,
            cab_booking_id BIGINT NOT NULL REFERENCES cab_bookings(id),
            partner_id BIGINT NOT NULL REFERENCES partners(id),
            vehicle_id BIGINT REFERENCES vehicles(id),
            driver_id BIGINT REFERENCES drivers(id),
            assigned_by UUID,
            assigned_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            assignment_type VARCHAR(50) DEFAULT 'MANUAL'
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cab_assign_cab ON cab_booking_assignments(cab_booking_id)
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_cab_assign_driver ON cab_booking_assignments(driver_id)
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_timelines (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            event_type VARCHAR(100),
            event_description TEXT,
            event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            created_by UUID
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_timeline_booking ON booking_timelines(master_booking_id)
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_events (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL,
            event_name VARCHAR(100),
            event_payload JSONB,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_booking_events_mbid ON booking_events(master_booking_id)
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_cancellations (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            cancelled_by UUID,
            cancellation_reason TEXT,
            cancellation_charge NUMERIC(12,2) DEFAULT 0,
            refund_amount NUMERIC(12,2) DEFAULT 0,
            cancelled_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_reviews (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            customer_id BIGINT NOT NULL REFERENCES customers(id),
            rating INTEGER CHECK (rating BETWEEN 1 AND 5),
            review TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_contacts (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            contact_name VARCHAR(255),
            mobile VARCHAR(15),
            email VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_documents (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            document_type VARCHAR(100),
            file_url TEXT,
            generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS booking_notes (
            id BIGSERIAL PRIMARY KEY,
            master_booking_id BIGINT NOT NULL REFERENCES master_bookings(id),
            note TEXT,
            created_by UUID,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        );
    """))


def downgrade() -> None:
    conn = op.get_bind()
    for t in [
        "booking_notes", "booking_documents", "booking_contacts",
        "booking_reviews", "booking_cancellations", "booking_events",
        "booking_timelines", "cab_booking_assignments", "cab_bookings",
        "booking_services", "master_bookings",
    ]:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {t} CASCADE"))
