"""
Authentication module with enhanced security
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple
from functools import wraps
import secrets
import threading
import time
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


def authenticate_ws_token(token: Optional[str], db: Session) -> Optional[User]:
    """Authenticate a raw JWT (e.g. from an Authorization header).

    Returns the authenticated, active User (with a valid session, if the
    token is session-bound) or ``None`` on any failure. Unlike
    ``decode_access_token`` this never raises. WebSocket handshakes do not
    use it: they authenticate with a ticket (:func:`authenticate_ws`).
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        return None

    username = payload.get("sub")
    if not username:
        return None

    # Validate the bound session, mirroring get_current_user.
    session_token = payload.get("session")
    if session_token:
        from .services.security_service import SecurityService
        if not SecurityService.validate_session(db, session_token):
            return None

    user = db.query(User).options(joinedload(User.role)).filter(User.username == username).first()
    if user is None or not user.is_active:
        return None
    if user.is_locked():
        return None

    return user


# WebSocket tickets. The session JWT used to travel in ?token= on WebSocket
# URLs, where it ends up in proxy logs and browser history. The client now
# trades it for an opaque ticket (POST /api/auth/ws-ticket) right before each
# connect; the ticket is single use, short-lived and bound to the user and
# session. In-memory: the backend runs one uvicorn worker.
_WS_TICKET_TTL = 30.0
_ws_tickets: dict = {}
_ws_tickets_lock = threading.Lock()


def issue_ws_ticket(user_id: int, session_token: Optional[str]) -> str:
    ticket = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _ws_tickets_lock:
        for key in [k for k, v in _ws_tickets.items() if v[2] < now]:
            del _ws_tickets[key]
        _ws_tickets[ticket] = (user_id, session_token, now + _WS_TICKET_TTL)
    return ticket


def authenticate_ws(ticket: Optional[str], db: Session) -> Optional[User]:
    """Authenticate a WebSocket handshake from its ``?ticket=``.

    Redeems the ticket (once) and returns its active, unlocked user, or
    ``None`` on any failure. Never raises — callers close the socket instead.
    """
    return authenticate_ws_session(ticket, db)[0]


def authenticate_ws_session(ticket: Optional[str], db: Session) -> Tuple[Optional[User], Optional[str]]:
    """Like :func:`authenticate_ws`, also returning the session token behind
    the ticket — for :func:`watch_ws_access` on long-lived sockets."""
    if not ticket:
        return None, None
    with _ws_tickets_lock:
        entry = _ws_tickets.pop(ticket, None)
    if not entry or entry[2] < time.monotonic():
        return None, None
    user_id, session_token, _ = entry

    # The session may have been logged out or terminated since issuance.
    if session_token:
        from .services.security_service import SecurityService
        if not SecurityService.validate_session(db, session_token):
            return None, None

    user = db.query(User).options(joinedload(User.role)).filter(User.id == user_id).first()
    if user is None or not user.is_active or user.is_locked():
        return None, None
    return user, session_token


_WS_RECHECK_SECONDS = 30.0


def ws_access_still_valid(user_id: int, session_token: Optional[str],
                         allowed: Optional[Callable[[Session, User], bool]] = None) -> bool:
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if session_token:
            from .services.security_service import SecurityService
            # Read-only: validate_session would slide the expiry, so an idle
            # tab with an open socket would never time out.
            if not SecurityService.is_session_live(db, session_token):
                return False
        user = db.query(User).options(joinedload(User.role)).filter(User.id == user_id).first()
        if user is None or not user.is_active or user.is_locked():
            return False
        return bool(allowed(db, user)) if allowed else True
    finally:
        db.close()


async def watch_ws_access(user_id: int, session_token: Optional[str],
                          allowed: Optional[Callable[[Session, User], bool]] = None,
                          interval: float = _WS_RECHECK_SECONDS) -> None:
    """Return once the user may no longer use a long-lived WebSocket.

    Access was checked only at the handshake: a root console stayed open after
    logout, a password change, a terminated session, an account lock or a
    revoked permission. Run it next to the bridge tasks and close on return.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            ok = await asyncio.to_thread(ws_access_still_valid, user_id, session_token, allowed)
        except Exception:
            continue  # transient DB error: try again next round
        if not ok:
            return


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


def _validate_ip(ip: str) -> bool:
    """Return True if ip is a valid IPv4 or IPv6 address."""
    import ipaddress
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _is_proxy_peer(ip: Optional[str]) -> bool:
    """Whether a direct peer may be a reverse proxy whose headers we trust."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def get_client_ip(request: Request) -> str:
    """Get client IP from request, handling proxies.

    Proxy headers are honoured only when the direct peer is a private/loopback
    address (the bundled nginx on the docker network); a client reaching the
    API directly cannot spoof its address. X-Real-IP is preferred — nginx sets
    it to $remote_addr. For X-Forwarded-For the right-most entry is used: it is
    the one appended by our proxy, while left-most entries are client-supplied
    ($proxy_add_x_forwarded_for keeps them), so trusting the first one let
    anyone rotate fake IPs past brute-force blocking and poison the audit log.
    """
    peer = request.client.host if request.client else None

    if _is_proxy_peer(peer):
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip and _validate_ip(real_ip):
            return real_ip

        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            candidate = forwarded.split(",")[-1].strip()
            if _validate_ip(candidate):
                return candidate

    return peer or "unknown"


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
