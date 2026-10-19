# WAY TERO — Auth Models
# Import all models here so Alembic can auto-detect them.
from app.modules.auth.models.permission import Permission
from app.modules.auth.models.role import Role
from app.modules.auth.models.role_permission import RolePermission
from app.modules.auth.models.user import User
from app.modules.auth.models.user_role import UserRole
from app.modules.auth.models.user_session import UserSession
from app.modules.auth.models.staff_work_daily import StaffWorkDaily

# users.branch_id (migration 0085) points at branches; importing the model keeps
# the FK resolvable for anything that loads User without the admin models first.
from app.modules.master.models import Branch  # noqa: F401

__all__ = [
    "Permission",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
    "UserSession",
    "StaffWorkDaily",
]
