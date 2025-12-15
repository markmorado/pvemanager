"""
Authentication module with enhanced security
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from functools import wraps
import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .db import get_db
from .models import User

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Bearer token
security = HTTPBearer()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)


def create_access_token(data: dict, session_token: str = None, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token with optional session binding"""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": expire,
        "iat": now
    })
    
    # Include session token for validation
    if session_token:
        to_encode["session"] = session_token
    
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """Decode JWT access token"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user with session validation"""
    token = credentials.credentials
    payload = decode_access_token(token)
    
    username: str = payload.get("sub")
    session_token: str = payload.get("session")
    
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Validate session if present
    if session_token:
        from .services.security_service import SecurityService
        session = SecurityService.validate_session(db, session_token)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or invalid. Please login again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    
    user = db.query(User).options(joinedload(User.role)).filter(User.username == username).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )
    
    # Check if account is locked
    if user.is_locked():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is locked. Please try again later."
        )
    
    return user


def get_current_session_token(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> str:
    """Get current session token from JWT"""
    token = credentials.credentials
    payload = decode_access_token(token)
    return payload.get("session")


def get_current_active_admin(current_user: User = Depends(get_current_user)) -> User:
    """Get current active admin user"""
    if not current_user.is_admin and (not current_user.role or current_user.role.name != "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required"
        )
    return current_user


class PermissionChecker:
    """
    Dependency for checking permissions.
    Usage: Depends(PermissionChecker("vms.create"))
    
    Supports both legacy (vms.create) and new (vm:create) permission formats.
    """
    def __init__(self, permission: str):
        self.permission = permission
    
    def __call__(self, current_user: User = Depends(get_current_user)) -> User:
        # Use new RBAC engine
        from .rbac import PermissionEngine
        
        if not PermissionEngine.has_permission(current_user, self.permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {self.permission}"
            )
        return current_user


def check_permission(user: User, permission: str) -> bool:
    """Check if user has specific permission"""
    from .rbac import PermissionEngine
    return PermissionEngine.has_permission(user, permission)


def require_permission(user: User, permission: str) -> None:
    """Require user to have specific permission, raise 403 if not"""
    from .rbac import PermissionEngine
    PermissionEngine.check_permission(user, permission)


def get_client_ip(request: Request) -> str:
    """Get client IP from request, handling proxies"""
    # Try X-Forwarded-For first (from nginx/proxy)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    
    # Try X-Real-IP
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    
    # Fall back to direct client
    if request.client:
        return request.client.host
    
    return "unknown"


# Optional auth - allows both authenticated and anonymous access
class OptionalHTTPBearer(HTTPBearer):
    async def __call__(self, request: Request):
        try:
            return await super().__call__(request)
        except HTTPException:
            return None


optional_security = OptionalHTTPBearer()


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user if authenticated, None otherwise"""
    if credentials is None:
        return None
    
    try:
        return get_current_user(credentials, db)
    except HTTPException:
        return None
