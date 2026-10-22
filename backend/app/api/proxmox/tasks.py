from fastapi import APIRouter, Depends, Request, HTTPException, Query, Form, status, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, defer
from sqlalchemy import func
from loguru import logger
from typing import List
import ssl
import asyncio
import httpx
import websockets

from ...db import get_db
from ...models import ProxmoxServer, VMInstance, User, IPAMAllocation, IPAMNetwork, VMSnapshotArchive, ProxmoxTask, DeployTask, AppOperation, InstalledApp, ScriptExecution
from ...schemas import ProxmoxServerCreate, ProxmoxServerUpdate, ProxmoxServerResponse
from ...proxmox import ProxmoxClient, get_proxmox_resources
from ...auth import get_current_user, PermissionChecker, require_permission, check_permission
from ...logging_service import LoggingService
from ...ipam_service import IPAMService
from ...models import TaskQueue
from ...services.task_queue_service import TaskQueueService, process_task_queue
from ._helpers import (check_vm_access, require_vm_access, _get_proxmox_client,
                        get_next_vmid, archive_and_delete_snapshots,
                        save_vm_instance, get_vm_instance, soft_delete_vm_instance)

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


# ==================== ProxmoxTaskService ====================

class ProxmoxTaskService:
    """Service for managing Proxmox UPID tasks stored in DB."""

    @staticmethod
    def register(
        db: Session,
        upid: str,
        server_id: int,
        user_id: int,
        action: str,
        node: str = None,
        vmid: int = None,
        vm_type: str = None,
        description: str = None,
    ) -> ProxmoxTask:
        task = ProxmoxTask(
            upid=upid,
            server_id=server_id,
            user_id=user_id,
            node=node,
            vmid=vmid,
            vm_type=vm_type,
            action=action,
            description=description,
            status="running",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        logger.info(f"[PROXMOX TASK] Registered UPID task #{task.id}: {action} on {node}/{vmid}")
        return task

    @staticmethod
    def get_user_tasks(db: Session, user_id: int, limit: int = 50) -> list:
        return (
            db.query(ProxmoxTask)
            .filter(ProxmoxTask.user_id == user_id)
            .order_by(ProxmoxTask.created_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_active(db: Session, user_id: int) -> list:
        return (
            db.query(ProxmoxTask)
            .filter(ProxmoxTask.user_id == user_id, ProxmoxTask.status == "running")
            .order_by(ProxmoxTask.created_at.desc())
            .all()
        )


def _require_upid_access(db: Session, user: User, server_id: int, upid: str) -> None:
    """Tenants may only read tasks they started or that target their own guests.

    Task logs carry node paths, storage names and other tenants' guest details;
    vm:view (granted to every standard user) must not open all of them.
    """
    from ._helpers import can_view_all_instances, get_owned_vmids
    if can_view_all_instances(user):
        return
    if db.query(ProxmoxTask.id).filter(
        ProxmoxTask.upid == upid, ProxmoxTask.user_id == user.id
    ).first():
        return
    # UPID:<node>:<pid>:<pstart>:<starttime>:<type>:<id>:<user>:
    parts = upid.split(":")
    if len(parts) >= 7 and parts[6].isdigit() and int(parts[6]) in get_owned_vmids(db, user, server_id):
        return
    raise HTTPException(status_code=403, detail="You do not have access to this task")


@router.get("/api/{server_id}/task/{upid}/status")
def get_task_status(
    server_id: int,
    upid: str,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить статус задачи по UPID"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    _require_upid_access(db, current_user, server_id, upid)
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_task_status(node, upid)
        return JSONResponse(content=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task status {upid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/task/{upid}/log")
def get_task_log(
    server_id: int,
    upid: str,
    node: str,
    start: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить лог задачи по UPID"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    _require_upid_access(db, current_user, server_id, upid)
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        logs = client.get_task_log(node, upid, start, limit)
        return JSONResponse(content={"logs": logs})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task log {upid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Bulk (TaskQueue) endpoints ─────────────────────────────────────────────

@router.get("/api/tasks")
def get_user_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100)
):
    """Get recent bulk-operation tasks for current user"""
    tasks = TaskQueueService.get_user_tasks(db, current_user.id, limit)
    return {"tasks": [task.to_dict() for task in tasks]}


@router.get("/api/tasks/active")
def get_active_bulk_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all active (pending/running) bulk tasks for current user"""
    tasks = db.query(TaskQueue).filter(
        TaskQueue.user_id == current_user.id,
        TaskQueue.status.in_(['pending', 'running'])
    ).order_by(TaskQueue.created_at.desc()).all()
    return {"tasks": [task.to_dict() for task in tasks]}


@router.get("/api/tasks/{task_id}")
def get_task_by_id(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get status of a specific bulk task"""
    task = db.query(TaskQueue).filter(
        TaskQueue.id == task_id,
        TaskQueue.user_id == current_user.id
    ).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return task.to_dict()


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cancel a pending bulk task"""
    success = TaskQueueService.cancel_task(db, task_id, current_user.id)
    
    if not success:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled (not found, not pending, or not yours)")
    
    return {"success": True, "message": "Task cancelled"}


# ─── All tasks (bulk + proxmox UPID) combined ──────────────────────────────

@router.get("/api/all-tasks")
def get_all_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(30, ge=1, le=500),
    status_filter: str = Query(None, alias="status"),
):
    """
    Return both TaskQueue (bulk ops) and ProxmoxTask (UPID) records for the
    current user, merged and sorted by created_at descending.
    """
    # Large text columns are not part of the list payload — don't load them.
    bulk_q = db.query(TaskQueue).filter(TaskQueue.user_id == current_user.id)
    proxmox_q = (db.query(ProxmoxTask).options(defer(ProxmoxTask.log_text))
                 .filter(ProxmoxTask.user_id == current_user.id))
    deploy_q = db.query(DeployTask).filter(DeployTask.user_id == current_user.id)
    appstore_q = db.query(AppOperation).filter(AppOperation.user_id == current_user.id)
    script_q = (db.query(ScriptExecution).options(defer(ScriptExecution.output))
                .filter(ScriptExecution.user_id == current_user.id))

    if status_filter:
        # Normalise statuses: proxmox uses running/completed/failed; bulk uses the same + pending/cancelled
        bulk_q = bulk_q.filter(TaskQueue.status == status_filter)
        proxmox_q = proxmox_q.filter(ProxmoxTask.status == status_filter)
        deploy_q = deploy_q.filter(DeployTask.status == status_filter)
        appstore_q = appstore_q.filter(AppOperation.status == status_filter)
        script_q = script_q.filter(ScriptExecution.status == status_filter)

    bulk_tasks = bulk_q.order_by(TaskQueue.created_at.desc()).limit(limit).all()
    proxmox_tasks = proxmox_q.order_by(ProxmoxTask.created_at.desc()).limit(limit).all()
    deploy_tasks = deploy_q.order_by(DeployTask.created_at.desc()).limit(limit).all()
    appstore_ops = appstore_q.order_by(AppOperation.started_at.desc()).limit(limit).all()
    script_executions = script_q.order_by(ScriptExecution.started_at.desc()).limit(limit).all()

    # App Store operations carry only installed_app_id; resolve app name/node/server.
    ia_ids = {o.installed_app_id for o in appstore_ops if o.installed_app_id}
    ia_map = {}
    if ia_ids:
        for ia in db.query(InstalledApp).filter(InstalledApp.id.in_(ia_ids)).all():
            ia_map[ia.id] = ia
    appstore_tasks = [
        o.to_task_dict(
            app_name=(ia_map.get(o.installed_app_id).name if ia_map.get(o.installed_app_id) else None),
            node=(ia_map.get(o.installed_app_id).node if ia_map.get(o.installed_app_id) else None),
            server_id=(ia_map.get(o.installed_app_id).server_id if ia_map.get(o.installed_app_id) else None),
        )
        for o in appstore_ops
    ]

    # Resolve server_id -> name once for enrichment (proxmox/deploy/appstore tasks carry server_id).
    server_ids = (
        {t.server_id for t in proxmox_tasks if t.server_id}
        | {t.server_id for t in deploy_tasks if t.server_id}
        | {t.get("server_id") for t in appstore_tasks if t.get("server_id")}
        | {e.server_id for e in script_executions if e.server_id}
    )
    server_names = {}
    if server_ids:
        for sid, sname in db.query(ProxmoxServer.id, ProxmoxServer.name).filter(ProxmoxServer.id.in_(server_ids)).all():
            server_names[sid] = sname

    def _enrich(d: dict) -> dict:
        # All listed tasks belong to the current user.
        if not d.get("user"):
            d["user"] = current_user.username
        if d.get("server_id") and not d.get("server_name"):
            d["server_name"] = server_names.get(d["server_id"])
        # Тип column reads `type` first; fall back to action/task_type/kind.
        if not d.get("type"):
            d["type"] = d.get("action") or d.get("task_type") or d.get("kind")
        return d

    all_tasks = sorted(
        [_enrich(t.to_dict()) for t in bulk_tasks]
        + [_enrich(t.to_dict()) for t in proxmox_tasks]
        + [_enrich(t.to_dict()) for t in deploy_tasks]
        + [_enrich(t) for t in appstore_tasks]
        + [_enrich(e.to_task_dict()) for e in script_executions],
        key=lambda t: t.get("created_at") or "",
        reverse=True,
    )[:limit]

    return {"tasks": all_tasks}


@router.get("/api/all-tasks/active-count")
def get_active_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return count of currently active tasks (pending/running) for navbar badge."""
    bulk_count = db.query(func.count(TaskQueue.id)).filter(
        TaskQueue.user_id == current_user.id,
        TaskQueue.status.in_(["pending", "running"]),
    ).scalar() or 0

    proxmox_count = db.query(func.count(ProxmoxTask.id)).filter(
        ProxmoxTask.user_id == current_user.id,
        ProxmoxTask.status == "running",
    ).scalar() or 0

    deploy_count = db.query(func.count(DeployTask.id)).filter(
        DeployTask.user_id == current_user.id,
        DeployTask.status.in_(["pending", "running"]),
    ).scalar() or 0

    appstore_count = db.query(func.count(AppOperation.id)).filter(
        AppOperation.user_id == current_user.id,
        AppOperation.status == "running",
    ).scalar() or 0

    script_count = db.query(func.count(ScriptExecution.id)).filter(
        ScriptExecution.user_id == current_user.id,
        ScriptExecution.status == "running",
    ).scalar() or 0

    return {"count": bulk_count + proxmox_count + deploy_count + appstore_count + script_count}


# ─── Proxmox UPID task log from DB ──────────────────────────────────────────

@router.get("/api/proxmox-tasks")
def list_proxmox_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100),
):
    """List ProxmoxTask records for current user."""
    tasks = ProxmoxTaskService.get_user_tasks(db, current_user.id, limit)
    return {"tasks": [t.to_dict() for t in tasks]}


@router.get("/api/proxmox-tasks/{task_id}/log")
def get_proxmox_task_log_db(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the cached log text for a ProxmoxTask stored in DB."""
    task = db.query(ProxmoxTask).filter(
        ProxmoxTask.id == task_id,
        ProxmoxTask.user_id == current_user.id,
    ).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"log_text": task.log_text or "", "status": task.status, "exit_status": task.exit_status}


@router.delete("/api/all-tasks/completed")
def clear_completed_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all completed/failed/cancelled tasks for the current user."""
    done_statuses = ["completed", "failed", "cancelled"]
    bulk_deleted = db.query(TaskQueue).filter(
        TaskQueue.user_id == current_user.id,
        TaskQueue.status.in_(done_statuses),
    ).delete(synchronize_session=False)
    proxmox_deleted = db.query(ProxmoxTask).filter(
        ProxmoxTask.user_id == current_user.id,
        ProxmoxTask.status.in_(done_statuses),
    ).delete(synchronize_session=False)
    deploy_deleted = db.query(DeployTask).filter(
        DeployTask.user_id == current_user.id,
        DeployTask.status.in_(done_statuses),
    ).delete(synchronize_session=False)
    # App Store: чистим только историю операций (installed_apps не трогаем).
    appstore_deleted = db.query(AppOperation).filter(
        AppOperation.user_id == current_user.id,
        AppOperation.status.in_(done_statuses),
    ).delete(synchronize_session=False)
    script_deleted = db.query(ScriptExecution).filter(
        ScriptExecution.user_id == current_user.id,
        ScriptExecution.status.in_(["success", "failed"]),
    ).delete(synchronize_session=False)
    db.commit()
    return {"deleted": bulk_deleted + proxmox_deleted + deploy_deleted + appstore_deleted + script_deleted}

