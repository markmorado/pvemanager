from datetime import datetime
from typing import Optional
import ipaddress

from pydantic import BaseModel, Field, field_validator, model_validator, EmailStr


# ==================== User Schemas ====================

class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="Username")
    email: EmailStr = Field(..., description="Email address")
    full_name: Optional[str] = Field(None, max_length=100, description="Full name")


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, max_length=100, description="Password")


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, max_length=100)
    password: Optional[str] = Field(None, min_length=6, max_length=100)
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    role_id: Optional[int] = None
    require_password_change: Optional[bool] = None


class UserResponse(UserBase):
    id: int
    is_active: bool
    is_admin: bool
    created_at: datetime
    last_login: Optional[datetime] = None

    model_config = {
        "from_attributes": True
    }


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    username: Optional[str] = None


class LoginRequest(BaseModel):
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")


# ==================== Proxmox Server Schemas ====================

class ProxmoxServerBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Server name")
    hostname: str = Field(..., min_length=1, max_length=255, description="Server hostname/FQDN")
    ip_address: str = Field(..., min_length=7, max_length=50, description="IP address")
    port: int = Field(default=8006, ge=1, le=65535, description="Proxmox API port")
    api_user: str = Field(default="root@pam", max_length=100, description="API username (e.g., root@pam)")
    api_token_name: Optional[str] = Field(None, max_length=100, description="API token name")
    api_token_value: Optional[str] = Field(None, max_length=255, description="API token value")
    use_password: bool = Field(default=False, description="Use password instead of token")
    password: Optional[str] = Field(None, max_length=255, description="Password (if use_password=True)")
    verify_ssl: bool = Field(default=False, description="Verify SSL certificate")
    description: Optional[str] = Field(None, description="Server description")

    @field_validator('ip_address')
    @classmethod
    def validate_ip_address(cls, v):
        """Validate IP address format"""
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError('Invalid IP address format')

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        """Validate server name"""
        if not v or not v.strip():
            raise ValueError('Server name cannot be empty')
        return v.strip()

    @field_validator('hostname')
    @classmethod
    def validate_hostname(cls, v):
        """Validate hostname"""
        if not v or not v.strip():
            raise ValueError('Hostname cannot be empty')
        return v.strip()

    @model_validator(mode='after')
    def validate_auth_config(self):
        """Validate authentication configuration"""
        if self.use_password:
            if not self.password:
                raise ValueError('Password is required when use_password=True')
        else:
            if not self.api_token_name or not self.api_token_value:
                raise ValueError('API token name and value are required when using token auth')
        return self


class ProxmoxServerCreate(BaseModel):
    """Schema for creating a new Proxmox server"""
    name: str = Field(..., min_length=1, max_length=100, description="Server name")
    hostname: str = Field(..., min_length=1, max_length=255, description="Server hostname/FQDN")
    ip_address: str = Field(..., min_length=7, max_length=50, description="IP address")
    port: int = Field(default=8006, ge=1, le=65535, description="Proxmox API port")
    api_user: str = Field(default="root@pam", max_length=100, description="API username (e.g., root@pam)")
    api_token_name: Optional[str] = Field(None, max_length=100, description="API token name")
    api_token_value: Optional[str] = Field(None, max_length=255, description="API token value")
    use_password: bool = Field(default=False, description="Use password instead of token")
    password: Optional[str] = Field(None, max_length=255, description="Password (if use_password=True)")
    verify_ssl: bool = Field(default=False, description="Verify SSL certificate")
    description: Optional[str] = Field(None, description="Server description")

    @field_validator('ip_address')
    @classmethod
    def validate_ip_address(cls, v):
        """Validate IP address format"""
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError('Invalid IP address format')

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        """Validate server name"""
        if not v or not v.strip():
            raise ValueError('Server name cannot be empty')
        return v.strip()

    @field_validator('hostname')
    @classmethod
    def validate_hostname(cls, v):
        """Validate hostname"""
        if not v or not v.strip():
            raise ValueError('Hostname cannot be empty')
        return v.strip()

    @model_validator(mode='after')
    def validate_auth_config(self):
        """Validate authentication configuration"""
        if self.use_password:
            if not self.password:
                raise ValueError('Password is required when use_password=True')
        else:
            if not self.api_token_name or not self.api_token_value:
                raise ValueError('API token name and value are required when using token auth')
        return self


class ProxmoxServerUpdate(BaseModel):
    """Schema for updating a Proxmox server"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    hostname: Optional[str] = Field(None, min_length=1, max_length=255)
    ip_address: Optional[str] = Field(None, min_length=7, max_length=50)
    port: Optional[int] = Field(None, ge=1, le=65535)
    api_user: Optional[str] = Field(None, max_length=100)
    api_token_name: Optional[str] = Field(None, max_length=100)
    api_token_value: Optional[str] = Field(None, max_length=255)
    use_password: Optional[bool] = None
    password: Optional[str] = Field(None, max_length=255)
    verify_ssl: Optional[bool] = None
    description: Optional[str] = None


class ProxmoxServerResponse(BaseModel):
    """Schema for Proxmox server response"""
    id: int
    name: str
    hostname: str
    ip_address: str
    port: int
    api_user: str
    verify_ssl: bool
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_check: Optional[datetime] = None
    is_online: Optional[bool] = None
    last_error: Optional[str] = None

    # Exclude sensitive fields
    api_token_value: Optional[str] = Field(None, exclude=True)
    password: Optional[str] = Field(None, exclude=True)

    model_config = {
        "from_attributes": True
    }


class ProxmoxVMResponse(BaseModel):
    """Schema for Proxmox VM/Container response"""
    vmid: int
    name: Optional[str] = None
    status: str
    type: str  # 'qemu' or 'lxc'
    node: str
    cpu: Optional[float] = None
    mem: Optional[int] = None
    maxmem: Optional[int] = None
    disk: Optional[int] = None
    maxdisk: Optional[int] = None
    uptime: Optional[int] = None


# ==================== OS Template Schemas ====================

class OSTemplateGroupBase(BaseModel):
    """Base schema for OS Template Group"""
    name: str = Field(..., min_length=1, max_length=100, description="Group name (e.g., Ubuntu, Debian)")
    icon: Optional[str] = Field(None, max_length=200, description="Icon (emoji, class or HTML)")
    description: Optional[str] = Field(None, description="Group description")
    sort_order: int = Field(default=0, description="Sort order")
    is_active: bool = Field(default=True, description="Is group active")


class OSTemplateGroupCreate(OSTemplateGroupBase):
    """Schema for creating OS Template Group"""
    pass


class OSTemplateGroupUpdate(BaseModel):
    """Schema for updating OS Template Group"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    icon: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class OSTemplateGroupResponse(OSTemplateGroupBase):
    """Schema for OS Template Group response"""
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True
    }


class OSTemplateBase(BaseModel):
    """Base schema for OS Template"""
    group_id: int = Field(..., description="Template group ID")
    server_id: int = Field(..., description="Proxmox server ID")
    name: str = Field(..., min_length=1, max_length=100, description="Display name")
    vmid: int = Field(..., ge=100, description="Proxmox template VMID")
    node: str = Field(..., min_length=1, max_length=100, description="Proxmox node name")
    default_cores: int = Field(default=1, ge=1, le=128, description="Default CPU cores")
    default_memory: int = Field(default=1024, ge=128, description="Default memory in MB")
    default_disk: int = Field(default=10, ge=1, description="Default disk size in GB")
    min_cores: int = Field(default=1, ge=1, description="Minimum CPU cores")
    min_memory: int = Field(default=512, ge=128, description="Minimum memory in MB")
    min_disk: int = Field(default=5, ge=1, description="Minimum disk size in GB")
    description: Optional[str] = Field(None, description="Template description")
    is_active: bool = Field(default=True, description="Is template active")
    sort_order: int = Field(default=0, description="Sort order")


class OSTemplateCreate(OSTemplateBase):
    """Schema for creating OS Template"""
    pass


class OSTemplateUpdate(BaseModel):
    """Schema for updating OS Template"""
    group_id: Optional[int] = None
    server_id: Optional[int] = None
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    vmid: Optional[int] = Field(None, ge=100)
    node: Optional[str] = Field(None, min_length=1, max_length=100)
    default_cores: Optional[int] = Field(None, ge=1, le=128)
    default_memory: Optional[int] = Field(None, ge=128)
    default_disk: Optional[int] = Field(None, ge=1)
    min_cores: Optional[int] = Field(None, ge=1)
    min_memory: Optional[int] = Field(None, ge=128)
    min_disk: Optional[int] = Field(None, ge=1)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class OSTemplateResponse(OSTemplateBase):
    """Schema for OS Template response"""
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True
    }


class OSTemplateWithGroup(OSTemplateResponse):
    """Schema for OS Template with group info"""
    group_name: Optional[str] = None
    group_icon: Optional[str] = None
    server_name: Optional[str] = None


class VMDeployRequest(BaseModel):
    """Schema for deploying a new VM from template"""
    template_id: int = Field(..., description="OS Template ID")
    name: str = Field(..., min_length=1, max_length=100, description="New VM name")
    vmid: Optional[int] = Field(None, ge=100, le=999999999, description="Specific VMID to use (optional, for reinstall)")
    target_node: Optional[str] = Field(None, description="Target node for VM deployment (optional, allows cross-node deployment)")
    cores: Optional[int] = Field(None, ge=1, le=128, description="CPU cores")
    memory: Optional[int] = Field(None, ge=128, description="Memory in MB")
    disk: Optional[int] = Field(None, ge=1, description="Disk size in GB")
    target_storage: Optional[str] = Field(None, description="Target storage for VM disk (e.g., local-lvm)")
    start_after_create: bool = Field(default=True, description="Start VM after creation")
    onboot: bool = Field(default=False, description="Start VM on host boot")
    # High Availability
    enable_ha: bool = Field(default=False, description="Enable High Availability for VM (cluster only)")
    # Network configuration
    network_bridge: Optional[str] = Field(default="vmbr0", description="Network bridge")
    ip_address: Optional[str] = Field(None, description="Static IP address")
    gateway: Optional[str] = Field(None, description="Gateway IP")
    # IPAM integration
    ipam_network_id: Optional[int] = Field(None, description="IPAM network ID for auto IP allocation")
    ipam_pool_id: Optional[int] = Field(None, description="IPAM pool ID (optional)")
    # Cloud-init (if template supports)
    cloud_init_user: Optional[str] = Field(None, max_length=50, description="Cloud-init username")
    cloud_init_password: Optional[str] = Field(None, max_length=100, description="Cloud-init password")
    ssh_keys: Optional[str] = Field(None, description="SSH public keys")


class VMDeployResponse(BaseModel):
    """Schema for VM deploy response"""
    success: bool
    vmid: int
    name: str
    node: str
    server_id: int
    task_upid: Optional[str] = None
    message: str


# ==================== IPAM Schemas ====================

class IPAMNetworkBase(BaseModel):
    """Base schema for IPAM Network"""
    name: str = Field(..., min_length=1, max_length=100, description="Network name")
    description: Optional[str] = Field(None, description="Network description")
    network: str = Field(..., description="Network CIDR (e.g., 10.10.10.0/24)")
    gateway: Optional[str] = Field(None, description="Gateway IP")
    vlan_id: Optional[int] = Field(None, ge=1, le=4094, description="VLAN ID")
    dns_primary: Optional[str] = Field(None, description="Primary DNS server")
    dns_secondary: Optional[str] = Field(None, description="Secondary DNS server")
    dns_domain: Optional[str] = Field(None, max_length=255, description="DNS domain")
    proxmox_server_id: Optional[int] = Field(None, description="Associated Proxmox server ID")
    proxmox_bridge: Optional[str] = Field(None, max_length=20, description="Proxmox bridge (e.g., vmbr0)")
    is_active: bool = Field(default=True, description="Is network active")

    @field_validator('network')
    @classmethod
    def validate_network_cidr(cls, v):
        """Validate network CIDR format"""
        try:
            ipaddress.ip_network(v, strict=False)
            return v
        except ValueError:
            raise ValueError('Invalid network CIDR format (e.g., 10.10.10.0/24)')

    @field_validator('gateway', 'dns_primary', 'dns_secondary')
    @classmethod
    def validate_ip(cls, v):
        """Validate IP address format"""
        if v is None:
            return v
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError('Invalid IP address format')


class IPAMNetworkCreate(IPAMNetworkBase):
    """Schema for creating IPAM Network"""
    pass


class IPAMNetworkUpdate(BaseModel):
    """Schema for updating IPAM Network"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    gateway: Optional[str] = None
    vlan_id: Optional[int] = Field(None, ge=1, le=4094)
    dns_primary: Optional[str] = None
    dns_secondary: Optional[str] = None
    dns_domain: Optional[str] = Field(None, max_length=255)
    proxmox_server_id: Optional[int] = None
    proxmox_bridge: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None


class IPAMNetworkResponse(IPAMNetworkBase):
    """Schema for IPAM Network response"""
    id: int
    created_at: datetime
    updated_at: datetime
    # Computed fields (will be added in API)
    total_ips: Optional[int] = None
    used_ips: Optional[int] = None
    available_ips: Optional[int] = None
    utilization_percent: Optional[float] = None
    server_name: Optional[str] = None

    model_config = {
        "from_attributes": True
    }


class IPAMPoolBase(BaseModel):
    """Base schema for IPAM Pool"""
    network_id: int = Field(..., description="Parent network ID")
    name: str = Field(..., min_length=1, max_length=100, description="Pool name")
    pool_type: str = Field(default="static", description="Pool type: static, dhcp, reserved")
    range_start: str = Field(..., description="Range start IP")
    range_end: str = Field(..., description="Range end IP")
    auto_assign: bool = Field(default=True, description="Allow auto-assignment from this pool")
    description: Optional[str] = Field(None, description="Pool description")
    is_active: bool = Field(default=True, description="Is pool active")

    @field_validator('pool_type')
    @classmethod
    def validate_pool_type(cls, v):
        """Validate pool type"""
        allowed = ['static', 'dhcp', 'reserved']
        if v not in allowed:
            raise ValueError(f'Pool type must be one of: {", ".join(allowed)}')
        return v

    @field_validator('range_start', 'range_end')
    @classmethod
    def validate_range_ip(cls, v):
        """Validate IP address format"""
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError('Invalid IP address format')


class IPAMPoolCreate(IPAMPoolBase):
    """Schema for creating IPAM Pool"""
    pass


class IPAMPoolUpdate(BaseModel):
    """Schema for updating IPAM Pool"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    pool_type: Optional[str] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    auto_assign: Optional[bool] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class IPAMPoolResponse(IPAMPoolBase):
    """Schema for IPAM Pool response"""
    id: int
    created_at: datetime
    updated_at: datetime
    # Computed fields
    total_ips: Optional[int] = None
    used_ips: Optional[int] = None
    available_ips: Optional[int] = None

    model_config = {
        "from_attributes": True
    }


class IPAMAllocationBase(BaseModel):
    """Base schema for IPAM Allocation"""
    network_id: int = Field(..., description="Network ID")
    pool_id: Optional[int] = Field(None, description="Pool ID (optional)")
    ip_address: str = Field(..., description="IP address")
    mac_address: Optional[str] = Field(None, max_length=17, description="MAC address")
    resource_type: Optional[str] = Field(None, description="Resource type: vm, lxc, physical, service, reserved")
    resource_id: Optional[int] = Field(None, description="Resource ID (e.g., VMID)")
    resource_name: Optional[str] = Field(None, max_length=100, description="Resource name")
    proxmox_server_id: Optional[int] = Field(None, description="Proxmox server ID")
    proxmox_vmid: Optional[int] = Field(None, description="Proxmox VMID")
    proxmox_node: Optional[str] = Field(None, max_length=100, description="Proxmox node")
    status: str = Field(default="allocated", description="Status: allocated, reserved, available, conflict")
    allocation_type: str = Field(default="static", description="Type: static, dhcp, floating")
    hostname: Optional[str] = Field(None, max_length=255, description="Hostname")
    fqdn: Optional[str] = Field(None, max_length=255, description="Fully qualified domain name")
    dns_ptr_record: bool = Field(default=False, description="Create PTR record")
    expires_at: Optional[datetime] = Field(None, description="Expiration date (for temp allocations)")
    notes: Optional[str] = Field(None, description="Notes")

    @field_validator('ip_address')
    @classmethod
    def validate_ip_address(cls, v):
        """Validate IP address format"""
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            raise ValueError('Invalid IP address format')

    @field_validator('mac_address')
    @classmethod
    def validate_mac_address(cls, v):
        """Validate MAC address format"""
        if v is None:
            return v
        import re
        mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
        if not mac_pattern.match(v):
            raise ValueError('Invalid MAC address format (e.g., AA:BB:CC:DD:EE:FF)')
        return v.upper()

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        """Validate status"""
        allowed = ['allocated', 'reserved', 'available', 'conflict']
        if v not in allowed:
            raise ValueError(f'Status must be one of: {", ".join(allowed)}')
        return v


class IPAMAllocationCreate(IPAMAllocationBase):
    """Schema for creating IPAM Allocation"""
    allocated_by: Optional[str] = Field(None, max_length=100, description="Allocated by username")


class IPAMAllocationUpdate(BaseModel):
    """Schema for updating IPAM Allocation"""
    pool_id: Optional[int] = None
    mac_address: Optional[str] = Field(None, max_length=17)
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    resource_name: Optional[str] = Field(None, max_length=100)
    proxmox_server_id: Optional[int] = None
    proxmox_vmid: Optional[int] = None
    proxmox_node: Optional[str] = Field(None, max_length=100)
    status: Optional[str] = None
    allocation_type: Optional[str] = None
    hostname: Optional[str] = Field(None, max_length=255)
    fqdn: Optional[str] = Field(None, max_length=255)
    dns_ptr_record: Optional[bool] = None
    expires_at: Optional[datetime] = None
    notes: Optional[str] = None


class IPAMAllocationResponse(IPAMAllocationBase):
    """Schema for IPAM Allocation response"""
    id: int
    allocated_by: Optional[str] = None
    allocated_at: datetime
    last_seen: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # Extended info
    network_name: Optional[str] = None
    pool_name: Optional[str] = None

    model_config = {
        "from_attributes": True
    }


class IPAMHistoryResponse(BaseModel):
    """Schema for IPAM History response"""
    id: int
    ip_address: str
    network_id: Optional[int] = None
    action: str
    old_value: Optional[dict] = None
    new_value: Optional[dict] = None
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    resource_name: Optional[str] = None
    performed_by: Optional[str] = None
    performed_at: datetime
    notes: Optional[str] = None

    model_config = {
        "from_attributes": True
    }


class IPAMAutoAllocateRequest(BaseModel):
    """Schema for auto-allocating an IP from a network/pool"""
    network_id: int = Field(..., description="Network ID to allocate from")
    pool_id: Optional[int] = Field(None, description="Specific pool ID (optional)")
    resource_type: Optional[str] = Field(None, description="Resource type")
    resource_id: Optional[int] = Field(None, description="Resource ID")
    resource_name: Optional[str] = Field(None, max_length=100, description="Resource name")
    hostname: Optional[str] = Field(None, max_length=255, description="Hostname")
    notes: Optional[str] = Field(None, description="Notes")


class IPAMScanRequest(BaseModel):
    """Schema for network scan request"""
    network_id: int = Field(..., description="Network ID to scan")
    scan_type: str = Field(default="ping", description="Scan type: ping, arp, full")
    update_last_seen: bool = Field(default=True, description="Update last_seen for found IPs")
    detect_new: bool = Field(default=True, description="Detect and add new allocations")


class IPAMSyncRequest(BaseModel):
    """Schema for Proxmox sync request"""
    proxmox_server_id: int = Field(..., description="Proxmox server ID to sync from")
    network_id: Optional[int] = Field(None, description="Target network ID (optional)")
    create_allocations: bool = Field(default=True, description="Create allocations for found VMs")
    update_existing: bool = Field(default=True, description="Update existing allocations")


class IPAMNetworkStats(BaseModel):
    """Schema for network statistics"""
    network_id: int
    network_name: str
    network_cidr: str
    total_ips: int
    allocated_ips: int
    reserved_ips: int
    available_ips: int
    utilization_percent: float
    pools_count: int
    vms_count: int
    lxc_count: int
    physical_count: int
    other_count: int


"""
Pydantic schemas for notifications
"""

from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field


class NotificationCreate(BaseModel):
    """Schema for creating a notification"""
    user_id: int
    type: str = Field(..., description="Type: vm_status, resource_alert, system, ipam, template")
    level: str = Field(..., description="Level: critical, warning, info, success")
    title: str = Field(..., max_length=255)
    message: Optional[str] = None
    data: Optional[dict] = None
    link: Optional[str] = None
    source: Optional[str] = Field(None, description="Source: proxmox, ipam, system, docker")
    source_id: Optional[str] = None
    expires_at: Optional[datetime] = None


class NotificationUpdate(BaseModel):
    """Schema for updating a notification"""
    read: Optional[bool] = None


class NotificationResponse(BaseModel):
    """Schema for notification response"""
    id: int
    user_id: int
    type: str
    level: str
    title: str
    message: Optional[str]
    data: Optional[dict]
    link: Optional[str]
    source: Optional[str]
    source_id: Optional[str]
    read: bool
    read_at: Optional[datetime]
    created_at: datetime
    expires_at: Optional[datetime]

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    """Schema for notification list response"""
    total: int
    unread_count: int
    notifications: List[NotificationResponse]


class NotificationPreferenceUpdate(BaseModel):
    """Schema for updating notification preferences"""
    enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None
    email_critical_only: Optional[bool] = None
    telegram_enabled: Optional[bool] = None
    telegram_chat_id: Optional[str] = None
    webhook_url: Optional[str] = None
    notification_levels: Optional[List[str]] = None
    notification_types: Optional[List[str]] = None
    quiet_hours_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    quiet_hours_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class NotificationPreferenceResponse(BaseModel):
    """Schema for notification preference response"""
    id: int
    user_id: int
    enabled: bool
    email_enabled: bool
    email_critical_only: bool
    telegram_enabled: bool
    telegram_chat_id: Optional[str]
    webhook_url: Optional[str]
    notification_levels: List[str]
    notification_types: List[str]
    quiet_hours_start: Optional[str]
    quiet_hours_end: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Notification schemas added above
