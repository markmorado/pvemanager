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


# ==================== Realms ====================

@router.get("/api/servers/{server_id}/access/realms")
async def list_realms(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"realms": await _run_in_executor(client.get_realms)})
    except Exception as e:
        logger.error(f"Error listing realms: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/access/realms")
async def create_realm(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    realm = data.pop("realm", None)
    realm_type = data.pop("type", None)
    if not realm or not realm_type:
        raise HTTPException(status_code=400, detail="realm and type are required")
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_realm(realm, realm_type, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating realm: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/access/realms/{realm}")
async def update_realm(
    server_id: int,
    realm: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_realm(realm, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating realm {realm}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/access/realms/{realm}")
async def delete_realm(
    server_id: int,
    realm: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_realm(realm)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting realm {realm}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Users & API tokens ====================

@router.get("/api/servers/{server_id}/access/users")
async def list_access_users(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"users": await _run_in_executor(client.get_access_users)})
    except Exception as e:
        logger.error(f"Error listing access users: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/access/users/{userid}/tokens")
async def list_user_tokens(
    server_id: int,
    userid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"tokens": await _run_in_executor(lambda: client.get_user_tokens(userid))})
    except Exception as e:
        logger.error(f"Error listing tokens for {userid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/access/users/{userid}/tokens/{tokenid}")
async def create_user_token(
    server_id: int,
    userid: str,
    tokenid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:manage")),
):
    """Создать API-токен. Секрет (value) возвращается один раз."""
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_user_token(
            userid, tokenid,
            comment=data.get("comment"),
            privsep=bool(data.get("privsep", True)),
            expire=data.get("expire"),
        )))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating token for {userid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/access/users/{userid}/tokens/{tokenid}")
async def delete_user_token(
    server_id: int,
    userid: str,
    tokenid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("access:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_user_token(userid, tokenid)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting token {tokenid} for {userid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
