from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from loguru import logger

from ...db import get_db
from ...models import ProxmoxServer, User
from ...auth import PermissionChecker
from ._helpers import _get_proxmox_client
from ...proxmox import _run_in_executor

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


def _resolve_client(db: Session, server_id: int):
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
    return client


def _ok_or_400(result: dict):
    if result.get("success"):
        return JSONResponse(content=result)
    raise HTTPException(status_code=400, detail=result.get("error", "Operation failed"))


# ==================== Physical disks ====================

@router.get("/api/servers/{server_id}/nodes/{node}/disks")
async def list_disks(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"disks": await _run_in_executor(lambda: client.get_node_disks(node))})
    except Exception as e:
        logger.error(f"Error listing disks on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/nodes/{node}/disks/smart")
async def disk_smart(
    server_id: int,
    node: str,
    disk: str = Query(..., description="devpath, напр. /dev/sda"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"smart": await _run_in_executor(lambda: client.get_disk_smart(node, disk))})
    except Exception as e:
        logger.error(f"Error getting SMART for {disk} on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/disks/wipe")
async def wipe_disk(
    server_id: int,
    node: str,
    disk: str = Query(..., description="devpath, напр. /dev/sdb"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.wipe_disk(node, disk)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error wiping disk {disk} on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== ZFS pools ====================

@router.get("/api/servers/{server_id}/nodes/{node}/disks/zfs")
async def list_zfs_pools(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"pools": await _run_in_executor(lambda: client.get_zfs_pools(node))})
    except Exception as e:
        logger.error(f"Error listing ZFS pools on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/nodes/{node}/disks/zfs/{name}")
async def get_zfs_pool(
    server_id: int,
    node: str,
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"pool": await _run_in_executor(lambda: client.get_zfs_pool(node, name))})
    except Exception as e:
        logger.error(f"Error getting ZFS pool {name} on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/disks/zfs")
async def create_zfs_pool(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    name = data.get("name")
    devices = data.get("devices")
    if not name or not devices:
        raise HTTPException(status_code=400, detail="name and devices are required")
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_zfs_pool(
            node, name, devices,
            raidlevel=data.get("raidlevel", "single"),
            ashift=int(data.get("ashift", 12)),
            compression=data.get("compression", "on"),
            add_storage=bool(data.get("add_storage", True)),
        )))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating ZFS pool on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/nodes/{node}/disks/zfs/{name}")
async def destroy_zfs_pool(
    server_id: int,
    node: str,
    name: str,
    cleanup_disks: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("storage:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.destroy_zfs_pool(
            node, name, cleanup_config=True, cleanup_disks=cleanup_disks)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error destroying ZFS pool {name} on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
