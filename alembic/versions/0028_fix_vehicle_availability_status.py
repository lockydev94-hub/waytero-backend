"""Fix vehicle availability: backfill ACTIVE vehicles to AVAILABLE status
   and ensure upsert on ACTIVE transition sets AVAILABLE.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-04

Doc Ref: DB Schema Part 3 §17 — vehicle_availability
"""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Create vehicle_availability rows for ACTIVE vehicles that don't have one yet
    conn.execute(sa.text("""
        INSERT INTO vehicle_availability (vehicle_id, availability_status, updated_at)
        SELECT v.id, 'AVAILABLE', NOW()
        FROM vehicles v
        LEFT JOIN vehicle_availability va ON va.vehicle_id = v.id
        WHERE v.status = 'ACTIVE'
          AND v.deleted_at IS NULL
          AND va.vehicle_id IS NULL
        ON CONFLICT DO NOTHING
    """))

    # 2. Update existing ACTIVE vehicle availability records that are incorrectly UNAVAILABLE
    #    (they were set to UNAVAILABLE by the old service code during ACTIVE transition)
    #    Only update if the vehicle is ACTIVE and NOT currently ON_TRIP
    conn.execute(sa.text("""
        UPDATE vehicle_availability va
        SET availability_status = 'AVAILABLE', updated_at = NOW()
        FROM vehicles v
        WHERE va.vehicle_id = v.id
          AND v.status = 'ACTIVE'
          AND v.deleted_at IS NULL
          AND va.availability_status = 'UNAVAILABLE'
    """))


def downgrade() -> None:
    # Revert AVAILABLE back to UNAVAILABLE for ACTIVE vehicles
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE vehicle_availability va
        SET availability_status = 'UNAVAILABLE', updated_at = NOW()
        FROM vehicles v
        WHERE va.vehicle_id = v.id
          AND v.status = 'ACTIVE'
          AND va.availability_status = 'AVAILABLE'
    """))
