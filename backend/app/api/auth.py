import hashlib
import html
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from loguru import logger

from ..db import get_db
from ..models import User, PasswordResetToken
from ..schemas import (
    UserCreate, UserResponse, LoginRequest, Token,
    ForgotPasswordRequest, ResetPasswordRequest,
    TwoFactorStatusResponse, TwoFactorSetupResponse,
    TwoFactorEnableRequest, TwoFactorEnableResponse,
    TwoFactorDisableRequest, MessageResponse,
)
from ..services import two_factor_service as tfa
from ..auth import (
    get_password_hash,
    verify_password,
    create_access_token,
    decode_access_token,
    get_current_user,
    get_current_active_admin,
    get_client_ip,
    get_current_session_token,
    issue_ws_ticket,
    security,
)
from fastapi.security import HTTPAuthorizationCredentials
from ..config import settings, utcnow
from ..logging_service import LoggingService
from ..services.security_service import SecurityService

router = APIRouter()

_DUMMY_HASH: list = []


def _dummy_password_hash() -> str:
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(get_password_hash(secrets.token_urlsafe(16)))
    return _DUMMY_HASH[0]


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
        # Spend the same bcrypt time as a real check, so response timing does not
        # reveal which usernames exist.
        verify_password(login_data.password, _dummy_password_hash())
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
    
    # Two-factor authentication gate (after password + active checks).
    # Password is correct at this point; require the second factor before
    # issuing a token or creating a session.
    if user.two_factor_enabled:
        if not login_data.code:
            return {"two_factor_required": True, "token_type": "bearer"}

        from ..services import two_factor_service as tfa
        code_ok = tfa.verify_totp(user.two_factor_secret, login_data.code, replay_key=user.id)
        if not code_ok:
            backup = list(user.two_factor_backup_codes or [])
            if tfa.consume_backup_code(login_data.code, backup):
                user.two_factor_backup_codes = backup
                db.commit()
                code_ok = True

        if not code_ok:
            SecurityService.record_login_attempt(
                db, client_ip, login_data.username, False, "Invalid 2FA code", user_agent
            )
            # Count it against the account like a wrong password: with the
            # password known, only the per-IP block limited guessing the
            # 6-digit code, and it does not stop an attacker using many IPs.
            if SecurityService.increment_failed_attempts(db, user):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account locked due to too many failed attempts",
                )
            LoggingService.log_auth(
                db=db,
                action="login_failed",
                username=login_data.username,
                ip_address=client_ip,
                success=False,
                error_message="Invalid two-factor code",
                user_agent=user_agent,
                user_id=user.id,
            )
            # Use 400 (not 401) so the SPA's global 401 handler does not force a
            # redirect/reload and wipe the login form + error message.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid two-factor code",
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


@router.post("/api/auth/refresh", response_model=Token)
def refresh_token(
    current_user: User = Depends(get_current_user),
    session_token: str = Depends(get_current_session_token),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Refresh JWT access token without re-login (extends session)"""
    session_timeout = SecurityService.get_setting_int(db, "session_timeout_minutes", 60)
    access_token_expires = timedelta(minutes=session_timeout)
    claims = {"sub": current_user.username}
    # Keep the impersonation link: dropping it stranded the admin in the
    # user's account (no way back via stop-impersonate) and unmarked the
    # session as impersonated in the audit trail.
    impersonator = decode_access_token(credentials.credentials).get("impersonator")
    if impersonator:
        claims["impersonator"] = impersonator
    new_token = create_access_token(
        data=claims,
        session_token=session_token,
        expires_delta=access_token_expires
    )
    return {
        "access_token": new_token,
        "token_type": "bearer"
    }


@router.post("/api/auth/ws-ticket")
def create_ws_ticket(
    current_user: User = Depends(get_current_user),
    session_token: str = Depends(get_current_session_token),
):
    """Single-use, short-lived ticket for one WebSocket handshake (?ticket=).

    Browsers cannot send headers on WebSocket requests; this keeps the session
    JWT out of WebSocket URLs.
    """
    return {"ticket": issue_ws_ticket(current_user.id, session_token)}


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


# ==================== Impersonation ====================


def _user_is_admin(user: User) -> bool:
    """Whether the user has administrator privileges (flag or admin role)."""
    return bool(user.is_admin or (user.role and user.role.name == "admin"))


@router.post("/api/auth/impersonate/{user_id}", response_model=Token)
def impersonate_user(
    user_id: int,
    request: Request,
    admin: User = Depends(get_current_active_admin),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Issue a token acting as another user, keeping a link back to the admin.

    Only administrators may impersonate. The returned JWT carries an
    ``impersonator`` claim (the admin's username) so ``/stop-impersonate`` can
    restore the original admin session. Nested impersonation is rejected.
    """
    # Reject nested impersonation: the acting admin must not already be
    # impersonating someone else.
    payload = decode_access_token(credentials.credentials)
    if payload.get("impersonator"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Already impersonating. Return to your admin account first.",
        )

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot impersonate yourself"
        )
    if not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot impersonate a disabled user"
        )
    if _user_is_admin(target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cannot impersonate another administrator"
        )

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")[:500]

    session, _ = SecurityService.create_session(db, target, client_ip, user_agent)

    session_timeout = SecurityService.get_setting_int(db, "session_timeout_minutes", 60)
    access_token = create_access_token(
        data={"sub": target.username, "impersonator": admin.username},
        session_token=session.session_token,
        expires_delta=timedelta(minutes=session_timeout),
    )

    LoggingService.log_auth(
        db=db,
        action="impersonate_start",
        username=admin.username,
        ip_address=client_ip,
        success=True,
        user_agent=user_agent,
        user_id=admin.id,
        details={"target_user_id": target.id, "target_username": target.username},
    )
    logger.warning(f"Admin {admin.username} started impersonating {target.username}")

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/api/auth/stop-impersonate", response_model=Token)
def stop_impersonating(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Return to the original admin account after impersonating a user."""
    payload = decode_access_token(credentials.credentials)
    impersonator_username = payload.get("impersonator")
    if not impersonator_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Not currently impersonating"
        )

    # The impersonation token is exchanged for a fresh admin token, so it must
    # still be backed by a live session — otherwise a leaked token keeps being
    # redeemable for admin access after logout/termination until its JWT expiry.
    impersonation_session = payload.get("session")
    if not impersonation_session or not SecurityService.validate_session(db, impersonation_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please login again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    admin = db.query(User).filter(User.username == impersonator_username).first()
    if not admin or not admin.is_active or not _user_is_admin(admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Original administrator account is no longer available",
        )

    # Terminate the impersonation session so it does not linger.
    SecurityService.terminate_session(db, impersonation_session)

    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")[:500]

    session, _ = SecurityService.create_session(db, admin, client_ip, user_agent)

    session_timeout = SecurityService.get_setting_int(db, "session_timeout_minutes", 60)
    access_token = create_access_token(
        data={"sub": admin.username},
        session_token=session.session_token,
        expires_delta=timedelta(minutes=session_timeout),
    )

    LoggingService.log_auth(
        db=db,
        action="impersonate_stop",
        username=admin.username,
        ip_address=client_ip,
        success=True,
        user_agent=user_agent,
        user_id=admin.id,
        details={"impersonated_username": payload.get("sub")},
    )
    logger.warning(f"Admin {admin.username} stopped impersonating {payload.get('sub')}")

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/api/auth/me")
def get_current_user_info(
    current_user: User = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Get current user information with permissions"""
    permissions = {}
    if current_user.role:
        permissions = current_user.role.permissions or {}
    elif current_user.is_admin:
        # Admin has all permissions (new resource:action format)
        from ..services.security_service import SecurityService
        all_perms = SecurityService.get_all_permissions_v2()
        for category, perms in all_perms.items():
            for perm in perms:
                permissions[perm] = True
    
    # Expose the impersonator (original admin username) so the SPA can render a
    # "return to admin" banner while acting as another user.
    impersonator = None
    try:
        impersonator = decode_access_token(credentials.credentials).get("impersonator")
    except HTTPException:
        impersonator = None

    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_admin": current_user.is_admin,
        "is_active": current_user.is_active,
        "impersonator": impersonator,
        "role": {
            "id": current_user.role.id if current_user.role else None,
            "name": current_user.role.name if current_user.role else ("admin" if current_user.is_admin else "user"),
            "display_name": current_user.role.display_name if current_user.role else ("Administrator" if current_user.is_admin else "User")
        },
        "permissions": permissions,
        "two_factor_enabled": current_user.two_factor_enabled,
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


# ==================== Two-Factor Authentication ====================

@router.get("/api/auth/2fa/status", response_model=TwoFactorStatusResponse)
def two_factor_status(current_user: User = Depends(get_current_user)):
    """Return whether 2FA is enabled and how many backup codes remain."""
    remaining = len(current_user.two_factor_backup_codes or []) if current_user.two_factor_enabled else 0
    return TwoFactorStatusResponse(
        enabled=bool(current_user.two_factor_enabled),
        backup_codes_remaining=remaining,
    )


@router.post("/api/auth/2fa/setup", response_model=TwoFactorSetupResponse)
def two_factor_setup(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Begin 2FA enrollment: generate a fresh secret and QR code.

    The secret is stored but 2FA is NOT active until confirmed via /enable.
    """
    if current_user.two_factor_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA is already enabled")

    secret = tfa.generate_secret()
    current_user.two_factor_secret = secret
    db.commit()

    uri = tfa.build_otpauth_uri(secret, current_user.username)
    return TwoFactorSetupResponse(secret=secret, otpauth_uri=uri, qr_svg=tfa.qr_svg(uri))


@router.post("/api/auth/2fa/enable", response_model=TwoFactorEnableResponse)
def two_factor_enable(
    data: TwoFactorEnableRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Confirm enrollment by verifying a TOTP code; return one-time backup codes."""
    if current_user.two_factor_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA is already enabled")
    if not current_user.two_factor_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Run 2FA setup first")
    if not tfa.verify_totp(current_user.two_factor_secret, data.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")

    backup_codes = tfa.generate_backup_codes()
    current_user.two_factor_backup_codes = tfa.hash_backup_codes(backup_codes)
    current_user.two_factor_enabled = True
    db.commit()

    LoggingService.log_auth(
        db=db, action="2fa_enabled", username=current_user.username,
        ip_address=get_client_ip(request), success=True, user_id=current_user.id,
    )
    logger.info(f"User {current_user.username} enabled 2FA")
    return TwoFactorEnableResponse(enabled=True, backup_codes=backup_codes)


@router.post("/api/auth/2fa/disable", response_model=MessageResponse)
def two_factor_disable(
    data: TwoFactorDisableRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disable 2FA. Requires the account password (and current code if enabled)."""
    if not verify_password(data.password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

    if current_user.two_factor_enabled:
        code_ok = tfa.verify_totp(current_user.two_factor_secret, data.code or "", replay_key=current_user.id)
        if not code_ok:
            backup = list(current_user.two_factor_backup_codes or [])
            code_ok = tfa.consume_backup_code(data.code or "", backup)
        if not code_ok:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid two-factor code")

    current_user.two_factor_enabled = False
    current_user.two_factor_secret = None
    current_user.two_factor_backup_codes = None
    db.commit()

    LoggingService.log_auth(
        db=db, action="2fa_disabled", username=current_user.username,
        ip_address=get_client_ip(request), success=True, user_id=current_user.id,
    )
    logger.info(f"User {current_user.username} disabled 2FA")
    return MessageResponse(message="Two-factor authentication disabled")


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
    lang = request.cookies.get("language", "en")
    is_valid, errors = SecurityService.validate_password(db, user_data.password, lang)
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
# Note: no explicit CSRF token needed here.
# This endpoint is protected by JWT Bearer token (Authorization header).
# Browsers cannot automatically include the Authorization header in cross-site requests,
# so CSRF attacks are not possible — the attacker cannot obtain or inject the token.
def change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    request: Request = None,
    current_user: User = Depends(get_current_user),
    session_token: str = Depends(get_current_session_token),
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
    lang = request.cookies.get("language", "en")
    is_valid, errors = SecurityService.validate_password(db, new_password, lang)
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

    # A changed password must lock out whoever may have used the old one:
    # end every other session, keep the one that made the change.
    SecurityService.terminate_all_user_sessions(db, current_user.id, except_token=session_token)
    
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


# ==================== Forgot / Reset Password (self-service) ====================

# Generic response returned by /forgot-password regardless of whether the email
# exists — prevents account enumeration.
_FORGOT_PASSWORD_GENERIC = {
    "message": "If an account with that email exists, a password reset link has been sent."
}


def _reset_link_origin(request: Request) -> str:
    """Origin the password-reset link points to.

    The request's Origin/Host headers are attacker-controlled: anyone can ask
    for a reset of the victim's address with ``Origin: https://evil.example``
    and the emailed link would hand the one-time token to that host (reset
    poisoning). Prefer the configured PANEL_URL, then a single explicit
    CORS origin; the request origin is only a last-resort fallback.
    """
    if settings.PANEL_URL:
        return settings.PANEL_URL.rstrip("/")
    cors = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    if len(cors) == 1 and cors[0] != "*":
        return cors[0].rstrip("/")
    logger.warning(
        "PANEL_URL is not set: password-reset links are built from request headers. "
        "Set PANEL_URL (e.g. https://panel.example.com) to prevent reset-link poisoning."
    )
    return (request.headers.get("origin") or str(request.base_url)).rstrip("/")


def _hash_reset_token(raw_token: str) -> str:
    """Hash a raw reset token for storage/lookup (only the hash is persisted)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def _send_reset_email(to_email: str, display_name: str, lang: str, link: str, expire_minutes: int) -> None:
    """Send the password-reset email via the configured SMTP channel.

    Runs as a FastAPI background task so the HTTP response time does not leak
    whether the address exists.
    """
    try:
        from ..services.notification_channels import get_email_channel, get_panel_name
        panel = get_panel_name()
        name = display_name or to_email

        if (lang or "").startswith("ru"):
            subject = f"{panel}: восстановление пароля"
            body = (
                f"Здравствуйте, {name}!\n\n"
                f"Поступил запрос на сброс пароля для вашего аккаунта в {panel}.\n"
                f"Чтобы задать новый пароль, перейдите по ссылке (действует {expire_minutes} мин.):\n{link}\n\n"
                f"Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо — "
                f"ваш текущий пароль останется без изменений."
            )
            btn = "Сбросить пароль"
            note = f"Ссылка действительна {expire_minutes} минут. Если вы не запрашивали сброс — проигнорируйте письмо."
        else:
            subject = f"{panel}: password reset"
            body = (
                f"Hello, {name}!\n\n"
                f"We received a request to reset the password for your {panel} account.\n"
                f"To set a new password, open this link (valid for {expire_minutes} min):\n{link}\n\n"
                f"If you did not request a password reset, you can safely ignore this email — "
                f"your current password will remain unchanged."
            )
            btn = "Reset password"
            note = f"This link is valid for {expire_minutes} minutes. If you didn't request a reset, ignore this email."

        # full_name is user-editable — escape everything interpolated into HTML.
        esc = html.escape
        html_body = f"""
        <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;color:#1a1a1a">
          <h2 style="font-size:20px;margin:0 0 16px">{esc(panel)}</h2>
          <p style="font-size:15px;line-height:1.5;margin:0 0 20px">{esc(body.splitlines()[0])}</p>
          <p style="margin:0 0 24px">
            <a href="{esc(link)}" style="display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;
               padding:10px 20px;border-radius:8px;font-size:15px;font-weight:600">{btn}</a>
          </p>
          <p style="font-size:13px;color:#666;line-height:1.5;margin:0 0 8px">{note}</p>
          <p style="font-size:12px;color:#999;word-break:break-all;margin:0">{esc(link)}</p>
        </div>
        """

        channel = get_email_channel()
        ok = await channel.send(to_email, subject, body, html_body)
        if ok:
            logger.info(f"Password reset email sent to {to_email}")
        else:
            logger.warning(f"Password reset email NOT sent to {to_email} (SMTP not configured or send failed)")
    except Exception as e:
        logger.error(f"Failed to send password reset email to {to_email}: {e}")


@router.post("/api/auth/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Start the self-service password reset flow.

    Always returns a generic message (no account enumeration). When the email
    belongs to an active user, a one-time reset link is emailed via SMTP.
    """
    client_ip = get_client_ip(request)
    email = payload.email.strip()

    user = db.query(User).filter(User.email.ilike(email)).first()

    if not user or not user.is_active:
        LoggingService.log_auth(
            db=db,
            action="password_reset_requested",
            username=email,
            ip_address=client_ip,
            success=False,
            error_message="Unknown or inactive email",
        )
        return _FORGOT_PASSWORD_GENERIC

    # Throttle: ignore repeat requests within 60s to avoid email spam
    recent = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.created_at >= utcnow() - timedelta(seconds=60),
        )
        .first()
    )
    if recent:
        return _FORGOT_PASSWORD_GENERIC

    # Invalidate any previously issued (unused) tokens for this user
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: utcnow()}, synchronize_session=False)

    raw_token = secrets.token_urlsafe(32)
    expire_minutes = settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_reset_token(raw_token),
        expires_at=utcnow() + timedelta(minutes=expire_minutes),
        ip_address=client_ip,
    )
    db.add(reset)
    db.commit()

    link = f"{_reset_link_origin(request)}/reset-password?token={raw_token}"

    background.add_task(
        _send_reset_email,
        user.email,
        user.full_name or user.username,
        user.language or "ru",
        link,
        expire_minutes,
    )

    LoggingService.log_auth(
        db=db,
        action="password_reset_requested",
        username=user.username,
        ip_address=client_ip,
        success=True,
        user_id=user.id,
    )
    logger.info(f"Password reset requested for {user.username} from {client_ip}")

    return _FORGOT_PASSWORD_GENERIC


@router.get("/api/auth/reset-password/validate")
def validate_reset_token(token: str, db: Session = Depends(get_db)):
    """Check whether a reset token is still valid (for the reset form to gate UI)."""
    reset = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash_reset_token(token))
        .first()
    )
    return {"valid": bool(reset and reset.is_valid())}


@router.post("/api/auth/reset-password")
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Complete the reset flow: validate the one-time token and set a new password."""
    client_ip = get_client_ip(request)

    reset = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash_reset_token(payload.token))
        .first()
    )

    if not reset or not reset.is_valid():
        LoggingService.log_auth(
            db=db,
            action="password_reset_failed",
            username="unknown",
            ip_address=client_ip,
            success=False,
            error_message="Invalid or expired reset token",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        )

    user = db.query(User).filter(User.id == reset.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired",
        )

    # Validate the new password against the panel's password policy
    lang = user.language or "en"
    is_valid, errors = SecurityService.validate_password(db, payload.new_password, lang)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(errors),
        )

    # Apply the new password and clear any lock state
    user.hashed_password = get_password_hash(payload.new_password)
    user.last_password_change = utcnow()
    user.require_password_change = False
    user.failed_login_attempts = 0
    user.locked_until = None

    # Consume this token and invalidate any other outstanding ones
    reset.used_at = utcnow()
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: utcnow()}, synchronize_session=False)
    db.commit()

    # Force re-login everywhere
    SecurityService.terminate_all_user_sessions(db, user.id)

    LoggingService.log_auth(
        db=db,
        action="password_reset",
        username=user.username,
        ip_address=client_ip,
        success=True,
        user_id=user.id,
    )
    logger.info(f"Password reset completed for {user.username} from {client_ip}")

    return {"message": "Password has been reset successfully"}
