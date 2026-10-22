from .base import *
from sqlalchemy.orm import backref

# Association table: User <-> ProxmoxServer (assigned servers)
user_servers = Table(
    'user_servers',
    Base.metadata,
    Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Column('server_id', Integer, ForeignKey('proxmox_servers.id', ondelete='CASCADE'), primary_key=True)
)

class Role(Base):
    """User roles with permissions"""
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    permissions = Column(JSON, nullable=False, default={})
    is_system = Column(Boolean, default=False, nullable=False)  # System roles can't be deleted
    is_active = Column(Boolean, default=True, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    users = relationship("User", back_populates="role")

    def has_permission(self, permission: str) -> bool:
        """Check if role has specific permission"""
        if not self.permissions:
            return False
        return self.permissions.get(permission, False)

    def __repr__(self):
        return f"<Role(id={self.id}, name='{self.name}')>"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=True)
    
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)  # Legacy, use role instead
    
    # Role-based access
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True, index=True)
    role = relationship("Role", back_populates="users", lazy="joined")
    
    # SSH Public Key for VM/LXC deployment
    ssh_public_key = Column(Text, nullable=True)

    # UI language preference (ru/en)
    language = Column(String(10), nullable=False, server_default='ru')
    
    # Security fields
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_password_change = Column(DateTime(timezone=True), nullable=True)
    require_password_change = Column(Boolean, default=False, nullable=False)
    two_factor_enabled = Column(Boolean, default=False, nullable=False)
    # Encrypted at rest (Fernet) — see migration 44; legacy plaintext rows are
    # still readable (decrypt falls back) until the migration rewrites them.
    two_factor_secret = Column(EncryptedString(255), nullable=True)
    # List of SHA-256 hashes of single-use recovery codes (JSON array).
    two_factor_backup_codes = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    sessions = relationship("ActiveSession", back_populates="user", cascade="all, delete-orphan")
    assigned_servers = relationship("ProxmoxServer", secondary=user_servers, back_populates="assigned_users", lazy="select")
    
    def has_permission(self, permission: str) -> bool:
        """
        Check if user has specific permission.
        Uses new RBAC engine with legacy format support.
        """
        try:
            from ..rbac import PermissionEngine
            return PermissionEngine.has_permission(self, permission)
        except ImportError:
            if self.is_admin:
                return True
            if self.role:
                return self.role.has_permission(permission)
            return False
    
    def get_permissions(self) -> set:
        """Get all effective permissions for this user"""
        try:
            from ..rbac import PermissionEngine
            return PermissionEngine.get_user_permissions(self)
        except ImportError:
            if self.is_admin:
                return set()  # Admin has all
            if self.role and self.role.permissions:
                return {k for k, v in self.role.permissions.items() if v}
            return set()
    
    def is_locked(self) -> bool:
        """Check if account is locked"""
        if self.locked_until and make_tz_aware(self.locked_until) > utcnow():
            return True
        return False
    
    @property
    def role_name(self) -> str:
        """Get role name"""
        if self.role:
            return self.role.name
        return "admin" if self.is_admin else "user"
    
    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', is_admin={self.is_admin})>"

class UserQuota(Base):
    """Per-user resource quota (limits).

    A row is created only when an admin sets at least one limit. Absence of a
    row — or a ``NULL`` value in any column — means *unlimited* for that metric.
    Memory is stored in MB and disk in GB, matching the units used by the
    ``vm_instances`` cache (see monitoring worker conversions).
    """
    __tablename__ = "user_quotas"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    max_instances = Column(Integer, nullable=True)   # NULL = unlimited
    max_cores = Column(Integer, nullable=True)        # sum of vCPUs
    max_memory_mb = Column(Integer, nullable=True)    # sum of RAM in MB
    max_disk_gb = Column(Integer, nullable=True)      # sum of disk in GB
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", backref=backref("quota", uselist=False, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<UserQuota(user_id={self.user_id})>"

class PasswordResetToken(Base):
    """One-time tokens for the self-service 'forgot password' flow.

    Only a SHA-256 hash of the token is stored; the raw token lives solely in
    the emailed link. A token is single-use (``used_at``) and time-limited
    (``expires_at``).
    """
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")

    def is_valid(self) -> bool:
        """Token is unused and not expired."""
        if self.used_at is not None:
            return False
        if self.expires_at is None or make_tz_aware(self.expires_at) <= utcnow():
            return False
        return True

    def __repr__(self):
        return f"<PasswordResetToken(id={self.id}, user_id={self.user_id}, used={self.used_at is not None})>"

class ActiveSession(Base):
    """Active user sessions for single-session enforcement"""
    __tablename__ = "active_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_token = Column(String(64), unique=True, nullable=False, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    device_info = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_activity = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="sessions")

    def is_expired(self) -> bool:
        """Check if session is expired"""
        from datetime import timezone
        now = datetime.now(timezone.utc)
        if self.expires_at is None:
            return True
        return now > make_tz_aware(self.expires_at)

    def __repr__(self):
        return f"<ActiveSession(id={self.id}, user_id={self.user_id}, active={self.is_active})>"

class LoginAttempt(Base):
    """Login attempts for brute-force protection"""
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), nullable=True, index=True)
    ip_address = Column(String(45), nullable=False, index=True)
    user_agent = Column(String(500), nullable=True)
    success = Column(Boolean, default=False, nullable=False)
    failure_reason = Column(String(200), nullable=True)
    attempted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<LoginAttempt(id={self.id}, ip={self.ip_address}, success={self.success})>"

class BlockedIP(Base):
    """Blocked IP addresses"""
    __tablename__ = "blocked_ips"

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String(45), unique=True, nullable=False, index=True)
    reason = Column(String(500), nullable=True)
    blocked_by = Column(String(100), nullable=True)
    blocked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_permanent = Column(Boolean, default=False, nullable=False)
    attempts_count = Column(Integer, default=0, nullable=False)

    def is_blocked(self) -> bool:
        """Check if IP is currently blocked"""
        if self.is_permanent:
            return True
        if self.expires_at and make_tz_aware(self.expires_at) > utcnow():
            return True
        return False

    def __repr__(self):
        return f"<BlockedIP(id={self.id}, ip={self.ip_address})>"

class UserSSHKey(Base):
    """SSH key attached to a user account."""
    __tablename__ = "user_ssh_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    public_key = Column(Text, nullable=False)
    private_key = Column(EncryptedString(8000), nullable=True)
    fingerprint = Column(String(100), nullable=True, index=True)
    comment = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", backref="ssh_keys_pool")

    __table_args__ = (
        Index('idx_user_ssh_keys_user', 'user_id'),
    )

    def __repr__(self):
        return f"<UserSSHKey(id={self.id}, user_id={self.user_id}, name='{self.name}')>"
