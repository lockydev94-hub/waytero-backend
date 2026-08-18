"""0056_tour_module — tour package catalog and fixed-package bookings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tour_packages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("partner_id", sa.BigInteger(), sa.ForeignKey("partners.id"), nullable=False),
        sa.Column("package_code", sa.String(50), nullable=False, unique=True),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("package_name", sa.String(255), nullable=False),
        sa.Column("package_type", sa.String(50), nullable=False, server_default="FIXED"),
        sa.Column("destination", sa.String(255), nullable=False),
        sa.Column("city_id", sa.BigInteger(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("duration_nights", sa.Integer(), nullable=False),
        sa.Column("minimum_persons", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("maximum_persons", sa.Integer()),
        sa.Column("short_description", sa.String(500)),
        sa.Column("description", sa.Text()),
        sa.Column("terms_and_conditions", sa.Text()),
        sa.Column("status", sa.String(50), nullable=False, server_default="DRAFT"),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tour_packages_status", "tour_packages", ["status"])
    op.create_index("idx_tour_packages_partner", "tour_packages", ["partner_id"])
    op.create_index("idx_tour_packages_city", "tour_packages", ["city_id"])

    op.create_table(
        "tour_itineraries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("package_id", sa.BigInteger(), sa.ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("overnight_city_id", sa.BigInteger(), sa.ForeignKey("cities.id")),
        sa.Column("activities", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("package_id", "day_number", name="uq_tour_itinerary_day"),
    )
    for table, column in (("tour_package_inclusions", "inclusion_text"), ("tour_package_exclusions", "exclusion_text")):
        op.create_table(
            table,
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("package_id", sa.BigInteger(), sa.ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False),
            sa.Column(column, sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    op.create_table(
        "tour_package_media",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("package_id", sa.BigInteger(), sa.ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("media_type", sa.String(20), nullable=False, server_default="IMAGE"),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("caption", sa.String(255)),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "tour_package_pricing",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("package_id", sa.BigInteger(), sa.ForeignKey("tour_packages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("persons_count", sa.Integer(), nullable=False),
        sa.Column("package_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("package_id", "persons_count", name="uq_tour_pricing_pax"),
    )
    op.create_table(
        "tour_bookings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("uuid", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("master_booking_id", sa.BigInteger(), sa.ForeignKey("master_bookings.id"), nullable=False),
        sa.Column("package_id", sa.BigInteger(), sa.ForeignKey("tour_packages.id"), nullable=False),
        sa.Column("booking_number", sa.String(100), nullable=False, unique=True),
        sa.Column("travel_start_date", sa.Date(), nullable=False),
        sa.Column("travel_end_date", sa.Date(), nullable=False),
        sa.Column("persons_count", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("platform_commission", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("partner_payout", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("payment_status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("booking_status", sa.String(50), nullable=False, server_default="PENDING_CONFIRMATION"),
        sa.Column("special_requests", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tour_bookings_status", "tour_bookings", ["booking_status"])
    op.create_index("idx_tour_bookings_travel_date", "tour_bookings", ["travel_start_date"])
    op.create_table(
        "tour_participants",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tour_booking_id", sa.BigInteger(), sa.ForeignKey("tour_bookings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("participant_name", sa.String(255), nullable=False),
        sa.Column("mobile", sa.String(15)),
        sa.Column("age", sa.Integer()),
        sa.Column("gender", sa.String(20)),
        sa.Column("id_type", sa.String(50)),
        sa.Column("id_number", sa.String(100)),
    )


def downgrade() -> None:
    op.drop_table("tour_participants")
    op.drop_index("idx_tour_bookings_travel_date", table_name="tour_bookings")
    op.drop_index("idx_tour_bookings_status", table_name="tour_bookings")
    op.drop_table("tour_bookings")
    op.drop_table("tour_package_pricing")
    op.drop_table("tour_package_media")
    op.drop_table("tour_package_exclusions")
    op.drop_table("tour_package_inclusions")
    op.drop_table("tour_itineraries")
    op.drop_index("idx_tour_packages_city", table_name="tour_packages")
    op.drop_index("idx_tour_packages_partner", table_name="tour_packages")
    op.drop_index("idx_tour_packages_status", table_name="tour_packages")
    op.drop_table("tour_packages")
