# 📖 PVEmanager - Documentation

> Complete guide for installation, configuration and usage of PVEmanager v1.0

---

## 📑 Table of Contents

1. [Quick Start](#-quick-start)
2. [Installation and Deployment](#-installation-and-deployment)
3. [Main Features](#-main-features)
4. [Notification System](#-notification-system)
5. [VM and Container Management](#-vm-and-container-management)
6. [Bulk Operations](#-bulk-operations)
7. [OS Templates](#-os-templates)
8. [Proxmox Clusters](#-proxmox-clusters)
9. [Snapshots](#-snapshots)
10. [IPAM](#-ipam)
11. [Remote Command Execution](#-remote-command-execution)
11. [Monitoring](#-monitoring)
12. [Security (RBAC v2)](#-security)
13. [Localization](#-localization)
14. [Settings](#-settings)
15. [API Reference](#-api-reference)
16. [Troubleshooting](#-troubleshooting)
17. [FAQ](#-faq)

---

## 🚀 Quick Start

### Requirements

- Docker and Docker Compose
- 2GB RAM minimum
- Proxmox VE server with API access

### Start in 1 minute

```bash
# Clone repository
git clone https://github.com/your-repo/pvemanager.git
cd pvemanager

# Copy and configure environment variables
cp .env.example .env
cp backend/.env.example backend/.env

# Start
docker compose up -d

# Open in browser
open http://localhost:8000
```

**Default credentials:**
- Login: `admin`
- Password: `admin123`

> ⚠️ Make sure to change password after first login!

---

## 📦 Installation and Deployment

### Deployment Options

#### 1. Standalone (Development)

```bash
docker compose up -d
```
- Port: 8000
- Without NGINX
- Suitable for local development

#### 2. With NGINX (HTTP)

```bash
./deploy.sh
# Select option 2
```

#### 3. With NGINX + SSL (Production)

```bash
./deploy.sh
# Select option 3
# Specify domain and email for Let's Encrypt
```

### Environment Variables

#### Main (`.env`)

```bash
# Database
DATABASE_URL=postgresql://postgres:password@db:5432/serverpanel
POSTGRES_PASSWORD=your_secure_password

# Secret key (generate unique!)
SECRET_KEY=your-very-long-secret-key-change-me

# Proxmox (optional, can be added via UI)
PROXMOX_HOST=192.168.1.100
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=your_password
```

#### Email and Telegram Notifications

SMTP and Telegram bot settings are now managed via web interface:

1. Go to **Settings → Notifications**
2. In "Notification Channels Configuration" section fill:
   - **SMTP** - your mail server details
   - **Telegram** - bot token from @BotFather
3. Click **Test** button to verify settings

---

## 🎯 Main Features

### Dashboard

- Overall server statistics
- VM/LXC container count
- Resource usage graphs
- Quick access to recent events

### Proxmox Servers

- Add multiple servers
- API Token or password authentication
- Automatic API token creation
- Server status monitoring

### VM and LXC Management

- View all virtual machines
- Start / Stop / Restart / Shutdown
- Configuration changes (CPU, RAM, Disk)
- VNC console in browser
- Remote command execution

---

## 🔔 Notification System

### Overview

Notification system provides:
- **Real-time alerts** about VM events
- **Email notifications** via SMTP
- **Telegram notifications** via Bot API
- **In-App notifications** with bell icon

### Notification Types

| Type | Description | Example |
|------|-------------|---------|
| `vm_status` | VM state changes | VM started/stopped |
| `resource_alert` | Resource alerts | CPU > 80%, RAM > 85% |
| `system` | System events | Connection errors |

### Severity Levels

| Level | Color | Description |
|-------|-------|-------------|
| `critical` | 🔴 Red | Critical issues |
| `warning` | 🟠 Orange | Warnings |
| `info` | 🔵 Blue | Informational |
| `success` | 🟢 Green | Successful operations |

### Email Setup

1. Go to **Settings** → **Notifications**
2. In "Notification Channels Configuration" fill SMTP data:
   - **SMTP server** - mail server address
   - **Port** - 465 for SSL, 587 for STARTTLS  
   - **User** - your email
   - **Password** - app password
   - **Sender email** - sender address
3. In "Notification Settings" check **Email notifications**
4. Click **Test** to verify

**Supported SMTP servers:**

| Server | Host | Port | TLS |
|--------|------|------|-----|
| Yandex | smtp.yandex.ru | 465 | SSL |
| Gmail | smtp.gmail.com | 587 | STARTTLS |
| Mail.ru | smtp.mail.ru | 465 | SSL |

> ⚠️ For Yandex and Gmail use "App Password", not main password!

### Telegram Setup

1. Create bot via [@BotFather](https://t.me/BotFather)
2. In **Settings → Notifications** enter bot token
3. Send `/start` to your bot
4. Get Chat ID via [@userinfobot](https://t.me/userinfobot)
5. In "Notification Settings" enable Telegram and enter Chat ID
6. Click **Verify** to confirm

### Notification Settings

| Parameter | Description |
|-----------|-------------|
| Enabled | Activate channel |
| Critical only | Send only critical level |
| Quiet hours | Period without notifications (e.g., 23:00 - 07:00) |

### Background Monitoring

Automatically tracks:
- **VM Status** every 30 seconds
- **Resources** every 60 seconds
- **Thresholds**: CPU > 80%, RAM > 85%, Disk > 90%

---

## 🖥️ VM and Container Management

### VM Actions

| Action | Description | Hotkey |
|--------|-------------|--------|
| Start | Start VM | - |
| Stop | Stop (ACPI shutdown) | - |
| Restart | Reboot | - |
| Force Stop | Force stop | - |
| Delete | Delete VM | - |

### Bulk Operations

Select multiple VMs/containers and perform mass actions:

1. Check the checkbox next to each VM you want to select
2. Use "Select All" to select all visible VMs
3. Click action button in the bulk actions bar:
   - **Start All** — Start all selected
   - **Stop All** — Stop all selected
   - **Restart All** — Restart all selected
   - **Delete All** — Delete all selected (with confirmation)

See [Bulk Operations](#-bulk-operations) for more details.

### VNC Console

1. Open VM details
2. Click **Console** button
3. Console opens in new tab
4. Fullscreen mode supported

### Configuration Changes

```
CPU: 1-32 cores
RAM: 512MB - 128GB
Disk: Increase size (decrease not possible)
```

---

## ⚡ Bulk Operations

### Overview

Bulk operations allow you to perform the same action on multiple VMs or containers at once. This is useful when you need to start, stop, or restart many virtual machines simultaneously.

### Supported Actions

| Action | Description | Confirmation Required |
|--------|-------------|----------------------|
| Bulk Start | Start all selected VMs/containers | No |
| Bulk Stop | Stop all selected (ACPI shutdown) | No |
| Bulk Restart | Restart all selected | No |
| Bulk Shutdown | Graceful shutdown | No |
| Bulk Delete | Delete all selected | Yes (double confirm) |

### How to Use

1. **Navigate to Virtual Machines page**
2. **Select VMs** using checkboxes:
   - Click individual checkboxes
   - Or use "Select All" to select all visible VMs
3. **Bulk Actions Bar** appears at the bottom when VMs are selected
4. **Click desired action** button
5. **Confirm** if prompted (for delete operations)

### Task Queue

When you initiate a bulk operation:

1. **Task is created** and added to the queue
2. **Background processing** starts automatically
3. **Progress is tracked** (completed/failed items)
4. **Results are saved** for each item

### Viewing Task Status

Currently, tasks can be viewed via API:

```bash
# Get your recent tasks
curl -X GET "http://localhost:8000/api/tasks" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Limitations

- Only one bulk task runs at a time
- Tasks are processed sequentially
- Maximum items per task: unlimited (but consider server load)
- Tasks cannot be paused (only cancelled if pending)

---

## 📋 OS Templates

### Concept

OS Templates allow quick VM deployment from preconfigured templates.

### Creating Template Group

1. **OS Templates** → **Groups** → **Add Group**
2. Enter name (e.g., "Linux Servers")
3. Add description

### Adding Template

1. **OS Templates** → **Templates** → **Add Template**
2. Or: **Scan** → select from Proxmox

### Deploying VM

1. Select template
2. Specify:
   - VM name
   - **Target Node** (for clusters - select where to deploy)
   - CPU/RAM/Disk
   - IP address (or auto from IPAM)
   - SSH key (optional)
3. Click **Deploy**

### Cross-Node Template Deployment (Clusters)

When using Proxmox clusters, templates can be deployed to any cluster node:

#### How it works

1. **Create template on any node** - Template is initially created on one cluster node
2. **Select target node** - When deploying VM, choose any online cluster node
3. **Automatic replication** - If template doesn't exist on target node:
   - System clones template to target node
   - Converts clone to template
   - Tracks replicated templates in database
4. **Subsequent deployments** - Use already replicated template (fast)

#### Requirements

- Proxmox cluster (nodes must be in same cluster)
- Shared storage recommended (but not required)
- Template must be accessible from source node

#### Example

```
Cluster: pve1, pve2, pve3
Template: Ubuntu-22.04 on pve1

Deploy to pve1: Uses original template (fast)
Deploy to pve2: Replicates template → deploys (first time slower)
Deploy to pve3: Replicates template → deploys (first time slower)

Next deploy to pve2: Uses replicated template (fast)
```

### Reinstalling VM

1. Open VM details
2. Click **Reinstall**
3. Select template
4. VMID preserved

---

## 🔗 Proxmox Clusters

### Overview

PVEmanager fully supports Proxmox clusters, enabling management of multiple nodes as a single entity.

### Adding a Cluster

1. Go to **Proxmox VE** → **Add Server**
2. Enter **any node's** IP address (e.g., pve1)
3. Panel automatically discovers all cluster nodes
4. All nodes appear in node selectors

### Cluster Benefits

| Feature | Standalone | Cluster |
|---------|------------|---------|
| Node count | 1 | Multiple |
| Cross-node templates | ❌ | ✅ |
| Target node selection | ❌ | ✅ |
| Automatic failover | ❌ | ✅ (Proxmox HA) |

### Creating a Proxmox Cluster

If your nodes aren't clustered yet:

```bash
# On first node (pve1)
pvecm create my-cluster

# On other nodes (pve2, pve3, etc.)
pvecm add 10.10.10.11  # IP of pve1
```

### Cross-Node Template Deployment

See [OS Templates - Cross-Node Template Deployment](#cross-node-template-deployment-clusters)

---

## 🌐 IPAM

### Networks

Create networks to organize IP space:

```
Name: Production Network
CIDR: 192.168.1.0/24
VLAN: 100
Gateway: 192.168.1.1
DNS: 8.8.8.8, 8.8.4.4
```

### IP Pools

Pools define ranges for automatic allocation:

```
Pool: Web Servers
Start: 192.168.1.10
End: 192.168.1.50
```

### IP Status

| Status | Description |
|--------|-------------|
| 🟢 Available | Free |
| 🔵 Allocated | Assigned to VM |
| 🟠 Reserved | Reserved |

---

## 💻 Remote Command Execution

### Requirements

- **QEMU Guest Agent** installed and running in VM
- VM must be running

### Installing Guest Agent

**Debian/Ubuntu:**
```bash
apt install qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

**CentOS/RHEL/AlmaLinux:**
```bash
yum install qemu-guest-agent
systemctl enable --now qemu-guest-agent
```

**Windows:**
Download [virtio-win drivers](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/)

### Usage

1. Open VM details
2. Go to **Commands** tab
3. Enter command or select from quick commands
4. Click **Execute**

### Quick Commands

| Command | Description |
|---------|-------------|
| `df -h` | Disk usage |
| `free -h` | Memory usage |
| `uptime` | Uptime |
| `ps aux` | Process list |
| `systemctl status` | Service status |

### Limitations

- Timeout: 30 seconds
- Interactive commands not supported
- Requires Guest Agent

---

## 📊 Monitoring

### Real-time Metrics

- **CPU**: Total load + per core
- **RAM**: Used/Total
- **Disk**: Used/Total + I/O
- **Network**: In/Out traffic

### Graph Periods

- Hour
- Day
- Week
- Month

### Alerts

Threshold values for notifications:

| Metric | Warning | Critical |
|--------|---------|----------|
| CPU | 80% | 90% |
| RAM | 85% | 95% |
| Disk | 90% | 95% |

---

## 🔒 Security

### Overview

PVEmanager includes comprehensive security features to protect your infrastructure.

### RBAC v2 — Role-Based Access Control

The new RBAC v2 system uses atomic permissions with `resource:action` format.

#### Permission Format

```
resource:action[:scope]
```

Examples:
- `vm:view` — View virtual machines
- `server:create` — Add new Proxmox servers
- `log:export` — Export audit logs
- `role:manage` — Full role management

#### Available Resources

| Resource | Description |
|----------|-------------|
| `dashboard` | Dashboard access |
| `server` | Proxmox servers |
| `vm` | Virtual machines |
| `lxc` | LXC containers |
| `template` | OS templates |
| `storage` | Storage pools |
| `backup` | Backups |
| `ipam` | IP address management |
| `user` | User management |
| `role` | Role management |
| `log` | Audit logs |
| `setting` | Panel settings |
| `notification` | Notifications |

#### Available Actions

| Action | Description |
|--------|-------------|
| `view` | Read access |
| `create` | Create new resources |
| `update` | Modify existing resources |
| `delete` | Remove resources |
| `start` | Start VM/container |
| `stop` | Stop VM/container |
| `restart` | Restart VM/container |
| `console` | Access console |
| `migrate` | Migrate between nodes |
| `manage` | Full management (implies view, update, etc.) |
| `export` | Export data (logs) |
| `execute` | Execute commands |

#### Default Roles

| Role | Description |
|------|-------------|
| `admin` | Full access to all features |
| `moderator` | VM management, view logs, no settings |
| `user` | VPS-style access — only own instances |
| `demo` | Read-only access |

### VPS-Style User Isolation

Users with `user` role have VPS-style access — they can only see and manage instances assigned to them.

#### How It Works

1. **Instance Ownership**: Each VM/LXC can have an `owner_id` pointing to a user
2. **Automatic Assignment**: When a user creates a VM, they automatically become the owner
3. **Access Control**: All operations (view, start, stop, console, snapshots) check ownership

#### User Role Permissions

```
vms:view:own      — View only own instances
vms:start:own     — Start own instances
vms:stop:own      — Stop own instances
vms:restart:own   — Restart own instances
vms:console:own   — Access console of own instances
vms:snapshots:own — Manage snapshots of own instances
```

#### Admin Assignment

Admins can assign instances to users:
1. Open instance details
2. Click "Assign Owner"
3. Select user from dropdown
4. Save changes

### Snapshots

Snapshots allow you to save and restore the state of VMs and containers.

#### VM Snapshots

```bash
# List snapshots
GET /api/{server_id}/vm/{vmid}/snapshots?node={node}

# Create snapshot
POST /api/{server_id}/vm/{vmid}/snapshots?node={node}
{
  "snapname": "before-update",
  "description": "Before system update",
  "vmstate": false  # Include RAM state (optional)
}

# Delete snapshot
DELETE /api/{server_id}/vm/{vmid}/snapshots/{snapname}?node={node}

# Rollback to snapshot
POST /api/{server_id}/vm/{vmid}/snapshots/{snapname}/rollback?node={node}&start=false
```

#### Container Snapshots

Same endpoints, but use `/container/` instead of `/vm/`.

### SDN (Software Defined Networking)

Manage Proxmox SDN zones and virtual networks.

#### Check SDN Availability

```bash
GET /api/servers/{server_id}/sdn/status
```

Returns:
```json
{
  "server_id": 1,
  "sdn_available": true,
  "pending_changes": false
}
```

#### Zone Types

| Type | Description |
|------|-------------|
| `simple` | Basic isolated network |
| `vlan` | VLAN-based separation |
| `vxlan` | VXLAN overlay network |
| `evpn` | EVPN/VXLAN with BGP |

#### SDN API Endpoints

```bash
# List zones
GET /api/servers/{server_id}/sdn/zones

# Create zone
POST /api/servers/{server_id}/sdn/zones
{
  "zone": "myzone",
  "type": "simple"
}

# List VNets
GET /api/servers/{server_id}/sdn/vnets

# Create VNet
POST /api/servers/{server_id}/sdn/vnets
{
  "vnet": "vnet1",
  "zone": "myzone",
  "alias": "Production Network"
}

# Create subnet
POST /api/servers/{server_id}/sdn/vnets/{vnet}/subnets
{
  "subnet": "10.0.0.0/24",
  "gateway": "10.0.0.1",
  "snat": true
}

# Apply changes (required after modifications)
POST /api/servers/{server_id}/sdn/apply
```

#### Legacy Compatibility

Old permission format (`vms.view`, `proxmox.manage`) is still supported and automatically converted to new format.

### IP Blocking

Automatic and manual IP blocking protects against brute-force attacks.

#### Automatic Blocking

When a user fails to login multiple times from the same IP:
1. After threshold (default: 10) failed attempts
2. IP is blocked for configured duration (default: 60 minutes)
3. Block can be temporary or permanent

#### Manual Blocking

Administrators can manually block IPs:
- Block with reason
- Set duration or make permanent
- View blocked IP list
- Unblock as needed

### Session Management

Track and control active user sessions.

| Feature | Description |
|---------|-------------|
| Active Sessions | View all currently logged in users |
| Device Info | Browser and device detection |
| IP Tracking | Session IP address logging |
| Single Session | Option to allow only one session per user |
| Force Logout | Terminate any session |

### Login Protection

| Setting | Default | Description |
|---------|---------|-------------|
| Max Login Attempts | 5 | Attempts before account lockout |
| Lockout Duration | 30 min | How long account stays locked |
| IP Block Threshold | 10 | Failed attempts before IP block |
| IP Block Duration | 60 min | How long IP stays blocked |

### Security Settings

Access via **Settings → Security**:

| Setting | Default | Description |
|---------|---------|-------------|
| Session Timeout | 60 min | Auto-logout after inactivity |
| Single Session | Off | Allow only one session per user |
| Password Min Length | 8 | Minimum password characters |
| Require Uppercase | Yes | Password must have uppercase |
| Require Lowercase | Yes | Password must have lowercase |
| Require Numbers | Yes | Password must have digits |
| Require Special | No | Password must have special chars |
| Password Expiry | 0 (never) | Days until password expires |
| API Rate Limit | 60/min | API requests per minute |

### Best Practices

1. **Change default passwords** immediately after installation
2. **Enable SSL/HTTPS** in production
3. **Use strong passwords** with complexity requirements
4. **Monitor login attempts** for suspicious activity
5. **Configure firewall** to limit access to panel
6. **Regular updates** to get security patches

---

## 🌍 Localization

### Supported Languages

- 🇷🇺 Russian
- 🇺🇸 English (default)

### Switching Language

1. **Settings** → **Panel Settings**
2. Select language
3. Save
4. Page will reload

### Adding New Language

1. Open `backend/app/i18n.py`
2. Add translation for each key:

```python
"key_name": {
    "ru": "Russian text",
    "en": "English text",
    "uz": "O'zbek matni"  # New language
}
```

---

## ⚙️ Settings

### User Profile

- Full name
- Email (for notifications)
- Password change

### Panel Settings

| Parameter | Description | Default |
|-----------|-------------|---------|
| Refresh interval | Data refresh frequency | 30 sec |
| Log retention | How many days to keep logs | 30 days |
| Language | Interface language | English |

### Notification Settings

- In-App: Always enabled
- Email: Requires SMTP setup
- Telegram: Requires Bot Token

---

## 🔌 API Reference

### Authentication

```bash
# Get token
POST /api/auth/login
Content-Type: application/json

{
    "username": "admin",
    "password": "admin123"
}

# Response
{
    "access_token": "eyJ...",
    "token_type": "bearer"
}
```

### Using Token

```bash
curl -H "Authorization: Bearer eyJ..." \
     http://localhost:8000/api/notifications
```

### Main Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/servers` | List Proxmox servers |
| GET | `/api/servers/{id}` | Get specific server |
| POST | `/api/servers` | Add new server |
| PUT | `/api/servers/{id}` | Update server |
| DELETE | `/api/servers/{id}` | Delete server |
| POST | `/api/servers/{id}/test` | Test server connection |
| GET | `/api/servers/{id}/resources` | VMs and containers on server |
| POST | `/api/servers/{id}/vm/{vmid}/{action}` | Control VM (start/stop/restart) |
| POST | `/api/servers/{id}/container/{vmid}/{action}` | Control Container (start/stop/restart) |
| GET | `/api/servers/{id}/vm/{vmid}/status` | Get VM status |
| GET | `/api/servers/{id}/container/{vmid}/status` | Get Container status |
| GET | `/ipam/api/networks` | List IPAM networks |
| POST | `/ipam/api/networks` | Create IPAM network |
| GET | `/ipam/api/allocations` | List IP allocations |
| POST | `/ipam/api/allocations` | Create IP allocation |
| GET | `/templates/api/groups` | List template groups |
| GET | `/templates/api/templates` | List OS templates |
| POST | `/templates/api/templates` | Create OS template |
| GET | `/api/notifications` | List notifications |
| PATCH | `/api/notifications/{id}/read` | Mark notification as read |
| DELETE | `/api/notifications/{id}` | Delete notification |
| GET | `/api/users` | List users (admin only) |
| POST | `/api/users` | Create user (admin only) |
| GET | `/settings/api/panel` | Get panel settings |
| PUT | `/settings/api/panel` | Update panel settings |
| GET | `/logs/api/logs` | Get audit logs |
| GET | `/logs/api/stats` | Get log statistics |

### API Examples

For detailed API usage examples, see [API_EXAMPLES.md](API_EXAMPLES.md)

### Swagger Documentation

Available at: `http://localhost:8000/docs`

---

## 🔧 Troubleshooting

### Startup Issues

#### Container won't start

```bash
# Check logs
docker compose logs app

# Check status
docker compose ps
```

#### Database connection error

```bash
# Check if DB is running
docker compose logs db

# Restart
docker compose restart db
docker compose restart app
```

### Proxmox Issues

#### "Connection refused"

- Check server IP address
- Check that port 8006 is open
- Check firewall on Proxmox

#### "Authentication failed"

- Check login/password
- Use format `user@pam` or `user@pve`
- Try creating API Token

### Notification Issues

#### Email not sending

1. Check SMTP settings in `backend/.env`
2. For port 465 SSL is required
3. For port 587 STARTTLS is required
4. Check "App Password" for Yandex/Gmail

```bash
# Check variables in container
docker compose exec app env | grep SMTP
```

#### Telegram not working

1. Check bot token
2. Send `/start` to bot
3. Check Chat ID
4. Make sure bot is not blocked

#### "Test" button freezes

- Check logs: `docker compose logs app --tail 50`
- SMTP server may be unreachable
- Timeout is 10 seconds

### VNC Issues

#### "VNC connection failed"

- Check that VM is running
- Check that VNC is enabled in Proxmox
- Try restarting VM

### Command Issues

#### "Guest Agent not running"

```bash
# In VM run:
systemctl status qemu-guest-agent
systemctl start qemu-guest-agent
```

#### Command hangs

- Timeout is 30 seconds
- Don't use interactive commands
- Check that VM responds

### Performance Issues

#### High memory usage

```bash
# Restart containers
docker compose restart

# Clean logs
docker system prune -f
```

#### Slow page loading

- Increase refresh interval in settings
- Check network to Proxmox server

---

## ❓ FAQ

### General Questions

**Q: Can I use without Proxmox?**
A: No, the panel is specifically designed for Proxmox management.

**Q: How many servers can I add?**
A: Unlimited.

**Q: Is Proxmox cluster supported?**
A: Yes, add any cluster node.

### Security

**Q: Are passwords stored in plain text?**
A: No, bcrypt hashing is used.

**Q: How to change SECRET_KEY?**
A: Change in `.env` and restart. All sessions will be invalidated.

### Notifications

**Q: Why emails are not arriving?**
A: Check SMTP settings, especially port and TLS.

**Q: Can I disable notifications?**
A: Yes, in notification settings.

**Q: How to set quiet hours?**
A: In notification settings specify period (e.g., 23:00 - 07:00).

### Integrations

**Q: Is there an API for integration?**
A: Yes, REST API with documentation at `/docs`.

**Q: Can I add webhooks?**
A: Not yet, but planned for future versions.

---

## 📞 Support

- **GitHub Issues**: [Create issue](https://github.com/your-repo/server-panel/issues)
- **Documentation**: This file (WIKI.md)
- **Changelog**: [CHANGELOG.md](CHANGELOG.md)

---

*Last updated: December 2025*
*Version: 1.9.0*
