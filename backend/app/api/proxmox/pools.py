from fastapi import APIRouter, Depends, Request, HTTPException
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


@router.get("/api/servers/{server_id}/pools")
async def list_pools(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("pool:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"pools": await _run_in_executor(client.get_pools)})
    except Exception as e:
        logger.error(f"Error listing pools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/pools/{poolid}")
async def get_pool(
    server_id: int,
    poolid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("pool:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"pool": await _run_in_executor(lambda: client.get_pool(poolid))})
    except Exception as e:
        logger.error(f"Error getting pool {poolid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/pools")
async def create_pool(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("pool:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    poolid = data.get("poolid")
    if not poolid:
        raise HTTPException(status_code=400, detail="poolid is required")
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_pool(poolid, data.get("comment"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating pool: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/pools/{poolid}")
async def update_pool(
    server_id: int,
    poolid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("pool:manage")),
):
    """Добавить/убрать участников пула (vms, storage) или изменить comment."""
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_pool(
            poolid,
            vms=data.get("vms"),
            storage=data.get("storage"),
            comment=data.get("comment"),
            delete=bool(data.get("delete")),
        )))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating pool {poolid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/pools/{poolid}")
async def delete_pool(
    server_id: int,
    poolid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("pool:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_pool(poolid)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting pool {poolid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
