"""
API endpoints for creating LXC containers from Proxmox CT templates (vztmpl files).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from ...auth import PermissionChecker
from ...db import get_db, SessionLocal
from ...ipam_service import IPAMService
from ...logging_service import LoggingService
from ...models import DeployTask, IPAMNetwork, ProxmoxServer, User, VMInstance
from ...proxmox import ProxmoxClient
from ...websocket_manager import broadcast_task_update
from ._helpers import (_get_proxmox_client, get_next_vmid, save_vm_instance,
                       can_view_all_instances, require_server_visible, VZTMPL_VOLID_RE,
                       PveNode)

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])

_lxc_deploy_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxc_deploy")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Pydantic schemas ─────────────────────────────────────────────────────────

class LXCDeployRequest(BaseModel):
    server_id: int
    ostemplate: str          # volid, e.g. "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
    node: Optional[PveNode] = None
    name: str                # hostname / display name
    vmid: Optional[int] = None
    cores: int = 1
    memory: int = 512        # МБ
    swap: int = 512          # МБ
    disk: int = 8            # ГБ
    storage: str = "local-lvm"
    bridge: str = "vmbr0"
    ip_address: Optional[str] = None
    gateway: Optional[str] = None
    ipam_network_id: Optional[int] = None
    ipam_pool_id: Optional[int] = None
    password: Optional[str] = None
    ssh_keys: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v):
        if v is not None and len(v) < 5:
            raise ValueError("Пароль должен содержать не менее 5 символов (требование Proxmox)")
        return v
    ssh_key_ids: Optional[List[int]] = None
    owner_id: Optional[int] = None
    nameserver: Optional[str] = None
    searchdomain: Optional[str] = None
    unprivileged: bool = True
    start_after_create: bool = True
    onboot: bool = False


class LXCDeployResponse(BaseModel):
    task_id: int
    status: str
    name: str


class LXCTemplateItem(BaseModel):
    volid: str
    storage: str
    node: str
    name: str
    size: int


# ── Helpers ──────────────────────────────────────────────────────────────────

def _update_task(task_id: int, status: str, step: str, progress: int,
                 vmid: int = None, node: str = None, error: str = None):
    db = SessionLocal()
    try:
        task = db.query(DeployTask).filter(DeployTask.id == task_id).first()
        if not task:
            return
        task.status = status
        task.step = step
        task.progress = progress
        if vmid is not None:
            task.vmid = vmid
        if node is not None:
            task.node = node
        if error is not None:
            task.error_message = error
        if status == "running" and not task.started_at:
            task.started_at = _utcnow()
        if status in ("completed", "failed"):
            task.completed_at = _utcnow()
        db.commit()
        try:
            broadcast_task_update(task.user_id, "deploy_task_update", task.to_dict())
        except Exception:
            pass
    finally:
        db.close()


def _do_lxc_deploy(task_id: int, req: LXCDeployRequest, user_id: int, username: str,
                   owner_id: Optional[int] = None):
    """Blocking LXC deploy — runs in thread pool."""
    db = SessionLocal()
    server = None
    deploy_node = None
    new_vmid = None
    ipam_allocation = None
    try:
        _update_task(task_id, "running", "Подготовка...", 5)

        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == req.server_id).first()
        if not server:
            _update_task(task_id, "failed", "Сервер не найден", 0, error="Сервер Proxmox не найден")
            return

        # Connect
        _update_task(task_id, "running", "Подключение к Proxmox...", 10)
        client = _get_proxmox_client(server)
        if not client.is_connected():
            _update_task(task_id, "failed", "Ошибка подключения к Proxmox", 10,
                         error="Не удалось подключиться к Proxmox серверу")
            return

        # Determine node
        deploy_node = req.node
        if not deploy_node:
            nodes = client.get_nodes()
            if not nodes:
                _update_task(task_id, "failed", "Нет доступных нод", 15, error="Не найдено нод Proxmox")
                return
            # Try to find a node that has the template storage
            for n in nodes:
                try:
                    storages = client.proxmox.nodes(n["node"]).storage.get()
                    for s in storages:
                        if "vztmpl" in s.get("content", ""):
                            deploy_node = n["node"]
                            break
                except Exception:
                    pass
                if deploy_node:
                    break
            if not deploy_node:
                deploy_node = nodes[0]["node"]

        # Allocate VMID
        _update_task(task_id, "running", "Генерация VMID...", 20)
        if req.vmid:
            new_vmid = req.vmid
        else:
            new_vmid = get_next_vmid(db, server.id)

        # IPAM
        _update_task(task_id, "running", "Выделение IP-адреса...", 25)
        allocated_ip = req.ip_address
        gateway = req.gateway
        network_mask = "24"
        ipam_allocation = None

        if req.ipam_network_id and not req.ip_address:
            ipam = IPAMService(db)
            ipam_allocation, error = ipam.auto_allocate_ip(
                network_id=req.ipam_network_id,
                pool_id=req.ipam_pool_id,
                resource_type="ct",
                resource_name=req.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=deploy_node,
                allocated_by=username,
                notes=f"Auto-allocated for LXC {req.name}",
            )
            if not error:
                allocated_ip = ipam_allocation.ip_address
                if not gateway:
                    net = db.query(IPAMNetwork).filter(IPAMNetwork.id == req.ipam_network_id).first()
                    if net and net.gateway:
                        gateway = net.gateway
        elif req.ip_address and req.ipam_network_id:
            ipam = IPAMService(db)
            ipam_allocation, _ = ipam.allocate_ip(
                ip_address=req.ip_address,
                network_id=req.ipam_network_id,
                pool_id=req.ipam_pool_id,
                resource_type="ct",
                resource_name=req.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=deploy_node,
                allocated_by=username,
                notes=f"Manually assigned for LXC {req.name}",
            )

        if allocated_ip and req.ipam_network_id:
            try:
                network_id = req.ipam_network_id
                if ipam_allocation and hasattr(ipam_allocation, "network_id"):
                    network_id = ipam_allocation.network_id
                if network_id:
                    net = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
                    if net and net.network and "/" in net.network:
                        network_mask = net.network.split("/")[1]
                    if not gateway and net and net.gateway:
                        gateway = net.gateway
            except Exception as e:
                logger.warning(f"Could not get network info from IPAM: {e}")

        # Build ip_config string for LXC net0
        if allocated_ip:
            ip_config = f"ip={allocated_ip}/{network_mask},gw={gateway}" if gateway else f"ip={allocated_ip}/{network_mask}"
        else:
            ip_config = "ip=dhcp"

        # Create LXC
        _update_task(task_id, "running", f"Создание LXC контейнера VMID {new_vmid}...", 35,
                     vmid=new_vmid, node=deploy_node)
        net0_str = f"name=eth0,bridge={req.bridge},{ip_config},firewall=1"
        try:
            upid = client.create_lxc_container(
                node=deploy_node,
                vmid=new_vmid,
                ostemplate=req.ostemplate,
                hostname=req.name,
                cores=req.cores,
                memory=req.memory,
                swap=req.swap,
                storage=req.storage,
                rootfs_size=req.disk,
                net0=net0_str,
                nameserver=req.nameserver,
                searchdomain=req.searchdomain,
                password=req.password,
                ssh_public_keys=req.ssh_keys,
                unprivileged=req.unprivileged,
                start_after_create=False,  # we start manually after config
                onboot=req.onboot,
                description=f"Created from {req.ostemplate} by {username}",
            )
        except Exception as e:
            _update_task(task_id, "failed", "Ошибка создания контейнера", 35,
                         error=f"Proxmox: {e}")
            return
        if not upid:
            _update_task(task_id, "failed", "Ошибка создания контейнера", 35,
                         error="Proxmox не вернул UPID задачи создания контейнера")
            return

        # Wait for creation task
        _update_task(task_id, "running", "Ожидание завершения создания...", 50,
                     vmid=new_vmid, node=deploy_node)
        success = client.wait_for_task(deploy_node, upid, timeout=300)
        if not success:
            _update_task(task_id, "failed", "Таймаут создания контейнера", 50,
                         error="Создание контейнера заняло слишком долго (>5 мин)")
            return

        _update_task(task_id, "running", "Контейнер создан", 70, vmid=new_vmid, node=deploy_node)

        # Start if requested
        if req.start_after_create:
            _update_task(task_id, "running", "Запуск контейнера...", 85,
                         vmid=new_vmid, node=deploy_node)
            client.start_container(deploy_node, new_vmid)

        # Save to DB
        _update_task(task_id, "running", "Сохранение в базе данных...", 95,
                     vmid=new_vmid, node=deploy_node)
        save_vm_instance(
            db=db,
            server_id=server.id,
            vmid=new_vmid,
            node=deploy_node,
            vm_type="lxc",
            name=req.name,
            cores=req.cores,
            memory=req.memory,
            disk_size=req.disk,
            ip_address=allocated_ip,
            ip_prefix=int(network_mask),
            gateway=gateway,
            cloud_init_user="root",
            cloud_init_password=req.password,
            ssh_keys=req.ssh_keys,
            template_id=None,
            template_name=req.ostemplate,
            description=f"Created from CT template {req.ostemplate}",
            owner_id=owner_id or user_id,
        )

        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="ct",
            resource_id=new_vmid,
            username=username,
            resource_name=req.name,
            server_id=server.id,
            server_name=server.name,
            node_name=deploy_node,
            details={
                "ostemplate": req.ostemplate,
                "cores": req.cores,
                "memory": req.memory,
                "disk": req.disk,
                "ip_address": allocated_ip,
            },
            success=True,
        )

        _update_task(task_id, "completed", "Контейнер успешно создан!", 100,
                     vmid=new_vmid, node=deploy_node)
        logger.info(f"[LXC DEPLOY] Task #{task_id}: {req.name} (VMID {new_vmid}) deployed by {username}")

    except Exception as e:
        err = str(e)
        logger.error(f"[LXC DEPLOY] Task #{task_id} failed: {err}")
        # Release IPAM allocation on failure
        try:
            if ipam_allocation is not None:
                ipam = IPAMService(db)
                ipam.release_ip(ipam_allocation.id)
                logger.info(f"[LXC DEPLOY] Task #{task_id}: Released IPAM IP {ipam_allocation.ip_address} on failure")
        except Exception as ipam_err:
            logger.warning(f"[LXC DEPLOY] Task #{task_id}: Could not release IPAM IP: {ipam_err}")
        try:
            if server:
                LoggingService.log_proxmox_action(
                    db=db,
                    action="create",
                    resource_type="ct",
                    resource_id=req.vmid,
                    username=username,
                    resource_name=req.name,
                    server_id=server.id,
                    server_name=server.name,
                    node_name=deploy_node,
                    details={"ostemplate": req.ostemplate},
                    success=False,
                    error_message=err,
                )
        except Exception:
            pass
        _update_task(task_id, "failed", f"Ошибка: {err[:150]}", 0, error=err)
    finally:
        db.close()


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/lxc-templates/{server_id}")
def list_lxc_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view")),
):
    """
    Список LXC CT-шаблонов (vztmpl) на хранилищах выбранного Proxmox сервера.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")

    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Не удалось подключиться к Proxmox серверу")

    node_filter = None
    if server.hostname and server.hostname != server.ip_address:
        node_filter = server.hostname

    templates = client.get_lxc_storage_templates(node=node_filter)
    return JSONResponse(content={"server_id": server_id, "templates": templates})


@router.get("/api/lxc-storages/{server_id}")
def list_lxc_storages(
    server_id: int,
    node: Optional[str] = None,
    content: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view")),
):
    """
    Список хранилищ ноды для дисков инстансов.

    content:
        'rootdir' — хранилища для rootfs LXC контейнера
        'images'  — хранилища для диска ВМ KVM (файлы диска)
        None      — обратная совместимость: rootdir ИЛИ images
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")

    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Не удалось подключиться к Proxmox серверу")

    target_node = node or server.hostname or None
    if not target_node:
        nodes = client.get_nodes()
        target_node = nodes[0]["node"] if nodes else None
    if not target_node:
        raise HTTPException(status_code=400, detail="Не удалось определить ноду")

    if content in ("rootdir", "images", "vztmpl", "import", "iso"):
        # Точная фильтрация по типу контента (активные/включённые хранилища)
        storages = client.get_download_target_storages(target_node, content)
    else:
        # Без параметра — прежнее поведение: rootdir ИЛИ images
        storages = [
            s for s in client.get_node_storages(target_node)
            if "rootdir" in (s.get("content", "") or "") or "images" in (s.get("content", "") or "")
        ]

    result = [{
        "storage": s.get("storage"),
        "type": s.get("type"),
        "avail": s.get("avail"),
        "total": s.get("total"),
        "used": s.get("used"),
        "active": s.get("active", 0),
    } for s in storages]
    return JSONResponse(content={"storages": result})


@router.post("/api/lxc/deploy", response_model=LXCDeployResponse, status_code=202)
async def deploy_lxc_from_template(
    req: LXCDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:create")),
):
    """
    Асинхронное создание LXC контейнера из CT-шаблона.
    Возвращает task_id для отслеживания статуса.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == req.server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    require_server_visible(db, current_user, server.id)

    if not req.ostemplate:
        raise HTTPException(status_code=400, detail="Необходимо указать ostemplate")
    # A vzdump archive is accepted by `pct create` too (that is a restore):
    # it would hand over another guest's filesystem.
    if not VZTMPL_VOLID_RE.match(req.ostemplate):
        raise HTTPException(status_code=400, detail="ostemplate must be a CT template (<storage>:vztmpl/<file>)")
    # root in a privileged container is root on the node.
    if not req.unprivileged and not can_view_all_instances(current_user):
        raise HTTPException(status_code=403, detail="Privileged containers require the vm:manage permission")
    if not req.name or len(req.name) < 1:
        raise HTTPException(status_code=400, detail="Укажите имя контейнера")
    from ...schemas import validate_instance_name
    try:
        req.name = validate_instance_name(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Resolve owner / SSH keys
    is_admin = current_user.has_permission("user:manage") or current_user.is_admin
    instance_owner_id = current_user.id
    if req.owner_id and req.owner_id != current_user.id:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admins can assign instance to another user")
        owner = db.query(User).filter(User.id == req.owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner user not found")
        instance_owner_id = owner.id

    # Enforce per-user resource quota (no-op if owner has no quota set)
    from ...services.quota_service import check_quota
    check_quota(db, instance_owner_id, add_cores=req.cores, add_memory_mb=req.memory, add_disk_gb=req.disk)

    from ..ssh_keys import build_deploy_ssh_keys
    req.ssh_keys = build_deploy_ssh_keys(
        db, current_user, instance_owner_id, req.ssh_key_ids, req.ssh_keys,
    ) or None

    # Create task record
    task = DeployTask(
        status="pending",
        step="В очереди...",
        progress=0,
        name=req.name,
        user_id=current_user.id,
        created_at=_utcnow(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _lxc_deploy_executor,
        _do_lxc_deploy,
        task_id,
        req,
        current_user.id,
        current_user.username,
        instance_owner_id,
    )

    return LXCDeployResponse(task_id=task_id, status="pending", name=req.name)
