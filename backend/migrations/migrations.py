"""
PVEmanager Database Migrations
==============================
Unified migration file for all database schema changes.

This file contains all migrations in order:
1. Notifications system (tables: notifications, notification_preferences)
2. RBAC and Security (tables: roles, active_sessions, login_attempts, blocked_ips, security_settings)
3. Cross-node Template Support (columns in os_templates)
4. VM Cache (columns in vm_instances)
5. Task Queue
6. OS Template Icons
7. VM Template Name
8. Enhanced Audit Logs
9. RBAC v2 - Migrate to new permission format

Usage:
    from migrations.migrations import run_all_migrations
    run_all_migrations(engine, db_session)
"""

import json
from sqlalchemy import text
from loguru import logger


# ==================== Default Data ====================

DEFAULT_ROLES = [
    {
        "name": "admin",
        "display_name": "Administrator",
        "description": "Full access to all features",
        "is_system": True,
        "permissions": {
            "dashboard.view": True,
            "proxmox.view": True,
            "proxmox.manage": True,
            "proxmox.servers.add": True,
            "proxmox.servers.edit": True,
            "proxmox.servers.delete": True,
            "vms.view": True,
            "vms.create": True,
            "vms.start": True,
            "vms.stop": True,
            "vms.restart": True,
            "vms.delete": True,
            "vms.console": True,
            "vms.migrate": True,
            "templates.view": True,
            "templates.manage": True,
            "ipam.view": True,
            "ipam.manage": True,
            "logs.view": True,
            "logs.export": True,
            "logs.delete": True,
            "settings.view": True,
            "settings.panel": True,
            "settings.security": True,
            "users.view": True,
            "users.create": True,
            "users.edit": True,
            "users.delete": True,
            "quota:view": True,
            "quota:manage": True,
            "roles.view": True,
            "roles.manage": True,
            "notifications.manage": True,
            "backup:view": True,
            "backup:create": True,
            "backup:restore": True,
            "backup:delete": True,
            "backup:manage": True,
        }
    },
    {
        "name": "moderator",
        "display_name": "Moderator",
        "description": "Can manage VMs and view logs",
        "is_system": True,
        "permissions": {
            "dashboard.view": True,
            "proxmox.view": True,
            "proxmox.manage": False,
            "proxmox.servers.add": False,
            "proxmox.servers.edit": False,
            "proxmox.servers.delete": False,
            "vms.view": True,
            "vms.create": True,
            "vms.start": True,
            "vms.stop": True,
            "vms.restart": True,
            "vms.delete": False,
            "vms.console": True,
            "vms.migrate": False,
            "templates.view": True,
            "templates.manage": False,
            "ipam.view": True,
            "ipam.manage": False,
            "logs.view": True,
            "logs.export": True,
            "logs.delete": False,
            "settings.view": True,
            "settings.panel": False,
            "settings.security": False,
            "users.view": True,
            "users.create": False,
            "users.edit": False,
            "users.delete": False,
            "roles.view": False,
            "roles.manage": False,
            "notifications.manage": True,
            "backup:view": True,
            "backup:create": True,
            "backup:restore": True,
            "backup:delete": False,
            "backup:manage": False,
        }
    },
    {
        "name": "user",
        "display_name": "User",
        "description": "VPS-style user: can only access their own instances",
        "is_system": True,
        "permissions": {
            "dashboard.view": False,
            "proxmox.view": True,
            "proxmox.manage": False,
            "proxmox.servers.add": False,
            "proxmox.servers.edit": False,
            "proxmox.servers.delete": False,
            "vms.view": False,
            "vms:view:own": True,
            "vms.create": False,
            "vms.start": True,
            "vms:start:own": True,
            "vms.stop": True,
            "vms:stop:own": True,
            "vms.restart": True,
            "vms:restart:own": True,
            "vms.delete": False,
            "vms.console": True,
            "vms:console:own": True,
            "vms:snapshots:own": True,
            "vms.migrate": False,
            "templates.view": True,
            "templates.manage": False,
            "ipam.view": False,
            "ipam.manage": False,
            "logs.view": False,
            "logs.export": False,
            "logs.delete": False,
            "settings.view": True,
            "settings.panel": False,
            "settings.security": False,
            "users.view": False,
            "users.create": False,
            "users.edit": False,
            "users.delete": False,
            "roles.view": False,
            "roles.manage": False,
            "notifications.manage": True,
        }
    },
    {
        "name": "demo",
        "display_name": "Demo User",
        "description": "Read-only access for demonstration",
        "is_system": True,
        "permissions": {
            "dashboard.view": True,
            "proxmox.view": True,
            "proxmox.manage": False,
            "proxmox.servers.add": False,
            "proxmox.servers.edit": False,
            "proxmox.servers.delete": False,
            "vms.view": True,
            "vms.create": False,
            "vms.start": False,
            "vms.stop": False,
            "vms.restart": False,
            "vms.delete": False,
            "vms.console": False,
            "vms.migrate": False,
            "templates.view": True,
            "templates.manage": False,
            "ipam.view": True,
            "ipam.manage": False,
            "logs.view": False,
            "logs.export": False,
            "logs.delete": False,
            "settings.view": False,
            "settings.panel": False,
            "settings.security": False,
            "users.view": False,
            "users.create": False,
            "users.edit": False,
            "users.delete": False,
            "roles.view": False,
            "roles.manage": False,
            "notifications.manage": False,
        }
    },
]

SECURITY_DEFAULTS = [
    ("max_login_attempts", "5", "Maximum failed login attempts before lockout"),
    ("lockout_duration_minutes", "30", "Account lockout duration in minutes"),
    ("session_timeout_minutes", "60", "Session timeout in minutes"),
    ("single_session_enabled", "false", "Allow only one active session per user"),
    ("ip_block_threshold", "10", "Failed attempts before IP is blocked"),
    ("ip_block_duration_minutes", "60", "IP block duration in minutes"),
    ("password_min_length", "8", "Minimum password length"),
    ("password_require_uppercase", "true", "Require uppercase letters in password"),
    ("password_require_lowercase", "true", "Require lowercase letters in password"),
    ("password_require_numbers", "true", "Require numbers in password"),
    ("password_require_special", "false", "Require special characters in password"),
    ("password_expiry_days", "0", "Password expiry in days (0 = never)"),
    ("api_rate_limit_per_minute", "60", "API rate limit per minute per user"),
]


# ==================== Helper Functions ====================

def table_exists(conn, table_name: str) -> bool:
    """Check if a table exists"""
    result = conn.execute(text(f"""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = '{table_name}'
        )
    """))
    return result.scalar()


def column_exists(conn, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table"""
    result = conn.execute(text(f"""
        SELECT column_name 
        FROM information_schema.columns 
        WHERE table_name = '{table_name}' AND column_name = '{column_name}'
    """))
    return result.fetchone() is not None


def add_column_if_not_exists(conn, table_name: str, column_name: str, column_type: str):
    """Add a column if it doesn't exist"""
    if not column_exists(conn, table_name, column_name):
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
        logger.debug(f"Added column {table_name}.{column_name}")
        return True
    return False


# ==================== Migration 1: Notifications ====================

def migrate_notifications(conn):
    """Create notifications system tables"""
    
    if table_exists(conn, 'notifications'):
        logger.info("✓ Notifications tables already exist")
        return
    
    logger.info("Creating notifications tables...")
    
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            type VARCHAR(50) NOT NULL,
            level VARCHAR(20) NOT NULL CHECK (level IN ('critical', 'warning', 'info', 'success')),
            title VARCHAR(255) NOT NULL,
            message TEXT,
            data JSONB,
            link VARCHAR(500),
            source VARCHAR(50),
            source_id VARCHAR(100),
            read BOOLEAN DEFAULT FALSE,
            read_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP
        )
    """))
    
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read, created_at DESC)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_notifications_level ON notifications(level, created_at DESC)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC)
    """))
    
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS notification_preferences (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) UNIQUE ON DELETE CASCADE,
            enabled BOOLEAN DEFAULT TRUE,
            email_enabled BOOLEAN DEFAULT FALSE,
            email_critical_only BOOLEAN DEFAULT TRUE,
            telegram_enabled BOOLEAN DEFAULT FALSE,
            telegram_chat_id VARCHAR(100),
            webhook_url VARCHAR(500),
            notification_levels JSONB DEFAULT '["critical", "warning", "info", "success"]',
            notification_types JSONB DEFAULT '["vm_status", "resource_alert", "system", "ipam", "template"]',
            quiet_hours_start TIME,
            quiet_hours_end TIME,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """))
    
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_notification_preferences_user ON notification_preferences(user_id)
    """))
    
    logger.info("✓ Notifications tables created")


# ==================== Migration 2: RBAC & Security ====================

def migrate_rbac_security(conn):
    """Create RBAC and security tables"""
    
    # Roles table
    if not table_exists(conn, 'roles'):
        logger.info("Creating roles table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS roles (
                id SERIAL PRIMARY KEY,
                name VARCHAR(50) UNIQUE NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                description TEXT,
                permissions JSONB NOT NULL DEFAULT '{}',
                is_system BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """))
        logger.info("✓ Roles table created")
    
    # Active sessions table
    if not table_exists(conn, 'active_sessions'):
        logger.info("Creating active_sessions table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS active_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_token VARCHAR(64) UNIQUE NOT NULL,
                ip_address VARCHAR(45),
                user_agent VARCHAR(500),
                device_info VARCHAR(200),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                last_activity TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE
            )
        """))
        logger.info("✓ Active sessions table created")
    
    # Login attempts table
    if not table_exists(conn, 'login_attempts'):
        logger.info("Creating login_attempts table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100),
                ip_address VARCHAR(45) NOT NULL,
                user_agent VARCHAR(500),
                success BOOLEAN DEFAULT FALSE,
                failure_reason VARCHAR(200),
                attempted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """))
        logger.info("✓ Login attempts table created")
    
    # Blocked IPs table
    if not table_exists(conn, 'blocked_ips'):
        logger.info("Creating blocked_ips table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS blocked_ips (
                id SERIAL PRIMARY KEY,
                ip_address VARCHAR(45) UNIQUE NOT NULL,
                reason VARCHAR(500),
                blocked_by VARCHAR(100),
                blocked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE,
                is_permanent BOOLEAN DEFAULT FALSE,
                attempts_count INTEGER DEFAULT 0
            )
        """))
        logger.info("✓ Blocked IPs table created")
    
    # Security settings table
    if not table_exists(conn, 'security_settings'):
        logger.info("Creating security_settings table...")
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS security_settings (
                id SERIAL PRIMARY KEY,
                key VARCHAR(100) UNIQUE NOT NULL,
                value VARCHAR(500) NOT NULL,
                description TEXT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """))
        logger.info("✓ Security settings table created")
    
    # Add columns to users table
    user_columns = [
        ("role_id", "INTEGER REFERENCES roles(id)"),
        ("failed_login_attempts", "INTEGER DEFAULT 0"),
        ("locked_until", "TIMESTAMP WITH TIME ZONE"),
        ("last_password_change", "TIMESTAMP WITH TIME ZONE"),
        ("require_password_change", "BOOLEAN DEFAULT FALSE"),
        ("two_factor_enabled", "BOOLEAN DEFAULT FALSE"),
        ("two_factor_secret", "VARCHAR(100)"),
    ]
    
    for col_name, col_type in user_columns:
        try:
            add_column_if_not_exists(conn, 'users', col_name, col_type)
        except Exception as e:
            logger.debug(f"Column users.{col_name}: {e}")
    
    # Create indexes
    indexes = [
        ("idx_sessions_user", "active_sessions(user_id)"),
        ("idx_sessions_token", "active_sessions(session_token)"),
        ("idx_sessions_expires", "active_sessions(expires_at)"),
        ("idx_login_attempts_ip", "login_attempts(ip_address)"),
        ("idx_login_attempts_time", "login_attempts(attempted_at)"),
        ("idx_blocked_ips_ip", "blocked_ips(ip_address)"),
        ("idx_blocked_ips_expires", "blocked_ips(expires_at)"),
        ("idx_users_role", "users(role_id)"),
    ]
    
    for idx_name, idx_def in indexes:
        try:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}"))
        except Exception as e:
            logger.debug(f"Index {idx_name}: {e}")
    
    # Insert default roles
    for role in DEFAULT_ROLES:
        conn.execute(text("""
            INSERT INTO roles (name, display_name, description, permissions, is_system, is_active)
            VALUES (:name, :display_name, :description, :permissions, :is_system, TRUE)
            ON CONFLICT (name) DO NOTHING
        """), {
            "name": role["name"],
            "display_name": role["display_name"],
            "description": role["description"],
            "permissions": json.dumps(role["permissions"]),
            "is_system": role["is_system"]
        })
    
    # Insert default security settings
    for key, value, description in SECURITY_DEFAULTS:
        conn.execute(text("""
            INSERT INTO security_settings (key, value, description)
            VALUES (:key, :value, :description)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "value": value, "description": description})
    
    # Set admin role for existing admin users
    conn.execute(text("""
        UPDATE users 
        SET role_id = (SELECT id FROM roles WHERE name = 'admin')
        WHERE is_admin = TRUE AND role_id IS NULL
    """))
    
    # Set user role for non-admin users
    conn.execute(text("""
        UPDATE users 
        SET role_id = (SELECT id FROM roles WHERE name = 'user')
        WHERE is_admin = FALSE AND role_id IS NULL
    """))
    
    logger.info("✓ RBAC and security migration completed")


# ==================== Migration 3: Cross-node Templates ====================

def migrate_cross_node_templates(conn):
    """Add cross-node template support"""
    
    if not table_exists(conn, 'os_templates'):
        logger.info("Table os_templates does not exist, skipping cross-node migration")
        return
    
    # Add source_node column
    if add_column_if_not_exists(conn, 'os_templates', 'source_node', 'VARCHAR(100)'):
        # Copy existing node values to source_node
        conn.execute(text("""
            UPDATE os_templates 
            SET source_node = node 
            WHERE source_node IS NULL
        """))
        logger.info("✓ Added source_node column to os_templates")
    
    # Add replicated_nodes column
    if add_column_if_not_exists(conn, 'os_templates', 'replicated_nodes', "JSONB DEFAULT '{}'::jsonb"):
        logger.info("✓ Added replicated_nodes column to os_templates")

    # Add vm_type column (qemu/lxc) — existing rows default to qemu
    if add_column_if_not_exists(conn, 'os_templates', 'vm_type', "VARCHAR(10) NOT NULL DEFAULT 'qemu'"):
        logger.info("✓ Added vm_type column to os_templates")

    # Add volid column for LXC vztmpl file-based templates
    if add_column_if_not_exists(conn, 'os_templates', 'volid', "VARCHAR(500)"):
        logger.info("✓ Added volid column to os_templates")

    # vmid must be nullable for vztmpl-based templates
    try:
        conn.execute(text("ALTER TABLE os_templates ALTER COLUMN vmid DROP NOT NULL"))
    except Exception:
        pass
    
    # Make node column nullable
    try:
        conn.execute(text("""
            ALTER TABLE os_templates 
            ALTER COLUMN node DROP NOT NULL
        """))
    except Exception:
        pass  # Column might already be nullable
    
    logger.info("✓ Cross-node template migration completed")


# ==================== Migration 4: VM Cache ====================

def migrate_vm_cache(conn):
    """Add VM cache fields to vm_instances table"""
    
    if not table_exists(conn, 'vm_instances'):
        logger.info("Table vm_instances does not exist, skipping VM cache migration")
        return
    
    # Add new columns
    cache_columns = [
        ("status", "VARCHAR(20) DEFAULT 'unknown'"),
        ("is_template", "BOOLEAN DEFAULT FALSE"),
        ("os_type", "VARCHAR(50)"),
        ("last_sync_at", "TIMESTAMP WITH TIME ZONE"),
    ]
    
    for col_name, col_type in cache_columns:
        add_column_if_not_exists(conn, 'vm_instances', col_name, col_type)
    
    # Alter memory and disk_size to BigInteger
    try:
        conn.execute(text("""
            ALTER TABLE vm_instances 
            ALTER COLUMN memory TYPE BIGINT
        """))
    except Exception:
        pass
    
    try:
        conn.execute(text("""
            ALTER TABLE vm_instances 
            ALTER COLUMN disk_size TYPE BIGINT
        """))
    except Exception:
        pass
    
    logger.info("✓ VM cache migration completed")


# ==================== Migration 5: Task Queue ====================

def migrate_task_queue(conn):
    """Create task_queue table for bulk operations"""
    
    if table_exists(conn, 'task_queue'):
        logger.info("Table task_queue already exists, skipping")
        return
    
    conn.execute(text("""
        CREATE TABLE task_queue (
            id SERIAL PRIMARY KEY,
            task_type VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            user_id INTEGER NOT NULL,
            total_items INTEGER NOT NULL DEFAULT 0,
            completed_items INTEGER NOT NULL DEFAULT 0,
            failed_items INTEGER NOT NULL DEFAULT 0,
            task_data JSONB NOT NULL,
            results JSONB,
            error_message TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            started_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE
        )
    """))
    
    # Create indexes
    conn.execute(text("""
        CREATE INDEX idx_task_queue_task_type ON task_queue (task_type)
    """))
    conn.execute(text("""
        CREATE INDEX idx_task_queue_status ON task_queue (status)
    """))
    conn.execute(text("""
        CREATE INDEX idx_task_queue_user_id ON task_queue (user_id)
    """))
    conn.execute(text("""
        CREATE INDEX idx_task_status_created ON task_queue (status, created_at)
    """))
    conn.execute(text("""
        CREATE INDEX idx_task_user_status ON task_queue (user_id, status)
    """))
    
    logger.info("✓ Task queue table created")


# ==================== Migration 6: Update OS Template Group Icons ====================

# Маппинг названий групп на новые CSS-адаптивные иконки
OS_ICON_MAPPING = {
    'Ubuntu': '<i class="fa-brands fa-ubuntu os-icon os-icon-ubuntu"></i>',
    'Debian': '<i class="fa-brands fa-debian os-icon os-icon-debian"></i>',
    'CentOS': '<i class="fa-brands fa-centos os-icon os-icon-centos"></i>',
    'Rocky Linux': '<span class="os-icon-rocky-svg"></span>',
    'AlmaLinux': '<span class="os-icon-alma-svg"></span>',
    'Fedora': '<i class="fa-brands fa-fedora os-icon os-icon-fedora"></i>',
    'RHEL': '<i class="fa-brands fa-redhat os-icon os-icon-rhel"></i>',
    'Oracle Linux': '<i class="fa-brands fa-linux os-icon os-icon-oracle"></i>',
    'openSUSE': '<i class="fa-brands fa-suse os-icon os-icon-suse"></i>',
    'Arch Linux': '<i class="fa-brands fa-linux os-icon os-icon-arch"></i>',
    'Gentoo': '<i class="fa-brands fa-linux os-icon os-icon-gentoo"></i>',
    'Alpine': '<i class="fa-brands fa-linux os-icon os-icon-alpine"></i>',
    'Windows': '<i class="fa-brands fa-windows os-icon os-icon-windows"></i>',
    'FreeBSD': '<i class="fa-brands fa-freebsd os-icon os-icon-freebsd"></i>',
    'OpenBSD': '<i class="fa-brands fa-linux os-icon os-icon-openbsd"></i>',
    'Other': '<i class="fa-solid fa-compact-disc os-icon os-icon-other"></i>',
}


def migrate_os_template_icons(conn):
    """Update OS template group icons to theme-adaptive CSS classes"""
    logger.info("Migration 6: Updating OS template group icons...")
    
    # Check if os_template_groups table exists
    result = conn.execute(text("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'os_template_groups'
        )
    """))
    if not result.scalar():
        logger.info("os_template_groups table does not exist, skipping icon migration")
        return
    
    # First, ensure the icon column is large enough for HTML
    try:
        conn.execute(text("""
            ALTER TABLE os_template_groups ALTER COLUMN icon TYPE VARCHAR(200)
        """))
        logger.info("Extended icon column to VARCHAR(200)")
    except Exception as e:
        logger.debug(f"Icon column already extended or error: {e}")
    
    # Update icons for each known group name
    for group_name, icon_html in OS_ICON_MAPPING.items():
        try:
            conn.execute(text("""
                UPDATE os_template_groups 
                SET icon = :icon 
                WHERE name = :name
            """), {"icon": icon_html, "name": group_name})
            logger.debug(f"Updated icon for group: {group_name}")
        except Exception as e:
            logger.warning(f"Failed to update icon for {group_name}: {e}")
    
    logger.info("✓ OS template group icons updated")


# ==================== Migration 7: VM Template Name ====================

def migrate_vm_template_name(conn):
    """Add template_name column to vm_instances for caching template name"""
    logger.info("Migration 7: Adding template_name to vm_instances...")
    
    if not table_exists(conn, 'vm_instances'):
        logger.info("Table vm_instances does not exist, skipping")
        return
    
    add_column_if_not_exists(conn, 'vm_instances', 'template_name', 'VARCHAR(100)')
    
    logger.info("✓ VM template_name migration completed")


# ==================== Migration 8: Enhanced Audit Logs ====================

def migrate_enhanced_audit_logs(conn):
    """Add enhanced logging columns to audit_logs table"""
    logger.info("Migration 8: Enhancing audit_logs table...")
    
    if not table_exists(conn, 'audit_logs'):
        logger.info("Table audit_logs does not exist, skipping")
        return
    
    # New context columns
    add_column_if_not_exists(conn, 'audit_logs', 'request_id', 'VARCHAR(36)')
    add_column_if_not_exists(conn, 'audit_logs', 'session_id', 'VARCHAR(64)')
    
    # Enhanced user tracking
    add_column_if_not_exists(conn, 'audit_logs', 'geo_location', 'VARCHAR(100)')
    
    # Proxmox server context
    add_column_if_not_exists(conn, 'audit_logs', 'server_id', 'INTEGER')
    add_column_if_not_exists(conn, 'audit_logs', 'server_name', 'VARCHAR(100)')
    add_column_if_not_exists(conn, 'audit_logs', 'node_name', 'VARCHAR(100)')
    
    # Request/Response details
    add_column_if_not_exists(conn, 'audit_logs', 'request_body', 'JSONB')
    add_column_if_not_exists(conn, 'audit_logs', 'response_body', 'JSONB')
    add_column_if_not_exists(conn, 'audit_logs', 'query_params', 'VARCHAR(1000)')
    
    # Error details
    add_column_if_not_exists(conn, 'audit_logs', 'error_traceback', 'TEXT')
    
    # Create new indexes
    try:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_audit_request_id ON audit_logs(request_id)"
        ))
    except:
        pass
    
    try:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_audit_ip ON audit_logs(ip_address)"
        ))
    except:
        pass
    
    try:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_audit_server ON audit_logs(server_id)"
        ))
    except:
        pass
    
    logger.info("✓ Enhanced audit_logs migration completed")


# ==================== Migration 9: RBAC v2 - New Permission Format ====================

def migrate_rbac_v2(conn):
    """
    Migrate roles to new RBAC v2 permission format.
    Converts old format (resource.action) to new format (resource:action)
    """
    logger.info("Migration 9: Migrating to RBAC v2 permission format...")
    
    if not table_exists(conn, 'roles'):
        logger.info("Table roles does not exist, skipping RBAC v2 migration")
        return
    
    # Import migration utilities
    try:
        from app.rbac.migration import (
            migrate_all_roles_to_new_format,
            ensure_default_roles_new_format
        )
    except ImportError:
        try:
            from backend.app.rbac.migration import (
                migrate_all_roles_to_new_format,
                ensure_default_roles_new_format
            )
        except ImportError:
            logger.warning("Could not import RBAC migration utilities, skipping v2 migration")
            return
    
    # Migrate existing roles
    migrated_count = migrate_all_roles_to_new_format(conn)
    logger.info(f"Migrated {migrated_count} roles to new permission format")
    
    # Ensure default roles have new format
    ensure_default_roles_new_format(conn)
    
    logger.info("✓ RBAC v2 migration completed")


# ==================== Migration 10: VM Instance Owner ====================

def migrate_vm_instance_owner(conn):
    """
    Add owner_id column to vm_instances for VPS-style user isolation.
    Users with 'user' role will only see their own instances.
    """
    logger.info("Migration 10: Adding VM instance owner support...")
    
    if not table_exists(conn, 'vm_instances'):
        logger.info("Table vm_instances does not exist, skipping owner migration")
        return
    
    # Add owner_id column
    if not column_exists(conn, 'vm_instances', 'owner_id'):
        logger.info("Adding owner_id column to vm_instances...")
        try:
            conn.execute(text(
                """ALTER TABLE vm_instances 
                   ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL"""
            ))
            logger.info("✓ Added owner_id column")
        except Exception as e:
            logger.warning(f"Could not add owner_id column: {e}")
    else:
        logger.info("Column owner_id already exists")
    
    # Create index for owner_id
    try:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_vm_instance_owner ON vm_instances(owner_id)"
        ))
        logger.info("✓ Created index on owner_id")
    except Exception as e:
        logger.warning(f"Could not create owner_id index: {e}")
    
    # Add VPS-style permissions to user role (idempotent: only add missing keys)
    try:
        result = conn.execute(text("SELECT permissions FROM roles WHERE name = 'user'"))
        row = result.fetchone()
        if row:
            import json
            permissions = row[0] if isinstance(row[0], dict) else json.loads(row[0] or '{}')

            vps_permissions = {
                "vms:view:own": True,      # View only own VMs
                "vms:start:own": True,     # Start own VMs
                "vms:stop:own": True,      # Stop own VMs
                "vms:restart:own": True,   # Restart own VMs
                "vms:console:own": True,   # Console to own VMs
                "vms:snapshots:own": True, # Manage snapshots of own VMs
            }

            # Only add keys that are not already present — never overwrite existing values
            missing = {k: v for k, v in vps_permissions.items() if k not in permissions}
            if missing:
                permissions.update(missing)
                conn.execute(
                    text("UPDATE roles SET permissions = :perms, updated_at = NOW() WHERE name = 'user'"),
                    {"perms": json.dumps(permissions)}
                )
                logger.info(f"✓ Added missing VPS permissions to user role: {list(missing.keys())}")
            else:
                logger.info("✓ User role already has VPS instance ownership permissions, skipping")
    except Exception as e:
        logger.warning(f"Could not update user role permissions: {e}")
    
    logger.info("✓ VM instance owner migration completed")


# ==================== Migration 11: Snapshot Archives ====================

def migrate_snapshot_archives(conn):
    """
    Create vm_snapshot_archives table to store snapshot configurations
    before VM/container deletion for audit and recovery purposes.
    """
    logger.info("Migration 11: Creating snapshot archives table...")
    
    if table_exists(conn, 'vm_snapshot_archives'):
        logger.info("Table vm_snapshot_archives already exists, skipping")
        return
    
    conn.execute(text("""
        CREATE TABLE vm_snapshot_archives (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL,
            server_name VARCHAR(100),
            vmid INTEGER NOT NULL,
            vm_name VARCHAR(100),
            vm_type VARCHAR(20) NOT NULL,
            node VARCHAR(100) NOT NULL,
            snapname VARCHAR(100) NOT NULL,
            description TEXT,
            snaptime BIGINT,
            parent VARCHAR(100),
            vmstate BOOLEAN NOT NULL DEFAULT FALSE,
            snapshot_config JSONB,
            deleted_by VARCHAR(100),
            deletion_reason TEXT,
            archived_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        )
    """))
    
    # Create indexes
    conn.execute(text("""
        CREATE INDEX idx_snapshot_archive_server_vmid ON vm_snapshot_archives(server_id, vmid)
    """))
    conn.execute(text("""
        CREATE INDEX idx_snapshot_archive_archived ON vm_snapshot_archives(archived_at)
    """))
    conn.execute(text("""
        CREATE INDEX idx_snapshot_archive_vmid ON vm_snapshot_archives(vmid)
    """))
    
    logger.info("✓ Snapshot archives table created")


# ==================== Migration 12: User SSH Public Key ====================

def migrate_user_ssh_key(conn):
    """Add SSH public key column to users table"""
    
    if column_exists(conn, 'users', 'ssh_public_key'):
        logger.info("✓ User SSH public key column already exists")
        return
    
    logger.info("Adding SSH public key column to users table...")
    
    add_column_if_not_exists(conn, 'users', 'ssh_public_key', 'TEXT')
    
    logger.info("✓ User SSH public key column added")


# ==================== Migration 13: Git Repository URL Setting ====================

def migrate_git_repository_setting(conn):
    """Add git repository URL setting to panel_settings"""
    
    logger.info("Adding git repository URL setting...")
    
    # Check if setting already exists
    result = conn.execute(text(
        "SELECT COUNT(*) FROM panel_settings WHERE key = 'git_repository_url'"
    )).scalar()
    
    if result > 0:
        logger.info("✓ Git repository URL setting already exists")
        return
    
    # Insert default repository setting
    conn.execute(text("""
        INSERT INTO panel_settings (key, value, description, created_at, updated_at)
        VALUES (
            'git_repository_url',
            'https://git.tzim.uz/markmorado/pvemanager',
            'Git repository URL for panel updates',
            NOW(),
            NOW()
        )
    """))
    
    logger.info("✓ Git repository URL setting added")


# ==================== Main Migration Function ====================

def migrate_encrypt_passwords(conn):
    """Migration 14: Encrypt plaintext passwords stored in ProxmoxServer and VMInstance.

    Idempotent: already-encrypted values (valid Fernet tokens) are skipped.
    Skipped entirely when FERNET_KEY env var is not set.
    """
    import os
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        logger.warning("Migration 14: cryptography package not available, skipping")
        return

    fernet_key = os.environ.get("FERNET_KEY")
    if not fernet_key:
        logger.info("Migration 14: FERNET_KEY not set, skipping password encryption")
        return

    f = Fernet(fernet_key.encode())

    def _is_encrypted(value: str) -> bool:
        try:
            f.decrypt(value.encode())
            return True
        except (InvalidToken, Exception):
            return False

    # --- ProxmoxServer.password ---
    rows = conn.execute(
        text("SELECT id, password FROM proxmox_servers WHERE password IS NOT NULL AND password != ''")
    ).fetchall()
    count = 0
    for row in rows:
        if not _is_encrypted(row.password):
            encrypted = f.encrypt(row.password.encode()).decode()
            conn.execute(
                text("UPDATE proxmox_servers SET password = :p WHERE id = :id"),
                {"p": encrypted, "id": row.id}
            )
            count += 1
    if count:
        logger.info(f"Migration 14: Encrypted {count} Proxmox server password(s)")

    # --- VMInstance.cloud_init_password ---
    rows = conn.execute(
        text("SELECT id, cloud_init_password FROM vm_instances WHERE cloud_init_password IS NOT NULL AND cloud_init_password != ''")
    ).fetchall()
    count = 0
    for row in rows:
        if not _is_encrypted(row.cloud_init_password):
            encrypted = f.encrypt(row.cloud_init_password.encode()).decode()
            conn.execute(
                text("UPDATE vm_instances SET cloud_init_password = :p WHERE id = :id"),
                {"p": encrypted, "id": row.id}
            )
            count += 1
    if count:
        logger.info(f"Migration 14: Encrypted {count} VM cloud-init password(s)")


def migrate_backup_jobs(conn):
    """Migration 15: Create backup_jobs table for scheduled backups"""
    logger.info("Migration 15: Creating backup_jobs table...")

    if table_exists(conn, 'backup_jobs'):
        logger.info("Table backup_jobs already exists, skipping")
        return

    conn.execute(text("""
        CREATE TABLE backup_jobs (
            id SERIAL PRIMARY KEY,
            server_id INTEGER NOT NULL REFERENCES proxmox_servers(id) ON DELETE CASCADE,
            node VARCHAR(100) NOT NULL,
            vmids JSONB NOT NULL DEFAULT '[]',
            storage VARCHAR(100) NOT NULL,
            mode VARCHAR(20) NOT NULL DEFAULT 'snapshot',
            compress VARCHAR(20) NOT NULL DEFAULT 'zstd',
            notes VARCHAR(500),
            keep_last INTEGER NOT NULL DEFAULT 3,
            keep_daily INTEGER NOT NULL DEFAULT 7,
            keep_weekly INTEGER NOT NULL DEFAULT 4,
            keep_monthly INTEGER NOT NULL DEFAULT 6,
            cron_expression VARCHAR(100) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            last_run_at TIMESTAMP WITH TIME ZONE,
            last_status VARCHAR(20),
            last_error TEXT,
            last_upid VARCHAR(200)
        )
    """))

    conn.execute(text("CREATE INDEX idx_backup_jobs_server ON backup_jobs(server_id)"))
    conn.execute(text("CREATE INDEX idx_backup_jobs_enabled ON backup_jobs(enabled)"))
    conn.execute(text("CREATE INDEX idx_backup_jobs_owner ON backup_jobs(owner_id)"))

    logger.info("✓ backup_jobs table created")


# ==================== Migration 16: IPAM Network Node ====================

def migrate_ipam_network_node(conn):
    """Add proxmox_node column to ipam_networks table"""
    if not table_exists(conn, 'ipam_networks'):
        logger.info("Table ipam_networks does not exist, skipping")
        return

    if add_column_if_not_exists(conn, 'ipam_networks', 'proxmox_node', 'VARCHAR(100)'):
        logger.info("✓ Added proxmox_node column to ipam_networks")
    else:
        logger.info("✓ ipam_networks.proxmox_node already exists")


def migrate_cluster_support(conn):
    """Migration 17: Add cluster_name column to proxmox_servers table"""
    logger.info("Migration 17: Adding cluster support to proxmox_servers...")

    if add_column_if_not_exists(conn, 'proxmox_servers', 'cluster_name', 'VARCHAR(100)'):
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_proxmox_cluster_name
            ON proxmox_servers(cluster_name)
        """))
        logger.info("✓ Added cluster_name column and index to proxmox_servers")
    else:
        logger.info("✓ proxmox_servers.cluster_name already exists")

    # Ensure admin role has cluster:manage (new format) and remove old proxmox.cluster.manage key
    try:
        result = conn.execute(text("SELECT permissions FROM roles WHERE name = 'admin'")).fetchone()
        if result:
            import json as _json
            perms = _json.loads(result.permissions) if isinstance(result.permissions, str) else result.permissions
            changed = False
            # Remove old dot-format key that triggers full permission reset in Migration 9
            if 'proxmox.cluster.manage' in perms:
                del perms['proxmox.cluster.manage']
                changed = True
            # Add new-format key if missing
            if not perms.get('cluster:manage'):
                perms['cluster:manage'] = True
                changed = True
            if changed:
                conn.execute(
                    text("UPDATE roles SET permissions = :p WHERE name = 'admin'"),
                    {"p": _json.dumps(perms)}
                )
                logger.info("✓ Migrated admin role: proxmox.cluster.manage → cluster:manage")
            else:
                logger.info("✓ Admin role cluster:manage already up to date")
    except Exception as e:
        logger.warning(f"Could not update admin role permissions: {e}")


# ==================== Migration 18: Proxmox Tasks (UPID) ====================

def migrate_proxmox_tasks(conn):
    """Create proxmox_tasks table for tracking long-running Proxmox UPID tasks."""
    logger.info("Migration 18: Creating proxmox_tasks table...")

    if table_exists(conn, 'proxmox_tasks'):
        logger.info("Table proxmox_tasks already exists, skipping")
        return

    conn.execute(text("""
        CREATE TABLE proxmox_tasks (
            id SERIAL PRIMARY KEY,
            upid VARCHAR(200) NOT NULL,
            server_id INTEGER REFERENCES proxmox_servers(id) ON DELETE SET NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            node VARCHAR(100),
            vmid INTEGER,
            vm_type VARCHAR(10),
            action VARCHAR(100) NOT NULL,
            description VARCHAR(500),
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            exit_status VARCHAR(100),
            log_text TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            completed_at TIMESTAMP WITH TIME ZONE
        )
    """))

    conn.execute(text("CREATE INDEX idx_proxmox_tasks_upid ON proxmox_tasks(upid)"))
    conn.execute(text("CREATE INDEX idx_proxmox_tasks_user_id ON proxmox_tasks(user_id)"))
    conn.execute(text("CREATE INDEX idx_proxmox_tasks_status ON proxmox_tasks(status)"))
    conn.execute(text("CREATE INDEX idx_proxmox_tasks_user_status ON proxmox_tasks(user_id, status)"))
    conn.execute(text("CREATE INDEX idx_proxmox_tasks_created ON proxmox_tasks(created_at DESC)"))

    logger.info("✓ proxmox_tasks table created")


def migrate_workspaces(conn):
    """Migration 19: Create workspaces, workspace_servers, workspace_users tables."""
    logger.info("Migration 19: Creating workspaces tables...")

    if table_exists(conn, 'workspaces'):
        logger.info("Table workspaces already exists, skipping")
        return

    # Create workspaces table
    conn.execute(text("""
        CREATE TABLE workspaces (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL UNIQUE,
            description TEXT,
            color VARCHAR(20) DEFAULT '#667eea',
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        )
    """))
    conn.execute(text("CREATE INDEX idx_workspaces_name ON workspaces(name)"))

    # Create workspace_servers junction table
    conn.execute(text("""
        CREATE TABLE workspace_servers (
            id SERIAL PRIMARY KEY,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            server_id INTEGER NOT NULL REFERENCES proxmox_servers(id) ON DELETE CASCADE,
            UNIQUE(workspace_id, server_id)
        )
    """))
    conn.execute(text("CREATE INDEX idx_ws_servers_workspace ON workspace_servers(workspace_id)"))
    conn.execute(text("CREATE INDEX idx_ws_servers_server ON workspace_servers(server_id)"))

    # Create workspace_users junction table
    conn.execute(text("""
        CREATE TABLE workspace_users (
            id SERIAL PRIMARY KEY,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(workspace_id, user_id)
        )
    """))
    conn.execute(text("CREATE INDEX idx_ws_users_workspace ON workspace_users(workspace_id)"))
    conn.execute(text("CREATE INDEX idx_ws_users_user ON workspace_users(user_id)"))

    # Insert default workspace containing all existing servers
    conn.execute(text("""
        INSERT INTO workspaces (name, description, color, is_default)
        VALUES ('Default', 'Default workspace containing all servers', '#667eea', TRUE)
    """))

    # Add all existing servers to the default workspace
    conn.execute(text("""
        INSERT INTO workspace_servers (workspace_id, server_id)
        SELECT w.id, s.id
        FROM workspaces w, proxmox_servers s
        WHERE w.is_default = TRUE
    """))

    logger.info("✓ workspaces tables created, Default workspace populated")


def migrate_user_servers(conn):
    """Migration 20: Create user_servers association table for server-to-user assignments."""
    logger.info("Migration 20: Creating user_servers table...")

    if table_exists(conn, 'user_servers'):
        logger.info("Table user_servers already exists, skipping")
        return

    conn.execute(text("""
        CREATE TABLE user_servers (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            server_id INTEGER NOT NULL REFERENCES proxmox_servers(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, server_id)
        )
    """))
    conn.execute(text("CREATE INDEX idx_user_servers_user ON user_servers(user_id)"))
    conn.execute(text("CREATE INDEX idx_user_servers_server ON user_servers(server_id)"))

    logger.info("✓ user_servers table created")


def migrate_user_role_remove_dashboard(conn):
    """Migration 21: Ensure dashboard:view is False for user role (new format only)."""
    logger.info("Migration 21: Updating user role permissions (dashboard:view = false)...")

    if not table_exists(conn, 'roles'):
        logger.info("Table roles does not exist, skipping")
        return

    result = conn.execute(text("SELECT id, permissions FROM roles WHERE name = 'user'"))
    row = result.fetchone()
    if not row:
        logger.info("Role 'user' not found, skipping")
        return

    import json as _json
    perms = row[1] if isinstance(row[1], dict) else _json.loads(row[1] or '{}')

    changed = False

    # Remove old dot-format key if present (it triggers Migration 9 to "migrate" the role every start)
    if 'dashboard.view' in perms:
        del perms['dashboard.view']
        changed = True

    # Ensure new-format key is False
    if perms.get('dashboard:view') is not False:
        perms['dashboard:view'] = False
        changed = True

    if changed:
        conn.execute(
            text("UPDATE roles SET permissions = :p WHERE name = 'user'"),
            {"p": _json.dumps(perms)}
        )
        logger.info("✓ user role: dashboard:view set to False (old dot-format key removed)")
    else:
        logger.info("✓ user role: dashboard:view already False, skipping")


def migrate_deploy_tasks(conn):
    """Migration 22: Create deploy_tasks table for async VM deployment tracking"""
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS deploy_tasks (
            id SERIAL PRIMARY KEY,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            step VARCHAR(200),
            progress INTEGER NOT NULL DEFAULT 0,
            name VARCHAR(100) NOT NULL,
            template_id INTEGER NOT NULL,
            server_id INTEGER,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vmid INTEGER,
            node VARCHAR(100),
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_deploy_task_user_status
        ON deploy_tasks (user_id, status)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_deploy_tasks_created
        ON deploy_tasks (created_at)
    """))
    logger.info("✓ deploy_tasks table created")


def migrate_deploy_tasks_kind(conn):
    """Migration 23: Add 'kind' column to deploy_tasks (deploy/reinstall/clone/change_password)"""
    # Check if column exists
    result = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'deploy_tasks' AND column_name = 'kind'
    """))
    if result.fetchone():
        logger.info("✓ deploy_tasks.kind already exists")
        return
    conn.execute(text("""
        ALTER TABLE deploy_tasks
        ADD COLUMN kind VARCHAR(30) NOT NULL DEFAULT 'deploy'
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_deploy_tasks_kind
        ON deploy_tasks (kind)
    """))
    # template_id may be NULL for non-deploy tasks (clone/reinstall don't always need it)
    conn.execute(text("""
        ALTER TABLE deploy_tasks ALTER COLUMN template_id DROP NOT NULL
    """))
    logger.info("✓ deploy_tasks.kind column added")


def migrate_deploy_tasks_target_server(conn):
    """Migration 36: Add 'target_server_id' column to deploy_tasks (remote-migrate destination cluster)"""
    add_column_if_not_exists(conn, 'deploy_tasks', 'target_server_id', 'INTEGER')
    logger.info("✓ deploy_tasks.target_server_id column added")


def migrate_user_ssh_keys_table(conn):
    """Migration 24: Create user_ssh_keys table for SSH key library per user."""
    if table_exists(conn, 'user_ssh_keys'):
        logger.info("✓ user_ssh_keys table already exists")
        return

    logger.info("Creating user_ssh_keys table...")
    conn.execute(text("""
        CREATE TABLE user_ssh_keys (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            public_key TEXT NOT NULL,
            private_key VARCHAR(8000),
            fingerprint VARCHAR(100),
            comment VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text("""
        CREATE INDEX idx_user_ssh_keys_user ON user_ssh_keys(user_id)
    """))
    conn.execute(text("""
        CREATE INDEX idx_user_ssh_keys_fp ON user_ssh_keys(fingerprint)
    """))
    logger.info("✓ user_ssh_keys table created")


def migrate_proxmox_task_progress(conn):
    """Migration 25: Add 'progress' column to proxmox_tasks (0-100, parsed from log)."""
    if not table_exists(conn, 'proxmox_tasks'):
        logger.info("Table proxmox_tasks does not exist, skipping progress migration")
        return

    if add_column_if_not_exists(conn, 'proxmox_tasks', 'progress', 'INTEGER'):
        logger.info("✓ Added progress column to proxmox_tasks")
    else:
        logger.info("✓ proxmox_tasks.progress already exists")


def migrate_user_language(conn):
    """Migration 26: Add 'language' column to users table (per-user UI language)."""
    if add_column_if_not_exists(conn, 'users', 'language', "VARCHAR(10) DEFAULT 'ru'"):
        logger.info("✓ Added language column to users")
    else:
        logger.info("✓ users.language already exists")


def migrate_password_reset_tokens(conn):
    """Migration 27: Create password_reset_tokens table for self-service password reset."""
    if table_exists(conn, 'password_reset_tokens'):
        logger.info("✓ password_reset_tokens table already exists")
        return

    logger.info("Creating password_reset_tokens table...")
    conn.execute(text("""
        CREATE TABLE password_reset_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            ip_address VARCHAR(45),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text("""
        CREATE INDEX idx_password_reset_tokens_user ON password_reset_tokens(user_id)
    """))
    conn.execute(text("""
        CREATE INDEX idx_password_reset_tokens_hash ON password_reset_tokens(token_hash)
    """))
    logger.info("✓ password_reset_tokens table created")


def migrate_user_quotas(conn):
    """Migration 28: Create user_quotas table for per-user resource limits.

    No default rows are inserted — absence of a row (or a NULL column) means
    unlimited for that metric.
    """
    if table_exists(conn, 'user_quotas'):
        logger.info("✓ user_quotas table already exists")
        return

    logger.info("Creating user_quotas table...")
    conn.execute(text("""
        CREATE TABLE user_quotas (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            max_instances INTEGER,
            max_cores INTEGER,
            max_memory_mb INTEGER,
            max_disk_gb INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text("CREATE INDEX idx_user_quotas_user ON user_quotas(user_id)"))
    logger.info("✓ user_quotas table created")


def migrate_granular_permissions(conn):
    """
    Migration 29: Backfill granular RBAC permissions.

    Splits umbrella permissions (server:view/manage, vm:view, user:manage) into
    granular ones (firewall:*, network:*, node:*, storage:*, pool:*, access:*)
    for existing roles, and ensures the default roles include the new keys.
    """
    logger.info("Migration 29: Backfilling granular RBAC permissions...")

    if not table_exists(conn, 'roles'):
        logger.info("Table roles does not exist, skipping granular permissions migration")
        return

    try:
        from app.rbac.migration import (
            backfill_granular_permissions,
            ensure_default_roles_new_format,
        )
    except ImportError:
        try:
            from backend.app.rbac.migration import (
                backfill_granular_permissions,
                ensure_default_roles_new_format,
            )
        except ImportError:
            logger.warning("Could not import RBAC utilities, skipping granular permissions migration")
            return

    updated = backfill_granular_permissions(conn)
    logger.info(f"Backfilled granular permissions for {updated} role(s)")

    # Ensure default roles pick up the new granular keys as well
    ensure_default_roles_new_format(conn)

    logger.info("✓ Granular permissions migration completed")


def migrate_two_factor(conn):
    """Migration 30: Two-factor authentication columns on users.

    Ensures the TOTP columns exist (older DBs predating init.sql) and adds the
    ``two_factor_backup_codes`` JSON column for single-use recovery codes.
    """
    added = False
    if add_column_if_not_exists(conn, 'users', 'two_factor_enabled', "BOOLEAN DEFAULT FALSE NOT NULL"):
        added = True
    if add_column_if_not_exists(conn, 'users', 'two_factor_secret', "VARCHAR(100)"):
        added = True
    if add_column_if_not_exists(conn, 'users', 'two_factor_backup_codes', "JSONB"):
        added = True
    if added:
        logger.info("✓ Added two-factor authentication columns to users")
    else:
        logger.info("✓ users two-factor columns already exist")


def migrate_appstore_catalog(conn):
    """Create catalog_apps table for the App Store module (M1)."""
    if table_exists(conn, 'catalog_apps'):
        logger.info("Table catalog_apps already exists, skipping")
        return

    conn.execute(text("""
        CREATE TABLE catalog_apps (
            app_id VARCHAR(100) PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            version VARCHAR(50),
            tipi_version INTEGER,
            categories JSONB NOT NULL DEFAULT '[]'::jsonb,
            short_desc TEXT,
            description_md TEXT,
            logo_path VARCHAR(300),
            port INTEGER,
            form_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
            compose_yaml TEXT,
            architectures JSONB NOT NULL DEFAULT '[]'::jsonb,
            available BOOLEAN NOT NULL DEFAULT TRUE,
            unavailable_reason VARCHAR(200),
            deprecated BOOLEAN NOT NULL DEFAULT FALSE,
            dynamic_config BOOLEAN NOT NULL DEFAULT FALSE,
            source_url VARCHAR(500),
            author VARCHAR(200),
            synced_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        )
    """))
    conn.execute(text("CREATE INDEX idx_catalog_apps_available ON catalog_apps (available)"))
    conn.execute(text("CREATE INDEX idx_catalog_apps_name ON catalog_apps (name)"))
    logger.info("✓ App Store catalog_apps table created")


def migrate_appstore_catalog_source(conn):
    """Add source column to catalog_apps (мультиисточник: runtipi | umbrel)."""
    if not table_exists(conn, 'catalog_apps'):
        return
    if column_exists(conn, 'catalog_apps', 'source'):
        return
    conn.execute(text(
        "ALTER TABLE catalog_apps ADD COLUMN source VARCHAR(50) NOT NULL DEFAULT 'runtipi'"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_catalog_apps_source ON catalog_apps (source)"
    ))
    logger.info("✓ App Store catalog_apps.source column added")


def migrate_appstore_catalog_data_files(conn):
    """Add data_files column to catalog_apps (файлы приложения для bind-mount'ов)."""
    if not table_exists(conn, 'catalog_apps'):
        return
    if column_exists(conn, 'catalog_apps', 'data_files'):
        return
    conn.execute(text("ALTER TABLE catalog_apps ADD COLUMN data_files JSONB"))
    logger.info("✓ App Store catalog_apps.data_files column added")


def migrate_appstore_default_credentials(conn):
    """Add default_credentials column to catalog_apps (учётные данные из манифеста)."""
    if not table_exists(conn, 'catalog_apps'):
        return
    if column_exists(conn, 'catalog_apps', 'default_credentials'):
        return
    conn.execute(text("ALTER TABLE catalog_apps ADD COLUMN default_credentials JSONB"))
    logger.info("✓ App Store catalog_apps.default_credentials column added")


def migrate_server_error_kind(conn):
    """Add last_error_kind column to proxmox_servers ('auth' | 'connection')."""
    if not table_exists(conn, 'proxmox_servers'):
        return
    if add_column_if_not_exists(conn, 'proxmox_servers', 'last_error_kind', 'VARCHAR(20)'):
        logger.info("✓ proxmox_servers.last_error_kind column added")


def pick_backfill_owner(candidates):
    """Pick the owner for one instance from creation evidence, or None.

    ``candidates`` is an iterable of ``(server_id, user_id)`` pairs already
    filtered to the instance's VMID. A ``server_id`` of ``None`` means the
    source did not record one (deploy_tasks does not set it for LXC), so the
    pair is treated as applicable to any server.

    Ownership grants access, so a guess is worse than leaving the row alone:
    the owner is returned only when every applicable pair names the same user.
    """
    users = {user_id for _, user_id in candidates if user_id is not None}
    return users.pop() if len(users) == 1 else None


def migrate_backfill_instance_owner(conn):
    """
    Backfill vm_instances.owner_id for instances that were created through the
    panel but ended up ownerless.

    Until save_vm_instance() learned to set the owner when updating an existing
    row, every deploy lost it: the cache sync registers a VM with
    owner_id = NULL within ~10s of it appearing on Proxmox — while the clone is
    still running — so the deploy's own save minutes later always took the
    update path, which ignored owner_id.

    The creator is recovered from two sources, in order of confidence:
      1. deploy_tasks — user_id is a real foreign key stored when the deploy was
         queued (server_id is absent for LXC deploys, so it may be NULL)
      2. audit_logs — 'vm_create' / 'ct_create' / 'lxc_create' entries, which
         only carry a username and so are matched by name

    A row is updated only when the evidence names exactly one existing user.
    Ambiguous and unmatched instances keep owner_id = NULL for an operator to
    assign deliberately with backend/assign_instances_owner.py. Instances that
    already have an owner are never touched, which also makes the migration
    idempotent.

    Caveat: this recovers who *created* the instance, not necessarily who it was
    meant for. A VM an admin created on behalf of a user is attributed to the
    admin, because before the fix the intended owner was never persisted
    anywhere. Those need the CLI script to be corrected.
    """
    if not table_exists(conn, 'vm_instances'):
        return
    if not column_exists(conn, 'vm_instances', 'owner_id'):
        logger.info("vm_instances.owner_id missing, skipping owner backfill")
        return

    orphans = conn.execute(text(
        "SELECT id, server_id, vmid, name FROM vm_instances "
        "WHERE owner_id IS NULL AND deleted_at IS NULL"
    )).fetchall()
    if not orphans:
        logger.info("✓ No ownerless instances to backfill")
        return

    logger.info(f"Migration 41: backfilling owner for {len(orphans)} ownerless instance(s)...")

    known_user_ids = {row[0] for row in conn.execute(text("SELECT id FROM users"))}
    user_id_by_name = {
        row[1]: row[0]
        for row in conn.execute(text("SELECT id, username FROM users"))
        if row[1]
    }

    # vmid -> [(server_id, user_id)], strongest source first
    from_deploys: dict = {}
    from_logs: dict = {}

    if table_exists(conn, 'deploy_tasks'):
        for user_id, server_id, vmid in conn.execute(text(
            "SELECT user_id, server_id, vmid FROM deploy_tasks WHERE vmid IS NOT NULL"
        )):
            if user_id in known_user_ids:
                from_deploys.setdefault(vmid, []).append((server_id, user_id))

    if table_exists(conn, 'audit_logs'):
        for username, server_id, resource_id, success in conn.execute(text(
            "SELECT username, server_id, resource_id, success FROM audit_logs "
            "WHERE action IN ('vm_create', 'ct_create', 'lxc_create', 'container_create') "
            "AND username IS NOT NULL AND resource_id IS NOT NULL"
        )):
            if not success:
                continue
            user_id = user_id_by_name.get(username)
            if user_id is None:
                continue
            try:
                vmid = int(resource_id)
            except (TypeError, ValueError):
                continue
            from_logs.setdefault(vmid, []).append((server_id, user_id))

    assigned = 0
    ambiguous = 0
    unmatched = 0

    for inst_id, server_id, vmid, name in orphans:
        owner_id = None
        had_evidence = False
        for source in (from_deploys, from_logs):
            applicable = [
                (sid, uid) for sid, uid in source.get(vmid, [])
                if sid is None or sid == server_id
            ]
            if not applicable:
                continue
            had_evidence = True
            owner_id = pick_backfill_owner(applicable)
            if owner_id is not None:
                break

        if owner_id is None:
            # Evidence that names several users (a reused VMID, most likely) is
            # not usable, but it is worth distinguishing from no evidence at all.
            if had_evidence:
                ambiguous += 1
            else:
                unmatched += 1
            continue

        conn.execute(
            text("UPDATE vm_instances SET owner_id = :owner_id "
                 "WHERE id = :id AND owner_id IS NULL"),
            {"owner_id": owner_id, "id": inst_id},
        )
        assigned += 1
        logger.info(f"  VM {vmid} ({name}) -> user_id={owner_id}")

    logger.info(
        f"✓ Instance owner backfill: {assigned} assigned, "
        f"{ambiguous} ambiguous, {unmatched} without evidence"
    )
    if ambiguous or unmatched:
        logger.info(
            "  Remaining instances stay ownerless on purpose — assign them with "
            "backend/assign_instances_owner.py"
        )


def migrate_script_repo_metadata_format(conn):
    """Add metadata_format column to script_git_repos (header-comment | community-scripts-ct)."""
    if not table_exists(conn, 'script_git_repos'):
        return
    if add_column_if_not_exists(conn, 'script_git_repos', 'metadata_format',
                                 "VARCHAR(30) NOT NULL DEFAULT 'header-comment'"):
        logger.info("✓ Added metadata_format column to script_git_repos")


def migrate_appstore_installed(conn):
    """Create installed_apps and app_operations tables for the App Engine (M2)."""
    if not table_exists(conn, 'installed_apps'):
        conn.execute(text("""
            CREATE TABLE installed_apps (
                id SERIAL PRIMARY KEY,
                app_id VARCHAR(100),
                server_id INTEGER,
                vmid INTEGER,
                node VARCHAR(100),
                name VARCHAR(200) NOT NULL,
                version_installed VARCHAR(50),
                tipi_version_installed INTEGER,
                env_encrypted TEXT,
                ip VARCHAR(64),
                port INTEGER,
                status VARCHAR(20) NOT NULL DEFAULT 'installing',
                update_available BOOLEAN NOT NULL DEFAULT FALSE,
                last_snapshot VARCHAR(200),
                owner_id INTEGER,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            )
        """))
        conn.execute(text("CREATE INDEX idx_installed_apps_status ON installed_apps (status)"))
        conn.execute(text("CREATE INDEX idx_installed_apps_owner ON installed_apps (owner_id)"))
        conn.execute(text("CREATE INDEX idx_installed_apps_server_vmid ON installed_apps (server_id, vmid)"))
        logger.info("✓ App Store installed_apps table created")

    if not table_exists(conn, 'app_operations'):
        conn.execute(text("""
            CREATE TABLE app_operations (
                id SERIAL PRIMARY KEY,
                installed_app_id INTEGER REFERENCES installed_apps(id) ON DELETE CASCADE,
                type VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'running',
                progress INTEGER NOT NULL DEFAULT 0,
                steps_log JSONB NOT NULL DEFAULT '[]'::jsonb,
                error_text TEXT,
                user_id INTEGER,
                started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
                finished_at TIMESTAMP WITH TIME ZONE
            )
        """))
        conn.execute(text("CREATE INDEX idx_app_operations_installed ON app_operations (installed_app_id)"))
        logger.info("✓ App Store app_operations table created")

    # M4: версия, зафиксированная в pre-update снапшоте (для отката версии в БД)
    add_column_if_not_exists(conn, 'installed_apps', 'snapshot_version', 'VARCHAR(50)')
    add_column_if_not_exists(conn, 'installed_apps', 'snapshot_tipi_version', 'INTEGER')
    # Параметры создания CT для корректного повтора установки (retry)
    add_column_if_not_exists(conn, 'installed_apps', 'install_params', 'JSONB')


def migrate_ipam_network_workspace(conn):
    """Migration 33: Bind IPAM networks to a workspace + default-network flag.

    Adds ipam_networks.workspace_id (FK workspaces) and ipam_networks.is_default.
    Backfills workspace_id from the network's proxmox_server_id via workspace_servers
    (each server belongs to a workspace), so existing server-bound networks land in
    the right workspace automatically.
    """
    if not table_exists(conn, 'ipam_networks'):
        logger.info("Table ipam_networks does not exist, skipping workspace migration")
        return

    added = False
    if add_column_if_not_exists(conn, 'ipam_networks', 'workspace_id', 'INTEGER REFERENCES workspaces(id) ON DELETE SET NULL'):
        added = True
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_ipam_network_workspace ON ipam_networks(workspace_id)"
        ))
    if add_column_if_not_exists(conn, 'ipam_networks', 'is_default', 'BOOLEAN NOT NULL DEFAULT FALSE'):
        added = True

    # Backfill workspace_id from server binding (only for rows not yet assigned).
    if table_exists(conn, 'workspace_servers'):
        try:
            conn.execute(text("""
                UPDATE ipam_networks n
                SET workspace_id = ws.workspace_id
                FROM workspace_servers ws
                WHERE n.workspace_id IS NULL
                  AND n.proxmox_server_id IS NOT NULL
                  AND ws.server_id = n.proxmox_server_id
            """))
        except Exception as e:
            logger.debug(f"IPAM workspace backfill: {e}")

    if added:
        logger.info("✓ Added workspace_id/is_default to ipam_networks")
    else:
        logger.info("✓ ipam_networks.workspace_id/is_default already exist")


def migrate_app_operations_server_id(conn):
    """Migration 37: app_operations.server_id (per-server golden-template tracking).

    Нужен только для операций без installed_app_id (golden_template) — чтобы
    прогресс/история сборки золотого шаблона различались по серверам Proxmox,
    а не показывали последнюю сборку глобально независимо от того, для
    какого сервера она запускалась.
    """
    if not table_exists(conn, 'app_operations'):
        logger.info("Table app_operations does not exist, skipping server_id migration")
        return
    if add_column_if_not_exists(
        conn, 'app_operations', 'server_id',
        'INTEGER REFERENCES proxmox_servers(id) ON DELETE CASCADE'
    ):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_app_operations_server ON app_operations(server_id)"
        ))
        logger.info("✓ Added app_operations.server_id")
    else:
        logger.info("✓ app_operations.server_id already exists")


def migrate_ipam_multi_ip(conn):
    """Migration 43: несколько IP-адресов на одного гостя.

    Раньше на (server_id, vmid) подразумевалась ровно одна аллокация. Теперь
    их может быть несколько: один основной (net0/ipconfig0, попадает в колонку
    IP) и произвольное число алиасов, навешенных внутри гостя. Колонки
    apply_* хранят состояние применения алиаса к живому гостю.
    """
    if not table_exists(conn, 'ipam_allocations'):
        logger.info("Table ipam_allocations does not exist, skipping multi-IP migration")
        return

    added = False
    for column, ddl in (
        ('is_primary', 'BOOLEAN NOT NULL DEFAULT FALSE'),
        ('assignment_kind', "VARCHAR(10) NOT NULL DEFAULT 'primary'"),
        ('target_interface', 'VARCHAR(20)'),
        ('apply_status', 'VARCHAR(20)'),
        ('apply_error', 'TEXT'),
        ('applied_at', 'TIMESTAMP WITH TIME ZONE'),
    ):
        if add_column_if_not_exists(conn, 'ipam_allocations', column, ddl):
            added = True

    if added:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_ipam_alloc_primary "
            "ON ipam_allocations(proxmox_server_id, proxmox_vmid, is_primary)"
        ))
        # Всё, что существовало до этой миграции, было единственным адресом
        # своего гостя — значит основным. Записи без привязки к гостю
        # (ручные брони) основными не считаем.
        conn.execute(text("""
            UPDATE ipam_allocations
            SET is_primary = TRUE, assignment_kind = 'primary'
            WHERE proxmox_vmid IS NOT NULL AND is_primary = FALSE
        """))
        logger.info("✓ Added multi-IP columns to ipam_allocations")
    else:
        logger.info("✓ ipam_allocations multi-IP columns already exist")


def migrate_setting_view_panel_only(conn):
    """Migration 42: drop setting:view where it was only granting the profile page.

    `setting:view` used to gate the whole /settings page, which mixed panel-wide
    configuration with a user's own account (password, 2FA, SSH keys,
    notification preferences). The account half now lives at /profile and needs
    no permission, so roles that were given setting:view just to let their users
    reach their own settings would silently gain the panel settings page.

    Only roles that clearly were not meant to administer the panel are touched:
    those holding setting:view without setting:update or setting:manage. Runs
    once — a marker in panel_settings keeps a later admin grant from being
    reverted on the next startup.
    """
    if not table_exists(conn, 'roles') or not table_exists(conn, 'panel_settings'):
        logger.info("Tables roles/panel_settings missing, skipping setting:view split")
        return

    marker = 'migration_setting_view_panel_only'
    done = conn.execute(
        text("SELECT 1 FROM panel_settings WHERE key = :k"), {"k": marker}
    ).fetchone()
    if done:
        logger.info("✓ setting:view split already applied, skipping")
        return

    import json as _json
    rows = conn.execute(text("SELECT id, name, permissions FROM roles")).fetchall()
    changed = []

    for role_id, role_name, permissions in rows:
        perms = permissions if isinstance(permissions, dict) else _json.loads(permissions or '{}')
        if not perms.get('setting:view'):
            continue
        if perms.get('setting:update') or perms.get('setting:manage'):
            continue  # a real panel administrator — leave the grant alone

        del perms['setting:view']
        conn.execute(
            text("UPDATE roles SET permissions = :p WHERE id = :id"),
            {"p": _json.dumps(perms), "id": role_id},
        )
        changed.append(role_name)

    conn.execute(
        text("""
            INSERT INTO panel_settings (key, value, description)
            VALUES (:k, 'done', 'setting:view no longer gates a user''s own profile page')
            ON CONFLICT (key) DO NOTHING
        """),
        {"k": marker},
    )

    if changed:
        logger.info(f"✓ Removed panel-only setting:view from role(s): {', '.join(changed)}")
    else:
        logger.info("✓ No roles needed the setting:view split")


def migrate_encrypt_two_factor_secret(conn):
    """Migration 44: store users.two_factor_secret encrypted (Fernet).

    A plaintext TOTP seed in a DB dump or backup lets anyone generate valid
    codes and skip the second factor. The column is widened first — a Fernet
    token of a 32-char base32 seed is ~140 chars. Idempotent; the encryption
    step is skipped when FERNET_KEY is not set (the model then stores
    plaintext, as before).
    """
    import os
    if not column_exists(conn, 'users', 'two_factor_secret'):
        return
    conn.execute(text("ALTER TABLE users ALTER COLUMN two_factor_secret TYPE VARCHAR(255)"))

    fernet_key = os.environ.get("FERNET_KEY")
    if not fernet_key:
        return
    from cryptography.fernet import Fernet, InvalidToken
    f = Fernet(fernet_key.encode())
    rows = conn.execute(text(
        "SELECT id, two_factor_secret FROM users "
        "WHERE two_factor_secret IS NOT NULL AND two_factor_secret != ''"
    )).fetchall()
    count = 0
    for row in rows:
        try:
            f.decrypt(row.two_factor_secret.encode())
            continue  # already encrypted
        except (InvalidToken, Exception):
            pass
        conn.execute(
            text("UPDATE users SET two_factor_secret = :s WHERE id = :id"),
            {"s": f.encrypt(row.two_factor_secret.encode()).decode(), "id": row.id},
        )
        count += 1
    if count:
        logger.info(f"Migration 44: Encrypted {count} two-factor secret(s)")


def migrate_hot_path_indexes(conn):
    """Migration 45: indexes for per-user task polling.

    /api/all-tasks and the active-task badge poll every few seconds per
    logged-in user and filter these tables by user_id; without an index each
    poll scans the whole table.
    """
    for table, name, cols in (
        ("app_operations", "idx_app_operations_user", "user_id, started_at"),
        ("proxmox_tasks", "idx_proxmox_tasks_user_created", "user_id, created_at"),
        ("deploy_tasks", "idx_deploy_tasks_user_created", "user_id, created_at"),
        ("script_executions", "idx_script_executions_user_started", "user_id, started_at"),
    ):
        if table_exists(conn, table):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})"))


def migrate_encrypt_notification_secrets(conn):
    """Migration 46: encrypt the SMTP password and Telegram bot token at rest.

    They sat in panel_settings in plaintext while server passwords and TOTP
    seeds are Fernet-encrypted. Idempotent; skipped without FERNET_KEY.
    """
    import os
    fernet_key = os.environ.get("FERNET_KEY")
    if not fernet_key or not table_exists(conn, 'panel_settings'):
        return
    from cryptography.fernet import Fernet, InvalidToken
    f = Fernet(fernet_key.encode())
    rows = conn.execute(text(
        "SELECT id, value FROM panel_settings "
        "WHERE key IN ('smtp_password', 'telegram_bot_token') AND value IS NOT NULL AND value != ''"
    )).fetchall()
    count = 0
    for row in rows:
        try:
            f.decrypt(row.value.encode())
            continue  # already encrypted
        except (InvalidToken, Exception):
            pass
        conn.execute(text("UPDATE panel_settings SET value = :v WHERE id = :id"),
                     {"v": f.encrypt(row.value.encode()).decode(), "id": row.id})
        count += 1
    if count:
        logger.info(f"Migration 46: Encrypted {count} notification secret(s)")


def run_all_migrations(engine, db_session=None):
    """
    Run all migrations in order.
    
    Args:
        engine: SQLAlchemy engine
        db_session: Optional database session (for compatibility)
    """
    logger.info("=" * 50)
    logger.info("Running PVEmanager database migrations...")
    logger.info("=" * 50)
    
    try:
        with engine.connect() as conn:
            # Migration 1: Notifications
            try:
                migrate_notifications(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Notifications migration: {e}")
                conn.rollback()
            
            # Migration 2: RBAC & Security
            try:
                migrate_rbac_security(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"RBAC migration: {e}")
                conn.rollback()
            
            # Migration 3: Cross-node Templates
            try:
                migrate_cross_node_templates(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Cross-node templates migration: {e}")
                conn.rollback()
            
            # Migration 4: VM Cache
            try:
                migrate_vm_cache(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"VM cache migration: {e}")
                conn.rollback()
            
            # Migration 5: Task Queue
            try:
                migrate_task_queue(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Task queue migration: {e}")
                conn.rollback()
            
            # Migration 6: OS Template Icons (theme-adaptive)
            try:
                migrate_os_template_icons(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"OS template icons migration: {e}")
                conn.rollback()
            
            # Migration 7: VM Template Name
            try:
                migrate_vm_template_name(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"VM template name migration: {e}")
                conn.rollback()
            
            # Migration 8: Enhanced Audit Logs
            try:
                migrate_enhanced_audit_logs(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Enhanced audit logs migration: {e}")
                conn.rollback()
            
            # Migration 9: RBAC v2 - New Permission Format
            try:
                migrate_rbac_v2(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"RBAC v2 migration: {e}")
                conn.rollback()
            
            # Migration 10: VM Instance Owner (VPS-style user isolation)
            try:
                migrate_vm_instance_owner(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"VM instance owner migration: {e}")
                conn.rollback()
            
            # Migration 11: Snapshot Archives
            try:
                migrate_snapshot_archives(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Snapshot archives migration: {e}")
                conn.rollback()
            
            # Migration 12: User SSH Public Key
            try:
                migrate_user_ssh_key(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"User SSH key migration: {e}")
                conn.rollback()
            
            # Migration 13: Git Repository URL Setting
            try:
                migrate_git_repository_setting(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Git repository setting migration: {e}")
                conn.rollback()

            # Migration 14: Encrypt plaintext passwords (idempotent, skipped if FERNET_KEY not set)
            try:
                migrate_encrypt_passwords(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Password encryption migration: {e}")
                conn.rollback()

            # Migration 15: Backup Jobs
            try:
                migrate_backup_jobs(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Backup jobs migration: {e}")
                conn.rollback()

            # Migration 16: IPAM Network Node
            try:
                migrate_ipam_network_node(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"IPAM network node migration: {e}")
                conn.rollback()

            # Migration 17: Cluster Support (cluster_name column)
            try:
                migrate_cluster_support(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Cluster support migration: {e}")
                conn.rollback()

            # Migration 18: Proxmox Tasks (UPID tracking)
            try:
                migrate_proxmox_tasks(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Proxmox tasks migration: {e}")
                conn.rollback()

            # Migration 19: Workspaces
            try:
                migrate_workspaces(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Workspaces migration: {e}")
                conn.rollback()

            # Migration 20: User-Server assignments
            try:
                migrate_user_servers(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"User-Server assignments migration: {e}")
                conn.rollback()

            # Migration 21: Update user role - disable dashboard.view
            try:
                migrate_user_role_remove_dashboard(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"User role dashboard migration: {e}")
                conn.rollback()

            # Migration 22: Deploy Tasks table
            try:
                migrate_deploy_tasks(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Deploy tasks migration: {e}")
                conn.rollback()

            # Migration 23: deploy_tasks.kind column
            try:
                migrate_deploy_tasks_kind(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Deploy tasks kind migration: {e}")
                conn.rollback()

            # Migration 24: user_ssh_keys table
            try:
                migrate_user_ssh_keys_table(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"User SSH keys table migration: {e}")
                conn.rollback()

            # Migration 25: proxmox_tasks.progress column
            try:
                migrate_proxmox_task_progress(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Proxmox task progress migration: {e}")
                conn.rollback()

            # Migration 26: users.language column
            try:
                migrate_user_language(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"User language migration: {e}")
                conn.rollback()

            # Migration 27: password_reset_tokens table
            try:
                migrate_password_reset_tokens(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Password reset tokens migration: {e}")
                conn.rollback()

            # Migration 28: user_quotas table
            try:
                migrate_user_quotas(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"User quotas migration: {e}")
                conn.rollback()

            # Migration 29: Backfill granular RBAC permissions
            try:
                migrate_granular_permissions(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Granular permissions migration: {e}")
                conn.rollback()

            # Migration 30: Two-factor authentication columns
            try:
                migrate_two_factor(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Two-factor migration: {e}")
                conn.rollback()

            # Migration 31: App Store catalog
            try:
                migrate_appstore_catalog(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"App Store catalog migration: {e}")
                conn.rollback()

            # Migration 32: App Store installed apps + operations
            try:
                migrate_appstore_installed(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"App Store installed migration: {e}")
                conn.rollback()

            # Migration 33: IPAM network workspace binding + default flag
            try:
                migrate_ipam_network_workspace(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"IPAM network workspace migration: {e}")
                conn.rollback()

            # Migration 34: App Store catalog source column (runtipi | umbrel)
            try:
                migrate_appstore_catalog_source(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"App Store catalog source migration: {e}")
                conn.rollback()

            # Migration 35: script_git_repos metadata_format column (header-comment | community-scripts-ct)
            try:
                migrate_script_repo_metadata_format(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Script repo metadata_format migration: {e}")
                conn.rollback()

            # Migration 36: deploy_tasks.target_server_id column (remote-migrate destination cluster)
            try:
                migrate_deploy_tasks_target_server(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Deploy tasks target_server_id migration: {e}")
                conn.rollback()

            # Migration 37: app_operations.server_id column (per-server golden template)
            try:
                migrate_app_operations_server_id(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"App operations server_id migration: {e}")
                conn.rollback()

            # Migration 38: catalog_apps.data_files column (файлы приложения из репозитория)
            try:
                migrate_appstore_catalog_data_files(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"App Store catalog data_files migration: {e}")
                conn.rollback()

            # Migration 39: catalog_apps.default_credentials column (учётные данные из манифеста)
            try:
                migrate_appstore_default_credentials(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"App Store default_credentials migration: {e}")
                conn.rollback()

            # Migration 40: proxmox_servers.last_error_kind ('auth' | 'connection')
            try:
                migrate_server_error_kind(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Server last_error_kind migration: {e}")
                conn.rollback()

            # Migration 41: backfill vm_instances.owner_id for ownerless instances
            try:
                migrate_backfill_instance_owner(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Instance owner backfill migration: {e}")
                conn.rollback()

            # Migration 42: setting:view now covers panel settings only
            try:
                migrate_setting_view_panel_only(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"setting:view split migration: {e}")
                conn.rollback()

            # Migration 43: several IP addresses per guest
            try:
                migrate_ipam_multi_ip(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"IPAM multi-IP migration: {e}")
                conn.rollback()

            # Migration 44: encrypt users.two_factor_secret at rest
            try:
                migrate_encrypt_two_factor_secret(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Two-factor secret encryption migration: {e}")
                conn.rollback()

            # Migration 45: indexes for per-user task polling
            try:
                migrate_hot_path_indexes(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Hot-path index migration: {e}")
                conn.rollback()

            # Migration 46: encrypt SMTP password / Telegram token at rest
            try:
                migrate_encrypt_notification_secrets(conn)
                conn.commit()
            except Exception as e:
                logger.warning(f"Notification secrets encryption migration: {e}")
                conn.rollback()

        logger.info("=" * 50)
        logger.info("✓ All migrations completed successfully")
        logger.info("=" * 50)
        return True
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


# ==================== CLI Entry Point ====================

if __name__ == "__main__":
    print("Running PVEmanager migrations...")
    
    try:
        from app.db import engine
    except ImportError:
        from backend.app.db import engine
    
    success = run_all_migrations(engine)
    
    if success:
        print("✓ All migrations completed successfully")
    else:
        print("✗ Migration failed")
        exit(1)
