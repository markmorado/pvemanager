"""
Cluster Topology Management API
=================================
Endpoints for creating Proxmox clusters, joining nodes, ejecting nodes,
and preparing nodes for join (backup + cleanup).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from loguru import logger
import asyncio
from typing import Optional

from ...db import get_db
from ...models import ProxmoxServer, User
from ...auth import PermissionChecker
from ...proxmox import ProxmoxClient
from ._helpers import _get_proxmox_client, _extract_fingerprint, get_visible_server_ids
from ...proxmox import _run_in_executor

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


# ==================== Helpers ====================

def _get_server_or_404(db: Session, server_id: int) -> ProxmoxServer:
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found")
    return server


def _get_password_client(server: ProxmoxServer, override_password: str = None) -> ProxmoxClient:
    """
    Build a ProxmoxClient that uses password auth (required for cluster ops).
    Falls back to stored password, or raises 400 if none available.
    """
    pw = override_password or server.password
    if not pw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Для операций с кластером требуется пароль root@pam. "
                "Убедитесь, что сервер добавлен через Auto-Setup с паролем, "
                "или передайте пароль явно в параметре rootpw."
            )
        )
    return ProxmoxClient(
        host=server.ip_address,
        user=server.api_user,
        password=pw,
        verify_ssl=server.verify_ssl,
    )


# ==================== GET /api/cluster/topology ====================

@router.get("/api/cluster/topology")
def get_cluster_topology(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("server:view")),
):
    """
    Получить серверы, видимые пользователю, сгруппированные по кластеру.
    Standalone-серверы (cluster_name=NULL) — в отдельном списке.

    Фильтруется так же, как GET /api/servers: по доступу пользователя и по
    активной рабочей области (заголовок X-Active-Workspace).
    """
    visible_ids = get_visible_server_ids(request, db, current_user)
    if visible_ids is not None and not visible_ids:
        return {"clusters": {}, "standalone": []}

    query = db.query(ProxmoxServer)
    if visible_ids is not None:
        query = query.filter(ProxmoxServer.id.in_(visible_ids))
    servers = query.all()

    clusters: dict = {}
    standalone = []

    for s in servers:
        entry = {
            "id": s.id,
            "name": s.name,
            "hostname": s.hostname,
            "ip_address": s.ip_address,
            "port": s.port,
            "is_online": s.is_online,
            "last_error_kind": s.last_error_kind,
            "cluster_name": s.cluster_name,
        }
        if s.cluster_name:
            clusters.setdefault(s.cluster_name, []).append(entry)
        else:
            standalone.append(entry)

    return {"clusters": clusters, "standalone": standalone}


# ==================== POST /api/cluster/create ====================

@router.post("/api/cluster/create", status_code=status.HTTP_201_CREATED)
async def create_cluster(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Создать новый Proxmox corosync кластер на указанной ноде.

    Body:
        server_id (int): ID ноды в панели
        cluster_name (str): Уникальное имя кластера
        link0_ip (str, optional): IP для corosync link0
        rootpw (str, optional): root-пароль (если не сохранён)
    """
    server_id = body.get("server_id")
    cluster_name = body.get("cluster_name", "").strip()
    link0_ip = body.get("link0_ip")
    rootpw = body.get("rootpw")

    if not server_id or not cluster_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать server_id и cluster_name"
        )

    # Validate cluster_name matches Proxmox node name rules:
    # lowercase, alphanumeric + hyphens, starts with letter, max 15 chars
    import re
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9\-]{0,14}$', cluster_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cluster_name должен начинаться с буквы, содержать только буквы/цифры/дефис, макс 15 символов"
        )

    server = _get_server_or_404(db, server_id)

    # Check if server is already part of a cluster
    if server.cluster_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Сервер '{server.name}' уже состоит в кластере '{server.cluster_name}'"
        )

    client = _get_password_client(server, rootpw)

    # Verify server is not already in a cluster on Proxmox side
    if await _run_in_executor(client.is_cluster):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нода уже является частью кластера Proxmox. Используйте join для добавления в существующий кластер."
        )

    result = await _run_in_executor(client.create_cluster, cluster_name=cluster_name, link0=link0_ip)

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ошибка создания кластера: {result.get('error')}"
        )

    # Update panel DB
    server.cluster_name = cluster_name
    db.commit()

    logger.info(
        f"User {current_user.username} created cluster '{cluster_name}' on server '{server.name}'"
    )

    return {
        "success": True,
        "cluster_name": cluster_name,
        "server_id": server_id,
        "server_name": server.name,
        "upid": result.get("upid"),
        "message": f"Кластер '{cluster_name}' создаётся. Отслеживайте задачу через upid.",
    }


# ==================== GET /api/cluster/{server_id}/join-info ====================

@router.get("/api/cluster/{server_id}/join-info")
def get_cluster_join_info(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Получить fingerprint и join-данные с кластерной ноды.
    Используется для последующего вызова /api/cluster/join.
    """
    server = _get_server_or_404(db, server_id)
    client = _get_proxmox_client(server)

    result = client.get_cluster_join_info()

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось получить join-информацию: {result.get('error')}"
        )

    data = result.get("data", {})

    # Extract the most useful fields for the frontend
    totem = data.get("totem", {})
    return {
        "server_id": server_id,
        "server_name": server.name,
        "cluster_name": totem.get("cluster_name") or server.cluster_name,
        "fingerprint": _extract_fingerprint(data, target_ip=server.ip_address),
        "nodelist": data.get("nodelist", []),
        "config_digest": data.get("config_digest"),
        "preferred_node_ip": server.ip_address,
    }


# ==================== GET /api/cluster/{server_id}/pre-join-check ====================

@router.get("/api/cluster/{server_id}/pre-join-check")
def pre_join_check(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Проверить ноду перед присоединением к кластеру:
    - Список существующих VM и LXC (их нужно бэкапировать и удалить)
    - Список хранилищ, поддерживающих content=backup
    """
    server = _get_server_or_404(db, server_id)
    client = _get_proxmox_client(server)

    nodes = client.get_nodes()
    node_name = nodes[0].get("node") if nodes else server.hostname

    vms = client.get_vms(node=node_name)
    containers = client.get_containers(node=node_name)
    backup_storages = client.get_backup_storages(node=node_name)

    vm_list = [
        {
            "vmid": v.get("vmid"),
            "name": v.get("name", f"vm-{v.get('vmid')}"),
            "status": v.get("status"),
            "type": "qemu",
            "maxdisk": v.get("maxdisk", 0),
            "mem": v.get("maxmem", 0),
        }
        for v in vms
    ]
    ct_list = [
        {
            "vmid": c.get("vmid"),
            "name": c.get("name", f"ct-{c.get('vmid')}"),
            "status": c.get("status"),
            "type": "lxc",
            "maxdisk": c.get("maxdisk", 0),
            "mem": c.get("maxmem", 0),
        }
        for c in containers
    ]

    guests = vm_list + ct_list
    storages = [
        {
            "storage": s.get("storage"),
            "type": s.get("type"),
            "avail": s.get("avail"),
            "total": s.get("total"),
        }
        for s in backup_storages
    ]

    return {
        "server_id": server_id,
        "server_name": server.name,
        "node_name": node_name,
        "guests_count": len(guests),
        "guests": guests,
        "backup_storages": storages,
        "ready_for_join": len(guests) == 0,
        "warning": (
            "После присоединения к кластеру /etc/pve будет перезаписан. "
            "Рекомендуется использовать shared storage (NFS, Ceph) — "
            "локальные хранилища ноды могут стать недоступными через кластерный интерфейс."
            if guests else None
        ),
    }


# ==================== POST /api/cluster/prepare-join ====================

@router.post("/api/cluster/prepare-join")
async def prepare_join(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Создать резервные копии всех VM/LXC на ноде через vzdump и удалить их,
    чтобы нода стала готова для присоединения к кластеру.

    Body:
        node_server_id (int): ID ноды в панели
        backup_storage (str): Хранилище для бэкапов (content=backup)
        compress (str, optional): zstd | lzo | gzip | 0 (default: zstd)

    Returns:
        backed_up: список успешно забэкапированных vmid
        deleted: список удалённых vmid
        errors: список ошибок (vmid + сообщение)
        ready_for_join: True если все VM удалены
    """
    node_server_id = body.get("node_server_id")
    backup_storage = body.get("backup_storage", "").strip()
    compress = body.get("compress", "zstd")

    if not node_server_id or not backup_storage:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать node_server_id и backup_storage"
        )

    server = _get_server_or_404(db, node_server_id)
    client = _get_proxmox_client(server)

    nodes = await _run_in_executor(client.get_nodes)
    node_name = nodes[0].get("node") if nodes else server.hostname

    # Collect all guests
    vms = await _run_in_executor(client.get_vms, node=node_name)
    containers = await _run_in_executor(client.get_containers, node=node_name)
    all_guests = [
        {"vmid": v.get("vmid"), "type": "qemu"} for v in vms
    ] + [
        {"vmid": c.get("vmid"), "type": "lxc"} for c in containers
    ]

    if not all_guests:
        return {
            "backed_up": [],
            "deleted": [],
            "errors": [],
            "ready_for_join": True,
            "message": "Нода не содержит VM/LXC — готова к join",
        }

    backed_up = []
    errors = []

    # Step 1: Create backups for all guests
    upid_map: dict = {}  # vmid -> upid
    for guest in all_guests:
        vmid = guest["vmid"]
        result = await _run_in_executor(
            client.vzdump_guest,
            node=node_name,
            vmid=vmid,
            storage=backup_storage,
            compress=compress,
        )
        if result.get("success"):
            upid_map[vmid] = result["upid"]
        else:
            errors.append({"vmid": vmid, "phase": "backup", "error": result.get("error")})

    # Step 2: Poll backup tasks until done (max 30 min, check every 5 sec)
    MAX_WAIT = 1800  # seconds
    POLL_INTERVAL = 5
    elapsed = 0

    while upid_map and elapsed < MAX_WAIT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        finished_vmids = []
        for vmid, upid in list(upid_map.items()):
            try:
                task_status = await _run_in_executor(client.proxmox.nodes(node_name).tasks(upid).status.get)
                if task_status.get("status") == "stopped":
                    finished_vmids.append(vmid)
                    if task_status.get("exitstatus") == "OK":
                        backed_up.append(vmid)
                    else:
                        errors.append({
                            "vmid": vmid,
                            "phase": "backup",
                            "error": f"vzdump завершился с ошибкой: {task_status.get('exitstatus')}",
                        })
            except Exception as e:
                logger.warning(f"Could not check task status for vmid {vmid}: {e}")

        for vmid in finished_vmids:
            del upid_map[vmid]

    # Timeout: treat remaining as errors
    for vmid in upid_map:
        errors.append({"vmid": vmid, "phase": "backup", "error": "Таймаут ожидания бэкапа (30 мин)"})

    # Step 3: Delete guests that were successfully backed up
    deleted = []
    vmids_to_delete = set(backed_up)

    for guest in all_guests:
        vmid = guest["vmid"]
        if vmid not in vmids_to_delete:
            continue

        try:
            if guest["type"] == "qemu":
                # Stop first if running
                qemu_info = await _run_in_executor(client.proxmox.nodes(node_name).qemu(vmid).status.current.get)
                if qemu_info.get("status") == "running":
                    await _run_in_executor(client.proxmox.nodes(node_name).qemu(vmid).status.stop.post)
                    await asyncio.sleep(3)
                await _run_in_executor(client.proxmox.nodes(node_name).qemu(vmid).delete)
            else:
                ct_info = await _run_in_executor(client.proxmox.nodes(node_name).lxc(vmid).status.current.get)
                if ct_info.get("status") == "running":
                    await _run_in_executor(client.proxmox.nodes(node_name).lxc(vmid).status.stop.post)
                    await asyncio.sleep(3)
                await _run_in_executor(client.proxmox.nodes(node_name).lxc(vmid).delete)

            deleted.append(vmid)
            logger.info(f"Deleted guest vmid {vmid} on {server.name} during prepare-join")

        except Exception as e:
            errors.append({"vmid": vmid, "phase": "delete", "error": str(e)})

    ready_for_join = len(errors) == 0 and len(deleted) == len(all_guests)

    logger.info(
        f"User {current_user.username} prepare-join on '{server.name}': "
        f"backed_up={backed_up}, deleted={deleted}, errors={[e['vmid'] for e in errors]}"
    )

    return {
        "backed_up": backed_up,
        "deleted": deleted,
        "errors": errors,
        "ready_for_join": ready_for_join,
        "message": (
            "Нода очищена и готова к присоединению к кластеру"
            if ready_for_join
            else f"Завершено с {len(errors)} ошибками. Исправьте их вручную перед join."
        ),
    }


# ==================== POST /api/cluster/join ====================

@router.post("/api/cluster/join")
async def join_cluster(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Присоединить standalone-ноду к существующему кластеру.

    Body:
        node_server_id (int): ID присоединяемой ноды в панели
        cluster_server_id (int): ID любой ноды существующего кластера
        rootpw (str): root-пароль присоединяемой ноды
        link0 (str, optional): Локальный IP присоединяемой ноды для corosync
        cluster_host (str, optional): Адрес кластерной ноды, к которому
            подключается присоединяемая нода (IP в corosync-сети). Если не
            задан — берётся ip_address кластерной ноды из панели, что может
            быть публичным FQDN и недоступно из выделенной приватной сети.

    Returns 409 если нода содержит VM/LXC (сначала вызовите prepare-join).
    """
    node_server_id = body.get("node_server_id")
    cluster_server_id = body.get("cluster_server_id")
    rootpw = body.get("rootpw", "").strip()
    link0 = (body.get("link0") or "").strip() or None
    cluster_host = (body.get("cluster_host") or "").strip() or None

    if not node_server_id or not cluster_server_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать node_server_id и cluster_server_id"
        )
    if not rootpw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать rootpw (root-пароль присоединяемой ноды)"
        )
    if node_server_id == cluster_server_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="node_server_id и cluster_server_id не могут совпадать"
        )

    node_server = _get_server_or_404(db, node_server_id)
    cluster_server = _get_server_or_404(db, cluster_server_id)

    if not cluster_server.cluster_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Сервер '{cluster_server.name}' не является кластерной нодой в панели. "
                   "Сначала создайте кластер или выберите ноду с cluster_name."
        )

    # --- Pre-check: ноды не должно содержать гестов ---
    node_client = _get_proxmox_client(node_server)
    nodes = await _run_in_executor(node_client.get_nodes)
    node_name = nodes[0].get("node") if nodes else node_server.hostname

    vms = await _run_in_executor(node_client.get_vms, node=node_name)
    containers = await _run_in_executor(node_client.get_containers, node=node_name)

    if vms or containers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GUESTS_FOUND",
                "message": (
                    f"Нода '{node_server.name}' содержит {len(vms)} VM и {len(containers)} LXC. "
                    "При присоединении к кластеру /etc/pve будет перезаписан и все конфиги потеряются. "
                    "Используйте 'prepare-join' для создания бэкапов и очистки ноды."
                ),
                "vms": [{"vmid": v.get("vmid"), "name": v.get("name")} for v in vms],
                "lxc": [{"vmid": c.get("vmid"), "name": c.get("name")} for c in containers],
            }
        )

    # --- Получить join-info с кластерной ноды ---
    cluster_client = _get_proxmox_client(cluster_server)
    join_info_result = await _run_in_executor(cluster_client.get_cluster_join_info)

    if not join_info_result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось получить join-информацию с '{cluster_server.name}': "
                   f"{join_info_result.get('error')}"
        )

    join_data = join_info_result.get("data", {})
    # Адрес, к которому реально подключается присоединяемая нода: явный
    # cluster_host (IP corosync-сети) в приоритете, иначе — ip_address из панели.
    connect_host = cluster_host or cluster_server.ip_address
    fingerprint = _extract_fingerprint(join_data, target_ip=connect_host) \
        or _extract_fingerprint(join_data, target_ip=cluster_server.ip_address)
    cluster_name = join_data.get("totem", {}).get("cluster_name") or cluster_server.cluster_name

    if not fingerprint:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось получить fingerprint кластера"
        )

    # --- Выполнить join на присоединяемой ноде (используем пароль из запроса) ---
    joining_client = _get_password_client(node_server, rootpw)
    result = await _run_in_executor(
        joining_client.join_cluster,
        cluster_host=connect_host,
        rootpw=rootpw,
        fingerprint=fingerprint,
        link0=link0,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ошибка присоединения к кластеру: {result.get('error')}"
        )

    node_upid = result.get("upid")

    # --- Подтвердить членство перед записью в БД ---
    # Задача join выполняется асинхронно и может упасть (напр. таймаут
    # подключения к кластерной ноде по недоступному адресу). При УСПЕШНОМ join
    # API присоединяемой ноды перезапускается, поэтому надёжнее спрашивать
    # кластерную ноду (node1): как только присоединяемая нода реально войдёт в
    # кластер, она появится в её списке нод. Окно ~40с — чтобы уложиться в
    # proxy_read_timeout (60с) фронтового nginx.
    joined_confirmed = False
    for _ in range(20):
        await asyncio.sleep(2)
        try:
            cluster_nodes = await _run_in_executor(cluster_client.get_nodes)
        except Exception:
            cluster_nodes = []
        names = {n.get("node") for n in cluster_nodes if isinstance(n, dict)}
        if node_name in names:
            joined_confirmed = True
            break

    if joined_confirmed:
        # Только теперь помечаем ноду как члена кластера в панели.
        node_server.cluster_name = cluster_name
        db.commit()
        logger.info(
            f"User {current_user.username} joined '{node_server.name}' "
            f"to cluster '{cluster_name}' (via '{cluster_server.name}')"
        )
        message = f"Нода '{node_server.name}' присоединена к кластеру '{cluster_name}'."
    else:
        # Членство не подтверждено — НЕ выставляем cluster_name, чтобы панель
        # не показывала ложное присоединение.
        logger.warning(
            f"Join of '{node_server.name}' to '{cluster_name}' not confirmed "
            f"within timeout (upid={node_upid})"
        )
        message = (
            f"Задача присоединения запущена (upid={node_upid}), но членство "
            f"'{node_server.name}' в кластере пока не подтверждено. Проверьте задачу; "
            "если нода не вошла в кластер — убедитесь, что кластерная нода доступна "
            "с присоединяемой по адресу corosync (link0), а не по публичному FQDN."
        )

    return {
        "success": True,
        "node_server_id": node_server_id,
        "node_name": node_server.name,
        "cluster_name": cluster_name if joined_confirmed else None,
        "confirmed": joined_confirmed,
        "upid": node_upid,
        "message": message,
    }


# ==================== GET /api/cluster/{server_id}/import-info ====================

@router.get("/api/cluster/{server_id}/import-info")
async def get_cluster_import_info(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Обнаружить кластер, сформированный на стороне Proxmox в обход панели
    (напр. через pvecm/веб-интерфейс Proxmox), и сопоставить его ноды с
    серверами, уже добавленными в панель — по IP/hostname из nodelist.
    Ничего не меняет, только читает.
    """
    server = _get_server_or_404(db, server_id)
    if server.cluster_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Сервер '{server.name}' уже привязан к кластеру '{server.cluster_name}' в панели"
        )

    client = _get_proxmox_client(server)

    is_cluster = await _run_in_executor(client.is_cluster)
    if not is_cluster:
        return {
            "server_id": server_id,
            "is_cluster": False,
            "cluster_name": None,
            "nodes": [],
            "message": "Нода не состоит в кластере Proxmox — нечего импортировать.",
        }

    join_info_result = await _run_in_executor(client.get_cluster_join_info)
    if not join_info_result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось получить информацию о кластере: {join_info_result.get('error')}"
        )

    data = join_info_result.get("data", {})
    cluster_name = data.get("totem", {}).get("cluster_name")
    nodelist = data.get("nodelist", [])

    all_servers = db.query(ProxmoxServer).all()

    def find_match(entry: dict):
        addr_candidates = {entry.get("pve_addr"), entry.get("ring0_addr")}
        addr_candidates.discard(None)
        for s in all_servers:
            if s.ip_address in addr_candidates:
                return s
        name = entry.get("name")
        if name:
            for s in all_servers:
                if s.hostname == name:
                    return s
        return None

    nodes = []
    for entry in nodelist:
        if not isinstance(entry, dict):
            continue
        match = find_match(entry)
        nodes.append({
            "name": entry.get("name"),
            "address": entry.get("pve_addr") or entry.get("ring0_addr"),
            "matched_server_id": match.id if match else None,
            "matched_server_name": match.name if match else None,
            "already_linked": bool(match and match.cluster_name == cluster_name),
            "linked_elsewhere": bool(match and match.cluster_name and match.cluster_name != cluster_name),
        })

    return {
        "server_id": server_id,
        "is_cluster": True,
        "cluster_name": cluster_name,
        "nodes": nodes,
    }


# ==================== POST /api/cluster/import ====================

@router.post("/api/cluster/import")
async def import_cluster(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Привязать существующие панельные записи серверов к реальному
    Proxmox-кластеру, обнаруженному на server_id (см. import-info).
    Только правит cluster_name в БД панели — никаких pvecm-операций не
    выполняется, реальный кластер не затрагивается.

    Body:
        server_id (int): нода-якорь, на которой обнаружен кластер
        server_ids (list[int]): панельные ID серверов для привязки к кластеру
            (каждый должен совпадать по IP или hostname с нодой из nodelist
            кластера — иначе привязка для него отклоняется)
    """
    server_id = body.get("server_id")
    server_ids = body.get("server_ids") or []

    if not server_id or not server_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать server_id и server_ids"
        )

    anchor = _get_server_or_404(db, server_id)
    client = _get_proxmox_client(anchor)

    if not await _run_in_executor(client.is_cluster):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Сервер '{anchor.name}' не состоит в кластере Proxmox"
        )

    join_info_result = await _run_in_executor(client.get_cluster_join_info)
    if not join_info_result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось получить информацию о кластере: {join_info_result.get('error')}"
        )

    data = join_info_result.get("data", {})
    cluster_name = data.get("totem", {}).get("cluster_name")
    if not cluster_name:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось определить имя кластера"
        )
    nodelist = data.get("nodelist", [])

    addr_by_node: set = set()
    name_by_node: set = set()
    for entry in nodelist:
        if not isinstance(entry, dict):
            continue
        for addr in (entry.get("pve_addr"), entry.get("ring0_addr")):
            if addr:
                addr_by_node.add(addr)
        if entry.get("name"):
            name_by_node.add(entry.get("name"))

    applied = []
    errors = []

    for sid in server_ids:
        s = db.query(ProxmoxServer).filter(ProxmoxServer.id == sid).first()
        if not s:
            errors.append({"server_id": sid, "error": "Сервер не найден"})
            continue
        if s.cluster_name == cluster_name:
            applied.append(sid)  # уже привязан — идемпотентно
            continue
        if s.cluster_name and s.cluster_name != cluster_name:
            errors.append({
                "server_id": sid,
                "error": f"'{s.name}' уже привязан к другому кластеру '{s.cluster_name}'",
            })
            continue
        if s.ip_address not in addr_by_node and s.hostname not in name_by_node:
            errors.append({
                "server_id": sid,
                "error": f"IP/hostname '{s.name}' не найден среди нод кластера '{cluster_name}' — привязка отклонена",
            })
            continue
        s.cluster_name = cluster_name
        applied.append(sid)

    db.commit()

    logger.info(
        f"User {current_user.username} imported existing cluster '{cluster_name}' "
        f"for panel servers {applied} (anchor='{anchor.name}', errors={len(errors)})"
    )

    return {
        "success": len(errors) == 0,
        "cluster_name": cluster_name,
        "applied": applied,
        "errors": errors,
        "message": (
            f"Кластер '{cluster_name}' импортирован для {len(applied)} серверов."
            if not errors
            else f"Импортировано {len(applied)}, ошибок: {len(errors)}."
        ),
    }


# ==================== POST /api/cluster/{server_id}/eject-node ====================

@router.post("/api/cluster/{server_id}/eject-node")
def eject_cluster_node(
    server_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("cluster:manage")),
):
    """
    Удалить ноду из кластера Proxmox.
    Вызывается на ЛЮБОЙ другой кластерной ноде (не на удаляемой).
    Удаляемая нода должна быть ВЫКЛЮЧЕНА.

    body:
        node_name (str): hostname ноды для удаления
        rootpw (str, optional): root-пароль для SSH fallback
        clear_panel_entry (bool, optional): Очистить cluster_name у панельной записи ноды (default: true)
        panel_only (bool, optional): Не трогать Proxmox, только отвязать запись в
            панели. Для случаев, когда нода на самом деле не в кластере (напр.
            прошлый join не завершился) или недоступна для delnode/SSH.
    """
    node_name = body.get("node_name", "").strip()
    rootpw = body.get("rootpw")
    clear_panel_entry = body.get("clear_panel_entry", True)
    panel_only = bool(body.get("panel_only", False))

    if not node_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Необходимо указать node_name"
        )

    # server_id — это нода, с которой выполняем команду (остающаяся в кластере)
    server = _get_server_or_404(db, server_id)

    # --- Режим "только панель": не обращаемся к Proxmox, чистим запись в БД ---
    if panel_only:
        ejected_server = db.query(ProxmoxServer).filter(
            ProxmoxServer.hostname == node_name,
            ProxmoxServer.cluster_name == server.cluster_name,
        ).first()
        if not ejected_server:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Панельная запись ноды '{node_name}' в кластере "
                       f"'{server.cluster_name}' не найдена."
            )
        ejected_server.cluster_name = None
        db.commit()
        logger.info(
            f"User {current_user.username} detached panel entry '{ejected_server.name}' "
            f"from cluster '{server.cluster_name}' (panel-only, no Proxmox op)"
        )
        return {
            "success": True,
            "ejected_node": node_name,
            "cluster_name": server.cluster_name,
            "method": "panel",
            "message": (
                f"Запись ноды '{node_name}' отвязана от кластера в панели. "
                "Proxmox не затрагивался."
            ),
        }

    # Build client — prefer password for SSH fallback availability
    if rootpw or server.password:
        client = _get_password_client(server, rootpw)
        client._password = rootpw or server.password  # expose for SSH fallback
    else:
        client = _get_proxmox_client(server)

    result = client.delete_cluster_node(node_name)

    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось удалить ноду '{node_name}': {result.get('error')}"
        )

    # Clear cluster_name on matching panel entry
    if clear_panel_entry:
        ejected_server = db.query(ProxmoxServer).filter(
            ProxmoxServer.hostname == node_name,
            ProxmoxServer.cluster_name == server.cluster_name,
        ).first()
        if ejected_server:
            ejected_server.cluster_name = None
            db.commit()
            logger.info(
                f"Cleared cluster_name for panel server '{ejected_server.name}' (ejected node)"
            )

    logger.info(
        f"User {current_user.username} ejected node '{node_name}' "
        f"from cluster '{server.cluster_name}' via server '{server.name}' "
        f"(method: {result.get('method', 'rest')})"
    )

    return {
        "success": True,
        "ejected_node": node_name,
        "cluster_name": server.cluster_name,
        "method": result.get("method", "rest"),
        "message": (
            f"Нода '{node_name}' удалена из кластера. "
            "Убедитесь, что нода выключена и не подключена к той же кластерной сети."
        ),
    }
