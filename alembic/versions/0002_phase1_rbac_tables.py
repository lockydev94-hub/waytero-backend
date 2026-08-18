"""Phase 1 - RBAC tables and seed data

Revision ID: 0002_phase1_rbac
Revises: 0001_phase1_auth
Create Date: 2026-06-01
"""

from datetime import datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "0002_phase1_rbac"
down_revision = "0001_phase1_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("role_code", sa.String(length=50), nullable=False),
        sa.Column("role_name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system_role", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint("uq_roles_role_code", "roles", ["role_code"])
    op.create_index("ix_roles_role_code", "roles", ["role_code"])

    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("permission_code", sa.String(length=100), nullable=False),
        sa.Column("permission_name", sa.String(length=255), nullable=False),
        sa.Column("module_name", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_unique_constraint("uq_permissions_permission_code", "permissions", ["permission_code"])
    op.create_index("ix_permissions_permission_code", "permissions", ["permission_code"])

    op.create_table(
        "user_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
    )
    op.create_unique_constraint("uq_user_roles_user_role", "user_roles", ["user_id", "role_id"])
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    op.create_table(
        "role_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"], ondelete="CASCADE"),
    )
    op.create_unique_constraint(
        "uq_role_permissions_role_permission",
        "role_permissions",
        ["role_id", "permission_id"],
    )
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"])
    op.create_index("ix_role_permissions_permission_id", "role_permissions", ["permission_id"])

    now = datetime.now(timezone.utc)

    roles_table = sa.table(
        "roles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("role_code", sa.String()),
        sa.column("role_name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("is_system_role", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    permissions_table = sa.table(
        "permissions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("permission_code", sa.String()),
        sa.column("permission_name", sa.String()),
        sa.column("module_name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    role_permissions_table = sa.table(
        "role_permissions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("role_id", postgresql.UUID(as_uuid=True)),
        sa.column("permission_id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    user_roles_table = sa.table(
        "user_roles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("user_id", postgresql.UUID(as_uuid=True)),
        sa.column("role_id", postgresql.UUID(as_uuid=True)),
        sa.column("assigned_by", postgresql.UUID(as_uuid=True)),
        sa.column("assigned_at", sa.DateTime(timezone=True)),
    )

    roles = {
        "CUSTOMER": "Customer",
        "PARTNER": "Partner",
        "DRIVER": "Driver",
        "CCO": "Customer Care Officer",
        "VERIFICATION_OFFICER": "Verification Officer",
        "FINANCE_MANAGER": "Finance Manager",
        "ADMIN": "Administrator",
        "SUPER_ADMIN": "Super Administrator",
    }
    role_rows = []
    role_ids = {}
    for role_code, role_name in roles.items():
        role_id = uuid.uuid4()
        role_ids[role_code] = role_id
        role_rows.append(
            {
                "id": role_id,
                "role_code": role_code,
                "role_name": role_name,
                "description": f"System role for {role_name}",
                "is_system_role": True,
                "created_at": now,
                "updated_at": now,
            }
        )
    op.bulk_insert(roles_table, role_rows)

    permissions = {
        "auth.otp.send": ("Send OTP", "auth"),
        "auth.otp.verify": ("Verify OTP", "auth"),
        "auth.self.read": ("Read own profile", "auth"),
        "auth.password.change": ("Change own password", "auth"),
        "auth.password.reset": ("Reset password", "auth"),
        "auth.sessions.read": ("Read own sessions", "auth"),
        "auth.sessions.revoke": ("Revoke own sessions", "auth"),
        "portal.customer": ("Access customer channels", "auth"),
        "portal.partner": ("Access partner portal", "auth"),
        "portal.driver": ("Access captain app", "auth"),
        "portal.verification": ("Access verification tools", "auth"),
        "portal.finance": ("Access finance tools", "auth"),
        "portal.operations": ("Access CCO tools", "auth"),
        "portal.admin": ("Access admin portal", "auth"),
        "rbac.manage": ("Manage RBAC mappings", "auth"),
    }
    permission_rows = []
    permission_ids = {}
    for permission_code, (permission_name, module_name) in permissions.items():
        permission_id = uuid.uuid4()
        permission_ids[permission_code] = permission_id
        permission_rows.append(
            {
                "id": permission_id,
                "permission_code": permission_code,
                "permission_name": permission_name,
                "module_name": module_name,
                "description": f"Permission to {permission_name.lower()}",
                "created_at": now,
            }
        )
    op.bulk_insert(permissions_table, permission_rows)

    role_permission_map = {
        "CUSTOMER": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.customer",
        ],
        "PARTNER": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.password.change",
            "auth.password.reset",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.partner",
        ],
        "DRIVER": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.driver",
        ],
        "CCO": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.password.change",
            "auth.password.reset",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.operations",
        ],
        "VERIFICATION_OFFICER": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.password.change",
            "auth.password.reset",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.verification",
        ],
        "FINANCE_MANAGER": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.password.change",
            "auth.password.reset",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.finance",
        ],
        "ADMIN": [
            "auth.otp.send",
            "auth.otp.verify",
            "auth.self.read",
            "auth.password.change",
            "auth.password.reset",
            "auth.sessions.read",
            "auth.sessions.revoke",
            "portal.admin",
        ],
        "SUPER_ADMIN": list(permissions.keys()),
    }
    role_permission_rows = []
    for role_code, permission_codes in role_permission_map.items():
        for permission_code in permission_codes:
            role_permission_rows.append(
                {
                    "id": uuid.uuid4(),
                    "role_id": role_ids[role_code],
                    "permission_id": permission_ids[permission_code],
                    "created_at": now,
                }
            )
    op.bulk_insert(role_permissions_table, role_permission_rows)

    connection = op.get_bind()
    existing_users = connection.execute(
        sa.text("SELECT id, user_type FROM users")
    ).mappings()
    default_user_role_rows = []
    for user in existing_users:
        role_id = role_ids.get(user["user_type"])
        if role_id:
            default_user_role_rows.append(
                {
                    "id": uuid.uuid4(),
                    "user_id": user["id"],
                    "role_id": role_id,
                    "assigned_by": None,
                    "assigned_at": now,
                }
            )
    if default_user_role_rows:
        op.bulk_insert(user_roles_table, default_user_role_rows)


def downgrade() -> None:
    op.drop_index("ix_role_permissions_permission_id", table_name="role_permissions")
    op.drop_index("ix_role_permissions_role_id", table_name="role_permissions")
    op.drop_table("role_permissions")

    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
    op.drop_index("ix_user_roles_user_id", table_name="user_roles")
    op.drop_table("user_roles")

    op.drop_index("ix_permissions_permission_code", table_name="permissions")
    op.drop_table("permissions")

    op.drop_index("ix_roles_role_code", table_name="roles")
    op.drop_table("roles")
