from fastapi import APIRouter, Depends, Request, HTTPException, Query, Form, status, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from loguru import logger
from typing import Annotated, List, Optional
from pydantic import Field
from starlette.requests import HTTPConnection
from fastapi import WebSocketException
import re
import secrets
import ssl
import threading
import time
from types import SimpleNamespace
import asyncio
import httpx
import websockets

from ...db import get_db
from ...models import ProxmoxServer, VMInstance, User, IPAMAllocation, IPAMNetwork, VMSnapshotArchive
from ...schemas import ProxmoxServerCreate, ProxmoxServerUpdate, ProxmoxServerResponse
from ...proxmox import ProxmoxClient, get_proxmox_resources
from ...auth import get_current_user, PermissionChecker, require_permission, check_permission
from ...logging_service import LoggingService
from ...ipam_service import IPAMService


# ==================== OS name derivation ====================

# Known LXC distro slugs → human-readable display name.
_LXC_DISTRO_NAMES = {
    "debian": "Debian",
    "ubuntu": "Ubuntu",
    "alpine": "Alpine",
    "fedora": "Fedora",
    "centos": "CentOS",
    "rockylinux": "Rocky Linux",
    "rocky": "Rocky Linux",
    "almalinux": "AlmaLinux",
    "alma": "AlmaLinux",
    "archlinux": "Arch Linux",
    "arch": "Arch Linux",
    "opensuse": "openSUSE",
    "gentoo": "Gentoo",
    "devuan": "Devuan",
    "nixos": "NixOS",
    "oracle": "Oracle Linux",
    "oraclelinux": "Oracle Linux",
    "kali": "Kali Linux",
    "mint": "Linux Mint",
    "slackware": "Slackware",
    "void": "Void Linux",
}


def pretty_os_from_template(template_name: Optional[str]) -> Optional[str]:
    """Derive a friendly OS name (e.g. "Debian 13", "Ubuntu 24.04") from an
    LXC CT template volid/filename such as
    ``local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst``.

    Returns ``None`` if the input doesn't look like a CT template path, so the
    caller can fall back to its previous value.
    """
    if not template_name:
        return None

    # Only vztmpl-style paths carry a distro/version in the filename. VM clone
    # templates keep their own descriptive names — leave those untouched.
    if "vztmpl" not in template_name and "/" not in template_name and ".tar" not in template_name:
        return None

    # Strip storage prefix and directory: keep just the filename.
    base = template_name.rsplit("/", 1)[-1]
    # Head is everything before the first underscore, e.g.
    # "debian-13-standard", "ubuntu-24.04-standard", "centos-9-stream-default".
    head = base.split("_", 1)[0]
    tokens = head.split("-")
    if not tokens:
        return None

    distro = _LXC_DISTRO_NAMES.get(tokens[0].lower())
    if not distro:
        return None

    # First token that looks like a version number (digits, optionally dotted).
    version = next((t for t in tokens[1:] if re.match(r"^\d+(\.\d+)*$", t)), None)
    # Preserve edition qualifiers like "stream" (CentOS Stream) when present.
    edition = "Stream" if "stream" in (t.lower() for t in tokens[1:]) else None

    parts = [distro]
    if version:
        parts.append(version)
    if edition:
        parts.append(edition)
    return " ".join(parts)


# ==================== Helper Functions for User Isolation ====================

def can_view_all_instances(current_user: User) -> bool:
    """
    Whether the user may see/manage *every* instance, not just their own.

    Only administrators — or roles that explicitly grant ``vm:manage`` — get the
    global view. Every other authenticated user (including users with no role,
    custom roles, or the stock ``user`` role) is scoped to instances they own.

    Note: ``vm:view`` is deliberately NOT treated as a global view permission.
    The stock ``user`` role carries ``vm:view`` simply so the instances feature
    is usable at all; granting it would let any tenant see every VM.
    """
    if getattr(current_user, 'is_admin', False):
        return True
    role = current_user.role
    if role and role.name == 'admin':
        return True
    # Same evaluation as PermissionChecker("vm:manage") — a raw dict lookup
    # missed ``vm:*``, so such a role passed vm:manage-gated endpoints while
    # being treated as a tenant everywhere else.
    return check_permission(current_user, 'vm:manage')


# Stand-in request without headers: visibility checks that must not be
# narrowed by the client-chosen X-Active-Workspace header.
_NO_WORKSPACE_HEADER = SimpleNamespace(headers={})


def pve_authority(server: ProxmoxServer) -> str:
    """``host:port`` of the server's Proxmox API (as ProxmoxClient.from_server).

    Console endpoints built their URLs with a hard-coded ``:8006`` and failed
    on servers registered with another port.
    """
    host = server.ip_address or getattr(server, "hostname", "") or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6
    return f"{host}:{server.port or 8006}"


# Console sessions need a PVE auth ticket (PVEAuthCookie) of the panel's
# Proxmox account — usually root@pam. It used to be returned to the browser,
# handing every tenant with vm:console a root ticket for the whole cluster.
# Now the ticket stays here and the browser gets an opaque, single-use handle
# bound to the user and guest. In-memory: the backend runs one uvicorn worker.
_PVE_TICKET_TTL = 120.0
_pve_tickets: dict = {}
_pve_tickets_lock = threading.Lock()


def stash_pve_ticket(ticket: Optional[str], user_id: int, server_id: int, vmid: int) -> Optional[str]:
    if not ticket:
        return None
    handle = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _pve_tickets_lock:
        for key in [k for k, v in _pve_tickets.items() if v[4] < now]:
            del _pve_tickets[key]
        _pve_tickets[handle] = (ticket, user_id, server_id, vmid, now + _PVE_TICKET_TTL)
    return handle


def claim_pve_ticket(handle: Optional[str], user_id: int, server_id: int, vmid: int) -> Optional[str]:
    """Redeem a handle from :func:`stash_pve_ticket` (once), or None."""
    if not handle:
        return None
    with _pve_tickets_lock:
        entry = _pve_tickets.pop(handle, None)
    if not entry or entry[4] < time.monotonic():
        return None
    if entry[1:4] != (user_id, server_id, vmid):
        return None
    return entry[0]


def get_visible_server_ids(
    request: Request, db: Session, current_user: User
) -> Optional[set]:
    """
    Set of Proxmox server IDs the user may see, or ``None`` for "no restriction".

    Two filters combine here:

    * **Access** — privileged users (admin/moderator) see every server; everyone
      else sees servers assigned to them directly OR reachable through a
      workspace they belong to. Adding a user to a workspace is therefore
      enough — no separate direct assignment is required.
    * **Active workspace** — the ``X-Active-Workspace`` header narrows the result
      further, for privileged and non-privileged users alike.

    ``None`` means "everything" (privileged user with no active workspace) and is
    deliberately distinct from an empty set, which means "nothing visible".
    Callers must treat the two differently — that distinction is exactly what
    ``/api/cluster/topology`` used to get wrong by skipping this check entirely.
    """
    from ...api.workspaces import get_workspace_server_ids
    from ...models import WorkspaceUser, WorkspaceServer

    ws_filter = get_workspace_server_ids(request, db, current_user)

    is_privileged = current_user.is_admin or (
        current_user.role and current_user.role.name in ('admin', 'moderator')
    )
    if is_privileged:
        return None if ws_filter is None else set(ws_filter)

    assigned_ids = {s.id for s in current_user.assigned_servers}

    user_ws_ids = {
        r.workspace_id for r in
        db.query(WorkspaceUser.workspace_id).filter(WorkspaceUser.user_id == current_user.id).all()
    }
    ws_server_ids = set()
    if user_ws_ids:
        ws_server_ids = {
            r.server_id for r in
            db.query(WorkspaceServer.server_id).filter(
                WorkspaceServer.workspace_id.in_(user_ws_ids)
            ).all()
        }

    effective_ids = assigned_ids | ws_server_ids
    if ws_filter is not None:
        effective_ids &= set(ws_filter)
    return effective_ids


def enforce_server_visibility(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Router dependency: 404 for {server_id} routes outside the user's servers.

    Permission checks alone (node:view, firewall:view, storage:view … are in
    the stock ``user`` role) let a tenant read node syslog, firewall rules,
    disks or pools of every server in the panel. Privileged users are not
    narrowed; the active-workspace header is ignored here (it is a UI
    filter, not an access boundary). Guests the user owns stay reachable
    even on a server that is not assigned to them.
    """
    raw = request.path_params.get("server_id")
    if raw is None:
        return
    try:
        server_id = int(raw)
    except (TypeError, ValueError):
        return
    if current_user.is_admin or (
        current_user.role and current_user.role.name in ('admin', 'moderator')
    ):
        return
    vmid = request.path_params.get("vmid")
    if vmid is not None:
        try:
            if check_vm_access(db, current_user, server_id, int(vmid)):
                return
        except (TypeError, ValueError):
            pass
    visible = get_visible_server_ids(_NO_WORKSPACE_HEADER, db, current_user)
    if visible is not None and server_id not in visible:
        raise HTTPException(status_code=404, detail="Proxmox server not found")


def check_vm_access(db: Session, current_user: User, server_id: int, vmid: int) -> bool:
    """
    Check if user has access to a specific VM instance.

    Admins (and roles granting ``vm:manage``) can access all VMs. Everyone else
    can only access VMs where they are the owner.

    Returns True if access is allowed, False otherwise.
    """
    if can_view_all_instances(current_user):
        return True

    instance = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None)
    ).first()

    return bool(instance and instance.owner_id == current_user.id)


def require_server_visible(db: Session, current_user: User, server_id: int) -> None:
    """404 unless the server is visible to the user.

    For endpoints taking ``server_id`` in the request BODY: the router-level
    :func:`enforce_server_visibility` only sees path parameters.
    """
    visible = get_visible_server_ids(_NO_WORKSPACE_HEADER, db, current_user)
    if visible is not None and server_id not in visible:
        raise HTTPException(status_code=404, detail="Proxmox server not found")


# Volume ids a create request may reference. Anything else lets the request
# point at another guest's disk or backup (pct create from a vzdump archive is
# a restore; a disk volume can be attached as a CD-ROM).
ISO_VOLID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*:iso/[^/\s]+$")
VZTMPL_VOLID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*:vztmpl/[^/\s]+$")


# ── Identifiers that end up in Proxmox API URL paths ──────────────────────
# proxmoxer splits path arguments on '/' and joins them unescaped, so a node,
# storage or other segment carrying '/', '#' or '?' rewrites the request path:
# an access check made for one guest would act on another resource.
NODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$"
PVE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
_NODE_RE = re.compile(NODE_PATTERN)
_PVE_ID_RE = re.compile(PVE_ID_PATTERN)
_UPID_RE = re.compile(r"^UPID:[A-Za-z0-9.-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[\w.-]*:[^:/?#\s]*:[^:/?#\s]+:?$")
_UNSAFE_SEGMENT_RE = re.compile(r"[/#?\\\x00-\x1f]")
# Path parameters declared with the :path converter (or CIDRs) may hold '/'.
_SLASH_PATH_PARAMS = {"subnet_cidr"}
_SUBNET_RE = re.compile(r"^[A-Za-z0-9_.:-]+(/\d{1,3})?$")
_NODE_QUERY_KEYS = ("node", "target_node", "source_node")

PveNode = Annotated[str, Field(pattern=NODE_PATTERN)]
PveId = Annotated[str, Field(pattern=PVE_ID_PATTERN)]


def require_node_name(value, field: str = "node") -> None:
    """400 unless ``value`` is a plain node name (for dict/form bodies)."""
    if value is not None and value != "" and not (isinstance(value, str) and _NODE_RE.match(value)):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def require_pve_id(value, field: str) -> None:
    """400 unless ``value`` is a plain storage/pool/bridge-like id."""
    if value is not None and value != "" and not (isinstance(value, str) and _PVE_ID_RE.match(value)):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def _identifier_error(conn: HTTPConnection) -> Optional[str]:
    for key, value in conn.path_params.items():
        if not isinstance(value, str):
            continue
        if key == "upid":
            if not _UPID_RE.match(value):
                return key
        elif key in _SLASH_PATH_PARAMS:
            if not _SUBNET_RE.match(value):
                return key
        elif _UNSAFE_SEGMENT_RE.search(value):
            return key
    for key in _NODE_QUERY_KEYS:
        value = conn.query_params.get(key)
        if value and not _NODE_RE.match(value):
            return key
    storage = conn.query_params.get("storage")  # e.g. backup listing: storage(...) path
    if storage and not _PVE_ID_RE.match(storage):
        return "storage"
    upid = conn.query_params.get("upid")
    if upid and not _UPID_RE.match(upid):
        return "upid"
    return None


def validate_pve_identifiers(conn: HTTPConnection) -> None:
    """Router dependency for every /proxmox route (HTTP and WebSocket)."""
    bad = _identifier_error(conn)
    if bad is None:
        return
    if conn.scope.get("type") == "websocket":
        raise WebSocketException(code=1008, reason=f"Invalid {bad}")
    raise HTTPException(status_code=400, detail=f"Invalid {bad}")


def require_vm_access(db: Session, current_user: User, server_id: int, vmid: int):
    """
    Require user to have access to VM, raise 403 if not.
    """
    if not check_vm_access(db, current_user, server_id, vmid):
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this virtual machine"
        )


def get_owned_vmids(db: Session, current_user: User, server_id: Optional[int] = None) -> set:
    """
    Set of VMIDs the user owns (optionally scoped to a single Proxmox server).

    Used to filter live Proxmox resource listings (which carry no owner
    information) down to instances the tenant actually owns. Only meaningful for
    non-privileged users — callers should skip filtering when
    ``can_view_all_instances`` is True.
    """
    query = db.query(VMInstance.vmid).filter(
        VMInstance.owner_id == current_user.id,
        VMInstance.deleted_at.is_(None),
    )
    if server_id is not None:
        query = query.filter(VMInstance.server_id == server_id)
    return {row[0] for row in query.all()}


def _get_proxmox_client(server: ProxmoxServer) -> ProxmoxClient:
    """Build a ProxmoxClient from a ProxmoxServer model (password or token auth).

    Raises HTTPException 503, если у сервера нет валидных кредов: раньше
    from_server молча создавал клиент с proxmox=None, и все методы возвращали
    пустые списки — пользователь видел «пустой сервер» вместо ошибки настройки.
    """
    has_password = bool(getattr(server, "use_password", False) and getattr(server, "password", ""))
    has_token = bool(getattr(server, "api_token_name", "") and getattr(server, "api_token_value", ""))
    if not (has_password or has_token):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Server '{server.name}' has no valid Proxmox credentials configured "
                f"(API token or password). Edit the server settings."
            ),
        )
    return ProxmoxClient.from_server(server)


def _extract_fingerprint(join_data: dict, target_ip: str = None) -> Optional[str]:
    """
    Достать SHA-256 fingerprint из ответа GET /cluster/config/join.

    В Proxmox API нет поля `fingerprint` на верхнем уровне — оно лежит в
    nodelist[].pve_fp у каждой ноды. Выбираем запись целевой кластерной ноды
    по pve_addr/ring0_addr == target_ip, с фолбэком на preferred_node и на
    первую ноду с непустым pve_fp.
    """
    if not isinstance(join_data, dict):
        return None

    # Совместимость: если провайдер всё же вернул fingerprint сверху — берём его.
    top = join_data.get("fingerprint")
    if top:
        return top

    nodelist = join_data.get("nodelist") or []

    def _fp(entry: dict) -> Optional[str]:
        return entry.get("pve_fp") if isinstance(entry, dict) else None

    # 1) По IP целевой ноды
    if target_ip:
        for entry in nodelist:
            if not isinstance(entry, dict):
                continue
            if target_ip in (entry.get("pve_addr"), entry.get("ring0_addr")) and _fp(entry):
                return entry["pve_fp"]

    # 2) По preferred_node
    preferred = join_data.get("preferred_node")
    if preferred:
        for entry in nodelist:
            if isinstance(entry, dict) and entry.get("name") == preferred and _fp(entry):
                return entry["pve_fp"]

    # 3) Первая нода с непустым pve_fp
    for entry in nodelist:
        if _fp(entry):
            return entry["pve_fp"]

    return None


def _build_remote_endpoint(target_server: ProxmoxServer, target_node: str) -> str:
    """
    Собрать `target-endpoint` строку для PVE `remote_migrate` API.

    Формат: apitoken=PVEAPIToken={user}!{token}={secret},host={ip}:{port},fingerprint={fp}
    Требует API-токен у целевого сервера (remote_migrate не принимает пароль).
    """
    has_token = bool(getattr(target_server, "api_token_name", "") and getattr(target_server, "api_token_value", ""))
    if not has_token or getattr(target_server, "use_password", False):
        raise ValueError(
            f"Server '{target_server.name}' must be configured with an API token "
            f"(not password) for remote migration. Edit the target server settings."
        )

    client = _get_proxmox_client(target_server)
    try:
        join_data = client.proxmox.cluster.config.join.get()
    except Exception as e:
        raise ValueError(f"Failed to fetch target cluster fingerprint from '{target_server.name}': {e}") from e

    fingerprint = _extract_fingerprint(join_data, target_ip=target_server.ip_address)
    if not fingerprint:
        raise ValueError(f"Could not determine SSL fingerprint of target server '{target_server.name}'")

    token = f"{target_server.api_user}!{target_server.api_token_name}={target_server.api_token_value}"
    return (
        f"apitoken=PVEAPIToken={token},"
        f"host={target_server.ip_address}:{target_server.port},"
        f"fingerprint={fingerprint}"
    )


def get_next_vmid(db: Session, server_id: int) -> int:
    """Get next available VMID from Proxmox server (sequential)"""

    # Get server from DB
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        # Create Proxmox client and get next VMID from Proxmox API
        client = _get_proxmox_client(server)

        # Get next VMID from Proxmox (it returns sequential IDs)
        vmid = client.get_next_vmid()
        if vmid:
            return vmid
    except Exception as e:
        logger.warning(f"Could not get VMID from Proxmox, falling back to DB: {e}")

    # Fallback: get from DB if Proxmox not available
    used_vmids = set(
        row[0] for row in db.query(VMInstance.vmid)
        .filter(VMInstance.server_id == server_id, VMInstance.deleted_at.is_(None))
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

    # Проверяем, существует ли уже активная запись
    existing = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.is_(None)
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
        # Владельца проставляем только когда он передан явно: часть вызовов
        # (например, сохранение конфига перед удалением) владельца не знает,
        # и None не должен обезличивать инстанс.
        # Без этой строки владелец, выбранный при создании, терялся: фоновый
        # sync_vm_cache идёт каждые 10с и заводит запись с owner_id=NULL ещё
        # во время клонирования, поэтому деплой почти всегда попадает сюда,
        # а не в ветку создания новой записи.
        if owner_id is not None:
            existing.owner_id = owner_id
        existing.updated_at = func.now()
        db.commit()
        db.refresh(existing)
        return existing

    # Проверяем, есть ли удалённая запись с тем же vmid (soft-deleted)
    deleted_existing = db.query(VMInstance).filter(
        VMInstance.server_id == server_id,
        VMInstance.vmid == vmid,
        VMInstance.deleted_at.isnot(None)
    ).first()

    if deleted_existing:
        # Восстанавливаем и обновляем удалённую запись
        deleted_existing.node = node
        deleted_existing.vm_type = vm_type
        deleted_existing.name = name
        deleted_existing.cores = cores
        deleted_existing.memory = memory
        deleted_existing.disk_size = disk_size
        deleted_existing.ip_address = ip_address
        deleted_existing.ip_prefix = ip_prefix
        deleted_existing.gateway = gateway
        deleted_existing.nameserver = nameserver
        deleted_existing.cloud_init_user = cloud_init_user
        deleted_existing.cloud_init_password = cloud_init_password
        deleted_existing.ssh_keys = ssh_keys
        deleted_existing.template_id = template_id
        deleted_existing.template_name = template_name
        deleted_existing.description = description
        deleted_existing.extra_config = extra_config
        if owner_id is not None:
            deleted_existing.owner_id = owner_id
        deleted_existing.status = 'unknown'
        deleted_existing.deleted_at = None  # Восстанавливаем запись
        deleted_existing.updated_at = func.now()
        db.commit()
        db.refresh(deleted_existing)
        return deleted_existing

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
        VMInstance.deleted_at.is_(None)
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
