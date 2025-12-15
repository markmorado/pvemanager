"""
Authorization Middleware - FastAPI dependencies for permission checks
Replaces old PermissionChecker with enhanced functionality
"""

from typing import Optional, List, Union
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session

from .engine import PermissionEngine
from .permissions import ScopeType, resolve_permission


class PermissionChecker:
    """
    FastAPI dependency for checking permissions.
    
    Usage:
        @router.get("/vms")
        async def list_vms(user = Depends(PermissionChecker("vm:view"))):
            ...
        
        # Multiple permissions (any)
        @router.post("/vms")
        async def create_vm(user = Depends(PermissionChecker(["vm:create", "lxc:create"]))):
            ...
    """
    
    def __init__(
        self, 
        permission: Union[str, List[str]],
        require_all: bool = False,
        scope: ScopeType = ScopeType.GLOBAL
    ):
        """
        Args:
            permission: Permission code or list of codes
            require_all: If True, requires all permissions (AND), else any (OR)
            scope: Permission scope level
        """
        if isinstance(permission, str):
            self.permissions = [permission]
        else:
            self.permissions = permission
        self.require_all = require_all
        self.scope = scope
    
    def __call__(self, request: Request, db: Session = None):
        """Check permissions for current user"""
        # Import here to avoid circular imports
        from ..auth import get_current_user
        from ..db import get_db
        
        # Get user from request state if available
        user = getattr(request.state, 'user', None)
        
        if user is None:
            # Fall back to standard auth
            from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
            from ..auth import decode_access_token
            from ..models import User
            from sqlalchemy.orm import joinedload
            
            security = HTTPBearer()
            
            # Get token from header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            
            token = auth_header.split(" ")[1]
            payload = decode_access_token(token)
            username = payload.get("sub")
            
            if db is None:
                # Get DB session
                from ..db import SessionLocal
                db = SessionLocal()
                try:
                    user = db.query(User).options(joinedload(User.role)).filter(
                        User.username == username
                    ).first()
                finally:
                    db.close()
            else:
                user = db.query(User).options(joinedload(User.role)).filter(
                    User.username == username
                ).first()
            
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found",
                )
        
        # Check permissions
        if self.require_all:
            # Must have all permissions
            for perm in self.permissions:
                if not PermissionEngine.has_permission(user, perm, self.scope):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Permission denied: {perm}"
                    )
        else:
            # Must have at least one permission
            has_any = False
            for perm in self.permissions:
                if PermissionEngine.has_permission(user, perm, self.scope):
                    has_any = True
                    break
            
            if not has_any:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: requires one of {self.permissions}"
                )
        
        return user


class ScopedPermissionChecker:
    """
    Permission checker with resource-level scope.
    
    Usage:
        @router.get("/vms/{vm_id}")
        async def get_vm(
            vm_id: int,
            user = Depends(ScopedPermissionChecker("vm:view", resource_param="vm_id"))
        ):
            ...
    """
    
    def __init__(
        self,
        permission: str,
        scope: ScopeType = ScopeType.INSTANCE,
        resource_param: str = "resource_id",
        organization_param: Optional[str] = None,
        workspace_param: Optional[str] = None
    ):
        self.permission = permission
        self.scope = scope
        self.resource_param = resource_param
        self.organization_param = organization_param
        self.workspace_param = workspace_param
    
    def __call__(self, request: Request):
        """Check scoped permission"""
        from ..auth import get_current_user
        from ..db import SessionLocal
        from ..models import User
        from sqlalchemy.orm import joinedload
        
        # Get resource ID from path params
        resource_id = request.path_params.get(self.resource_param)
        organization_id = request.path_params.get(self.organization_param) if self.organization_param else None
        workspace_id = request.path_params.get(self.workspace_param) if self.workspace_param else None
        
        # Get user
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        
        token = auth_header.split(" ")[1]
        from ..auth import decode_access_token
        payload = decode_access_token(token)
        username = payload.get("sub")
        
        db = SessionLocal()
        try:
            user = db.query(User).options(joinedload(User.role)).filter(
                User.username == username
            ).first()
            
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found",
                )
            
            # Check permission with scope
            if not PermissionEngine.has_permission(
                user,
                self.permission,
                self.scope,
                resource_id=int(resource_id) if resource_id else None,
                organization_id=int(organization_id) if organization_id else None,
                workspace_id=int(workspace_id) if workspace_id else None
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: {self.permission}"
                )
            
            return user
        finally:
            db.close()


class ResourceAccessChecker:
    """
    Check access to specific resource based on ownership/membership.
    Future use for organization/workspace scoping.
    """
    
    def __init__(
        self,
        resource_type: str,
        permission: str,
        id_param: str = "id"
    ):
        self.resource_type = resource_type
        self.permission = permission
        self.id_param = id_param
    
    def __call__(self, request: Request):
        """Check resource access"""
        # Placeholder for future organization/workspace checks
        # For now, just check permission
        checker = PermissionChecker(self.permission)
        return checker(request)
