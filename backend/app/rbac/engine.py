"""
Permission Engine - Central authorization logic
Handles all permission checks with scope awareness
"""

from typing import Optional, List, Set, Dict, Any
from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from loguru import logger

from .permissions import (
    Permission, 
    PermissionRegistry, 
    PERMISSIONS, 
    ScopeType,
    resolve_permission,
    LEGACY_PERMISSION_MAP
)


class PermissionEngine:
    """
    Central permission checking engine.
    All authorization decisions go through this class.
    """
    
    @staticmethod
    def get_user_permissions(user) -> Set[str]:
        """
        Get all effective permissions for a user.
        Returns set of permission codes.
        """
        permissions = set()
        
        # Check if user is admin (has all permissions)
        if getattr(user, 'is_admin', False):
            return {p.code for p in PERMISSIONS.all()}
        
        # Get permissions from role
        if user.role and user.role.permissions:
            role_perms = user.role.permissions
            for perm_code, enabled in role_perms.items():
                if enabled:
                    # Resolve legacy to new format
                    resolved = resolve_permission(perm_code)
                    permissions.add(resolved)
                    
                    # Also keep legacy for backwards compatibility
                    if perm_code != resolved:
                        permissions.add(perm_code)
        
        return permissions
    
    @staticmethod
    def has_permission(
        user,
        permission: str,
        scope: ScopeType = ScopeType.GLOBAL,
        resource_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
    ) -> bool:
        """
        Check if user has specific permission.
        
        Args:
            user: User object
            permission: Permission code (new or legacy format)
            scope: Permission scope level
            resource_id: Specific resource ID (for instance scope)
            organization_id: Organization context
            workspace_id: Workspace context
            
        Returns:
            True if user has permission, False otherwise
        """
        # Admin bypass - full access
        if getattr(user, 'is_admin', False):
            return True
        
        # Get user permissions
        user_permissions = PermissionEngine.get_user_permissions(user)
        
        # Resolve permission code
        resolved_perm = resolve_permission(permission)
        
        # Check direct permission
        if resolved_perm in user_permissions or permission in user_permissions:
            return True
        
        # Check wildcard permissions (e.g., vm:* grants all vm permissions)
        resource = resolved_perm.split(':')[0] if ':' in resolved_perm else permission.split('.')[0]
        wildcard = f"{resource}:*"
        if wildcard in user_permissions:
            return True
        
        # Check manage permission (implies most actions)
        manage_perm = f"{resource}:manage"
        if manage_perm in user_permissions:
            # manage implies view, update, and some actions
            action = resolved_perm.split(':')[1] if ':' in resolved_perm else None
            implied_actions = {'view', 'list', 'update', 'start', 'stop', 'restart'}
            if action in implied_actions:
                return True
        
        return False
    
    @staticmethod
    def check_permission(
        user,
        permission: str,
        scope: ScopeType = ScopeType.GLOBAL,
        resource_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
    ) -> None:
        """
        Check permission and raise 403 if denied.
        """
        if not PermissionEngine.has_permission(
            user, permission, scope, resource_id, organization_id, workspace_id
        ):
            logger.warning(
                f"Permission denied: user={user.username}, "
                f"permission={permission}, scope={scope}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission}"
            )
    
    @staticmethod
    def has_any_permission(user, permissions: List[str]) -> bool:
        """Check if user has any of the listed permissions"""
        for perm in permissions:
            if PermissionEngine.has_permission(user, perm):
                return True
        return False
    
    @staticmethod
    def has_all_permissions(user, permissions: List[str]) -> bool:
        """Check if user has all of the listed permissions"""
        for perm in permissions:
            if not PermissionEngine.has_permission(user, perm):
                return False
        return True
    
    @staticmethod
    def filter_permissions(user, permissions: List[str]) -> List[str]:
        """Filter list to only permissions user has"""
        return [p for p in permissions if PermissionEngine.has_permission(user, p)]
    
    @staticmethod
    def get_effective_permissions(user) -> Dict[str, Any]:
        """
        Get effective permissions with metadata.
        Returns dict suitable for UI display.
        """
        user_perms = PermissionEngine.get_user_permissions(user)
        result = {}
        
        for perm in PERMISSIONS.all():
            has_perm = perm.code in user_perms
            result[perm.code] = {
                "code": perm.code,
                "display_name": perm.display_name,
                "description": perm.description,
                "category": perm.category,
                "granted": has_perm,
                "source": "admin" if user.is_admin else ("role" if has_perm else None)
            }
        
        return result
    
    @staticmethod
    def get_permissions_by_category(user) -> Dict[str, List[Dict]]:
        """Get effective permissions grouped by category"""
        effective = PermissionEngine.get_effective_permissions(user)
        by_category = {}
        
        for perm_info in effective.values():
            category = perm_info["category"]
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(perm_info)
        
        return by_category
    
    @staticmethod
    def can_assign_permission(assigner, permission: str) -> bool:
        """
        Check if user can assign a specific permission to others.
        Users cannot grant permissions they don't have themselves.
        """
        # Must have role management permission
        if not PermissionEngine.has_permission(assigner, "role:manage"):
            return False
        
        # Cannot assign permission you don't have (unless admin)
        if not assigner.is_admin:
            if not PermissionEngine.has_permission(assigner, permission):
                return False
        
        return True
    
    @staticmethod
    def validate_role_permissions(assigner, permissions: Dict[str, bool]) -> tuple[Dict, List[str]]:
        """
        Validate permissions that user is trying to assign.
        Returns (valid_permissions, denied_permissions)
        """
        valid = {}
        denied = []
        
        for perm, enabled in permissions.items():
            if enabled:
                if PermissionEngine.can_assign_permission(assigner, perm):
                    valid[perm] = True
                else:
                    denied.append(perm)
            else:
                valid[perm] = False
        
        return valid, denied


# Convenience functions
def require_permission(
    user,
    permission: str,
    scope: ScopeType = ScopeType.GLOBAL,
    resource_id: Optional[int] = None,
) -> None:
    """Require permission or raise 403"""
    PermissionEngine.check_permission(user, permission, scope, resource_id)


def check_permission(user, permission: str) -> bool:
    """Check if user has permission"""
    return PermissionEngine.has_permission(user, permission)


def get_user_permissions(user) -> Set[str]:
    """Get all user permissions"""
    return PermissionEngine.get_user_permissions(user)
