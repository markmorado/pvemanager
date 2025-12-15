"""
Permission Decorators - Function decorators for permission checks
"""

from functools import wraps
from typing import List, Callable, Union
from fastapi import HTTPException, status

from .engine import PermissionEngine
from .permissions import ScopeType


def requires_permission(
    permission: str,
    scope: ScopeType = ScopeType.GLOBAL,
    user_param: str = "current_user"
):
    """
    Decorator to require a specific permission.
    
    Usage:
        @requires_permission("vm:create")
        async def create_vm(current_user: User, ...):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            user = kwargs.get(user_param)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found in request"
                )
            
            PermissionEngine.check_permission(user, permission, scope)
            return await func(*args, **kwargs)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            user = kwargs.get(user_param)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found in request"
                )
            
            PermissionEngine.check_permission(user, permission, scope)
            return func(*args, **kwargs)
        
        # Return appropriate wrapper based on function type
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator


def requires_any_permission(
    permissions: List[str],
    scope: ScopeType = ScopeType.GLOBAL,
    user_param: str = "current_user"
):
    """
    Decorator to require any one of the listed permissions.
    
    Usage:
        @requires_any_permission(["vm:create", "lxc:create"])
        async def create_instance(current_user: User, ...):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            user = kwargs.get(user_param)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found in request"
                )
            
            if not PermissionEngine.has_any_permission(user, permissions):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: requires one of {permissions}"
                )
            
            return await func(*args, **kwargs)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            user = kwargs.get(user_param)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found in request"
                )
            
            if not PermissionEngine.has_any_permission(user, permissions):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: requires one of {permissions}"
                )
            
            return func(*args, **kwargs)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator


def requires_all_permissions(
    permissions: List[str],
    scope: ScopeType = ScopeType.GLOBAL,
    user_param: str = "current_user"
):
    """
    Decorator to require all of the listed permissions.
    
    Usage:
        @requires_all_permissions(["vm:view", "vm:update"])
        async def update_vm(current_user: User, ...):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            user = kwargs.get(user_param)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found in request"
                )
            
            for perm in permissions:
                PermissionEngine.check_permission(user, perm, scope)
            
            return await func(*args, **kwargs)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            user = kwargs.get(user_param)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found in request"
                )
            
            for perm in permissions:
                PermissionEngine.check_permission(user, perm, scope)
            
            return func(*args, **kwargs)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator
