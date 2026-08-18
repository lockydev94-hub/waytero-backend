"""Coupon System — full advanced coupon engine

Revision ID: 0020_coupon_system
Revises: 0019_service_types_table
Create Date: 2026-07-30

Creates tables:
  coupons               — master coupon definitions
  coupon_service_rules  — service-type / cab-category restrictions
  coupon_city_rules     — city-wise discount cap protection
  coupon_customer_rules — customer-specific (mobile) whitelist
  coupon_usages         — audit trail of redemptions

Business Rules Implemented:
  1. Coupon can apply to ALL services OR specific service types.
  2. For CAB service, can restrict to specific vehicle categories.
  3. Discount amount cannot exceed the default service price.
  4. For city-restricted coupons, discount cannot exceed city base price.
  5. Customer-specific coupons: only whitelisted mobiles can redeem.
  6. Per-user usage limit and global usage limit enforced.

Seeds:
  No default coupons seeded — admin creates them.
"""

import sqlalchemy as sa
from alembic import op
from datetime import datetime, timezone

revision = "0020_coupon_system"
down_revision = "0019_service_types_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── coupons (master) ─────────────────────────────────────────
    op.create_table(
        "coupons",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("coupon_code", sa.String(50), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),

        # Scope
        sa.Column("apply_to", sa.String(50), nullable=False, server_default="ALL"),
        # ALL | CAB | HOTEL | TOUR

        # Discount
        sa.Column("discount_type", sa.String(20), nullable=False),
        # PERCENTAGE | FLAT
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("max_discount_amount", sa.Numeric(12, 2), nullable=True),
        # For PERCENTAGE coupons: cap the rupee discount (e.g. 20% but max ₹500)

        # Minimum order guard
        sa.Column("min_booking_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),

        # Usage limits
        sa.Column("max_usage_total", sa.Integer, nullable=True),  # NULL = unlimited
        sa.Column("max_usage_per_customer", sa.Integer, nullable=False, server_default="1"),
        sa.Column("current_usage_count", sa.Integer, nullable=False, server_default="0"),

        # Validity
        sa.Column("valid_from", sa.Date, nullable=False),
        sa.Column("valid_to", sa.Date, nullable=False),

        # Customer restriction flag
        sa.Column("is_customer_specific", sa.Boolean, nullable=False, server_default="false"),
        # When true, only mobiles listed in coupon_customer_rules can redeem

        # City restriction flag
        sa.Column("is_city_specific", sa.Boolean, nullable=False, server_default="false"),
        # When true, only bookings in cities listed in coupon_city_rules are eligible

        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),

        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("idx_coupon_code", "coupons", ["coupon_code"])
    op.create_index("idx_coupon_active", "coupons", ["is_active"])
    op.create_index("idx_coupon_validity", "coupons", ["valid_from", "valid_to"])

    # ── coupon_service_rules ─────────────────────────────────────
    # Rows here restrict coupon to specific service_type + optional vehicle_category_id
    op.create_table(
        "coupon_service_rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("coupon_id", sa.BigInteger,
                  sa.ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_type", sa.String(50), nullable=False),
        # CAB | HOTEL | TOUR
        sa.Column("vehicle_category_id", sa.BigInteger,
                  sa.ForeignKey("vehicle_categories.id", ondelete="CASCADE"), nullable=True),
        # NULL = applies to all vehicle categories for this service_type
    )
    op.create_index("idx_csr_coupon", "coupon_service_rules", ["coupon_id"])

    # ── coupon_city_rules ─────────────────────────────────────────
    # City-specific eligibility + discount cap derived from city pricing
    op.create_table(
        "coupon_city_rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("coupon_id", sa.BigInteger,
                  sa.ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("city_id", sa.BigInteger,
                  sa.ForeignKey("cities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("max_discount_override", sa.Numeric(12, 2), nullable=True),
        # Admin can set a city-specific cap (e.g. discount cannot exceed city base_fare)
        # NULL = use global max_discount_amount from coupons table
    )
    op.create_index("idx_ccr_coupon", "coupon_city_rules", ["coupon_id"])
    op.create_index("idx_ccr_city", "coupon_city_rules", ["city_id"])

    # ── coupon_customer_rules ─────────────────────────────────────
    # Whitelist of mobile numbers for customer-specific coupons
    op.create_table(
        "coupon_customer_rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("coupon_id", sa.BigInteger,
                  sa.ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mobile_number", sa.String(15), nullable=False),
        sa.Column("customer_id", sa.BigInteger,
                  sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        # Resolved at redemption time if customer exists
    )
    op.create_index("idx_ccust_coupon", "coupon_customer_rules", ["coupon_id"])
    op.create_index("idx_ccust_mobile", "coupon_customer_rules", ["mobile_number"])

    # ── coupon_usages ─────────────────────────────────────────────
    op.create_table(
        "coupon_usages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("coupon_id", sa.BigInteger,
                  sa.ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.BigInteger,
                  sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("master_booking_id", sa.BigInteger,
                  sa.ForeignKey("master_bookings.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("discount_applied", sa.Numeric(12, 2), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("idx_cu_coupon", "coupon_usages", ["coupon_id"])
    op.create_index("idx_cu_customer", "coupon_usages", ["customer_id"])


def downgrade() -> None:
    op.drop_table("coupon_usages")
    op.drop_table("coupon_customer_rules")
    op.drop_table("coupon_city_rules")
    op.drop_table("coupon_service_rules")
    op.drop_table("coupons")
