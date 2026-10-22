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
from concurrent.futures import ThreadPoolExecutor, as_completed

RESOURCES_MAX_WORKERS = 8  # parallel Proxmox fetches for /api/resources/all

from ...db import get_db
from ...models import ProxmoxServer, VMInstance, User, IPAMAllocation, IPAMNetwork, VMSnapshotArchive
from app.schemas import ProxmoxServerCreate, ProxmoxServerUpdate, ProxmoxServerResponse
from ...proxmox import ProxmoxClient, get_proxmox_resources, _run_in_executor
from ...proxmox.token_provisioning import (
    TokenProvisionError, provision_api_token, revoke_api_token,
)
from ...auth import get_current_user, PermissionChecker, require_permission, check_permission
from ...logging_service import LoggingService
from ...ipam_service import IPAMService
from ._helpers import (check_vm_access, require_vm_access, _get_proxmox_client,
                        get_next_vmid, archive_and_delete_snapshots,
                        save_vm_instance, get_vm_instance, soft_delete_vm_instance,
                        can_view_all_instances, get_owned_vmids, get_visible_server_ids)

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


def _attach_workspaces(db: Session, servers: list) -> None:
    """Прикрепить к каждому серверу список рабочих областей, в которых он состоит.

    Пишет транзиентный атрибут ``server.workspaces`` (список словарей), который
    затем читает Pydantic-схема ``ProxmoxServerResponse`` через ``from_attributes``.
    """
    from ...models import WorkspaceServer, Workspace

    if not servers:
        return

    server_ids = [s.id for s in servers]
    rows = (
        db.query(WorkspaceServer.server_id, Workspace.id, Workspace.name, Workspace.color)
        .join(Workspace, Workspace.id == WorkspaceServer.workspace_id)
        .filter(WorkspaceServer.server_id.in_(server_ids))
        .all()
    )

    mapping: dict[int, list] = {}
    for sid, wid, wname, wcolor in rows:
        mapping.setdefault(sid, []).append({"id": wid, "name": wname, "color": wcolor})

    for s in servers:
        s.workspaces = mapping.get(s.id, [])


# ==================== Proxmox Server CRUD ====================

@router.get("/api/servers", response_model=List[ProxmoxServerResponse])
def list_proxmox_servers(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """Получить список всех Proxmox серверов (фильтруется по доступу и активному workspace)"""
    visible_ids = get_visible_server_ids(request, db, current_user)
    if visible_ids is not None and not visible_ids:
        return []

    query = db.query(ProxmoxServer).order_by(ProxmoxServer.id)
    if visible_ids is not None:
        query = query.filter(ProxmoxServer.id.in_(visible_ids))
    servers = query.all()
    _attach_workspaces(db, servers)
    return servers


@router.post("/api/servers", response_model=ProxmoxServerResponse, status_code=status.HTTP_201_CREATED)
def create_proxmox_server(
    server_data: ProxmoxServerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:create"))
):
    """Добавить новый Proxmox сервер"""
    # Разрешаем несколько серверов с одинаковым IP (например, разные порты или кластеры)
    # Проверяем только дубликаты имён
    existing = db.query(ProxmoxServer).filter(ProxmoxServer.name == server_data.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Server with name '{server_data.name}' already exists"
        )

    payload = server_data.model_dump()
    # Not a column — it selects how credentials are obtained, not what is stored.
    auto_token = payload.pop("auto_create_token", False)
    provisioned_token: str | None = None

    if auto_token:
        try:
            token_name, token_value = provision_api_token(
                ip_address=payload["ip_address"],
                port=payload.get("port"),
                api_user=payload["api_user"],
                password=payload.get("password"),
                verify_ssl=payload.get("verify_ssl", False),
                comment=f"PVEmanager ({payload['name']})",
            )
        except TokenProvisionError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        payload["api_token_name"] = token_name
        payload["api_token_value"] = token_value
        # The password stays stored (encrypted) for SSH fallbacks like pvecm
        # delnode, but day-to-day API calls now go through the token.
        payload["use_password"] = False
        provisioned_token = token_name

    server = ProxmoxServer(**payload)
    db.add(server)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # The secret was never persisted, so the token on the node is unusable —
        # remove it instead of leaving an orphan behind.
        if provisioned_token:
            revoke_api_token(
                ip_address=payload["ip_address"],
                port=payload.get("port"),
                api_user=payload["api_user"],
                password=payload.get("password"),
                token_name=provisioned_token,
                verify_ssl=payload.get("verify_ssl", False),
            )
        raise
    db.refresh(server)

    # Сразу проверяем подключение, чтобы статус is_online отобразился без задержки
    # в 30 секунд (период monitoring_worker.run_server_availability_check).
    try:
        client = _get_proxmox_client(server)
        ok, error_kind, check_error = client.check_connection()
        if ok:
            server.update_status(True)
        else:
            server.update_status(False, check_error or "Failed to connect", error_kind)
        db.commit()
        db.refresh(server)
    except Exception as e:
        logger.warning(f"Initial connectivity check failed for {server.name}: {e}")
        try:
            server.update_status(False, str(e))
            db.commit()
            db.refresh(server)
        except Exception:
            db.rollback()

    # Если сервер онлайн — мгновенно подтягиваем VM/LXC в локальный кэш,
    # чтобы NodeDetailPage и InstancesPage сразу увидели инстансы (без ожидания
    # 10-секундного цикла vm_cache_sync).
    if server.is_online:
        try:
            from ...workers.monitoring_worker import monitoring_worker
            monitoring_worker.sync_vm_cache()
        except Exception as e:
            logger.warning(f"Initial VM cache sync failed for server {server.name}: {e}")

    # Уведомляем все подключённые клиенты, что сервер добавлен — фронтенд
    # сразу инвалидирует список серверов и список VM.
    try:
        from ...websocket_manager import broadcast_event
        broadcast_event(
            "server_added",
            server_id=server.id,
            name=server.name,
            is_online=bool(server.is_online),
        )
    except Exception as e:
        logger.debug(f"Failed to broadcast server_added: {e}")

    logger.info(f"User {current_user.username} added Proxmox server: {server.name}")
    return server




@router.get("/api/servers/{server_id}", response_model=ProxmoxServerResponse)
def get_proxmox_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """Получить информацию о конкретном Proxmox сервере"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    _attach_workspaces(db, [server])
    return server


@router.get("/api/servers/{server_id}/cluster-info")
def get_server_cluster_info(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """
    Получить информацию о кластерном режиме сервера.
    Возвращает is_cluster=true если сервер в кластере (HA доступна).
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            return JSONResponse(content={
                "server_id": server_id,
                "server_name": server.name,
                "is_cluster": False,
                "error": "Cannot connect to server"
            })
        
        is_cluster = client.is_cluster()
        nodes = client.get_nodes()
        node_names = [n.get('node') for n in nodes if n.get('node')]
        # Fallback: если get_nodes() вернул пустой список (например, нет прав),
        # используем hostname сервера как имя ноды
        if not node_names:
            node_names = [server.hostname]

        return JSONResponse(content={
            "server_id": server_id,
            "server_name": server.name,
            "is_cluster": is_cluster,
            "node_count": len(node_names),
            "nodes": node_names
        })
    except Exception as e:
        logger.error(f"Error checking cluster info for server {server_id}: {e}")
        return JSONResponse(content={
            "server_id": server_id,
            "server_name": server.name,
            "is_cluster": False,
            "error": str(e)
        })


@router.put("/api/servers/{server_id}", response_model=ProxmoxServerResponse)
def update_proxmox_server(
    server_id: int,
    server_data: ProxmoxServerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:update"))
):
    """Обновить Proxmox сервер"""
    from ...proxmox import clear_server_cache
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    # Если изменились учётные данные - очищаем кеш
    update_data = server_data.model_dump(exclude_unset=True)

    # Пустая строка в полях учётных данных означает "не менять" (клиент
    # присылает их пустыми, т.к. секреты не возвращаются с бэкенда) —
    # иначе она затирает реальный токен/пароль и сервер уходит в offline.
    for cred_field in ('api_token_name', 'api_token_value', 'password'):
        if update_data.get(cred_field) == '':
            update_data.pop(cred_field)

    if any(key in update_data for key in ['password', 'api_token_name', 'api_token_value', 'api_user']):
        clear_server_cache(server.ip_address)
        logger.info(f"Cleared cache for server {server.ip_address} due to credential update")
    
    # Update only provided fields
    for field, value in update_data.items():
        setattr(server, field, value)
    
    db.commit()
    db.refresh(server)
    
    logger.info(f"User {current_user.username} updated Proxmox server: {server.name}")

    # Если изменились параметры подключения — сразу проверяем доступность,
    # чтобы статус обновился без 30-секундной задержки monitoring_worker.
    creds_changed = any(
        key in update_data
        for key in ('password', 'api_token_name', 'api_token_value', 'api_user',
                    'ip_address', 'hostname', 'port', 'verify_ssl', 'use_password')
    )
    if creds_changed:
        try:
            client = _get_proxmox_client(server)
            ok, error_kind, check_error = client.check_connection()
            if ok:
                server.update_status(True)
            else:
                server.update_status(False, check_error or "Failed to connect", error_kind)
            db.commit()
            db.refresh(server)
            if server.is_online:
                try:
                    from ...workers.monitoring_worker import monitoring_worker
                    monitoring_worker.sync_vm_cache()
                except Exception as _e:
                    logger.debug(f"VM sync after update failed: {_e}")
        except Exception as e:
            logger.warning(f"Connectivity check after update failed for {server.name}: {e}")
            try:
                server.update_status(False, str(e))
                db.commit()
                db.refresh(server)
            except Exception:
                db.rollback()

    # Broadcast — фронтенд инвалидирует кэши списков серверов и инстансов
    try:
        from ...websocket_manager import broadcast_event
        broadcast_event(
            "server_updated",
            server_id=server.id,
            name=server.name,
            is_online=bool(server.is_online),
        )
    except Exception as e:
        logger.debug(f"Failed to broadcast server_updated: {e}")

    return server


@router.delete("/api/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_proxmox_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:delete"))
):
    """Удалить Proxmox сервер и связанные OS Templates"""
    from ...proxmox import clear_server_cache
    from ...models import OSTemplate, OSTemplateGroup, VMInstance, Notification
    from ...workers import monitoring_worker
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    server_name = server.name
    server_ip = server.ip_address
    
    # Release ALL IPAM allocations for this server
    try:
        ipam = IPAMService(db)
        released_count = ipam.release_all_by_server(
            proxmox_server_id=server_id,
            released_by=current_user.username
        )
        if released_count > 0:
            logger.info(f"Released {released_count} IPAM allocations for server {server_name}")
    except Exception as e:
        logger.warning(f"Failed to release IPAM allocations for server: {e}")
    
    # Удаляем связанные OS Templates
    templates_deleted = db.query(OSTemplate).filter(OSTemplate.server_id == server_id).delete()
    if templates_deleted > 0:
        logger.info(f"Deleted {templates_deleted} OS templates for server {server_name}")
    
    # Удаляем кэш VM/контейнеров для этого сервера
    vms_deleted = db.query(VMInstance).filter(VMInstance.server_id == server_id).delete()
    if vms_deleted > 0:
        logger.info(f"Deleted {vms_deleted} cached VM instances for server {server_name}")
    
    # Удаляем пустые группы шаблонов (без шаблонов)
    empty_groups = db.query(OSTemplateGroup).filter(
        ~OSTemplateGroup.id.in_(
            db.query(OSTemplate.group_id).filter(OSTemplate.group_id != None).distinct()
        )
    ).all()
    for group in empty_groups:
        # Проверяем что группа действительно пуста
        template_count = db.query(OSTemplate).filter(OSTemplate.group_id == group.id).count()
        if template_count == 0:
            logger.info(f"Deleting empty template group: {group.name}")
            db.delete(group)
    
    logger.info(f"User {current_user.username} deleted Proxmox server: {server_name} ({server_ip})")
    
    # Удаляем уведомления о сервере (offline/online) чтобы они не показывались после удаления
    notifications_deleted = db.query(Notification).filter(
        Notification.source_id == str(server_id),
        Notification.source.in_(["monitoring", "server_monitor"])
    ).delete(synchronize_session=False)
    if notifications_deleted > 0:
        logger.info(f"Deleted {notifications_deleted} notifications for server {server_name}")
    
    # Очищаем кеш подключений для этого сервера
    clear_server_cache(server_ip)
    
    db.delete(server)
    db.commit()
    
    # Очищаем in-memory состояние мониторинга для удалённого сервера
    try:
        monitoring_worker.cleanup_server_state(server_id)
    except Exception as e:
        logger.warning(f"Failed to cleanup monitoring state for server {server_id}: {e}")

    # Broadcast — фронтенд мгновенно убирает удалённый сервер и его инстансы
    try:
        from ...websocket_manager import broadcast_event
        broadcast_event(
            "server_deleted",
            server_id=server_id,
            name=server_name,
        )
    except Exception as e:
        logger.debug(f"Failed to broadcast server_deleted: {e}")

    return None


@router.post("/api/servers/test", status_code=status.HTTP_200_OK)
def test_proxmox_credentials(
    payload: ProxmoxServerCreate,
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """Проверить произвольные учётные данные Proxmox без сохранения в БД."""
    try:
        # auto_create_token still authenticates by password at this point — the
        # token only gets created when the server is actually saved.
        if payload.use_password or payload.auto_create_token:
            client = ProxmoxClient(
                host=payload.ip_address,
                user=payload.api_user,
                password=payload.password,
                verify_ssl=payload.verify_ssl,
            )
        else:
            client = ProxmoxClient(
                host=payload.ip_address,
                user=payload.api_user,
                token_name=payload.api_token_name,
                token_value=payload.api_token_value,
                verify_ssl=payload.verify_ssl,
            )

        if client.is_connected():
            return {"success": True, "status": "success", "message": "Connection successful"}
        return {"success": False, "status": "error", "message": "Failed to connect to Proxmox server"}
    except Exception as e:
        logger.error(f"Error testing Proxmox credentials for {payload.ip_address}: {e}")
        return {"success": False, "status": "error", "message": str(e)}


def _ssh_hosts(server: ProxmoxServer) -> list:
    """Host names the panel uses for SSH to this server (port suffix stripped)."""
    hosts = []
    for value in (server.ip_address, server.hostname):
        host = (value or "").split(":")[0].strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


@router.get("/api/servers/{server_id}/ssh-host-key")
def get_ssh_host_key(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:update"))
):
    """Pinned (trust-on-first-use) SSH host keys of the server."""
    from ...ssh_client import pinned_host_keys
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    return {"hosts": [{"host": h, "keys": pinned_host_keys(h)} for h in _ssh_hosts(server)]}


@router.delete("/api/servers/{server_id}/ssh-host-key")
def reset_ssh_host_key(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:update"))
):
    """Forget the pinned SSH host key — e.g. after the node was reinstalled.

    The next SSH connection pins whatever key the host presents then, so only
    do this when the key change is expected.
    """
    from ...ssh_client import forget_host_key
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    removed = sum(forget_host_key(h) for h in _ssh_hosts(server))
    logger.warning(
        f"User {current_user.username} reset pinned SSH host key of server {server.name} "
        f"({removed} entr{'y' if removed == 1 else 'ies'} removed)"
    )
    LoggingService.log_proxmox_action(
        db=db, action="ssh_host_key_reset", resource_type="server",
        resource_id=server.id, username=current_user.username,
        server_id=server.id, server_name=server.name, success=True,
        details={"removed": removed},
    )
    return {"success": True, "removed": removed}


@router.post("/api/servers/{server_id}/provision-token", status_code=status.HTTP_200_OK)
def provision_server_token(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:update"))
):
    """Перевести уже подключённый сервер с парольной авторизации на API-токен.

    Использует сохранённый пароль сервера: подключается им один раз, создаёт
    токен и переключает сервер на токен-авторизацию. Пароль остаётся в БД
    (зашифрованным) — он нужен для SSH-фолбэков вроде pvecm delnode.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    if not server.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server has no stored password — re-add it with credentials to create a token",
        )

    try:
        token_name, token_value = provision_api_token(
            ip_address=server.ip_address,
            port=server.port,
            api_user=server.api_user,
            password=server.password,
            verify_ssl=server.verify_ssl,
            comment=f"PVEmanager ({server.name})",
        )
    except TokenProvisionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    previous_token = server.api_token_name
    server.api_token_name = token_name
    server.api_token_value = token_value
    server.use_password = False
    try:
        db.commit()
    except Exception:
        db.rollback()
        revoke_api_token(
            ip_address=server.ip_address, port=server.port, api_user=server.api_user,
            password=server.password, token_name=token_name, verify_ssl=server.verify_ssl,
        )
        raise
    db.refresh(server)

    # Cached connections still carry the old credentials.
    try:
        from ...proxmox.client import clear_server_cache
        clear_server_cache(server.ip_address)
    except Exception as e:
        logger.debug(f"Failed to clear connection cache for {server.name}: {e}")

    logger.info(
        f"User {current_user.username} switched server '{server.name}' to API token "
        f"'{server.api_user}!{token_name}'"
        + (f" (previous token: {previous_token})" if previous_token else "")
    )
    return {"success": True, "token_name": token_name}


@router.post("/api/servers/{server_id}/test", status_code=status.HTTP_200_OK)
def test_proxmox_connection(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """Проверить подключение к Proxmox серверу"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        # Determine auth method
        client = _get_proxmox_client(server)

        if client.is_connected():
            server.update_status(True)
            db.commit()
            return {"success": True, "status": "success", "message": "Connection successful"}
        else:
            server.update_status(False, "Failed to connect")
            db.commit()
            return {"success": False, "status": "error", "message": "Failed to connect to Proxmox server"}

    except Exception as e:
        server.update_status(False, str(e))
        db.commit()
        logger.error(f"Error testing Proxmox connection to {server.name}: {e}")
        return {"success": False, "status": "error", "message": str(e)}


# ==================== Proxmox Resources ====================

@router.get("/api/{server_id}/resources")
def get_server_resources(
    server_id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """API для получения всех ресурсов (VM + LXC) с Proxmox сервера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    # Если hostname указан (нода в кластере), фильтруем по ней
    # Проверяем что hostname не пустой и не равен IP адресу (автономный сервер)
    node_filter = None
    if server.hostname and server.hostname != server.ip_address:
        node_filter = server.hostname
    
    try:
        if server.use_password:
            resources = get_proxmox_resources(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl,
                node=node_filter
            )
        else:
            resources = get_proxmox_resources(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl,
                node=node_filter
            )
        
        # Фильтруем шаблоны - они не должны считаться как VM
        vms = [vm for vm in resources.get('vms', []) if not vm.get('template', 0)]
        containers = [ct for ct in resources.get('containers', []) if not ct.get('template', 0)]

        # Изоляция по владельцу: обычный пользователь видит на ноде только свои
        # инстансы. Живые данные Proxmox не содержат owner_id, поэтому сверяемся
        # с локальным кэшем vm_instances по vmid.
        if not can_view_all_instances(current_user):
            owned = get_owned_vmids(db, current_user, server_id)
            vms = [vm for vm in vms if vm.get('vmid') in owned]
            containers = [ct for ct in containers if ct.get('vmid') in owned]

        # Получаем uptime ноды
        node_uptime = None
        try:
            client = _get_proxmox_client(server)
            nodes = client.get_nodes()
            if nodes:
                target_node = node_filter or (nodes[0].get('node') if nodes else None)
                for n in nodes:
                    if target_node and n.get('node') == target_node:
                        node_uptime = n.get('uptime')
                        break
                if node_uptime is None and nodes:
                    node_uptime = nodes[0].get('uptime')
        except Exception:
            pass
        
        server.update_status(True)
        db.commit()
        
        return JSONResponse(content={
            "server_id": server_id,
            "server_name": server.name,
            "vms": vms,
            "containers": containers,
            "node_uptime": node_uptime
        })
    except HTTPException:
        raise
    except Exception as e:
        server.update_status(False, str(e))
        db.commit()
        raise HTTPException(status_code=500, detail=f"Error getting resources: {str(e)}")


@router.get("/api/resources/all")
def get_all_resources(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """API для получения ресурсов со всех видимых пользователю Proxmox серверов.

    Набор серверов фильтруется так же, как в GET /api/servers: по доступу
    пользователя и по активной рабочей области. Иначе агрегаты (total_vms,
    total_containers) считались бы по всей панели, а не по текущей области.
    """
    visible_ids = get_visible_server_ids(request, db, current_user)
    if visible_ids is not None and not visible_ids:
        return JSONResponse(content={"servers": [], "total_vms": 0, "total_containers": 0})

    # order_by: without it Postgres returns heap order, which shifts every time
    # update_status rewrites a row — the server list would reshuffle per request.
    servers_q = db.query(ProxmoxServer).order_by(ProxmoxServer.id)
    if visible_ids is not None:
        servers_q = servers_q.filter(ProxmoxServer.id.in_(visible_ids))
    proxmox_servers = servers_q.all()

    # Обычный пользователь видит только свои инстансы во всех агрегатах.
    is_privileged = can_view_all_instances(current_user)

    all_resources = {
        "servers": [],
        "total_vms": 0,
        "total_containers": 0
    }

    # Credentials are read here, on the request thread — the worker threads get
    # plain kwargs so they never touch the ORM (Session is not thread-safe and a
    # lazy load from a worker would blow up mid-request).
    fetch_args = {}
    for server in proxmox_servers:
        logger.info(f"Getting resources from server {server.name}, use_password={server.use_password}")
        common = dict(host=server.ip_address, user=server.api_user, verify_ssl=server.verify_ssl)
        if server.use_password:
            fetch_args[server.id] = {**common, 'password': server.password}
        else:
            logger.info(f"Using API token for {server.name}: user={server.api_user}")
            fetch_args[server.id] = {
                **common,
                'token_name': server.api_token_name,
                'token_value': server.api_token_value,
            }

    # One slow or unreachable server must not hold up the rest: total wait is the
    # slowest server, not the sum of all of them.
    fetched: dict[int, dict] = {}
    errors: dict[int, str] = {}
    if proxmox_servers:
        with ThreadPoolExecutor(max_workers=min(RESOURCES_MAX_WORKERS, len(proxmox_servers))) as pool:
            futures = {
                pool.submit(get_proxmox_resources, **fetch_args[s.id]): s
                for s in proxmox_servers
            }
            for future in as_completed(futures):
                server = futures[future]
                try:
                    fetched[server.id] = future.result()
                except Exception as e:
                    logger.error(f"Error getting resources from server {server.name} ({server.ip_address}): {e}")
                    errors[server.id] = str(e)

    # Assemble in the original server order — the response must not reshuffle
    # depending on which cluster happened to answer first.
    for server in proxmox_servers:
        if server.id in errors:
            server.update_status(False, errors[server.id])
            all_resources["servers"].append({
                "id": server.id,
                "name": server.name,
                "ip": server.ip_address,
                "error": errors[server.id],
                "vms": [],
                "containers": [],
                "vms_count": 0,
                "containers_count": 0
            })
            continue

        resources = fetched.get(server.id) or {}
        vms = resources.get('vms', [])
        containers = resources.get('containers', [])

        # Изоляция по владельцу (живые данные Proxmox не содержат owner_id).
        if not is_privileged:
            owned = get_owned_vmids(db, current_user, server.id)
            vms = [vm for vm in vms if vm.get('vmid') in owned]
            containers = [ct for ct in containers if ct.get('vmid') in owned]

        logger.info(f"Got {len(vms)} VMs and {len(containers)} containers from {server.name}")

        server.update_status(True)

        all_resources["servers"].append({
            "id": server.id,
            "name": server.name,
            "ip": server.ip_address,
            "vms": vms,
            "containers": containers,
            "vms_count": len(vms),
            "containers_count": len(containers)
        })

        all_resources["total_vms"] += len(vms)
        all_resources["total_containers"] += len(containers)

    db.commit()  # single commit for every status update, instead of one per server

    return JSONResponse(content=all_resources)


# ==================== Node (Host) Status ====================

@router.get("/api/{server_id}/storages")
def get_storages(
    server_id: int,
    node: str = Query(..., description="Node name"),
    content_type: str = Query(None, description="Filter by content type (images, rootdir, vztmpl, etc)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view"))
):
    """Получить список хранилищ на ноде Proxmox"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        storages = client.get_node_storages(node)
        
        # Фильтрация по типу контента если указано
        if content_type and storages:
            filtered = []
            for storage in storages:
                content = storage.get('content', '')
                if content_type in content.split(','):
                    filtered.append(storage)
            storages = filtered
        
        # Сортировка: сначала активные, потом по имени
        storages.sort(key=lambda x: (not x.get('active', 1), x.get('storage', '')))
        
        return JSONResponse(content={"storages": storages})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting storages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/node/status")
def get_node_status(
    server_id: int,
    node: str = Query(..., description="Node name"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить текущий статус ноды Proxmox (хоста)"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_node_status(node)
        if not status:
            raise HTTPException(status_code=404, detail="Node status not found")
        
        return JSONResponse(content=status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting node {node} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/node/rrddata")
def get_node_rrddata(
    server_id: int,
    node: str = Query(..., description="Node name"),
    timeframe: str = Query("hour", regex="^(hour|day|week|month|year)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vm:view"))
):
    """Получить исторические данные ноды для графиков"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        rrddata = client.get_node_rrddata(node, timeframe)
        return JSONResponse(content=rrddata)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting node {node} RRD data: {e}")
        raise HTTPException(status_code=500, detail=str(e))
