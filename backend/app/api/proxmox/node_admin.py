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


# ==================== Power: reboot / shutdown ====================

@router.post("/api/servers/{server_id}/nodes/{node}/status")
async def node_power_action(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:power")),
):
    """Управление питанием ноды: reboot | shutdown (как в UI Proxmox)."""
    data = await request.json()
    command = data.get("command")
    if command not in ("reboot", "shutdown"):
        raise HTTPException(status_code=400, detail="command must be reboot or shutdown")
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.node_power_action(node, command)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error {command} node {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Syslog ====================

@router.get("/api/servers/{server_id}/nodes/{node}/syslog")
async def node_syslog(
    server_id: int,
    node: str,
    limit: int = 500,
    start: int = 0,
    service: str = None,
    since: str = None,
    until: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"syslog": await _run_in_executor(
            lambda: client.get_node_syslog(node, limit, start, service, since, until))})
    except Exception as e:
        logger.error(f"Error getting syslog on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Task history ====================

@router.get("/api/servers/{server_id}/nodes/{node}/tasks")
async def node_tasks(
    server_id: int,
    node: str,
    limit: int = 100,
    start: int = 0,
    errors: bool = False,
    typefilter: str = None,
    userfilter: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"tasks": await _run_in_executor(
            lambda: client.get_node_tasks(node, limit, start, errors, typefilter, userfilter))})
    except Exception as e:
        logger.error(f"Error getting tasks on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/nodes/{node}/tasks/{upid}/log")
async def node_task_log(
    server_id: int,
    node: str,
    upid: str,
    limit: int = 500,
    start: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={
            "log": await _run_in_executor(lambda: client.get_node_task_log(node, upid, limit, start)),
            "status": await _run_in_executor(lambda: client.get_node_task_status(node, upid)),
        })
    except Exception as e:
        logger.error(f"Error getting task log {upid} on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== System: DNS / hosts / time ====================

@router.get("/api/servers/{server_id}/nodes/{node}/dns")
async def get_node_dns(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"dns": await _run_in_executor(lambda: client.get_node_dns(node))})
    except Exception as e:
        logger.error(f"Error getting DNS on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/dns")
async def set_node_dns(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.set_node_dns(
            node, data.get("search"), data.get("dns1"), data.get("dns2"), data.get("dns3"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting DNS on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/nodes/{node}/hosts")
async def get_node_hosts(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"hosts": await _run_in_executor(lambda: client.get_node_hosts(node))})
    except Exception as e:
        logger.error(f"Error getting hosts on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/hosts")
async def set_node_hosts(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    hosts_data = data.get("data")
    if hosts_data is None:
        raise HTTPException(status_code=400, detail="data is required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.set_node_hosts(node, hosts_data, data.get("digest"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting hosts on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/nodes/{node}/time")
async def get_node_time(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"time": await _run_in_executor(lambda: client.get_node_time(node))})
    except Exception as e:
        logger.error(f"Error getting time on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/time")
async def set_node_timezone(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    timezone = data.get("timezone")
    if not timezone:
        raise HTTPException(status_code=400, detail="timezone is required")
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.set_node_timezone(node, timezone)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting timezone on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Services ====================

@router.get("/api/servers/{server_id}/nodes/{node}/services")
async def list_services(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"services": await _run_in_executor(lambda: client.get_node_services(node))})
    except Exception as e:
        logger.error(f"Error listing services on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/services/{service}/{action}")
async def service_action(
    server_id: int,
    node: str,
    service: str,
    action: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    if action not in ("start", "stop", "restart", "reload"):
        raise HTTPException(status_code=400, detail="action must be start/stop/restart/reload")
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.node_service_action(node, service, action)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error {action} service {service} on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== APT: updates ====================

@router.get("/api/servers/{server_id}/nodes/{node}/apt/updates")
async def apt_updates(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"updates": await _run_in_executor(lambda: client.get_apt_updates(node))})
    except Exception as e:
        logger.error(f"Error getting APT updates on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/apt/refresh")
async def apt_refresh(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.apt_refresh(node)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing APT on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== APT: repositories ====================

@router.get("/api/servers/{server_id}/nodes/{node}/apt/repositories")
async def apt_repositories(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"repositories": await _run_in_executor(lambda: client.get_apt_repositories(node))})
    except Exception as e:
        logger.error(f"Error getting APT repositories on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/apt/repositories")
async def set_apt_repository(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    """Включить/выключить репозиторий (path + index + enabled)."""
    client = _resolve_client(db, server_id)
    data = await request.json()
    path = data.get("path")
    index = data.get("index")
    if not path or index is None:
        raise HTTPException(status_code=400, detail="path and index are required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.set_apt_repository(node, path, int(index), bool(data.get("enabled")))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling APT repo on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== TLS certificates ====================

@router.get("/api/servers/{server_id}/nodes/{node}/certificates")
async def node_certificates(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"certificates": await _run_in_executor(
            lambda: client.get_node_certificates(node))})
    except Exception as e:
        logger.error(f"Error getting certificates on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/certificates")
async def upload_node_certificate(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    """Загрузить пользовательский сертификат pveproxy (PEM cert + key)."""
    client = _resolve_client(db, server_id)
    data = await request.json()
    certificates = data.get("certificates")
    if not certificates:
        raise HTTPException(status_code=400, detail="certificates (PEM) is required")
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.upload_node_certificate(
            node, certificates, data.get("key"),
            bool(data.get("force", True)), bool(data.get("restart", True)))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading certificate on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/nodes/{node}/certificates")
async def delete_node_certificate(
    server_id: int,
    node: str,
    restart: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    """Удалить пользовательский сертификат (вернуть самоподписанный)."""
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.delete_node_certificate(node, restart)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting certificate on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/apt/repositories")
async def add_standard_repository(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("node:manage")),
):
    """Добавить стандартный репозиторий PVE по handle."""
    client = _resolve_client(db, server_id)
    data = await request.json()
    handle = data.get("handle")
    if not handle:
        raise HTTPException(status_code=400, detail="handle is required")
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.add_apt_standard_repository(node, handle)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding standard repo on {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
