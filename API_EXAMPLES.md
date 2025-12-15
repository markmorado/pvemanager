# PVEmanager API Examples

This document provides examples for using the PVEmanager API. All API endpoints require authentication via JWT token.

## Authentication

### Login

```bash
curl -X POST "http://localhost:8000/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "admin123"
  }'
```

Response:
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "token_type": "bearer"
}
```

### Using the Token

For all subsequent requests, include the token in the Authorization header:

```bash
curl -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..." \
     http://localhost:8000/api/servers
```

## System Updates

### Get Current Version

```bash
curl -X GET "http://localhost:8000/settings/api/version"
```

Response:
```json
{
  "version": "1.0"
}
```

### Check for Updates

```bash
curl -X GET "http://localhost:8000/settings/api/updates/check" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Response:
```json
{
  "current_version": "2.1.3",
  "latest_version": "1.0",
  "update_available": true,
  "changelog": "## [v1.0] - 2025-12-15\n\n### Initial Release...",
  "commits_behind": 5,
  "error": null
}
```

### Perform Update

```bash
curl -X POST "http://localhost:8000/settings/api/updates/perform" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Response:
```json
{
  "success": true,
  "message": "Update started. The panel will restart shortly."
}
```

## Proxmox Servers Management

### List All Proxmox Servers

```bash
curl -X GET "http://localhost:8000/api/servers" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get Specific Proxmox Server

```bash
curl -X GET "http://localhost:8000/api/servers/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Add New Proxmox Server

```bash
curl -X POST "http://localhost:8000/api/servers" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "pve-main",
    "hostname": "pve1.example.com",
    "ip_address": "192.168.1.100",
    "port": 8006,
    "api_user": "root@pam",
    "api_token_name": "pvemanager",
    "api_token_value": "YOUR_API_TOKEN",
    "use_password": false,
    "verify_ssl": false
  }'
```

### Update Proxmox Server

```bash
curl -X PUT "http://localhost:8000/api/servers/1" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "pve-main-updated",
    "hostname": "pve1.example.com",
    "ip_address": "192.168.1.100",
    "port": 8006,
    "api_user": "root@pam",
    "api_token_name": "pvemanager",
    "api_token_value": "YOUR_UPDATED_API_TOKEN",
    "use_password": false,
    "verify_ssl": false
  }'
```

### Delete Proxmox Server

```bash
curl -X DELETE "http://localhost:8000/api/servers/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Test Proxmox Server Connection

```bash
curl -X POST "http://localhost:8000/api/servers/1/test" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Virtual Machines Management

### List VMs on a Server

```bash
curl -X GET "http://localhost:8000/api/servers/1/resources" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Start a VM

```bash
curl -X POST "http://localhost:8000/api/servers/1/vm/100/start?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Stop a VM

```bash
curl -X POST "http://localhost:8000/api/servers/1/vm/100/stop?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Restart a VM

```bash
curl -X POST "http://localhost:8000/api/servers/1/vm/100/restart?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get VM Status

```bash
curl -X GET "http://localhost:8000/api/servers/1/vm/100/status?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Containers Management

### Start a Container

```bash
curl -X POST "http://localhost:8000/api/servers/1/container/101/start?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Stop a Container

```bash
curl -X POST "http://localhost:8000/api/servers/1/container/101/stop?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Restart a Container

```bash
curl -X POST "http://localhost:8000/api/servers/1/container/101/restart?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Snapshots

### List VM Snapshots

```bash
curl -X GET "http://localhost:8000/api/servers/1/vm/100/snapshots?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create VM Snapshot

```bash
curl -X POST "http://localhost:8000/api/servers/1/vm/100/snapshots?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "snapname": "before-update",
    "description": "Snapshot before system update",
    "vmstate": false
  }'
```

### Delete VM Snapshot

```bash
curl -X DELETE "http://localhost:8000/api/servers/1/vm/100/snapshots/before-update?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Rollback VM to Snapshot

```bash
curl -X POST "http://localhost:8000/api/servers/1/vm/100/snapshots/before-update/rollback?node=pve1&start=false" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### List Container Snapshots

```bash
curl -X GET "http://localhost:8000/api/servers/1/container/101/snapshots?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create Container Snapshot

```bash
curl -X POST "http://localhost:8000/api/servers/1/container/101/snapshots?node=pve1" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "snapname": "weekly-backup",
    "description": "Weekly backup snapshot"
  }'
```

### Get Proxmox Task Status

```bash
curl -X GET "http://localhost:8000/api/servers/1/task/UPID:pve1:000ABC:snapshot:1234567890:vzdump::root@pam:/status" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Response:
```json
{
  "status": "stopped",
  "exitstatus": "OK",
  "node": "pve1",
  "type": "snapshot",
  "upid": "UPID:pve1:000ABC..."
}
```

### List Archived Snapshots

```bash
curl -X GET "http://localhost:8000/api/snapshot-archives?server_id=1&vmid=100" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## IPAM (IP Address Management)

### List Networks

```bash
curl -X GET "http://localhost:8000/ipam/api/networks" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create Network

```bash
curl -X POST "http://localhost:8000/ipam/api/networks" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production Network",
    "network": "192.168.10.0/24",
    "gateway": "192.168.10.1",
    "description": "Production VLAN"
  }'
```

### Allocate IP Address

```bash
curl -X POST "http://localhost:8000/ipam/api/allocations" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "ip_address": "192.168.10.10",
    "network_id": 1,
    "resource_type": "vm",
    "resource_id": 100,
    "resource_name": "web-server-01",
    "hostname": "web01",
    "allocation_type": "static"
  }'
```

### Get Next Available IP

```bash
curl -X GET "http://localhost:8000/ipam/api/allocations/next-available/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## OS Templates Management

### List Template Groups

```bash
curl -X GET "http://localhost:8000/templates/api/groups" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### List Templates

```bash
curl -X GET "http://localhost:8000/templates/api/templates" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create Template

```bash
curl -X POST "http://localhost:8000/templates/api/templates" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "group_id": 1,
    "server_id": 1,
    "name": "Ubuntu 22.04 Template",
    "vmid": 200,
    "node": "pve1",
    "default_cores": 2,
    "default_memory": 2048,
    "default_disk": 20,
    "min_cores": 1,
    "min_memory": 512
  }'
```

## Notifications

### List Notifications

```bash
curl -X GET "http://localhost:8000/api/notifications?limit=50" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Mark Notification as Read

```bash
curl -X PATCH "http://localhost:8000/api/notifications/1/read" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Delete Notification

```bash
curl -X DELETE "http://localhost:8000/api/notifications/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Users Management

### List Users (Admin Only)

```bash
curl -X GET "http://localhost:8000/api/users" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get User Details

```bash
curl -X GET "http://localhost:8000/api/users/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create User (Admin Only)

```bash
curl -X POST "http://localhost:8000/api/users" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "newuser",
    "email": "newuser@example.com",
    "full_name": "New User",
    "password": "securepassword123",
    "is_active": true,
    "is_admin": false,
    "role_id": 3
  }'
```

## Roles and Permissions

### List Roles

```bash
curl -X GET "http://localhost:8000/users/api/roles" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get All Permissions (Legacy Format)

```bash
curl -X GET "http://localhost:8000/users/api/permissions" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get All Permissions (RBAC v2 Format)

```bash
curl -X GET "http://localhost:8000/users/api/permissions/v2" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Response:
```json
{
  "Dashboard": {
    "dashboard:view": "View Dashboard"
  },
  "Proxmox Servers": {
    "server:view": "View Servers",
    "server:create": "Add Server",
    "server:update": "Edit Server",
    "server:delete": "Delete Server",
    "server:manage": "Manage Servers"
  },
  "Virtual Machines": {
    "vm:view": "View VMs",
    "vm:create": "Create VMs",
    "vm:update": "Update VMs",
    "vm:delete": "Delete VMs",
    "vm:start": "Start VMs",
    "vm:stop": "Stop VMs",
    "vm:restart": "Restart VMs",
    "vm:console": "VM Console",
    "vm:migrate": "Migrate VMs",
    "vm:execute": "Execute Commands"
  }
}
```

### Create Role

```bash
curl -X POST "http://localhost:8000/users/api/roles" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "operator",
    "display_name": "Operator",
    "description": "Can manage VMs but not create them",
    "permissions": {
      "dashboard:view": true,
      "server:view": true,
      "vm:view": true,
      "vm:start": true,
      "vm:stop": true,
      "vm:restart": true,
      "vm:console": true
    }
  }'
```

## Settings

### Get Panel Settings

```bash
curl -X GET "http://localhost:8000/settings/api/panel" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Update Panel Settings

```bash
curl -X PUT "http://localhost:8000/settings/api/panel" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "refresh_interval": 10,
    "log_retention_days": 60,
    "language": "en"
  }'
```

## Logs

### Get Logs

```bash
curl -X GET "http://localhost:8000/logs/api/logs?limit=100" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get Log Statistics

```bash
curl -X GET "http://localhost:8000/logs/api/stats?hours=24" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Bulk Operations (Task Queue)

### Create Bulk Start Task

```bash
curl -X POST "http://localhost:8000/api/tasks/bulk-start" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"server_id": 1, "vmid": 100, "vm_type": "qemu", "node": "pve1", "name": "web-server"},
      {"server_id": 1, "vmid": 101, "vm_type": "lxc", "node": "pve1", "name": "db-container"}
    ]
  }'
```

### Create Bulk Stop Task

```bash
curl -X POST "http://localhost:8000/api/tasks/bulk-stop" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"server_id": 1, "vmid": 100, "vm_type": "qemu", "node": "pve1"},
      {"server_id": 1, "vmid": 101, "vm_type": "lxc", "node": "pve1"}
    ]
  }'
```

### Create Bulk Delete Task

```bash
curl -X POST "http://localhost:8000/api/tasks/bulk-delete" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"server_id": 1, "vmid": 102, "vm_type": "qemu", "node": "pve1"}
    ]
  }'
```

### Get Task Status

```bash
curl -X GET "http://localhost:8000/api/tasks/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Response:
```json
{
  "id": 1,
  "task_type": "bulk_start",
  "status": "completed",
  "total_items": 2,
  "completed_items": 2,
  "failed_items": 0,
  "results": [
    {"vmid": 100, "name": "web-server", "success": true, "message": "OK"},
    {"vmid": 101, "name": "db-container", "success": true, "message": "OK"}
  ],
  "created_at": "2025-12-13T10:00:00Z",
  "completed_at": "2025-12-13T10:00:05Z"
}
```

### Get User's Tasks

```bash
curl -X GET "http://localhost:8000/api/tasks?limit=20" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Cancel Pending Task

```bash
curl -X DELETE "http://localhost:8000/api/tasks/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Security

### Get Blocked IPs

```bash
curl -X GET "http://localhost:8000/api/security/blocked-ips" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Block IP Address

```bash
curl -X POST "http://localhost:8000/api/security/block-ip" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "ip_address": "192.168.1.100",
    "reason": "Suspicious activity",
    "duration_minutes": 60,
    "permanent": false
  }'
```

### Unblock IP Address

```bash
curl -X DELETE "http://localhost:8000/api/security/blocked-ips/192.168.1.100" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get Active Sessions

```bash
curl -X GET "http://localhost:8000/api/security/sessions" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Terminate Session

```bash
curl -X DELETE "http://localhost:8000/api/security/sessions/abc123token" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get Login Attempts

```bash
curl -X GET "http://localhost:8000/api/security/login-attempts?limit=50" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get Security Settings

```bash
curl -X GET "http://localhost:8000/api/security/settings" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Update Security Setting

```bash
curl -X PUT "http://localhost:8000/api/security/settings/max_login_attempts" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "value": "10"
  }'
```

## Error Responses

The API uses standard HTTP status codes:

- `200` - Success
- `201` - Created
- `204` - No Content (successful deletion)
- `400` - Bad Request (invalid input)
- `401` - Unauthorized (missing or invalid token)
- `403` - Forbidden (insufficient permissions)
- `404` - Not Found
- `500` - Internal Server Error

Error response format:
```json
{
  "detail": "Error message"
}
```