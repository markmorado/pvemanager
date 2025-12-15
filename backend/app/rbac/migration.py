"""
RBAC Migration Utilities
Converts legacy permission format to new format
"""

from typing import Dict, Optional
from loguru import logger
from sqlalchemy import text

from .permissions import LEGACY_PERMISSION_MAP, PERMISSIONS


# New format DEFAULT_ROLES with resource:action format
NEW_DEFAULT_ROLES = [
    {
        "name": "admin",
        "display_name": "Administrator",
        "description": "Full access to all features",
        "is_system": True,
        "permissions": {
            # Dashboard
            "dashboard:view": True,
            # Servers
            "server:view": True,
            "server:create": True,
            "server:update": True,
            "server:delete": True,
            "server:manage": True,
            # VMs
            "vm:view": True,
            "vm:create": True,
            "vm:update": True,
            "vm:delete": True,
            "vm:start": True,
            "vm:stop": True,
            "vm:restart": True,
            "vm:console": True,
            "vm:migrate": True,
            "vm:execute": True,
            # LXC
            "lxc:view": True,
            "lxc:create": True,
            "lxc:update": True,
            "lxc:delete": True,
            "lxc:start": True,
            "lxc:stop": True,
            "lxc:restart": True,
            "lxc:console": True,
            "lxc:migrate": True,
            # Templates
            "template:view": True,
            "template:create": True,
            "template:update": True,
            "template:delete": True,
            "template:manage": True,
            # Storage
            "storage:view": True,
            "storage:manage": True,
            # Backups
            "backup:view": True,
            "backup:create": True,
            "backup:delete": True,
            "backup:manage": True,
            # IPAM
            "ipam:view": True,
            "ipam:manage": True,
            # Users
            "user:view": True,
            "user:create": True,
            "user:update": True,
            "user:delete": True,
            # Roles
            "role:view": True,
            "role:create": True,
            "role:update": True,
            "role:delete": True,
            "role:manage": True,
            # Logs
            "log:view": True,
            "log:export": True,
            "log:delete": True,
            # Settings
            "setting:view": True,
            "setting:update": True,
            "setting:manage": True,
            # Notifications
            "notification:view": True,
            "notification:manage": True,
        }
    },
    {
        "name": "moderator",
        "display_name": "Moderator",
        "description": "Can manage VMs and view logs",
        "is_system": True,
        "permissions": {
            "dashboard:view": True,
            "server:view": True,
            "vm:view": True,
            "vm:create": True,
            "vm:start": True,
            "vm:stop": True,
            "vm:restart": True,
            "vm:console": True,
            "lxc:view": True,
            "lxc:create": True,
            "lxc:start": True,
            "lxc:stop": True,
            "lxc:restart": True,
            "lxc:console": True,
            "template:view": True,
            "storage:view": True,
            "backup:view": True,
            "backup:create": True,
            "ipam:view": True,
            "user:view": True,
            "log:view": True,
            "log:export": True,
            "setting:view": True,
            "notification:view": True,
            "notification:manage": True,
        }
    },
    {
        "name": "user",
        "display_name": "User",
        "description": "Standard user with limited access",
        "is_system": True,
        "permissions": {
            "dashboard:view": True,
            "server:view": True,
            "vm:view": True,
            "vm:start": True,
            "vm:stop": True,
            "vm:restart": True,
            "vm:console": True,
            "lxc:view": True,
            "lxc:start": True,
            "lxc:stop": True,
            "lxc:restart": True,
            "lxc:console": True,
            "template:view": True,
            "storage:view": True,
            "ipam:view": True,
            "setting:view": True,
            "notification:view": True,
            "notification:manage": True,
        }
    },
    {
        "name": "demo",
        "display_name": "Demo User",
        "description": "Read-only access for demonstration",
        "is_system": True,
        "permissions": {
            "dashboard:view": True,
            "server:view": True,
            "vm:view": True,
            "lxc:view": True,
            "template:view": True,
            "storage:view": True,
            "ipam:view": True,
        }
    },
]


def convert_permissions_to_new_format(old_permissions: Dict[str, bool]) -> Dict[str, bool]:
    """
    Convert legacy permission format to new format.
    
    Args:
        old_permissions: Dict with old format like {"vms.view": True, "proxmox.manage": True}
    
    Returns:
        Dict with new format like {"vm:view": True, "server:manage": True}
    """
    if not old_permissions:
        return {}
    
    new_permissions = {}
    
    for perm, value in old_permissions.items():
        # Check if already in new format
        if ":" in perm:
            new_permissions[perm] = value
            continue
        
        # Convert using legacy map
        if perm in LEGACY_PERMISSION_MAP:
            new_perm = LEGACY_PERMISSION_MAP[perm]
            # Only add True permissions to new format
            if value:
                new_permissions[new_perm] = value
        else:
            # Keep unknown permissions as-is for backwards compatibility
            if value:
                logger.warning(f"Unknown permission format: {perm}")
                new_permissions[perm] = value
    
    return new_permissions


def convert_permissions_to_legacy_format(new_permissions: Dict[str, bool]) -> Dict[str, bool]:
    """
    Convert new permission format back to legacy format (for backwards compatibility).
    
    Args:
        new_permissions: Dict with new format like {"vm:view": True}
    
    Returns:
        Dict with old format like {"vms.view": True}
    """
    if not new_permissions:
        return {}
    
    # Create reverse map
    reverse_map = {v: k for k, v in LEGACY_PERMISSION_MAP.items()}
    
    legacy_permissions = {}
    
    for perm, value in new_permissions.items():
        if not value:
            continue
            
        # Check if in new format
        if ":" in perm and perm in reverse_map:
            legacy_perm = reverse_map[perm]
            legacy_permissions[legacy_perm] = value
        else:
            # Keep as-is
            legacy_permissions[perm] = value
    
    return legacy_permissions


def get_permission_categories() -> Dict[str, Dict[str, str]]:
    """
    Get permissions grouped by category with display names.
    
    Returns:
        Dict like {"Virtual Machines": {"vm:view": "View VMs", "vm:create": "Create VMs"}}
    """
    categories = {}
    
    for perm in PERMISSIONS.all():
        if perm.category not in categories:
            categories[perm.category] = {}
        categories[perm.category][perm.code] = perm.display_name
    
    return categories


def get_permission_categories_legacy() -> Dict[str, Dict[str, str]]:
    """
    Get permissions in legacy format grouped by category.
    For backwards compatibility with existing UI.
    
    Returns:
        Dict like {"Virtual Machines": {"vms.view": "View VMs"}}
    """
    # Create reverse map
    reverse_map = {v: k for k, v in LEGACY_PERMISSION_MAP.items()}
    
    categories = {}
    
    for perm in PERMISSIONS.all():
        if perm.category not in categories:
            categories[perm.category] = {}
        
        # Use legacy code if available
        legacy_code = reverse_map.get(perm.code, perm.code)
        categories[perm.category][legacy_code] = perm.display_name
    
    return categories


def migrate_role_permissions(conn, role_id: int, old_permissions: dict) -> dict:
    """
    Migrate a single role's permissions to new format.
    
    Args:
        conn: Database connection
        role_id: Role ID
        old_permissions: Old permissions dict
    
    Returns:
        New permissions dict
    """
    import json
    
    new_permissions = convert_permissions_to_new_format(old_permissions)
    
    conn.execute(
        text("UPDATE roles SET permissions = :perms WHERE id = :id"),
        {"perms": json.dumps(new_permissions), "id": role_id}
    )
    
    logger.info(f"Migrated role {role_id} permissions to new format")
    return new_permissions


def migrate_all_roles_to_new_format(conn) -> int:
    """
    Migrate all roles to new permission format.
    
    Args:
        conn: Database connection
    
    Returns:
        Number of migrated roles
    """
    import json
    
    # Get all roles
    result = conn.execute(text("SELECT id, name, permissions FROM roles"))
    roles = result.fetchall()
    
    migrated_count = 0
    
    for role in roles:
        role_id, role_name, permissions = role
        
        if not permissions:
            continue
        
        # Parse permissions if string
        if isinstance(permissions, str):
            try:
                permissions = json.loads(permissions)
            except:
                continue
        
        # Check if already migrated (has : in keys)
        has_new_format = any(":" in k for k in permissions.keys())
        has_old_format = any("." in k and ":" not in k for k in permissions.keys())
        
        if has_old_format and not has_new_format:
            new_permissions = convert_permissions_to_new_format(permissions)
            
            conn.execute(
                text("UPDATE roles SET permissions = :perms WHERE id = :id"),
                {"perms": json.dumps(new_permissions), "id": role_id}
            )
            
            logger.info(f"Migrated role '{role_name}' (id={role_id}) to new permission format")
            migrated_count += 1
        elif has_old_format and has_new_format:
            # Mixed format - merge and convert
            new_permissions = convert_permissions_to_new_format(permissions)
            
            conn.execute(
                text("UPDATE roles SET permissions = :perms WHERE id = :id"),
                {"perms": json.dumps(new_permissions), "id": role_id}
            )
            
            logger.info(f"Merged and migrated role '{role_name}' (id={role_id})")
            migrated_count += 1
    
    return migrated_count


def ensure_default_roles_new_format(conn) -> None:
    """
    Ensure default roles exist with new permission format.
    Updates existing roles or creates new ones.
    """
    import json
    
    for role_data in NEW_DEFAULT_ROLES:
        # Check if role exists
        result = conn.execute(
            text("SELECT id, permissions FROM roles WHERE name = :name"),
            {"name": role_data["name"]}
        )
        existing = result.fetchone()
        
        if existing:
            role_id, current_perms = existing
            
            # Parse current permissions
            if isinstance(current_perms, str):
                try:
                    current_perms = json.loads(current_perms)
                except:
                    current_perms = {}
            
            # Check if needs update
            has_old_format = any("." in k and ":" not in k for k in (current_perms or {}).keys())
            
            if has_old_format or not current_perms:
                # Update to new format
                conn.execute(
                    text("""
                        UPDATE roles 
                        SET permissions = :perms,
                            display_name = :display_name,
                            description = :description
                        WHERE id = :id
                    """),
                    {
                        "perms": json.dumps(role_data["permissions"]),
                        "display_name": role_data["display_name"],
                        "description": role_data["description"],
                        "id": role_id
                    }
                )
                logger.info(f"Updated role '{role_data['name']}' with new permission format")
        else:
            # Create new role
            conn.execute(
                text("""
                    INSERT INTO roles (name, display_name, description, permissions, is_system)
                    VALUES (:name, :display_name, :description, :permissions, :is_system)
                """),
                {
                    "name": role_data["name"],
                    "display_name": role_data["display_name"],
                    "description": role_data["description"],
                    "permissions": json.dumps(role_data["permissions"]),
                    "is_system": role_data["is_system"]
                }
            )
            logger.info(f"Created role '{role_data['name']}' with new permission format")
