from sqlalchemy import Boolean, Column, Integer, BigInteger, String, DateTime, Text, Index, JSON, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from typing import Optional

from .db import Base


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


# ==================== RBAC Models ====================

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
    
    # Security fields
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_password_change = Column(DateTime(timezone=True), nullable=True)
    require_password_change = Column(Boolean, default=False, nullable=False)
    two_factor_enabled = Column(Boolean, default=False, nullable=False)
    two_factor_secret = Column(String(100), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    sessions = relationship("ActiveSession", back_populates="user", cascade="all, delete-orphan")
    
    def has_permission(self, permission: str) -> bool:
        """
        Check if user has specific permission.
        Uses new RBAC engine with legacy format support.
        """
        # Import here to avoid circular imports
        try:
            from .rbac import PermissionEngine
            return PermissionEngine.has_permission(self, permission)
        except ImportError:
            # Fallback for tests or if rbac module not available
            if self.is_admin:
                return True
            if self.role:
                return self.role.has_permission(permission)
            return False
    
    def get_permissions(self) -> set:
        """Get all effective permissions for this user"""
        try:
            from .rbac import PermissionEngine
            return PermissionEngine.get_user_permissions(self)
        except ImportError:
            if self.is_admin:
                return set()  # Admin has all
            if self.role and self.role.permissions:
                return {k for k, v in self.role.permissions.items() if v}
            return set()
    
    def is_locked(self) -> bool:
        """Check if account is locked"""
        if self.locked_until and self.locked_until > utcnow():
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
        # Compare timezone-aware datetimes
        return now > self.expires_at

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
        if self.expires_at and self.expires_at > utcnow():
            return True
        return False

    def __repr__(self):
        return f"<BlockedIP(id={self.id}, ip={self.ip_address})>"


class SecuritySetting(Base):
    """Security settings"""
    __tablename__ = "security_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<SecuritySetting(key='{self.key}', value='{self.value}')>"


class Notification(Base):
    """User notifications"""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(50), nullable=False)
    level = Column(String(20), nullable=False)
    title = Column(String(255), nullable=False)
    message = Column(Text)
    data = Column(JSON)
    link = Column(String(500))
    source = Column(String(50))
    source_id = Column(String(100))
    read = Column(Boolean, default=False)
    read_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type,
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "data": self.data,
            "link": self.link,
            "source": self.source,
            "source_id": self.source_id,
            "read": self.read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class NotificationPreference(Base):
    """User notification preferences"""
    __tablename__ = "notification_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    email_enabled = Column(Boolean, default=False)
    email_critical_only = Column(Boolean, default=True)
    telegram_enabled = Column(Boolean, default=False)
    telegram_chat_id = Column(String(100))
    webhook_url = Column(String(500))
    notification_levels = Column(JSON, default=["critical", "warning", "info", "success"])
    notification_types = Column(JSON, default=["vm_status", "resource_alert", "system"])
    quiet_hours_start = Column(String(5))
    quiet_hours_end = Column(String(5))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "email_enabled": self.email_enabled,
            "email_critical_only": self.email_critical_only,
            "telegram_enabled": self.telegram_enabled,
            "telegram_chat_id": self.telegram_chat_id,
            "webhook_url": self.webhook_url,
            "notification_levels": self.notification_levels,
            "notification_types": self.notification_types,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OSTemplateGroup(Base):
    """Group of OS templates (e.g., 'Ubuntu', 'Debian', 'Windows')"""
    __tablename__ = "os_template_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)  # e.g., "Ubuntu", "CentOS"
    icon = Column(String(200), nullable=True)  # FA icon HTML e.g., '<i class="fa-brands fa-ubuntu os-icon"></i>'
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, default=True, nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<OSTemplateGroup(id={self.id}, name='{self.name}')>"


class OSTemplate(Base):
    """OS Template linked to Proxmox VM template"""
    __tablename__ = "os_templates"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, nullable=False, index=True)  # FK to OSTemplateGroup
    server_id = Column(Integer, nullable=False, index=True)  # FK to ProxmoxServer
    
    name = Column(String(100), nullable=False)  # Display name, e.g., "Ubuntu 22.04 LTS"
    vmid = Column(Integer, nullable=False)  # Proxmox template VMID on source node
    node = Column(String(100), nullable=True)  # Primary/source node (optional for cross-node templates)
    
    # Cross-node replication support
    source_node = Column(String(100), nullable=True)  # Primary node where template exists
    replicated_nodes = Column(JSON, nullable=True, default={})  # {"node_name": vmid, ...}
    
    # Default VM configuration
    default_cores = Column(Integer, nullable=False, default=1)
    default_memory = Column(Integer, nullable=False, default=1024)  # in MB
    default_disk = Column(Integer, nullable=False, default=10)  # in GB
    min_cores = Column(Integer, nullable=False, default=1)
    min_memory = Column(Integer, nullable=False, default=512)  # in MB
    min_disk = Column(Integer, nullable=False, default=5)  # in GB
    
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_os_template_group', 'group_id'),
        Index('idx_os_template_server', 'server_id'),
        Index('idx_os_template_vmid', 'server_id', 'vmid'),
    )
    
    def get_source_node(self) -> str:
        """Get the primary source node for this template"""
        return self.source_node or self.node
    
    def get_vmid_for_node(self, node_name: str) -> Optional[int]:
        """Get template VMID for specific node, returns None if not replicated"""
        source = self.get_source_node()
        if node_name == source:
            return self.vmid
        if self.replicated_nodes and node_name in self.replicated_nodes:
            return self.replicated_nodes[node_name]
        return None
    
    def add_replicated_node(self, node_name: str, vmid: int):
        """Add a replicated node entry"""
        if not self.replicated_nodes:
            self.replicated_nodes = {}
        self.replicated_nodes[node_name] = vmid

    def __repr__(self):
        return f"<OSTemplate(id={self.id}, name='{self.name}', vmid={self.vmid})>"


class ProxmoxServer(Base):
    """Proxmox VE server with API token authentication"""
    __tablename__ = "proxmox_servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)
    hostname = Column(String(255), nullable=False, index=True)
    ip_address = Column(String(50), nullable=False, index=True)
    port = Column(Integer, nullable=False, default=8006)

    # API Token Authentication (recommended)
    api_user = Column(String(100), nullable=False, default="root@pam")
    api_token_name = Column(String(100), nullable=True)  # e.g., "my-token"
    api_token_value = Column(String(255), nullable=True)  # Secret token value

    # Optional: Password authentication (less secure)
    use_password = Column(Boolean, nullable=False, default=False)
    password = Column(String(255), nullable=True)  # Encrypted in production

    # SSL/TLS verification
    verify_ssl = Column(Boolean, nullable=False, default=False)

    # Metadata
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_check = Column(DateTime(timezone=True), nullable=True)
    
    # Status tracking
    is_online = Column(Boolean, nullable=True, default=None)
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_proxmox_status', 'is_online', 'last_check'),
    )

    def __repr__(self):
        return f"<ProxmoxServer(id={self.id}, name='{self.name}', ip='{self.ip_address}')>"

    @property
    def connection_info(self) -> dict:
        """Get safe connection info without sensitive data"""
        return {
            'id': self.id,
            'name': self.name,
            'hostname': self.hostname,
            'ip_address': self.ip_address,
            'port': self.port,
            'api_user': self.api_user,
            'verify_ssl': self.verify_ssl,
            'is_online': self.is_online,
            'last_check': self.last_check
        }

    def update_status(self, is_online: bool, error: str = None):
        """Update server status"""
        self.is_online = is_online
        self.last_check = utcnow()
        if error:
            self.last_error = error
        elif is_online:
            self.last_error = None


class VMInstance(Base):
    """VM/Container instance cache and configuration storage"""
    __tablename__ = "vm_instances"

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, nullable=False, index=True)  # FK to ProxmoxServer
    vmid = Column(Integer, nullable=False, index=True)  # Proxmox VMID
    node = Column(String(100), nullable=False)  # Proxmox node name
    vm_type = Column(String(20), nullable=False)  # 'qemu' or 'lxc'
    name = Column(String(100), nullable=False)
    
    # Owner (for VPS-style user isolation)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    owner = relationship("User", backref="instances", foreign_keys=[owner_id])
    
    # Status (cached from Proxmox)
    status = Column(String(20), default='unknown')  # running, stopped, paused, etc.
    is_template = Column(Boolean, default=False)
    
    # Hardware configuration (cached)
    cores = Column(Integer, nullable=True)
    memory = Column(BigInteger, nullable=True)  # in bytes
    disk_size = Column(BigInteger, nullable=True)  # in bytes
    
    # OS info
    os_type = Column(String(50), nullable=True)  # ostype field from Proxmox
    
    # Network configuration
    ip_address = Column(String(50), nullable=True)  # Primary IP
    ip_prefix = Column(Integer, nullable=True, default=24)  # CIDR prefix
    gateway = Column(String(50), nullable=True)
    nameserver = Column(String(50), nullable=True)
    
    # Cloud-init configuration
    cloud_init_user = Column(String(100), nullable=True)
    cloud_init_password = Column(String(255), nullable=True)  # Should be encrypted
    ssh_keys = Column(Text, nullable=True)  # SSH public keys
    
    # Template info
    template_id = Column(Integer, nullable=True)  # FK to OSTemplate (if deployed from template)
    template_name = Column(String(100), nullable=True)  # Cached template name for display
    
    # Metadata
    description = Column(Text, nullable=True)
    tags = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Soft delete
    last_sync_at = Column(DateTime(timezone=True), nullable=True)  # Last sync with Proxmox
    
    # Additional config as JSON
    extra_config = Column(JSON, nullable=True)

    __table_args__ = (
        Index('idx_vm_instance_server_vmid', 'server_id', 'vmid', unique=True),
        Index('idx_vm_instance_active', 'server_id', 'deleted_at'),
        Index('idx_vm_instance_owner', 'owner_id'),
    )

    def __repr__(self):
        return f"<VMInstance(id={self.id}, server_id={self.server_id}, vmid={self.vmid}, name='{self.name}')>"


# ==================== IPAM Models ====================

class IPAMNetwork(Base):
    """Сети/подсети для IPAM"""
    __tablename__ = "ipam_networks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # "Production", "Management"
    description = Column(Text, nullable=True)
    
    # Сетевые параметры
    network = Column(String(18), nullable=False, index=True)  # "10.10.10.0/24"
    gateway = Column(String(45), nullable=True)   # "10.10.10.1" (support IPv6)
    vlan_id = Column(Integer, nullable=True)      # 100
    
    # DNS настройки
    dns_primary = Column(String(45), nullable=True)
    dns_secondary = Column(String(45), nullable=True)
    dns_domain = Column(String(255), nullable=True)  # "example.local"
    
    # Привязка к Proxmox
    proxmox_server_id = Column(Integer, nullable=True, index=True)  # FK to ProxmoxServer
    proxmox_bridge = Column(String(20), nullable=True)  # "vmbr0"
    
    # Метаданные
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_ipam_network_name', 'name'),
        Index('idx_ipam_network_active', 'is_active'),
    )

    def __repr__(self):
        return f"<IPAMNetwork(id={self.id}, name='{self.name}', network='{self.network}')>"


class IPAMPool(Base):
    """Пулы IP-адресов внутри сети"""
    __tablename__ = "ipam_pools"

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(Integer, nullable=False, index=True)  # FK to IPAMNetwork
    
    name = Column(String(100), nullable=False)  # "DHCP Pool", "Static IPs", "Reserved"
    pool_type = Column(String(20), default="static", nullable=False)  # "static", "dhcp", "reserved"
    
    # Диапазон
    range_start = Column(String(45), nullable=False)  # "10.10.10.100"
    range_end = Column(String(45), nullable=False)    # "10.10.10.200"
    
    # Для автоматического выделения
    auto_assign = Column(Boolean, default=True, nullable=False)
    
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_ipam_pool_network', 'network_id'),
        Index('idx_ipam_pool_type', 'pool_type'),
    )

    def __repr__(self):
        return f"<IPAMPool(id={self.id}, name='{self.name}', range='{self.range_start}-{self.range_end}')>"


class IPAMAllocation(Base):
    """Выделенные IP-адреса"""
    __tablename__ = "ipam_allocations"

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(Integer, nullable=False, index=True)  # FK to IPAMNetwork
    pool_id = Column(Integer, nullable=True, index=True)      # FK to IPAMPool (optional)
    
    # IP адрес
    ip_address = Column(String(45), nullable=False, unique=True, index=True)
    mac_address = Column(String(17), nullable=True)  # "AA:BB:CC:DD:EE:FF"
    
    # Привязка к ресурсу
    resource_type = Column(String(20), nullable=True)  # "vm", "lxc", "physical", "service", "reserved"
    resource_id = Column(Integer, nullable=True)       # VMID или ID сервера
    resource_name = Column(String(100), nullable=True) # "web-server-01"
    
    # Привязка к Proxmox
    proxmox_server_id = Column(Integer, nullable=True, index=True)
    proxmox_vmid = Column(Integer, nullable=True)
    proxmox_node = Column(String(100), nullable=True)
    
    # Статус
    status = Column(String(20), default="allocated", nullable=False)  # "allocated", "reserved", "available", "conflict"
    allocation_type = Column(String(20), default="static", nullable=False)  # "static", "dhcp", "floating"
    
    # DNS
    hostname = Column(String(255), nullable=True, index=True)
    fqdn = Column(String(255), nullable=True)  # "web-server-01.example.local"
    dns_ptr_record = Column(Boolean, default=False, nullable=False)  # Create PTR record
    
    # Аудит
    allocated_by = Column(String(100), nullable=True)  # username
    allocated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)  # для временных выделений
    last_seen = Column(DateTime(timezone=True), nullable=True)   # последний ping/arp
    
    notes = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_ipam_alloc_network', 'network_id'),
        Index('idx_ipam_alloc_resource', 'resource_type', 'resource_id'),
        Index('idx_ipam_alloc_proxmox', 'proxmox_server_id', 'proxmox_vmid'),
        Index('idx_ipam_alloc_status', 'status'),
    )

    def __repr__(self):
        return f"<IPAMAllocation(id={self.id}, ip='{self.ip_address}', resource='{self.resource_name}')>"


class IPAMHistory(Base):
    """История изменений IPAM"""
    __tablename__ = "ipam_history"

    id = Column(Integer, primary_key=True, index=True)
    
    ip_address = Column(String(45), nullable=False, index=True)
    network_id = Column(Integer, nullable=True, index=True)
    
    action = Column(String(50), nullable=False)  # "allocated", "released", "modified", "conflict_detected", "scanned"
    
    # Детали изменения
    old_value = Column(JSON, nullable=True)  # {"resource_name": "old-vm", "status": "allocated"}
    new_value = Column(JSON, nullable=True)  # {"resource_name": "new-vm", "status": "allocated"}
    
    # Связь с ресурсом
    resource_type = Column(String(20), nullable=True)
    resource_id = Column(Integer, nullable=True)
    resource_name = Column(String(100), nullable=True)
    
    # Аудит
    performed_by = Column(String(100), nullable=True)  # username or "system"
    performed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_ipam_history_ip', 'ip_address'),
        Index('idx_ipam_history_action', 'action'),
        Index('idx_ipam_history_date', 'performed_at'),
    )

    def __repr__(self):
        return f"<IPAMHistory(id={self.id}, ip='{self.ip_address}', action='{self.action}')>"


# ==================== Audit Log Model ====================

class AuditLog(Base):
    """Логи аудита системы"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    
    # Уровень и категория
    level = Column(String(20), nullable=False, index=True)  # debug, info, warning, error, critical
    category = Column(String(50), nullable=False, index=True)  # auth, proxmox, ipam, system, api
    
    # Действие
    action = Column(String(100), nullable=False, index=True)  # login, vm_start, vm_stop, etc.
    message = Column(Text, nullable=False)
    
    # Контекст запроса
    request_id = Column(String(36), nullable=True, index=True)  # UUID для связи логов одного запроса
    session_id = Column(String(64), nullable=True, index=True)  # ID сессии пользователя
    
    # Пользователь
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(100), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True, index=True)
    user_agent = Column(String(500), nullable=True)
    geo_location = Column(String(100), nullable=True)  # Геолокация по IP (опционально)
    
    # Ресурс
    resource_type = Column(String(50), nullable=True, index=True)  # vm, container, server, network, user
    resource_id = Column(String(100), nullable=True)
    resource_name = Column(String(200), nullable=True)
    server_id = Column(Integer, nullable=True, index=True)  # ID Proxmox сервера
    server_name = Column(String(100), nullable=True)  # Имя Proxmox сервера
    node_name = Column(String(100), nullable=True)  # Имя ноды
    
    # Детали (JSON)
    details = Column(JSON, nullable=True)  # {"vmid": 100, "node": "pve1", "old_state": "stopped", "new_state": "running"}
    request_body = Column(JSON, nullable=True)  # Тело запроса (без паролей)
    response_body = Column(JSON, nullable=True)  # Сокращённое тело ответа
    
    # HTTP запрос (для API логов)
    request_method = Column(String(10), nullable=True)
    request_path = Column(String(500), nullable=True)
    query_params = Column(String(1000), nullable=True)  # Query параметры
    response_status = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)  # Время выполнения в мс
    
    # Результат
    success = Column(Boolean, default=True, nullable=False)
    error_message = Column(Text, nullable=True)
    error_traceback = Column(Text, nullable=True)  # Stack trace при ошибке
    
    # Время
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index('idx_audit_level_category', 'level', 'category'),
        Index('idx_audit_user_time', 'username', 'created_at'),
        Index('idx_audit_resource', 'resource_type', 'resource_id'),
        Index('idx_audit_created', 'created_at'),
        Index('idx_audit_request_id', 'request_id'),
        Index('idx_audit_ip', 'ip_address'),
        Index('idx_audit_server', 'server_id'),
    )

    def __repr__(self):
        return f"<AuditLog(id={self.id}, level='{self.level}', action='{self.action}')>"


# ==================== Panel Settings Model ====================

class PanelSettings(Base):
    """Настройки панели"""
    __tablename__ = "panel_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)  # Уникальный ключ настройки
    value = Column(String(500), nullable=False)  # Значение настройки
    description = Column(Text, nullable=True)  # Описание настройки
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<PanelSettings(id={self.id}, key='{self.key}', value='{self.value}')>"


# ==================== Task Queue Model ====================

class TaskQueue(Base):
    """Background task queue for bulk operations"""
    __tablename__ = "task_queue"

    id = Column(Integer, primary_key=True, index=True)
    
    # Task identification
    task_type = Column(String(50), nullable=False, index=True)  # bulk_start, bulk_stop, bulk_restart, bulk_delete
    status = Column(String(20), nullable=False, default='pending', index=True)  # pending, running, completed, failed, cancelled
    
    # Task details
    user_id = Column(Integer, nullable=False, index=True)  # Who initiated
    
    # Progress tracking
    total_items = Column(Integer, nullable=False, default=0)
    completed_items = Column(Integer, nullable=False, default=0)
    failed_items = Column(Integer, nullable=False, default=0)
    
    # Task data (JSON with list of VMs/containers to process)
    task_data = Column(JSON, nullable=False)  # [{server_id, vmid, vm_type, name}, ...]
    
    # Results (JSON with success/error for each item)
    results = Column(JSON, nullable=True)  # [{vmid, success, message}, ...]
    
    # Error message if task failed
    error_message = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index('idx_task_status_created', 'status', 'created_at'),
        Index('idx_task_user_status', 'user_id', 'status'),
    )

    def __repr__(self):
        return f"<TaskQueue(id={self.id}, type='{self.task_type}', status='{self.status}')>"
    
    @property
    def progress_percent(self) -> int:
        if self.total_items == 0:
            return 0
        return int((self.completed_items + self.failed_items) / self.total_items * 100)
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'task_type': self.task_type,
            'status': self.status,
            'total_items': self.total_items,
            'completed_items': self.completed_items,
            'failed_items': self.failed_items,
            'progress_percent': self.progress_percent,
            'results': self.results,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


# ==================== Snapshot Archive ====================

class VMSnapshotArchive(Base):
    """
    Archive of VM/Container snapshots before deletion.
    Stores snapshot configuration when instance is deleted for audit and potential recovery.
    """
    __tablename__ = "vm_snapshot_archives"

    id = Column(Integer, primary_key=True, index=True)
    
    # Reference to deleted instance
    server_id = Column(Integer, nullable=False, index=True)
    server_name = Column(String(100), nullable=True)
    vmid = Column(Integer, nullable=False, index=True)
    vm_name = Column(String(100), nullable=True)
    vm_type = Column(String(20), nullable=False)  # 'qemu' or 'lxc'
    node = Column(String(100), nullable=False)
    
    # Snapshot details
    snapname = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    snaptime = Column(BigInteger, nullable=True)  # Unix timestamp from Proxmox
    parent = Column(String(100), nullable=True)  # Parent snapshot name
    
    # For VM snapshots - whether RAM state was included
    vmstate = Column(Boolean, default=False, nullable=False)
    
    # Full snapshot config as JSON
    snapshot_config = Column(JSON, nullable=True)
    
    # Deletion metadata
    deleted_by = Column(String(100), nullable=True)  # Username who deleted the instance
    deletion_reason = Column(Text, nullable=True)  # Why it was deleted
    archived_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_snapshot_archive_server_vmid', 'server_id', 'vmid'),
        Index('idx_snapshot_archive_archived', 'archived_at'),
    )

    def __repr__(self):
        return f"<VMSnapshotArchive(id={self.id}, vmid={self.vmid}, snapname='{self.snapname}')>"



