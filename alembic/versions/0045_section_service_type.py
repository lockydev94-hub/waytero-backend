"""0045_section_service_type — add service_type column to section_variants

Revision ID: 0045_section_service_type
Revises: 0044_website_cms
Create Date: 2026-08-10

Doc Ref:
  BRD Part 6 §155 — homepage admin control (hero binds to service type)
  Docs/04_API_Documentation/12_ADMIN_API.md §25 (Settings)

Why:
  The hero variant needs to know which service type it promotes so the
  customer-web frontend can inject the matching search form (CAB search,
  HOTEL search, TOUR search). The service_type column also drives which
  destination page the search submits to.

  This migration is idempotent — ADD COLUMN IF NOT EXISTS. Re-running it
  is a no-op. The CHECK constraint is also created with IF NOT EXISTS
  via DO block, matching the project migration style.
"""

import sqlalchemy as sa
from alembic import op


revision = "0045_section_service_type"
down_revision = "0044_website_cms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add the column.
    conn.execute(
        sa.text(
            """
            ALTER TABLE section_variants
                ADD COLUMN IF NOT EXISTS service_type VARCHAR(20)
            """
        )
    )

    # 2. CHECK constraint — CAB | HOTEL | TOUR | ALL or NULL.
    #    Postgres doesn't support ADD CONSTRAINT IF NOT EXISTS, so check
    #    pg_constraint first to keep the migration re-runnable.
    conn.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_variants_service_type'
                ) THEN
                    ALTER TABLE section_variants
                        ADD CONSTRAINT chk_variants_service_type
                        CHECK (
                            service_type IS NULL
                            OR service_type IN ('CAB','HOTEL','TOUR','ALL')
                        );
                END IF;
            END $$
            """
        )
    )

    # 3. Index — powers the "find the active hero for CAB" query.
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_variants_service_type
                ON section_variants (service_type)
                WHERE service_type IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_variants_service_type"))
    conn.execute(
        sa.text("ALTER TABLE section_variants DROP CONSTRAINT IF EXISTS chk_variants_service_type")
    )
    conn.execute(sa.text("ALTER TABLE section_variants DROP COLUMN IF EXISTS service_type"))
