"""
Permission Registry - Central definition of all permissions
Format: resource:action[:scope]

Resources: cluster, node, vm, lxc, storage, network, backup, template, user, role, log, setting, ipam
Actions: view, list, create, update, delete, start, stop, restart, console, migrate, manage
Scopes: global, organization, workspace, instance (optional, defaults to global)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from enum import Enum


class ResourceType(str, Enum):
    """Resource types in the system"""
    CLUSTER = "cluster"
    NODE = "node"
    SERVER = "server"  # Proxmox server
    VM = "vm"
    LXC = "lxc"
    STORAGE = "storage"
    NETWORK = "network"
    BACKUP = "backup"
    TEMPLATE = "template"
    USER = "user"
    ROLE = "role"
    LOG = "log"
    SETTING = "setting"
    IPAM = "ipam"
    NOTIFICATION = "notification"
    DASHBOARD = "dashboard"


class ActionType(str, Enum):
    """Action types"""
    VIEW = "view"
    LIST = "list"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    CONSOLE = "console"
    MIGRATE = "migrate"
    MANAGE = "manage"
    EXPORT = "export"
    EXECUTE = "execute"  # For running commands


class ScopeType(str, Enum):
    """Permission scope levels"""
    GLOBAL = "global"          # Platform-wide access
    ORGANIZATION = "org"       # Within organization
    WORKSPACE = "workspace"    # Within workspace
    INSTANCE = "instance"      # Specific resource only


@dataclass
class Permission:
    """
    Atomic permission definition.
    
    Format: resource:action or resource:action:scope
    Examples:
        - vm:view (global scope)
        - vm:create:workspace (workspace scope)
        - vm:delete:instance (instance scope)
    """
    resource: str
    action: str
    scope: ScopeType = ScopeType.GLOBAL
    display_name: str = ""
    description: str = ""
    category: str = ""
    requires: List[str] = field(default_factory=list)  # Dependencies
    
    @property
    def code(self) -> str:
        """Get permission code"""
        if self.scope == ScopeType.GLOBAL:
            return f"{self.resource}:{self.action}"
        return f"{self.resource}:{self.action}:{self.scope.value}"
    
    @property
    def legacy_code(self) -> str:
        """Get legacy permission code for backwards compatibility"""
        # Map to old format: resource.action
        return f"{self.resource}.{self.action}"
    
    def __hash__(self):
        return hash(self.code)
    
    def __eq__(self, other):
        if isinstance(other, Permission):
            return self.code == other.code
        return False


class PermissionRegistry:
    """
    Central registry of all permissions.
    Provides validation, lookup, and category grouping.
    """
    
    _instance = None
    _permissions: Dict[str, Permission] = {}
    _by_category: Dict[str, List[Permission]] = {}
    _by_resource: Dict[str, List[Permission]] = {}
    _legacy_map: Dict[str, str] = {}  # legacy code -> new code
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._permissions = {}
        self._by_category = {}
        self._by_resource = {}
        self._legacy_map = {}
    
    def register(self, permission: Permission) -> None:
        """Register a permission"""
        self._permissions[permission.code] = permission
        
        # Index by category
        if permission.category not in self._by_category:
            self._by_category[permission.category] = []
        self._by_category[permission.category].append(permission)
        
        # Index by resource
        if permission.resource not in self._by_resource:
            self._by_resource[permission.resource] = []
        self._by_resource[permission.resource].append(permission)
        
        # Legacy mapping
        self._legacy_map[permission.legacy_code] = permission.code
    
    def get(self, code: str) -> Optional[Permission]:
        """Get permission by code"""
        # Try direct lookup
        if code in self._permissions:
            return self._permissions[code]
        # Try legacy format
        if code in self._legacy_map:
            return self._permissions[self._legacy_map[code]]
        return None
    
    def exists(self, code: str) -> bool:
        """Check if permission exists"""
        return code in self._permissions or code in self._legacy_map
    
    def resolve(self, code: str) -> Optional[str]:
        """Resolve legacy code to new code"""
        if code in self._permissions:
            return code
        return self._legacy_map.get(code)
    
    def all(self) -> List[Permission]:
        """Get all permissions"""
        return list(self._permissions.values())
    
    def by_category(self, category: str) -> List[Permission]:
        """Get permissions by category"""
        return self._by_category.get(category, [])
    
    def by_resource(self, resource: str) -> List[Permission]:
        """Get permissions by resource"""
        return self._by_resource.get(resource, [])
    
    def categories(self) -> List[str]:
        """Get all categories"""
        return list(self._by_category.keys())
    
    def resources(self) -> List[str]:
        """Get all resources"""
        return list(self._by_resource.keys())
    
    def validate(self, codes: List[str]) -> tuple[List[str], List[str]]:
        """Validate permission codes, returns (valid, invalid)"""
        valid = []
        invalid = []
        for code in codes:
            if self.exists(code):
                valid.append(self.resolve(code) or code)
            else:
                invalid.append(code)
        return valid, invalid


# ==================== Permission Definitions ====================

def _create_permissions() -> PermissionRegistry:
    """Create and populate the permission registry"""
    registry = PermissionRegistry()
    
    # Dashboard permissions
    registry.register(Permission(
        resource="dashboard", action="view",
        display_name="View Dashboard",
        description="Access to dashboard and overview",
        category="Dashboard"
    ))
    
    # Server (Proxmox) permissions
    registry.register(Permission(
        resource="server", action="view",
        display_name="View Servers",
        description="View Proxmox server list and status",
        category="Proxmox Servers"
    ))
    registry.register(Permission(
        resource="server", action="create",
        display_name="Add Server",
        description="Add new Proxmox servers",
        category="Proxmox Servers"
    ))
    registry.register(Permission(
        resource="server", action="update",
        display_name="Edit Server",
        description="Edit Proxmox server settings",
        category="Proxmox Servers"
    ))
    registry.register(Permission(
        resource="server", action="delete",
        display_name="Delete Server",
        description="Remove Proxmox servers",
        category="Proxmox Servers"
    ))
    registry.register(Permission(
        resource="server", action="manage",
        display_name="Manage Servers",
        description="Full server management including cluster operations",
        category="Proxmox Servers",
        requires=["server:view"]
    ))
    
    # VM permissions
    registry.register(Permission(
        resource="vm", action="view",
        display_name="View VMs",
        description="View virtual machines list and details",
        category="Virtual Machines"
    ))
    registry.register(Permission(
        resource="vm", action="create",
        display_name="Create VMs",
        description="Create new virtual machines",
        category="Virtual Machines",
        requires=["vm:view", "template:view"]
    ))
    registry.register(Permission(
        resource="vm", action="update",
        display_name="Update VMs",
        description="Modify VM configuration",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="delete",
        display_name="Delete VMs",
        description="Delete virtual machines",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="start",
        display_name="Start VMs",
        description="Start virtual machines",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="stop",
        display_name="Stop VMs",
        description="Stop virtual machines",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="restart",
        display_name="Restart VMs",
        description="Restart virtual machines",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="console",
        display_name="VM Console",
        description="Access VM console (noVNC/xterm.js)",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="migrate",
        display_name="Migrate VMs",
        description="Migrate VMs between nodes",
        category="Virtual Machines",
        requires=["vm:view"]
    ))
    registry.register(Permission(
        resource="vm", action="execute",
        display_name="Execute Commands",
        description="Execute commands on VMs via QEMU agent",
        category="Virtual Machines",
        requires=["vm:console"]
    ))
    
    # LXC (Container) permissions - same structure as VM
    registry.register(Permission(
        resource="lxc", action="view",
        display_name="View Containers",
        description="View LXC containers list and details",
        category="Containers"
    ))
    registry.register(Permission(
        resource="lxc", action="create",
        display_name="Create Containers",
        description="Create new LXC containers",
        category="Containers",
        requires=["lxc:view", "template:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="update",
        display_name="Update Containers",
        description="Modify container configuration",
        category="Containers",
        requires=["lxc:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="delete",
        display_name="Delete Containers",
        description="Delete LXC containers",
        category="Containers",
        requires=["lxc:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="start",
        display_name="Start Containers",
        description="Start LXC containers",
        category="Containers",
        requires=["lxc:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="stop",
        display_name="Stop Containers",
        description="Stop LXC containers",
        category="Containers",
        requires=["lxc:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="restart",
        display_name="Restart Containers",
        description="Restart LXC containers",
        category="Containers",
        requires=["lxc:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="console",
        display_name="Container Console",
        description="Access container console",
        category="Containers",
        requires=["lxc:view"]
    ))
    registry.register(Permission(
        resource="lxc", action="migrate",
        display_name="Migrate Containers",
        description="Migrate containers between nodes",
        category="Containers",
        requires=["lxc:view"]
    ))
    
    # Template permissions
    registry.register(Permission(
        resource="template", action="view",
        display_name="View Templates",
        description="View OS templates",
        category="Templates"
    ))
    registry.register(Permission(
        resource="template", action="create",
        display_name="Create Templates",
        description="Create new templates",
        category="Templates",
        requires=["template:view"]
    ))
    registry.register(Permission(
        resource="template", action="update",
        display_name="Update Templates",
        description="Edit template settings",
        category="Templates",
        requires=["template:view"]
    ))
    registry.register(Permission(
        resource="template", action="delete",
        display_name="Delete Templates",
        description="Delete templates",
        category="Templates",
        requires=["template:view"]
    ))
    registry.register(Permission(
        resource="template", action="manage",
        display_name="Manage Templates",
        description="Full template management including download",
        category="Templates",
        requires=["template:view"]
    ))
    
    # Storage permissions
    registry.register(Permission(
        resource="storage", action="view",
        display_name="View Storage",
        description="View storage pools and usage",
        category="Storage"
    ))
    registry.register(Permission(
        resource="storage", action="manage",
        display_name="Manage Storage",
        description="Manage storage pools and volumes",
        category="Storage",
        requires=["storage:view"]
    ))
    
    # Backup permissions
    registry.register(Permission(
        resource="backup", action="view",
        display_name="View Backups",
        description="View backup list",
        category="Backups"
    ))
    registry.register(Permission(
        resource="backup", action="create",
        display_name="Create Backups",
        description="Create new backups",
        category="Backups",
        requires=["backup:view"]
    ))
    registry.register(Permission(
        resource="backup", action="delete",
        display_name="Delete Backups",
        description="Delete backups",
        category="Backups",
        requires=["backup:view"]
    ))
    registry.register(Permission(
        resource="backup", action="manage",
        display_name="Restore Backups",
        description="Restore from backups",
        category="Backups",
        requires=["backup:view", "vm:create"]
    ))
    
    # IPAM permissions
    registry.register(Permission(
        resource="ipam", action="view",
        display_name="View IPAM",
        description="View IP address management",
        category="IPAM"
    ))
    registry.register(Permission(
        resource="ipam", action="manage",
        display_name="Manage IPAM",
        description="Manage networks and IP allocations",
        category="IPAM",
        requires=["ipam:view"]
    ))
    
    # User management permissions
    registry.register(Permission(
        resource="user", action="view",
        display_name="View Users",
        description="View user list",
        category="User Management"
    ))
    registry.register(Permission(
        resource="user", action="create",
        display_name="Create Users",
        description="Create new users",
        category="User Management",
        requires=["user:view"]
    ))
    registry.register(Permission(
        resource="user", action="update",
        display_name="Update Users",
        description="Edit user profiles",
        category="User Management",
        requires=["user:view"]
    ))
    registry.register(Permission(
        resource="user", action="delete",
        display_name="Delete Users",
        description="Delete users",
        category="User Management",
        requires=["user:view"]
    ))
    
    # Role management permissions
    registry.register(Permission(
        resource="role", action="view",
        display_name="View Roles",
        description="View roles and permissions",
        category="Role Management"
    ))
    registry.register(Permission(
        resource="role", action="create",
        display_name="Create Roles",
        description="Create new roles",
        category="Role Management",
        requires=["role:view"]
    ))
    registry.register(Permission(
        resource="role", action="update",
        display_name="Update Roles",
        description="Edit role permissions",
        category="Role Management",
        requires=["role:view"]
    ))
    registry.register(Permission(
        resource="role", action="delete",
        display_name="Delete Roles",
        description="Delete roles",
        category="Role Management",
        requires=["role:view"]
    ))
    registry.register(Permission(
        resource="role", action="manage",
        display_name="Manage Roles",
        description="Full role management including assignment",
        category="Role Management",
        requires=["role:view", "user:view"]
    ))
    
    # Log permissions
    registry.register(Permission(
        resource="log", action="view",
        display_name="View Logs",
        description="View audit logs",
        category="Logs"
    ))
    registry.register(Permission(
        resource="log", action="export",
        display_name="Export Logs",
        description="Export log data",
        category="Logs",
        requires=["log:view"]
    ))
    registry.register(Permission(
        resource="log", action="delete",
        display_name="Delete Logs",
        description="Delete log entries",
        category="Logs",
        requires=["log:view"]
    ))
    
    # Settings permissions
    registry.register(Permission(
        resource="setting", action="view",
        display_name="View Settings",
        description="View panel settings",
        category="Settings"
    ))
    registry.register(Permission(
        resource="setting", action="update",
        display_name="Update Settings",
        description="Modify panel settings",
        category="Settings",
        requires=["setting:view"]
    ))
    registry.register(Permission(
        resource="setting", action="manage",
        display_name="Manage Security Settings",
        description="Manage security and advanced settings",
        category="Settings",
        requires=["setting:view"]
    ))
    
    # Notification permissions
    registry.register(Permission(
        resource="notification", action="view",
        display_name="View Notifications",
        description="View notifications",
        category="Notifications"
    ))
    registry.register(Permission(
        resource="notification", action="manage",
        display_name="Manage Notifications",
        description="Manage notification settings",
        category="Notifications",
        requires=["notification:view"]
    ))
    
    return registry


# Global registry instance
PERMISSIONS = _create_permissions()


# ==================== Legacy Permission Mapping ====================

LEGACY_PERMISSION_MAP = {
    # Old format -> New format
    "dashboard.view": "dashboard:view",
    "proxmox.view": "server:view",
    "proxmox.manage": "server:manage",
    "proxmox.servers.add": "server:create",
    "proxmox.servers.edit": "server:update",
    "proxmox.servers.delete": "server:delete",
    "proxmox.vm.manage": "vm:manage",
    "vms.view": "vm:view",
    "vms.create": "vm:create",
    "vms.start": "vm:start",
    "vms.stop": "vm:stop",
    "vms.restart": "vm:restart",
    "vms.delete": "vm:delete",
    "vms.console": "vm:console",
    "vms.migrate": "vm:migrate",
    "templates.view": "template:view",
    "templates.manage": "template:manage",
    "ipam.view": "ipam:view",
    "ipam.manage": "ipam:manage",
    "logs.view": "log:view",
    "logs.export": "log:export",
    "logs.delete": "log:delete",
    "settings.view": "setting:view",
    "settings.panel": "setting:update",
    "settings.security": "setting:manage",
    "users.view": "user:view",
    "users.create": "user:create",
    "users.edit": "user:update",
    "users.delete": "user:delete",
    "roles.view": "role:view",
    "roles.manage": "role:manage",
    "notifications.manage": "notification:manage",
}


def resolve_permission(code: str) -> str:
    """Resolve legacy permission code to new format"""
    return LEGACY_PERMISSION_MAP.get(code, code)


def get_permission_info(code: str) -> Optional[Permission]:
    """Get permission object by code (supports legacy)"""
    resolved = resolve_permission(code)
    return PERMISSIONS.get(resolved)
