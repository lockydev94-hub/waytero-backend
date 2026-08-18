"""Vehicle Verification System

Revision ID: 0017_vehicle_verification
Revises: 0016_partner_office_address_gst
Create Date: 2026-07-29

Doc Ref:
  DB Schema Part 3 §12-15 — vehicles, vehicle_documents
  BRD — Vehicle Verification Flow: PENDING → UNDER_REVIEW → APPROVED → ACTIVE
  Admin user types: ADMIN | SUPER_ADMIN | VERIFICATION_OFFICER

Creates:
  vehicle_verification_assignments — assign verification officer to a vehicle
  vehicle_photo_uploads            — multiple categorized vehicle photos

Adds columns to vehicles:
  assigned_officer_id              — UUID FK to users (nullable)
  verified_by                      — UUID FK to users
  verification_remarks             — admin/officer remarks

Adds verified_by, verified_at to vehicle_documents (already has verification_status).

Seeding: No default rows — safe ON CONFLICT DO NOTHING on restart.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_vehicle_verification"
down_revision = "0016_partner_office_address_gst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Add verification columns to vehicles ──────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE vehicles
            ADD COLUMN IF NOT EXISTS assigned_officer_id UUID REFERENCES users(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS verified_by         UUID REFERENCES users(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS verification_remarks TEXT
    """))

    # ── 2. Add verified_by to vehicle_documents ──────────────────────────────
    conn.execute(sa.text("""
        ALTER TABLE vehicle_documents
            ADD COLUMN IF NOT EXISTS verified_by UUID REFERENCES users(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS remarks      TEXT
    """))

    # ── 3. vehicle_verification_assignments ──────────────────────────────────
    # Tracks assignment history (who assigned whom, when)
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_verification_assignments (
            id              BIGSERIAL PRIMARY KEY,
            vehicle_id      BIGINT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            officer_id      UUID   NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
            assigned_by     UUID   NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
            assigned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            unassigned_at   TIMESTAMPTZ,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE
        )
    """))

    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_vva_vehicle ON vehicle_verification_assignments(vehicle_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_vva_officer ON vehicle_verification_assignments(officer_id)"
    ))

    # ── 4. vehicle_photo_uploads ─────────────────────────────────────────────
    # Separate table for multi-photo uploads (beyond documents)
    # photo_type: FRONT | BACK | LEFT | RIGHT | INTERIOR | ODOMETER | ENGINE | OTHER
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS vehicle_photo_uploads (
            id                  BIGSERIAL PRIMARY KEY,
            vehicle_id          BIGINT  NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            photo_type          VARCHAR(50) NOT NULL,
            file_url            TEXT NOT NULL,
            caption             VARCHAR(255),
            verification_status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
            uploaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            verified_at         TIMESTAMPTZ,
            verified_by         UUID REFERENCES users(id) ON DELETE SET NULL,
            remarks             TEXT
        )
    """))

    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_vpu_vehicle ON vehicle_photo_uploads(vehicle_id)"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS vehicle_photo_uploads CASCADE"))
    conn.execute(sa.text("DROP TABLE IF EXISTS vehicle_verification_assignments CASCADE"))
    conn.execute(sa.text("""
        ALTER TABLE vehicle_documents
            DROP COLUMN IF EXISTS verified_by,
            DROP COLUMN IF EXISTS remarks
    """))
    conn.execute(sa.text("""
        ALTER TABLE vehicles
            DROP COLUMN IF EXISTS assigned_officer_id,
            DROP COLUMN IF EXISTS verified_by,
            DROP COLUMN IF EXISTS verification_remarks
    """))
