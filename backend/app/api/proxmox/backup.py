"""
Backup & Storage API
====================
Endpoints for managing Proxmox storages, backup files,
creating backups (vzdump), restoring, and scheduled backup jobs.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from loguru import logger

from ...db import get_db
from ...models import BackupJob, ProxmoxServer, User
from ...auth import PermissionChecker
from ...logging_service import LoggingService
from ._helpers import (_get_proxmox_client, require_vm_access,
                       can_view_all_instances, get_owned_vmids,
                       require_node_name, require_pve_id)
from ...proxmox import _run_in_executor
from .tasks import ProxmoxTaskService, _require_upid_access

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


# vzdump archives: "<storage>:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst";
# PBS snapshots: "<storage>:backup/vm/101/2024-01-01T00:00:00Z".
_ARCHIVE_VMID_RES = (
    re.compile(r"vzdump-(?:qemu|lxc|openvz)-(\d+)-"),
    re.compile(r":backup/(?:vm|ct)/(\d+)/"),
)


def archive_vmid(volid: str) -> Optional[int]:
    """Guest VMID a backup archive belongs to, or None when it can't be told."""
    for rx in _ARCHIVE_VMID_RES:
        m = rx.search(volid or "")
        if m:
            return int(m.group(1))
    return None


_BACKUP_VOLID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*:backup/[^\s?#\\]+$")


def _require_backup_volid(volid) -> None:
    """A backup volid, with no '..' segments (it is split into URL path parts)."""
    if not (isinstance(volid, str) and _BACKUP_VOLID_RE.match(volid)) or ".." in volid.split("/"):
        raise HTTPException(status_code=400, detail="Invalid backup volume id")


def _require_archive_access(db: Session, user: User, server_id: int, volid: str) -> None:
    """Tenants may only touch backups of guests they own.

    Restoring someone else's archive into one's own guest (or a new VMID)
    hands over that guest's disks; deleting it destroys their backup.
    """
    if can_view_all_instances(user):
        return
    vmid = archive_vmid(volid)
    if vmid is None or vmid not in get_owned_vmids(db, user, server_id):
        raise HTTPException(status_code=403, detail="You do not have access to this backup")


def _require_new_vmid_allowed(user: User) -> None:
    # A restore to a fresh VMID creates a guest the panel does not track
    # (no owner, outside quotas) — keep it to users with the global view.
    if not can_view_all_instances(user):
        raise HTTPException(
            status_code=403,
            detail="Restoring to a new VMID requires the vm:manage permission",
        )


# ==================== Storage Management ====================

@router.get("/api/backups/storages/{server_id}")
def get_storages(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:view")),
):
    """List all storages configured on a Proxmox server"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        client = _get_proxmox_client(server)
        storages = client.get_cluster_storages()
        # Конфиг кластера общий для всех нод и ничего не говорит о том, поднялось ли
        # хранилище на конкретной ноде. Доступность берём из /cluster/resources —
        # один запрос на весь кластер.
        node_status = client.get_storage_node_status()
        for s in storages:
            statuses = node_status.get(s.get("storage"), [])
            s["nodes_status"] = statuses
            s["inactive_nodes"] = [n["node"] for n in statuses if not n["active"]]
        return JSONResponse(content={"storages": storages})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing storages for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/backups/scan-nfs/{server_id}")
def scan_nfs(
    server_id: int,
    server: str,
    node: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Scan an NFS server for available exports, to populate the storage-add dialog."""
    pve_server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not pve_server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        client = _get_proxmox_client(pve_server)
        target_node = node
        if not target_node:
            nodes = client.get_nodes()
            if not nodes:
                raise HTTPException(status_code=400, detail="No nodes available to scan from")
            target_node = nodes[0]["node"]
        exports = client.scan_nfs_exports(target_node, server)
        return JSONResponse(content={"exports": exports})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error scanning NFS exports on {server} for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/backups/storages/{server_id}")
async def create_storage(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Add a new storage to a Proxmox server"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    data = await request.json()
    storage_id = data.pop("storage", None)
    storage_type = data.pop("type", None)
    if not storage_id or not storage_type:
        raise HTTPException(status_code=400, detail="storage and type are required")

    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.create_storage, storage_id, storage_type, **data)
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="storage_create", resource_type="storage",
                resource_id=storage_id, username=current_user.username,
                server_id=server_id, server_name=server.name, success=True,
            )
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/backups/storages/{server_id}/{storage_id}")
async def update_storage(
    server_id: int,
    storage_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Update storage configuration"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    data = await request.json()
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.update_storage, storage_id, **data)
        if result.get("success"):
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/backups/storages/{server_id}/{storage_id}")
def delete_storage(
    server_id: int,
    storage_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Delete storage from Proxmox"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        client = _get_proxmox_client(server)
        result = client.delete_storage(storage_id)
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="storage_delete", resource_type="storage",
                resource_id=storage_id, username=current_user.username,
                server_id=server_id, server_name=server.name, success=True,
            )
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Backup Listing ====================

@router.get("/api/backups/list/{server_id}")
def list_backups(
    server_id: int,
    storage: str,
    node: Optional[str] = None,
    vmid: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:view")),
):
    """List backup files stored in a given storage.

    If ``node`` is omitted (or empty), backup files are aggregated from all
    cluster nodes that have the storage available. Duplicate ``volid`` entries
    (which appear on shared storages) are de-duplicated.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    # Tenants only see backups of the guests they own.
    owned: Optional[set] = None
    if not can_view_all_instances(current_user):
        owned = get_owned_vmids(db, current_user, server_id)
        if vmid is not None and vmid not in owned:
            return JSONResponse(content={"backups": []})
    try:
        client = _get_proxmox_client(server)

        nodes_to_query: list[str] = []
        if node:
            nodes_to_query = [node]
        else:
            try:
                cluster_nodes = client.get_nodes() or []
                nodes_to_query = [
                    n.get("node") for n in cluster_nodes
                    if n.get("node") and (n.get("status") in (None, "online"))
                ]
            except Exception as e:
                logger.warning(f"Failed to enumerate cluster nodes for backup listing: {e}")

        seen: set[str] = set()
        backups: list[dict] = []
        for n in nodes_to_query:
            try:
                items = client.list_backups(n, storage, vmid) or []
            except Exception as e:
                logger.warning(f"Skipping node {n} when listing backups: {e}")
                continue
            for item in items:
                if owned is not None:
                    try:
                        if int(item.get("vmid")) not in owned:
                            continue
                    except (TypeError, ValueError):
                        continue
                volid = item.get("volid")
                if volid and volid in seen:
                    continue
                if volid:
                    seen.add(volid)
                # annotate with the node where it was discovered (helpful for
                # restore / delete actions on non-shared storages)
                item.setdefault("node", n)
                backups.append(item)

        return JSONResponse(content={"backups": backups})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing backups: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/backups/{server_id}/backup")
async def delete_backup(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:delete")),
):
    """Delete a specific backup file"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    data = await request.json()
    node = data.get("node")
    storage = data.get("storage")
    volid = data.get("volid")
    if not all([node, storage, volid]):
        raise HTTPException(status_code=400, detail="node, storage, volid are required")
    # All three become Proxmox API path segments.
    require_node_name(node)
    require_pve_id(storage, "storage")
    _require_backup_volid(volid)
    _require_archive_access(db, current_user, server.id, volid)

    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.delete_backup, node, storage, volid)
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="backup_delete", resource_type="backup",
                resource_id=volid, username=current_user.username,
                server_id=server_id, server_name=server.name,
                node_name=node, success=True,
            )
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Create Backup ====================

@router.post("/api/backups/create")
async def create_backup(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:create")),
):
    """Trigger vzdump backup for a VM/container. Returns UPID for tracking."""
    data = await request.json()
    server_id = data.get("server_id")
    node = data.get("node")
    vmid = data.get("vmid")
    storage = data.get("storage")
    require_node_name(node)

    if not all([server_id, node, vmid, storage]):
        raise HTTPException(status_code=400, detail="server_id, node, vmid, storage are required")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="vmid must be an integer")
    require_vm_access(db, current_user, server.id, vmid)

    mode = data.get("mode", "snapshot")
    compress = data.get("compress", "zstd")
    remove = int(data.get("remove", 1))
    notes = data.get("notes")
    keep_last = data.get("keep_last")

    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(
            client.create_backup,
            node=node, vmid=int(vmid), storage=storage,
            mode=mode, compress=compress, remove=remove,
            keep_last=keep_last, notes=notes,
        )
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="backup_create", resource_type="backup",
                resource_id=str(vmid), username=current_user.username,
                server_id=server_id, server_name=server.name,
                node_name=node, success=True,
                details={"storage": storage, "mode": mode, "compress": compress},
            )
            return JSONResponse(content={"success": True, "upid": result.get("upid")})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Task Status ====================

@router.get("/api/backups/task/{server_id}/{node}/{upid:path}")
def get_task_status(
    server_id: int,
    node: str,
    upid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:view")),
):
    """Get Proxmox task status and log by UPID"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    _require_upid_access(db, current_user, server_id, upid)
    try:
        client = _get_proxmox_client(server)
        status = client.get_task_status(node, upid)
        log = client.get_task_log(node, upid)
        return JSONResponse(content={"status": status, "log": log})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Restore ====================

@router.post("/api/backups/restore")
async def restore_backup(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:restore")),
):
    """Restore a VM or LXC from a backup file. Returns UPID."""
    data = await request.json()
    server_id = data.get("server_id")
    node = data.get("node")
    vmid = data.get("vmid")
    archive = data.get("archive")   # volume id
    require_node_name(node)
    # Target storage for the restored disks. Empty => "from backup configuration"
    # (disks go to the storage recorded in the backup). NOT the backup storage.
    storage = data.get("storage") or None
    vm_type = data.get("vm_type", "qemu")
    new_vmid = data.get("new_vmid")
    start = bool(data.get("start", False))
    unique = bool(data.get("unique", True))

    if not all([server_id, node, vmid, archive]):
        raise HTTPException(
            status_code=400,
            detail="server_id, node, vmid, archive are required",
        )

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="vmid must be an integer")
    # Restoring without new_vmid overwrites the original guest.
    require_vm_access(db, current_user, server.id, vmid)
    _require_archive_access(db, current_user, server.id, archive)
    if new_vmid:
        _require_new_vmid_allowed(current_user)

    try:
        client = _get_proxmox_client(server)
        if vm_type == "lxc":
            result = await _run_in_executor(
                client.restore_lxc,
                node=node, vmid=int(vmid), archive=archive,
                storage=storage, new_vmid=int(new_vmid) if new_vmid else None,
                start=start,
            )
        else:
            result = await _run_in_executor(
                client.restore_vm,
                node=node, vmid=int(vmid), archive=archive,
                storage=storage, new_vmid=int(new_vmid) if new_vmid else None,
                start=start, unique=unique,
            )

        if result.get("success"):
            target_vmid = int(new_vmid) if new_vmid else int(vmid)
            upid = result.get("upid")
            # Track the restore as a Proxmox UPID task so it shows up on the Tasks
            # page and the poller can report live progress / final status.
            if upid:
                try:
                    ptask = ProxmoxTaskService.register(
                        db=db, upid=upid, server_id=server_id, user_id=current_user.id,
                        action="backup_restore", node=node, vmid=target_vmid,
                        vm_type=vm_type, description=f"Восстановление {vm_type.upper()} {target_vmid}",
                    )
                    ptask.progress = 0
                    db.commit()
                except Exception as _te:
                    logger.warning(f"Could not register restore task: {_te}")
            LoggingService.log_proxmox_action(
                db=db, action="backup_restore", resource_type="backup",
                resource_id=str(vmid), username=current_user.username,
                server_id=server_id, server_name=server.name,
                node_name=node, success=True,
                details={"archive": archive, "storage": storage, "vm_type": vm_type},
            )
            return JSONResponse(content={"success": True, "upid": upid})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/backups/restore-bulk")
async def restore_backup_bulk(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:restore")),
):
    """Restore several backups at once.

    Body:
        server_id (int), storage (str)
        mode: "in_place" (overwrite original VMID, force) | "new_vmid" (clone to fresh VMIDs)
        start (bool): start each guest after restore
        items: [{node, vmid, archive, vm_type}]  — expected one archive per distinct VMID

    Returns a per-item result list with UPIDs / errors.
    """
    data = await request.json()
    server_id = data.get("server_id")
    # Target storage for restored disks. Empty => "from backup configuration".
    storage = data.get("storage") or None
    mode = data.get("mode", "in_place")
    start = bool(data.get("start", False))
    items = data.get("items", [])

    if not server_id or not items:
        raise HTTPException(status_code=400, detail="server_id and items are required")
    if mode not in ("in_place", "new_vmid"):
        raise HTTPException(status_code=400, detail="mode must be 'in_place' or 'new_vmid'")

    if not isinstance(items, list) or len(items) > 200:
        raise HTTPException(status_code=400, detail="items must be a list of at most 200 entries")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Authorize every item before restoring any: an in-place restore is a
    # forced overwrite of the guest.
    if mode == "new_vmid":
        _require_new_vmid_allowed(current_user)
    for it in items:
        if not isinstance(it, dict):
            raise HTTPException(status_code=400, detail="items must be objects")
        require_node_name(it.get("node"))
        try:
            item_vmid = int(it.get("vmid"))
        except (TypeError, ValueError):
            continue  # reported per item below
        if it.get("archive"):
            require_vm_access(db, current_user, server.id, item_vmid)
            _require_archive_access(db, current_user, server.id, it["archive"])

    client = _get_proxmox_client(server)
    allocated: set = set()

    def alloc_vmid(start_from: int):
        """Find the next free VMID at/above start_from, skipping ones already handed out."""
        candidate = start_from
        for _ in range(10000):
            if candidate in allocated:
                candidate += 1
                continue
            try:
                # /cluster/nextid?vmid=X returns X if free, raises if taken
                ok = client.proxmox.cluster.nextid.get(vmid=candidate)
                chosen = int(ok) if ok else candidate
                allocated.add(chosen)
                return chosen
            except Exception:
                candidate += 1
        return None

    next_start = None
    if mode == "new_vmid":
        next_start = await _run_in_executor(client.get_next_vmid) or 100

    results = []
    for it in items:
        node = it.get("node")
        vmid = it.get("vmid")
        archive = it.get("archive")
        vm_type = it.get("vm_type", "qemu")

        if not node or not vmid or not archive:
            results.append({"vmid": vmid, "archive": archive, "success": False,
                            "error": "node, vmid and archive are required"})
            continue

        new_vmid = None
        if mode == "new_vmid":
            new_vmid = await _run_in_executor(alloc_vmid, next_start)
            if not new_vmid:
                results.append({"vmid": vmid, "archive": archive, "success": False,
                                "error": "No free VMID available"})
                continue
            next_start = new_vmid + 1

        force = (mode == "in_place")
        try:
            if vm_type == "lxc":
                r = await _run_in_executor(
                    client.restore_lxc,
                    node=node, vmid=int(vmid), archive=archive, storage=storage,
                    new_vmid=new_vmid, start=start, force=force,
                )
            else:
                r = await _run_in_executor(
                    client.restore_vm,
                    node=node, vmid=int(vmid), archive=archive, storage=storage,
                    new_vmid=new_vmid, start=start, unique=(mode == "new_vmid"), force=force,
                )
            target = new_vmid or int(vmid)
            if r.get("success"):
                results.append({"vmid": vmid, "target_vmid": target, "archive": archive,
                                "success": True, "upid": r.get("upid")})
                upid = r.get("upid")
                if upid:
                    try:
                        ptask = ProxmoxTaskService.register(
                            db=db, upid=upid, server_id=server_id, user_id=current_user.id,
                            action="backup_restore", node=node, vmid=target,
                            vm_type=vm_type, description=f"Восстановление {vm_type.upper()} {target}",
                        )
                        ptask.progress = 0
                        db.commit()
                    except Exception as _te:
                        logger.warning(f"Could not register bulk restore task: {_te}")
                LoggingService.log_proxmox_action(
                    db=db, action="backup_restore", resource_type="backup",
                    resource_id=str(target), username=current_user.username,
                    server_id=server_id, server_name=server.name, node_name=node, success=True,
                    details={"archive": archive, "storage": storage, "vm_type": vm_type,
                             "mode": mode, "bulk": True},
                )
            else:
                results.append({"vmid": vmid, "target_vmid": target, "archive": archive,
                                "success": False, "error": r.get("error", "Failed")})
        except Exception as e:
            results.append({"vmid": vmid, "archive": archive, "success": False, "error": str(e)})

    succeeded = sum(1 for r in results if r["success"])
    return JSONResponse(content={
        "success": succeeded > 0,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    })


# ==================== Scheduled Backup Jobs ====================

@router.get("/api/backups/proxmox-jobs/{server_id}")
def list_proxmox_native_jobs(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:view")),
):
    """Fetch native vzdump backup schedules configured directly in Proxmox"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        client = _get_proxmox_client(server)
        jobs = client.get_cluster_backup_jobs()
        return JSONResponse(content={"jobs": jobs, "server_name": server.name})
    except Exception as e:
        logger.error(f"Error fetching proxmox native jobs for server {server_id}: {e}")
        return JSONResponse(content={"jobs": [], "error": str(e)})


def _build_native_job_props(data: dict, *, for_create: bool) -> dict:
    """Собрать параметры vzdump для POST/PUT /cluster/backup из тела запроса."""
    props: dict = {}

    schedule = data.get("schedule")
    if schedule:
        props["schedule"] = schedule
    storage = data.get("storage")
    if storage:
        props["storage"] = storage
    if data.get("mode"):
        props["mode"] = data["mode"]
    if data.get("compress") is not None:
        props["compress"] = data["compress"]
    props["enabled"] = 1 if data.get("enabled", True) else 0
    if data.get("comment"):
        props["comment"] = data["comment"]
    if data.get("mailto"):
        props["mailto"] = data["mailto"]
    if data.get("notes_template"):
        props["notes-template"] = data["notes_template"]
    if data.get("node"):
        props["node"] = data["node"]

    # Выбор гостей: все / список VMID / пул. Взаимоисключающие поля в PVE —
    # при обновлении чистим неиспользуемые через delete, иначе PVE ругается.
    selection = data.get("selection_mode", "all")
    to_delete: list[str] = []
    if selection == "all":
        props["all"] = 1
        to_delete += ["vmid", "pool"]
    elif selection == "pool" and data.get("pool"):
        props["pool"] = data["pool"]
        to_delete += ["vmid", "all"]
    elif selection == "vmid":
        vmid = data.get("vmid")
        if isinstance(vmid, list):
            vmid = ",".join(str(v) for v in vmid)
        if vmid:
            props["vmid"] = vmid
        to_delete += ["all", "pool"]
    if not for_create and to_delete:
        props["delete"] = ",".join(to_delete)

    # Retention → prune-backups="keep-last=N,keep-daily=N,..."
    keep_parts = []
    for key, pve_key in (
        ("keep_last", "keep-last"), ("keep_daily", "keep-daily"),
        ("keep_weekly", "keep-weekly"), ("keep_monthly", "keep-monthly"),
    ):
        val = data.get(key)
        if val:
            keep_parts.append(f"{pve_key}={int(val)}")
    if keep_parts:
        props["prune-backups"] = ",".join(keep_parts)

    return props


@router.post("/api/backups/proxmox-jobs/{server_id}")
async def create_proxmox_native_job(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Создать нативное задание бэкапа Proxmox (cluster/backup)."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    data = await request.json()
    if not data.get("schedule") or not data.get("storage"):
        raise HTTPException(status_code=400, detail="schedule and storage are required")

    props = _build_native_job_props(data, for_create=True)
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.create_cluster_backup_job, props)
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="create", resource_type="backup_job",
                resource_id=str(props.get("id") or "native"), username=current_user.username,
                server_id=server_id, server_name=server.name, success=True,
                details={"native": True, "schedule": props.get("schedule")},
            )
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/backups/proxmox-jobs/{server_id}/{job_id}")
async def update_proxmox_native_job(
    server_id: int,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Обновить нативное задание бэкапа Proxmox (cluster/backup/{id})."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    data = await request.json()
    props = _build_native_job_props(data, for_create=False)
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.update_cluster_backup_job, job_id, props)
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="update", resource_type="backup_job",
                resource_id=job_id, username=current_user.username,
                server_id=server_id, server_name=server.name, success=True,
                details={"native": True},
            )
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/backups/proxmox-jobs/{server_id}/{job_id}")
async def delete_proxmox_native_job(
    server_id: int,
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Удалить нативное задание бэкапа Proxmox (cluster/backup/{id})."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.delete_cluster_backup_job, job_id)
        if result.get("success"):
            LoggingService.log_proxmox_action(
                db=db, action="delete", resource_type="backup_job",
                resource_id=job_id, username=current_user.username,
                server_id=server_id, server_name=server.name, success=True,
                details={"native": True},
            )
            return JSONResponse(content={"success": True})
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _is_job_admin(user: User) -> bool:
    return bool(user.is_admin or (user.role and user.role.name == "admin"))


def _get_job_for_user(db: Session, user: User, job_id: int) -> BackupJob:
    job = db.query(BackupJob).filter(BackupJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not (_is_job_admin(user) or job.owner_id == user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    return job


def _clean_job_vmids(db: Session, user: User, server_id: int, vmids) -> list:
    """Normalize ``vmids`` to ``["all"]`` or a list of ints the user may back up.

    A job backs up with panel credentials and prunes older archives of every
    listed guest (keep_last), so a tenant listing someone else's VMID could
    wipe that guest's backups.
    """
    if isinstance(vmids, str):
        vmids = [v.strip() for v in vmids.split(",") if v.strip()]
    if not isinstance(vmids, list) or not vmids:
        raise HTTPException(status_code=400, detail="vmids must be a non-empty list or 'all'")
    if vmids == ["all"]:
        if not can_view_all_instances(user):
            raise HTTPException(status_code=403, detail="Backing up all guests requires vm:manage")
        return ["all"]
    try:
        cleaned = sorted({int(v) for v in vmids})
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="vmids must be integers or 'all'")
    if any(v < 100 for v in cleaned):
        raise HTTPException(status_code=400, detail="Invalid VMID")
    for v in cleaned:
        require_vm_access(db, user, server_id, v)
    return cleaned


def _check_cron(expr) -> str:
    from apscheduler.triggers.cron import CronTrigger
    if not isinstance(expr, str) or len(expr.split()) != 5:
        raise HTTPException(status_code=400, detail="cron_expression must have 5 fields")
    try:
        CronTrigger.from_crontab(expr, timezone="UTC")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
    return expr.strip()


def _int_field(data: dict, name: str, default: int) -> int:
    try:
        value = int(data.get(name, default))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer")
    if value < 0:
        raise HTTPException(status_code=400, detail=f"{name} must not be negative")
    return value


@router.get("/api/backups/jobs")
def list_backup_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:view")),
):
    """List scheduled backup jobs"""
    if current_user.is_admin or (current_user.role and current_user.role.name == "admin"):
        jobs = db.query(BackupJob).all()
    else:
        jobs = db.query(BackupJob).filter(BackupJob.owner_id == current_user.id).all()

    return JSONResponse(content={"jobs": [j.to_dict() for j in jobs]})


@router.post("/api/backups/jobs")
async def create_backup_job(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Create a new scheduled backup job"""
    data = await request.json()

    required = ["server_id", "node", "vmids", "storage", "cron_expression"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {', '.join(missing)}")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == data["server_id"]).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    job = BackupJob(
        server_id=server.id,
        node=data["node"],
        vmids=_clean_job_vmids(db, current_user, server.id, data["vmids"]),
        storage=data["storage"],
        mode=data.get("mode", "snapshot"),
        compress=data.get("compress", "zstd"),
        notes=data.get("notes"),
        keep_last=_int_field(data, "keep_last", 3),
        keep_daily=_int_field(data, "keep_daily", 7),
        keep_weekly=_int_field(data, "keep_weekly", 4),
        keep_monthly=_int_field(data, "keep_monthly", 6),
        cron_expression=_check_cron(data["cron_expression"]),
        enabled=bool(data.get("enabled", True)),
        owner_id=current_user.id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Register with scheduler
    try:
        from ...services.backup_scheduler import scheduler
        scheduler.add_job_from_model(job)
    except Exception as e:
        logger.warning(f"Could not register job {job.id} with scheduler: {e}")

    LoggingService.log_proxmox_action(
        db=db, action="create", resource_type="backup_job",
        resource_id=str(job.id), username=current_user.username, success=True,
        server_id=job.server_id, node_name=job.node,
    )
    return JSONResponse(content={"success": True, "job": job.to_dict()})


@router.put("/api/backups/jobs/{job_id}")
async def update_backup_job(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Update a scheduled backup job"""
    job = _get_job_for_user(db, current_user, job_id)

    data = await request.json()
    if "vmids" in data:
        data["vmids"] = _clean_job_vmids(db, current_user, job.server_id, data["vmids"])
    if "cron_expression" in data:
        data["cron_expression"] = _check_cron(data["cron_expression"])
    for name in ("keep_last", "keep_daily", "keep_weekly", "keep_monthly"):
        if name in data:
            data[name] = _int_field(data, name, 0)
    if "enabled" in data:
        data["enabled"] = bool(data["enabled"])
    updatable = ["node", "vmids", "storage", "mode", "compress", "notes",
                 "keep_last", "keep_daily", "keep_weekly", "keep_monthly",
                 "cron_expression", "enabled"]
    for field in updatable:
        if field in data:
            setattr(job, field, data[field])
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)

    try:
        from ...services.backup_scheduler import scheduler
        scheduler.update_job_from_model(job)
    except Exception as e:
        logger.warning(f"Could not update job {job.id} in scheduler: {e}")

    return JSONResponse(content={"success": True, "job": job.to_dict()})


@router.delete("/api/backups/jobs/{job_id}")
def delete_backup_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Delete a scheduled backup job"""
    job = _get_job_for_user(db, current_user, job_id)

    try:
        from ...services.backup_scheduler import scheduler
        scheduler.remove_job(job_id)
    except Exception as e:
        logger.warning(f"Could not remove job {job_id} from scheduler: {e}")

    db.delete(job)
    db.commit()
    return JSONResponse(content={"success": True})


@router.patch("/api/backups/jobs/{job_id}/toggle")
def toggle_backup_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:manage")),
):
    """Enable or disable a scheduled backup job"""
    job = _get_job_for_user(db, current_user, job_id)

    job.enabled = not job.enabled
    job.updated_at = datetime.now(timezone.utc)
    db.commit()

    try:
        from ...services.backup_scheduler import scheduler
        if job.enabled:
            scheduler.add_job_from_model(job)
        else:
            scheduler.remove_job(job_id)
    except Exception as e:
        logger.warning(f"Could not toggle job {job_id} in scheduler: {e}")

    return JSONResponse(content={"success": True, "enabled": job.enabled})


@router.post("/api/backups/jobs/{job_id}/run-now")
def run_backup_job_now(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("backup:create")),
):
    """Trigger a scheduled backup job immediately"""
    _get_job_for_user(db, current_user, job_id)

    try:
        from ...services.backup_scheduler import run_backup_job
        import asyncio
        # Run in background - fire and forget via thread
        import threading
        thread = threading.Thread(
            target=lambda: asyncio.run(run_backup_job(job_id)),
            daemon=True,
        )
        thread.start()
        return JSONResponse(content={"success": True, "message": "Job triggered"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
