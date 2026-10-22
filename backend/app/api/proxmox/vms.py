from fastapi import APIRouter, Depends, Request, HTTPException, Query, Form, status, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from loguru import logger
from typing import List
import ssl
import asyncio
import base64
import httpx
import websockets

from ...db import get_db
from ...models import ProxmoxServer, VMInstance, User, IPAMAllocation, IPAMNetwork, VMSnapshotArchive, UserSSHKey
from ...schemas import ProxmoxServerCreate, ProxmoxServerUpdate, ProxmoxServerResponse
from ...proxmox import ProxmoxClient, get_proxmox_resources
from ...auth import (get_current_user, PermissionChecker, require_permission,
                     check_permission, authenticate_ws_session, watch_ws_access)
from ...rbac import PermissionEngine
from ...logging_service import LoggingService
from ...ipam_service import IPAMService
from ._helpers import enforce_server_visibility, pve_authority, stash_pve_ticket, claim_pve_ticket
from ._helpers import PveNode, require_node_name
from ._helpers import (check_vm_access, require_vm_access, _get_proxmox_client,
                        get_next_vmid, archive_and_delete_snapshots,
                        save_vm_instance, get_vm_instance, soft_delete_vm_instance,
                        can_view_all_instances, pretty_os_from_template,
                        get_visible_server_ids, _NO_WORKSPACE_HEADER)
from ...services.metrics_history import query_instance_metrics, timeframe_to_range

router = APIRouter()


def _cleanup_installed_apps(db: Session, server_id: int, vmid: int) -> int:
    """Удалить записи App Store (installed_apps), привязанные к удаляемому LXC/VM.

    Без этого приложение, установленное через каталог, остаётся «хвостом»
    в разделе «Мои приложения» после удаления инстанса со страницы инстансов.
    Каскад (app_operations.installed_app_id ON DELETE CASCADE) чистит журнал операций.
    """
    try:
        from ...models import InstalledApp
        rows = (
            db.query(InstalledApp)
            .filter(InstalledApp.server_id == server_id, InstalledApp.vmid == vmid)
            .all()
        )
        if not rows:
            return 0
        notify = [(ia.id, ia.owner_id) for ia in rows]
        for ia in rows:
            db.delete(ia)
        db.commit()
        logger.info(f"Removed {len(rows)} installed_apps record(s) for vmid={vmid} server={server_id}")
        # Live-обновление раздела «Мои приложения» у владельца (тот же WS-эвент, что и штатное удаление)
        try:
            from ...websocket_manager import broadcast_task_update
            for _iaid, _owner in notify:
                if _owner:
                    broadcast_task_update(_owner, "appstore_app_deleted", {"installed_app_id": _iaid})
        except Exception:
            pass
        return len(rows)
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to cleanup installed_apps for vmid={vmid}: {e}")
        return 0


import time as time_lib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_live_metrics_cache = {}
_live_metrics_lock = threading.Lock()
METRICS_TTL = 10  # seconds
METRICS_MAX_WORKERS = 8  # parallel Proxmox fetches for the VM list

from ...models import TaskQueue
from ...services.task_queue_service import TaskQueueService, process_task_queue
from ...api.proxmox.tasks import ProxmoxTaskService
from pydantic import BaseModel
from typing import List as TypingList, Literal, Optional
from pydantic import Field


class BulkOperationItem(BaseModel):
    server_id: int
    vmid: int
    vm_type: Literal['qemu', 'lxc']
    name: str = Field(..., max_length=255)
    node: PveNode
    target_node: Optional[PveNode] = None  # для action=migrate
    online: Optional[bool] = None       # live-миграция запущенной VM


class BulkOperationRequest(BaseModel):
    action: str  # start, stop, restart, shutdown, delete, migrate
    items: TypingList[BulkOperationItem] = Field(..., max_length=500)


# Same permissions as the single-guest endpoints (control_vm / migrate / delete).
_BULK_ACTION_PERMISSIONS = {
    'start': {'qemu': 'vm:start', 'lxc': 'vm:start'},
    'stop': {'qemu': 'vm:stop', 'lxc': 'vm:stop'},
    'shutdown': {'qemu': 'vm:stop', 'lxc': 'vm:stop'},
    'restart': {'qemu': 'vm:restart', 'lxc': 'vm:restart'},
    'delete': {'qemu': 'vm:delete', 'lxc': 'vm:delete'},
    'migrate': {'qemu': 'vm:migrate', 'lxc': 'lxc:migrate'},
}


class VMOwnerUpdate(BaseModel):
    user_id: Optional[int] = None


@router.post("/api/sync-vms")
def sync_vms_now(
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """
    Force immediate synchronization of VMs/containers from all Proxmox servers.
    Useful when you create VMs directly in Proxmox and want them to appear in the panel immediately.
    """
    try:
        from ...workers.monitoring_worker import MonitoringWorker
        
        worker = MonitoringWorker()
        worker.sync_vm_cache()
        
        return JSONResponse(content={
            "status": "success",
            "message": "VM synchronization completed"
        })
    except Exception as e:
        logger.error(f"Manual VM sync failed: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e)
            }
        )


@router.get("/api/virtual-machines")
def get_all_virtual_machines(
    request: Request,
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """
    API для получения всех VM/LXC в плоском списке для таблицы.
    Данные читаются из локального кэша (таблица vm_instances), который
    обновляется фоновым worker каждые 30 секунд.
    
    Для пользователей с ролью 'user' возвращаются только их собственные инстансы.
    """
    from ...models import VMInstance
    from ...api.workspaces import get_workspace_server_ids

    # Workspace filtering
    workspace_server_ids = get_workspace_server_ids(request, db, current_user)

    # Get servers for name lookup (filtered by workspace)
    servers_q = db.query(ProxmoxServer)
    if workspace_server_ids is not None:
        servers_q = servers_q.filter(ProxmoxServer.id.in_(workspace_server_ids))
    servers = servers_q.all()
    server_map = {s.id: s for s in servers}
    
    # Pre-load IPAM allocations for IP lookup
    ipam_allocations = db.query(IPAMAllocation).filter(
        IPAMAllocation.status.in_(['allocated', 'reserved'])
    ).all()
    
    # Pre-load IPAM networks for network name lookup
    ipam_networks = db.query(IPAMNetwork).all()
    network_map = {n.id: n for n in ipam_networks}
    
    # Build IPAM lookups.
    # Только по идентификатору гостя: сопоставление по имени давало чужой адрес
    # одноимённым гостям на разных серверах (два разных "wireguard").
    # У гостя может быть несколько адресов: в ipam_by_vmid кладём основной
    # (он идёт в колонку IP), в ipam_all_by_vmid — весь список для поля ips.
    ipam_by_vmid = {}
    ipam_all_by_vmid = {}

    for alloc in ipam_allocations:
        if alloc.proxmox_server_id and alloc.proxmox_vmid:
            key = (alloc.proxmox_server_id, alloc.proxmox_vmid)
            ipam_all_by_vmid.setdefault(key, []).append(alloc)
            current = ipam_by_vmid.get(key)
            # Основной — тот, у кого стоит флаг; иначе самая ранняя запись
            # (аллокации старше миграции multi-IP флага не имеют).
            if current is None or (alloc.is_primary and not current.is_primary):
                ipam_by_vmid[key] = alloc
        elif alloc.resource_id:
            ipam_by_vmid.setdefault((None, alloc.resource_id), alloc)

    # Build base query for cached VMs (not deleted, not templates)
    # joinedload(owner) avoids an N+1 query when rendering the owner column.
    query = db.query(VMInstance).options(joinedload(VMInstance.owner)).filter(
        VMInstance.deleted_at.is_(None),
        VMInstance.is_template == False
    )

    # Workspace filter: only show VMs from servers in the active workspace
    if workspace_server_ids is not None:
        query = query.filter(VMInstance.server_id.in_(workspace_server_ids))
    
    # VPS-style user isolation: only admins (or roles granting vm:manage) see
    # every instance. Everyone else — including users with no role or a custom
    # role — is restricted to instances they own. See can_view_all_instances.
    if not can_view_all_instances(current_user):
        query = query.filter(VMInstance.owner_id == current_user.id)
    
    cached_vms = query.order_by(VMInstance.name).all()

    # ── Live metrics from Proxmox cluster/resources (CPU, RAM, disk, uptime) ──
    # One HTTP call per cluster returns metrics for all VMs/LXC.
    # Map: (server_id_in_db, vmid) -> live data dict
    live_metrics: dict[tuple[int, int], dict] = {}
    
    # Serve whatever the cache still covers and collect the servers to refresh.
    # The lock guards the dict only — never a network call, otherwise concurrent
    # requests queue up behind each other's Proxmox timeouts.
    stale_servers = []
    now = time_lib.time()
    with _live_metrics_lock:
        for server in servers:
            cached = _live_metrics_cache.get(server.id)
            if cached and (now - cached['time']) < METRICS_TTL:
                for vmid, res in cached['data'].items():
                    live_metrics[(server.id, vmid)] = res
                continue
            # An offline server contributes nothing but a connection timeout —
            # the cached VM rows already carry its last known status.
            if server.is_online is False:
                continue
            stale_servers.append(server)

    def _fetch_live_metrics(server: ProxmoxServer) -> dict[int, dict]:
        """One /cluster/resources call → {vmid: resource}. Runs off the request thread."""
        client = _get_proxmox_client(server)
        return {
            res['vmid']: res
            for res in client.get_cluster_resources(type_='vm')
            if res.get('vmid') is not None
        }

    # Refresh in parallel: one unreachable cluster must not serialise the rest.
    if stale_servers:
        with ThreadPoolExecutor(max_workers=min(METRICS_MAX_WORKERS, len(stale_servers))) as pool:
            futures = {pool.submit(_fetch_live_metrics, s): s for s in stale_servers}
            for future in as_completed(futures):
                server = futures[future]
                try:
                    server_metrics = future.result()
                except Exception as e:
                    logger.debug(f"Could not fetch live metrics from {server.name}: {e}")
                    continue
                for vmid, res in server_metrics.items():
                    live_metrics[(server.id, vmid)] = res
                with _live_metrics_lock:
                    _live_metrics_cache[server.id] = {'time': time_lib.time(), 'data': server_metrics}

    result = []
    
    # Detect cluster servers (servers with hostnames pve1, pve2, pve3 pattern)
    # For cluster servers, show cluster name based on node, not server_id
    cluster_nodes = {}  # node_name -> server
    for server in servers:
        # Match node name to server by hostname (e.g., 'pve1', 'pve2')
        if server.hostname:
            cluster_nodes[server.hostname.lower()] = server
    
    for vm in cached_vms:
        server = server_map.get(vm.server_id)
        server_name = server.name if server else "Unknown"
        
        # For cluster VMs, try to show the correct server based on node name
        node_lower = vm.node.lower() if vm.node else ""
        if node_lower in cluster_nodes:
            actual_server = cluster_nodes[node_lower]
            server_name = actual_server.name
        
        # Get IP from IPAM or cache
        ipam_alloc = (
            ipam_by_vmid.get((vm.server_id, vm.vmid)) or
            ipam_by_vmid.get((None, vm.vmid))
        )
        
        ip_address = ipam_alloc.ip_address if ipam_alloc else (vm.ip_address or "")
        ip_hostname = ipam_alloc.hostname if ipam_alloc else ""
        owner = ipam_alloc.allocated_by if ipam_alloc else ""

        # Полный список адресов гостя: основной первым, дальше алиасы
        guest_allocs = sorted(
            ipam_all_by_vmid.get((vm.server_id, vm.vmid), []),
            key=lambda a: (not bool(a.is_primary), a.id),
        )
        ip_list = [
            {
                "ip": a.ip_address,
                "is_primary": bool(a.is_primary),
                "assignment_kind": a.assignment_kind,
                "target_interface": a.target_interface,
                "apply_status": a.apply_status,
            }
            for a in guest_allocs
        ]
        
        # Get network name from IPAM
        ip_network_name = ""
        if ipam_alloc and ipam_alloc.network_id:
            network = network_map.get(ipam_alloc.network_id)
            if network:
                ip_network_name = network.name
        
        # OS type - prefer a friendly name derived from the CT template file
        # (e.g. "Debian 13" instead of the raw "local:vztmpl/debian-13-...tar.zst"),
        # then fall back to the raw template_name / os_type.
        os_template = (
            pretty_os_from_template(vm.template_name)
            or vm.template_name
            or vm.os_type
            or ("QEMU/KVM" if vm.vm_type == "qemu" else "Linux")
        )
        if not vm.template_name and vm.vm_type == "lxc" and os_template:
            os_template = os_template.capitalize()

        # Live metrics (cpu/mem/disk/uptime/net) from cluster/resources
        live = live_metrics.get((vm.server_id, vm.vmid)) or {}

        result.append({
            "server_id": vm.server_id,
            "server_name": server_name,
            "cluster": server_name,
            "vmid": vm.vmid,
            "name": vm.name,
            "hostname": ip_hostname or f"{'vps' if vm.vm_type == 'qemu' else 'lxc'}{vm.vmid}.{server.hostname if server else 'local'}",
            "type": vm.vm_type,
            "status": live.get('status') or vm.status or "unknown",
            "lock": live.get('lock') or None,
            "node": vm.node,
            "cores": vm.cores or 0,
            "memory": vm.memory or 0,
            "disk": live.get('disk') or 0,
            # ip/ip_address — основной адрес (обратная совместимость),
            # ips — все адреса гостя
            "ip": ip_address,
            "ip_address": ip_address,
            "ips": ip_list,
            "ip_hostname": ip_network_name,
            "os": os_template,
            "os_template": os_template,
            "owner": owner,
            "owner_user": (
                {
                    "username": vm.owner.username,
                    "email": vm.owner.email,
                    "full_name": vm.owner.full_name,
                }
                if vm.owner else None
            ),
            "owner_hostname": "",
            "storage": "Storage1 (DIR)",
            "tags": vm.tags or "",
            # Live runtime metrics
            "cpu": live.get('cpu'),
            "mem": live.get('mem'),
            "maxmem": live.get('maxmem') or vm.memory or 0,
            "maxdisk": live.get('maxdisk') or vm.disk_size or 0,
            "uptime": live.get('uptime'),
            "netin": live.get('netin'),
            "netout": live.get('netout'),
        })
    
    return JSONResponse(content=result)


# ==================== VM/Container Control ====================

@router.post("/api/{server_id}/vm/{vmid}/execute")
def execute_vm_command(
    server_id: int,
    vmid: int,
    node: str,
    command: str = Query(..., description="Команда для выполнения"),
    timeout: int = Query(30, ge=1, le=300, description="Таймаут в секундах"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """
    Выполнить команду на VM через QEMU guest agent
    
    Примеры команд:
    - ls -la /tmp
    - df -h
    - free -m
    - systemctl status nginx
    """
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        result = client.execute_command(node, vmid, command, timeout)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing command on VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/execute-script")
def execute_vm_script(
    server_id: int,
    vmid: int,
    node: str = Query(..., description="Имя ноды Proxmox"),
    script: str = Form(..., description="Содержимое bash скрипта"),
    interpreter: str = Form("/bin/bash", description="Путь к интерпретатору"),
    timeout: int = Query(60, ge=1, le=600, description="Таймаут в секундах"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """
    Выполнить bash скрипт на VM через QEMU guest agent
    
    Скрипт будет сохранен во временный файл на VM и выполнен.
    После выполнения временный файл будет удален.
    """
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        result = client.execute_script(node, vmid, script, interpreter, timeout)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing script on VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Specific VM/Container Actions (must be registered BEFORE generic {action} routes) ====================

class CloneRequest(BaseModel):
    new_name: str
    full: bool = True
    target_node: Optional[PveNode] = None
    target_storage: Optional[str] = None
    description: Optional[str] = None
    owner_id: Optional[int] = None


def _resolve_clone_owner(db: Session, current_user: User, owner_id: Optional[int]) -> int:
    """Resolve the owner for a cloned instance (admins may assign to others)."""
    if owner_id and owner_id != current_user.id:
        is_admin = current_user.has_permission("user:manage") or current_user.is_admin
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admins can assign instance to another user")
        owner = db.query(User).filter(User.id == owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner user not found")
        return owner.id
    return current_user.id


def _check_clone_quota(db: Session, server_id: int, vmid: int, owner_id: int) -> None:
    """A clone is a new instance with the source's resources — count it.

    Deploy, LXC and ISO creation enforced quotas, cloning did not, so a tenant
    could multiply one small allowed VM past every limit.
    """
    from ...services.quota_service import check_quota
    src = get_vm_instance(db, server_id, vmid)
    check_quota(
        db, owner_id,
        add_cores=(src.cores or 0) if src else 0,
        add_memory_mb=(src.memory or 0) if src else 0,
        add_disk_gb=(src.disk_size or 0) if src else 0,
    )


class MigrateRequest(BaseModel):
    target_node: PveNode
    target_storage: Optional[str] = None
    online: bool = False


class RemoteMigrateRequest(BaseModel):
    target_server_id: int
    target_node: PveNode
    target_vmid: Optional[int] = None
    target_storage: Optional[str] = None
    target_bridge: Optional[str] = None
    online: bool = False
    delete_source: bool = True


class ChangePasswordRequest(BaseModel):
    # Fed to chpasswd as "user:password" lines: a newline would add a second
    # entry, a colon in the name would shift the password field.
    username: str = Field(default="root", pattern=r"^[^:\s]{1,64}$")
    password: str = Field(..., min_length=1, max_length=256, pattern=r"^[^\r\n]+$")


def _resolve_server(db: Session, server_id: int) -> ProxmoxServer:
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    return server


def _get_client_or_503(server: ProxmoxServer):
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
    return client


# -------- Clone --------

@router.post("/api/{server_id}/vm/{vmid}/clone", status_code=202)
def clone_vm_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: CloneRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:create"))
):
    """Клонировать существующую VM (qemu) — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_clone_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)
    owner_id = _resolve_clone_owner(db, current_user, body.owner_id)
    _check_clone_quota(db, server_id, vmid, owner_id)

    task = DeployTask(
        kind='clone', name=body.new_name, status='pending', step='В очереди...', progress=0,
        template_id=None, server_id=server_id, user_id=current_user.id, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_clone_sync, task.id, server_id, vmid, node, 'qemu',
        body.new_name, body.full, body.target_node, body.target_storage, body.description,
        current_user.id, current_user.username, owner_id,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=body.new_name)


@router.post("/api/{server_id}/container/{vmid}/clone", status_code=202)
def clone_container_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: CloneRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:create"))
):
    """Клонировать существующий LXC контейнер — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_clone_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)
    owner_id = _resolve_clone_owner(db, current_user, body.owner_id)
    _check_clone_quota(db, server_id, vmid, owner_id)

    task = DeployTask(
        kind='clone', name=body.new_name, status='pending', step='В очереди...', progress=0,
        template_id=None, server_id=server_id, user_id=current_user.id, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_clone_sync, task.id, server_id, vmid, node, 'lxc',
        body.new_name, body.full, body.target_node, body.target_storage, body.description,
        current_user.id, current_user.username, owner_id,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=body.new_name)


# -------- Migrate (move VM/LXC to another cluster node) --------

@router.post("/api/{server_id}/vm/{vmid}/migrate", status_code=202)
def migrate_vm_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: MigrateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:migrate"))
):
    """Мигрировать VM (qemu) на другую ноду — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_migrate_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)
    if body.target_node == node:
        raise HTTPException(status_code=400, detail="Целевая нода совпадает с текущей")

    task = DeployTask(
        kind='migrate', name=f'migrate {vmid} → {body.target_node}', status='pending',
        step='В очереди...', progress=0,
        template_id=None, server_id=server_id, user_id=current_user.id, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_migrate_sync, task.id, server_id, vmid, node, 'qemu',
        body.target_node, body.target_storage, body.online,
        current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=task.name)


@router.post("/api/{server_id}/container/{vmid}/migrate", status_code=202)
def migrate_container_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: MigrateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("lxc:migrate"))
):
    """Мигрировать LXC контейнер на другую ноду — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_migrate_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)
    if body.target_node == node:
        raise HTTPException(status_code=400, detail="Целевая нода совпадает с текущей")

    task = DeployTask(
        kind='migrate', name=f'migrate {vmid} → {body.target_node}', status='pending',
        step='В очереди...', progress=0,
        template_id=None, server_id=server_id, user_id=current_user.id, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_migrate_sync, task.id, server_id, vmid, node, 'lxc',
        body.target_node, body.target_storage, body.online,
        current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=task.name)


# -------- Remote migrate (move VM/LXC to a different, independent cluster) --------

def _require_pve8_plus(client, server_name: str):
    """Remote_migrate API существует только в PVE >= 8.0 — проверяем до сабмита задачи."""
    try:
        version = (client.proxmox.version.get() or {}).get('version', '')
        major = int(str(version).split('.')[0])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось определить версию PVE сервера '{server_name}': {e}")
    if major < 8:
        raise HTTPException(
            status_code=400,
            detail=f"Remote-миграция требует Proxmox VE >= 8.0 (сервер '{server_name}': версия {version})",
        )


@router.post("/api/{server_id}/vm/{vmid}/remote-migrate", status_code=202)
def remote_migrate_vm_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: RemoteMigrateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:remote_migrate"))
):
    """Мигрировать VM (qemu) на другой (независимый) Proxmox-кластер — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_remote_migrate_sync

    require_vm_access(db, current_user, server_id, vmid)
    source_server = _resolve_server(db, server_id)
    if body.target_server_id == server_id:
        raise HTTPException(status_code=400, detail="Для миграции внутри одного кластера используйте обычную миграцию")
    target_server = _resolve_server(db, body.target_server_id)

    _require_pve8_plus(_get_client_or_503(source_server), source_server.name)
    _require_pve8_plus(_get_client_or_503(target_server), target_server.name)

    task = DeployTask(
        kind='remote_migrate',
        name=f'remote migrate {vmid} → {target_server.name}/{body.target_node}',
        status='pending', step='В очереди...', progress=0,
        template_id=None, server_id=server_id, target_server_id=body.target_server_id,
        user_id=current_user.id, node=node, vmid=vmid,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_remote_migrate_sync, task.id, server_id, vmid, node, 'qemu',
        body.target_server_id, body.target_node, body.target_vmid or vmid,
        body.target_storage, body.target_bridge, body.online, body.delete_source,
        current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=task.name)


@router.post("/api/{server_id}/container/{vmid}/remote-migrate", status_code=202)
def remote_migrate_container_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: RemoteMigrateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("lxc:remote_migrate"))
):
    """Мигрировать LXC контейнер на другой (независимый) Proxmox-кластер — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_remote_migrate_sync

    require_vm_access(db, current_user, server_id, vmid)
    source_server = _resolve_server(db, server_id)
    if body.target_server_id == server_id:
        raise HTTPException(status_code=400, detail="Для миграции внутри одного кластера используйте обычную миграцию")
    target_server = _resolve_server(db, body.target_server_id)

    _require_pve8_plus(_get_client_or_503(source_server), source_server.name)
    _require_pve8_plus(_get_client_or_503(target_server), target_server.name)

    task = DeployTask(
        kind='remote_migrate',
        name=f'remote migrate {vmid} → {target_server.name}/{body.target_node}',
        status='pending', step='В очереди...', progress=0,
        template_id=None, server_id=server_id, target_server_id=body.target_server_id,
        user_id=current_user.id, node=node, vmid=vmid,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_remote_migrate_sync, task.id, server_id, vmid, node, 'lxc',
        body.target_server_id, body.target_node, body.target_vmid or vmid,
        body.target_storage, body.target_bridge, body.online, body.delete_source,
        current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=task.name)


# -------- Reinstall (re-clone from saved template) --------

@router.post("/api/{server_id}/vm/{vmid}/reinstall", status_code=202)
def reinstall_vm_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:delete"))
):
    """Переустановить VM/LXC из исходного шаблона — фоновая задача."""
    from ...models import DeployTask, OSTemplate
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_reinstall_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)

    cached = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None),
    ).first()
    if not cached:
        raise HTTPException(status_code=404, detail="VM not found")

    is_lxc = (cached.vm_type == 'lxc')
    tpl = None
    if cached.template_id:
        tpl = db.query(OSTemplate).filter(OSTemplate.id == cached.template_id).first()
        if not tpl or not tpl.vmid:
            raise HTTPException(status_code=400, detail="Template not found or invalid")
    elif cached.template_name and ':' in cached.template_name:
        # LXC created from CT template file (e.g. local:vztmpl/debian-13-...tar.zst)
        # or QEMU VM created from an install ISO (e.g. local:iso/ubuntu-24.04.iso)
        tpl = None
    else:
        raise HTTPException(status_code=400, detail="VM has no associated template; reinstall is not available")

    task = DeployTask(
        kind='reinstall', name=cached.name, status='pending', step='В очереди...', progress=0,
        template_id=tpl.id if tpl else None, server_id=server_id, user_id=current_user.id,
        vmid=vmid, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_reinstall_sync, task.id, server_id, vmid, node,
        current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=cached.name)


# -------- Change Password --------

@router.post("/api/{server_id}/vm/{vmid}/change-password", status_code=202)
def change_vm_password_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """Сменить пароль на VM через QEMU guest agent — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_change_password_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)
    if not body.password or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Password too short")

    cached = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None),
    ).first()
    name = cached.name if cached else f"VM {vmid}"

    task = DeployTask(
        kind='change_password', name=name, status='pending', step='В очереди...', progress=0,
        template_id=None, server_id=server_id, user_id=current_user.id,
        vmid=vmid, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_change_password_sync, task.id, server_id, vmid, node, 'qemu',
        body.username, body.password, current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=name)


@router.post("/api/{server_id}/container/{vmid}/change-password", status_code=202)
def change_container_password_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """Сменить пароль в LXC через pct exec/chpasswd — фоновая задача."""
    from ...models import DeployTask
    from ..templates import _deploy_executor, DeployTaskStartResponse
    from .async_ops import _do_change_password_sync

    require_vm_access(db, current_user, server_id, vmid)
    _resolve_server(db, server_id)
    if not body.password or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Password too short")

    cached = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None),
    ).first()
    name = cached.name if cached else f"CT {vmid}"

    task = DeployTask(
        kind='change_password', name=name, status='pending', step='В очереди...', progress=0,
        template_id=None, server_id=server_id, user_id=current_user.id,
        vmid=vmid, node=node,
    )
    db.add(task); db.commit(); db.refresh(task)

    _deploy_executor.submit(
        _do_change_password_sync, task.id, server_id, vmid, node, 'lxc',
        body.username, body.password, current_user.id, current_user.username,
    )
    return DeployTaskStartResponse(task_id=task.id, status='pending', name=name)


# ==================== Generic VM/Container Action Handler ====================

@router.delete("/api/{server_id}/vm/{vmid}")
async def delete_vm(
    request: Request,
    server_id: int,
    vmid: int,
    node: str,
    force: bool = Query(False, description="Force delete without saving config"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:delete"))
):
    """Удалить VM"""
    from ...i18n import t
    lang = request.cookies.get("language", "en")
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail=t("server_not_found", lang))
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail=t("failed_to_connect", lang))
        
        # Auto-stop running VM before destroy (user already typed the name to confirm)
        status = client.get_vm_status(node, vmid)
        if status and isinstance(status, dict) and status.get('status') == 'running':
            logger.info(f"VM {vmid} is running, stopping before delete...")
            try:
                client.stop_vm(node, vmid, force=True)
            except Exception as _se:
                logger.warning(f"stop_vm({vmid}) failed: {_se}")
            for _ in range(30):
                await asyncio.sleep(0.5)
                st = client.get_vm_status(node, vmid)
                if not st or not isinstance(st, dict) or st.get('status') != 'running':
                    break
            else:
                raise HTTPException(status_code=400, detail=t("cannot_delete_running_vm", lang))
        
        # Сохраняем конфигурацию VM в базу перед удалением (если не force)
        if not force:
            config = client.get_vm_config(node, vmid)
            interfaces = client.get_vm_interfaces(node, vmid)
            
            # Проверяем что config - это словарь
            if not isinstance(config, dict):
                config = {}
            
            # Извлекаем IP адрес из интерфейсов
            ip_address = None
            ip_prefix = 24
            if interfaces and isinstance(interfaces, list):
                for iface in interfaces:
                    if isinstance(iface, dict) and iface.get('ips'):
                        for ip_info in iface['ips']:
                            if isinstance(ip_info, dict) and ip_info.get('type') == 'ipv4':
                                ip_address = ip_info.get('address')
                                ip_prefix = ip_info.get('prefix', 24)
                                break
                    if ip_address:
                        break
            
            # Сохраняем в базу
            try:
                bootdisk = config.get('bootdisk', 'scsi0')
                disk_info = config.get(bootdisk, {})
                disk_size = None
                if isinstance(disk_info, dict):
                    size_str = disk_info.get('size', '0G')
                    disk_size = int(size_str.replace('G', '')) if isinstance(size_str, str) else None

                # save_vm_instance() безусловно перезаписывает template_id/template_name
                # у уже существующей записи; этот вызов их не знает (Proxmox config их не
                # содержит), поэтому явно переносим текущие значения — иначе привязка к
                # шаблону теряется навсегда, если удаление после этого не состоится
                # (VM осталась, а "переустановить из шаблона" сломалась).
                prev_instance = db.query(VMInstance).filter(
                    VMInstance.server_id == server_id,
                    VMInstance.vmid == vmid,
                    VMInstance.deleted_at.is_(None),
                ).first()

                save_vm_instance(
                    db=db,
                    server_id=server_id,
                    vmid=vmid,
                    node=node,
                    vm_type='qemu',
                    name=config.get('name', f'VM-{vmid}'),
                    cores=config.get('cores'),
                    memory=config.get('memory'),
                    disk_size=disk_size,
                    ip_address=ip_address,
                    ip_prefix=ip_prefix,
                    description=config.get('description'),
                    template_id=prev_instance.template_id if prev_instance else None,
                    template_name=prev_instance.template_name if prev_instance else None,
                )
                logger.info(f"Saved VM {vmid} configuration before deletion")
            except Exception as e:
                logger.warning(f"Failed to save VM config before deletion: {e}")
        
        # Archive and delete snapshots before deleting VM
        vm_name = status.get('name') if isinstance(status, dict) else f'VM-{vmid}'
        snapshot_result = archive_and_delete_snapshots(
            db=db,
            client=client,
            server_id=server_id,
            server_name=server.name,
            vmid=vmid,
            vm_name=vm_name,
            vm_type='qemu',
            node=node,
            deleted_by=current_user.username,
            deletion_reason=f"VM {vmid} deleted by {current_user.username}"
        )
        
        if snapshot_result["archived"] > 0:
            logger.info(f"Archived {snapshot_result['archived']} snapshots before deleting VM {vmid}")
        if snapshot_result["errors"]:
            logger.warning(f"Snapshot cleanup errors for VM {vmid}: {snapshot_result['errors']}")
        
        # Удаляем VM
        result = client.delete_vm(node, vmid)
        if result:
            # Помечаем запись в локальном кэше удалённой сразу, не дожидаясь
            # фонового sync_vm_cache (тикает раз в 10с) — иначе VM ещё
            # какое-то время висит в списке после реального удаления.
            try:
                soft_delete_vm_instance(db, server_id, vmid)
            except Exception as _sde:
                logger.warning(f"Failed to soft-delete cached VM {vmid}: {_sde}")
            # Освобождаем IP в IPAM (если есть)
            try:
                ipam = IPAMService(db)
                released, released_ip = ipam.release_ip_by_vmid(
                    proxmox_server_id=server_id,
                    proxmox_vmid=vmid,
                    released_by=current_user.username,
                    reason=f"VM {vmid} deleted"
                )
                if released:
                    logger.info(f"Auto-released IPAM allocation for IP {released_ip} after VM {vmid} deletion")
            except Exception as e:
                logger.warning(f"Failed to release IPAM allocation for VM {vmid}: {e}")

            # Убираем «хвост» App Store в «Мои приложения» (если инстанс был установлен из каталога)
            _cleanup_installed_apps(db, server_id, vmid)

            # Register ProxmoxTask for delete tracking
            try:
                from datetime import datetime, timezone as _tz
                _ptask = ProxmoxTaskService.register(
                    db=db, upid=f"delete-vm-{vmid}-{server_id}", server_id=server_id,
                    user_id=current_user.id, action='delete',
                    node=node, vmid=vmid, vm_type='qemu',
                    description=f"Удаление VM {vm_name or vmid}",
                )
                _ptask.status = 'completed'
                _ptask.exit_status = 'OK'
                _ptask.completed_at = datetime.now(_tz.utc)
                db.commit()
            except Exception as _te:
                logger.warning(f"Failed to register ProxmoxTask for VM {vmid} delete: {_te}")
            # Log successful deletion
            LoggingService.log_proxmox_action(
                db=db,
                action="delete",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=vm_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={
                    "force": force,
                    "snapshots_archived": snapshot_result["archived"],
                    "snapshots_deleted": snapshot_result["deleted"],
                    "snapshot_names": snapshot_result["snapshots"]
                },
                success=True
            )
            logger.info(f"User {current_user.username} deleted VM {vmid} on {server.name}")
            try:
                from ...websocket_manager import broadcast_event
                broadcast_event("vm_deleted", server_id=server_id, vmid=vmid, node=node, name=vm_name, vm_type="qemu")
            except Exception as _we:
                logger.warning(f"Failed to broadcast vm_deleted for VM {vmid}: {_we}")
            return JSONResponse(content={
                "status": "success",
                "message": f"VM {vmid} удалена",
                "snapshots_archived": snapshot_result["archived"],
                "snapshots_deleted": snapshot_result["deleted"]
            })
        else:
            LoggingService.log_proxmox_action(
                db=db,
                action="delete",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=False,
                error_message="Не удалось удалить VM"
            )
            raise HTTPException(status_code=500, detail="Не удалось удалить VM")
    except HTTPException:
        raise
    except Exception as e:
        LoggingService.log_proxmox_action(
            db=db,
            action="delete",
            resource_type="vm",
            resource_id=vmid,
            username=current_user.username,
            server_id=server_id if server else None,
            server_name=server.name if server else None,
            node_name=node,
            success=False,
            error_message=str(e)
        )
        logger.error(f"Error deleting VM {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== High Availability (HA) Endpoints ====================

@router.get("/api/{server_id}/ha/status", dependencies=[Depends(enforce_server_visibility)])
def get_ha_status(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить статус HA кластера и список ресурсов в HA"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        is_cluster = client.is_cluster()
        ha_resources = client.get_ha_resources() if is_cluster else []
        ha_groups = client.get_ha_groups() if is_cluster else []
        
        return JSONResponse(content={
            "server_id": server_id,
            "is_cluster": is_cluster,
            "ha_enabled": is_cluster,
            "resources": ha_resources,
            "groups": ha_groups
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting HA status for {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/ha/{vm_type}/{vmid}")
def get_resource_ha_status(
    server_id: int,
    vm_type: str,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить HA статус конкретной VM или контейнера"""
    require_vm_access(db, current_user, server_id, vmid)
    if vm_type not in ['vm', 'ct']:
        raise HTTPException(status_code=400, detail="vm_type must be 'vm' or 'ct'")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        is_cluster = client.is_cluster()
        
        if not is_cluster:
            return JSONResponse(content={
                "vmid": vmid,
                "vm_type": vm_type,
                "is_cluster": False,
                "ha_available": False,
                "in_ha": False
            })
        
        ha_status = client.get_ha_status(vmid, vm_type)
        
        return JSONResponse(content={
            "vmid": vmid,
            "vm_type": vm_type,
            "is_cluster": True,
            "ha_available": True,
            **ha_status
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting HA status for {vm_type}:{vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/ha/{vm_type}/{vmid}/add")
def add_resource_to_ha(
    server_id: int,
    vm_type: str,
    vmid: int,
    group: str = Query(None, description="HA group name"),
    max_restart: int = Query(1, description="Max restart attempts"),
    max_relocate: int = Query(1, description="Max relocate attempts"),
    state: str = Query("started", description="Target state: started, stopped, enabled, disabled, ignored"),
    comment: str = Query(None, description="Comment"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Добавить VM или контейнер в HA"""
    require_vm_access(db, current_user, server_id, vmid)
    if vm_type not in ['vm', 'ct']:
        raise HTTPException(status_code=400, detail="vm_type must be 'vm' or 'ct'")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        # Проверяем что это кластер
        if not client.is_cluster():
            raise HTTPException(status_code=400, detail="HA is only available in cluster mode")
        
        result = client.add_to_ha(
            vmid=vmid,
            vm_type=vm_type,
            group=group,
            max_restart=max_restart,
            max_relocate=max_relocate,
            state=state,
            comment=comment
        )
        
        if result.get('success'):
            # Log action
            resource_type = "vm" if vm_type == "vm" else "container"
            LoggingService.log_proxmox_action(
                db=db,
                action="add_to_ha",
                resource_type=resource_type,
                resource_id=vmid,
                username=current_user.username,
                server_id=server_id,
                server_name=server.name,
                details={"group": group, "state": state},
                success=True
            )
            logger.info(f"User {current_user.username} added {vm_type}:{vmid} to HA on {server.name}")
            return JSONResponse(content=result)
        else:
            if result.get('already_in_ha'):
                raise HTTPException(status_code=409, detail="Resource is already in HA")
            raise HTTPException(status_code=500, detail=result.get('error', 'Failed to add to HA'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding {vm_type}:{vmid} to HA: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/ha/{vm_type}/{vmid}/remove")
def remove_resource_from_ha(
    server_id: int,
    vm_type: str,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Удалить VM или контейнер из HA"""
    require_vm_access(db, current_user, server_id, vmid)
    if vm_type not in ['vm', 'ct']:
        raise HTTPException(status_code=400, detail="vm_type must be 'vm' or 'ct'")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        result = client.remove_from_ha(vmid, vm_type)
        
        if result.get('success'):
            # Log action
            resource_type = "vm" if vm_type == "vm" else "container"
            LoggingService.log_proxmox_action(
                db=db,
                action="remove_from_ha",
                resource_type=resource_type,
                resource_id=vmid,
                username=current_user.username,
                server_id=server_id,
                server_name=server.name,
                success=True
            )
            logger.info(f"User {current_user.username} removed {vm_type}:{vmid} from HA on {server.name}")
            return JSONResponse(content=result)
        else:
            if result.get('not_in_ha'):
                raise HTTPException(status_code=404, detail="Resource is not in HA")
            raise HTTPException(status_code=500, detail=result.get('error', 'Failed to remove from HA'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing {vm_type}:{vmid} from HA: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/container/{vmid}")
async def delete_container(
    request: Request,
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:delete"))
):
    """Удалить LXC контейнер"""
    from ...i18n import t
    lang = request.cookies.get("language", "en")
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail=t("server_not_found", lang))
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail=t("failed_to_connect", lang))
        
        # Auto-stop running container before destroy (user already typed the name to confirm)
        status = client.get_container_status(node, vmid)
        if status and isinstance(status, dict) and status.get('status') == 'running':
            logger.info(f"Container {vmid} is running, stopping before delete...")
            try:
                client.stop_container(node, vmid, force=True)
            except Exception as _se:
                logger.warning(f"stop_container({vmid}) failed: {_se}")
            # Wait up to 15s for it to actually stop
            for _ in range(30):
                await asyncio.sleep(0.5)
                st = client.get_container_status(node, vmid)
                if not st or not isinstance(st, dict) or st.get('status') != 'running':
                    break
            else:
                raise HTTPException(status_code=400, detail=t("cannot_delete_running_container", lang))
        
        # Archive and delete snapshots before deleting container
        container_name = status.get('name') if isinstance(status, dict) else f'CT-{vmid}'
        snapshot_result = archive_and_delete_snapshots(
            db=db,
            client=client,
            server_id=server_id,
            server_name=server.name,
            vmid=vmid,
            vm_name=container_name,
            vm_type='lxc',
            node=node,
            deleted_by=current_user.username,
            deletion_reason=f"Container {vmid} deleted by {current_user.username}"
        )
        
        if snapshot_result["archived"] > 0:
            logger.info(f"Archived {snapshot_result['archived']} snapshots before deleting container {vmid}")
        if snapshot_result["errors"]:
            logger.warning(f"Snapshot cleanup errors for container {vmid}: {snapshot_result['errors']}")
        
        # Удаляем контейнер
        result = client.delete_container(node, vmid)
        if result:
            # Помечаем запись в локальном кэше удалённой сразу, не дожидаясь
            # фонового sync_vm_cache (тикает раз в 10с) — иначе контейнер ещё
            # какое-то время висит в списке после реального удаления.
            try:
                soft_delete_vm_instance(db, server_id, vmid)
            except Exception as _sde:
                logger.warning(f"Failed to soft-delete cached container {vmid}: {_sde}")
            # Освобождаем IP в IPAM (если есть)
            try:
                ipam = IPAMService(db)
                released, released_ip = ipam.release_ip_by_vmid(
                    proxmox_server_id=server_id,
                    proxmox_vmid=vmid,
                    released_by=current_user.username,
                    reason=f"Container {vmid} deleted"
                )
                if released:
                    logger.info(f"Auto-released IPAM allocation for IP {released_ip} after container {vmid} deletion")
            except Exception as e:
                logger.warning(f"Failed to release IPAM allocation for container {vmid}: {e}")

            # Убираем «хвост» App Store в «Мои приложения» (если контейнер был установлен из каталога)
            _cleanup_installed_apps(db, server_id, vmid)

            # Register ProxmoxTask for delete tracking
            try:
                ProxmoxTaskService.register(
                    db=db, upid=f"delete-ct-{vmid}-{server_id}", server_id=server_id,
                    user_id=current_user.id, action='delete',
                    node=node, vmid=vmid, vm_type='lxc',
                    description=f"Удаление LXC {container_name or vmid}",
                )
            except Exception as _te:
                logger.warning(f"Failed to register ProxmoxTask for container {vmid} delete: {_te}")
            # Log deletion with snapshot info
            LoggingService.log_proxmox_action(
                db=db,
                action="delete",
                resource_type="container",
                resource_id=vmid,
                username=current_user.username,
                resource_name=container_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={
                    "snapshots_archived": snapshot_result["archived"],
                    "snapshots_deleted": snapshot_result["deleted"],
                    "snapshot_names": snapshot_result["snapshots"]
                },
                success=True
            )
            
            logger.info(f"User {current_user.username} deleted container {vmid} on {server.name}")
            try:
                from ...websocket_manager import broadcast_event
                broadcast_event("vm_deleted", server_id=server_id, vmid=vmid, node=node, name=container_name, vm_type="lxc")
            except Exception as _we:
                logger.warning(f"Failed to broadcast vm_deleted for container {vmid}: {_we}")
            return JSONResponse(content={
                "status": "success",
                "message": f"Контейнер {vmid} удалён",
                "snapshots_archived": snapshot_result["archived"],
                "snapshots_deleted": snapshot_result["deleted"]
            })
        else:
            raise HTTPException(status_code=500, detail="Не удалось удалить контейнер")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting container {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/vm/{vmid}/config")
def get_vm_config(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить конфигурацию VM"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        config = client.get_vm_config(node, vmid)
        return JSONResponse(content=config)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VM {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/nodes/{node}/hardware/pci", dependencies=[Depends(enforce_server_visibility)])
def list_node_pci_devices(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Список PCI-устройств ноды для passthrough (hostpciN)."""
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    try:
        return JSONResponse(content=client.get_node_pci_devices(node))
    except Exception as e:
        logger.error(f"Error listing PCI devices on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/nodes/{node}/hardware/usb", dependencies=[Depends(enforce_server_visibility)])
def list_node_usb_devices(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Список USB-устройств ноды для passthrough (usbN)."""
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    try:
        return JSONResponse(content=client.get_node_usb_devices(node))
    except Exception as e:
        logger.error(f"Error listing USB devices on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/{server_id}/vm/{vmid}/config")
async def update_vm_config(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Обновить конфигурацию VM (CPU, Memory, etc.)"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.update_vm_config(node, vmid, data)
        if success:
            logger.info(f"User {current_user.username} updated VM {vmid} config on {server.name}")
            return JSONResponse(content={"status": "success", "message": "Конфигурация обновлена"})
        else:
            raise HTTPException(status_code=500, detail="Не удалось обновить конфигурацию")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating VM {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/disk/resize")
async def resize_vm_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Изменить размер диска VM"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        data = await request.json()
        disk = data.get('disk')
        size = data.get('size')
        
        if not disk or not size:
            raise HTTPException(status_code=400, detail="Требуются параметры disk и size")
        
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.resize_vm_disk(node, vmid, disk, size)
        if success:
            # Обновляем размер диска в базе данных
            vm_instance = get_vm_instance(db, server_id, vmid)
            if vm_instance:
                # Конвертируем размер в GB (размер приходит как "32G")
                size_gb = int(size.rstrip('GMgm').strip())
                vm_instance.disk_size = size_gb
                vm_instance.updated_at = func.now()
                db.commit()
                logger.info(f"Updated disk size in database: VM {vmid} -> {size_gb}GB")
            
            logger.info(f"User {current_user.username} resized disk {disk} of VM {vmid} to {size}")

            # Автоматически расширяем раздел и ФС внутри гостя (best-effort).
            # Требует qemu-guest-agent и growpart в гостевой ОС; при их
            # отсутствии ресайз диска всё равно считается успешным.
            grow = client.grow_vm_filesystem(node, vmid)
            grew = bool(grow.get('changed'))
            # Включаем и error: если вызов guest agent упал с исключением,
            # stdout/stderr пустые, и без error пользователь видел пустой блок.
            grow_output = (
                (grow.get('stdout', '') or '')
                + (grow.get('stderr', '') or '')
                + (grow.get('error', '') or '')
            ).strip() or 'growpart не вернул вывода (qemu-guest-agent не отвечает?)'
            if grew:
                message = f"Размер диска {disk} изменен на {size}. Файловая система автоматически расширена."
            else:
                message = (
                    f"Размер диска {disk} изменен на {size}, но файловую систему "
                    f"расширить не удалось — см. вывод growpart."
                )

            return JSONResponse(content={
                "status": "success",
                "message": message,
                "filesystem_grown": grew,
                "grow_output": grow_output,
            })
        else:
            raise HTTPException(status_code=500, detail="Не удалось изменить размер диска")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resizing VM {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/disk/move")
async def move_vm_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Переместить диск VM в другое хранилище (move_disk)."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    disk = data.get('disk')
    target_storage = data.get('target_storage')
    if not disk or not target_storage:
        raise HTTPException(status_code=400, detail="Требуются параметры disk и target_storage")

    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        result = client.move_vm_disk(
            node, vmid, disk, target_storage,
            delete=bool(data.get('delete', True)),
            target_format=data.get('format') or None,
        )
        if result.get('success'):
            logger.info(f"User {current_user.username} moved disk {disk} of VM {vmid} to {target_storage}")
            return JSONResponse(content={"status": "success", "upid": result.get('upid')})
        raise HTTPException(status_code=400, detail=result.get('error', 'Failed'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error moving VM {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/disk/add")
async def add_vm_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Добавить новый диск VM."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    disk = data.get('disk')
    storage = data.get('storage')
    size_gb = data.get('size_gb')
    if not disk or not storage or not size_gb:
        raise HTTPException(status_code=400, detail="Требуются параметры disk, storage и size_gb")

    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        result = client.add_vm_disk(
            node, vmid, disk, storage, int(size_gb),
            ssd=bool(data.get('ssd', False)),
            discard=bool(data.get('discard', False)),
            iothread=bool(data.get('iothread', False)),
        )
        if result.get('success'):
            logger.info(f"User {current_user.username} added disk {disk} ({size_gb}G) to VM {vmid}")
            return JSONResponse(content={"status": "success"})
        raise HTTPException(status_code=400, detail=result.get('error', 'Failed'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding disk to VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/disk/attach-physical")
async def attach_vm_physical_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Пробросить физический диск ноды в VM (raw device passthrough)."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    disk = data.get('disk')
    devpath = data.get('devpath')
    if not disk or not devpath:
        raise HTTPException(status_code=400, detail="Требуются параметры disk и devpath")

    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        result = client.attach_vm_physical_disk(
            node, vmid, disk, devpath,
            aio=data.get('aio'),
            discard=bool(data.get('discard', False)),
            ssd=bool(data.get('ssd', False)),
            serial=data.get('serial') or None,
        )
        if result.get('success'):
            logger.info(f"User {current_user.username} passed physical disk {devpath} as {disk} to VM {vmid}")
            return JSONResponse(content={"status": "success"})
        raise HTTPException(status_code=400, detail=result.get('error', 'Failed'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error attaching physical disk to VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/disk/detach")
async def detach_vm_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Отключить (или удалить) диск VM."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    disk = data.get('disk')
    if not disk:
        raise HTTPException(status_code=400, detail="Требуется параметр disk")

    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        result = client.detach_vm_disk(node, vmid, disk, destroy=bool(data.get('destroy', False)))
        if result.get('success'):
            logger.info(f"User {current_user.username} detached disk {disk} of VM {vmid}")
            return JSONResponse(content={"status": "success"})
        raise HTTPException(status_code=400, detail=result.get('error', 'Failed'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error detaching VM {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/unlock")
def unlock_vm_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Снять блокировку (lock) с VM — аналог qm unlock."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        result = client.unlock_vm(node, vmid)
        if result.get("success"):
            logger.info(f"User {current_user.username} unlocked VM {vmid} on {server.name}")
            return JSONResponse(content={"status": "success"})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unlocking VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/unlock")
def unlock_container_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Снять блокировку (lock) с LXC — аналог pct unlock."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        result = client.unlock_container(node, vmid)
        if result.get("success"):
            logger.info(f"User {current_user.username} unlocked container {vmid} on {server.name}")
            return JSONResponse(content={"status": "success"})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unlocking container {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/cloud-init")
async def update_vm_cloud_init(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Обновить cloud-init параметры VM (user/password/SSH/IP/DNS)."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        result = client.update_cloud_init(
            node, vmid,
            ciuser=data.get('ciuser'),
            cipassword=data.get('cipassword'),
            sshkeys=data.get('sshkeys'),
            ipconfig0=data.get('ipconfig0'),
            nameserver=data.get('nameserver'),
            searchdomain=data.get('searchdomain'),
        )
        if result.get('success'):
            logger.info(f"User {current_user.username} updated cloud-init of VM {vmid} on {server.name}")
            return JSONResponse(content={"status": "success"})
        raise HTTPException(status_code=400, detail=result.get('error', 'Failed'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating cloud-init for VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/serial/enable")
def enable_vm_serial(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Добавить serial0 (socket) в конфиг VM, если его нет — для serial-консоли.

    Возвращает added=True, если устройство было только что добавлено (тогда для
    появления в гостевой ОС может потребоваться перезагрузка VM).
    """
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        cfg = client.get_vm_config(node, vmid) or {}
        present = any(k.startswith("serial") for k in cfg)
        added = False
        if not present:
            client.update_vm_config(node, vmid, {"serial0": "socket"})
            added = True
            logger.info(f"User {current_user.username} enabled serial0 on VM {vmid}")
        return JSONResponse(content={"status": "success", "added": added, "present": True})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error enabling serial for VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/disk/resize")
async def resize_container_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Изменить размер диска контейнера"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        data = await request.json()
        disk = data.get('disk', 'rootfs')
        size = data.get('size')
        
        if not size:
            raise HTTPException(status_code=400, detail="Требуется параметр size")
        
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.resize_container_disk(node, vmid, disk, size)
        if success:
            # Обновляем размер диска в базе данных
            vm_instance = get_vm_instance(db, server_id, vmid)
            if vm_instance:
                # Конвертируем размер в GB (размер приходит как "32G")
                size_gb = int(size.rstrip('GMgm').strip())
                vm_instance.disk_size = size_gb
                vm_instance.updated_at = func.now()
                db.commit()
                logger.info(f"Updated disk size in database: Container {vmid} -> {size_gb}GB")
            
            # Перезапускаем контейнер для применения изменений
            restart_success = client.restart_container(node, vmid)
            if restart_success:
                logger.info(f"Container {vmid} restarted to apply disk resize")
            
            logger.info(f"User {current_user.username} resized disk {disk} of container {vmid} to {size}")
            return JSONResponse(content={
                "status": "success", 
                "message": f"Размер диска {disk} изменен на {size}. Контейнер перезапускается для применения изменений."
            })
        else:
            raise HTTPException(status_code=500, detail="Не удалось изменить размер диска")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resizing container {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/disk/move")
async def move_container_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Переместить том контейнера в другое хранилище (move_volume)."""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    disk = data.get('disk')
    target_storage = data.get('target_storage')
    if not disk or not target_storage:
        raise HTTPException(status_code=400, detail="Требуются параметры disk и target_storage")

    try:
        client = _get_proxmox_client(server)
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        result = client.move_container_disk(
            node, vmid, disk, target_storage,
            delete=bool(data.get('delete', True)),
        )
        if result.get('success'):
            logger.info(f"User {current_user.username} moved volume {disk} of container {vmid} to {target_storage}")
            return JSONResponse(content={"status": "success", "upid": result.get('upid')})
        raise HTTPException(status_code=400, detail=result.get('error', 'Failed'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error moving container {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/config")
def get_container_config(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить конфигурацию LXC контейнера"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        config = client.get_container_config(node, vmid)
        return JSONResponse(content=config)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting container {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/{server_id}/container/{vmid}/config")
async def update_container_config(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Обновить конфигурацию LXC контейнера"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.update_container_config(node, vmid, data)
        if success:
            logger.info(f"User {current_user.username} updated container {vmid} config on {server.name}")
            return JSONResponse(content={"status": "success", "message": "Конфигурация обновлена"})
        else:
            raise HTTPException(status_code=500, detail="Не удалось обновить конфигурацию")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating container {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/nodes", dependencies=[Depends(enforce_server_visibility)])
def get_server_nodes(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """Получить список нод Proxmox сервера вместе с идентификатором кластера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)

        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        nodes = client.get_nodes()

        # Определяем идентификатор кластера для корректной дедупликации на фронте.
        # Если сервер входит в кластер — используем имя кластера (все члены одного
        # кластера вернут одинаковое значение). Для standalone — используем уникальный
        # "standalone-{server_id}", чтобы независимые серверы с одинаковыми именами
        # нод не скрывали друг друга.
        cluster_id = f"standalone-{server_id}"
        try:
            cluster_status = client.proxmox.cluster.status.get()
            for item in cluster_status:
                if item.get('type') == 'cluster' and item.get('name'):
                    cluster_id = item['name']
                    break
        except Exception:
            pass

        # Версия Proxmox VE (общая для кластера), например "8.2.4".
        version = None
        try:
            ver = client.proxmox.version.get()
            if ver:
                version = ver.get("version")
        except Exception:
            pass

        logger.info(f"Nodes for server {server_id} ({server.name}), cluster_id={cluster_id}: {len(nodes)} node(s)")
        return JSONResponse(content={"nodes": nodes, "cluster_id": cluster_id, "version": version})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting nodes from {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== VM/Container Monitoring ====================

@router.get("/api/{server_id}/vm/{vmid}/status")
def get_vm_status(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить детальный статус VM (CPU, RAM, Disk, Network)"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)

        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        status = client.get_vm_status(node, vmid)
        return JSONResponse(content=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VM {vmid} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/status")
def get_container_status(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить детальный статус LXC контейнера"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)

        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")

        status = client.get_container_status(node, vmid)
        return JSONResponse(content=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting container {vmid} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/vm/{vmid}/interfaces")
def get_vm_interfaces(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить сетевые интерфейсы и IP адреса VM через QEMU guest agent"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        interfaces = client.get_vm_interfaces(node, vmid)
        return JSONResponse(content={"interfaces": interfaces})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VM {vmid} interfaces: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/interfaces")
def get_container_interfaces(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить сетевые интерфейсы и IP адреса контейнера"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        interfaces = client.get_container_interfaces(node, vmid)
        return JSONResponse(content={"interfaces": interfaces})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting container {vmid} interfaces: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/status")
def get_container_status_api(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить статус контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_container_status(node, vmid)
        return JSONResponse(content=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting container {vmid} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/vm/{vmid}/rrddata")
def get_vm_rrddata(
    server_id: int,
    vmid: int,
    node: str,
    timeframe: str = Query("hour", regex="^(hour|day|week|month|year)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить исторические данные VM для графиков (CPU, RAM, Network, Disk IO)"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        rrddata = client.get_vm_rrddata(node, vmid, timeframe)
        return JSONResponse(content=rrddata)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VM {vmid} RRD data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/rrddata")
def get_container_rrddata(
    server_id: int,
    vmid: int,
    node: str,
    timeframe: str = Query("hour", regex="^(hour|day|week|month|year)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить исторические данные контейнера для графиков"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        rrddata = client.get_container_rrddata(node, vmid, timeframe)
        return JSONResponse(content=rrddata)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting container {vmid} RRD data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Metrics History (DB-only) ====================

def _resolve_metrics_window(timeframe, from_ts, to_ts):
    from ...config import settings
    now = int(time_lib.time())
    if from_ts is not None and to_ts is not None:
        f, t = int(from_ts), int(to_ts)
    else:
        f, t = timeframe_to_range(timeframe, now)
    max_span = getattr(settings, "METRICS_RETENTION_DAYS", 30) * 86400
    if t - f > max_span:
        f = t - max_span
    return f, t


@router.get("/api/{server_id}/vm/{vmid}/metrics")
def get_vm_metrics(
    server_id: int,
    vmid: int,
    node: str = Query(None),
    timeframe: str = Query("hour", regex="^(hour|day|week|month)$"),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
    nic: str = Query("all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view")),
):
    """Retrieve VM metrics time-series from the local DB (no Proxmox call)."""
    require_vm_access(db, current_user, server_id, vmid)
    f, t = _resolve_metrics_window(timeframe, from_ts, to_ts)
    return query_instance_metrics(db, server_id, vmid, f, t, nic)


@router.get("/api/{server_id}/container/{vmid}/metrics")
def get_container_metrics(
    server_id: int,
    vmid: int,
    node: str = Query(None),
    timeframe: str = Query("hour", regex="^(hour|day|week|month)$"),
    from_ts: int = Query(None),
    to_ts: int = Query(None),
    nic: str = Query("all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view")),
):
    """Retrieve container metrics time-series from the local DB (no Proxmox call)."""
    require_vm_access(db, current_user, server_id, vmid)
    f, t = _resolve_metrics_window(timeframe, from_ts, to_ts)
    return query_instance_metrics(db, server_id, vmid, f, t, nic)


# ==================== VNC Console ====================

@router.get("/api/{server_id}/vm/{vmid}/vnc")
def get_vm_vnc(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """Получить VNC данные для подключения к VM"""
    import requests
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Для VNC ОБЯЗАТЕЛЬНО нужен password auth, API token не работает с vncwebsocket
        # Получаем auth ticket и создаём VNC proxy в одной сессии
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="VNC requires password authentication. Please add password to server settings.")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{pve_authority(server)}/api2/json/access/ticket",
            data={
                "username": auth_username,
                "password": password_to_use
            },
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём VNC proxy с этим же ticket
        # generate-password=1: Proxmox 8.x возвращает случайный VNC пароль для VNCAuth;
        # в Proxmox 9.x (NoAuth) поле password пустое, но соединение проходит без пароля.
        vnc_response = requests.post(
            f"https://{pve_authority(server)}/api2/json/nodes/{node}/qemu/{vmid}/vncproxy",
            data={"websocket": 1, "generate-password": 1},
            headers={
                "CSRFPreventionToken": csrf_token
            },
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if vnc_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create VNC proxy: {vnc_response.text}")
        
        vnc_data = vnc_response.json().get("data", {})
        
        # password = сгенерированный VNC пароль (Proxmox 8.x) или пустой (Proxmox 9.x NoAuth)
        # Если password не вернулся — используем ticket как пароль (совместимость)
        vnc_password = vnc_data.get('password') or vnc_data.get('ticket', '')
        response_data = {
            'port': vnc_data.get('port'),
            'ticket': vnc_data.get('ticket'),
            'password': vnc_password,
            'node': node,
            'vmid': vmid,
            'type': 'qemu',
            # Opaque handle, redeemed by the WS proxy — never the PVE ticket itself.
            'auth_ticket': stash_pve_ticket(auth_ticket, current_user.id, server_id, vmid),
        }
        
        logger.info(f"User {current_user.username} opened VNC console for VM {vmid} (password_len={len(vnc_password)}, has_generate_password={'password' in vnc_data})")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VNC for VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/vnc")
def get_container_vnc(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """Получить VNC данные для подключения к LXC контейнеру"""
    import requests
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Для VNC ОБЯЗАТЕЛЬНО нужен password auth
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="VNC requires password authentication. Please add password to server settings.")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{pve_authority(server)}/api2/json/access/ticket",
            data={
                "username": auth_username,
                "password": password_to_use
            },
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём VNC proxy с этим же ticket
        # LXC vncproxy не поддерживает generate-password; используем ticket как VNCAuth пароль
        vnc_response = requests.post(
            f"https://{pve_authority(server)}/api2/json/nodes/{node}/lxc/{vmid}/vncproxy",
            data={"websocket": 1},
            headers={
                "CSRFPreventionToken": csrf_token
            },
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if vnc_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create VNC proxy: {vnc_response.text}")
        
        vnc_data = vnc_response.json().get("data", {})
        
        # Для LXC: VNCAuth пароль = ticket (стандартный Proxmox подход для LXC)
        vnc_password = vnc_data.get('ticket', '')
        response_data = {
            'port': vnc_data.get('port'),
            'ticket': vnc_data.get('ticket'),
            'password': vnc_password,
            'node': node,
            'vmid': vmid,
            'type': 'lxc',
            # Opaque handle, redeemed by the WS proxy — never the PVE ticket itself.
            'auth_ticket': stash_pve_ticket(auth_ticket, current_user.id, server_id, vmid),
        }
        
        logger.info(f"User {current_user.username} opened VNC console for container {vmid}")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VNC for container {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== VNC WebSocket Proxy ====================

@router.websocket("/ws/vnc/{server_id}/{node}/{vmtype}/{vmid}")
async def vnc_websocket_proxy(
    websocket: WebSocket,
    server_id: int,
    node: str,
    vmtype: str,
    vmid: int,
    port: int,
    vncticket: str,
    vnc_password: str = None,  # VNC пароль сгенерированный через generate-password
    auth_ticket: str = None,  # Auth ticket переданный с frontend
    ticket: str = Query(None),  # WS-тикет панели (POST /api/auth/ws-ticket)
    db: Session = Depends(get_db)
):
    """WebSocket прокси для VNC подключения к Proxmox"""
    import websockets
    import urllib.parse

    # --- Authenticate the panel user before bridging to Proxmox ------------
    user, ws_session = authenticate_ws_session(ticket, db)
    if not user:
        await websocket.close(code=4001, reason="Authentication required")
        return

    def _still_allowed(db_, u):
        return PermissionEngine.has_permission(u, "vm:console") and check_vm_access(db_, u, server_id, vmid)

    if not _still_allowed(db, user):
        await websocket.close(code=4003, reason="Access denied")
        return

    await websocket.accept()
    logger.info(f"VNC WebSocket connection accepted for {vmtype}/{vmid} (user={user.username})")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        logger.error(f"Proxmox server {server_id} not found")
        await websocket.close(code=1008, reason="Proxmox server not found")
        return
    
    # auth_ticket is the handle from GET .../vnc; the PVE ticket behind it was
    # created in the same session as vncticket.
    if auth_ticket:
        auth_ticket = claim_pve_ticket(auth_ticket, user.id, server_id, vmid)
        if not auth_ticket:
            await websocket.close(code=4003, reason="Console session expired, reopen the console")
            return
    
    # Построить URL для Proxmox WebSocket
    # Преобразуем vmtype: vm -> qemu, container -> lxc
    proxmox_vmtype = "qemu" if vmtype == "vm" else "lxc"
    encoded_ticket = urllib.parse.quote(vncticket, safe='')
    proxmox_ws_url = f"wss://{pve_authority(server)}/api2/json/nodes/{node}/{proxmox_vmtype}/{vmid}/vncwebsocket?port={port}&vncticket={encoded_ticket}"
    
    logger.info(f"Connecting to Proxmox VNC: {server.name} for {proxmox_vmtype}/{vmid}")
    
    # SSL контекст для самоподписанных сертификатов
    ssl_context = ssl.create_default_context()
    if not server.verify_ssl:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    proxmox_ws = None
    bytes_to_proxmox = 0
    bytes_from_proxmox = 0
    
    # Заголовки для авторизации - auth_ticket уже получен в той же сессии что и vncticket
    extra_headers = []
    if auth_ticket:
        extra_headers.append(("Cookie", f"PVEAuthCookie={auth_ticket}"))
        logger.info(f"Using PVEAuthCookie for WebSocket auth")
    elif not server.use_password and server.api_token_name and server.api_token_value:
        # Для API token - но это обычно не работает для VNC WebSocket в Proxmox
        extra_headers.append(("Authorization", f"PVEAPIToken={server.api_user}!{server.api_token_name}={server.api_token_value}"))
        logger.info("Using API token for WebSocket auth (may not work for VNC)")
    else:
        logger.error("No auth_ticket provided and no API token available")
        await websocket.close(code=1008, reason="No authentication available for VNC")
        return
    
    try:
        # Подключаемся к Proxmox VNC WebSocket
        proxmox_ws = await websockets.connect(
            proxmox_ws_url,
            ssl=ssl_context,
            extra_headers=extra_headers,
            subprotocols=['binary'],
            max_size=None,
            ping_interval=None,
            close_timeout=5
        )
        
        logger.info(f"Connected to Proxmox VNC for {vmtype}/{vmid}")
        
        # Счётчики для отладки
        bytes_to_proxmox = 0
        bytes_from_proxmox = 0
        
        # Создаем задачи для двунаправленного проксирования
        async def client_to_proxmox():
            """Пересылка данных от клиента к Proxmox"""
            nonlocal bytes_to_proxmox
            try:
                while True:
                    try:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if "bytes" in message:
                            bytes_to_proxmox += len(message["bytes"])
                            await proxmox_ws.send(message["bytes"])
                        elif "text" in message:
                            bytes_to_proxmox += len(message["text"])
                            await proxmox_ws.send(message["text"])
                    except WebSocketDisconnect:
                        break
            except Exception as e:
                logger.info(f"Client to Proxmox ended: {e}")
        
        async def proxmox_to_client():
            """Пересылка данных от Proxmox к клиенту"""
            nonlocal bytes_from_proxmox
            try:
                async for message in proxmox_ws:
                    try:
                        if isinstance(message, bytes):
                            bytes_from_proxmox += len(message)
                            await websocket.send_bytes(message)
                        else:
                            bytes_from_proxmox += len(message)
                            await websocket.send_text(message)
                    except Exception as send_err:
                        logger.info(f"Send to client failed: {send_err}")
                        break
            except Exception as e:
                logger.info(f"Proxmox to client ended: {e}")
        
        # Запускаем обе задачи
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_proxmox()),
                asyncio.create_task(proxmox_to_client()),
                # Closes the bridge once the session/permission is gone.
                asyncio.create_task(watch_ws_access(user.id, ws_session, _still_allowed)),
            ],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Отменяем оставшиеся задачи
        for task in pending:
            task.cancel()
            
    except Exception as e:
        logger.error(f"VNC WebSocket proxy error: {e}")
    finally:
        logger.info(f"VNC stats - To Proxmox: {bytes_to_proxmox} bytes, From Proxmox: {bytes_from_proxmox} bytes")
        if proxmox_ws:
            await proxmox_ws.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"VNC WebSocket connection closed for {vmtype}/{vmid}")



@router.get("/api/{server_id}/vm/{vmid}/saved-config")
def get_saved_vm_config(
    server_id: int,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Get saved VM configuration from database"""
    require_vm_access(db, current_user, server_id, vmid)
    instance = get_vm_instance(db, server_id, vmid)
    if not instance:
        return JSONResponse(content={"found": False})
    
    return JSONResponse(content={
        "found": True,
        "config": {
            "cores": instance.cores,
            "memory": instance.memory,
            "disk_size": instance.disk_size,
            "ip_address": instance.ip_address,
            "ip_prefix": instance.ip_prefix,
            "gateway": instance.gateway,
            "nameserver": instance.nameserver,
            "cloud_init_user": instance.cloud_init_user,
            "cloud_init_password": instance.cloud_init_password,
            "ssh_keys": instance.ssh_keys,
            "name": instance.name,
            "template_id": instance.template_id
        }
    })


# ==================== LXC Container Creation ====================

@router.get("/api/{server_id}/all-lxc-templates", dependencies=[Depends(enforce_server_visibility)])
def get_all_lxc_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """Получить список LXC шаблонов со всех нод кластера с информацией о shared storage"""
    logger.info(f"[LXC API] Request for ALL templates from server_id={server_id}")
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        templates = client.get_all_lxc_templates()
        logger.info(f"[LXC API] Found {len(templates)} total templates across all nodes")
        return JSONResponse(content={"templates": templates})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting all LXC templates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/available-lxc-templates", dependencies=[Depends(enforce_server_visibility)])
def get_available_lxc_templates(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view"))
):
    """Получить список шаблонов доступных для загрузки из репозитория"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Determine auth method
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        templates = client.get_available_lxc_templates(node)
        return JSONResponse(content=templates)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting available LXC templates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/download-lxc-template", dependencies=[Depends(enforce_server_visibility)])
async def download_lxc_template(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage"))
):
    """Скачать шаблон LXC из репозитория"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    node = data.get('node')
    storage = data.get('storage')
    template = data.get('template')
    require_node_name(node)
    
    if not all([node, storage, template]):
        raise HTTPException(status_code=400, detail="Missing required parameters: node, storage, template")
    
    try:
        # Determine auth method
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        upid = client.download_lxc_template(node, storage, template)
        if not upid:
            raise HTTPException(status_code=500, detail="Failed to start template download")
        
        # Register UPID task for real-time tracking
        try:
            ProxmoxTaskService.register(
                db=db,
                upid=upid,
                server_id=server_id,
                user_id=current_user.id,
                action="download_template",
                node=node,
                description=f"Download template: {template}",
            )
        except Exception as _e:
            logger.warning(f"Failed to register UPID task for template download: {_e}")

        LoggingService.log_proxmox_action(
            db=db,
            action="download_template",
            resource_type="template",
            resource_id=template,
            username=current_user.username,
            resource_name=template,
            server_id=server_id,
            server_name=server.name,
            node_name=node,
            details={"storage": storage},
            success=True,
            ip_address=request.client.host if request.client else None
        )
        
        return JSONResponse(content={"success": True, "upid": upid})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading LXC template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Terminal Proxy (xterm.js) ====================

@router.get("/api/{server_id}/vm/{vmid}/terminal")
def get_vm_terminal(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """Получить данные для xterm.js терминального подключения к VM"""
    import requests
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="Terminal requires password authentication")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{pve_authority(server)}/api2/json/access/ticket",
            data={"username": auth_username, "password": password_to_use},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём terminal proxy
        term_response = requests.post(
            f"https://{pve_authority(server)}/api2/json/nodes/{node}/qemu/{vmid}/termproxy",
            headers={"CSRFPreventionToken": csrf_token},
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if term_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create terminal proxy: {term_response.text}")
        
        term_data = term_response.json().get("data", {})
        
        response_data = {
            'port': term_data.get('port'),
            'ticket': term_data.get('ticket'),
            'node': node,
            'vmid': vmid,
            'type': 'qemu',
            # Opaque handle, redeemed by the WS proxy — never the PVE ticket itself.
            'auth_ticket': stash_pve_ticket(auth_ticket, current_user.id, server_id, vmid),
        }
        
        logger.info(f"User {current_user.username} opened terminal for VM {vmid}")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting terminal for VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/exec")
async def exec_in_container(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:console"))
):
    """Выполнить команду в LXC контейнере через pct exec"""
    require_vm_access(db, current_user, server_id, vmid)
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        data = await request.json()
        command = data.get('command', '/bin/bash')
        args = data.get('args', [])
        
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        # Выполняем команду
        full_command = [command] + (args if isinstance(args, list) else [])
        result = client.exec_in_container(node, vmid, full_command)
        
        if not result:
            raise HTTPException(status_code=500, detail="Failed to execute command")
        
        logger.info(f"User {current_user.username} executed command in LXC {vmid}: {command}")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing command in LXC {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))



# ==================== Terminal WebSocket Proxy ====================

@router.websocket("/ws/terminal/{server_id}/{node}/{vmid}")
async def terminal_websocket_fixed(
    websocket: WebSocket,
    server_id: int,
    node: str,
    vmid: int,
    ticket: str = Query(None),  # WS-тикет панели (POST /api/auth/ws-ticket)
    db: Session = Depends(get_db)
):
    """WebSocket терминал для LXC контейнеров через Proxmox termproxy API"""
    import websockets
    import httpx
    from urllib.parse import quote

    # --- Authenticate the panel user before opening a root shell -----------
    user, ws_session = authenticate_ws_session(ticket, db)
    if not user:
        await websocket.close(code=4001, reason="Authentication required")
        return

    def _still_allowed(db_, u):
        return PermissionEngine.has_permission(u, "vm:console") and check_vm_access(db_, u, server_id, vmid)

    if not _still_allowed(db, user):
        await websocket.close(code=4003, reason="Access denied")
        return

    await websocket.accept()
    logger.info(f"Terminal WebSocket accepted for container {vmid} on node {node} (user={user.username})")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        await websocket.close(code=1008, reason="Proxmox server not found")
        return

    proxmox_ws = None
    bytes_to_proxmox = 0
    bytes_from_proxmox = 0

    try:
        # Шаг 1: Auth ticket
        async with httpx.AsyncClient(verify=server.verify_ssl, timeout=10) as client:
            if not server.password:
                await websocket.close(code=1011, reason="Terminal requires password authentication")
                return

            auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
            auth_resp = await client.post(
                f"https://{pve_authority(server)}/api2/json/access/ticket",
                data={"username": auth_username, "password": server.password}
            )
            if auth_resp.status_code != 200:
                logger.error(f"Auth failed: {auth_resp.status_code} {auth_resp.text}")
                await websocket.close(code=1011, reason="Authentication failed")
                return

            auth_data = auth_resp.json()["data"]
            ticket = auth_data["ticket"]
            csrf_token = auth_data["CSRFPreventionToken"]
            logger.info(f"✅ Auth ticket obtained for {auth_username}")

            # Шаг 2: Termproxy
            term_resp = await client.post(
                f"https://{pve_authority(server)}/api2/json/nodes/{node}/lxc/{vmid}/termproxy",
                headers={"CSRFPreventionToken": csrf_token},
                cookies={"PVEAuthCookie": ticket}
            )
            if term_resp.status_code != 200:
                logger.error(f"Termproxy failed: {term_resp.status_code} - {term_resp.text}")
                await websocket.close(code=1011, reason="Failed to create terminal session")
                return

            term_data = term_resp.json()["data"]
            vncticket = term_data["ticket"]
            port = term_data["port"]
            logger.info(f"✅ Termproxy response: port={port}, ticket_prefix={vncticket[:30]}...")

        # Шаг 3: WebSocket к Proxmox — идентично VNC handler
        proxmox_ws_url = (
            f"wss://{pve_authority(server)}/api2/json/nodes/{node}/lxc/{vmid}/vncwebsocket"
            f"?port={port}&vncticket={quote(vncticket, safe='')}"
        )

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        proxmox_ws = await websockets.connect(
            proxmox_ws_url,
            ssl=ssl_context,
            extra_headers=[("Cookie", f"PVEAuthCookie={ticket}")],
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,
            close_timeout=5
        )
        logger.info(f"✅ Connected to Proxmox terminal WebSocket (port={port})")

        # CRITICAL: Proxmox termproxy requires auth handshake first.
        # Send "USERNAME:VNCTICKET\n" as the very first message.
        # Proxmox responds with "OK" (bytes 0x4F 0x4B), optionally followed by initial terminal data.
        await proxmox_ws.send(f"{auth_username}:{vncticket}\n")
        logger.info(f"✅ Sent termproxy auth: {auth_username}:***")

        try:
            ok_raw = await asyncio.wait_for(proxmox_ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Proxmox termproxy auth timeout — no OK received")
            await websocket.close(code=1011, reason="Terminal auth timeout")
            return

        ok_bytes = ok_raw if isinstance(ok_raw, bytes) else ok_raw.encode("utf-8")
        if not ok_bytes.startswith(b"OK"):
            logger.error(f"Proxmox termproxy auth rejected: {ok_bytes[:50]!r}")
            await websocket.close(code=1011, reason="Terminal authentication rejected")
            return
        logger.info("✅ Proxmox termproxy auth OK!")

        # Send any terminal data that arrived together with "OK"
        if len(ok_bytes) > 2:
            await websocket.send_bytes(ok_bytes[2:])

        async def client_to_proxmox():
            nonlocal bytes_to_proxmox
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if "bytes" in message:
                        bytes_to_proxmox += len(message["bytes"])
                        await proxmox_ws.send(message["bytes"])
                    elif "text" in message:
                        bytes_to_proxmox += len(message["text"])
                        await proxmox_ws.send(message["text"].encode("utf-8"))
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"client_to_proxmox ended: {e}")

        async def proxmox_to_client():
            nonlocal bytes_from_proxmox
            try:
                async for message in proxmox_ws:
                    if isinstance(message, bytes):
                        bytes_from_proxmox += len(message)
                        await websocket.send_bytes(message)
                    else:
                        bytes_from_proxmox += len(message)
                        await websocket.send_bytes(message.encode("utf-8"))
            except Exception as e:
                logger.debug(f"proxmox_to_client ended: {e}")

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_proxmox()),
                asyncio.create_task(proxmox_to_client()),
                # Closes the bridge once the session/permission is gone.
                asyncio.create_task(watch_ws_access(user.id, ws_session, _still_allowed)),
            ],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"Terminal WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass
    finally:
        logger.info(f"Terminal stats — to Proxmox: {bytes_to_proxmox}B, from Proxmox: {bytes_from_proxmox}B")
        if proxmox_ws:
            try:
                await proxmox_ws.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"Terminal session closed for container {vmid}")


# ==================== VM Serial Console WebSocket Proxy ====================

@router.websocket("/ws/serial/{server_id}/{node}/{vmid}")
async def serial_websocket(
    websocket: WebSocket,
    server_id: int,
    node: str,
    vmid: int,
    ticket: str = Query(None),  # WS-тикет панели (POST /api/auth/ws-ticket)
    db: Session = Depends(get_db)
):
    """WebSocket serial-консоль для VM (qemu) через Proxmox termproxy.

    Идентично LXC-терминалу, но использует эндпоинты qemu. Требует, чтобы у VM
    был настроен serial0 (см. /vm/{vmid}/serial/enable) и гость выводил в ttyS0.
    """
    import websockets
    import httpx
    from urllib.parse import quote

    user, ws_session = authenticate_ws_session(ticket, db)
    if not user:
        await websocket.close(code=4001, reason="Authentication required")
        return

    def _still_allowed(db_, u):
        return PermissionEngine.has_permission(u, "vm:console") and check_vm_access(db_, u, server_id, vmid)

    if not _still_allowed(db, user):
        await websocket.close(code=4003, reason="Access denied")
        return

    await websocket.accept()
    logger.info(f"Serial WebSocket accepted for VM {vmid} on node {node} (user={user.username})")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        await websocket.close(code=1008, reason="Proxmox server not found")
        return

    proxmox_ws = None
    try:
        async with httpx.AsyncClient(verify=server.verify_ssl, timeout=10) as client:
            if not server.password:
                await websocket.close(code=1011, reason="Serial console requires password authentication")
                return

            auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
            auth_resp = await client.post(
                f"https://{pve_authority(server)}/api2/json/access/ticket",
                data={"username": auth_username, "password": server.password}
            )
            if auth_resp.status_code != 200:
                await websocket.close(code=1011, reason="Authentication failed")
                return

            auth_data = auth_resp.json()["data"]
            ticket = auth_data["ticket"]
            csrf_token = auth_data["CSRFPreventionToken"]

            term_resp = await client.post(
                f"https://{pve_authority(server)}/api2/json/nodes/{node}/qemu/{vmid}/termproxy",
                headers={"CSRFPreventionToken": csrf_token},
                cookies={"PVEAuthCookie": ticket}
            )
            if term_resp.status_code != 200:
                logger.error(f"Serial termproxy failed: {term_resp.status_code} - {term_resp.text}")
                await websocket.close(code=1011, reason="Failed to create serial session (is serial0 configured?)")
                return

            term_data = term_resp.json()["data"]
            vncticket = term_data["ticket"]
            port = term_data["port"]

        proxmox_ws_url = (
            f"wss://{pve_authority(server)}/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket"
            f"?port={port}&vncticket={quote(vncticket, safe='')}"
        )

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        proxmox_ws = await websockets.connect(
            proxmox_ws_url,
            ssl=ssl_context,
            extra_headers=[("Cookie", f"PVEAuthCookie={ticket}")],
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,
            close_timeout=5
        )

        await proxmox_ws.send(f"{auth_username}:{vncticket}\n")
        try:
            ok_raw = await asyncio.wait_for(proxmox_ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            await websocket.close(code=1011, reason="Serial auth timeout")
            return

        ok_bytes = ok_raw if isinstance(ok_raw, bytes) else ok_raw.encode("utf-8")
        if not ok_bytes.startswith(b"OK"):
            await websocket.close(code=1011, reason="Serial authentication rejected")
            return
        if len(ok_bytes) > 2:
            await websocket.send_bytes(ok_bytes[2:])

        async def client_to_proxmox():
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if "bytes" in message:
                        await proxmox_ws.send(message["bytes"])
                    elif "text" in message:
                        await proxmox_ws.send(message["text"].encode("utf-8"))
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"serial client_to_proxmox ended: {e}")

        async def proxmox_to_client():
            try:
                async for message in proxmox_ws:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_bytes(message.encode("utf-8"))
            except Exception as e:
                logger.debug(f"serial proxmox_to_client ended: {e}")

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_proxmox()),
                asyncio.create_task(proxmox_to_client()),
                # Closes the bridge once the session/permission is gone.
                asyncio.create_task(watch_ws_access(user.id, ws_session, _still_allowed)),
            ],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"Serial WebSocket error: {e}")
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass
    finally:
        if proxmox_ws:
            try:
                await proxmox_ws.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"Serial session closed for VM {vmid}")


# ==================== Node Shell (APT upgrade) ====================

# Разрешённые команды node-termproxy (Proxmox: upgrade | login | ceph_install)
_ALLOWED_NODE_SHELL_CMDS = {"upgrade", "login"}


@router.websocket("/ws/node-shell/{server_id}/{node}")
async def node_shell_websocket(
    websocket: WebSocket,
    server_id: int,
    node: str,
    cmd: str = Query("upgrade"),  # upgrade | login
    ticket: str = Query(None),    # WS-тикет панели (POST /api/auth/ws-ticket)
    db: Session = Depends(get_db)
):
    """
    WebSocket root-терминал на уровне ноды через Proxmox termproxy API.
    Используется для установки обновлений (cmd=upgrade → apt dist-upgrade),
    как это делает штатная кнопка «Upgrade» в интерфейсе Proxmox.
    """
    import websockets
    import httpx
    from urllib.parse import quote

    # --- Authenticate the panel user before opening a root shell -----------
    user, ws_session = authenticate_ws_session(ticket, db)
    if not user:
        await websocket.close(code=4001, reason="Authentication required")
        return

    def _still_allowed(db_, u):
        # WebSocket routes are not covered by the router's enforce_server_visibility.
        visible = get_visible_server_ids(_NO_WORKSPACE_HEADER, db_, u)
        return (PermissionEngine.has_permission(u, "node:upgrade")
                and (visible is None or server_id in visible))

    if not _still_allowed(db, user):
        await websocket.close(code=4003, reason="Access denied")
        return
    if cmd not in _ALLOWED_NODE_SHELL_CMDS:
        await websocket.close(code=4003, reason="Command not allowed")
        return

    await websocket.accept()
    logger.info(f"Node shell WebSocket accepted for node {node} (cmd={cmd}, user={user.username})")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        await websocket.close(code=1008, reason="Proxmox server not found")
        return

    proxmox_ws = None
    bytes_to_proxmox = 0
    bytes_from_proxmox = 0

    try:
        # Шаг 1: Auth ticket (termproxy требует пароль, не API-токен)
        async with httpx.AsyncClient(verify=server.verify_ssl, timeout=10) as client:
            if not server.password:
                await websocket.close(code=1011, reason="Node shell requires password authentication")
                return

            auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
            auth_resp = await client.post(
                f"https://{pve_authority(server)}/api2/json/access/ticket",
                data={"username": auth_username, "password": server.password}
            )
            if auth_resp.status_code != 200:
                logger.error(f"Auth failed: {auth_resp.status_code} {auth_resp.text}")
                await websocket.close(code=1011, reason="Authentication failed")
                return

            auth_data = auth_resp.json()["data"]
            ticket = auth_data["ticket"]
            csrf_token = auth_data["CSRFPreventionToken"]

            # Шаг 2: Termproxy на уровне ноды с нужной командой
            term_resp = await client.post(
                f"https://{pve_authority(server)}/api2/json/nodes/{node}/termproxy",
                headers={"CSRFPreventionToken": csrf_token},
                cookies={"PVEAuthCookie": ticket},
                data={"cmd": cmd},
            )
            if term_resp.status_code != 200:
                logger.error(f"Node termproxy failed: {term_resp.status_code} - {term_resp.text}")
                await websocket.close(code=1011, reason="Failed to create node shell session")
                return

            term_data = term_resp.json()["data"]
            vncticket = term_data["ticket"]
            port = term_data["port"]
            logger.info(f"✅ Node termproxy: node={node}, cmd={cmd}, port={port}")

        # Шаг 3: WebSocket к Proxmox (node-level vncwebsocket)
        proxmox_ws_url = (
            f"wss://{pve_authority(server)}/api2/json/nodes/{node}/vncwebsocket"
            f"?port={port}&vncticket={quote(vncticket, safe='')}"
        )

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        proxmox_ws = await websockets.connect(
            proxmox_ws_url,
            ssl=ssl_context,
            extra_headers=[("Cookie", f"PVEAuthCookie={ticket}")],
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,
            close_timeout=5
        )

        # Auth handshake: "USERNAME:VNCTICKET\n" → "OK"
        await proxmox_ws.send(f"{auth_username}:{vncticket}\n")
        try:
            ok_raw = await asyncio.wait_for(proxmox_ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Node shell auth timeout — no OK received")
            await websocket.close(code=1011, reason="Node shell auth timeout")
            return

        ok_bytes = ok_raw if isinstance(ok_raw, bytes) else ok_raw.encode("utf-8")
        if not ok_bytes.startswith(b"OK"):
            logger.error(f"Node shell auth rejected: {ok_bytes[:50]!r}")
            await websocket.close(code=1011, reason="Node shell authentication rejected")
            return

        if len(ok_bytes) > 2:
            await websocket.send_bytes(ok_bytes[2:])

        async def client_to_proxmox():
            nonlocal bytes_to_proxmox
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if "bytes" in message:
                        bytes_to_proxmox += len(message["bytes"])
                        await proxmox_ws.send(message["bytes"])
                    elif "text" in message:
                        bytes_to_proxmox += len(message["text"])
                        await proxmox_ws.send(message["text"].encode("utf-8"))
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"client_to_proxmox ended: {e}")

        async def proxmox_to_client():
            nonlocal bytes_from_proxmox
            try:
                async for message in proxmox_ws:
                    if isinstance(message, bytes):
                        bytes_from_proxmox += len(message)
                        await websocket.send_bytes(message)
                    else:
                        bytes_from_proxmox += len(message)
                        await websocket.send_bytes(message.encode("utf-8"))
            except Exception as e:
                logger.debug(f"proxmox_to_client ended: {e}")

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_proxmox()),
                asyncio.create_task(proxmox_to_client()),
                # Closes the bridge once the session/permission is gone.
                asyncio.create_task(watch_ws_access(user.id, ws_session, _still_allowed)),
            ],
            return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"Node shell WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass
    finally:
        logger.info(f"Node shell stats — to Proxmox: {bytes_to_proxmox}B, from Proxmox: {bytes_from_proxmox}B")
        if proxmox_ws:
            try:
                await proxmox_ws.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"Node shell session closed for node {node}")


# ==================== Bulk Operations API ====================

@router.post("/api/bulk-operation")
def create_bulk_operation(
    request: BulkOperationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a bulk operation task.
    Returns task ID for tracking progress.

    Every item is checked like the single-guest endpoint would check it:
    the action's permission and access to that guest. The queue worker acts
    with panel credentials, so items it receives must already be authorized.
    """
    # Map action to task type
    action_to_task_type = {
        'start': 'bulk_start',
        'stop': 'bulk_stop',
        'restart': 'bulk_restart',
        'shutdown': 'bulk_shutdown',
        'delete': 'bulk_delete',
        'migrate': 'bulk_migrate',
    }

    task_type = action_to_task_type.get(request.action)
    if not task_type:
        raise HTTPException(status_code=400, detail=f"Invalid action: {request.action}")

    if not request.items:
        raise HTTPException(status_code=400, detail="No items selected")

    perms = _BULK_ACTION_PERMISSIONS[request.action]
    for perm in {perms[i.vm_type] for i in request.items}:
        if not check_permission(current_user, perm):
            raise HTTPException(status_code=403, detail=f"Permission denied: {perm}")
    for i in request.items:
        require_vm_access(db, current_user, i.server_id, i.vmid)

    # For migrate, каждый элемент обязан нести целевую ноду
    if request.action == 'migrate':
        missing = [i.vmid for i in request.items if not i.target_node]
        if missing:
            raise HTTPException(status_code=400, detail="target_node is required for migrate")
    
    # Convert to list of dicts
    items = [item.model_dump() for item in request.items]
    
    try:
        task = TaskQueueService.create_task(
            db=db,
            task_type=task_type,
            user_id=current_user.id,
            items=items
        )
        
        logger.info(f"User {current_user.username} created bulk operation: {task_type} for {len(items)} items")
        
        return {
            "success": True,
            "task_id": task.id,
            "message": f"Bulk operation queued: {len(items)} items"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===================== VM Owner Management =====================

@router.get("/api/{server_id}/vm/{vmid}/owner")
def get_vm_owner(
    server_id: int,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get current owner of a VM/LXC. Admin only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")

    instance = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None)
    ).first()

    owner = None
    if instance and instance.owner_id:
        owner_user = db.query(User).filter(User.id == instance.owner_id).first()
        if owner_user:
            owner = {"id": owner_user.id, "username": owner_user.username, "full_name": owner_user.full_name}

    users = (
        db.query(User)
        .filter(User.is_active == True, User.is_admin == False)
        .order_by(User.username)
        .all()
    )

    return {
        "owner_id": instance.owner_id if instance else None,
        "owner": owner,
        "users": [{"id": u.id, "username": u.username, "full_name": u.full_name} for u in users]
    }


def _collect_owner_ssh_keys(db: Session, user_id: int | None) -> str:
    """Собрать все SSH-ключи пользователя (профильный + библиотека)."""
    if not user_id:
        return ""

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return ""

    keys: list[str] = []

    if user.ssh_public_key:
        keys.append(user.ssh_public_key.strip())

    lib_keys = db.query(UserSSHKey).filter(UserSSHKey.user_id == user_id).all()
    for k in lib_keys:
        if k.public_key:
            keys.append(k.public_key.strip())

    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)

    return "\n".join(unique)


def _apply_ssh_keys_to_instance(
    db: Session,
    instance: VMInstance,
    new_keys: str,
) -> dict:
    """Применить SSH-ключи к инстансу через Proxmox API (best-effort).

    Для QEMU — обновляет cloud-init sshkeys.
    Для LXC  — пишет /root/.ssh/authorized_keys через exec.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == instance.server_id).first()
    if not server:
        return {"success": False, "error": "Server not found"}

    try:
        client = _get_proxmox_client(server)
    except HTTPException:
        return {"success": False, "error": "Cannot connect to Proxmox"}

    if not client.is_connected():
        return {"success": False, "error": "Proxmox client not connected"}

    if instance.vm_type == "qemu":
        result = client.update_cloud_init(
            node=instance.node,
            vmid=instance.vmid,
            sshkeys=new_keys if new_keys else "",
        )
        return result

    if instance.vm_type == "lxc":
        try:
            if new_keys:
                encoded = base64.b64encode(new_keys.encode()).decode()
                cmd = (
                    "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                    f"echo {encoded} | base64 -d > /root/.ssh/authorized_keys && "
                    "chmod 600 /root/.ssh/authorized_keys"
                )
            else:
                cmd = "rm -f /root/.ssh/authorized_keys"
            client.exec_in_container(instance.node, instance.vmid, ["bash", "-c", cmd])
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unknown vm_type: {instance.vm_type}"}


@router.put("/api/{server_id}/vm/{vmid}/owner")
def set_vm_owner(
    server_id: int,
    vmid: int,
    body: VMOwnerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Set (or clear) the owner of a VM/LXC. Admin only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")

    instance = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None)
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="VM not found in cache")

    target_user = None
    if body.user_id is not None:
        target_user = db.query(User).filter(User.id == body.user_id, User.is_active == True).first()
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

    old_owner_id = instance.owner_id

    # --- SSH key swap (best-effort) ---
    ssh_swap_result = None
    if old_owner_id != body.user_id:
        new_keys = _collect_owner_ssh_keys(db, body.user_id)
        try:
            ssh_swap_result = _apply_ssh_keys_to_instance(db, instance, new_keys)
            if ssh_swap_result.get("success"):
                logger.info(
                    f"SSH keys swapped on VM {vmid}: "
                    f"old_owner={old_owner_id} -> new_owner={body.user_id}"
                )
            else:
                logger.warning(
                    f"SSH key swap failed for VM {vmid}: {ssh_swap_result.get('error')}"
                )
        except Exception as e:
            logger.warning(f"SSH key swap error for VM {vmid}: {e}")
            ssh_swap_result = {"success": False, "error": str(e)}

        instance.ssh_keys = new_keys or None

    instance.owner_id = body.user_id
    db.commit()
    logger.info(f"Admin {current_user.username} set owner of VM {vmid} (server {server_id}) to user_id={body.user_id}")

    # Push a real-time event so every connected client refreshes the owner
    # column without a manual page reload.
    # The payload carries the new owner's e-mail, so it goes to administrators
    # and the two owners involved only — not to every connected tenant.
    try:
        from ...websocket_manager import ws_manager, run_async_safe
        from ...models import Role
        owner_user = (
            {
                "username": target_user.username,
                "email": target_user.email,
                "full_name": target_user.full_name,
            }
            if target_user else None
        )
        payload = {
            "type": "vm_owner_changed",
            "server_id": server_id,
            "vmid": vmid,
            "owner_id": body.user_id,
            "owner_user": owner_user,
        }
        admin_ids = {
            uid for (uid,) in db.query(User.id)
            .outerjoin(Role, Role.id == User.role_id)
            .filter(User.is_active == True)  # noqa: E712
            .filter((User.is_admin == True) | (Role.name == "admin"))  # noqa: E712
        }
        recipients = admin_ids | {uid for uid in (old_owner_id, body.user_id) if uid}
        for uid in recipients:
            run_async_safe(ws_manager.send_to_user(uid, payload))
    except Exception as e:
        logger.debug(f"Failed to broadcast vm_owner_changed: {e}")

    response = {"ok": True, "owner_id": body.user_id}
    if ssh_swap_result is not None:
        response["ssh_keys_updated"] = ssh_swap_result.get("success", False)
    return response



# ==================== Additional Instance Operations (Notes, ISO, Execute) ====================

class NotesRequest(BaseModel):
    description: Optional[str] = ""


# `device` becomes a VM config key: keep it to CD-ROM capable buses.
_CDROM_DEVICE = r"^(ide|sata|scsi)\d{1,2}$"


class IsoAttachRequest(BaseModel):
    volid: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*:iso/[^/\s]+$")
    device: str = Field(default="ide2", pattern=_CDROM_DEVICE)
    # Поставить этот ISO первым в порядке загрузки (грузиться с образа: live-CD и т.п.).
    boot_from_iso: bool = False
    # Перезапустить ВМ, чтобы новый порядок загрузки вступил в силу (только если запущена).
    reboot_after: bool = True


class IsoDetachRequest(BaseModel):
    device: str = Field(default="ide2", pattern=_CDROM_DEVICE)
    # После извлечения ISO — поставить диск первым в порядке загрузки (грузиться с ОС).
    boot_from_disk: bool = True
    # Перезапустить ВМ, чтобы новый порядок загрузки вступил в силу (только если запущена).
    reboot_after: bool = True


class ExecuteCommandRequest(BaseModel):
    command: str
    timeout: int = 30


# -------- Notes / Description --------

@router.put("/api/{server_id}/vm/{vmid}/notes")
def update_vm_notes(
    server_id: int,
    vmid: int,
    node: str,
    body: NotesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Обновить заметку (description) у VM."""
    require_vm_access(db, current_user, server_id, vmid)
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    ok = client.set_vm_notes(node, vmid, body.description or '')
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update notes")
    try:
        cached = db.query(VMInstance).filter(
            VMInstance.server_id == server_id,
            VMInstance.vmid == vmid,
            VMInstance.deleted_at.is_(None),
        ).first()
        if cached:
            cached.description = body.description or ''
            db.commit()
    except Exception:
        db.rollback()
    return {"status": "success"}


@router.put("/api/{server_id}/container/{vmid}/notes")
def update_container_notes(
    server_id: int,
    vmid: int,
    node: str,
    body: NotesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Обновить заметку (description) у LXC."""
    require_vm_access(db, current_user, server_id, vmid)
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    ok = client.set_container_notes(node, vmid, body.description or '')
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update notes")
    try:
        cached = db.query(VMInstance).filter(
            VMInstance.server_id == server_id,
            VMInstance.vmid == vmid,
            VMInstance.deleted_at.is_(None),
        ).first()
        if cached:
            cached.description = body.description or ''
            db.commit()
    except Exception:
        db.rollback()
    return {"status": "success"}


# -------- ISO mount/unmount (KVM only) --------

@router.get("/api/{server_id}/node/{node}/isos", dependencies=[Depends(enforce_server_visibility)])
def list_node_isos(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Список ISO-образов на ноде."""
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    return {"isos": client.get_node_isos(node)}


@router.post("/api/{server_id}/vm/{vmid}/iso/attach")
def attach_iso_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: IsoAttachRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Подключить ISO (CD-ROM) к VM."""
    require_vm_access(db, current_user, server_id, vmid)
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    device = body.device or 'ide2'
    ok = client.attach_iso(node, vmid, body.volid, device)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to attach ISO")
    boot_iso_set = False
    rebooted = False
    if body.boot_from_iso:
        boot_iso_set = client.set_boot_iso_first(node, vmid, device)
        # Новый порядок загрузки применяется только при power-cycle QEMU.
        # Перезапускаем лишь запущенную ВМ.
        if boot_iso_set and body.reboot_after:
            status = client.get_vm_status(node, vmid) or {}
            if status.get('status') == 'running':
                rebooted = client.hybrid_restart_vm(node, vmid)
    LoggingService.log_proxmox_action(
        db=db, action='attach-iso', resource_type='vm', resource_id=vmid,
        username=current_user.username, server_id=server_id,
        server_name=server.name, node_name=node,
        details={'volid': body.volid, 'device': device,
                 'boot_from_iso': boot_iso_set, 'rebooted': rebooted},
        success=True,
    )
    return {"status": "success", "boot_from_iso": boot_iso_set, "rebooted": rebooted}


@router.post("/api/{server_id}/vm/{vmid}/iso/detach")
def detach_iso_endpoint(
    server_id: int,
    vmid: int,
    node: str,
    body: IsoDetachRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:manage"))
):
    """Отключить ISO (CD-ROM) у VM."""
    require_vm_access(db, current_user, server_id, vmid)
    server = _resolve_server(db, server_id)
    client = _get_client_or_503(server)
    device = body.device or 'ide2'
    ok = client.detach_iso(node, vmid, device)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to detach ISO")
    boot_disk_set = False
    rebooted = False
    if body.boot_from_disk:
        boot_disk_set = client.set_boot_disk_first(node, vmid, exclude_device=device)
        # Новый порядок загрузки применяется только при power-cycle QEMU (гостевой
        # reboot конфиг не перечитывает). Перезапускаем лишь запущенную ВМ.
        if boot_disk_set and body.reboot_after:
            status = client.get_vm_status(node, vmid) or {}
            if status.get('status') == 'running':
                rebooted = client.hybrid_restart_vm(node, vmid)
    LoggingService.log_proxmox_action(
        db=db, action='detach-iso', resource_type='vm', resource_id=vmid,
        username=current_user.username, server_id=server_id,
        server_name=server.name, node_name=node,
        details={'device': device, 'boot_from_disk': boot_disk_set, 'rebooted': rebooted},
        success=True,
    )
    return {"status": "success", "boot_from_disk": boot_disk_set, "rebooted": rebooted}


# ══════════════════════════════════════════════════════════════════════════
# Catch-all маршруты управления VM/LXC (start/stop/restart/shutdown).
# ВАЖНО: регистрируются ПОСЛЕДНИМИ. Шаблон /{action} перехватывает любой
# одно-сегментный POST (например /unlock, /cloud-init, /exec), поэтому все
# конкретные POST-маршруты должны быть объявлены выше по файлу.
# ══════════════════════════════════════════════════════════════════════════

@router.post("/api/{server_id}/vm/{vmid}/{action}")
def control_vm(
    server_id: int, 
    vmid: int, 
    action: str, 
    node: str,
    force: int = 0,
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Управление VM (start/stop/restart/shutdown/reset)"""
    if action not in ['start', 'stop', 'restart', 'shutdown', 'reset']:
        raise HTTPException(status_code=400, detail="Invalid action")

    # Проверка прав в зависимости от действия
    permission_map = {
        'start': 'vm:start',
        'stop': 'vm:stop',
        'shutdown': 'vm:stop',
        'restart': 'vm:restart',
        'reset': 'vm:restart',
    }
    if not current_user.has_permission(permission_map[action]):
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied: {permission_map[action]}"
        )
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        # Get VM name for logging
        vm_name = None
        try:
            vm_status = client.get_vm_status(node, vmid)
            vm_name = vm_status.get('name') if isinstance(vm_status, dict) else None
        except Exception:
            pass
        
        upid = None
        if action == 'start':
            upid = client.start_vm(node, vmid)
            success = bool(upid)
        elif action == 'stop':
            if force:
                success = client.force_stop_vm(node, vmid)
            else:
                upid = client.stop_vm(node, vmid, force=False)
                success = bool(upid)
        elif action == 'shutdown':
            upid = client.shutdown_vm(node, vmid)
            success = bool(upid)
        elif action == 'reset':
            upid = client.reset_vm(node, vmid)
            success = bool(upid)
        else:  # restart
            upid = client.restart_vm(node, vmid)
            success = bool(upid)

        action_name = 'kill' if action == 'stop' and force else action
        if success:
            # Immediately update vm_instances cache so page refresh returns correct status
            expected_status = 'running' if action in ('start', 'restart', 'reset') else 'stopped'
            try:
                cached_vm = db.query(VMInstance).filter(
                    VMInstance.server_id == server_id,
                    VMInstance.vmid == vmid,
                    VMInstance.deleted_at.is_(None)
                ).first()
                if cached_vm:
                    cached_vm.status = expected_status
                    db.commit()
            except Exception as _ce:
                logger.warning(f"Failed to update VM {vmid} status cache: {_ce}")
                db.rollback()
            # Register ProxmoxTask for UPID-based tracking
            if upid:
                _desc_map = {
                    'start': f"Запуск VM {vm_name or vmid}",
                    'stop': f"Остановка VM {vm_name or vmid}",
                    'restart': f"Перезапуск VM {vm_name or vmid}",
                    'reset': f"Жёсткий сброс VM {vm_name or vmid}",
                }
                try:
                    ProxmoxTaskService.register(
                        db=db, upid=upid, server_id=server_id,
                        user_id=current_user.id, action=action_name,
                        node=node, vmid=vmid, vm_type='qemu',
                        description=_desc_map.get(action, action_name),
                    )
                except Exception as _te:
                    logger.warning(f"Failed to register ProxmoxTask for VM {vmid} {action_name}: {_te}")
            # Log successful action
            LoggingService.log_proxmox_action(
                db=db,
                action=action_name,
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=vm_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={"force": force},
                success=True
            )
            logger.info(f"User {current_user.username} executed {action_name} on VM {vmid} at {server.name}")
            return JSONResponse(content={"status": "success", "action": action_name, "vmid": vmid, "node": node, "upid": upid})
        else:
            # Log failed action
            LoggingService.log_proxmox_action(
                db=db,
                action=action_name,
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=vm_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={"force": force},
                success=False,
                error_message="Failed to execute action"
            )
            raise HTTPException(status_code=500, detail="Failed to execute action")
    except HTTPException:
        raise
    except Exception as e:
        # Log error
        LoggingService.log_proxmox_action(
            db=db,
            action=action,
            resource_type="vm",
            resource_id=vmid,
            username=current_user.username,
            server_id=server_id if server else None,
            server_name=server.name if server else None,
            node_name=node,
            details={"force": force},
            success=False,
            error_message=str(e)
        )
        logger.error(f"Error controlling VM {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/{action}")
def control_container(
    server_id: int, 
    vmid: int, 
    action: str, 
    node: str,
    force: int = 0,
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Управление LXC контейнером (start/stop/restart/shutdown)"""
    if action not in ['start', 'stop', 'restart', 'shutdown']:
        raise HTTPException(status_code=400, detail="Invalid action")
    
    # Проверка прав в зависимости от действия
    permission_map = {
        'start': 'vm:start',
        'stop': 'vm:stop',
        'shutdown': 'vm:stop',
        'restart': 'vm:restart',
    }
    require_permission(current_user, permission_map[action])
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        upid = None
        if action == 'start':
            upid = client.start_container(node, vmid)
            success = bool(upid)
        elif action == 'stop':
            if force:
                success = client.force_stop_container(node, vmid)
            else:
                upid = client.stop_container(node, vmid, force=False)
                success = bool(upid)
        elif action == 'shutdown':
            upid = client.shutdown_container(node, vmid)
            success = bool(upid)
        else:  # restart
            upid = client.restart_container(node, vmid)
            success = bool(upid)

        action_name = 'kill' if action == 'stop' and force else action
        if success:
            # Immediately update vm_instances cache so page refresh returns correct status
            expected_status = 'running' if action in ('start', 'restart') else 'stopped'
            try:
                cached_ct = db.query(VMInstance).filter(
                    VMInstance.server_id == server_id,
                    VMInstance.vmid == vmid,
                    VMInstance.deleted_at.is_(None)
                ).first()
                if cached_ct:
                    cached_ct.status = expected_status
                    db.commit()
            except Exception as _ce:
                logger.warning(f"Failed to update LXC {vmid} status cache: {_ce}")
                db.rollback()
            # Register ProxmoxTask for UPID-based tracking
            if upid:
                _desc_map = {
                    'start': f"Запуск LXC {vmid}",
                    'stop': f"Остановка LXC {vmid}",
                    'restart': f"Перезапуск LXC {vmid}",
                }
                try:
                    ProxmoxTaskService.register(
                        db=db, upid=upid, server_id=server_id,
                        user_id=current_user.id, action=action_name,
                        node=node, vmid=vmid, vm_type='lxc',
                        description=_desc_map.get(action, action_name),
                    )
                except Exception as _te:
                    logger.warning(f"Failed to register ProxmoxTask for LXC {vmid} {action_name}: {_te}")
            logger.info(f"User {current_user.username} executed {action_name} on container {vmid} at {server.name}")
            return JSONResponse(content={"status": "success", "action": action_name, "vmid": vmid, "node": node, "upid": upid})
        else:
            raise HTTPException(status_code=500, detail="Failed to execute action")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error controlling container {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
