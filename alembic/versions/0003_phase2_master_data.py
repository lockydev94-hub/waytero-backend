"""Phase 2 - Master data tables: countries, states, cities, system_configurations

Revision ID: 0003_phase2_master
Revises: 0002_phase1_rbac
Create Date: 2026-06-02

Doc Ref: DB Architecture Part 1 — Master Data (Sections 9, 10, 11, 12)
Phase: 2 — Master Data Module
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone

revision = "0003_phase2_master"
down_revision = "0002_phase1_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ============================================================
    # COUNTRIES
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS countries (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(150) NOT NULL,
            iso_code VARCHAR(10),
            phone_code VARCHAR(10),
            is_active BOOLEAN DEFAULT TRUE
        )
    """))

    # ============================================================
    # STATES
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS states (
            id BIGSERIAL PRIMARY KEY,
            country_id BIGINT NOT NULL REFERENCES countries(id),
            name VARCHAR(150) NOT NULL,
            state_code VARCHAR(20),
            is_active BOOLEAN DEFAULT TRUE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_states_country ON states(country_id)"))

    # ============================================================
    # CITIES — Doc Ref: Section 11 (pricing is city-based)
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS cities (
            id BIGSERIAL PRIMARY KEY,
            state_id BIGINT NOT NULL REFERENCES states(id),
            name VARCHAR(150) NOT NULL,
            city_code VARCHAR(50),
            is_active BOOLEAN DEFAULT TRUE
        )
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS idx_cities_state ON cities(state_id)"))

    # ============================================================
    # SYSTEM CONFIGURATIONS — Doc Ref: Section 12
    # ============================================================
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS system_configurations (
            id BIGSERIAL PRIMARY KEY,
            config_key VARCHAR(200) UNIQUE NOT NULL,
            config_value TEXT,
            description TEXT,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))

    # ============================================================
    # SEED — India + Odisha + major cities
    # ============================================================
    conn.execute(sa.text("""
        INSERT INTO countries (name, iso_code, phone_code, is_active)
        VALUES ('India', 'IN', '+91', TRUE)
        ON CONFLICT DO NOTHING
    """))

    conn.execute(sa.text("""
        INSERT INTO states (country_id, name, state_code, is_active)
        SELECT c.id, s.name, s.code, TRUE
        FROM countries c,
        (VALUES
            ('Odisha', 'OD'),
            ('Delhi', 'DL'),
            ('Maharashtra', 'MH'),
            ('Karnataka', 'KA'),
            ('Tamil Nadu', 'TN'),
            ('West Bengal', 'WB'),
            ('Rajasthan', 'RJ'),
            ('Gujarat', 'GJ'),
            ('Uttar Pradesh', 'UP'),
            ('Telangana', 'TG')
        ) AS s(name, code)
        WHERE c.iso_code = 'IN'
        ON CONFLICT DO NOTHING
    """))

    conn.execute(sa.text("""
        INSERT INTO cities (state_id, name, city_code, is_active)
        SELECT s.id, c.name, c.code, TRUE
        FROM states s,
        (VALUES
            ('Odisha', 'Bhubaneswar', 'BBS'),
            ('Odisha', 'Puri', 'PRI'),
            ('Odisha', 'Cuttack', 'CTK'),
            ('Odisha', 'Rourkela', 'RKL'),
            ('Odisha', 'Berhampur', 'BRH'),
            ('Delhi', 'New Delhi', 'DEL'),
            ('Maharashtra', 'Mumbai', 'MUM'),
            ('Maharashtra', 'Pune', 'PUN'),
            ('Karnataka', 'Bengaluru', 'BLR'),
            ('Tamil Nadu', 'Chennai', 'CHN'),
            ('West Bengal', 'Kolkata', 'KOL'),
            ('Rajasthan', 'Jaipur', 'JAI'),
            ('Gujarat', 'Ahmedabad', 'AMD'),
            ('Uttar Pradesh', 'Lucknow', 'LKO'),
            ('Telangana', 'Hyderabad', 'HYD')
        ) AS c(state, name, code)
        WHERE s.name = c.state
        ON CONFLICT DO NOTHING
    """))

    conn.execute(sa.text("""
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES
            ('GST_PERCENTAGE', '18', 'GST percentage applied on platform commission', NOW()),
            ('OTP_EXPIRY_MINUTES', '5', 'OTP validity in minutes', NOW()),
            ('MAX_LOGIN_ATTEMPTS', '5', 'Max failed login attempts before account lock', NOW()),
            ('BOOKING_CANCELLATION_HOURS', '2', 'Hours before trip for free cancellation', NOW()),
            ('WALLET_MINIMUM_BALANCE', '0', 'Minimum wallet balance for partner', NOW()),
            ('PLATFORM_COMMISSION_PERCENTAGE', '15', 'Default platform commission %', NOW())
        ON CONFLICT (config_key) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS system_configurations CASCADE"))
    conn.execute(sa.text("DROP TABLE IF EXISTS cities CASCADE"))
    conn.execute(sa.text("DROP TABLE IF EXISTS states CASCADE"))
    conn.execute(sa.text("DROP TABLE IF EXISTS countries CASCADE"))
