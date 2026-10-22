"""
API endpoints for creating blank KVM VMs prepared for an OS install from an ISO.

Unlike the template deploy (`/templates/api/deploy`) and the cloud-image import
(`images.py`), nothing is cloned here: the VM gets an empty disk, the ISO is
mounted as a CD-ROM and put first in the boot order, so the guest boots straight
into the installer via the noVNC console.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...auth import PermissionChecker
from ...db import get_db, SessionLocal
from ...ipam_service import IPAMService
from ...logging_service import LoggingService
from ...models import DeployTask, IPAMNetwork, ProxmoxServer, User
from ...websocket_manager import broadcast_task_update
from ._helpers import (_get_proxmox_client, get_next_vmid, save_vm_instance,
                       require_server_visible, ISO_VOLID_RE, PveNode)

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])

_iso_vm_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="iso_vm")

DISK_BUSES = ("scsi", "sata", "virtio")
NET_MODELS = ("virtio", "e1000", "rtl8139", "vmxnet3")
BIOS_TYPES = ("seabios", "ovmf")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Pydantic schemas ─────────────────────────────────────────────────────────

class ISOVMCreateRequest(BaseModel):
    server_id: int
    node: PveNode
    name: str
    iso: str                       # volid, e.g. "local:iso/ubuntu-24.04.iso"
    vmid: Optional[int] = None
    cores: int = 2
    memory: int = 2048             # МБ
    disk: int = 32                 # ГБ
    storage: str = "local-lvm"
    bridge: str = "vmbr0"
    ostype: str = "l26"
    disk_bus: str = "scsi"
    net_model: str = "virtio"
    bios: str = "seabios"
    tpm: bool = False
    extra_iso: Optional[str] = None  # второй CD-ROM, обычно virtio-win с драйверами
    ip_address: Optional[str] = None
    gateway: Optional[str] = None
    ipam_network_id: Optional[int] = None
    ipam_pool_id: Optional[int] = None
    owner_id: Optional[int] = None
    start_after_create: bool = True
    onboot: bool = False


class ISOVMCreateResponse(BaseModel):
    task_id: int
    status: str
    name: str


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


def _do_iso_vm_create(task_id: int, req: ISOVMCreateRequest, user_id: int,
                      username: str, owner_id: Optional[int] = None):
    """Blocking VM creation — runs in a thread pool."""
    db = SessionLocal()
    server = None
    new_vmid = None
    ipam_allocation = None
    try:
        _update_task(task_id, "running", "Подготовка...", 5)

        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == req.server_id).first()
        if not server:
            _update_task(task_id, "failed", "Сервер не найден", 0, error="Сервер Proxmox не найден")
            return

        _update_task(task_id, "running", "Подключение к Proxmox...", 10)
        client = _get_proxmox_client(server)
        if not client.is_connected():
            _update_task(task_id, "failed", "Ошибка подключения к Proxmox", 10,
                         error="Не удалось подключиться к Proxmox серверу")
            return

        _update_task(task_id, "running", "Генерация VMID...", 20)
        new_vmid = req.vmid or get_next_vmid(db, server.id)

        # IPAM: адрес только резервируется — без cloud-init его придётся
        # прописать вручную в установщике, поэтому он попадает в шаг задачи.
        allocated_ip = req.ip_address
        gateway = req.gateway
        network_mask = "24"
        if req.ipam_network_id and not req.ip_address:
            _update_task(task_id, "running", "Резервирование IP-адреса (IPAM)...", 25,
                         vmid=new_vmid, node=req.node)
            ipam = IPAMService(db)
            ipam_allocation, error = ipam.auto_allocate_ip(
                network_id=req.ipam_network_id,
                pool_id=req.ipam_pool_id,
                resource_type="vm",
                resource_name=req.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=req.node,
                allocated_by=username,
                notes=f"Reserved for ISO install of VM {req.name}",
            )
            if not error:
                allocated_ip = ipam_allocation.ip_address
        elif req.ip_address and req.ipam_network_id:
            ipam = IPAMService(db)
            ipam_allocation, _ = ipam.allocate_ip(
                ip_address=req.ip_address,
                network_id=req.ipam_network_id,
                pool_id=req.ipam_pool_id,
                resource_type="vm",
                resource_name=req.name,
                proxmox_server_id=server.id,
                proxmox_vmid=new_vmid,
                proxmox_node=req.node,
                allocated_by=username,
                notes=f"Manually assigned for ISO install of VM {req.name}",
            )

        if req.ipam_network_id:
            try:
                network_id = req.ipam_network_id
                if ipam_allocation and hasattr(ipam_allocation, "network_id"):
                    network_id = ipam_allocation.network_id
                net = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
                if net:
                    if net.network and "/" in net.network:
                        network_mask = net.network.split("/")[1]
                    if not gateway and net.gateway:
                        gateway = net.gateway
            except Exception as e:
                logger.warning(f"Could not get network info from IPAM: {e}")

        # Create VM
        _update_task(task_id, "running", f"Создание ВМ VMID {new_vmid}...", 40,
                     vmid=new_vmid, node=req.node)
        upid = client.create_vm_from_iso(
            node=req.node,
            vmid=new_vmid,
            name=req.name,
            memory=req.memory,
            cores=req.cores,
            disk_storage=req.storage,
            disk_size=req.disk,
            iso_volid=req.iso,
            bridge=req.bridge,
            ostype=req.ostype,
            disk_bus=req.disk_bus,
            net_model=req.net_model,
            bios=req.bios,
            tpm=req.tpm,
            extra_iso_volid=req.extra_iso,
            onboot=req.onboot,
            description=f"Created for ISO install ({req.iso}) by {username}",
        )
        if not upid:
            _update_task(task_id, "failed", "Ошибка создания ВМ", 40,
                         error="Proxmox не вернул UPID задачи создания ВМ")
            return

        _update_task(task_id, "running", "Ожидание создания диска...", 60,
                     vmid=new_vmid, node=req.node)
        if not client.wait_for_task(req.node, upid, timeout=300):
            _update_task(task_id, "failed", "Таймаут создания ВМ", 60,
                         error="Создание ВМ заняло слишком долго (>5 мин)")
            return

        if req.start_after_create:
            _update_task(task_id, "running", "Запуск ВМ...", 85, vmid=new_vmid, node=req.node)
            client.start_vm(req.node, new_vmid)

        _update_task(task_id, "running", "Сохранение в базе данных...", 95,
                     vmid=new_vmid, node=req.node)
        save_vm_instance(
            db=db,
            server_id=server.id,
            vmid=new_vmid,
            node=req.node,
            vm_type="qemu",
            name=req.name,
            cores=req.cores,
            memory=req.memory,
            disk_size=req.disk,
            ip_address=allocated_ip,
            ip_prefix=int(network_mask),
            gateway=gateway,
            template_id=None,
            template_name=req.iso,
            description=f"Created for ISO install from {req.iso}",
            owner_id=owner_id or user_id,
        )

        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="vm",
            resource_id=new_vmid,
            username=username,
            resource_name=req.name,
            server_id=server.id,
            server_name=server.name,
            node_name=req.node,
            details={
                "iso": req.iso,
                "cores": req.cores,
                "memory": req.memory,
                "disk": req.disk,
                "bios": req.bios,
                "ip_address": allocated_ip,
            },
            success=True,
        )

        done_step = "ВМ создана — откройте консоль для установки ОС"
        if allocated_ip:
            done_step = f"ВМ создана, IP зарезервирован: {allocated_ip}/{network_mask}"
        _update_task(task_id, "completed", done_step, 100, vmid=new_vmid, node=req.node)
        logger.info(f"[ISO VM] Task #{task_id}: {req.name} (VMID {new_vmid}) created by {username}")

    except Exception as e:
        err = str(e)
        logger.error(f"[ISO VM] Task #{task_id} failed: {err}")
        try:
            if ipam_allocation is not None:
                IPAMService(db).release_ip(ipam_allocation.id)
                logger.info(f"[ISO VM] Task #{task_id}: Released IPAM IP {ipam_allocation.ip_address} on failure")
        except Exception as ipam_err:
            logger.warning(f"[ISO VM] Task #{task_id}: Could not release IPAM IP: {ipam_err}")
        try:
            if server:
                LoggingService.log_proxmox_action(
                    db=db,
                    action="create",
                    resource_type="vm",
                    resource_id=new_vmid,
                    username=username,
                    resource_name=req.name,
                    server_id=server.id,
                    server_name=server.name,
                    node_name=req.node,
                    details={"iso": req.iso},
                    success=False,
                    error_message=err,
                )
        except Exception:
            pass
        _update_task(task_id, "failed", f"Ошибка: {err[:150]}", 0, error=err)
    finally:
        db.close()


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/api/vm/create-from-iso", response_model=ISOVMCreateResponse, status_code=202)
async def create_vm_from_iso(
    req: ISOVMCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:create")),
):
    """
    Асинхронное создание пустой KVM ВМ с примонтированным установочным ISO.
    Возвращает task_id — прогресс идёт тем же каналом, что и деплой из шаблона.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == req.server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Укажите имя ВМ")
    from ...schemas import validate_instance_name
    try:
        req.name = validate_instance_name(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not req.node:
        raise HTTPException(status_code=400, detail="Укажите ноду для размещения ВМ")
    require_server_visible(db, current_user, server.id)
    if not req.iso:
        raise HTTPException(status_code=400, detail="Выберите установочный ISO-образ")
    # Only ISO volumes: any other volid (another guest's disk) would be
    # attached to the new VM as a CD-ROM.
    for volid in (req.iso, req.extra_iso):
        if volid and not ISO_VOLID_RE.match(volid):
            raise HTTPException(status_code=400, detail="ISO must be <storage>:iso/<file>")
    if req.disk_bus not in DISK_BUSES:
        raise HTTPException(status_code=400, detail=f"Шина диска должна быть одной из: {', '.join(DISK_BUSES)}")
    if req.net_model not in NET_MODELS:
        raise HTTPException(status_code=400, detail=f"Модель сетевой карты должна быть одной из: {', '.join(NET_MODELS)}")
    if req.bios not in BIOS_TYPES:
        raise HTTPException(status_code=400, detail=f"BIOS должен быть одним из: {', '.join(BIOS_TYPES)}")
    if req.disk < 1:
        raise HTTPException(status_code=400, detail="Размер диска должен быть не меньше 1 GB")

    # Resolve owner (admin may create the instance for another user)
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

    task = DeployTask(
        status="pending",
        kind="vm_iso",
        step="В очереди...",
        progress=0,
        name=req.name,
        server_id=server.id,
        user_id=current_user.id,
        created_at=_utcnow(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _iso_vm_executor,
        _do_iso_vm_create,
        task_id,
        req,
        current_user.id,
        current_user.username,
        instance_owner_id,
    )

    return ISOVMCreateResponse(task_id=task_id, status="pending", name=req.name)
