from datetime import datetime
from typing import Optional, List
import ipaddress
import re

from pydantic import BaseModel, Field, field_validator, model_validator, EmailStr


_HOSTNAME_LABEL_RE = re.compile(r'^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$')

_DNS_NAME_RE = re.compile(r'^[a-zA-Z](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$')


def validate_instance_name(name: str) -> str:
    """Validate that an instance name is a valid DNS label (RFC 952 / RFC 1123).

    Proxmox requires VM/CT names to be valid DNS names: start with a letter,
    contain only letters, digits and hyphens, end with a letter or digit,
    max 63 characters.  Underscores, spaces and other special characters are
    rejected.
    """
    name = name.strip()
    if not name:
        raise ValueError("Имя не может быть пустым")
    if len(name) > 63:
        raise ValueError("Имя не может быть длиннее 63 символов")
    if not _DNS_NAME_RE.match(name):
        raise ValueError(
            "Имя должно быть валидным DNS-именем: начинаться с буквы, "
            "содержать только латинские буквы, цифры и дефис, "
            "заканчиваться буквой или цифрой"
        )
    return name


def validate_host(v: str) -> str:
    """Validate that value is a valid IP address or hostname/FQDN."""
    if v is None:
        raise ValueError('Host is required')
    v = v.strip()
    if not v:
        raise ValueError('Host cannot be empty')
    # Accept valid IP address (v4 or v6)
    try:
        ipaddress.ip_address(v)
        return v
    except ValueError:
        pass
    # Otherwise validate as hostname / FQDN
    host = v.rstrip('.')
    if len(host) > 253 or not host:
        raise ValueError('Invalid host: must be a valid IP address or hostname')
    if all(_HOSTNAME_LABEL_RE.match(label) for label in host.split('.')):
        return v
    raise ValueError('Invalid host: must be a valid IP address or hostname')


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
    two_factor_enabled: bool = False
    created_at: datetime
    last_login: Optional[datetime] = None

    model_config = {
        "from_attributes": True
    }


class Token(BaseModel):
    # access_token is absent when the login step succeeded on password but a
    # second factor is still required (two_factor_required=True).
    access_token: Optional[str] = None
    token_type: str = "bearer"
    two_factor_required: Optional[bool] = None


class TokenData(BaseModel):
    username: Optional[str] = None


class LoginRequest(BaseModel):
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")
    code: Optional[str] = Field(None, description="TOTP or backup code (when 2FA is enabled)")


# ==================== Two-Factor (2FA) Schemas ====================

class TwoFactorStatusResponse(BaseModel):
    enabled: bool
    backup_codes_remaining: int = 0


class TwoFactorSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_svg: str


class TwoFactorEnableRequest(BaseModel):
    code: str = Field(..., description="6-digit TOTP code from the authenticator app")


class TwoFactorEnableResponse(BaseModel):
    enabled: bool = True
    backup_codes: List[str]


class TwoFactorDisableRequest(BaseModel):
    password: str = Field(..., description="Current account password")
    code: Optional[str] = Field(None, description="Current TOTP or backup code")


class MessageResponse(BaseModel):
    message: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(..., description="Account email to send the reset link to")


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=8, max_length=128, description="One-time reset token from the emailed link")
    new_password: str = Field(..., description="New password")


# ==================== Proxmox Server Schemas ====================

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
    auto_create_token: bool = Field(
        default=False,
        description="Authenticate with the password once, then let the panel create its own API token",
    )
    verify_ssl: bool = Field(default=False, description="Verify SSL certificate")
    description: Optional[str] = Field(None, description="Server description")

    @field_validator('ip_address')
    @classmethod
    def validate_ip_address(cls, v):
        """Validate IP address or hostname/FQDN"""
        return validate_host(v)

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
        if self.auto_create_token:
            # The token does not exist yet — it is created during registration
            # using this password, so only the password is required here.
            if not self.password:
                raise ValueError('Password is required when auto_create_token=True')
        elif self.use_password:
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


class ServerWorkspaceBrief(BaseModel):
    """Compact workspace reference shown on server cards"""
    id: int
    name: str
    color: Optional[str] = None

    model_config = {"from_attributes": True}


class ProxmoxServerResponse(BaseModel):
    """Schema for Proxmox server response"""
    id: int
    name: str
    hostname: str
    ip_address: str
    port: int
    api_user: str
    verify_ssl: bool
    use_password: bool = False
    cluster_name: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_check: Optional[datetime] = None
    is_online: Optional[bool] = None
    last_error: Optional[str] = None
    # 'auth' — сервер отвечает, но отверг учётные данные; 'connection' — не отвечает
    last_error_kind: Optional[str] = None
    workspaces: List[ServerWorkspaceBrief] = []

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

# Group icons are rendered as markup in the UI: allow plain text/emoji or a
# single empty <i>/<span> carrying CSS classes — nothing that can run script.
_GROUP_ICON_RE = re.compile(r'^(?:[^<>&"]{0,50}|<(i|span) class="[\w\s-]{1,150}"></\1>)$')


def _check_group_icon(v: Optional[str]) -> Optional[str]:
    if v is not None and not _GROUP_ICON_RE.match(v):
        raise ValueError('icon must be text/emoji or <i class="..."></i>')
    return v


class OSTemplateGroupBase(BaseModel):
    """Base schema for OS Template Group"""
    name: str = Field(..., min_length=1, max_length=100, description="Group name (e.g., Ubuntu, Debian)")
    icon: Optional[str] = Field(None, max_length=200, description="Icon (emoji, class or HTML)")
    description: Optional[str] = Field(None, description="Group description")
    sort_order: int = Field(default=0, description="Sort order")
    is_active: bool = Field(default=True, description="Is group active")


class OSTemplateGroupCreate(OSTemplateGroupBase):
    """Schema for creating OS Template Group"""
    # Validated on input only, so legacy rows still serialize in responses.
    _icon = field_validator("icon")(_check_group_icon)


class OSTemplateGroupUpdate(BaseModel):
    """Schema for updating OS Template Group"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    icon: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

    _icon = field_validator("icon")(_check_group_icon)


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
    vmid: Optional[int] = Field(None, ge=100, description="Proxmox template VMID (None for vztmpl file templates)")
    vm_type: str = Field(default="qemu", description="Template type: qemu (KVM) or lxc")
    volid: Optional[str] = Field(None, max_length=500, description="vztmpl volume id (for LXC file templates)")
    node: Optional[str] = Field(None, max_length=100, description="Proxmox node name")
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
    vm_type: Optional[str] = None
    volid: Optional[str] = Field(None, max_length=500)
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
    ssh_key_ids: Optional[List[int]] = Field(default=None, description="IDs of saved SSH keys to inject")
    owner_id: Optional[int] = Field(default=None, description="Admin-only: assign instance to this user (also enables using their saved SSH keys)")


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
    proxmox_node: Optional[str] = Field(None, max_length=100, description="Proxmox node name (e.g., pve1)")
    proxmox_bridge: Optional[str] = Field(None, max_length=20, description="Proxmox bridge (e.g., vmbr0)")
    workspace_id: Optional[int] = Field(None, description="Workspace this network belongs to")
    is_default: bool = Field(default=False, description="Default network within its workspace")
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
    proxmox_node: Optional[str] = Field(None, max_length=100)
    proxmox_bridge: Optional[str] = Field(None, max_length=20)
    workspace_id: Optional[int] = None
    is_default: Optional[bool] = None
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
    workspace_name: Optional[str] = None

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
    # Multi-IP: гость может держать несколько адресов
    is_primary: bool = False
    assignment_kind: str = "primary"
    target_interface: Optional[str] = None
    apply_status: Optional[str] = None
    apply_error: Optional[str] = None
    applied_at: Optional[datetime] = None
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


# ==================== Workspace Schemas ====================

class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Workspace name")
    description: Optional[str] = Field(None, max_length=500)
    color: Optional[str] = Field("#667eea", max_length=20, description="Accent color (CSS hex)")
    server_ids: Optional[List[int]] = Field(default_factory=list, description="Initial server IDs")
    user_ids: Optional[List[int]] = Field(default_factory=list, description="Initial user IDs with access")


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    color: Optional[str] = Field(None, max_length=20)


class WorkspaceServerItem(BaseModel):
    id: int
    name: str
    ip_address: str
    is_online: bool
    cluster_name: Optional[str] = None

    model_config = {"from_attributes": True}


class WorkspaceUserItem(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str] = None

    model_config = {"from_attributes": True}


class WorkspaceResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    color: Optional[str] = None
    is_default: bool
    server_count: int = 0
    user_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceDetail(WorkspaceResponse):
    servers: List[WorkspaceServerItem] = []
    users: List[WorkspaceUserItem] = []


# ==================== Network topology ====================
# Response shape for GET /proxmox/api/network/topology. Deliberately a domain
# hierarchy (panel → cluster → node → guest) rather than ready-made graph
# nodes/edges: the frontend derives edges from nesting, and the grouping
# threshold changes the rendered node count client-side without a round-trip.
# Fields the graph does not draw yet (bridges, nic.bridge/vlan_tag, tags) are
# the groundwork for the planned "group by VLAN / by tag" views.


class TopologyNic(BaseModel):
    """One guest network card, parsed out of its ``netN`` config string."""

    key: str
    model: Optional[str] = None
    mac: Optional[str] = None
    bridge: Optional[str] = None
    vlan_tag: Optional[int] = None
    trunks: List[int] = []
    firewall: bool = False
    link_down: bool = False
    name: Optional[str] = None
    ip: Optional[str] = None
    gw: Optional[str] = None
    rate: Optional[float] = None
    mtu: Optional[int] = None


class TopologyBridge(BaseModel):
    """A bridge / bond / SDN vnet a guest can be attached to."""

    name: str
    type: Optional[str] = None
    cidr: Optional[str] = None
    vlan_aware: bool = False
    ports: Optional[str] = None
    active: bool = False
    zone: Optional[str] = None
    ipam_network_id: Optional[int] = None
    ipam_cidr: Optional[str] = None
    ipam_name: Optional[str] = None


class TopologyGuest(BaseModel):
    id: str
    server_id: int
    node: str
    vmid: int
    type: str                      # "qemu" | "lxc"
    name: str
    status: str                    # running | stopped | paused | unknown
    is_template: bool = False
    lock: Optional[str] = None
    cpu: Optional[float] = None
    cores: Optional[int] = None
    mem: Optional[int] = None
    maxmem: Optional[int] = None
    disk: Optional[int] = None
    maxdisk: Optional[int] = None
    uptime: Optional[int] = None
    tags: List[str] = []
    owner_id: Optional[int] = None
    owner_username: Optional[str] = None
    ip: Optional[str] = None
    nics: List[TopologyNic] = []


class TopologyNode(BaseModel):
    id: str
    server_id: int
    server_name: str
    node: str
    status: str                    # online | offline | unknown
    stale: bool = False            # True → rebuilt from cache, server unreachable
    cpu: Optional[float] = None
    maxcpu: Optional[int] = None
    mem: Optional[int] = None
    maxmem: Optional[int] = None
    uptime: Optional[int] = None
    bridges: List[TopologyBridge] = []
    guests: List[TopologyGuest] = []


class TopologyCluster(BaseModel):
    id: str
    name: str
    kind: str                      # "cluster" | "standalone"
    server_ids: List[int] = []
    online: bool = False
    nodes: List[TopologyNode] = []


class TopologyWarning(BaseModel):
    server_id: Optional[int] = None
    server_name: Optional[str] = None
    code: str                      # offline | no_credentials | error | nics_truncated
    message: str


class TopologyPanelSummary(BaseModel):
    name: str = "PVEmanager"
    cluster_count: int = 0
    node_count: int = 0
    guest_count: int = 0


class NetworkTopologyResponse(BaseModel):
    generated_at: datetime
    panel: TopologyPanelSummary
    clusters: List[TopologyCluster] = []
    warnings: List[TopologyWarning] = []
