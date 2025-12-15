-- ==============================================================================
-- PVEmanager Database Schema
-- ==============================================================================
-- Complete database initialization for fresh installations.
-- This file is executed on first PostgreSQL startup via Docker volume mount.
-- 
-- Version: 1.0
-- Last Updated: 2025-12-15
-- ==============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE serverpanel TO serverpanel;

-- ==============================================================================
-- USERS AND AUTHENTICATION
-- ==============================================================================

-- Roles table (RBAC v2 with resource:action format)
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    description TEXT,
    permissions JSONB NOT NULL DEFAULT '{}',
    is_system BOOLEAN DEFAULT FALSE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_roles_name ON roles(name);
CREATE INDEX IF NOT EXISTS idx_roles_active ON roles(is_active);

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    is_admin BOOLEAN DEFAULT FALSE NOT NULL,
    role_id INTEGER REFERENCES roles(id),
    failed_login_attempts INTEGER DEFAULT 0 NOT NULL,
    locked_until TIMESTAMP WITH TIME ZONE,
    last_password_change TIMESTAMP WITH TIME ZONE,
    require_password_change BOOLEAN DEFAULT FALSE NOT NULL,
    two_factor_enabled BOOLEAN DEFAULT FALSE NOT NULL,
    two_factor_secret VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    last_login TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);

-- Active sessions table
CREATE TABLE IF NOT EXISTS active_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token VARCHAR(64) UNIQUE NOT NULL,
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    device_info VARCHAR(200),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_activity TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    is_active BOOLEAN DEFAULT TRUE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON active_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON active_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON active_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON active_sessions(is_active);

-- Login attempts table
CREATE TABLE IF NOT EXISTS login_attempts (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100),
    ip_address VARCHAR(45) NOT NULL,
    user_agent VARCHAR(500),
    success BOOLEAN DEFAULT FALSE NOT NULL,
    failure_reason VARCHAR(200),
    attempted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address);
CREATE INDEX IF NOT EXISTS idx_login_attempts_time ON login_attempts(attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_username ON login_attempts(username);

-- Blocked IPs table
CREATE TABLE IF NOT EXISTS blocked_ips (
    id SERIAL PRIMARY KEY,
    ip_address VARCHAR(45) UNIQUE NOT NULL,
    reason VARCHAR(500),
    blocked_by VARCHAR(100),
    blocked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE,
    is_permanent BOOLEAN DEFAULT FALSE NOT NULL,
    attempts_count INTEGER DEFAULT 0 NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blocked_ips_ip ON blocked_ips(ip_address);
CREATE INDEX IF NOT EXISTS idx_blocked_ips_expires ON blocked_ips(expires_at);

-- Security settings table
CREATE TABLE IF NOT EXISTS security_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value VARCHAR(500) NOT NULL,
    description TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_security_settings_key ON security_settings(key);

-- ==============================================================================
-- NOTIFICATIONS
-- ==============================================================================

-- Notifications table
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
    read_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_level ON notifications(level, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);

-- Notification preferences table
CREATE TABLE IF NOT EXISTS notification_preferences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE UNIQUE,
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
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notification_preferences_user ON notification_preferences(user_id);

-- ==============================================================================
-- PROXMOX SERVERS
-- ==============================================================================

-- Proxmox servers table
CREATE TABLE IF NOT EXISTS proxmox_servers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    hostname VARCHAR(255) NOT NULL,
    ip_address VARCHAR(50) NOT NULL,
    port INTEGER NOT NULL DEFAULT 8006,
    api_user VARCHAR(100) NOT NULL DEFAULT 'root@pam',
    api_token_name VARCHAR(100),
    api_token_value VARCHAR(255),
    use_password BOOLEAN NOT NULL DEFAULT FALSE,
    password VARCHAR(255),
    verify_ssl BOOLEAN NOT NULL DEFAULT FALSE,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    last_check TIMESTAMP WITH TIME ZONE,
    is_online BOOLEAN DEFAULT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_proxmox_name ON proxmox_servers(name);
CREATE INDEX IF NOT EXISTS idx_proxmox_hostname ON proxmox_servers(hostname);
CREATE INDEX IF NOT EXISTS idx_proxmox_ip ON proxmox_servers(ip_address);
CREATE INDEX IF NOT EXISTS idx_proxmox_status ON proxmox_servers(is_online, last_check);

-- ==============================================================================
-- OS TEMPLATES
-- ==============================================================================

-- OS template groups table
CREATE TABLE IF NOT EXISTS os_template_groups (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    icon VARCHAR(200),
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_os_template_groups_name ON os_template_groups(name);
CREATE INDEX IF NOT EXISTS idx_os_template_groups_active ON os_template_groups(is_active);

-- OS templates table
CREATE TABLE IF NOT EXISTS os_templates (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL,
    server_id INTEGER NOT NULL,
    name VARCHAR(100) NOT NULL,
    vmid INTEGER NOT NULL,
    node VARCHAR(100),
    source_node VARCHAR(100),
    replicated_nodes JSONB DEFAULT '{}',
    default_cores INTEGER NOT NULL DEFAULT 1,
    default_memory INTEGER NOT NULL DEFAULT 1024,
    default_disk INTEGER NOT NULL DEFAULT 10,
    min_cores INTEGER NOT NULL DEFAULT 1,
    min_memory INTEGER NOT NULL DEFAULT 512,
    min_disk INTEGER NOT NULL DEFAULT 5,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_os_template_group ON os_templates(group_id);
CREATE INDEX IF NOT EXISTS idx_os_template_server ON os_templates(server_id);
CREATE INDEX IF NOT EXISTS idx_os_template_vmid ON os_templates(server_id, vmid);
CREATE INDEX IF NOT EXISTS idx_os_template_active ON os_templates(is_active);

-- ==============================================================================
-- VM INSTANCES
-- ==============================================================================

-- VM instances table (cache)
CREATE TABLE IF NOT EXISTS vm_instances (
    id SERIAL PRIMARY KEY,
    server_id INTEGER NOT NULL,
    vmid INTEGER NOT NULL,
    node VARCHAR(100) NOT NULL,
    vm_type VARCHAR(20) NOT NULL,
    name VARCHAR(100) NOT NULL,
    status VARCHAR(20) DEFAULT 'unknown',
    is_template BOOLEAN DEFAULT FALSE,
    cores INTEGER,
    memory BIGINT,
    disk_size BIGINT,
    os_type VARCHAR(50),
    ip_address VARCHAR(50),
    ip_prefix INTEGER DEFAULT 24,
    gateway VARCHAR(50),
    nameserver VARCHAR(50),
    cloud_init_user VARCHAR(100),
    cloud_init_password VARCHAR(255),
    ssh_keys TEXT,
    template_id INTEGER,
    template_name VARCHAR(100),
    description TEXT,
    tags VARCHAR(255),
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    last_sync_at TIMESTAMP WITH TIME ZONE,
    extra_config JSONB
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vm_instance_server_vmid ON vm_instances(server_id, vmid);
CREATE INDEX IF NOT EXISTS idx_vm_instance_active ON vm_instances(server_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_vm_instance_status ON vm_instances(status);
CREATE INDEX IF NOT EXISTS idx_vm_instance_type ON vm_instances(vm_type);
CREATE INDEX IF NOT EXISTS idx_vm_instance_owner ON vm_instances(owner_id);

-- ==============================================================================
-- IPAM (IP Address Management)
-- ==============================================================================

-- IPAM networks table
CREATE TABLE IF NOT EXISTS ipam_networks (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    network VARCHAR(18) NOT NULL,
    gateway VARCHAR(45),
    vlan_id INTEGER,
    dns_primary VARCHAR(45),
    dns_secondary VARCHAR(45),
    dns_domain VARCHAR(255),
    proxmox_server_id INTEGER,
    proxmox_bridge VARCHAR(20),
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ipam_network_name ON ipam_networks(name);
CREATE INDEX IF NOT EXISTS idx_ipam_network_network ON ipam_networks(network);
CREATE INDEX IF NOT EXISTS idx_ipam_network_active ON ipam_networks(is_active);

-- IPAM pools table
CREATE TABLE IF NOT EXISTS ipam_pools (
    id SERIAL PRIMARY KEY,
    network_id INTEGER NOT NULL,
    name VARCHAR(100) NOT NULL,
    pool_type VARCHAR(20) DEFAULT 'static' NOT NULL,
    range_start VARCHAR(45) NOT NULL,
    range_end VARCHAR(45) NOT NULL,
    auto_assign BOOLEAN DEFAULT TRUE NOT NULL,
    description TEXT,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ipam_pool_network ON ipam_pools(network_id);
CREATE INDEX IF NOT EXISTS idx_ipam_pool_type ON ipam_pools(pool_type);

-- IPAM allocations table
CREATE TABLE IF NOT EXISTS ipam_allocations (
    id SERIAL PRIMARY KEY,
    network_id INTEGER NOT NULL,
    pool_id INTEGER,
    ip_address VARCHAR(45) NOT NULL UNIQUE,
    mac_address VARCHAR(17),
    resource_type VARCHAR(20),
    resource_id INTEGER,
    resource_name VARCHAR(100),
    proxmox_server_id INTEGER,
    proxmox_vmid INTEGER,
    proxmox_node VARCHAR(100),
    status VARCHAR(20) DEFAULT 'allocated' NOT NULL,
    allocation_type VARCHAR(20) DEFAULT 'static' NOT NULL,
    hostname VARCHAR(255),
    fqdn VARCHAR(255),
    dns_ptr_record BOOLEAN DEFAULT FALSE NOT NULL,
    allocated_by VARCHAR(100),
    allocated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE,
    last_seen TIMESTAMP WITH TIME ZONE,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ipam_alloc_ip ON ipam_allocations(ip_address);
CREATE INDEX IF NOT EXISTS idx_ipam_alloc_network ON ipam_allocations(network_id);
CREATE INDEX IF NOT EXISTS idx_ipam_alloc_resource ON ipam_allocations(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_ipam_alloc_proxmox ON ipam_allocations(proxmox_server_id, proxmox_vmid);
CREATE INDEX IF NOT EXISTS idx_ipam_alloc_status ON ipam_allocations(status);
CREATE INDEX IF NOT EXISTS idx_ipam_alloc_hostname ON ipam_allocations(hostname);

-- IPAM history table
CREATE TABLE IF NOT EXISTS ipam_history (
    id SERIAL PRIMARY KEY,
    ip_address VARCHAR(45) NOT NULL,
    network_id INTEGER,
    action VARCHAR(50) NOT NULL,
    old_value JSONB,
    new_value JSONB,
    resource_type VARCHAR(20),
    resource_id INTEGER,
    resource_name VARCHAR(100),
    performed_by VARCHAR(100),
    performed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_ipam_history_ip ON ipam_history(ip_address);
CREATE INDEX IF NOT EXISTS idx_ipam_history_action ON ipam_history(action);
CREATE INDEX IF NOT EXISTS idx_ipam_history_date ON ipam_history(performed_at);

-- ==============================================================================
-- AUDIT LOGS
-- ==============================================================================

-- Audit logs table
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    level VARCHAR(20) NOT NULL,
    category VARCHAR(50) NOT NULL,
    action VARCHAR(100) NOT NULL,
    message TEXT NOT NULL,
    request_id VARCHAR(36),
    session_id VARCHAR(64),
    user_id INTEGER,
    username VARCHAR(100),
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),
    geo_location VARCHAR(100),
    resource_type VARCHAR(50),
    resource_id VARCHAR(100),
    resource_name VARCHAR(200),
    server_id INTEGER,
    server_name VARCHAR(100),
    node_name VARCHAR(100),
    details JSONB,
    request_body JSONB,
    response_body JSONB,
    request_method VARCHAR(10),
    request_path VARCHAR(500),
    query_params VARCHAR(1000),
    response_status INTEGER,
    duration_ms INTEGER,
    success BOOLEAN DEFAULT TRUE NOT NULL,
    error_message TEXT,
    error_traceback TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_level ON audit_logs(level);
CREATE INDEX IF NOT EXISTS idx_audit_category ON audit_logs(category);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_level_category ON audit_logs(level, category);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON audit_logs(username, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_logs(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_request_id ON audit_logs(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_ip ON audit_logs(ip_address);
CREATE INDEX IF NOT EXISTS idx_audit_server ON audit_logs(server_id);
CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_logs(user_id);

-- ==============================================================================
-- TASK QUEUE
-- ==============================================================================

-- Task queue table (for bulk operations)
CREATE TABLE IF NOT EXISTS task_queue (
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
);

CREATE INDEX IF NOT EXISTS idx_task_queue_type ON task_queue(task_type);
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_queue_user ON task_queue(user_id);
CREATE INDEX IF NOT EXISTS idx_task_status_created ON task_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_user_status ON task_queue(user_id, status);

-- ==============================================================================
-- PANEL SETTINGS
-- ==============================================================================

-- Panel settings table
CREATE TABLE IF NOT EXISTS panel_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value VARCHAR(500) NOT NULL,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_panel_settings_key ON panel_settings(key);

-- ==============================================================================
-- DEFAULT DATA
-- ==============================================================================

-- Default roles (RBAC v2 format with resource:action permissions)
INSERT INTO roles (name, display_name, description, permissions, is_system, is_active) VALUES
('admin', 'Administrator', 'Full access to all features', '{
    "dashboard:view": true,
    "server:view": true, "server:create": true, "server:update": true, "server:delete": true, "server:manage": true,
    "vm:view": true, "vm:create": true, "vm:update": true, "vm:delete": true, "vm:start": true, "vm:stop": true, "vm:restart": true, "vm:console": true, "vm:migrate": true, "vm:execute": true,
    "lxc:view": true, "lxc:create": true, "lxc:update": true, "lxc:delete": true, "lxc:start": true, "lxc:stop": true, "lxc:restart": true, "lxc:console": true, "lxc:migrate": true,
    "template:view": true, "template:create": true, "template:update": true, "template:delete": true, "template:manage": true,
    "storage:view": true, "storage:manage": true,
    "backup:view": true, "backup:create": true, "backup:delete": true, "backup:manage": true,
    "ipam:view": true, "ipam:manage": true,
    "user:view": true, "user:create": true, "user:update": true, "user:delete": true,
    "role:view": true, "role:create": true, "role:update": true, "role:delete": true, "role:manage": true,
    "log:view": true, "log:export": true, "log:delete": true,
    "setting:view": true, "setting:update": true, "setting:manage": true,
    "notification:view": true, "notification:manage": true
}', TRUE, TRUE),
('moderator', 'Moderator', 'Can manage VMs and view logs', '{
    "dashboard:view": true,
    "server:view": true,
    "vm:view": true, "vm:create": true, "vm:start": true, "vm:stop": true, "vm:restart": true, "vm:console": true,
    "lxc:view": true, "lxc:create": true, "lxc:start": true, "lxc:stop": true, "lxc:restart": true, "lxc:console": true,
    "template:view": true,
    "storage:view": true,
    "backup:view": true, "backup:create": true,
    "ipam:view": true,
    "user:view": true,
    "log:view": true, "log:export": true,
    "setting:view": true,
    "notification:view": true, "notification:manage": true
}', TRUE, TRUE),
('user', 'User', 'Standard user with limited access', '{
    "dashboard:view": true,
    "server:view": true,
    "vm:view": true, "vm:start": true, "vm:stop": true, "vm:restart": true, "vm:console": true,
    "lxc:view": true, "lxc:start": true, "lxc:stop": true, "lxc:restart": true, "lxc:console": true,
    "template:view": true,
    "storage:view": true,
    "ipam:view": true,
    "setting:view": true,
    "notification:view": true, "notification:manage": true
}', TRUE, TRUE),
('demo', 'Demo User', 'Read-only access for demonstration', '{
    "dashboard:view": true,
    "server:view": true,
    "vm:view": true,
    "lxc:view": true,
    "template:view": true,
    "storage:view": true,
    "ipam:view": true
}', TRUE, TRUE)
ON CONFLICT (name) DO NOTHING;

-- Default security settings
INSERT INTO security_settings (key, value, description) VALUES
('max_login_attempts', '5', 'Maximum failed login attempts before lockout'),
('lockout_duration_minutes', '30', 'Account lockout duration in minutes'),
('session_timeout_minutes', '60', 'Session timeout in minutes'),
('single_session_enabled', 'false', 'Allow only one active session per user'),
('ip_block_threshold', '10', 'Failed attempts before IP is blocked'),
('ip_block_duration_minutes', '60', 'IP block duration in minutes'),
('password_min_length', '8', 'Minimum password length'),
('password_require_uppercase', 'true', 'Require uppercase letters in password'),
('password_require_lowercase', 'true', 'Require lowercase letters in password'),
('password_require_numbers', 'true', 'Require numbers in password'),
('password_require_special', 'false', 'Require special characters in password'),
('password_expiry_days', '0', 'Password expiry in days (0 = never)'),
('api_rate_limit_per_minute', '60', 'API rate limit per minute per user')
ON CONFLICT (key) DO NOTHING;

-- Default OS template groups with theme-adaptive icons
INSERT INTO os_template_groups (name, icon, description, sort_order) VALUES
('Ubuntu', '<i class="fa-brands fa-ubuntu os-icon os-icon-ubuntu"></i>', 'Ubuntu Linux', 1),
('Debian', '<i class="fa-brands fa-debian os-icon os-icon-debian"></i>', 'Debian Linux', 2),
('CentOS', '<i class="fa-brands fa-centos os-icon os-icon-centos"></i>', 'CentOS Linux', 3),
('Rocky Linux', '<span class="os-icon-rocky-svg"></span>', 'Rocky Linux', 4),
('AlmaLinux', '<span class="os-icon-alma-svg"></span>', 'AlmaLinux', 5),
('Fedora', '<i class="fa-brands fa-fedora os-icon os-icon-fedora"></i>', 'Fedora Linux', 6),
('RHEL', '<i class="fa-brands fa-redhat os-icon os-icon-rhel"></i>', 'Red Hat Enterprise Linux', 7),
('Oracle Linux', '<i class="fa-brands fa-linux os-icon os-icon-oracle"></i>', 'Oracle Linux', 8),
('openSUSE', '<i class="fa-brands fa-suse os-icon os-icon-suse"></i>', 'openSUSE Linux', 9),
('Arch Linux', '<i class="fa-brands fa-linux os-icon os-icon-arch"></i>', 'Arch Linux', 10),
('Gentoo', '<i class="fa-brands fa-linux os-icon os-icon-gentoo"></i>', 'Gentoo Linux', 11),
('Alpine', '<i class="fa-brands fa-linux os-icon os-icon-alpine"></i>', 'Alpine Linux', 12),
('Windows', '<i class="fa-brands fa-windows os-icon os-icon-windows"></i>', 'Microsoft Windows', 13),
('FreeBSD', '<i class="fa-brands fa-freebsd os-icon os-icon-freebsd"></i>', 'FreeBSD', 14),
('OpenBSD', '<i class="fa-brands fa-linux os-icon os-icon-openbsd"></i>', 'OpenBSD', 15),
('Other', '<i class="fa-solid fa-compact-disc os-icon os-icon-other"></i>', 'Other operating systems', 99)
ON CONFLICT DO NOTHING;

-- ==============================================================================
-- END OF SCHEMA
-- ==============================================================================
