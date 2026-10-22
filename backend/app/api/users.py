"""
Users and Roles Management API
"""

from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, EmailStr
from loguru import logger

from ..db import get_db
from ..models import User, Role, ProxmoxServer, Workspace, WorkspaceServer, WorkspaceUser, UserQuota
from ..auth import (
    get_password_hash,
    get_current_user,
    get_current_active_admin,
    PermissionChecker,
    get_client_ip
)
from ..config import utcnow
from ..services.security_service import SecurityService
from ..logging_service import LoggingService

router = APIRouter()


# ==================== Privilege guards ====================

def _is_admin_user(user: User) -> bool:
    """Administrator by flag or by the system ``admin`` role."""
    return bool(user.is_admin or (user.role and user.role.name == "admin"))


def _guard_target(actor: User, target: User) -> None:
    """Only administrators may manage administrator accounts.

    Checking just ``is_admin`` let a user holding ``user:update`` reset the
    password of an account that is admin through its role and log in as it.
    """
    if _is_admin_user(target) and not _is_admin_user(actor):
        raise HTTPException(status_code=403, detail="Cannot manage an administrator account")


def _guard_permission_grant(actor: User, permissions: Optional[dict]) -> None:
    """A non-admin may only grant permissions they hold themselves."""
    if _is_admin_user(actor):
        return
    from ..rbac import PermissionEngine
    granted = {p for p, enabled in (permissions or {}).items() if enabled}
    missing = granted - PermissionEngine.get_user_permissions(actor)
    if missing:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot grant permissions you do not have: {', '.join(sorted(missing))}",
        )


def _guard_role_assignment(actor: User, role: Role) -> None:
    """Stop non-admins from handing out the admin role or broader roles."""
    if _is_admin_user(actor):
        return
    if role.name == "admin":
        raise HTTPException(status_code=403, detail="Only administrators can assign the admin role")
    _guard_permission_grant(actor, role.permissions)


# ==================== Schemas ====================

class RoleBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)
    display_name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    permissions: dict = Field(default_factory=dict)


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    display_name: Optional[str] = Field(None, min_length=2, max_length=100)
    description: Optional[str] = None
    permissions: Optional[dict] = None
    is_active: Optional[bool] = None


class RoleResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str]
    permissions: dict
    is_system: bool
    is_active: bool
    created_at: datetime
    user_count: int = 0
    
    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6)
    full_name: Optional[str] = Field(None, max_length=100)
    role_id: Optional[int] = None
    is_active: bool = True
    is_admin: bool = False


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, max_length=100)
    password: Optional[str] = Field(None, min_length=1, max_length=100)
    role_id: Optional[int] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    require_password_change: Optional[bool] = None


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str]
    is_active: bool
    is_admin: bool
    role_id: Optional[int]
    role_name: Optional[str] = None
    failed_login_attempts: int = 0
    locked_until: Optional[datetime] = None
    last_login: Optional[datetime] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class PasswordChange(BaseModel):
    new_password: str = Field(..., min_length=6)


class SecuritySettingsUpdate(BaseModel):
    max_login_attempts: Optional[int] = Field(None, ge=1, le=20)
    lockout_duration_minutes: Optional[int] = Field(None, ge=1, le=1440)
    session_timeout_minutes: Optional[int] = Field(None, ge=5, le=1440)
    ip_block_threshold: Optional[int] = Field(None, ge=1, le=100)
    ip_block_duration_minutes: Optional[int] = Field(None, ge=1, le=10080)
    password_min_length: Optional[int] = Field(None, ge=6, le=50)
    password_require_uppercase: Optional[bool] = None
    password_require_lowercase: Optional[bool] = None
    password_require_numbers: Optional[bool] = None
    password_require_special: Optional[bool] = None


class QuotaUpdate(BaseModel):
    """All fields optional; None / omitted clears the limit (unlimited)."""
    max_instances: Optional[int] = Field(None, ge=0)
    max_cores: Optional[int] = Field(None, ge=0)
    max_memory_mb: Optional[int] = Field(None, ge=0)
    max_disk_gb: Optional[int] = Field(None, ge=0)


class QuotaResponse(BaseModel):
    user_id: int
    max_instances: Optional[int] = None
    max_cores: Optional[int] = None
    max_memory_mb: Optional[int] = None
    max_disk_gb: Optional[int] = None
    # Current usage from the vm_instances cache
    used_instances: int = 0
    used_cores: int = 0
    used_memory_mb: int = 0
    used_disk_gb: int = 0


# ==================== Role API ====================

@router.get("/api/roles", response_model=List[RoleResponse])
async def list_roles(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("role:view"))
):
    """Get all roles"""
    from sqlalchemy import func
    roles = SecurityService.get_all_roles(db, include_inactive=True)

    # Count users per role
    counts = dict(
        db.query(User.role_id, func.count(User.id))
        .filter(User.role_id.isnot(None))
        .group_by(User.role_id)
        .all()
    )

    result = []
    for role in roles:
        result.append({
            "id": role.id,
            "name": role.name,
            "display_name": role.display_name,
            "description": role.description,
            "permissions": role.permissions or {},
            "is_system": role.is_system,
            "is_active": role.is_active,
            "created_at": role.created_at,
            "user_count": counts.get(role.id, 0),
        })
    return result


@router.get("/api/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("role:view"))
):
    """Get role by ID"""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.post("/api/roles", response_model=RoleResponse, status_code=201)
async def create_role(
    role_data: RoleCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("role:manage"))
):
    """Create a new role"""
    _guard_permission_grant(current_user, role_data.permissions)
    # Check if role name exists
    existing = db.query(Role).filter(Role.name == role_data.name.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Role name already exists")
    
    role = SecurityService.create_role(
        db=db,
        name=role_data.name,
        display_name=role_data.display_name,
        description=role_data.description,
        permissions=role_data.permissions
    )
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="role_created",
        message=f"Role '{role.name}' created by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="role",
        resource_id=str(role.id),
        resource_name=role.name
    )
    
    return role


@router.put("/api/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    role_data: RoleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("role:manage"))
):
    """Update a role"""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if not _is_admin_user(current_user):
        # Editing one's own role (or the admin role) is a direct escalation path.
        if role.name == "admin" or role.id == current_user.role_id:
            raise HTTPException(status_code=403, detail="Cannot modify this role")
        if role_data.permissions is not None:
            _guard_permission_grant(current_user, role_data.permissions)
    
    # Update fields
    if role_data.display_name is not None:
        role.display_name = role_data.display_name
    if role_data.description is not None:
        role.description = role_data.description
    if role_data.permissions is not None:
        role.permissions = role_data.permissions
    if role_data.is_active is not None:
        role.is_active = role_data.is_active
    
    role.updated_at = utcnow()
    db.commit()
    db.refresh(role)
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="role_updated",
        message=f"Role '{role.name}' updated by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="role",
        resource_id=str(role.id),
        resource_name=role.name
    )
    
    return role


@router.delete("/api/roles/{role_id}")
async def delete_role(
    role_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("role:manage"))
):
    """Delete a role"""
    success, message = SecurityService.delete_role(db, role_id)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.AUTH,
        action="role_deleted",
        message=f"Role ID {role_id} deleted by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="role",
        resource_id=str(role_id)
    )
    
    return {"message": "Role deleted"}


@router.get("/api/permissions/v2")
async def get_all_permissions_v2(
    current_user: User = Depends(PermissionChecker("role:view"))
):
    """
    Get all available permissions in new format.
    Returns permissions grouped by category with resource:action codes.
    """
    return SecurityService.get_all_permissions_v2()


# ==================== User API ====================

@router.get("/api/users", response_model=List[UserResponse])
async def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:view"))
):
    """Get all users"""
    users = db.query(User).order_by(User.username).all()
    
    # Add role_name
    result = []
    for user in users:
        user_dict = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "is_admin": user.is_admin,
            "role_id": user.role_id,
            "role_name": user.role.display_name if user.role else ("Administrator" if user.is_admin else "User"),
            "failed_login_attempts": user.failed_login_attempts or 0,
            "locked_until": user.locked_until,
            "last_login": user.last_login,
            "created_at": user.created_at
        }
        result.append(user_dict)
    
    return result


@router.get("/api/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:view"))
):
    """Get user by ID"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_admin": user.is_admin,
        "role_id": user.role_id,
        "role_name": user.role.display_name if user.role else None,
        "failed_login_attempts": user.failed_login_attempts or 0,
        "locked_until": user.locked_until,
        "last_login": user.last_login,
        "created_at": user.created_at
    }


@router.post("/api/users", response_model=UserResponse, status_code=201)
async def create_user(
    user_data: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:create"))
):
    """Create a new user"""
    # Check username
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    
    # Check email
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(status_code=400, detail="Email already exists")
    
    # Validate password
    lang = request.cookies.get("language", "en")
    is_valid, errors = SecurityService.validate_password(db, user_data.password, lang)
    if not is_valid:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    
    if user_data.is_admin and not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only admin can create administrators")

    # Validate role
    if user_data.role_id:
        role = db.query(Role).filter(Role.id == user_data.role_id).first()
        if not role:
            raise HTTPException(status_code=400, detail="Invalid role")
        _guard_role_assignment(current_user, role)
    
    # Create user
    user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=get_password_hash(user_data.password),
        full_name=user_data.full_name,
        role_id=user_data.role_id,
        is_active=user_data.is_active,
        is_admin=user_data.is_admin,
        last_password_change=utcnow()
    )
    
    db.add(user)
    db.commit()
    db.refresh(user)
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="user_created",
        message=f"User '{user.username}' created by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )
    
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_admin": user.is_admin,
        "role_id": user.role_id,
        "role_name": user.role.display_name if user.role else None,
        "failed_login_attempts": 0,
        "locked_until": None,
        "last_login": None,
        "created_at": user.created_at
    }


@router.put("/api/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Update a user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Prevent editing admin (flag or admin role) by non-admin
    _guard_target(current_user, user)
    password_changed = False
    
    # Update fields
    if user_data.email is not None:
        # Check email uniqueness
        existing = db.query(User).filter(User.email == user_data.email, User.id != user_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already exists")
        user.email = user_data.email
    
    if user_data.full_name is not None:
        user.full_name = user_data.full_name
    
    if user_data.password is not None:
        # Validate password
        lang = request.cookies.get("language", "en")
        is_valid, errors = SecurityService.validate_password(db, user_data.password, lang)
        if not is_valid:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        user.hashed_password = get_password_hash(user_data.password)
        user.last_password_change = utcnow()
        password_changed = True
    
    if user_data.role_id is not None:
        role = db.query(Role).filter(Role.id == user_data.role_id).first()
        if not role:
            raise HTTPException(status_code=400, detail="Invalid role")
        if role.id != user.role_id:
            _guard_role_assignment(current_user, role)
        user.role_id = user_data.role_id
    
    if user_data.is_active is not None:
        # Cannot deactivate yourself
        if user_id == current_user.id and not user_data.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
        user.is_active = user_data.is_active
        
        # Terminate sessions if deactivating
        if not user_data.is_active:
            SecurityService.terminate_all_user_sessions(db, user_id)
    
    if user_data.require_password_change is not None:
        user.require_password_change = user_data.require_password_change
    
    if user_data.is_admin is not None and bool(user_data.is_admin) != bool(user.is_admin):
        # Only admin can change admin status
        if not _is_admin_user(current_user):
            raise HTTPException(status_code=403, detail="Only admin can change admin status")
        # Cannot remove own admin status
        if user_id == current_user.id and not user_data.is_admin:
            raise HTTPException(status_code=400, detail="Cannot remove your own admin status")
        user.is_admin = user_data.is_admin
    
    user.updated_at = utcnow()
    db.commit()
    db.refresh(user)

    # Whoever knew the old password must not stay logged in.
    if password_changed and user_id != current_user.id:
        SecurityService.terminate_all_user_sessions(db, user_id)
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="user_updated",
        message=f"User '{user.username}' updated by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )
    
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_admin": user.is_admin,
        "role_id": user.role_id,
        "role_name": user.role.display_name if user.role else None,
        "failed_login_attempts": user.failed_login_attempts or 0,
        "locked_until": user.locked_until,
        "last_login": user.last_login,
        "created_at": user.created_at
    }


@router.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:delete"))
):
    """Delete a user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Cannot delete yourself
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    
    # Cannot delete admin
    if user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot delete admin user")
    _guard_target(current_user, user)
    
    username = user.username
    
    # Terminate all sessions
    SecurityService.terminate_all_user_sessions(db, user_id)
    
    db.delete(user)
    db.commit()
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.AUTH,
        action="user_deleted",
        message=f"User '{username}' deleted by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user_id),
        resource_name=username
    )
    
    return {"message": "User deleted"}


# ==================== Server Assignment API ====================

def _get_user_workspace_ids(db: Session, user_id: int) -> set:
    """Return set of workspace IDs the user belongs to."""
    rows = db.query(WorkspaceUser.workspace_id).filter(WorkspaceUser.user_id == user_id).all()
    return {r.workspace_id for r in rows}


def _get_server_workspace_info(db: Session, server_id: int):
    """Return list of {id, name} workspaces that own this server."""
    rows = (
        db.query(WorkspaceServer.workspace_id, Workspace.name)
        .join(Workspace, Workspace.id == WorkspaceServer.workspace_id)
        .filter(WorkspaceServer.server_id == server_id)
        .all()
    )
    return [{"id": r.workspace_id, "name": r.name} for r in rows]


@router.get("/api/users/{user_id}/servers")
async def get_user_servers(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:view"))
):
    """Get list of Proxmox servers assigned to a specific user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return [
        {"id": s.id, "name": s.name, "hostname": s.hostname, "ip_address": s.ip_address, "is_online": s.is_online}
        for s in user.assigned_servers
    ]


@router.get("/api/users/{user_id}/server-assignments")
async def get_user_server_assignments(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:view"))
):
    """
    Return all Proxmox servers enriched with:
    - workspaces each server belongs to
    - whether the server is compatible with the user (user is in at least one of its workspaces)
    - whether the server is currently assigned to the user
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_ws_ids = _get_user_workspace_ids(db, user_id)
    assigned_ids = {s.id for s in user.assigned_servers}

    # User's own workspaces info
    user_workspaces = [
        {"id": ws.id, "name": ws.name}
        for ws in db.query(Workspace).filter(Workspace.id.in_(user_ws_ids)).all()
    ] if user_ws_ids else []

    all_servers = db.query(ProxmoxServer).all()
    result = []
    for server in all_servers:
        server_workspaces = _get_server_workspace_info(db, server.id)
        server_ws_ids = {w["id"] for w in server_workspaces}
        # Compatible = user's workspaces and server's workspaces intersect
        compatible = bool(user_ws_ids & server_ws_ids) if (user_ws_ids and server_ws_ids) else False
        result.append({
            "id": server.id,
            "name": server.name,
            "ip_address": server.ip_address,
            "is_online": server.is_online,
            "last_error_kind": server.last_error_kind,
            "workspaces": server_workspaces,
            "compatible": compatible,
            "assigned": server.id in assigned_ids,
        })

    return {
        "servers": result,
        "user_workspaces": user_workspaces,
    }


@router.put("/api/users/{user_id}/servers")
async def set_user_servers(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Assign a list of Proxmox servers to a user (replaces current assignment).
    Rejects the operation if any server's workspaces don't intersect with the user's workspaces.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    data = await request.json()
    server_ids = data.get("server_ids", [])

    if not server_ids:
        user.assigned_servers = []
        db.commit()
        return {"message": "Servers cleared", "server_ids": []}

    servers = db.query(ProxmoxServer).filter(ProxmoxServer.id.in_(server_ids)).all()

    # Workspace compatibility validation
    user_ws_ids = _get_user_workspace_ids(db, user_id)
    conflicts = []
    for server in servers:
        server_workspaces = _get_server_workspace_info(db, server.id)
        server_ws_ids = {w["id"] for w in server_workspaces}
        # No overlap between user workspaces and server workspaces → conflict
        if user_ws_ids and server_ws_ids and not (user_ws_ids & server_ws_ids):
            user_workspaces_info = [
                {"id": ws.id, "name": ws.name}
                for ws in db.query(Workspace).filter(Workspace.id.in_(user_ws_ids)).all()
            ]
            conflicts.append({
                "server_id": server.id,
                "server_name": server.name,
                "server_workspaces": server_workspaces,
                "user_workspaces": user_workspaces_info,
            })

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workspace_conflict",
                "message": "Cannot assign: server(s) are not in any workspace the user belongs to",
                "conflicts": conflicts,
            }
        )

    user.assigned_servers = servers
    db.commit()

    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="user_servers_updated",
        message=f"Servers assigned to '{user.username}' by {current_user.username}: {[s.id for s in servers]}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )

    return {"message": "Servers assigned successfully", "server_ids": [s.id for s in servers]}


# ==================== User Quota API ====================

@router.get("/api/users/{user_id}/quota", response_model=QuotaResponse)
async def get_user_quota(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("quota:view"))
):
    """Get a user's resource quota plus current usage.

    If the user has no quota row, all limits are returned as null (unlimited).
    """
    from ..services.quota_service import get_user_usage

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    quota = db.query(UserQuota).filter(UserQuota.user_id == user_id).first()
    usage = get_user_usage(db, user_id)

    return QuotaResponse(
        user_id=user_id,
        max_instances=quota.max_instances if quota else None,
        max_cores=quota.max_cores if quota else None,
        max_memory_mb=quota.max_memory_mb if quota else None,
        max_disk_gb=quota.max_disk_gb if quota else None,
        used_instances=usage["instances"],
        used_cores=usage["cores"],
        used_memory_mb=usage["memory_mb"],
        used_disk_gb=usage["disk_gb"],
    )


@router.put("/api/users/{user_id}/quota", response_model=QuotaResponse)
async def set_user_quota(
    user_id: int,
    data: QuotaUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("quota:manage"))
):
    """Upsert a user's resource quota. A null/omitted field clears that limit."""
    from ..services.quota_service import get_user_usage

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    quota = db.query(UserQuota).filter(UserQuota.user_id == user_id).first()
    if quota is None:
        quota = UserQuota(user_id=user_id)
        db.add(quota)

    quota.max_instances = data.max_instances
    quota.max_cores = data.max_cores
    quota.max_memory_mb = data.max_memory_mb
    quota.max_disk_gb = data.max_disk_gb
    db.commit()

    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="user_quota_updated",
        message=(
            f"Quota set for '{user.username}' by {current_user.username}: "
            f"instances={data.max_instances}, cores={data.max_cores}, "
            f"memory_mb={data.max_memory_mb}, disk_gb={data.max_disk_gb}"
        ),
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )

    usage = get_user_usage(db, user_id)
    return QuotaResponse(
        user_id=user_id,
        max_instances=quota.max_instances,
        max_cores=quota.max_cores,
        max_memory_mb=quota.max_memory_mb,
        max_disk_gb=quota.max_disk_gb,
        used_instances=usage["instances"],
        used_cores=usage["cores"],
        used_memory_mb=usage["memory_mb"],
        used_disk_gb=usage["disk_gb"],
    )


@router.post("/api/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    password_data: PasswordChange,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Reset user password (admin action)"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _guard_target(current_user, user)
    
    # Validate password
    lang = request.cookies.get("language", "en")
    is_valid, errors = SecurityService.validate_password(db, password_data.new_password, lang)
    if not is_valid:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    
    user.hashed_password = get_password_hash(password_data.new_password)
    user.last_password_change = utcnow()
    user.require_password_change = True  # Force password change on next login
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    
    # Terminate all sessions
    SecurityService.terminate_all_user_sessions(db, user_id)
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.AUTH,
        action="password_reset",
        message=f"Password reset for user '{user.username}' by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )
    
    return {"message": "Password reset successfully"}


@router.post("/api/users/{user_id}/unlock")
async def unlock_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Unlock a locked user account"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _guard_target(current_user, user)
    
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="user_unlocked",
        message=f"User '{user.username}' unlocked by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )
    
    return {"message": "User unlocked"}


@router.post("/api/users/{user_id}/disable-2fa")
async def admin_disable_two_factor(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Admin reset: forcibly disable a user's two-factor authentication.

    Used when a user loses their authenticator device and all backup codes.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _guard_target(current_user, user)

    if not user.two_factor_enabled and not user.two_factor_secret:
        return {"message": "2FA is not enabled for this user"}

    user.two_factor_enabled = False
    user.two_factor_secret = None
    user.two_factor_backup_codes = None
    db.commit()

    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.AUTH,
        action="user_2fa_disabled",
        message=f"2FA for user '{user.username}' disabled by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )

    return {"message": "Two-factor authentication disabled for user"}


@router.post("/api/users/{user_id}/terminate-sessions")
async def terminate_user_sessions(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Terminate all sessions for a user"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _guard_target(current_user, user)
    
    count = SecurityService.terminate_all_user_sessions(db, user_id)
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="sessions_terminated",
        message=f"Terminated {count} sessions for user '{user.username}' by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request),
        resource_type="user",
        resource_id=str(user.id),
        resource_name=user.username
    )
    
    # count отдаётся отдельным полем: фронтенд склоняет фразу сам, показывать
    # message пользователю нельзя — он не переводится.
    return {"message": f"Terminated {count} sessions", "count": count}


# ==================== Security Settings API ====================

@router.get("/api/security/settings")
async def get_security_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Get security settings"""
    return {
        "max_login_attempts": SecurityService.get_setting_int(db, "max_login_attempts", 5),
        "lockout_duration_minutes": SecurityService.get_setting_int(db, "lockout_duration_minutes", 30),
        "session_timeout_minutes": SecurityService.get_setting_int(db, "session_timeout_minutes", 60),
        "ip_block_threshold": SecurityService.get_setting_int(db, "ip_block_threshold", 10),
        "ip_block_duration_minutes": SecurityService.get_setting_int(db, "ip_block_duration_minutes", 60),
        "password_min_length": SecurityService.get_setting_int(db, "password_min_length", 8),
        "password_require_uppercase": SecurityService.get_setting_bool(db, "password_require_uppercase", True),
        "password_require_lowercase": SecurityService.get_setting_bool(db, "password_require_lowercase", True),
        "password_require_numbers": SecurityService.get_setting_bool(db, "password_require_numbers", True),
        "password_require_special": SecurityService.get_setting_bool(db, "password_require_special", False),
    }


@router.put("/api/security/settings")
async def update_security_settings(
    settings_data: SecuritySettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Update security settings"""
    updated = []
    
    for key, value in settings_data.model_dump(exclude_unset=True).items():
        SecurityService.update_setting(db, key, str(value).lower() if isinstance(value, bool) else str(value))
        updated.append(key)
    
    if updated:
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.AUTH,
            action="security_settings_updated",
            message=f"Security settings updated by {current_user.username}: {', '.join(updated)}",
            username=current_user.username,
            user_id=current_user.id,
            ip_address=get_client_ip(request)
        )
    
    return {"message": "Settings updated", "updated": updated}


# ==================== Blocked IPs API ====================

@router.get("/api/security/blocked-ips")
async def list_blocked_ips(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Get list of blocked IPs"""
    from ..models import BlockedIP
    blocked = db.query(BlockedIP).order_by(BlockedIP.blocked_at.desc()).all()
    return [{
        "id": ip.id,
        "ip_address": ip.ip_address,
        "reason": ip.reason,
        "blocked_by": ip.blocked_by,
        "blocked_at": ip.blocked_at.isoformat() if ip.blocked_at else None,
        "expires_at": ip.expires_at.isoformat() if ip.expires_at else None,
        "is_permanent": ip.is_permanent,
        "attempts_count": ip.attempts_count,
        "is_active": ip.is_blocked()
    } for ip in blocked]


@router.delete("/api/security/blocked-ips/{ip_address:path}")
async def unblock_ip(
    ip_address: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Unblock an IP address"""
    if SecurityService.unblock_ip(db, ip_address):
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.AUTH,
            action="ip_unblocked",
            message=f"IP {ip_address} unblocked by {current_user.username}",
            username=current_user.username,
            user_id=current_user.id,
            ip_address=get_client_ip(request)
        )
        return {"message": f"IP {ip_address} unblocked"}
    
    raise HTTPException(status_code=404, detail="IP not found in blocked list")


# ==================== Sessions API ====================

@router.get("/api/sessions")
async def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:view"))
):
    """Get list of active sessions"""
    from ..models import ActiveSession
    
    sessions = db.query(ActiveSession).filter(
        ActiveSession.expires_at > utcnow()
    ).order_by(ActiveSession.last_activity.desc()).all()
    
    result = []
    for session in sessions:
        user = db.query(User).filter(User.id == session.user_id).first()
        result.append({
            "id": session.id,
            "user_id": session.user_id,
            "username": user.username if user else "Unknown",
            "ip_address": session.ip_address,
            "device_info": session.device_info,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "last_activity": session.last_activity.isoformat() if session.last_activity else None,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        })
    
    return result


@router.delete("/api/sessions/{session_id}")
async def terminate_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Terminate a specific session"""
    from ..models import ActiveSession
    
    session = db.query(ActiveSession).filter(ActiveSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    db.delete(session)
    db.commit()
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="session_terminated",
        message=f"Session {session_id} terminated by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request)
    )
    
    return {"message": "Session terminated"}


@router.post("/api/sessions/terminate-all")
async def terminate_all_sessions(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("user:update"))
):
    """Terminate all sessions except current user's"""
    from ..models import ActiveSession
    
    # Delete all sessions except current user's
    deleted = db.query(ActiveSession).filter(
        ActiveSession.user_id != current_user.id
    ).delete()
    db.commit()
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.AUTH,
        action="all_sessions_terminated",
        message=f"All sessions ({deleted}) terminated by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request)
    )
    
    return {"message": f"Terminated {deleted} sessions", "count": deleted}


# ==================== Security Events API ====================

@router.get("/api/security/events")
async def list_security_events(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Get security events from logs"""
    from ..models import AuditLog
    
    # Get auth-related logs
    events = db.query(AuditLog).filter(
        AuditLog.category == "auth"
    ).order_by(AuditLog.created_at.desc()).limit(limit).all()
    
    return [{
        "id": event.id,
        "event_type": event.action,
        "ip_address": event.ip_address,
        "username": event.username,
        "message": event.message,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    } for event in events]


# ==================== Block IP API ====================

@router.post("/api/security/blocked-ips")
async def block_ip(
    ip_data: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Block an IP address manually"""
    from ..models import BlockedIP
    
    ip_address = ip_data.get("ip_address")
    reason = ip_data.get("reason", "Manual block")
    duration_minutes = ip_data.get("duration_minutes")
    
    if not ip_address:
        raise HTTPException(status_code=400, detail="IP address is required")
    
    # Check if already blocked
    existing = db.query(BlockedIP).filter(BlockedIP.ip_address == ip_address).first()
    if existing:
        raise HTTPException(status_code=400, detail="IP is already blocked")
    
    # Calculate expiry
    expires_at = None
    is_permanent = True
    if duration_minutes:
        expires_at = utcnow() + timedelta(minutes=int(duration_minutes))
        is_permanent = False
    
    blocked_ip = BlockedIP(
        ip_address=ip_address,
        reason=reason,
        blocked_by=current_user.username,
        blocked_at=utcnow(),
        expires_at=expires_at,
        is_permanent=is_permanent
    )
    db.add(blocked_ip)
    db.commit()
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.AUTH,
        action="ip_blocked_manual",
        message=f"IP {ip_address} blocked manually by {current_user.username}: {reason}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request)
    )
    
    return {"message": f"IP {ip_address} blocked", "id": blocked_ip.id}


@router.delete("/api/security/blocked-ips/{blocked_id}")
async def unblock_ip_by_id(
    blocked_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Unblock an IP address by ID"""
    from ..models import BlockedIP
    
    blocked_ip = db.query(BlockedIP).filter(BlockedIP.id == blocked_id).first()
    if not blocked_ip:
        raise HTTPException(status_code=404, detail="Blocked IP not found")
    
    ip_address = blocked_ip.ip_address
    db.delete(blocked_ip)
    db.commit()
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="ip_unblocked",
        message=f"IP {ip_address} unblocked by {current_user.username}",
        username=current_user.username,
        user_id=current_user.id,
        ip_address=get_client_ip(request)
    )
    
    return {"message": f"IP {ip_address} unblocked"}
