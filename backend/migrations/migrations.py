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
            "roles.view": True,
            "roles.manage": True,
            "notifications.manage": True,
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
        }
    },
    {
        "name": "user",
        "display_name": "User",
        "description": "VPS-style user: can only access their own instances",
        "is_system": True,
        "permissions": {
            "dashboard.view": True,
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
    
    # Update user role with instance ownership permissions
    try:
        result = conn.execute(text("SELECT permissions FROM roles WHERE name = 'user'"))
        row = result.fetchone()
        if row:
            import json
            permissions = row[0] if isinstance(row[0], dict) else json.loads(row[0] or '{}')
            
            # Add VPS-style permissions for user role
            permissions.update({
                "vms:view:own": True,      # View only own VMs
                "vms:start:own": True,     # Start own VMs
                "vms:stop:own": True,      # Stop own VMs
                "vms:restart:own": True,   # Restart own VMs
                "vms:console:own": True,   # Console to own VMs
                "vms:snapshots:own": True, # Manage snapshots of own VMs
            })
            
            conn.execute(
                text("UPDATE roles SET permissions = :perms, updated_at = NOW() WHERE name = 'user'"),
                {"perms": json.dumps(permissions)}
            )
            logger.info("✓ Updated user role with instance ownership permissions")
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


# ==================== Main Migration Function ====================

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
