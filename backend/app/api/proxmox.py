from fastapi import APIRouter, Depends, Request, HTTPException, Query, Form, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from loguru import logger
from typing import List
import ssl
import asyncio
import httpx
import websockets

from ..db import get_db
from ..models import ProxmoxServer, VMInstance, User, IPAMAllocation, IPAMNetwork, VMSnapshotArchive
from ..schemas import ProxmoxServerCreate, ProxmoxServerUpdate, ProxmoxServerResponse
from ..proxmox_client import ProxmoxClient, get_proxmox_resources
from ..auth import get_current_user, PermissionChecker, require_permission, check_permission
from ..logging_service import LoggingService
from ..template_helpers import add_i18n_context
from ..ipam_service import IPAMService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ==================== Helper Functions for User Isolation ====================

def check_vm_access(db: Session, current_user: User, server_id: int, vmid: int) -> bool:
    """
    Check if user has access to a specific VM instance.
    
    Admins and users with 'vms:view' permission can access all VMs.
    Users with 'user' role can only access VMs where they are the owner.
    
    Returns True if access is allowed, False otherwise.
    """
    # Admin role has full access
    if current_user.role and current_user.role.name == 'admin':
        return True
    
    # Check permissions
    if current_user.role and current_user.role.permissions:
        perms = current_user.role.permissions
        # If user has full vms:view, they can access all VMs
        has_full_view = perms.get('vms.view', False) or perms.get('vms:view', False)
        has_own_only = perms.get('vms:view:own', False) or perms.get('vms.view.own', False)
        
        if has_full_view and not has_own_only:
            return True
    
    # For limited users, check ownership
    if current_user.role and current_user.role.name == 'user':
        instance = db.query(VMInstance).filter(
            VMInstance.server_id == server_id,
            VMInstance.vmid == vmid,
            VMInstance.deleted_at.is_(None)
        ).first()
        
        if instance and instance.owner_id == current_user.id:
            return True
        return False
    
    # Default: allow access (for backwards compatibility)
    return True


def require_vm_access(db: Session, current_user: User, server_id: int, vmid: int):
    """
    Require user to have access to VM, raise 403 if not.
    """
    if not check_vm_access(db, current_user, server_id, vmid):
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this virtual machine"
        )


# ==================== HTML Pages ====================

@router.get("/vms", response_class=HTMLResponse, include_in_schema=False)
def vms_page(request: Request, db: Session = Depends(get_db)):
    """Страница управления Proxmox серверами, VM и LXC"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    proxmox_servers = db.query(ProxmoxServer).all()
    
    context = {
        "request": request,
        "proxmox_servers": proxmox_servers,
        "page_title": t('nav_proxmox', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("proxmox_vms.html", context)


@router.get("/server/{server_id}", response_class=HTMLResponse, include_in_schema=False)
def server_detail_page(request: Request, server_id: int, db: Session = Depends(get_db)):
    """Страница детального просмотра VM/LXC конкретного Proxmox сервера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    context = {
        "request": request,
        "server": server,
        "page_title": server.name,
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("proxmox_server_detail.html", context)


@router.get("/server/{server_id}/instance/{vmid}", response_class=HTMLResponse, include_in_schema=False)
def instance_detail_page(request: Request, server_id: int, vmid: int, type: str = "qemu", node: str = "", db: Session = Depends(get_db)):
    """Страница детального просмотра конкретной VM или LXC"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    context = {
        "request": request,
        "server": server,
        "vmid": vmid,
        "type": type,
        "node": node,
        "page_title": f"VM {vmid}",
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("instance_detail.html", context)


# ==================== Proxmox Server CRUD ====================

@router.get("/api/servers", response_model=List[ProxmoxServerResponse])
def list_proxmox_servers(
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Получить список всех Proxmox серверов"""
    servers = db.query(ProxmoxServer).all()
    return servers


@router.post("/api/servers", response_model=ProxmoxServerResponse, status_code=status.HTTP_201_CREATED)
def create_proxmox_server(
    server_data: ProxmoxServerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.servers.add"))
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
    
    server = ProxmoxServer(**server_data.model_dump())
    db.add(server)
    db.commit()
    db.refresh(server)
    
    logger.info(f"User {current_user.username} added Proxmox server: {server.name} ({server.ip_address})")
    return server


@router.post("/api/servers/auto-setup", status_code=status.HTTP_201_CREATED)
async def auto_setup_proxmox_server(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.servers.add"))
):
    """
    Автоматическая настройка Proxmox сервера:
    1. Проверяем подключение по логину/паролю
    2. Создаем API Token автоматически
    3. Сохраняем сервер в базу с токеном и паролем (для VNC)
    """
    import requests as http_requests
    import uuid
    
    # Получаем данные из body
    data = await request.json()
    
    name = data.get('name')
    hostname = data.get('hostname')
    ip_address = data.get('ip_address')
    port = data.get('port', 8006)
    api_user = data.get('api_user', 'root@pam')
    password = data.get('password')
    verify_ssl = data.get('verify_ssl', False)
    description = data.get('description')
    
    if not all([name, ip_address, password]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать: name, ip_address, password"
        )
    
    # Разрешаем несколько серверов с одинаковым IP
    # Проверяем только дубликаты имён
    existing = db.query(ProxmoxServer).filter(ProxmoxServer.name == name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Сервер с именем '{name}' уже существует"
        )
    
    base_url = f"https://{ip_address}:{port}"
    
    # 1. Получить auth ticket
    logger.info(f"Attempting to connect to Proxmox at {base_url}")
    try:
        auth_response = http_requests.post(
            f"{base_url}/api2/json/access/ticket",
            data={"username": api_user, "password": password},
            verify=verify_ssl,
            timeout=15
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ошибка авторизации в Proxmox: неверный логин или пароль"
            )
        
        auth_data = auth_response.json().get("data", {})
        ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Не удалось получить auth ticket от Proxmox"
            )
        
        logger.info(f"Successfully authenticated to Proxmox {ip_address}")
        
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Connection error to Proxmox: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Не удалось подключиться к Proxmox: {str(e)}"
        )
    
    # 2. Создать API Token
    # Извлекаем username из api_user (root@pam -> root)
    username_part = api_user.split('@')[0] if '@' in api_user else api_user
    realm = api_user.split('@')[1] if '@' in api_user else 'pam'
    
    token_name = f"panel-{uuid.uuid4().hex[:8]}"
    
    headers = {
        "Cookie": f"PVEAuthCookie={ticket}",
        "CSRFPreventionToken": csrf_token
    }
    
    try:
        # Создаем токен через API
        token_response = http_requests.post(
            f"{base_url}/api2/json/access/users/{api_user}/token/{token_name}",
            headers=headers,
            data={"privsep": "0"},  # Полные права как у пользователя
            verify=verify_ssl,
            timeout=15
        )
        
        if token_response.status_code not in [200, 201]:
            logger.warning(f"Failed to create token: {token_response.status_code} - {token_response.text}")
            # Если не удалось создать токен, работаем только с паролем
            token_value = None
            token_name = None
            logger.info("Will use password authentication only")
        else:
            token_data = token_response.json().get("data", {})
            token_value = token_data.get("value")
            logger.info(f"Created API token: {token_name}")
            
    except Exception as e:
        logger.warning(f"Error creating token: {e}, will use password auth")
        token_value = None
        token_name = None
    
    # 3. Получить список нод кластера и их IP адреса
    cluster_nodes = []
    node_ips = {}  # node_name -> ip_address
    
    try:
        # Получаем список нод
        nodes_response = http_requests.get(
            f"{base_url}/api2/json/nodes",
            headers=headers,
            cookies={"PVEAuthCookie": ticket},
            verify=verify_ssl,
            timeout=15
        )
        if nodes_response.status_code == 200:
            nodes_data = nodes_response.json().get("data", [])
            cluster_nodes = [n for n in nodes_data if n.get("node")]
            logger.info(f"Found {len(cluster_nodes)} nodes in cluster")
        
        # Получаем IP адреса нод из cluster/status
        if len(cluster_nodes) > 1:
            cluster_status_response = http_requests.get(
                f"{base_url}/api2/json/cluster/status",
                headers=headers,
                cookies={"PVEAuthCookie": ticket},
                verify=verify_ssl,
                timeout=15
            )
            if cluster_status_response.status_code == 200:
                cluster_status = cluster_status_response.json().get("data", [])
                for item in cluster_status:
                    if item.get("type") == "node" and item.get("name") and item.get("ip"):
                        node_ips[item.get("name")] = item.get("ip")
                        logger.info(f"Node {item.get('name')} IP: {item.get('ip')}")
    except Exception as e:
        logger.warning(f"Could not get cluster nodes: {e}")
    
    # 4. Сохранить серверы в базу
    created_servers = []
    
    if len(cluster_nodes) > 1:
        # Это кластер - добавляем все ноды
        for node_info in cluster_nodes:
            node_name = node_info.get("node")
            # Используем IP ноды из cluster/status, или IP через который подключились
            node_ip = node_ips.get(node_name, ip_address)
            
            # Имя сервера: "BaseName - NodeName" или просто NodeName
            if name:
                server_name = f"{name} - {node_name}"
            else:
                server_name = node_name
            
            # Проверяем дубликаты
            existing = db.query(ProxmoxServer).filter(ProxmoxServer.name == server_name).first()
            if existing:
                logger.info(f"Server '{server_name}' already exists, skipping")
                continue
            
            # Для каждой ноды создаём токен (используем тот же, так как токен кластерный)
            server = ProxmoxServer(
                name=server_name,
                hostname=node_name,
                ip_address=node_ip,  # Используем реальный IP ноды
                port=port,
                api_user=api_user,
                api_token_name=token_name,
                api_token_value=token_value,
                use_password=token_value is None,
                password=password,
                verify_ssl=verify_ssl,
                description=f"{description or ''} (Node: {node_name})".strip(),
                is_online=node_info.get("status") == "online"
            )
            
            db.add(server)
            created_servers.append({
                "name": server_name,
                "node": node_name,
                "ip": node_ip,
                "status": node_info.get("status", "unknown")
            })
        
        db.commit()
        logger.info(f"User {current_user.username} auto-setup Proxmox cluster with {len(created_servers)} nodes")
        
        return {
            "cluster": True,
            "nodes_count": len(created_servers),
            "servers": created_servers,
            "message": f"Добавлено {len(created_servers)} нод кластера"
        }
    else:
        # Одиночный сервер
        server = ProxmoxServer(
            name=name,
            hostname=hostname or ip_address,
            ip_address=ip_address,
            port=port,
            api_user=api_user,
            api_token_name=token_name,
            api_token_value=token_value,
            use_password=token_value is None,
            password=password,
            verify_ssl=verify_ssl,
            description=description,
            is_online=True
        )
        
        db.add(server)
        db.commit()
        db.refresh(server)
        
        logger.info(f"User {current_user.username} auto-setup Proxmox server: {server.name} ({server.ip_address})")
        
        return {
            "cluster": False,
            "id": server.id,
            "name": server.name,
            "hostname": server.hostname,
            "ip_address": server.ip_address,
            "port": server.port,
            "api_user": server.api_user,
            "api_token_name": token_name or "(пароль)",
            "use_password": server.use_password,
            "verify_ssl": server.verify_ssl,
            "description": server.description,
            "is_online": server.is_online
        }


@router.get("/api/servers/{server_id}", response_model=ProxmoxServerResponse)
def get_proxmox_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Получить информацию о конкретном Proxmox сервере"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    return server


@router.get("/api/servers/{server_id}/cluster-info")
def get_server_cluster_info(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """
    Получить информацию о кластерном режиме сервера.
    Возвращает is_cluster=true если сервер в кластере (HA доступна).
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            return JSONResponse(content={
                "server_id": server_id,
                "server_name": server.name,
                "is_cluster": False,
                "error": "Cannot connect to server"
            })
        
        is_cluster = client.is_cluster()
        nodes = client.get_nodes() if is_cluster else []
        
        return JSONResponse(content={
            "server_id": server_id,
            "server_name": server.name,
            "is_cluster": is_cluster,
            "node_count": len(nodes),
            "nodes": [n.get('node') for n in nodes] if nodes else []
        })
    except Exception as e:
        logger.error(f"Error checking cluster info for server {server_id}: {e}")
        return JSONResponse(content={
            "server_id": server_id,
            "server_name": server.name,
            "is_cluster": False,
            "error": str(e)
        })


# ==================== SDN (Software Defined Networking) ====================

@router.get("/api/servers/{server_id}/sdn/status")
def get_sdn_status(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Check if SDN is available on the server"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        is_available = client.sdn_is_available()
        pending = client.get_sdn_pending() if is_available else []
        
        return JSONResponse(content={
            "server_id": server_id,
            "sdn_available": is_available,
            "pending_changes": len(pending) > 0,
            "pending": pending
        })
    except Exception as e:
        logger.error(f"Error checking SDN status for server {server_id}: {e}")
        return JSONResponse(content={
            "server_id": server_id,
            "sdn_available": False,
            "error": str(e)
        })


@router.get("/api/servers/{server_id}/sdn/zones")
def get_sdn_zones(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Get all SDN zones"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        zones = client.get_sdn_zones()
        return JSONResponse(content={"zones": zones})
    except Exception as e:
        logger.error(f"Error getting SDN zones for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/zones")
async def create_sdn_zone(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Create a new SDN zone"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    zone_name = data.get('zone')
    zone_type = data.get('type', 'simple')
    
    if not zone_name:
        raise HTTPException(status_code=400, detail="Zone name is required")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        # Pass additional options from request
        kwargs = {k: v for k, v in data.items() if k not in ['zone', 'type']}
        result = client.create_sdn_zone(zone_name, zone_type, **kwargs)
        
        if result.get('success'):
            logger.info(f"User {current_user.username} created SDN zone: {zone_name}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to create zone'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating SDN zone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/sdn/zones/{zone}")
def delete_sdn_zone(
    server_id: int,
    zone: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Delete an SDN zone"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.delete_sdn_zone(zone)
        
        if result.get('success'):
            logger.info(f"User {current_user.username} deleted SDN zone: {zone}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to delete zone'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting SDN zone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/sdn/vnets")
def get_sdn_vnets(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Get all SDN VNets"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        vnets = client.get_sdn_vnets()
        return JSONResponse(content={"vnets": vnets})
    except Exception as e:
        logger.error(f"Error getting SDN vnets for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/vnets")
async def create_sdn_vnet(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Create a new SDN VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    vnet_name = data.get('vnet')
    zone = data.get('zone')
    tag = data.get('tag')
    alias = data.get('alias')
    vlanaware = data.get('vlanaware', False)
    
    if not vnet_name or not zone:
        raise HTTPException(status_code=400, detail="VNet name and zone are required")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.create_sdn_vnet(vnet_name, zone, tag=tag, alias=alias, vlanaware=vlanaware)
        
        if result.get('success'):
            logger.info(f"User {current_user.username} created SDN vnet: {vnet_name} in zone {zone}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to create vnet'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating SDN vnet: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/sdn/vnets/{vnet}")
def delete_sdn_vnet(
    server_id: int,
    vnet: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Delete an SDN VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.delete_sdn_vnet(vnet)
        
        if result.get('success'):
            logger.info(f"User {current_user.username} deleted SDN vnet: {vnet}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to delete vnet'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting SDN vnet: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/sdn/vnets/{vnet}/subnets")
def get_sdn_subnets(
    server_id: int,
    vnet: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Get subnets for a VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        subnets = client.get_sdn_subnets(vnet)
        return JSONResponse(content={"subnets": subnets})
    except Exception as e:
        logger.error(f"Error getting SDN subnets for vnet {vnet}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/vnets/{vnet}/subnets")
async def create_sdn_subnet(
    server_id: int,
    vnet: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Create a subnet in a VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    subnet = data.get('subnet')
    gateway = data.get('gateway')
    snat = data.get('snat', False)
    dnszoneprefix = data.get('dnszoneprefix')
    
    if not subnet:
        raise HTTPException(status_code=400, detail="Subnet CIDR is required")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.create_sdn_subnet(vnet, subnet, gateway=gateway, snat=snat, dnszoneprefix=dnszoneprefix)
        
        if result.get('success'):
            logger.info(f"User {current_user.username} created subnet {subnet} in vnet {vnet}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to create subnet'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating SDN subnet: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/apply")
def apply_sdn_changes(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Apply pending SDN changes"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.apply_sdn_changes()
        
        if result.get('success'):
            logger.info(f"User {current_user.username} applied SDN changes on server {server.name}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to apply SDN changes'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying SDN changes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Snapshots ====================

@router.get("/api/{server_id}/vm/{vmid}/snapshots")
def get_vm_snapshots(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Get all snapshots for a VM"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        snapshots = client.get_vm_snapshots(node, vmid)
        # Filter out 'current' pseudo-snapshot if present
        snapshots = [s for s in snapshots if s.get('name') != 'current']
        return JSONResponse(content={"snapshots": snapshots})
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
    current_user: User = Depends(PermissionChecker("vms.view"))
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
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.create_vm_snapshot(node, vmid, snapname, description, vmstate)
        
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
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Delete a VM snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
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
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Rollback a VM to a snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
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
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Get all snapshots for a container"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        snapshots = client.get_container_snapshots(node, vmid)
        # Filter out 'current' pseudo-snapshot if present
        snapshots = [s for s in snapshots if s.get('name') != 'current']
        return JSONResponse(content={"snapshots": snapshots})
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
    current_user: User = Depends(PermissionChecker("vms.view"))
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
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        result = client.create_container_snapshot(node, vmid, snapname, description)
        
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
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Delete a container snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
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
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Rollback a container to a snapshot"""
    # VPS-style user isolation
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
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
    current_user: User = Depends(PermissionChecker("logs.view"))
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
    current_user: User = Depends(PermissionChecker("logs.view"))
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


@router.put("/api/servers/{server_id}", response_model=ProxmoxServerResponse)
def update_proxmox_server(
    server_id: int,
    server_data: ProxmoxServerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.servers.edit"))
):
    """Обновить Proxmox сервер"""
    from ..proxmox_client import clear_server_cache
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    # Если изменились учётные данные - очищаем кеш
    update_data = server_data.model_dump(exclude_unset=True)
    if any(key in update_data for key in ['password', 'api_token_name', 'api_token_value', 'api_user']):
        clear_server_cache(server.ip_address)
        logger.info(f"Cleared cache for server {server.ip_address} due to credential update")
    
    # Update only provided fields
    for field, value in update_data.items():
        setattr(server, field, value)
    
    db.commit()
    db.refresh(server)
    
    logger.info(f"User {current_user.username} updated Proxmox server: {server.name}")
    return server


@router.delete("/api/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_proxmox_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.servers.delete"))
):
    """Удалить Proxmox сервер и связанные OS Templates"""
    from ..proxmox_client import clear_server_cache
    from ..models import OSTemplate, OSTemplateGroup, VMInstance
    
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
    
    # Очищаем кеш подключений для этого сервера
    clear_server_cache(server_ip)
    
    db.delete(server)
    db.commit()
    return None


@router.post("/api/servers/{server_id}/test", status_code=status.HTTP_200_OK)
def test_proxmox_connection(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Проверить подключение к Proxmox серверу"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Determine auth method
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if client.is_connected():
            server.update_status(True)
            db.commit()
            return {"status": "success", "message": "Connection successful"}
        else:
            server.update_status(False, "Failed to connect")
            db.commit()
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
    
    except Exception as e:
        server.update_status(False, str(e))
        db.commit()
        logger.error(f"Error testing Proxmox connection to {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Proxmox Resources ====================

@router.get("/api/{server_id}/resources")
def get_server_resources(
    server_id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vms.view"))
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
        
        server.update_status(True)
        db.commit()
        
        return JSONResponse(content={
            "server_id": server_id,
            "server_name": server.name,
            "vms": vms,
            "containers": containers
        })
    except Exception as e:
        server.update_status(False, str(e))
        db.commit()
        raise HTTPException(status_code=500, detail=f"Error getting resources: {str(e)}")


@router.get("/api/resources/all")
def get_all_resources(
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """API для получения всех ресурсов со всех Proxmox серверов"""
    proxmox_servers = db.query(ProxmoxServer).all()
    
    all_resources = {
        "servers": [],
        "total_vms": 0,
        "total_containers": 0
    }
    
    for server in proxmox_servers:
        try:
            logger.info(f"Getting resources from server {server.name} ({server.ip_address}), use_password={server.use_password}")
            # Создаём клиента для каждого сервера независимо
            if server.use_password:
                resources = get_proxmox_resources(
                    host=server.ip_address,
                    user=server.api_user,
                    password=server.password,
                    verify_ssl=server.verify_ssl
                )
            else:
                logger.info(f"Using API token for {server.name}: user={server.api_user}, token_name={server.api_token_name}")
                resources = get_proxmox_resources(
                    host=server.ip_address,
                    user=server.api_user,
                    token_name=server.api_token_name,
                    token_value=server.api_token_value,
                    verify_ssl=server.verify_ssl
                )
            
            vms = resources.get('vms', [])
            containers = resources.get('containers', [])
            
            logger.info(f"Got {len(vms)} VMs and {len(containers)} containers from {server.name}")
            
            server.update_status(True)
            db.commit()
            
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
            
        except Exception as e:
            logger.error(f"Error getting resources from server {server.name} ({server.ip_address}): {e}")
            server.update_status(False, str(e))
            db.commit()
            all_resources["servers"].append({
                "id": server.id,
                "name": server.name,
                "ip": server.ip_address,
                "error": str(e),
                "vms": [],
                "containers": [],
                "vms_count": 0,
                "containers_count": 0
            })
            # Продолжаем обработку следующего сервера
            continue
    
    return JSONResponse(content=all_resources)


@router.post("/api/sync-vms")
def sync_vms_now(
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """
    Force immediate synchronization of VMs/containers from all Proxmox servers.
    Useful when you create VMs directly in Proxmox and want them to appear in the panel immediately.
    """
    try:
        from ..workers.monitoring_worker import MonitoringWorker
        
        worker = MonitoringWorker()
        worker.sync_vm_cache()
        
        return JSONResponse(content={
            "status": "success",
            "message": "VM synchronization completed"
        })
    except Exception as e:
        logger.error(f"Manual VM sync failed: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e)
            }
        )


@router.get("/api/virtual-machines")
def get_all_virtual_machines(
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """
    API для получения всех VM/LXC в плоском списке для таблицы.
    Данные читаются из локального кэша (таблица vm_instances), который
    обновляется фоновым worker каждые 30 секунд.
    
    Для пользователей с ролью 'user' возвращаются только их собственные инстансы.
    """
    from ..models import VMInstance
    
    # Get servers for name lookup
    servers = db.query(ProxmoxServer).all()
    server_map = {s.id: s for s in servers}
    
    # Pre-load IPAM allocations for IP lookup
    ipam_allocations = db.query(IPAMAllocation).filter(
        IPAMAllocation.status.in_(['allocated', 'reserved'])
    ).all()
    
    # Pre-load IPAM networks for network name lookup
    ipam_networks = db.query(IPAMNetwork).all()
    network_map = {n.id: n for n in ipam_networks}
    
    # Build IPAM lookups
    ipam_by_vmid = {}
    ipam_by_name = {}
    
    for alloc in ipam_allocations:
        if alloc.proxmox_server_id and alloc.proxmox_vmid:
            ipam_by_vmid[(alloc.proxmox_server_id, alloc.proxmox_vmid)] = alloc
        elif alloc.resource_id:
            ipam_by_vmid[(None, alloc.resource_id)] = alloc
        if alloc.hostname:
            ipam_by_name[alloc.hostname.lower()] = alloc
        if alloc.resource_name:
            ipam_by_name[alloc.resource_name.lower()] = alloc
    
    # Build base query for cached VMs (not deleted, not templates)
    query = db.query(VMInstance).filter(
        VMInstance.deleted_at.is_(None),
        VMInstance.is_template == False
    )
    
    # VPS-style user isolation: users with 'user' role see only their own instances
    # Check if user has 'vms.view' but not 'vms.view:all' (or is user role)
    user_role = current_user.role.name if current_user.role else None
    is_limited_user = user_role == 'user'
    
    # Also check permission-based isolation
    if hasattr(current_user, 'role') and current_user.role:
        perms = current_user.role.permissions or {}
        # If user has vms:view:own but not vms:view (full), filter by owner
        has_view_own = perms.get('vms:view:own', False) or perms.get('vms.view.own', False)
        has_view_all = perms.get('vms:view', False) or perms.get('vms.view', False)
        
        # Limited users can only see their own VMs
        if is_limited_user or (has_view_own and not has_view_all):
            query = query.filter(VMInstance.owner_id == current_user.id)
    
    cached_vms = query.order_by(VMInstance.name).all()
    
    result = []
    
    # Detect cluster servers (servers with hostnames pve1, pve2, pve3 pattern)
    # For cluster servers, show cluster name based on node, not server_id
    cluster_nodes = {}  # node_name -> server
    for server in servers:
        # Match node name to server by hostname (e.g., 'pve1', 'pve2')
        if server.hostname:
            cluster_nodes[server.hostname.lower()] = server
    
    for vm in cached_vms:
        server = server_map.get(vm.server_id)
        server_name = server.name if server else "Unknown"
        
        # For cluster VMs, try to show the correct server based on node name
        node_lower = vm.node.lower() if vm.node else ""
        if node_lower in cluster_nodes:
            actual_server = cluster_nodes[node_lower]
            server_name = actual_server.name
        
        # Get IP from IPAM or cache
        ipam_alloc = (
            ipam_by_vmid.get((vm.server_id, vm.vmid)) or 
            ipam_by_vmid.get((None, vm.vmid)) or
            ipam_by_name.get(vm.name.lower())
        )
        
        ip_address = ipam_alloc.ip_address if ipam_alloc else (vm.ip_address or "")
        ip_hostname = ipam_alloc.hostname if ipam_alloc else ""
        owner = ipam_alloc.allocated_by if ipam_alloc else ""
        
        # Get network name from IPAM
        ip_network_name = ""
        if ipam_alloc and ipam_alloc.network_id:
            network = network_map.get(ipam_alloc.network_id)
            if network:
                ip_network_name = network.name
        
        # OS type - prefer template_name, fallback to os_type
        os_template = vm.template_name or vm.os_type or ("QEMU/KVM" if vm.vm_type == "qemu" else "Linux")
        if not vm.template_name and vm.vm_type == "lxc" and os_template:
            os_template = os_template.capitalize()
        
        result.append({
            "server_id": vm.server_id,
            "server_name": server_name,
            "cluster": server_name,
            "vmid": vm.vmid,
            "name": vm.name,
            "hostname": ip_hostname or f"{'vps' if vm.vm_type == 'qemu' else 'lxc'}{vm.vmid}.{server.hostname if server else 'local'}",
            "type": vm.vm_type,
            "status": vm.status or "unknown",
            "node": vm.node,
            "cores": vm.cores or 0,
            "memory": vm.memory or 0,
            "disk": vm.disk_size or 0,
            "ip": ip_address,
            "ip_hostname": ip_network_name,
            "os": os_template,
            "os_template": os_template,
            "owner": owner,
            "owner_hostname": "",
            "storage": "Storage1 (DIR)"
        })
    
    return JSONResponse(content=result)


# ==================== VM/Container Control ====================

@router.post("/api/{server_id}/vm/{vmid}/execute")
def execute_vm_command(
    server_id: int,
    vmid: int,
    node: str,
    command: str = Query(..., description="Команда для выполнения"),
    timeout: int = Query(30, ge=1, le=300, description="Таймаут в секундах"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """
    Выполнить команду на VM через QEMU guest agent
    
    Примеры команд:
    - ls -la /tmp
    - df -h
    - free -m
    - systemctl status nginx
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        result = client.execute_command(node, vmid, command, timeout)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error executing command on VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/execute-script")
def execute_vm_script(
    server_id: int,
    vmid: int,
    node: str = Query(..., description="Имя ноды Proxmox"),
    script: str = Form(..., description="Содержимое bash скрипта"),
    interpreter: str = Form("/bin/bash", description="Путь к интерпретатору"),
    timeout: int = Query(60, ge=1, le=600, description="Таймаут в секундах"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """
    Выполнить bash скрипт на VM через QEMU guest agent
    
    Скрипт будет сохранен во временный файл на VM и выполнен.
    После выполнения временный файл будет удален.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        result = client.execute_script(node, vmid, script, interpreter, timeout)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error executing script on VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/{action}")
def control_vm(
    server_id: int, 
    vmid: int, 
    action: str, 
    node: str,
    force: int = 0,
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Управление VM (start/stop/restart)"""
    if action not in ['start', 'stop', 'restart']:
        raise HTTPException(status_code=400, detail="Invalid action")
    
    # Проверка прав в зависимости от действия
    permission_map = {
        'start': 'vms.start',
        'stop': 'vms.stop', 
        'restart': 'vms.restart'
    }
    if not current_user.has_permission(permission_map[action]):
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied: {permission_map[action]}"
        )
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        # Get VM name for logging
        vm_name = None
        try:
            vm_status = client.get_vm_status(node, vmid)
            vm_name = vm_status.get('name') if isinstance(vm_status, dict) else None
        except:
            pass
        
        if action == 'start':
            success = client.start_vm(node, vmid)
        elif action == 'stop':
            if force:
                success = client.force_stop_vm(node, vmid)
            else:
                success = client.stop_vm(node, vmid, force=False)
        else:  # restart
            success = client.restart_vm(node, vmid)
        
        action_name = 'kill' if action == 'stop' and force else action
        if success:
            # Log successful action
            LoggingService.log_proxmox_action(
                db=db,
                action=action_name,
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=vm_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={"force": force},
                success=True
            )
            logger.info(f"User {current_user.username} executed {action_name} on VM {vmid} at {server.name}")
            return JSONResponse(content={"status": "success", "action": action_name, "vmid": vmid, "node": node})
        else:
            # Log failed action
            LoggingService.log_proxmox_action(
                db=db,
                action=action_name,
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=vm_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={"force": force},
                success=False,
                error_message="Failed to execute action"
            )
            raise HTTPException(status_code=500, detail="Failed to execute action")
    except HTTPException:
        raise
    except Exception as e:
        # Log error
        LoggingService.log_proxmox_action(
            db=db,
            action=action,
            resource_type="vm",
            resource_id=vmid,
            username=current_user.username,
            server_id=server_id if server else None,
            server_name=server.name if server else None,
            node_name=node,
            details={"force": force},
            success=False,
            error_message=str(e)
        )
        logger.error(f"Error controlling VM {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/vm/{vmid}")
async def delete_vm(
    request: Request,
    server_id: int,
    vmid: int,
    node: str,
    force: bool = Query(False, description="Force delete without saving config"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.delete"))
):
    """Удалить VM"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail=t("server_not_found", lang))
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail=t("failed_to_connect", lang))
        
        # Проверяем статус VM
        status = client.get_vm_status(node, vmid)
        if status and isinstance(status, dict) and status.get('status') == 'running':
            raise HTTPException(status_code=400, detail=t("cannot_delete_running_vm", lang))
        
        # Сохраняем конфигурацию VM в базу перед удалением (если не force)
        if not force:
            config = client.get_vm_config(node, vmid)
            interfaces = client.get_vm_interfaces(node, vmid)
            
            # Проверяем что config - это словарь
            if not isinstance(config, dict):
                config = {}
            
            # Извлекаем IP адрес из интерфейсов
            ip_address = None
            ip_prefix = 24
            if interfaces and isinstance(interfaces, list):
                for iface in interfaces:
                    if isinstance(iface, dict) and iface.get('ips'):
                        for ip_info in iface['ips']:
                            if isinstance(ip_info, dict) and ip_info.get('type') == 'ipv4':
                                ip_address = ip_info.get('address')
                                ip_prefix = ip_info.get('prefix', 24)
                                break
                    if ip_address:
                        break
            
            # Сохраняем в базу
            try:
                bootdisk = config.get('bootdisk', 'scsi0')
                disk_info = config.get(bootdisk, {})
                disk_size = None
                if isinstance(disk_info, dict):
                    size_str = disk_info.get('size', '0G')
                    disk_size = int(size_str.replace('G', '')) if isinstance(size_str, str) else None
                
                save_vm_instance(
                    db=db,
                    server_id=server_id,
                    vmid=vmid,
                    node=node,
                    vm_type='qemu',
                    name=config.get('name', f'VM-{vmid}'),
                    cores=config.get('cores'),
                    memory=config.get('memory'),
                    disk_size=disk_size,
                    ip_address=ip_address,
                    ip_prefix=ip_prefix,
                    description=config.get('description')
                )
                logger.info(f"Saved VM {vmid} configuration before deletion")
            except Exception as e:
                logger.warning(f"Failed to save VM config before deletion: {e}")
        
        # Archive and delete snapshots before deleting VM
        vm_name = status.get('name') if isinstance(status, dict) else f'VM-{vmid}'
        snapshot_result = archive_and_delete_snapshots(
            db=db,
            client=client,
            server_id=server_id,
            server_name=server.name,
            vmid=vmid,
            vm_name=vm_name,
            vm_type='qemu',
            node=node,
            deleted_by=current_user.username,
            deletion_reason=f"VM {vmid} deleted by {current_user.username}"
        )
        
        if snapshot_result["archived"] > 0:
            logger.info(f"Archived {snapshot_result['archived']} snapshots before deleting VM {vmid}")
        if snapshot_result["errors"]:
            logger.warning(f"Snapshot cleanup errors for VM {vmid}: {snapshot_result['errors']}")
        
        # Удаляем VM
        result = client.delete_vm(node, vmid)
        if result:
            # Освобождаем IP в IPAM (если есть)
            try:
                ipam = IPAMService(db)
                released, released_ip = ipam.release_ip_by_vmid(
                    proxmox_server_id=server_id,
                    proxmox_vmid=vmid,
                    released_by=current_user.username,
                    reason=f"VM {vmid} deleted"
                )
                if released:
                    logger.info(f"Auto-released IPAM allocation for IP {released_ip} after VM {vmid} deletion")
            except Exception as e:
                logger.warning(f"Failed to release IPAM allocation for VM {vmid}: {e}")
            
            # Log successful deletion
            LoggingService.log_proxmox_action(
                db=db,
                action="delete",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                resource_name=vm_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={
                    "force": force,
                    "snapshots_archived": snapshot_result["archived"],
                    "snapshots_deleted": snapshot_result["deleted"],
                    "snapshot_names": snapshot_result["snapshots"]
                },
                success=True
            )
            logger.info(f"User {current_user.username} deleted VM {vmid} on {server.name}")
            return JSONResponse(content={
                "status": "success", 
                "message": f"VM {vmid} удалена",
                "snapshots_archived": snapshot_result["archived"],
                "snapshots_deleted": snapshot_result["deleted"]
            })
        else:
            LoggingService.log_proxmox_action(
                db=db,
                action="delete",
                resource_type="vm",
                resource_id=vmid,
                username=current_user.username,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                success=False,
                error_message="Не удалось удалить VM"
            )
            raise HTTPException(status_code=500, detail="Не удалось удалить VM")
    except HTTPException:
        raise
    except Exception as e:
        LoggingService.log_proxmox_action(
            db=db,
            action="delete",
            resource_type="vm",
            resource_id=vmid,
            username=current_user.username,
            server_id=server_id if server else None,
            server_name=server.name if server else None,
            node_name=node,
            success=False,
            error_message=str(e)
        )
        logger.error(f"Error deleting VM {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== High Availability (HA) Endpoints ====================

@router.get("/api/{server_id}/ha/status")
def get_ha_status(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить статус HA кластера и список ресурсов в HA"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        is_cluster = client.is_cluster()
        ha_resources = client.get_ha_resources() if is_cluster else []
        ha_groups = client.get_ha_groups() if is_cluster else []
        
        return JSONResponse(content={
            "server_id": server_id,
            "is_cluster": is_cluster,
            "ha_enabled": is_cluster,
            "resources": ha_resources,
            "groups": ha_groups
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting HA status for {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/ha/{vm_type}/{vmid}")
def get_resource_ha_status(
    server_id: int,
    vm_type: str,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить HA статус конкретной VM или контейнера"""
    if vm_type not in ['vm', 'ct']:
        raise HTTPException(status_code=400, detail="vm_type must be 'vm' or 'ct'")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        is_cluster = client.is_cluster()
        
        if not is_cluster:
            return JSONResponse(content={
                "vmid": vmid,
                "vm_type": vm_type,
                "is_cluster": False,
                "ha_available": False,
                "in_ha": False
            })
        
        ha_status = client.get_ha_status(vmid, vm_type)
        
        return JSONResponse(content={
            "vmid": vmid,
            "vm_type": vm_type,
            "is_cluster": True,
            "ha_available": True,
            **ha_status
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting HA status for {vm_type}:{vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/ha/{vm_type}/{vmid}/add")
def add_resource_to_ha(
    server_id: int,
    vm_type: str,
    vmid: int,
    group: str = Query(None, description="HA group name"),
    max_restart: int = Query(1, description="Max restart attempts"),
    max_relocate: int = Query(1, description="Max relocate attempts"),
    state: str = Query("started", description="Target state: started, stopped, enabled, disabled, ignored"),
    comment: str = Query(None, description="Comment"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Добавить VM или контейнер в HA"""
    if vm_type not in ['vm', 'ct']:
        raise HTTPException(status_code=400, detail="vm_type must be 'vm' or 'ct'")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        # Проверяем что это кластер
        if not client.is_cluster():
            raise HTTPException(status_code=400, detail="HA is only available in cluster mode")
        
        result = client.add_to_ha(
            vmid=vmid,
            vm_type=vm_type,
            group=group,
            max_restart=max_restart,
            max_relocate=max_relocate,
            state=state,
            comment=comment
        )
        
        if result.get('success'):
            # Log action
            resource_type = "vm" if vm_type == "vm" else "container"
            LoggingService.log_proxmox_action(
                db=db,
                action="add_to_ha",
                resource_type=resource_type,
                resource_id=vmid,
                username=current_user.username,
                server_id=server_id,
                server_name=server.name,
                details={"group": group, "state": state},
                success=True
            )
            logger.info(f"User {current_user.username} added {vm_type}:{vmid} to HA on {server.name}")
            return JSONResponse(content=result)
        else:
            if result.get('already_in_ha'):
                raise HTTPException(status_code=409, detail="Resource is already in HA")
            raise HTTPException(status_code=500, detail=result.get('error', 'Failed to add to HA'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding {vm_type}:{vmid} to HA: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/ha/{vm_type}/{vmid}/remove")
def remove_resource_from_ha(
    server_id: int,
    vm_type: str,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Удалить VM или контейнер из HA"""
    if vm_type not in ['vm', 'ct']:
        raise HTTPException(status_code=400, detail="vm_type must be 'vm' or 'ct'")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        result = client.remove_from_ha(vmid, vm_type)
        
        if result.get('success'):
            # Log action
            resource_type = "vm" if vm_type == "vm" else "container"
            LoggingService.log_proxmox_action(
                db=db,
                action="remove_from_ha",
                resource_type=resource_type,
                resource_id=vmid,
                username=current_user.username,
                server_id=server_id,
                server_name=server.name,
                success=True
            )
            logger.info(f"User {current_user.username} removed {vm_type}:{vmid} from HA on {server.name}")
            return JSONResponse(content=result)
        else:
            if result.get('not_in_ha'):
                raise HTTPException(status_code=404, detail="Resource is not in HA")
            raise HTTPException(status_code=500, detail=result.get('error', 'Failed to remove from HA'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing {vm_type}:{vmid} from HA: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/{server_id}/container/{vmid}")
async def delete_container(
    request: Request,
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.delete"))
):
    """Удалить LXC контейнер"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail=t("server_not_found", lang))
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail=t("failed_to_connect", lang))
        
        # Проверяем статус контейнера
        status = client.get_container_status(node, vmid)
        if status and isinstance(status, dict) and status.get('status') == 'running':
            raise HTTPException(status_code=400, detail=t("cannot_delete_running_container", lang))
        
        # Archive and delete snapshots before deleting container
        container_name = status.get('name') if isinstance(status, dict) else f'CT-{vmid}'
        snapshot_result = archive_and_delete_snapshots(
            db=db,
            client=client,
            server_id=server_id,
            server_name=server.name,
            vmid=vmid,
            vm_name=container_name,
            vm_type='lxc',
            node=node,
            deleted_by=current_user.username,
            deletion_reason=f"Container {vmid} deleted by {current_user.username}"
        )
        
        if snapshot_result["archived"] > 0:
            logger.info(f"Archived {snapshot_result['archived']} snapshots before deleting container {vmid}")
        if snapshot_result["errors"]:
            logger.warning(f"Snapshot cleanup errors for container {vmid}: {snapshot_result['errors']}")
        
        # Удаляем контейнер
        result = client.delete_container(node, vmid)
        if result:
            # Освобождаем IP в IPAM (если есть)
            try:
                ipam = IPAMService(db)
                released, released_ip = ipam.release_ip_by_vmid(
                    proxmox_server_id=server_id,
                    proxmox_vmid=vmid,
                    released_by=current_user.username,
                    reason=f"Container {vmid} deleted"
                )
                if released:
                    logger.info(f"Auto-released IPAM allocation for IP {released_ip} after container {vmid} deletion")
            except Exception as e:
                logger.warning(f"Failed to release IPAM allocation for container {vmid}: {e}")
            
            # Log deletion with snapshot info
            LoggingService.log_proxmox_action(
                db=db,
                action="delete",
                resource_type="container",
                resource_id=vmid,
                username=current_user.username,
                resource_name=container_name,
                server_id=server_id,
                server_name=server.name,
                node_name=node,
                details={
                    "snapshots_archived": snapshot_result["archived"],
                    "snapshots_deleted": snapshot_result["deleted"],
                    "snapshot_names": snapshot_result["snapshots"]
                },
                success=True
            )
            
            logger.info(f"User {current_user.username} deleted container {vmid} on {server.name}")
            return JSONResponse(content={
                "status": "success", 
                "message": f"Контейнер {vmid} удалён",
                "snapshots_archived": snapshot_result["archived"],
                "snapshots_deleted": snapshot_result["deleted"]
            })
        else:
            raise HTTPException(status_code=500, detail="Не удалось удалить контейнер")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting container {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/vm/{vmid}/config")
def get_vm_config(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить конфигурацию VM"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        config = client.get_vm_config(node, vmid)
        return JSONResponse(content=config)
    except Exception as e:
        logger.error(f"Error getting VM {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/{server_id}/vm/{vmid}/config")
async def update_vm_config(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Обновить конфигурацию VM (CPU, Memory, etc.)"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.update_vm_config(node, vmid, data)
        if success:
            logger.info(f"User {current_user.username} updated VM {vmid} config on {server.name}")
            return JSONResponse(content={"status": "success", "message": "Конфигурация обновлена"})
        else:
            raise HTTPException(status_code=500, detail="Не удалось обновить конфигурацию")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating VM {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/vm/{vmid}/disk/resize")
async def resize_vm_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Изменить размер диска VM"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        data = await request.json()
        disk = data.get('disk')
        size = data.get('size')
        
        if not disk or not size:
            raise HTTPException(status_code=400, detail="Требуются параметры disk и size")
        
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.resize_vm_disk(node, vmid, disk, size)
        if success:
            # Обновляем размер диска в базе данных
            vm_instance = get_vm_instance(db, server_id, vmid)
            if vm_instance:
                # Конвертируем размер в GB (размер приходит как "32G")
                size_gb = int(size.rstrip('GMgm').strip())
                vm_instance.disk_size = size_gb
                vm_instance.updated_at = func.now()
                db.commit()
                logger.info(f"Updated disk size in database: VM {vmid} -> {size_gb}GB")
            
            # Перезапускаем VM для применения изменений
            restart_success = client.restart_vm(node, vmid)
            if restart_success:
                logger.info(f"VM {vmid} restarted to apply disk resize")
            
            logger.info(f"User {current_user.username} resized disk {disk} of VM {vmid} to {size}")
            return JSONResponse(content={
                "status": "success", 
                "message": f"Размер диска {disk} изменен на {size}. VM перезапускается для применения изменений."
            })
        else:
            raise HTTPException(status_code=500, detail="Не удалось изменить размер диска")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resizing VM {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/disk/resize")
async def resize_container_disk(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Изменить размер диска контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        data = await request.json()
        disk = data.get('disk', 'rootfs')
        size = data.get('size')
        
        if not size:
            raise HTTPException(status_code=400, detail="Требуется параметр size")
        
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.resize_container_disk(node, vmid, disk, size)
        if success:
            # Обновляем размер диска в базе данных
            vm_instance = get_vm_instance(db, server_id, vmid)
            if vm_instance:
                # Конвертируем размер в GB (размер приходит как "32G")
                size_gb = int(size.rstrip('GMgm').strip())
                vm_instance.disk_size = size_gb
                vm_instance.updated_at = func.now()
                db.commit()
                logger.info(f"Updated disk size in database: Container {vmid} -> {size_gb}GB")
            
            # Перезапускаем контейнер для применения изменений
            restart_success = client.restart_container(node, vmid)
            if restart_success:
                logger.info(f"Container {vmid} restarted to apply disk resize")
            
            logger.info(f"User {current_user.username} resized disk {disk} of container {vmid} to {size}")
            return JSONResponse(content={
                "status": "success", 
                "message": f"Размер диска {disk} изменен на {size}. Контейнер перезапускается для применения изменений."
            })
        else:
            raise HTTPException(status_code=500, detail="Не удалось изменить размер диска")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resizing container {vmid} disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/config")
def get_container_config(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить конфигурацию LXC контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        config = client.get_container_config(node, vmid)
        return JSONResponse(content=config)
    except Exception as e:
        logger.error(f"Error getting container {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/{server_id}/container/{vmid}/config")
async def update_container_config(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.manage"))
):
    """Обновить конфигурацию LXC контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        success = client.update_container_config(node, vmid, data)
        if success:
            logger.info(f"User {current_user.username} updated container {vmid} config on {server.name}")
            return JSONResponse(content={"status": "success", "message": "Конфигурация обновлена"})
        else:
            raise HTTPException(status_code=500, detail="Не удалось обновить конфигурацию")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating container {vmid} config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/{action}")
def control_container(
    server_id: int, 
    vmid: int, 
    action: str, 
    node: str,
    force: int = 0,
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """Управление LXC контейнером (start/stop/restart)"""
    if action not in ['start', 'stop', 'restart']:
        raise HTTPException(status_code=400, detail="Invalid action")
    
    # Проверка прав в зависимости от действия
    permission_map = {
        'start': 'vms.start',
        'stop': 'vms.stop', 
        'restart': 'vms.restart'
    }
    require_permission(current_user, permission_map[action])
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        if action == 'start':
            success = client.start_container(node, vmid)
        elif action == 'stop':
            if force:
                success = client.force_stop_container(node, vmid)
            else:
                success = client.stop_container(node, vmid, force=False)
        else:  # restart
            success = client.restart_container(node, vmid)
        
        action_name = 'kill' if action == 'stop' and force else action
        if success:
            logger.info(f"User {current_user.username} executed {action_name} on container {vmid} at {server.name}")
            return JSONResponse(content={"status": "success", "action": action_name, "vmid": vmid, "node": node})
        else:
            raise HTTPException(status_code=500, detail="Failed to execute action")
    except Exception as e:
        logger.error(f"Error controlling container {vmid} on {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/nodes")
def get_server_nodes(
    server_id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Получить список нод Proxmox сервера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        nodes = client.get_nodes()
        logger.info(f"Nodes for server {server_id} ({server.name}): {nodes}")
        return JSONResponse(content={"nodes": nodes})
    except Exception as e:
        logger.error(f"Error getting nodes from {server.name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== VM/Container Monitoring ====================

@router.get("/api/{server_id}/vm/{vmid}/status")
def get_vm_status(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить детальный статус VM (CPU, RAM, Disk, Network)"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_vm_status(node, vmid)
        return JSONResponse(content=status)
    except Exception as e:
        logger.error(f"Error getting VM {vmid} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/status")
def get_container_status(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить детальный статус LXC контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_container_status(node, vmid)
        return JSONResponse(content=status)
    except Exception as e:
        logger.error(f"Error getting container {vmid} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/vm/{vmid}/interfaces")
def get_vm_interfaces(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить сетевые интерфейсы и IP адреса VM через QEMU guest agent"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        interfaces = client.get_vm_interfaces(node, vmid)
        return JSONResponse(content={"interfaces": interfaces})
    except Exception as e:
        logger.error(f"Error getting VM {vmid} interfaces: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/interfaces")
def get_container_interfaces(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить сетевые интерфейсы и IP адреса контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        interfaces = client.get_container_interfaces(node, vmid)
        return JSONResponse(content={"interfaces": interfaces})
    except Exception as e:
        logger.error(f"Error getting container {vmid} interfaces: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/status")
def get_container_status_api(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить статус контейнера"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_container_status(node, vmid)
        return JSONResponse(content=status)
    except Exception as e:
        logger.error(f"Error getting container {vmid} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/vm/{vmid}/rrddata")
def get_vm_rrddata(
    server_id: int,
    vmid: int,
    node: str,
    timeframe: str = Query("hour", regex="^(hour|day|week|month|year)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить исторические данные VM для графиков (CPU, RAM, Network, Disk IO)"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        rrddata = client.get_vm_rrddata(node, vmid, timeframe)
        return JSONResponse(content=rrddata)
    except Exception as e:
        logger.error(f"Error getting VM {vmid} RRD data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/rrddata")
def get_container_rrddata(
    server_id: int,
    vmid: int,
    node: str,
    timeframe: str = Query("hour", regex="^(hour|day|week|month|year)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить исторические данные контейнера для графиков"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        rrddata = client.get_container_rrddata(node, vmid, timeframe)
        return JSONResponse(content=rrddata)
    except Exception as e:
        logger.error(f"Error getting container {vmid} RRD data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== VNC Console ====================

@router.get("/api/{server_id}/vm/{vmid}/vnc")
def get_vm_vnc(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """Получить VNC данные для подключения к VM"""
    import requests
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Для VNC ОБЯЗАТЕЛЬНО нужен password auth, API token не работает с vncwebsocket
        # Получаем auth ticket и создаём VNC proxy в одной сессии
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="VNC requires password authentication. Please add password to server settings.")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/access/ticket",
            data={
                "username": auth_username,
                "password": password_to_use
            },
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём VNC proxy с этим же ticket (важно - та же сессия!)
        # generate-password=1 создаёт специальный пароль для VNC аутентификации
        vnc_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/nodes/{node}/qemu/{vmid}/vncproxy",
            data={"websocket": 1, "generate-password": 1},
            headers={
                "CSRFPreventionToken": csrf_token
            },
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if vnc_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create VNC proxy: {vnc_response.text}")
        
        vnc_data = vnc_response.json().get("data", {})
        
        # Формируем ответ - password это сгенерированный VNC пароль
        response_data = {
            'port': vnc_data.get('port'),
            'ticket': vnc_data.get('ticket'),
            'password': vnc_data.get('password'),  # Сгенерированный VNC пароль
            'host': server.ip_address,
            'node': node,
            'vmid': vmid,
            'type': 'qemu',
            'auth_ticket': auth_ticket
        }
        
        logger.info(f"User {current_user.username} opened VNC console for VM {vmid}")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VNC for VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/vnc")
def get_container_vnc(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """Получить VNC данные для подключения к LXC контейнеру"""
    import requests
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Для VNC ОБЯЗАТЕЛЬНО нужен password auth
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="VNC requires password authentication. Please add password to server settings.")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/access/ticket",
            data={
                "username": auth_username,
                "password": password_to_use
            },
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём VNC proxy с этим же ticket
        # generate-password=1 создаёт специальный пароль для VNC аутентификации
        vnc_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/nodes/{node}/lxc/{vmid}/vncproxy",
            data={"websocket": 1, "generate-password": 1},
            headers={
                "CSRFPreventionToken": csrf_token
            },
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if vnc_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create VNC proxy: {vnc_response.text}")
        
        vnc_data = vnc_response.json().get("data", {})
        
        # Формируем ответ - password это сгенерированный VNC пароль
        response_data = {
            'port': vnc_data.get('port'),
            'ticket': vnc_data.get('ticket'),
            'password': vnc_data.get('password'),  # Сгенерированный VNC пароль
            'host': server.ip_address,
            'node': node,
            'vmid': vmid,
            'type': 'lxc',
            'auth_ticket': auth_ticket
        }
        
        logger.info(f"User {current_user.username} opened VNC console for container {vmid}")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting VNC for container {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== VNC WebSocket Proxy ====================

@router.websocket("/ws/vnc/{server_id}/{node}/{vmtype}/{vmid}")
async def vnc_websocket_proxy(
    websocket: WebSocket,
    server_id: int,
    node: str,
    vmtype: str,
    vmid: int,
    port: int,
    vncticket: str,
    vnc_password: str = None,  # VNC пароль сгенерированный через generate-password
    auth_ticket: str = None,  # Auth ticket переданный с frontend
    db: Session = Depends(get_db)
):
    """WebSocket прокси для VNC подключения к Proxmox"""
    import websockets
    import urllib.parse
    
    await websocket.accept()
    logger.info(f"VNC WebSocket connection accepted for {vmtype}/{vmid}")
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        logger.error(f"Proxmox server {server_id} not found")
        await websocket.close(code=1008, reason="Proxmox server not found")
        return
    
    # Используем auth_ticket переданный с frontend (он создан в той же сессии что и vncticket)
    if auth_ticket:
        logger.info(f"Using auth_ticket from frontend for VNC WebSocket")
    
    # Отправляем VNC пароль клиенту первым сообщением (для noVNC credentials)
    if vnc_password:
        logger.info("Sending VNC password to client as first message")
        await websocket.send_text(vnc_password)
    
    # Построить URL для Proxmox WebSocket
    # Преобразуем vmtype: vm -> qemu, container -> lxc
    proxmox_vmtype = "qemu" if vmtype == "vm" else "lxc"
    encoded_ticket = urllib.parse.quote(vncticket, safe='')
    proxmox_ws_url = f"wss://{server.ip_address}:8006/api2/json/nodes/{node}/{proxmox_vmtype}/{vmid}/vncwebsocket?port={port}&vncticket={encoded_ticket}"
    
    logger.info(f"Connecting to Proxmox VNC: {server.ip_address}:8006 for {proxmox_vmtype}/{vmid}")
    
    # SSL контекст для самоподписанных сертификатов
    ssl_context = ssl.create_default_context()
    if not server.verify_ssl:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    proxmox_ws = None
    bytes_to_proxmox = 0
    bytes_from_proxmox = 0
    
    # Заголовки для авторизации - auth_ticket уже получен в той же сессии что и vncticket
    extra_headers = []
    if auth_ticket:
        extra_headers.append(("Cookie", f"PVEAuthCookie={auth_ticket}"))
        logger.info(f"Using PVEAuthCookie for WebSocket auth")
    elif not server.use_password and server.api_token_name and server.api_token_value:
        # Для API token - но это обычно не работает для VNC WebSocket в Proxmox
        extra_headers.append(("Authorization", f"PVEAPIToken={server.api_user}!{server.api_token_name}={server.api_token_value}"))
        logger.info("Using API token for WebSocket auth (may not work for VNC)")
    else:
        logger.error("No auth_ticket provided and no API token available")
        await websocket.close(code=1008, reason="No authentication available for VNC")
        return
    
    try:
        # Подключаемся к Proxmox VNC WebSocket
        proxmox_ws = await websockets.connect(
            proxmox_ws_url,
            ssl=ssl_context,
            extra_headers=extra_headers,
            subprotocols=['binary'],
            max_size=None,
            ping_interval=None,
            close_timeout=5
        )
        
        logger.info(f"Connected to Proxmox VNC for {vmtype}/{vmid}")
        
        # Счётчики для отладки
        bytes_to_proxmox = 0
        bytes_from_proxmox = 0
        
        # Создаем задачи для двунаправленного проксирования
        async def client_to_proxmox():
            """Пересылка данных от клиента к Proxmox"""
            nonlocal bytes_to_proxmox
            try:
                while True:
                    try:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if "bytes" in message:
                            bytes_to_proxmox += len(message["bytes"])
                            await proxmox_ws.send(message["bytes"])
                        elif "text" in message:
                            bytes_to_proxmox += len(message["text"])
                            await proxmox_ws.send(message["text"])
                    except WebSocketDisconnect:
                        break
            except Exception as e:
                logger.debug(f"Client to Proxmox ended: {e}")
        
        async def proxmox_to_client():
            """Пересылка данных от Proxmox к клиенту"""
            nonlocal bytes_from_proxmox
            try:
                async for message in proxmox_ws:
                    try:
                        if isinstance(message, bytes):
                            bytes_from_proxmox += len(message)
                            await websocket.send_bytes(message)
                        else:
                            bytes_from_proxmox += len(message)
                            await websocket.send_text(message)
                    except Exception:
                        break
            except Exception as e:
                logger.debug(f"Proxmox to client ended: {e}")
        
        # Запускаем обе задачи
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_proxmox()),
                asyncio.create_task(proxmox_to_client())
            ],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Отменяем оставшиеся задачи
        for task in pending:
            task.cancel()
            
    except Exception as e:
        logger.error(f"VNC WebSocket proxy error: {e}")
    finally:
        logger.debug(f"VNC stats - To Proxmox: {bytes_to_proxmox} bytes, From Proxmox: {bytes_from_proxmox} bytes")
        if proxmox_ws:
            await proxmox_ws.close()
        try:
            await websocket.close()
        except:
            pass
        logger.info(f"VNC WebSocket connection closed for {vmtype}/{vmid}")


# ==================== Node (Host) Status ====================

@router.get("/api/{server_id}/storages")
def get_storages(
    server_id: int,
    node: str = Query(..., description="Node name"),
    content_type: str = Query(None, description="Filter by content type (images, rootdir, vztmpl, etc)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.view"))
):
    """Получить список хранилищ на ноде Proxmox"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        storages = client.get_storages(node)
        
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
    except Exception as e:
        logger.error(f"Error getting storages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/node/status")
def get_node_status(
    server_id: int,
    node: str = Query(..., description="Node name"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить текущий статус ноды Proxmox (хоста)"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        status = client.get_node_status(node)
        if not status:
            raise HTTPException(status_code=404, detail="Node status not found")
        
        return JSONResponse(content=status)
    except Exception as e:
        logger.error(f"Error getting node {node} status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/node/rrddata")
def get_node_rrddata(
    server_id: int,
    node: str = Query(..., description="Node name"),
    timeframe: str = Query("hour", regex="^(hour|day|week|month|year)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить исторические данные ноды для графиков"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        rrddata = client.get_node_rrddata(node, timeframe)
        return JSONResponse(content=rrddata)
    except Exception as e:
        logger.error(f"Error getting node {node} RRD data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== VM Instance Management Functions ====================

def get_next_vmid(db: Session, server_id: int) -> int:
    """Get next available VMID from Proxmox server (sequential)"""
    
    # Get server from DB
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    try:
        # Create Proxmox client and get next VMID from Proxmox API
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        # Get next VMID from Proxmox (it returns sequential IDs)
        vmid = client.get_next_vmid()
        if vmid:
            return vmid
    except Exception as e:
        logger.warning(f"Could not get VMID from Proxmox, falling back to DB: {e}")
    
    # Fallback: get from DB if Proxmox not available
    used_vmids = set(
        row[0] for row in db.query(VMInstance.vmid)
        .filter(VMInstance.server_id == server_id, VMInstance.deleted_at == None)
        .all()
    )
    
    # Find first available VMID starting from 100
    for vmid in range(100, 999999):
        if vmid not in used_vmids:
            return vmid
    
    raise HTTPException(status_code=500, detail="No available VMID")


def archive_and_delete_snapshots(
    db: Session,
    client: ProxmoxClient,
    server_id: int,
    server_name: str,
    vmid: int,
    vm_name: str,
    vm_type: str,
    node: str,
    deleted_by: str,
    deletion_reason: str = None
) -> dict:
    """
    Archive all snapshots to database before deleting them.
    
    This function:
    1. Gets list of all snapshots for the VM/container
    2. Saves each snapshot's configuration to vm_snapshot_archives table
    3. Explicitly deletes each snapshot one by one
    4. Logs each deletion
    
    Args:
        db: Database session
        client: ProxmoxClient instance
        server_id: Proxmox server ID
        server_name: Proxmox server name for logging
        vmid: VM/container ID
        vm_name: VM/container name
        vm_type: 'qemu' for VM, 'lxc' for container
        node: Proxmox node name
        deleted_by: Username who initiated deletion
        deletion_reason: Reason for deletion
    
    Returns:
        Dict with archived count, deleted count, and any errors
    """
    result = {
        "archived": 0,
        "deleted": 0,
        "errors": [],
        "snapshots": []
    }
    
    try:
        # Get all snapshots
        if vm_type == 'lxc':
            snapshots = client.get_container_snapshots(node, vmid)
        else:
            snapshots = client.get_vm_snapshots(node, vmid)
        
        # Filter out 'current' state which is not a real snapshot
        snapshots = [s for s in snapshots if s.get('name') != 'current']
        
        if not snapshots:
            logger.info(f"No snapshots found for {vm_type} {vmid}")
            return result
        
        logger.info(f"Found {len(snapshots)} snapshots to archive for {vm_type} {vmid}")
        
        # Archive and delete each snapshot
        for snap in snapshots:
            snapname = snap.get('name')
            if not snapname:
                continue
            
            # Create archive record
            try:
                archive = VMSnapshotArchive(
                    server_id=server_id,
                    server_name=server_name,
                    vmid=vmid,
                    vm_name=vm_name,
                    vm_type=vm_type,
                    node=node,
                    snapname=snapname,
                    description=snap.get('description'),
                    snaptime=snap.get('snaptime'),
                    parent=snap.get('parent'),
                    vmstate=bool(snap.get('vmstate')),
                    snapshot_config=snap,
                    deleted_by=deleted_by,
                    deletion_reason=deletion_reason
                )
                db.add(archive)
                db.flush()  # Get ID immediately
                result["archived"] += 1
                result["snapshots"].append(snapname)
                logger.info(f"Archived snapshot {snapname} for {vm_type} {vmid}")
            except Exception as e:
                logger.error(f"Error archiving snapshot {snapname}: {e}")
                result["errors"].append(f"Archive {snapname}: {str(e)}")
            
            # Delete snapshot from Proxmox
            try:
                if vm_type == 'lxc':
                    delete_result = client.delete_container_snapshot(node, vmid, snapname, force=True)
                else:
                    delete_result = client.delete_vm_snapshot(node, vmid, snapname, force=True)
                
                if delete_result.get('success'):
                    result["deleted"] += 1
                    
                    # Log the deletion
                    LoggingService.log_proxmox_action(
                        db=db,
                        action="snapshot_delete",
                        resource_type=vm_type,
                        resource_id=vmid,
                        username=deleted_by,
                        resource_name=snapname,
                        server_id=server_id,
                        server_name=server_name,
                        node_name=node,
                        details={
                            "reason": f"Pre-deletion cleanup for {vm_type} {vmid}",
                            "cascade_delete": True
                        },
                        success=True
                    )
                    logger.info(f"Deleted snapshot {snapname} for {vm_type} {vmid}")
                else:
                    error_msg = delete_result.get('error', 'Unknown error')
                    result["errors"].append(f"Delete {snapname}: {error_msg}")
                    logger.warning(f"Failed to delete snapshot {snapname}: {error_msg}")
            except Exception as e:
                logger.error(f"Error deleting snapshot {snapname}: {e}")
                result["errors"].append(f"Delete {snapname}: {str(e)}")
        
        # Commit archive records
        db.commit()
        
        logger.info(f"Snapshot cleanup for {vm_type} {vmid}: archived={result['archived']}, deleted={result['deleted']}")
        
    except Exception as e:
        logger.error(f"Error in archive_and_delete_snapshots for {vm_type} {vmid}: {e}")
        result["errors"].append(f"General error: {str(e)}")
    
    return result


def save_vm_instance(
    db: Session,
    server_id: int,
    vmid: int,
    node: str,
    vm_type: str,
    name: str,
    cores: int = None,
    memory: int = None,
    disk_size: int = None,
    ip_address: str = None,
    ip_prefix: int = 24,
    gateway: str = None,
    nameserver: str = None,
    cloud_init_user: str = None,
    cloud_init_password: str = None,
    ssh_keys: str = None,
    template_id: int = None,
    template_name: str = None,
    description: str = None,
    extra_config: dict = None,
    owner_id: int = None
) -> VMInstance:
    """Save or update VM instance configuration"""
    
    # Проверяем, существует ли уже запись
    existing = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at == None
    ).first()
    
    if existing:
        # Обновляем существующую запись
        existing.node = node
        existing.vm_type = vm_type
        existing.name = name
        existing.cores = cores
        existing.memory = memory
        existing.disk_size = disk_size
        existing.ip_address = ip_address
        existing.ip_prefix = ip_prefix
        existing.gateway = gateway
        existing.nameserver = nameserver
        existing.cloud_init_user = cloud_init_user
        existing.cloud_init_password = cloud_init_password
        existing.ssh_keys = ssh_keys
        existing.template_id = template_id
        existing.template_name = template_name
        existing.description = description
        existing.extra_config = extra_config
        existing.updated_at = func.now()
        db.commit()
        db.refresh(existing)
        return existing
    else:
        # Создаем новую запись
        instance = VMInstance(
            server_id=server_id,
            vmid=vmid,
            node=node,
            vm_type=vm_type,
            name=name,
            cores=cores,
            memory=memory,
            disk_size=disk_size,
            ip_address=ip_address,
            ip_prefix=ip_prefix,
            gateway=gateway,
            nameserver=nameserver,
            cloud_init_user=cloud_init_user,
            cloud_init_password=cloud_init_password,
            ssh_keys=ssh_keys,
            template_id=template_id,
            template_name=template_name,
            description=description,
            extra_config=extra_config,
            owner_id=owner_id
        )
        db.add(instance)
        db.commit()
        db.refresh(instance)
        return instance


def get_vm_instance(db: Session, server_id: int, vmid: int) -> VMInstance:
    """Get VM instance configuration"""
    return db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at == None
    ).first()


def soft_delete_vm_instance(db: Session, server_id: int, vmid: int):
    """Soft delete VM instance (mark as deleted) and release IPAM"""
    instance = get_vm_instance(db, server_id, vmid)
    if instance:
        # Release IPAM allocation
        try:
            ipam = IPAMService(db)
            released = ipam.release_ip_by_vmid(
                proxmox_server_id=server_id,
                proxmox_vmid=vmid,
                released_by="system",
                reason="VM/Container soft deleted"
            )
            if released:
                logger.info(f"Released IPAM for soft-deleted instance {vmid} on server {server_id}")
        except Exception as e:
            logger.warning(f"Failed to release IPAM for soft-deleted instance: {e}")
        
        instance.deleted_at = func.now()
        db.commit()


@router.get("/api/{server_id}/vm/{vmid}/saved-config")
def get_saved_vm_config(
    server_id: int,
    vmid: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Get saved VM configuration from database"""
    instance = get_vm_instance(db, server_id, vmid)
    if not instance:
        return JSONResponse(content={"found": False})
    
    return JSONResponse(content={
        "found": True,
        "config": {
            "cores": instance.cores,
            "memory": instance.memory,
            "disk_size": instance.disk_size,
            "ip_address": instance.ip_address,
            "ip_prefix": instance.ip_prefix,
            "gateway": instance.gateway,
            "nameserver": instance.nameserver,
            "cloud_init_user": instance.cloud_init_user,
            "cloud_init_password": instance.cloud_init_password,
            "ssh_keys": instance.ssh_keys,
            "name": instance.name,
            "template_id": instance.template_id
        }
    })


# ==================== LXC Container Creation ====================

@router.get("/api/{server_id}/lxc-templates")
def get_lxc_templates(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.view"))
):
    """Получить список доступных шаблонов LXC"""
    logger.info(f"[LXC API] Request for templates: server_id={server_id}, node={node}")
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Determine auth method
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        templates = client.get_lxc_templates(node)
        logger.info(f"[LXC API] Found {len(templates)} templates")
        return JSONResponse(content=templates)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting LXC templates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/all-lxc-templates")
def get_all_lxc_templates(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.view"))
):
    """Получить список LXC шаблонов со всех нод кластера с информацией о shared storage"""
    logger.info(f"[LXC API] Request for ALL templates from server_id={server_id}")
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        templates = client.get_all_lxc_templates()
        logger.info(f"[LXC API] Found {len(templates)} total templates across all nodes")
        return JSONResponse(content={"templates": templates})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting all LXC templates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/available-lxc-templates")
def get_available_lxc_templates(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.view"))
):
    """Получить список шаблонов доступных для загрузки из репозитория"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        # Determine auth method
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        templates = client.get_available_lxc_templates(node)
        return JSONResponse(content=templates)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting available LXC templates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/download-lxc-template")
async def download_lxc_template(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("templates.manage"))
):
    """Скачать шаблон LXC из репозитория"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    node = data.get('node')
    storage = data.get('storage')
    template = data.get('template')
    
    if not all([node, storage, template]):
        raise HTTPException(status_code=400, detail="Missing required parameters: node, storage, template")
    
    try:
        # Determine auth method
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        upid = client.download_lxc_template(node, storage, template)
        if not upid:
            raise HTTPException(status_code=500, detail="Failed to start template download")
        
        LoggingService.log_proxmox_action(
            db=db,
            action="download_template",
            resource_type="template",
            resource_id=template,
            username=current_user.username,
            resource_name=template,
            server_id=server_id,
            server_name=server.name,
            node_name=node,
            details={"storage": storage},
            success=True,
            ip_address=request.client.host if request.client else None
        )
        
        return JSONResponse(content={"success": True, "upid": upid})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading LXC template: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/create-lxc")
async def create_lxc_container(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.create"))
):
    """Создать новый LXC контейнер"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    node = data.get('node')
    vmid = data.get('vmid')
    ostemplate = data.get('ostemplate')  # storage:vztmpl/template.tar.gz
    hostname = data.get('hostname')
    password = data.get('password')
    ssh_public_keys = data.get('ssh_public_keys')
    storage = data.get('storage', 'local-lvm')
    rootfs_size = data.get('rootfs_size', 8)
    memory = data.get('memory', 512)
    swap = data.get('swap', 512)
    cores = data.get('cores', 1)
    net0 = data.get('net0')
    unprivileged = data.get('unprivileged', True)
    start_after_create = data.get('start_after_create', False)
    onboot = data.get('onboot', False)
    description = data.get('description')
    features = data.get('features')
    
    if not all([node, ostemplate, hostname]):
        raise HTTPException(status_code=400, detail="Missing required parameters: node, ostemplate, hostname")
    
    if not password and not ssh_public_keys:
        raise HTTPException(status_code=400, detail="Either password or SSH keys must be provided")
    
    try:
        # Determine auth method
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        # Получить следующий VMID если не указан
        if not vmid:
            vmid = client.get_next_vmid()
            if not vmid:
                raise HTTPException(status_code=500, detail="Failed to get next VMID")
        
        upid = client.create_lxc_container(
            node=node,
            vmid=vmid,
            ostemplate=ostemplate,
            hostname=hostname,
            password=password,
            ssh_public_keys=ssh_public_keys,
            storage=storage,
            rootfs_size=rootfs_size,
            memory=memory,
            swap=swap,
            cores=cores,
            net0=net0,
            unprivileged=unprivileged,
            start_after_create=start_after_create,
            onboot=onboot,
            description=description,
            features=features
        )
        
        if not upid:
            raise HTTPException(status_code=500, detail="Failed to create LXC container")
        
        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="container",
            resource_id=vmid,
            username=current_user.username,
            resource_name=hostname,
            server_id=server_id,
            server_name=server.name,
            node_name=node,
            details={"template": ostemplate, "memory": memory, "cores": cores},
            success=True,
            ip_address=request.client.host if request.client else None
        )
        
        return JSONResponse(content={
            "success": True,
            "vmid": vmid,
            "upid": upid,
            "message": f"LXC container {hostname} (ID: {vmid}) creation started"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating LXC container: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/create-lxc-smart")
async def create_lxc_container_smart(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.create"))
):
    """
    Умное создание LXC контейнера с поддержкой кросс-нодного деплоя.
    
    Если шаблон на shared storage или на целевой ноде - создаём напрямую.
    Если шаблон на локальном хранилище другой ноды - создаём там и мигрируем.
    """
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    template_node = data.get('template_node')  # Нода где находится шаблон
    target_node = data.get('target_node')      # Целевая нода для контейнера
    ostemplate = data.get('ostemplate')
    hostname = data.get('hostname')
    password = data.get('password')
    storage = data.get('storage', 'local-lvm')
    rootfs_size = data.get('rootfs_size', 8)
    memory = data.get('memory', 512)
    swap = data.get('swap', 512)
    cores = data.get('cores', 1)
    start_after_create = data.get('start_after_create', False)
    onboot = data.get('onboot', False)
    enable_ha = data.get('enable_ha', False)
    is_shared = data.get('is_shared', False)
    net0 = data.get('net0')  # Network configuration from IPAM
    ipam_allocation_id = data.get('ipam_allocation_id')  # IPAM allocation ID for tracking
    
    if not all([template_node, target_node, ostemplate, hostname]):
        raise HTTPException(status_code=400, detail="Missing required parameters")
    
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        vmid = client.get_next_vmid()
        if not vmid:
            raise HTTPException(status_code=500, detail="Failed to get next VMID")
        
        # Определяем стратегию создания
        needs_migration = not is_shared and template_node != target_node
        create_node = template_node if needs_migration else target_node
        
        logger.info(f"[LXC Smart Create] Template on {template_node}, target {target_node}, shared={is_shared}, needs_migration={needs_migration}, net0={net0}")
        
        # Создаём контейнер
        upid = client.create_lxc_container(
            node=create_node,
            vmid=vmid,
            ostemplate=ostemplate,
            hostname=hostname,
            password=password,
            storage=storage,
            rootfs_size=rootfs_size,
            memory=memory,
            swap=swap,
            cores=cores,
            net0=net0,  # Network configuration from IPAM
            unprivileged=True,
            start_after_create=False,  # Не запускаем до миграции
            onboot=onboot
        )
        
        if not upid:
            raise HTTPException(status_code=500, detail="Failed to create LXC container")
        
        # Ждём завершения создания
        create_success = client.wait_for_task(create_node, upid, timeout=300)
        if not create_success:
            raise HTTPException(status_code=500, detail="Container creation timed out")
        
        # Если нужна миграция
        if needs_migration:
            logger.info(f"[LXC Smart Create] Migrating container {vmid} from {create_node} to {target_node}")
            
            migrate_upid = client.migrate_container(create_node, vmid, target_node, target_storage=storage)
            if migrate_upid:
                migrate_success = client.wait_for_task(create_node, migrate_upid, timeout=600)
                if not migrate_success:
                    logger.warning(f"Migration of container {vmid} may have timed out")
            else:
                logger.warning(f"Failed to start migration for container {vmid}")
        
        # Запускаем если нужно
        if start_after_create:
            final_node = target_node
            client.start_container(final_node, vmid)
        
        # Enable High Availability if requested (cluster only)
        ha_enabled = False
        if enable_ha:
            try:
                if client.is_cluster():
                    ha_result = client.add_to_ha(vmid=vmid, vm_type='ct', max_restart=3, max_relocate=3)
                    if ha_result.get('success'):
                        ha_enabled = True
                        logger.info(f"HA enabled for LXC container {vmid}")
                    else:
                        logger.warning(f"Failed to enable HA for LXC {vmid}: {ha_result.get('error')}")
                else:
                    logger.warning(f"HA requested but server {server.name} is not in a cluster")
            except Exception as ha_err:
                logger.warning(f"Failed to enable HA for LXC {vmid}: {ha_err}")
        
        # Update IPAM allocation with VMID and server info
        if ipam_allocation_id:
            try:
                from ..models import IPAMAllocation
                allocation = db.query(IPAMAllocation).filter(IPAMAllocation.id == ipam_allocation_id).first()
                if allocation:
                    allocation.resource_id = vmid
                    allocation.proxmox_vmid = vmid
                    allocation.proxmox_server_id = server.id
                    allocation.proxmox_node = target_node
                    allocation.resource_type = "lxc"
                    allocation.resource_name = hostname
                    db.commit()
                    logger.info(f"[IPAM] Updated allocation {ipam_allocation_id} with VMID {vmid}, server_id {server.id}")
            except Exception as ipam_err:
                logger.warning(f"[IPAM] Failed to update allocation {ipam_allocation_id}: {ipam_err}")
        
        LoggingService.log_proxmox_action(
            db=db,
            action="create",
            resource_type="container",
            resource_id=vmid,
            username=current_user.username,
            resource_name=hostname,
            server_id=server_id,
            server_name=server.name,
            node_name=target_node,
            details={
                "template_node": template_node,
                "template": ostemplate,
                "migrated": needs_migration
            },
            success=True,
            ip_address=request.client.host if request.client else None
        )
        
        migration_msg = f" (migrated from {template_node})" if needs_migration else ""
        return JSONResponse(content={
            "success": True,
            "vmid": vmid,
            "node": target_node,
            "message": f"LXC container {hostname} (ID: {vmid}) created on {target_node}{migration_msg}"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in smart LXC creation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/clone-lxc")
async def clone_lxc_container(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.create"))
):
    """Клонировать LXC контейнер"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    data = await request.json()
    
    node = data.get('node')
    source_vmid = data.get('source_vmid')
    new_vmid = data.get('new_vmid')
    hostname = data.get('hostname')
    full_clone = data.get('full_clone', True)
    target_storage = data.get('target_storage')
    description = data.get('description')
    disk_size = data.get('disk_size')  # Disk size in GB for resize after clone
    
    if not all([node, source_vmid, hostname]):
        raise HTTPException(status_code=400, detail="Missing required parameters")
    
    try:
        # Determine auth method
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
        
        if not new_vmid:
            new_vmid = client.get_next_vmid()
            if not new_vmid:
                raise HTTPException(status_code=500, detail="Failed to get next VMID")
        
        upid = client.clone_lxc_container(
            node=node,
            source_vmid=source_vmid,
            new_vmid=new_vmid,
            hostname=hostname,
            full_clone=full_clone,
            target_storage=target_storage,
            description=description
        )
        
        if not upid:
            raise HTTPException(status_code=500, detail="Failed to clone LXC container")
        
        # Wait for clone to complete and resize disk if requested
        if disk_size:
            clone_success = client.wait_for_task(node, upid, timeout=300)
            if clone_success:
                # Resize rootfs to requested size
                try:
                    client.resize_container_disk(node, new_vmid, 'rootfs', f'{disk_size}G')
                    logger.info(f"Resized LXC {new_vmid} rootfs to {disk_size}G")
                except Exception as e:
                    logger.warning(f"Failed to resize LXC {new_vmid} disk: {e}")
        
        # Get source template name for display
        source_template_name = None
        template_id = data.get('template_id')
        if template_id:
            template = db.query(OSTemplate).filter(OSTemplate.id == template_id).first()
            if template:
                source_template_name = template.name
        
        # Save LXC instance to database with owner
        save_vm_instance(
            db=db,
            server_id=server_id,
            vmid=new_vmid,
            node=node,
            vm_type='lxc',
            name=hostname,
            disk_size=disk_size,
            template_id=template_id,
            template_name=source_template_name,
            description=description or f"Cloned from container {source_vmid}",
            owner_id=current_user.id  # VPS-style user isolation
        )
        
        LoggingService.log_proxmox_action(
            db=db,
            action="clone",
            resource_type="container",
            resource_id=new_vmid,
            username=current_user.username,
            resource_name=hostname,
            server_id=server_id,
            server_name=server.name,
            node_name=node,
            details={"source_vmid": source_vmid, "full_clone": full_clone},
            success=True,
            ip_address=request.client.host if request.client else None
        )
        
        return JSONResponse(content={
            "success": True,
            "vmid": new_vmid,
            "upid": upid,
            "message": f"LXC container clone started"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cloning LXC container: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Terminal Proxy (xterm.js) ====================

@router.get("/api/{server_id}/vm/{vmid}/terminal")
def get_vm_terminal(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """Получить данные для xterm.js терминального подключения к VM"""
    import requests
    
    # VPS-style user isolation: check VM ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="Terminal requires password authentication")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/access/ticket",
            data={"username": auth_username, "password": password_to_use},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём terminal proxy
        term_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/nodes/{node}/qemu/{vmid}/termproxy",
            headers={"CSRFPreventionToken": csrf_token},
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if term_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create terminal proxy: {term_response.text}")
        
        term_data = term_response.json().get("data", {})
        
        response_data = {
            'port': term_data.get('port'),
            'ticket': term_data.get('ticket'),
            'host': server.ip_address,
            'node': node,
            'vmid': vmid,
            'type': 'qemu',
            'auth_ticket': auth_ticket
        }
        
        logger.info(f"User {current_user.username} opened terminal for VM {vmid}")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting terminal for VM {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/container/{vmid}/terminal")
def get_container_terminal(
    server_id: int,
    vmid: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """Получить данные для xterm.js терминального подключения к LXC контейнеру"""
    import requests
    
    # VPS-style user isolation: check container ownership for limited users
    require_vm_access(db, current_user, server_id, vmid)
    
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        password_to_use = server.password if server.password else None
        auth_username = server.api_user.split("!")[0] if "!" in server.api_user else server.api_user
        
        if not password_to_use:
            raise HTTPException(status_code=400, detail="Terminal requires password authentication")
        
        # 1. Получаем auth ticket
        auth_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/access/ticket",
            data={"username": auth_username, "password": password_to_use},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if auth_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Failed to authenticate to Proxmox")
        
        auth_data = auth_response.json().get("data", {})
        auth_ticket = auth_data.get("ticket")
        csrf_token = auth_data.get("CSRFPreventionToken")
        
        # 2. Создаём terminal proxy
        term_response = requests.post(
            f"https://{server.ip_address}:8006/api2/json/nodes/{node}/lxc/{vmid}/termproxy",
            headers={"CSRFPreventionToken": csrf_token},
            cookies={"PVEAuthCookie": auth_ticket},
            verify=server.verify_ssl,
            timeout=10
        )
        
        if term_response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Failed to create terminal proxy: {term_response.text}")
        
        term_data = term_response.json().get("data", {})
        
        response_data = {
            'port': term_data.get('port'),
            'ticket': term_data.get('ticket'),
            'host': server.ip_address,
            'node': node,
            'vmid': vmid,
            'type': 'lxc',
            'auth_ticket': auth_ticket
        }
        
        logger.info(f"User {current_user.username} opened terminal for LXC {vmid}")
        return JSONResponse(content=response_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting terminal for LXC {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/{server_id}/container/{vmid}/exec")
async def exec_in_container(
    server_id: int,
    vmid: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.console"))
):
    """Выполнить команду в LXC контейнере через pct exec"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        data = await request.json()
        command = data.get('command', '/bin/bash')
        args = data.get('args', [])
        
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        # Выполняем команду
        full_command = [command] + (args if isinstance(args, list) else [])
        result = client.exec_in_container(node, vmid, full_command)
        
        if not result:
            raise HTTPException(status_code=500, detail="Failed to execute command")
        
        logger.info(f"User {current_user.username} executed command in LXC {vmid}: {command}")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing command in LXC {vmid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/task/{upid}/status")
def get_task_status(
    server_id: int,
    upid: str,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить статус задачи по UPID"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
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
    current_user: User = Depends(PermissionChecker("vms.view"))
):
    """Получить лог задачи по UPID"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        if server.use_password:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        else:
            client = ProxmoxClient(
                host=server.ip_address,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        
        if not client.is_connected():
            raise HTTPException(status_code=503, detail="Failed to connect to Proxmox server")
        
        logs = client.get_task_log(node, upid, start, limit)
        return JSONResponse(content={"logs": logs})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task log {upid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Terminal WebSocket Proxy ====================

@router.websocket("/ws/terminal/{server_id}/{node}/{vmid}")
async def terminal_websocket_fixed(
    websocket: WebSocket,
    server_id: int,
    node: str,
    vmid: int,
    db: Session = Depends(get_db)
):
    """WebSocket терминал для LXC контейнеров через Proxmox termproxy API"""
    import websockets
    import httpx
    
    await websocket.accept()
    logger.info(f"Terminal WebSocket accepted for container {vmid} on node {node}")
    
    # Получаем сервер из БД
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        logger.error(f"Proxmox server {server_id} not found")
        await websocket.close(code=1008, reason="Proxmox server not found")
        return
    
    proxmox_ws = None
    
    try:
        # Шаг 1: Получаем auth ticket через Proxmox API
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            auth_url = f"https://{server.ip_address}:8006/api2/json/access/ticket"
            
            # Используем пароль если есть, иначе API token
            if server.password:
                auth_data = {
                    "username": server.username,
                    "password": server.password
                }
                response = await client.post(auth_url, data=auth_data)
            else:
                # Для API token используем другой метод авторизации
                logger.error("Terminal requires password authentication, API tokens not supported")
                await websocket.close(code=1011, reason="Terminal requires password auth")
                return
            
            if response.status_code != 200:
                logger.error(f"Auth failed: {response.status_code}")
                await websocket.close(code=1011, reason="Authentication failed")
                return
            
            auth_result = response.json()
            ticket = auth_result["data"]["ticket"]
            csrf_token = auth_result["data"]["CSRFPreventionToken"]
            
            logger.info(f"✅ Auth ticket obtained for {server.username}")
            
            # Шаг 2: Получаем termproxy ticket
            termproxy_url = f"https://{server.ip_address}:8006/api2/json/nodes/{node}/lxc/{vmid}/termproxy"
            headers = {
                "CSRFPreventionToken": csrf_token,
                "Cookie": f"PVEAuthCookie={ticket}"
            }
            payload = {
                "CSRFPreventionToken": csrf_token
            }
            
            response = await client.post(termproxy_url, headers=headers, data=payload)
            
            if response.status_code != 200:
                logger.error(f"Termproxy failed: {response.status_code} - {response.text}")
                await websocket.close(code=1011, reason="Failed to create terminal session")
                return
            
            termproxy_result = response.json()
            vncticket = termproxy_result["data"]["ticket"]
            port = termproxy_result["data"]["port"]
            
            logger.info(f"✅ Termproxy ticket obtained, port: {port}")
        
        # Шаг 3: Подключаемся к WebSocket
        websocket_url = (
            f"wss://{server.ip_address}:8006/api2/json/nodes/{node}/lxc/{vmid}/vncwebsocket?"
            f"port={port}&vncticket={vncticket}"
        )
        
        ssl_context = ssl.create_default_context()
        if not server.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        
        logger.info(f"Connecting to Proxmox terminal WebSocket...")
        
        async with websockets.connect(
            websocket_url,
            ssl=ssl_context,
            max_size=None,
            ping_interval=30,
            ping_timeout=10
        ) as proxmox_ws:
            logger.info(f"✅ Connected to Proxmox terminal WebSocket")
            
            async def client_to_proxmox():
                """Передача данных от xterm.js к Proxmox"""
                try:
                    while True:
                        data = await websocket.receive()
                        if 'text' in data:
                            msg = data['text']
                            # Proxmox ожидает чистые данные без протокола
                            if msg.startswith('0:'):
                                # stdin: "0:LENGTH:DATA"
                                parts = msg.split(':', 2)
                                if len(parts) == 3:
                                    terminal_data = parts[2]
                                    await proxmox_ws.send(terminal_data)
                            elif msg.startswith('1:'):
                                # resize - Proxmox не поддерживает через WebSocket
                                pass
                            elif msg == '2':
                                # ping
                                pass
                        elif 'bytes' in data:
                            await proxmox_ws.send(data['bytes'])
                except WebSocketDisconnect:
                    logger.info(f"Client disconnected from terminal {vmid}")
                except Exception as e:
                    logger.error(f"Error in client_to_proxmox: {e}")
            
            async def proxmox_to_client():
                """Передача данных от Proxmox к xterm.js"""
                try:
                    async for message in proxmox_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except websockets.exceptions.ConnectionClosed:
                    logger.info(f"Proxmox terminal WebSocket closed for {vmid}")
                except Exception as e:
                    logger.error(f"Error in proxmox_to_client: {e}")
            
            # Запускаем обе задачи параллельно
            await asyncio.gather(
                client_to_proxmox(),
                proxmox_to_client(),
                return_exceptions=True
            )
        
    except Exception as e:
        logger.error(f"Terminal WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        try:
            await websocket.close(code=1011, reason=str(e))
        except:
            pass
    finally:
        logger.info(f"Terminal session closed for container {vmid}")


# ==================== Bulk Operations API ====================

from ..models import TaskQueue
from ..services.task_queue_service import TaskQueueService, process_task_queue
from pydantic import BaseModel
from typing import List as TypingList, Optional


class BulkOperationItem(BaseModel):
    server_id: int
    vmid: int
    vm_type: str  # 'qemu' or 'lxc'
    name: str
    node: str


class BulkOperationRequest(BaseModel):
    action: str  # start, stop, restart, shutdown, delete
    items: TypingList[BulkOperationItem]


@router.post("/api/bulk-operation")
def create_bulk_operation(
    request: BulkOperationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("proxmox.vm.manage"))
):
    """
    Create a bulk operation task.
    Returns task ID for tracking progress.
    """
    # Map action to task type
    action_to_task_type = {
        'start': 'bulk_start',
        'stop': 'bulk_stop',
        'restart': 'bulk_restart',
        'shutdown': 'bulk_shutdown',
        'delete': 'bulk_delete',
    }
    
    task_type = action_to_task_type.get(request.action)
    if not task_type:
        raise HTTPException(status_code=400, detail=f"Invalid action: {request.action}")
    
    if not request.items:
        raise HTTPException(status_code=400, detail="No items selected")
    
    # For delete action, require higher permission
    if request.action == 'delete':
        if not check_permission(current_user, "vms.delete"):
            raise HTTPException(status_code=403, detail="Delete permission required")
    
    # Convert to list of dicts
    items = [item.model_dump() for item in request.items]
    
    try:
        task = TaskQueueService.create_task(
            db=db,
            task_type=task_type,
            user_id=current_user.id,
            items=items
        )
        
        logger.info(f"User {current_user.username} created bulk operation: {task_type} for {len(items)} items")
        
        return {
            "success": True,
            "task_id": task.id,
            "message": f"Bulk operation queued: {len(items)} items"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/tasks")
def get_user_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=100)
):
    """Get recent tasks for current user"""
    tasks = TaskQueueService.get_user_tasks(db, current_user.id, limit)
    return {
        "tasks": [task.to_dict() for task in tasks]
    }


@router.get("/api/tasks/active")
def get_active_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all active (pending/running) tasks for current user"""
    tasks = db.query(TaskQueue).filter(
        TaskQueue.user_id == current_user.id,
        TaskQueue.status.in_(['pending', 'running'])
    ).order_by(TaskQueue.created_at.desc()).all()
    
    return {
        "tasks": [task.to_dict() for task in tasks]
    }


@router.get("/api/tasks/{task_id}")
def get_task_status(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get status of a specific task"""
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
    """Cancel a pending task"""
    success = TaskQueueService.cancel_task(db, task_id, current_user.id)
    
    if not success:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled (not found, not pending, or not yours)")
    
    return {"success": True, "message": "Task cancelled"}

