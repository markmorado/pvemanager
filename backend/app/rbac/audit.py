"""
RBAC Audit Service - Logs all permission and role changes
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import desc
from loguru import logger

from .permissions import PERMISSIONS, resolve_permission


def utcnow() -> datetime:
    """Get current UTC time"""
    return datetime.now(timezone.utc)


class RBACAuditService:
    """
    Service for auditing RBAC changes.
    Logs role creation, permission changes, role assignments, etc.
    """
    
    AUDIT_ACTIONS = {
        "role_created": "Role Created",
        "role_updated": "Role Updated", 
        "role_deleted": "Role Deleted",
        "role_assigned": "Role Assigned to User",
        "role_removed": "Role Removed from User",
        "permission_added": "Permission Added",
        "permission_removed": "Permission Removed",
        "permission_denied": "Permission Denied",
    }
    
    @staticmethod
    def log_role_created(
        db: Session,
        role_id: int,
        role_name: str,
        permissions: Dict[str, bool],
        created_by_id: int,
        created_by_username: str,
        ip_address: Optional[str] = None
    ) -> None:
        """Log role creation"""
        from ..models import AuditLog
        
        log_entry = AuditLog(
            level="info",
            category="rbac",
            action="role_created",
            message=f"Role '{role_name}' created by {created_by_username}",
            user_id=created_by_id,
            username=created_by_username,
            ip_address=ip_address,
            resource_type="role",
            resource_id=str(role_id),
            resource_name=role_name,
            details={
                "permissions": permissions,
                "permissions_count": sum(1 for v in permissions.values() if v)
            },
            success=True
        )
        db.add(log_entry)
        db.commit()
        
        logger.info(f"RBAC Audit: Role '{role_name}' created by {created_by_username}")
    
    @staticmethod
    def log_role_updated(
        db: Session,
        role_id: int,
        role_name: str,
        old_permissions: Dict[str, bool],
        new_permissions: Dict[str, bool],
        updated_by_id: int,
        updated_by_username: str,
        ip_address: Optional[str] = None
    ) -> None:
        """Log role update with permission diff"""
        from ..models import AuditLog
        
        # Calculate diff
        added = []
        removed = []
        
        for perm, enabled in new_permissions.items():
            old_value = old_permissions.get(perm, False)
            if enabled and not old_value:
                added.append(perm)
            elif not enabled and old_value:
                removed.append(perm)
        
        log_entry = AuditLog(
            level="info",
            category="rbac",
            action="role_updated",
            message=f"Role '{role_name}' updated by {updated_by_username}: +{len(added)} -{len(removed)} permissions",
            user_id=updated_by_id,
            username=updated_by_username,
            ip_address=ip_address,
            resource_type="role",
            resource_id=str(role_id),
            resource_name=role_name,
            details={
                "permissions_added": added,
                "permissions_removed": removed,
                "old_permissions_count": sum(1 for v in old_permissions.values() if v),
                "new_permissions_count": sum(1 for v in new_permissions.values() if v)
            },
            success=True
        )
        db.add(log_entry)
        db.commit()
        
        logger.info(f"RBAC Audit: Role '{role_name}' updated: +{len(added)} -{len(removed)}")
    
    @staticmethod
    def log_role_deleted(
        db: Session,
        role_id: int,
        role_name: str,
        deleted_by_id: int,
        deleted_by_username: str,
        affected_users: int = 0,
        ip_address: Optional[str] = None
    ) -> None:
        """Log role deletion"""
        from ..models import AuditLog
        
        log_entry = AuditLog(
            level="warning",
            category="rbac",
            action="role_deleted",
            message=f"Role '{role_name}' deleted by {deleted_by_username}, {affected_users} users affected",
            user_id=deleted_by_id,
            username=deleted_by_username,
            ip_address=ip_address,
            resource_type="role",
            resource_id=str(role_id),
            resource_name=role_name,
            details={
                "affected_users": affected_users
            },
            success=True
        )
        db.add(log_entry)
        db.commit()
        
        logger.warning(f"RBAC Audit: Role '{role_name}' deleted, {affected_users} users affected")
    
    @staticmethod
    def log_role_assigned(
        db: Session,
        user_id: int,
        username: str,
        role_id: int,
        role_name: str,
        old_role_name: Optional[str],
        assigned_by_id: int,
        assigned_by_username: str,
        ip_address: Optional[str] = None
    ) -> None:
        """Log role assignment to user"""
        from ..models import AuditLog
        
        msg = f"Role '{role_name}' assigned to user '{username}'"
        if old_role_name:
            msg += f" (was: {old_role_name})"
        msg += f" by {assigned_by_username}"
        
        log_entry = AuditLog(
            level="info",
            category="rbac",
            action="role_assigned",
            message=msg,
            user_id=assigned_by_id,
            username=assigned_by_username,
            ip_address=ip_address,
            resource_type="user",
            resource_id=str(user_id),
            resource_name=username,
            details={
                "new_role_id": role_id,
                "new_role_name": role_name,
                "old_role_name": old_role_name,
                "target_user_id": user_id,
                "target_username": username
            },
            success=True
        )
        db.add(log_entry)
        db.commit()
        
        logger.info(f"RBAC Audit: {msg}")
    
    @staticmethod
    def log_permission_denied(
        db: Session,
        user_id: int,
        username: str,
        permission: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        request_path: Optional[str] = None
    ) -> None:
        """Log denied permission attempt"""
        from ..models import AuditLog
        
        log_entry = AuditLog(
            level="warning",
            category="rbac",
            action="permission_denied",
            message=f"Permission '{permission}' denied for user '{username}'",
            user_id=user_id,
            username=username,
            ip_address=ip_address,
            resource_type=resource_type,
            resource_id=resource_id,
            request_path=request_path,
            details={
                "permission": permission,
                "resolved_permission": resolve_permission(permission)
            },
            success=False
        )
        db.add(log_entry)
        db.commit()
        
        logger.warning(f"RBAC Audit: Permission '{permission}' denied for '{username}'")
    
    @staticmethod
    def get_rbac_logs(
        db: Session,
        limit: int = 100,
        offset: int = 0,
        user_id: Optional[int] = None,
        action: Optional[str] = None
    ) -> List:
        """Get RBAC audit logs"""
        from ..models import AuditLog
        
        query = db.query(AuditLog).filter(AuditLog.category == "rbac")
        
        if user_id:
            query = query.filter(AuditLog.user_id == user_id)
        if action:
            query = query.filter(AuditLog.action == action)
        
        return query.order_by(desc(AuditLog.created_at)).offset(offset).limit(limit).all()
    
    @staticmethod
    def get_user_permission_history(
        db: Session,
        user_id: int,
        limit: int = 50
    ) -> List:
        """Get permission change history for a user"""
        from ..models import AuditLog
        
        return db.query(AuditLog).filter(
            AuditLog.category == "rbac",
            AuditLog.details.contains({"target_user_id": user_id})
        ).order_by(desc(AuditLog.created_at)).limit(limit).all()
