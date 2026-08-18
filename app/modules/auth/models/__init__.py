# WAY TERO — Auth Models
# Import all models here so Alembic can auto-detect them.
from app.modules.auth.models.permission import Permission
from app.modules.auth.models.role import Role
from app.modules.auth.models.role_permission import RolePermission
from app.modules.auth.models.user import User
from app.modules.auth.models.user_role import UserRole
from app.modules.auth.models.user_session import UserSession

__all__ = [
    "Permission",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
    "UserSession",
]
