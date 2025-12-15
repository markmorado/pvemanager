"""
RBAC Module - Role-Based Access Control
Provides atomic permission model with resource:action:scope format
"""

from .permissions import (
    Permission, 
    PermissionRegistry, 
    PERMISSIONS,
    ScopeType,
    ResourceType,
    ActionType,
    LEGACY_PERMISSION_MAP,
    resolve_permission,
    get_permission_info
)
from .engine import (
    PermissionEngine, 
    require_permission, 
    check_permission,
    get_user_permissions
)
from .middleware import (
    PermissionChecker, 
    ScopedPermissionChecker,
    ResourceAccessChecker
)
from .decorators import (
    requires_permission, 
    requires_any_permission,
    requires_all_permissions
)
from .audit import RBACAuditService
from .migration import (
    convert_permissions_to_new_format,
    convert_permissions_to_legacy_format,
    get_permission_categories,
    get_permission_categories_legacy,
    migrate_all_roles_to_new_format,
    ensure_default_roles_new_format,
    NEW_DEFAULT_ROLES
)

__all__ = [
    # Permissions
    "Permission",
    "PermissionRegistry", 
    "PERMISSIONS",
    "ScopeType",
    "ResourceType",
    "ActionType",
    "LEGACY_PERMISSION_MAP",
    "resolve_permission",
    "get_permission_info",
    # Engine
    "PermissionEngine",
    "require_permission",
    "check_permission",
    "get_user_permissions",
    # Middleware
    "PermissionChecker",
    "ScopedPermissionChecker",
    "ResourceAccessChecker",
    # Decorators
    "requires_permission",
    "requires_any_permission",
    "requires_all_permissions",
    # Audit
    "RBACAuditService",
    # Migration
    "convert_permissions_to_new_format",
    "convert_permissions_to_legacy_format",
    "get_permission_categories",
    "get_permission_categories_legacy",
    "migrate_all_roles_to_new_format",
    "ensure_default_roles_new_format",
    "NEW_DEFAULT_ROLES",
]
