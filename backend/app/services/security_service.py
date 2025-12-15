"""
Security Service - Authentication, sessions, and protection
"""

import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func
from loguru import logger

from ..models import (
    User, Role, ActiveSession, LoginAttempt, BlockedIP, SecuritySetting
)


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


class SecurityService:
    """Service for security operations"""
    
    # Cache for security settings
    _settings_cache: Dict[str, str] = {}
    _cache_time: datetime = None
    _cache_ttl = 60  # seconds
    
    @classmethod
    def get_setting(cls, db: Session, key: str, default: str = None) -> str:
        """Get security setting with caching"""
        # Check cache
        now = utcnow()
        if cls._cache_time and (now - cls._cache_time).seconds < cls._cache_ttl:
            if key in cls._settings_cache:
                return cls._settings_cache[key]
        
        # Query from DB
        setting = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
        value = setting.value if setting else default
        
        # Update cache
        cls._settings_cache[key] = value
        cls._cache_time = now
        
        return value
    
    @classmethod
    def get_setting_int(cls, db: Session, key: str, default: int = 0) -> int:
        """Get security setting as integer"""
        value = cls.get_setting(db, key, str(default))
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    
    @classmethod
    def get_setting_bool(cls, db: Session, key: str, default: bool = False) -> bool:
        """Get security setting as boolean"""
        value = cls.get_setting(db, key, str(default).lower())
        
        # Handle None/empty values gracefully
        if value is None:
            return default
        
        # Accept real booleans from DB/cache
        if isinstance(value, bool):
            return value
        
        return str(value).lower() in ('true', '1', 'yes', 'on')
    
    @classmethod
    def update_setting(cls, db: Session, key: str, value: str) -> bool:
        """Update security setting"""
        setting = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
        if setting:
            setting.value = value
            setting.updated_at = utcnow()
        else:
            setting = SecuritySetting(key=key, value=value)
            db.add(setting)
        
        db.commit()
        
        # Clear cache
        cls._settings_cache.pop(key, None)
        return True
    
    # ==================== IP Blocking ====================
    
    @staticmethod
    def is_ip_blocked(db: Session, ip_address: str) -> Tuple[bool, Optional[str]]:
        """Check if IP is blocked, returns (is_blocked, reason)"""
        blocked = db.query(BlockedIP).filter(BlockedIP.ip_address == ip_address).first()
        
        if not blocked:
            return False, None
        
        if blocked.is_permanent:
            return True, blocked.reason
        
        # Compare timezone-aware datetimes
        now = utcnow()
        if blocked.expires_at and blocked.expires_at > now:
            return True, blocked.reason
        
        # Block expired, remove it
        db.delete(blocked)
        db.commit()
        return False, None
    
    @staticmethod
    def block_ip(
        db: Session, 
        ip_address: str, 
        reason: str, 
        blocked_by: str = "system",
        duration_minutes: int = 60,
        permanent: bool = False
    ) -> BlockedIP:
        """Block an IP address"""
        blocked = db.query(BlockedIP).filter(BlockedIP.ip_address == ip_address).first()
        
        if blocked:
            blocked.reason = reason
            blocked.blocked_by = blocked_by
            blocked.blocked_at = utcnow()
            blocked.attempts_count += 1
            blocked.is_permanent = permanent
            if not permanent:
                blocked.expires_at = utcnow() + timedelta(minutes=duration_minutes)
        else:
            blocked = BlockedIP(
                ip_address=ip_address,
                reason=reason,
                blocked_by=blocked_by,
                expires_at=None if permanent else utcnow() + timedelta(minutes=duration_minutes),
                is_permanent=permanent,
                attempts_count=1
            )
            db.add(blocked)
        
        db.commit()
        logger.warning(f"IP {ip_address} blocked: {reason}")
        return blocked
    
    @staticmethod
    def unblock_ip(db: Session, ip_address: str) -> bool:
        """Unblock an IP address"""
        blocked = db.query(BlockedIP).filter(BlockedIP.ip_address == ip_address).first()
        if blocked:
            db.delete(blocked)
            db.commit()
            logger.info(f"IP {ip_address} unblocked")
            return True
        return False
    
    # ==================== Login Attempts ====================
    
    @staticmethod
    def record_login_attempt(
        db: Session,
        ip_address: str,
        username: str = None,
        success: bool = False,
        failure_reason: str = None,
        user_agent: str = None
    ) -> LoginAttempt:
        """Record a login attempt"""
        attempt = LoginAttempt(
            username=username,
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else None,
            success=success,
            failure_reason=failure_reason
        )
        db.add(attempt)
        db.commit()
        return attempt
    
    @classmethod
    def check_brute_force(cls, db: Session, ip_address: str, username: str = None) -> Tuple[bool, str]:
        """
        Check for brute force attacks
        Returns (should_block, reason)
        """
        threshold = cls.get_setting_int(db, "ip_block_threshold", 10)
        window_minutes = 30
        
        # Count recent failed attempts from this IP
        since = utcnow() - timedelta(minutes=window_minutes)
        
        failed_count = db.query(LoginAttempt).filter(
            and_(
                LoginAttempt.ip_address == ip_address,
                LoginAttempt.success == False,
                LoginAttempt.attempted_at > since
            )
        ).count()
        
        if failed_count >= threshold:
            return True, f"Too many failed login attempts ({failed_count})"
        
        return False, None
    
    # ==================== Session Management ====================
    
    @staticmethod
    def generate_session_token() -> str:
        """Generate a secure session token"""
        return secrets.token_hex(32)
    
    @classmethod
    def create_session(
        cls,
        db: Session,
        user: User,
        ip_address: str = None,
        user_agent: str = None
    ) -> Tuple[ActiveSession, bool]:
        """
        Create a new session for user.
        Returns (session, was_other_session_terminated)
        """
        session_timeout = cls.get_setting_int(db, "session_timeout_minutes", 60)
        
        terminated_other = False
        
        # Create new session
        token = cls.generate_session_token()
        expires_at = utcnow() + timedelta(minutes=session_timeout)
        
        # Parse device info from user agent
        device_info = cls._parse_device_info(user_agent) if user_agent else None
        
        session = ActiveSession(
            user_id=user.id,
            session_token=token,
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else None,
            device_info=device_info,
            expires_at=expires_at,
            is_active=True
        )
        
        db.add(session)
        db.commit()
        db.refresh(session)
        
        logger.info(f"Session created for user {user.username} from {ip_address}")
        
        return session, terminated_other
    
    @staticmethod
    def _parse_device_info(user_agent: str) -> str:
        """Parse device info from user agent"""
        if not user_agent:
            return "Unknown"
        
        ua = user_agent.lower()
        
        # Browser detection
        browser = "Unknown"
        if "firefox" in ua:
            browser = "Firefox"
        elif "edg" in ua:
            browser = "Edge"
        elif "chrome" in ua:
            browser = "Chrome"
        elif "safari" in ua:
            browser = "Safari"
        elif "opera" in ua:
            browser = "Opera"
        
        # OS detection
        os = "Unknown"
        if "windows" in ua:
            os = "Windows"
        elif "mac os" in ua or "macos" in ua:
            os = "macOS"
        elif "linux" in ua:
            os = "Linux"
        elif "android" in ua:
            os = "Android"
        elif "iphone" in ua or "ipad" in ua:
            os = "iOS"
        
        return f"{browser} on {os}"
    
    @staticmethod
    def validate_session(db: Session, token: str) -> Optional[ActiveSession]:
        """Validate session token and return session if valid"""
        session = db.query(ActiveSession).filter(
            and_(
                ActiveSession.session_token == token,
                ActiveSession.is_active == True
            )
        ).first()
        
        if not session:
            return None
        
        # Check expiration
        # Compare timezone-aware datetimes
        now = utcnow()
        if session.expires_at < now:
            session.is_active = False
            db.commit()
            return None
        
        # Update last activity and extend session expiration (sliding expiration)
        session.last_activity = utcnow()
        # Get session timeout setting
        session_timeout = SecurityService.get_setting_int(db, "session_timeout_minutes", 60)
        session.expires_at = utcnow() + timedelta(minutes=session_timeout)
        db.commit()
        
        return session
    
    @staticmethod
    def terminate_session(db: Session, token: str) -> bool:
        """Terminate a session"""
        session = db.query(ActiveSession).filter(
            ActiveSession.session_token == token
        ).first()
        
        if session:
            session.is_active = False
            db.commit()
            return True
        return False
    
    @staticmethod
    def terminate_all_user_sessions(db: Session, user_id: int) -> int:
        """Terminate all sessions for a user"""
        count = db.query(ActiveSession).filter(
            and_(
                ActiveSession.user_id == user_id,
                ActiveSession.is_active == True
            )
        ).update({"is_active": False})
        db.commit()
        return count
    
    @staticmethod
    def get_user_sessions(db: Session, user_id: int) -> list:
        """Get all active sessions for a user"""
        return db.query(ActiveSession).filter(
            and_(
                ActiveSession.user_id == user_id,
                ActiveSession.is_active == True
            )
        ).all()
    
    @staticmethod
    def cleanup_expired_sessions(db: Session) -> int:
        """Clean up expired sessions"""
        count = db.query(ActiveSession).filter(
            or_(
                ActiveSession.expires_at < utcnow(),
                ActiveSession.is_active == False
            )
        ).delete()
        db.commit()
        logger.info(f"Cleaned up {count} expired sessions")
        return count
    
    # ==================== Account Locking ====================
    
    @classmethod
    def increment_failed_attempts(cls, db: Session, user: User) -> bool:
        """
        Increment failed login attempts for user.
        Returns True if account was locked.
        """
        max_attempts = cls.get_setting_int(db, "max_login_attempts", 5)
        lockout_minutes = cls.get_setting_int(db, "lockout_duration_minutes", 30)
        
        user.failed_login_attempts += 1
        
        if user.failed_login_attempts >= max_attempts:
            user.locked_until = utcnow() + timedelta(minutes=lockout_minutes)
            db.commit()
            logger.warning(f"User {user.username} locked out after {user.failed_login_attempts} failed attempts")
            return True
        
        db.commit()
        return False
    
    @staticmethod
    def reset_failed_attempts(db: Session, user: User):
        """Reset failed login attempts after successful login"""
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()
    
    @staticmethod
    def is_account_locked(user: User) -> Tuple[bool, Optional[datetime]]:
        """Check if account is locked, returns (is_locked, locked_until)"""
        if not user.locked_until:
            return False, None
        
        # Compare timezone-aware datetimes
        if user.locked_until > utcnow():
            return True, user.locked_until
        
        return False, None
    
    # ==================== Role & Permission Management ====================
    
    @staticmethod
    def get_role(db: Session, role_name: str) -> Optional[Role]:
        """Get role by name"""
        return db.query(Role).filter(Role.name == role_name).first()
    
    @staticmethod
    def get_all_roles(db: Session, include_inactive: bool = False) -> list:
        """Get all roles"""
        query = db.query(Role)
        if not include_inactive:
            query = query.filter(Role.is_active == True)
        return query.order_by(Role.name).all()
    
    @staticmethod
    def create_role(
        db: Session,
        name: str,
        display_name: str,
        description: str = None,
        permissions: dict = None
    ) -> Role:
        """Create a new role"""
        role = Role(
            name=name.lower(),
            display_name=display_name,
            description=description,
            permissions=permissions or {},
            is_system=False
        )
        db.add(role)
        db.commit()
        db.refresh(role)
        return role
    
    @staticmethod
    def update_role(
        db: Session,
        role_id: int,
        display_name: str = None,
        description: str = None,
        permissions: dict = None
    ) -> Optional[Role]:
        """Update a role"""
        role = db.query(Role).filter(Role.id == role_id).first()
        if not role:
            return None
        
        if display_name:
            role.display_name = display_name
        if description is not None:
            role.description = description
        if permissions is not None:
            role.permissions = permissions
        
        role.updated_at = utcnow()
        db.commit()
        db.refresh(role)
        return role
    
    @staticmethod
    def delete_role(db: Session, role_id: int) -> Tuple[bool, str]:
        """Delete a role if it's not a system role"""
        role = db.query(Role).filter(Role.id == role_id).first()
        if not role:
            return False, "Role not found"
        
        if role.is_system:
            return False, "Cannot delete system role"
        
        # Check if any users have this role
        user_count = db.query(User).filter(User.role_id == role_id).count()
        if user_count > 0:
            return False, f"Cannot delete role with {user_count} assigned users"
        
        db.delete(role)
        db.commit()
        return True, "Role deleted"
    
    @staticmethod
    def get_all_permissions() -> Dict[str, Dict[str, str]]:
        """
        Get all available permissions grouped by category.
        Returns permissions in legacy format for backwards compatibility with UI.
        """
        try:
            from ..rbac import get_permission_categories_legacy
            return get_permission_categories_legacy()
        except ImportError:
            # Fallback to static list if RBAC module not available
            return {
                "Dashboard": {
                    "dashboard.view": "View dashboard"
                },
                "Proxmox Servers": {
                    "proxmox.view": "View servers",
                    "proxmox.manage": "Manage server settings",
                    "proxmox.servers.add": "Add new servers",
                    "proxmox.servers.edit": "Edit servers",
                    "proxmox.servers.delete": "Delete servers"
                },
                "Virtual Machines": {
                    "vms.view": "View VMs",
                    "vms.create": "Create VMs",
                    "vms.start": "Start VMs",
                    "vms.stop": "Stop VMs",
                    "vms.restart": "Restart VMs",
                    "vms.delete": "Delete VMs",
                    "vms.console": "Access VM console",
                    "vms.migrate": "Migrate VMs"
                },
                "Templates": {
                    "templates.view": "View templates",
                    "templates.manage": "Manage templates"
                },
                "IPAM": {
                    "ipam.view": "View IPAM",
                    "ipam.manage": "Manage IPAM"
                },
                "Logs": {
                    "logs.view": "View logs",
                    "logs.export": "Export logs",
                    "logs.delete": "Delete logs"
                },
                "Settings": {
                    "settings.view": "View settings",
                    "settings.panel": "Panel settings",
                    "settings.security": "Security settings"
                },
                "Users": {
                    "users.view": "View users",
                    "users.create": "Create users",
                    "users.edit": "Edit users",
                    "users.delete": "Delete users"
                },
                "Roles": {
                    "roles.view": "View roles",
                    "roles.manage": "Manage roles"
                },
                "Notifications": {
                    "notifications.manage": "Manage notifications"
                }
            }
    
    @staticmethod
    def get_all_permissions_v2() -> Dict[str, Dict[str, str]]:
        """
        Get all available permissions in new format grouped by category.
        Uses new resource:action format.
        """
        try:
            from ..rbac import get_permission_categories
            return get_permission_categories()
        except ImportError:
            return SecurityService.get_all_permissions()
    
    # ==================== Password Validation ====================
    
    @classmethod
    def validate_password(cls, db: Session, password: str) -> Tuple[bool, list]:
        """
        Validate password against security requirements.
        Returns (is_valid, list_of_errors)
        """
        errors = []
        
        min_length = cls.get_setting_int(db, "password_min_length", 8)
        require_upper = cls.get_setting_bool(db, "password_require_uppercase", True)
        require_lower = cls.get_setting_bool(db, "password_require_lowercase", True)
        require_numbers = cls.get_setting_bool(db, "password_require_numbers", True)
        require_special = cls.get_setting_bool(db, "password_require_special", False)
        
        if len(password) < min_length:
            errors.append(f"Password must be at least {min_length} characters")
        
        if require_upper and not any(c.isupper() for c in password):
            errors.append("Password must contain at least one uppercase letter")
        
        if require_lower and not any(c.islower() for c in password):
            errors.append("Password must contain at least one lowercase letter")
        
        if require_numbers and not any(c.isdigit() for c in password):
            errors.append("Password must contain at least one number")
        
        if require_special and not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password):
            errors.append("Password must contain at least one special character")
        
        return len(errors) == 0, errors
