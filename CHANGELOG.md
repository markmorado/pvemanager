# Changelog

All notable changes to PVEmanager will be documented in this file.

---

## [v1.0] - 2025-12-15

### 🎉 Initial Production Release

Production-ready version with complete feature set for Proxmox management.

---

## [v2.1.4] - 2025-12-13 (Development)

### 🔄 System Updates Feature

#### New Features
- **Online System Updates**:
  - Check for updates from Settings → Panel → System Updates
  - View changelog of new version before updating
  - One-click update with automatic git pull and docker rebuild
  - Update banner displayed during system update
  - Automatic page reload after update completion

- **Update Service** (`update_service.py`):
  - `check_for_updates()` - Compare local and remote versions via git
  - `perform_update()` - Execute git pull + docker compose rebuild
  - Automatic safe.directory configuration for mounted projects

#### API Endpoints
- `GET /settings/api/version` - Get current panel version
- `GET /settings/api/updates/check` - Check for available updates
- `GET /settings/api/updates/status` - Get update status
- `POST /settings/api/updates/perform` - Perform system update

#### Requirements
- Project must be mounted as volume: `.:/project`
- Docker socket must be accessible: `/var/run/docker.sock`
- Git repository with remote origin configured

---

## [v2.1.3] - 2025-12-13

### 🐛 Bug Fixes

#### Dashboard Node Charts
- **Fixed duplicate node charts in dashboard**:
  - Added deduplication by IP + node name to prevent duplicate charts for cluster nodes
  - Fixed display name logic: "data" no longer shows as "data - data"
  - Removed duplicate `/api/{server_id}/nodes` endpoint
  - Proper handling of server names that already contain node suffix

---

## [v2.1.2] - 2025-12-13

### 📸 Snapshot Operation Queue

#### New Features
- **Snapshot Operation Queue System**:
  - Sequential execution of snapshot operations (create, delete, rollback)
  - Proxmox task status polling via UPID for accurate completion tracking
  - Visual queue indicator with current operation and pending count
  - Auto-refresh snapshot list after queue completion
  - 2-minute timeout with graceful error handling

- **Snapshot Archival on VM Deletion**:
  - Snapshots are archived to database before VM/container deletion
  - New `VMSnapshotArchive` model stores snapshot configurations
  - API endpoints for viewing archived snapshots: `GET /api/snapshot-archives`
  - Audit trail for deleted snapshots with timestamp and user info

#### UI Improvements
- Responsive snapshots tab with adaptive grid (1/2/3 columns)
- Minimalist status icons (● running, ○ stopped)
- Pulse animation for queue indicator
- Mobile-friendly snapshot cards

#### Bug Fixes
- Fixed regex pattern warning in snapshot name input
- Fixed container snapshots using wrong API path (`/lxc/` → `/container/`)
- Fixed snapshot cards touching container edges

---

## [v2.1.1] - 2025-12-13

### 🐛 Bug Fixes

#### IPAM IP Release Issues Fixed
- **Fixed IPAM not releasing IPs after instance deletion**:
  - Added status filter to `find_allocation_by_resource()` — now only finds active allocations
  - Soft delete of VM instances now releases IPAM allocations
  - Deleting Proxmox server now releases all IPAM allocations for that server
  - Added `release_all_by_server()` method to IPAMService for bulk release

---

## [v2.1.0] - 2025-01-XX

### 🖥️ VPS-Style User Isolation

#### New Features
- **Instance Ownership** — VPS-style user isolation:
  - Each VM/LXC instance can have an assigned owner (user)
  - Users with `user` role can only see and manage their own instances
  - Admins/moderators can see and manage all instances
  - `owner_id` field added to `vm_instances` table

- **Permission-Based Access Control**:
  - New permissions: `vms:view:own`, `vms:start:own`, `vms:stop:own`, `vms:restart:own`, `vms:console:own`, `vms:snapshots:own`
  - `user` role updated to VPS-style permissions by default
  - All VM/LXC operations now check ownership for limited users

### 🌐 SDN (Software Defined Networking)

#### New Features
- **SDN Zone Management**:
  - List, create, delete SDN zones
  - Support for Simple, VLAN, VXLAN zone types
  - API endpoints: `GET/POST/DELETE /api/servers/{server_id}/sdn/zones`

- **SDN VNet Management**:
  - List, create, delete Virtual Networks
  - Support for VLAN tags and aliases
  - API endpoints: `GET/POST/DELETE /api/servers/{server_id}/sdn/vnets`

- **SDN Subnet Management**:
  - Create and manage subnets within VNets
  - Gateway and SNAT configuration
  - API endpoint: `GET/POST /api/servers/{server_id}/sdn/vnets/{vnet}/subnets`

- **Apply SDN Changes**:
  - `POST /api/servers/{server_id}/sdn/apply` — Apply pending SDN configuration

### 📸 Snapshots

#### New Features
- **VM Snapshots**:
  - List all snapshots for a VM
  - Create snapshot with optional RAM state
  - Delete snapshots
  - Rollback to any snapshot
  - API endpoints: `GET/POST/DELETE /api/{server_id}/vm/{vmid}/snapshots`

- **Container Snapshots**:
  - List all snapshots for a container
  - Create and delete snapshots
  - Rollback to any snapshot
  - API endpoints: `GET/POST/DELETE /api/{server_id}/container/{vmid}/snapshots`

### 🔧 HA Cluster Improvements

#### New Features
- **Dynamic HA Option Visibility**:
  - HA option only shown in creation form when server is in cluster mode
  - API endpoint: `GET /api/servers/{server_id}/cluster-info`
  - Frontend checks cluster status on server selection

#### Database Changes
- **Migration 10: VM Instance Owner**:
  - Adds `owner_id` column to `vm_instances` table
  - Creates index for owner lookup
  - Updates `user` role with ownership permissions

---

## [v2.0.0] - 2025-12-13

### 🔐 RBAC v2 — New Permission System

#### New Features
- **Atomic Permission Model** — New `resource:action[:scope]` format:
  - Resources: `vm`, `lxc`, `server`, `template`, `storage`, `backup`, `ipam`, `user`, `role`, `log`, `setting`, `notification`
  - Actions: `view`, `create`, `update`, `delete`, `start`, `stop`, `restart`, `console`, `migrate`, `manage`, `export`, `execute`
  - Scopes: `global`, `org`, `workspace`, `instance` (future extensibility)
  - Examples: `vm:view`, `server:create`, `log:export`

- **Permission Engine** — Central authorization logic:
  - `PermissionEngine.has_permission(user, permission)` — Check user permissions
  - `PermissionEngine.check_permission()` — Raise 403 if denied
  - Wildcard support: `vm:*` grants all VM permissions
  - Implied permissions: `manage` implies `view`, `update`, `start`, `stop`, `restart`

- **Permission Registry** — Centralized permission definitions:
  - 40+ atomic permissions defined in `permissions.py`
  - Category grouping (Dashboard, Virtual Machines, Containers, etc.)
  - Dependency tracking (`requires` field)

- **Authorization Middleware** — FastAPI dependencies:
  - `PermissionChecker("vm:create")` — Single permission check
  - `ScopedPermissionChecker("vm:view", resource_param="vm_id")` — Resource-level
  - `ResourceAccessChecker` — Future organization/workspace support

- **Permission Decorators** — Function decorators:
  - `@requires_permission("vm:create")` — Require specific permission
  - `@requires_any_permission(["vm:create", "lxc:create"])` — Require any
  - `@requires_all_permissions(["vm:view", "vm:update"])` — Require all

- **RBAC Audit Service** — Logs all permission changes:
  - Role created/updated/deleted events
  - Role assignments to users
  - Permission denied events

- **Legacy Compatibility** — Full backwards compatibility:
  - 33 legacy-to-new permission mappings
  - Old format (`vms.view`) automatically resolved to new (`vm:view`)
  - Migration script converts existing roles

#### Database Changes
- **Migration 9: RBAC v2** — Automatic role migration:
  - Converts all roles from `resource.action` to `resource:action` format
  - Updates default roles with new permission codes
  - Idempotent — safe to run multiple times

#### New API Endpoints
- `GET /api/permissions/v2` — Get permissions in new format grouped by category

#### Files Added
- `backend/app/rbac/__init__.py` — Module exports
- `backend/app/rbac/permissions.py` — Permission definitions and registry
- `backend/app/rbac/engine.py` — Authorization logic
- `backend/app/rbac/middleware.py` — FastAPI dependencies
- `backend/app/rbac/decorators.py` — Function decorators
- `backend/app/rbac/audit.py` — Audit logging
- `backend/app/rbac/migration.py` — Migration utilities

### 📦 Database Consolidation

- **Unified init.sql** — Complete database schema in single file:
  - All 17 tables with proper indexes
  - Default roles with RBAC v2 permissions
  - Default security settings
  - Default OS template groups with icons
  - No more incremental migrations for fresh installs

### 🧹 Codebase Cleanup

- Removed empty `backend/app/api/v1/` directory
- Removed `__pycache__` directories
- Updated `.gitignore` patterns

---

## [v1.9.0] - 2025-12-13

### 🚀 New Features

#### Bulk Operations
- **Mass VM/Container operations** — Select multiple VMs and perform actions:
  - Bulk Start — Start all selected VMs/containers
  - Bulk Stop — Stop all selected VMs/containers
  - Bulk Restart — Restart all selected VMs/containers
  - Bulk Shutdown — Graceful shutdown
  - Bulk Delete — Delete multiple VMs at once
- **Task Queue System** — Background task processing:
  - Tasks processed sequentially to avoid overload
  - Real-time progress tracking
  - Success/failure status for each item
  - User can view their task history

#### Security Enhancements
- **IP Blocking** — Automatic and manual IP blocking:
  - Auto-block after configurable failed login attempts
  - Temporary or permanent blocks
  - Block duration configurable in settings
- **Session Management** — Active session tracking:
  - View all active sessions
  - Device and browser info
  - IP address tracking
  - Single session mode (optional)
- **Login Protection** — Enhanced authentication security:
  - Login attempt logging
  - Failed attempt counter per user
  - Account lockout after threshold
  - Configurable lockout duration
- **Security Settings** — Configurable security parameters:
  - Max login attempts before lockout
  - Lockout duration
  - Session timeout
  - IP block threshold
  - Password requirements (length, complexity)

### 🎨 UI/UX Improvements

#### OS Template Icons
- **Theme-adaptive icons** — OS icons now properly adapt to dark/light theme:
  - Ubuntu, Debian, CentOS, Rocky Linux, AlmaLinux, Fedora
  - RHEL, Oracle Linux, openSUSE, Arch, Gentoo, Alpine
  - Windows, FreeBSD, OpenBSD
  - Uses CSS classes for color adaptation
  - FontAwesome + custom SVG icons

#### VM Cache Improvements
- **Template name caching** — Template name now cached in `vm_instances`
- **Faster VM list loading** — All data loaded from cache

### 🔧 Technical

#### Unified Migrations
- All 7 migrations consolidated into single `migrations.py`:
  - Migration 1: Notifications system
  - Migration 2: RBAC and Security
  - Migration 3: Cross-node Template Support
  - Migration 4: VM Cache
  - Migration 5: Task Queue
  - Migration 6: OS Template Icons
  - Migration 7: VM Template Name

#### New Services
- `task_queue_service.py` — Task queue management and processing
- `security_service.py` — Security operations, IP blocking, sessions

#### New Database Tables
- `task_queue` — Bulk operation tasks
- `active_sessions` — User session tracking
- `login_attempts` — Login attempt logging
- `blocked_ips` — IP block list
- `security_settings` — Security configuration

#### New Columns
- `vm_instances.template_name` — Cached template name
- `os_template_groups.icon` — Extended to VARCHAR(200) for HTML icons

---

## [v1.8.2] - 2025-12-13

### 📚 Documentation Improvements

- **API Examples** — Added comprehensive API usage examples in `API_EXAMPLES.md`
- **README Update** — Updated documentation links and version information
- **WIKI Enhancement** — Improved API reference section with detailed endpoints table
- **Version Consistency** — Ensured all documentation files reflect current version v1.8.1

### 🐛 Bug Fixes

- **Documentation Links** — Fixed broken links in README.md and WIKI.md

### 🚀 Performance Improvements

#### VM Cache System
- **Database-cached VM list** — Virtual Machines page now loads instantly:
  - VM data synced to database every 30 seconds via background worker
  - Page loads from cache instead of Proxmox API calls
  - Cluster deduplication by VMID (prevents duplicates from shared storage)
  - Status, OS type, and IP addresses cached and displayed

- **Background Monitoring Worker** — APScheduler-based sync:
  - Runs every 30 seconds in background thread
  - Syncs all VMs/LXCs from all Proxmox servers
  - Handles cluster environments properly (by vmid, not server+vmid)

### 🐛 Bug Fixes

#### IPAM Integration
- **Fixed IP not showing for cached VMs** — IPAM lookup now uses multiple fallbacks:
  - Lookup by (server_id, vmid) 
  - Fallback to (None, vmid)
  - Fallback by VM name
  
- **Fixed IPAM IPs not releasing on delete** — Now uses `resource_id` fallback:
  - VMs and LXCs created before IPAM fields were added now release IPs properly

#### LXC Container Creation
- **Fixed container creation failing** — Network config now uses correct field:
  - Changed `net.cidr` to `net.network` for IP configuration
  - No more `/undefined` errors in network setup

### 🔧 Technical

- **Unified migrations** — All 4 migration files consolidated into single `migrations.py`:
  - 001_add_notifications
  - 002_rbac_security  
  - 003_template_cross_node
  - 004_vm_cache
  - Cleaner startup code in main.py

- **New database fields** in `vm_instances` table:
  - `status` — Current VM status (running/stopped/etc)
  - `is_template` — Template flag
  - `os_type` — Operating system type
  - `last_sync_at` — Last sync timestamp

---

## [v1.8.0] - 2025-12-11

### 🎉 Rebranding
- **Renamed to PVEmanager** — New name reflecting Proxmox VE focus

### 🚀 New Features

#### Smart LXC Container Creation
- **Cross-node template support** — Create containers from templates on any cluster node:
  - Templates loaded from ALL cluster nodes at once
  - Automatic detection of shared vs local storage
  - Template location shown in selector: `(shared)` or `(pve1)`, `(pve2)` etc.
  
- **Auto-migration for local templates** — Seamless cross-node deployment:
  - If template is on local storage of different node → create there, migrate to target
  - If template is on shared storage → create directly on target node
  - Migration happens automatically and transparently

#### Session Improvements
- **Extended token lifetime** — Increased from 30 min to 8 hours (configurable via `ACCESS_TOKEN_EXPIRE_MINUTES`)
- **No more unexpected logouts** during active work sessions

#### VM/LXC Creation Fixes
- **Disk size now applied correctly** — Fixed issue where disk size was not applied during VM creation
- **Added disk resize support for cloned LXC** — Containers can be resized after cloning

### 🐛 Bug Fixes

- **Fixed deleted instances remaining in list** — Now properly removed from both `allVMs` and `filteredVMs` arrays
- **Fixed HA-managed VM deletion** — VMs under Proxmox HA can now be deleted (auto-removed from HA first)
- **Fixed delete_vm returning 500 on success** — Now properly returns success status
- **Fixed toast notifications not showing** — Rewrote with inline styles for reliability

### 🔧 Technical

- New API endpoint: `GET /api/{server_id}/all-lxc-templates` — Get templates from all cluster nodes
- New API endpoint: `POST /api/{server_id}/create-lxc-smart` — Smart LXC creation with auto-migration
- Added `migrate_container()` method to ProxmoxClient
- Added `get_all_lxc_templates()` method to ProxmoxClient with shared storage detection

---

## [v1.7.0] - 2025-12-11

### 🚀 New Features

#### Cross-Node Template Deployment
- **Automatic template replication** - Deploy VMs from templates to any cluster node:
  - Templates can be deployed to any node in Proxmox cluster
  - Automatic replication of template to target node if not present
  - Replicated templates tracked in database (`replicated_nodes` JSON field)
  - Target node selector in Deploy VM modal

- **Cluster support** - Full support for Proxmox clusters:
  - Adding one cluster node auto-discovers all cluster nodes
  - Node status indicators (online/offline) in selectors
  - Cross-node cloning with automatic template conversion

### 🎨 UI/UX Improvements

#### Added
- **Custom Dialog System** - Complete replacement for native browser dialogs:
  - `showAlert()` - informational messages with icons
  - `showConfirm()` - confirmations with Promise-based API
  - `showPrompt()` - text input with validation
  - `showDeleteConfirm()` - dangerous actions with red theme
  - `showToast()` - notifications with backward compatibility
  - CSS animations (fade, slide, shake)
  - Dark/light theme support

- **SMTP/Telegram settings in UI** - Moved from .env:
  - SMTP configuration directly in settings panel
  - Telegram bot token and Chat ID
  - Test notifications without restart

- **Target Node Selector** - Choose deployment node:
  - Available in OS Templates deploy modal
  - Available in Proxmox Server detail create VM form
  - Auto-select (template node) option

#### Changed
- **Instance page title** - Now displays:
  - `VM 12345` for virtual machines
  - `LXC 12345` for containers
  - Instead of generic `VM/Container`

- **Removed branding** - Removed CloudPro/TZIM mentions:
  - Version moved to VERSION file
  - Neutral UI

#### Fixed
- **resizeDisk()** - Fixed disk resize button:
  - Removed escaped `\$` in onclick handler
  - Migrated to Promise-based `showConfirm()` API

- **Dark theme** - Fixed text colors:
  - IPAM modal windows and tables
  - Dynamically generated elements
  - `color: var(--text-primary)` everywhere

- **LXC templates** - Correct template list loading

- **All confirm dialogs** - Migrated to Promise-based API:
  - os_templates.html, ipam_dashboard.html, ipam_networks.html
  - ipam_allocations.html, proxmox_vms.html, proxmox_server_detail.html
  - instance_detail.html, users.html

### 🔧 Backend Improvements

#### Added
- **Cross-node template methods** in ProxmoxClient:
  - `clone_template_to_node()` - Clone template between nodes
  - `convert_to_template()` - Convert VM to template
  - `replicate_template_to_node()` - Full replication workflow
  - `find_template_on_nodes()` - Check template availability

- **IPAM Auto-release** - Automatic IP release on deletion:
  - `release_ip_by_vmid()` in IPAMService
  - Integration in `delete_vm()` and `delete_container()`
  - Logging to IPAM history

- **Permission checks** - Permission validation in API endpoints

- **Migration 003** - Cross-node template support:
  - `source_node` column in os_templates
  - `replicated_nodes` JSON column
  - `node` column made nullable

#### Fixed
- **SMTP SSL/TLS** - Correct connection logic:
  - Port 465 → SSL
  - Port 587 → STARTTLS
  - Порт 587 → STARTTLS

---

## [v1.6.1] - 2025-11-27

### 🔧 Bug Fixes & Improvements

#### Fixed
- **SMTP SSL/STARTTLS** - Fixed SSL selection logic for email:
  - Port 465 → SSL (SMTP_SSL)
  - Port 587 → STARTTLS
  - Added 10 second timeout
- **Test notification button** - Fixed "Test Email" button:
  - Added loading indicator
  - Added undefined response check
  - Improved result display
- **Translations** - Added "testing" key for loading indicator

#### Documentation
- **WIKI.md** - Created unified documentation file with:
  - Complete installation guide
  - Notification setup (Email, Telegram)
  - Troubleshooting section
  - FAQ
- **README.md** - Shortened and optimized
- **Cleanup** - Removed duplicate files:
  - NOTIFICATION_QUICKSTART.md
  - QUICKSTART.md
  - TESTING_I18N.md
  - install_notifications.sh
  - backend/run_migration.py

---

## [v1.6.0] - 2025-11-27

### 🔔 Notification System

#### Added
- **Email notifications** - SMTP integration:
  - Support for Yandex, Gmail, Mail.ru
  - HTML email templates
  - Configuration in UI
- **Telegram notifications** - Bot API:
  - Bot creation via @BotFather
  - Chat ID verification
  - Formatted messages
- **Notification settings tab** - Settings tab:
  - Enable/disable channels
  - "Critical only" filter
  - Quiet hours
  - Test buttons
- **Real-time notification system** - Full infrastructure:
  - Bell icon with unread badge
  - Notification Center dropdown
  - Toast notifications (auto-dismiss 5s)
  - Filters: All/Unread/Critical
- **Background monitoring worker**:
  - VM status (every 30 sec)
  - Resource alerts: CPU >80%, RAM >85%, Disk >90% (every 60 sec)
  - Cleanup expired (every 6 hours)
- **Notification types**:
  - `vm_status` - VM state changes
  - `resource_alert` - resource alerts
  - `system` - system events
- **Notification levels**: critical 🔴, warning 🟠, info 🔵, success 🟢
- **10 REST API endpoints** for notification management
- **APScheduler** integration for background tasks
- **Timezone support** - Asia/Tashkent (UTC+5)

#### Technical
- `backend/app/services/notification_channels.py` - Email and Telegram channels
- `backend/app/services/notification_service.py` - Business logic
- `backend/app/workers/monitoring_worker.py` - Background monitoring
- `backend/app/api/notifications.py` - REST API
- `backend/app/models/notification.py` - SQLAlchemy models
- Added `tzdata` to Dockerfile for timezone
- Automatic DB migration on startup

#### Dependencies
- `apscheduler==3.10.4`
- `httpx` for Telegram API

---

## [v1.5.0] - 2025-11-27

### 🎨 Design Improvements

#### Added
- **Modern gradient design** - Purple-blue accents
- **Material Design icons** - SVG icons instead of emoji
- **Enhanced shadows** - Multi-layer shadows with glow
- **Smooth animations**:
  - Card hover (translateY + scale)
  - Button ripple effect
  - Pulse on online indicators
- **Backdrop filters** - blur 20px on sidebar/header
- **Gradient typography** - Gradient text for headings

---

## [v1.4.0] - 2025-11-26

### 🌐 Internationalization (i18n)

#### Added
- **Bilingual support** - Russian and English
- **250+ translation keys**
- **Language switching** in settings
- **JavaScript `t()` function** for client-side translations

#### Localized
- ✅ Authentication
- ✅ Dashboard
- ✅ Proxmox VE
- ✅ OS Templates
- ✅ IPAM
- ✅ Settings
- ✅ All modals and forms

---

## [v1.3.0] - 2025-11-25

### VM Configuration Management

- Automatic configuration saving to DB
- Random VMID (10000-19999)
- VMID preservation on reinstall
- IP restoration after reinstall
- Soft delete with history

---

## [v1.2.0] - 2025-11-24

### OS Templates System

- Template groups management
- Quick deployment from templates
- VM reinstallation with VMID preservation
- Cloud-init integration
- Proxmox template scanning

---

## [v1.1.0] - 2025-11-23

### IPAM (IP Address Management)

- Network management (CIDR, VLAN)
- IP pool management
- Auto IP allocation
- Status tracking (available, allocated, reserved)

---

## [v1.0.0] - 2025-11-20

### Initial Release

- Proxmox VE integration
- VM/LXC management
- Real-time monitoring
- VNC console (noVNC)
- Remote command execution (QEMU Guest Agent)
- User authentication (JWT)
- Audit logging

---

## Version Summary

| Version | Highlights |
|---------|------------|
| v2.0.0 | RBAC v2 (resource:action format), Permission Engine, Audit Service, Unified init.sql |
| v1.9.0 | Bulk operations, Task queue, Security service, IP blocking |
| v1.8.2 | VM cache, IPAM fixes, Unified migrations |
| v1.8.0 | PVEmanager rebrand, Smart LXC, Extended sessions |
| v1.7.0 | Cross-node templates, Custom dialogs, IPAM auto-release |
| v1.6.1 | SMTP fix, documentation consolidation |
| v1.6.0 | Email/Telegram notifications, background monitoring |
| v1.5.0 | Modern Material Design |
| v1.4.0 | i18n (RU/EN) |
| v1.3.0 | VM config management |
| v1.2.0 | OS Templates |
| v1.1.0 | IPAM |
| v1.0.0 | Initial release |
