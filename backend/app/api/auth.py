from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from loguru import logger

from ..db import get_db
from ..models import User
from ..schemas import UserCreate, UserResponse, LoginRequest, Token
from ..auth import (
    get_password_hash, 
    verify_password, 
    create_access_token,
    get_current_user,
    get_current_active_admin,
    get_client_ip,
    get_current_session_token
)
from ..config import settings, utcnow
from ..logging_service import LoggingService
from ..template_helpers import add_i18n_context
from ..services.security_service import SecurityService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    """Login page"""
    context = {
        "request": request,
        "panel_name": settings.PANEL_NAME,
        "version": settings.VERSION,
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("login.html", context)


@router.post("/api/auth/login", response_model=Token)
def login(login_data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    Authenticate user and return JWT token with session management
    """
    # Get client info
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")[:500]
    
    # Check if IP is blocked
    is_blocked, block_reason = SecurityService.is_ip_blocked(db, client_ip)
    if is_blocked:
        logger.warning(f"Blocked IP {client_ip} attempted login: {block_reason}")
        SecurityService.record_login_attempt(
            db, client_ip, login_data.username, False, "IP blocked", user_agent
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: {block_reason}"
        )
    
    # Check for brute force
    should_block, reason = SecurityService.check_brute_force(db, client_ip, login_data.username)
    if should_block:
        block_duration = SecurityService.get_setting_int(db, "ip_block_duration_minutes", 60)
        SecurityService.block_ip(db, client_ip, reason, "system", block_duration)
        SecurityService.record_login_attempt(
            db, client_ip, login_data.username, False, "Brute force detected", user_agent
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Too many failed attempts. IP blocked temporarily."
        )
    
    # Find user
    user = db.query(User).filter(User.username == login_data.username).first()
    
    if not user:
        SecurityService.record_login_attempt(
            db, client_ip, login_data.username, False, "User not found", user_agent
        )
        LoggingService.log_auth(
            db=db,
            action="login_failed",
            username=login_data.username,
            ip_address=client_ip,
            success=False,
            error_message="User not found",
            user_agent=user_agent
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check if account is locked
    is_locked, locked_until = SecurityService.is_account_locked(user)
    if is_locked:
        SecurityService.record_login_attempt(
            db, client_ip, login_data.username, False, "Account locked", user_agent
        )
        LoggingService.log_auth(
            db=db,
            action="login_blocked",
            username=login_data.username,
            ip_address=client_ip,
            success=False,
            error_message="Account locked",
            user_agent=user_agent,
            user_id=user.id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account is locked until {locked_until.strftime('%H:%M:%S')}"
        )
    
    # Verify password
    if not verify_password(login_data.password, user.hashed_password):
        SecurityService.record_login_attempt(
            db, client_ip, login_data.username, False, "Wrong password", user_agent
        )
        
        # Increment failed attempts
        was_locked = SecurityService.increment_failed_attempts(db, user)
        
        LoggingService.log_auth(
            db=db,
            action="login_failed",
            username=login_data.username,
            ip_address=client_ip,
            success=False,
            error_message="Incorrect password" + (" - account locked" if was_locked else ""),
            user_agent=user_agent,
            user_id=user.id
        )
        
        if was_locked:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account locked due to too many failed attempts"
            )
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check if user is active
    if not user.is_active:
        SecurityService.record_login_attempt(
            db, client_ip, login_data.username, False, "Account disabled", user_agent
        )
        LoggingService.log_auth(
            db=db,
            action="login_blocked",
            username=login_data.username,
            ip_address=client_ip,
            success=False,
            error_message="Account disabled",
            user_agent=user_agent,
            user_id=user.id
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )
    
    # SUCCESS - Reset failed attempts
    SecurityService.reset_failed_attempts(db, user)
    
    # Create session
    session, was_other_terminated = SecurityService.create_session(
        db, user, client_ip, user_agent
    )
    
    # Update last login
    user.last_login = utcnow()
    db.commit()
    
    # Create access token with session binding
    session_timeout = SecurityService.get_setting_int(db, "session_timeout_minutes", 60)
    access_token_expires = timedelta(minutes=session_timeout)
    access_token = create_access_token(
        data={"sub": user.username},
        session_token=session.session_token,
        expires_delta=access_token_expires
    )
    
    # Record successful login
    SecurityService.record_login_attempt(
        db, client_ip, login_data.username, True, None, user_agent
    )
    
    # Log successful login
    LoggingService.log_auth(
        db=db,
        action="login",
        username=user.username,
        ip_address=client_ip,
        success=True,
        user_agent=user_agent,
        user_id=user.id,
        details={"session_id": session.id, "other_session_terminated": was_other_terminated}
    )
    
    logger.info(f"User {user.username} logged in from {client_ip}" + 
                (" (other session terminated)" if was_other_terminated else ""))
    
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "session_id": session.id,
        "other_session_terminated": was_other_terminated
    }


@router.post("/api/auth/logout")
def logout(
    request: Request, 
    current_user: User = Depends(get_current_user), 
    session_token: str = Depends(get_current_session_token),
    db: Session = Depends(get_db)
):
    """Logout user and terminate current session only"""
    client_ip = get_client_ip(request)
    
    # Terminate only current session (allow multiple sessions)
    if session_token:
        SecurityService.terminate_session(db, session_token)
    
    LoggingService.log_auth(
        db=db,
        action="logout",
        username=current_user.username,
        ip_address=client_ip,
        success=True,
        user_id=current_user.id
    )
    
    logger.info(f"User {current_user.username} logged out")
    return {"message": "Successfully logged out"}


@router.get("/api/auth/me")
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information with permissions"""
    permissions = {}
    if current_user.role:
        permissions = current_user.role.permissions or {}
    elif current_user.is_admin:
        # Admin has all permissions
        from ..services.security_service import SecurityService
        all_perms = SecurityService.get_all_permissions()
        for category, perms in all_perms.items():
            for perm in perms:
                permissions[perm] = True
    
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_admin": current_user.is_admin,
        "is_active": current_user.is_active,
        "role": {
            "id": current_user.role.id if current_user.role else None,
            "name": current_user.role.name if current_user.role else ("admin" if current_user.is_admin else "user"),
            "display_name": current_user.role.display_name if current_user.role else ("Administrator" if current_user.is_admin else "User")
        },
        "permissions": permissions,
        "require_password_change": current_user.require_password_change,
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None,
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None
    }


@router.get("/api/auth/sessions")
def get_my_sessions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get current user's active sessions"""
    sessions = SecurityService.get_user_sessions(db, current_user.id)
    return [{
        "id": s.id,
        "ip_address": s.ip_address,
        "device_info": s.device_info,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "last_activity": s.last_activity.isoformat() if s.last_activity else None,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None
    } for s in sessions]


@router.post("/api/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    user_data: UserCreate, 
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_active_admin)
):
    """
    Register a new user (admin only)
    """
    client_ip = get_client_ip(request)
    
    # Check if username already exists
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    
    # Check if email already exists
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Validate password
    is_valid, errors = SecurityService.validate_password(db, user_data.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(errors)
        )
    
    # Create new user
    hashed_password = get_password_hash(user_data.password)
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        full_name=user_data.full_name,
        hashed_password=hashed_password,
        is_active=True,
        is_admin=False,
        last_password_change=utcnow()
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    LoggingService.log_auth(
        db=db,
        action="user_created",
        username=current_admin.username,
        ip_address=client_ip,
        success=True,
        user_id=current_admin.id,
        details={"new_user": new_user.username}
    )
    
    logger.info(f"New user registered: {new_user.username} by admin {current_admin.username}")
    
    return new_user


@router.get("/api/users", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_active_admin)
):
    """
    List all users (admin only)
    """
    users = db.query(User).all()
    return users


@router.delete("/api/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_active_admin)
):
    """
    Delete a user (admin only)
    """
    client_ip = get_client_ip(request)
    user = db.query(User).filter(User.id == user_id).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Prevent deleting yourself
    if user.id == current_admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )
    
    username = user.username
    
    # Terminate sessions before deletion
    SecurityService.terminate_all_user_sessions(db, user_id)
    
    db.delete(user)
    db.commit()
    
    LoggingService.log_auth(
        db=db,
        action="user_deleted",
        username=current_admin.username,
        ip_address=client_ip,
        success=True,
        user_id=current_admin.id,
        details={"deleted_user": username}
    )
    
    logger.info(f"User {username} deleted by admin {current_admin.username}")
    
    return None


@router.post("/api/auth/change-password")
def change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    request: Request = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Change user password
    """
    client_ip = get_client_ip(request) if request else "unknown"
    
    # Verify old password
    if not verify_password(old_password, current_user.hashed_password):
        LoggingService.log_auth(
            db=db,
            action="password_change_failed",
            username=current_user.username,
            ip_address=client_ip,
            success=False,
            error_message="Incorrect current password",
            user_id=current_user.id
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect password"
        )
    
    # Validate new password
    is_valid, errors = SecurityService.validate_password(db, new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(errors)
        )
    
    # Update password
    current_user.hashed_password = get_password_hash(new_password)
    current_user.last_password_change = utcnow()
    current_user.require_password_change = False
    db.commit()
    
    LoggingService.log_auth(
        db=db,
        action="password_changed",
        username=current_user.username,
        ip_address=client_ip,
        success=True,
        user_id=current_user.id
    )
    
    logger.info(f"User {current_user.username} changed password")
    
    return {"message": "Password changed successfully"}
