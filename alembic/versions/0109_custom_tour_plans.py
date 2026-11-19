# ============================================================
# WAYTERO — CUSTOM (PRIVATE) TOUR PLANS
# File: alembic/versions/0109_custom_tour_plans.py
#
# Admin-built tour plans that belong to one customer and are never
# published to the website. The package row keeps the normal catalog
# fields so bookings, partner acceptance, invoices and PDFs continue to
# work unchanged. The rich planning data (day-wise places, KM, optional
# hotels, cost estimate) lives in `custom_plan` JSONB so the plan can
# evolve without a migration for every new planning field.
#
# Doc Ref: BRD Part 5 §6 — tour package management
#          BRD Part 3 §45 — advance collection & settlement custody
# ============================================================
from alembic import op
import sqlalchemy as sa

revision = "0109"
down_revision = "0108"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            ALTER TABLE tour_packages
                ADD COLUMN IF NOT EXISTS is_custom BOOLEAN NOT NULL DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS custom_customer_id BIGINT,
                ADD COLUMN IF NOT EXISTS custom_plan JSONB,
                ADD COLUMN IF NOT EXISTS estimated_cost NUMERIC(14, 2)
                    NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS quoted_price NUMERIC(14, 2)
            """
        )
    )

    # Add the customer FK separately so re-running the migration after a
    # partial failure is safe. The plan is private to this customer; deleting
    # the customer must not delete the package/booking history.
    conn.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'fk_tour_packages_custom_customer'
                ) THEN
                    ALTER TABLE tour_packages
                        ADD CONSTRAINT fk_tour_packages_custom_customer
                        FOREIGN KEY (custom_customer_id)
                        REFERENCES customers(id)
                        ON DELETE SET NULL;
                END IF;
            END $$
            """
        )
    )

    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_tour_packages_custom_customer "
            "ON tour_packages (custom_customer_id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_tour_packages_is_custom "
            "ON tour_packages (is_custom)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_tour_packages_custom_customer"))
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_tour_packages_is_custom"))
    conn.execute(
        sa.text(
            "ALTER TABLE tour_packages "
            "DROP CONSTRAINT IF EXISTS fk_tour_packages_custom_customer"
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE tour_packages
                DROP COLUMN IF EXISTS quoted_price,
                DROP COLUMN IF EXISTS estimated_cost,
                DROP COLUMN IF EXISTS custom_plan,
                DROP COLUMN IF EXISTS custom_customer_id,
                DROP COLUMN IF EXISTS is_custom
            """
        )
    )
