from fastapi import APIRouter, Depends, Request, HTTPException, Query, Form, status, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from loguru import logger
from typing import List
import ssl
import asyncio
import httpx
import websockets

from ...db import get_db
from ...models import ProxmoxServer, VMInstance, User, IPAMAllocation, IPAMNetwork, VMSnapshotArchive
from ...schemas import ProxmoxServerCreate, ProxmoxServerUpdate, ProxmoxServerResponse
from ...proxmox import ProxmoxClient, get_proxmox_resources, _run_in_executor
from ...auth import get_current_user, PermissionChecker, require_permission, check_permission
from ...logging_service import LoggingService
from ...ipam_service import IPAMService
from ._helpers import (check_vm_access, require_vm_access, _get_proxmox_client,
                        get_next_vmid, archive_and_delete_snapshots,
                        save_vm_instance, get_vm_instance, soft_delete_vm_instance)

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


# ==================== Snapshots ====================

@router.get("/api/{server_id}/vm/{vmid}/snapshots")
def get_vm_snapshots(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Get all snapshots for a VM"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        snapshots = client.get_vm_snapshots(node, vmid)
        # Filter out 'current' pseudo-snapshot if present
        snapshots = [s for s in snapshots if s.get('name') != 'current']
        return JSONResponse(content={"snapshots": snapshots})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VM {vmid} snapshots: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/snapshots")
async def create_vm_snapshot(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Create a VM snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    snapname = data.get('snapname')
    description = data.get('description')
    vmstate = data.get('vmstate', False)
    
    if not snapname:
        raise HTTPException(status_code=400, detail="Snapshot name is required")
    
    try:
        client = _get_proxmox_client(server)
        
        result = await _run_in_executor(client.create_vm_snapshot, node, vmid, snapname, description, vmstate)
        
        if result.get('success'):
            LoggingService.log_proxmox_action(
                db=db,
                action="snapshot_create",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=snapname,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=True
            )
            logger.info(f"User {current_user.username} created snapshot {snapname} for VM {vmid}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to create snapshot'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating VM {vmid} snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/vm/{vmid}/snapshots/{snapname}")
def delete_vm_snapshot(
    server_id: int,
    vmid: int,
    snapname: str,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Delete a VM snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        result = client.delete_vm_snapshot(node, vmid, snapname)
        
        if result.get('success'):
            LoggingService.log_proxmox_action(
                db=db,
                action="snapshot_delete",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=snapname,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=True
            )
            logger.info(f"User {current_user.username} deleted snapshot {snapname} for VM {vmid}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to delete snapshot'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting VM {vmid} snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/snapshots/{snapname}/rollback")
def rollback_vm_snapshot(
    server_id: int,
    vmid: int,
    snapname: str,
    node: str,
    start: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Rollback a VM to a snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        result = client.rollback_vm_snapshot(node, vmid, snapname, start)
        
        if result.get('success'):
            LoggingService.log_proxmox_action(
                db=db,
                action="snapshot_rollback",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=snapname,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=True
            )
            logger.info(f"User {current_user.username} rolled back VM {vmid} to snapshot {snapname}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to rollback snapshot'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rolling back VM {vmid} to snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Container Snapshots

@router.get("/api/{server_id}/container/{vmid}/snapshots")
def get_container_snapshots(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Get all snapshots for a container"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        snapshots = client.get_container_snapshots(node, vmid)
        # Filter out 'current' pseudo-snapshot if present
        snapshots = [s for s in snapshots if s.get('name') != 'current']
        return JSONResponse(content={"snapshots": snapshots})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting container {vmid} snapshots: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/snapshots")
async def create_container_snapshot(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Create a container snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    snapname = data.get('snapname')
    description = data.get('description')
    
    if not snapname:
        raise HTTPException(status_code=400, detail="Snapshot name is required")
    
    try:
        client = _get_proxmox_client(server)
        
        result = await _run_in_executor(client.create_container_snapshot, node, vmid, snapname, description)
        
        if result.get('success'):
            LoggingService.log_proxmox_action(
                db=db,
                action="snapshot_create",
                resource_type="container",
                resource_id=vmid,
                username=current_user.username,
                resource_name=snapname,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=True
            )
            logger.info(f"User {current_user.username} created snapshot {snapname} for container {vmid}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to create snapshot'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating container {vmid} snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/container/{vmid}/snapshots/{snapname}")
def delete_container_snapshot(
    server_id: int,
    vmid: int,
    snapname: str,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Delete a container snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        result = client.delete_container_snapshot(node, vmid, snapname)
        
        if result.get('success'):
            LoggingService.log_proxmox_action(
                db=db,
                action="snapshot_delete",
                resource_type="container",
                resource_id=vmid,
                username=current_user.username,
                resource_name=snapname,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=True
            )
            logger.info(f"User {current_user.username} deleted snapshot {snapname} for container {vmid}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to delete snapshot'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting container {vmid} snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/snapshots/{snapname}/rollback")
def rollback_container_snapshot(
    server_id: int,
    vmid: int,
    snapname: str,
    node: str,
    start: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Rollback a container to a snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        result = client.rollback_container_snapshot(node, vmid, snapname, start)
        
        if result.get('success'):
            LoggingService.log_proxmox_action(
                db=db,
                action="snapshot_rollback",
                resource_type="container",
                resource_id=vmid,
                username=current_user.username,
                resource_name=snapname,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=True
            )
            logger.info(f"User {current_user.username} rolled back container {vmid} to snapshot {snapname}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to rollback snapshot'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rolling back container {vmid} to snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Snapshot Archives ====================

@router.get("/api/snapshot-archives")
def get_snapshot_archives(
    server_id: int = None,
    vmid: int = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("log:view"))
):
    """
    Get archived snapshots from deleted VMs/containers.
    Admin-only endpoint for audit and recovery purposes.
    """
    query = db.query(VMSnapshotArchive).order_by(VMSnapshotArchive.archived_at.desc())
    
    if server_id:
        query = query.filter(VMSnapshotArchive.server_id == server_id)
    if vmid:
        query = query.filter(VMSnapshotArchive.vmid == vmid)
    
    total = query.count()
    archives = query.offset(offset).limit(limit).all()
    
    return JSONResponse(content={
        "total": total,
        "offset": offset,
        "limit": limit,
        "archives": [
            {
                "id": a.id,
                "server_id": a.server_id,
                "server_name": a.server_name,
                "vmid": a.vmid,
                "vm_name": a.vm_name,
                "vm_type": a.vm_type,
                "node": a.node,
                "snapname": a.snapname,
                "description": a.description,
                "snaptime": a.snaptime,
                "parent": a.parent,
                "vmstate": a.vmstate,
                "deleted_by": a.deleted_by,
                "deletion_reason": a.deletion_reason,
                "archived_at": a.archived_at.isoformat() if a.archived_at else None
            }
            for a in archives
        ]
    })


@router.get("/api/snapshot-archives/{archive_id}")
def get_snapshot_archive_detail(
    archive_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("log:view"))
):
    """Get full details of an archived snapshot including config"""
    archive = db.query(VMSnapshotArchive).filter(VMSnapshotArchive.id == archive_id).first()
    if not archive:
        raise HTTPException(status_code=404, detail="Archive not found")
    
    return JSONResponse(content={
        "id": archive.id,
        "server_id": archive.server_id,
        "server_name": archive.server_name,
        "vmid": archive.vmid,
        "vm_name": archive.vm_name,
        "vm_type": archive.vm_type,
        "node": archive.node,
        "snapname": archive.snapname,
        "description": archive.description,
        "snaptime": archive.snaptime,
        "parent": archive.parent,
        "vmstate": archive.vmstate,
        "snapshot_config": archive.snapshot_config,
        "deleted_by": archive.deleted_by,
        "deletion_reason": archive.deletion_reason,
        "archived_at": archive.archived_at.isoformat() if archive.archived_at else None
    })
