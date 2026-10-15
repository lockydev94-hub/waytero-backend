"""Coupon discount_type domain CHECK. Doc Ref: BRD Part 6 §129; finding A18."""

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                           WHERE conname = 'ck_coupons_discount_type'
                             AND conrelid = 'coupons'::regclass)
            THEN ALTER TABLE coupons
                ADD CONSTRAINT ck_coupons_discount_type
                CHECK (discount_type IN ('FLAT', 'PERCENTAGE'));
            END IF;
        END $$
    """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE coupons DROP CONSTRAINT IF EXISTS ck_coupons_discount_type"
    )
