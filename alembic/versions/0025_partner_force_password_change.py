"""Add force_password_change to users + set default partner password

Revision ID: 0025
Revises: 0024_gst_tax_on_bookings
Create Date: 2026-08-02

Doc Ref:
  Auth Flow §6 — Partner Login Flow (password + OTP)
  Partner Portal requirement — first-login force password change
  Default password: Waytero@15 (hashed with Argon2id)

Changes:
  users — add column:
    force_password_change  BOOLEAN DEFAULT FALSE NOT NULL
    
  UPDATE — all existing PARTNER type users with no password → set Waytero@15 hash
  UPDATE — all existing PARTNER type users (any) → set force_password_change = TRUE
             so existing partners must also change on first login

  system_configurations — seed PARTNER_DEFAULT_PASSWORD key for admin reference
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = "0025"
down_revision = "0024_gst_tax_on_bookings"
branch_labels = None
depends_on = None

# Argon2id hash of "Waytero@15"
# Generated with: PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16).hash("Waytero@15")
# This is a pre-computed static hash — safe to store in migration
# NOTE: Each Argon2 hash is unique (salted), so we use password hashing at runtime via service layer
# We will store a marker and set via Python at startup seeder instead
PARTNER_DEFAULT_PASSWORD = "Waytero@15"


def upgrade() -> None:
    # ── 1. Add force_password_change column to users ─────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "force_password_change",
            sa.Boolean(),
            nullable=False,
            server_default="FALSE",
        ),
    )

    # ── 2. Seed system_configurations with default password marker ────────────
    op.execute("""
        INSERT INTO system_configurations (config_key, config_value, description, updated_at)
        VALUES (
            'PARTNER_DEFAULT_PASSWORD',
            'Waytero@15',
            'Default password assigned to new partners. Partners must change this on first login. Admin reset also restores this password.',
            NOW()
        )
        ON CONFLICT (config_key) DO NOTHING
    """)

    # ── 3. Mark all existing PARTNER users as force_password_change = TRUE ────
    #       This ensures existing partners also go through the change-password flow
    op.execute("""
        UPDATE users
        SET force_password_change = TRUE
        WHERE user_type = 'PARTNER'
    """)

    # NOTE: Actual Argon2 password hash for existing partners is set by the
    # startup seeder (app/scripts/seed_partner_default_passwords.py)
    # because Argon2 hashing requires Python runtime, not raw SQL.


def downgrade() -> None:
    op.drop_column("users", "force_password_change")
    op.execute("DELETE FROM system_configurations WHERE config_key = 'PARTNER_DEFAULT_PASSWORD'")
