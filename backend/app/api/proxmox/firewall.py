from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from loguru import logger

from ...db import get_db
from ...models import ProxmoxServer, User
from ...auth import PermissionChecker
from ._helpers import _get_proxmox_client, require_vm_access
from ...proxmox import _run_in_executor

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


def _resolve_client(db: Session, server_id: int):
    """Достать сервер и подключённого клиента (или 404/503)."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
    return client


def _ok_or_400(result: dict):
    """Единый разбор ответа мутаций клиента ({success, error})."""
    if result.get("success"):
        return JSONResponse(content=result)
    raise HTTPException(status_code=400, detail=result.get("error", "Operation failed"))


# ==================== Options ====================

@router.get("/api/servers/{server_id}/firewall/options")
async def get_firewall_options(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"options": await _run_in_executor(client.get_cluster_firewall_options)})
    except Exception as e:
        logger.error(f"Error getting firewall options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/firewall/options")
async def update_firewall_options(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_cluster_firewall_options(**data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating firewall options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Datacenter rules ====================

@router.get("/api/servers/{server_id}/firewall/rules")
async def get_firewall_rules(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"rules": await _run_in_executor(client.get_cluster_firewall_rules)})
    except Exception as e:
        logger.error(f"Error getting firewall rules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/firewall/rules")
async def create_firewall_rule(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_cluster_firewall_rule(**data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating firewall rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/firewall/rules/{pos}")
async def update_firewall_rule(
    server_id: int,
    pos: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_cluster_firewall_rule(pos, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating firewall rule {pos}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/firewall/rules/{pos}")
async def delete_firewall_rule(
    server_id: int,
    pos: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_cluster_firewall_rule(pos)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting firewall rule {pos}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Security groups ====================

@router.get("/api/servers/{server_id}/firewall/groups")
async def get_firewall_groups(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"groups": await _run_in_executor(client.get_firewall_groups)})
    except Exception as e:
        logger.error(f"Error getting firewall groups: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/firewall/groups")
async def create_firewall_group(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    group = data.get("group")
    if not group:
        raise HTTPException(status_code=400, detail="Group name is required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.create_firewall_group(group, data.get("comment"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating firewall group: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/firewall/groups/{group}")
async def delete_firewall_group(
    server_id: int,
    group: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_firewall_group(group)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting firewall group {group}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/firewall/groups/{group}/rules")
async def get_firewall_group_rules(
    server_id: int,
    group: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"rules": await _run_in_executor(lambda: client.get_firewall_group_rules(group))})
    except Exception as e:
        logger.error(f"Error getting rules for group {group}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/firewall/groups/{group}/rules")
async def create_firewall_group_rule(
    server_id: int,
    group: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_firewall_group_rule(group, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating rule in group {group}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/firewall/groups/{group}/rules/{pos}")
async def update_firewall_group_rule(
    server_id: int,
    group: str,
    pos: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_firewall_group_rule(group, pos, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating rule {pos} in group {group}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/firewall/groups/{group}/rules/{pos}")
async def delete_firewall_group_rule(
    server_id: int,
    group: str,
    pos: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_firewall_group_rule(group, pos)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting rule {pos} in group {group}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== IP sets ====================

@router.get("/api/servers/{server_id}/firewall/ipsets")
async def get_firewall_ipsets(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"ipsets": await _run_in_executor(client.get_firewall_ipsets)})
    except Exception as e:
        logger.error(f"Error getting firewall ipsets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/firewall/ipsets")
async def create_firewall_ipset(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="IP set name is required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.create_firewall_ipset(name, data.get("comment"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating firewall ipset: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/firewall/ipsets/{name}")
async def delete_firewall_ipset(
    server_id: int,
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_firewall_ipset(name)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting firewall ipset {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/firewall/ipsets/{name}/entries")
async def get_firewall_ipset_entries(
    server_id: int,
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"entries": await _run_in_executor(lambda: client.get_firewall_ipset_entries(name))})
    except Exception as e:
        logger.error(f"Error getting entries for ipset {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/firewall/ipsets/{name}/entries")
async def add_firewall_ipset_entry(
    server_id: int,
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    cidr = data.get("cidr")
    if not cidr:
        raise HTTPException(status_code=400, detail="CIDR is required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.add_firewall_ipset_entry(
                name, cidr, data.get("comment"), bool(data.get("nomatch")))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding entry to ipset {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/firewall/ipsets/{name}/entries")
async def delete_firewall_ipset_entry(
    server_id: int,
    name: str,
    cidr: str = Query(..., description="CIDR записи (содержит '/', поэтому в query)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_firewall_ipset_entry(name, cidr)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting entry from ipset {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Aliases ====================

@router.get("/api/servers/{server_id}/firewall/aliases")
async def get_firewall_aliases(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"aliases": await _run_in_executor(client.get_firewall_aliases)})
    except Exception as e:
        logger.error(f"Error getting firewall aliases: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/firewall/aliases")
async def create_firewall_alias(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    name, cidr = data.get("name"), data.get("cidr")
    if not name or not cidr:
        raise HTTPException(status_code=400, detail="Name and CIDR are required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.create_firewall_alias(name, cidr, data.get("comment"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating firewall alias: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/firewall/aliases/{name}")
async def update_firewall_alias(
    server_id: int,
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    cidr = data.get("cidr")
    if not cidr:
        raise HTTPException(status_code=400, detail="CIDR is required")
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.update_firewall_alias(name, cidr, data.get("comment"), data.get("rename"))))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating firewall alias {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/firewall/aliases/{name}")
async def delete_firewall_alias(
    server_id: int,
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_firewall_alias(name)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting firewall alias {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Node-level: options ====================

@router.get("/api/servers/{server_id}/nodes/{node}/firewall/options")
async def get_node_firewall_options(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"options": await _run_in_executor(lambda: client.get_node_firewall_options(node))})
    except Exception as e:
        logger.error(f"Error getting node {node} firewall options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/firewall/options")
async def update_node_firewall_options(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_node_firewall_options(node, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating node {node} firewall options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Node-level: rules ====================

@router.get("/api/servers/{server_id}/nodes/{node}/firewall/rules")
async def get_node_firewall_rules(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"rules": await _run_in_executor(lambda: client.get_node_firewall_rules(node))})
    except Exception as e:
        logger.error(f"Error getting node {node} firewall rules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/firewall/rules")
async def create_node_firewall_rule(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.create_node_firewall_rule(node, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating node {node} firewall rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/firewall/rules/{pos}")
async def update_node_firewall_rule(
    server_id: int,
    node: str,
    pos: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.update_node_firewall_rule(node, pos, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating node {node} firewall rule {pos}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/nodes/{node}/firewall/rules/{pos}")
async def delete_node_firewall_rule(
    server_id: int,
    node: str,
    pos: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    client = _resolve_client(db, server_id)
    try:
        return _ok_or_400(await _run_in_executor(lambda: client.delete_node_firewall_rule(node, pos)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting node {node} firewall rule {pos}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/nodes/{node}/firewall/log")
async def get_node_firewall_log(
    server_id: int,
    node: str,
    limit: int = Query(100, ge=1, le=500),
    start: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"log": await _run_in_executor(lambda: client.get_node_firewall_log(node, limit, start))})
    except Exception as e:
        logger.error(f"Error getting node {node} firewall log: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Guest-level (VM/LXC) ====================

def _norm_type(vm_type: str) -> str:
    if vm_type not in ("qemu", "lxc", "vm", "ct"):
        raise HTTPException(status_code=400, detail="vm_type must be qemu/lxc")
    return vm_type


@router.get("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/options")
async def get_guest_firewall_options(
    server_id: int,
    vm_type: str,
    vmid: int,
    node: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    try:
        return JSONResponse(content={"options": await _run_in_executor(
            lambda: client.get_guest_firewall_options(node, vm_type, vmid))})
    except Exception as e:
        logger.error(f"Error getting guest {vmid} firewall options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/options")
async def update_guest_firewall_options(
    server_id: int,
    vm_type: str,
    vmid: int,
    request: Request,
    node: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.update_guest_firewall_options(node, vm_type, vmid, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating guest {vmid} firewall options: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/rules")
async def get_guest_firewall_rules(
    server_id: int,
    vm_type: str,
    vmid: int,
    node: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    try:
        return JSONResponse(content={"rules": await _run_in_executor(
            lambda: client.get_guest_firewall_rules(node, vm_type, vmid))})
    except Exception as e:
        logger.error(f"Error getting guest {vmid} firewall rules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/rules")
async def create_guest_firewall_rule(
    server_id: int,
    vm_type: str,
    vmid: int,
    request: Request,
    node: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.create_guest_firewall_rule(node, vm_type, vmid, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating guest {vmid} firewall rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/rules/{pos}")
async def update_guest_firewall_rule(
    server_id: int,
    vm_type: str,
    vmid: int,
    pos: int,
    request: Request,
    node: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    data = await request.json()
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.update_guest_firewall_rule(node, vm_type, vmid, pos, **data)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating guest {vmid} firewall rule {pos}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/rules/{pos}")
async def delete_guest_firewall_rule(
    server_id: int,
    vm_type: str,
    vmid: int,
    pos: int,
    node: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:manage")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    try:
        return _ok_or_400(await _run_in_executor(
            lambda: client.delete_guest_firewall_rule(node, vm_type, vmid, pos)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting guest {vmid} firewall rule {pos}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/guests/{vm_type}/{vmid}/firewall/log")
async def get_guest_firewall_log(
    server_id: int,
    vm_type: str,
    vmid: int,
    node: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    start: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    require_vm_access(db, current_user, server_id, vmid)
    client = _resolve_client(db, server_id)
    _norm_type(vm_type)
    try:
        return JSONResponse(content={"log": await _run_in_executor(
            lambda: client.get_guest_firewall_log(node, vm_type, vmid, limit, start))})
    except Exception as e:
        logger.error(f"Error getting guest {vmid} firewall log: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Macros (read-only) ====================

@router.get("/api/servers/{server_id}/firewall/macros")
async def get_firewall_macros(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("firewall:view")),
):
    client = _resolve_client(db, server_id)
    try:
        return JSONResponse(content={"macros": await _run_in_executor(client.get_firewall_macros)})
    except Exception as e:
        logger.error(f"Error getting firewall macros: {e}")
        raise HTTPException(status_code=500, detail=str(e))
