"""
Background monitoring worker for generating notifications
Uses APScheduler for periodic tasks
"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from loguru import logger


from ..config import utcnow, make_tz_aware


# qmrestore / vma extract emit lines like "progress 47% (read ...)".
# Some restores instead print bare "47% ..." markers. Capture either form.
_PROGRESS_RE = re.compile(r"(?:progress\s+)?(\d{1,3})\s*%")


def _bytes_to_mb(value: Optional[int]) -> Optional[int]:
    """Перевести байты Proxmox (maxmem) в мегабайты для хранения в БД."""
    if not value:
        return None
    return round(value / (1024 * 1024))


def _bytes_to_gb(value: Optional[int]) -> Optional[int]:
    """Перевести байты Proxmox (maxdisk) в гигабайты для хранения в БД."""
    if not value:
        return None
    return round(value / (1024 * 1024 * 1024))


def parse_task_progress(log_text: Optional[str]) -> Optional[int]:
    """Extract the latest restore/backup progress percentage from a task log.

    Returns the last percentage seen (0-100), or None when the log has none.
    """
    if not log_text:
        return None
    matches = _PROGRESS_RE.findall(log_text)
    if not matches:
        return None
    # The log is appended in order, so the last match is the most recent progress.
    pct = int(matches[-1])
    return max(0, min(100, pct))


try:
    from backend.app.db import SessionLocal
    from backend.app.models import User, ProxmoxServer, PanelSettings, VMInstance, IPAMAllocation, Notification, ProxmoxTask
    from backend.app.services.notification_service import NotificationService
    from backend.app.logging_service import LoggingService
    from backend.app.proxmox import ProxmoxClient
    from backend.app.schemas import NotificationCreate
    from backend.app.i18n import t
    from backend.app.websocket_manager import broadcast_task_update
except ImportError:
    from app.db import SessionLocal
    from app.models import User, ProxmoxServer, PanelSettings, VMInstance, IPAMAllocation, Notification, ProxmoxTask
    from app.services.notification_service import NotificationService
    from app.logging_service import LoggingService
    from app.proxmox import ProxmoxClient
    from app.schemas import NotificationCreate
    from app.i18n import t
    from app.websocket_manager import broadcast_task_update


def get_panel_language(db) -> str:
    """Get panel language from settings"""
    setting = db.query(PanelSettings).filter(PanelSettings.key == "language").first()
    return setting.value if setting else "ru"


def run_async(coro):
    """Run async coroutine from sync context"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If loop is running, create task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop, create new one
        return asyncio.run(coro)


class ProbeFailure(Exception):
    """Неудачная проверка доступности сервера с указанием причины.

    error_kind: 'auth' — API ответил 401, 'connection' — не отвечает.
    """

    def __init__(self, message: str, error_kind: str = None):
        super().__init__(message)
        self.error_kind = error_kind or 'connection'


class MonitoringWorker:
    """Background worker for monitoring and generating notifications"""
    
    # Сколько подряд неудачных опросов статуса UPID терпим, прежде чем пометить
    # задачу failed. Транзитный сетевой сбой не должен «убивать» живую задачу.
    UPID_FAIL_THRESHOLD = 3

    # Как часто перепроверять IP гостя у Proxmox. Синк идёт каждые 10 секунд,
    # а адрес меняется редко — без троттлинга это сотни лишних запросов в минуту.
    IP_DETECT_TTL = 300

    def __init__(self):
        self.last_vm_states: Dict[str, str] = {}  # vm_id -> status
        self.last_resource_alerts: Dict[str, float] = {}  # resource_id -> last_alert_time
        self.last_server_states: Dict[int, bool] = {}  # server_id -> is_online
        self.last_server_alerts: Dict[int, float] = {}  # server_id -> last_alert_time
        self.upid_fail_counts: Dict[int, int] = {}  # ProxmoxTask.id -> consecutive fetch failures
        self.last_ip_detect: Dict[tuple, float] = {}  # (server_id, vmid) -> monotonic time
    
    def _create_proxmox_client(self, server: ProxmoxServer) -> ProxmoxClient:
        """Create ProxmoxClient from server model, raising if auth is missing."""
        has_password = server.use_password and server.password
        has_token = server.api_token_name and server.api_token_value
        if not (has_password or has_token):
            raise ValueError(f"Server {server.name} has no valid authentication configured")
        return ProxmoxClient.from_server(server)
    
    def _notify_server_offline(self, db, server: ProxmoxServer, error: str, users: List[User],
                               error_kind: str = None):
        """Send notification that server went offline"""
        server_id = server.id

        # Check cooldown (don't spam alerts)
        last_alert = self.last_server_alerts.get(server_id)
        if last_alert:
            # 10 minute cooldown between alerts for same server
            if (datetime.now().timestamp() - last_alert) < 600:
                return

        # Update server status in DB
        server.is_online = False
        server.last_error = error
        server.last_error_kind = error_kind or 'connection'
        server.last_check = utcnow()
        db.commit()
        
        # Get panel language
        lang = get_panel_language(db)
        
        title = t("notify_server_offline_title", lang, server_name=server.name)
        message = t("notify_server_offline_message", lang, 
                   server_name=server.name, 
                   hostname=server.hostname,
                   error=error[:200])
        data = {
            "server_id": server.id,
            "server_name": server.name,
            "hostname": server.hostname,
            "error": error
        }
        
        # Send notification to all users (through all channels: in-app, email, telegram)
        for user in users:
            try:
                # Use async create_and_send for multi-channel delivery
                run_async(
                    NotificationService.create_and_send(
                        db=db,
                        user_id=user.id,
                        notification_type="system",
                        level="critical",
                        title=title,
                        message=message,
                        data=data,
                        source="monitoring",
                        source_id=str(server.id)
                    )
                )
            except Exception as e:
                logger.error(f"Failed to send notification for user {user.id}: {e}")
        
        self.last_server_alerts[server_id] = datetime.now().timestamp()
        self.last_server_states[server_id] = False
        logger.warning(f"Server {server.name} is OFFLINE: {error}")
    
    def _notify_server_online(self, db, server: ProxmoxServer, users: List[User]):
        """Send notification that server came back online"""
        server_id = server.id
        was_offline = self.last_server_states.get(server_id) == False
        
        # Update server status in DB
        server.is_online = True
        server.last_error = None
        server.last_check = utcnow()
        db.commit()
        
        # Only notify if server was previously offline
        if was_offline:
            # Get panel language
            lang = get_panel_language(db)
            
            title = t("notify_server_online_title", lang, server_name=server.name)
            message = t("notify_server_online_message", lang,
                       server_name=server.name,
                       hostname=server.hostname)
            data = {
                "server_id": server.id,
                "server_name": server.name,
                "hostname": server.hostname
            }
            
            for user in users:
                try:
                    run_async(
                        NotificationService.create_and_send(
                            db=db,
                            user_id=user.id,
                            notification_type="system",
                            level="success",
                            title=title,
                            message=message,
                            data=data,
                            source="monitoring",
                            source_id=str(server.id),
                            force_send=True  # Always send server recovery notifications
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to send notification for user {user.id}: {e}")
            
            logger.info(f"Server {server.name} is back ONLINE")
        
        self.last_server_states[server_id] = True
    
    def run_server_availability_check(self):
        """
        Dedicated server availability check - runs every 30 seconds.
        Sends notifications about offline servers with 10-minute repeat interval.
        Immediately notifies when server comes back online.
        """
        db = SessionLocal()
        try:
            # Get all active users with admin role for server notifications
            users = db.query(User).filter(
                User.is_active == True,
                User.is_admin == True
            ).all()
            
            if not users:
                logger.debug("[SERVER CHECK] No admin users, skipping server availability check")
                return
            
            # Get all Proxmox servers
            servers = db.query(ProxmoxServer).all()
            
            if not servers:
                logger.debug("[SERVER CHECK] No Proxmox servers configured")
                return
            
            logger.debug(f"[SERVER CHECK] Checking availability of {len(servers)} servers")
            
            for server in servers:
                try:
                    client = self._create_proxmox_client(server)

                    # Quick connectivity check - just test if API responds
                    ok, error_kind, check_error = client.check_connection()
                    if ok:
                        # Server is online
                        was_offline = self.last_server_states.get(server.id) == False

                        # Update server status in DB
                        if not server.is_online or was_offline:
                            server.is_online = True
                            server.last_error = None
                            server.last_error_kind = None
                            server.last_check = utcnow()
                            db.commit()

                        # Notify immediately if server recovered
                        if was_offline:
                            self._notify_server_recovered(db, server, users)

                        self.last_server_states[server.id] = True
                    else:
                        raise ProbeFailure(check_error or "API not responding", error_kind)

                except ValueError as e:
                    # Configuration error - log but don't mark as offline
                    logger.warning(f"[SERVER CHECK] Server {server.name} configuration error: {e}")

                except Exception as e:
                    # Server is offline
                    error_msg = str(e)
                    was_online = self.last_server_states.get(server.id, True)

                    # Update server status in DB
                    server.is_online = False
                    server.last_error = error_msg[:500]
                    server.last_error_kind = getattr(e, 'error_kind', None) or 'connection'
                    server.last_check = utcnow()
                    db.commit()
                    
                    # Check if we should send notification
                    last_alert = self.last_server_alerts.get(server.id, 0)
                    now = datetime.now().timestamp()
                    time_since_last_alert = now - last_alert
                    
                    # Send notification if:
                    # 1. Server just went offline (was_online)
                    # 2. OR 10 minutes passed since last notification
                    should_notify = was_online or (time_since_last_alert >= 600)
                    
                    if should_notify:
                        self._send_server_offline_notification(db, server, error_msg, users)
                        self.last_server_alerts[server.id] = now
                        
                        if was_online:
                            reason = 'AUTH FAILED' if server.last_error_kind == 'auth' else 'OFFLINE'
                            logger.warning(f"[SERVER CHECK] Server {server.name} went {reason}: {error_msg}")
                        else:
                            logger.warning(f"[SERVER CHECK] Server {server.name} still OFFLINE (repeat notification)")
                    
                    self.last_server_states[server.id] = False
                    
        except Exception as e:
            logger.error(f"[SERVER CHECK] Critical error: {e}", exc_info=True)
        finally:
            db.close()
    
    def _send_server_offline_notification(self, db, server: ProxmoxServer, error: str, users: List[User]):
        """Send server offline notification to all admin users"""
        lang = get_panel_language(db)
        
        title = t("notify_server_offline_title", lang, server_name=server.name)
        message = t("notify_server_offline_message", lang, 
                   server_name=server.name, 
                   hostname=server.hostname or server.ip_address,
                   error=error[:200])
        data = {
            "server_id": server.id,
            "server_name": server.name,
            "hostname": server.hostname or server.ip_address,
            "error": error
        }
        
        for user in users:
            try:
                run_async(
                    NotificationService.create_and_send(
                        db=db,
                        user_id=user.id,
                        notification_type="system",
                        level="critical",
                        title=title,
                        message=message,
                        data=data,
                        source="server_monitor",
                        source_id=str(server.id)
                    )
                )
            except Exception as e:
                logger.error(f"Failed to send offline notification to user {user.id}: {e}")
    
    def _notify_server_recovered(self, db, server: ProxmoxServer, users: List[User]):
        """Send immediate notification that server came back online"""
        lang = get_panel_language(db)
        
        title = t("notify_server_online_title", lang, server_name=server.name)
        message = t("notify_server_online_message", lang,
                   server_name=server.name,
                   hostname=server.hostname or server.ip_address)
        data = {
            "server_id": server.id,
            "server_name": server.name,
            "hostname": server.hostname or server.ip_address
        }
        
        for user in users:
            try:
                run_async(
                    NotificationService.create_and_send(
                        db=db,
                        user_id=user.id,
                        notification_type="system",
                        level="success",
                        title=title,
                        message=message,
                        data=data,
                        source="server_monitor",
                        source_id=str(server.id),
                        force_send=True  # Always send recovery notifications immediately
                    )
                )
            except Exception as e:
                logger.error(f"Failed to send recovery notification to user {user.id}: {e}")
        
        logger.info(f"[SERVER CHECK] Server {server.name} is back ONLINE - notification sent")
        # Clear alert cooldown so next offline will notify immediately
        self.last_server_alerts.pop(server.id, None)
        
    def run_vm_status_monitoring(self):
        """Monitor VM status changes and server connectivity"""
        db = SessionLocal()
        try:
            # Get all active users
            users = db.query(User).filter(User.is_active == True).all()
            
            if not users:
                logger.debug("[MONITORING] No active users, skipping VM monitoring")
                return
            
            # Get all Proxmox servers
            servers = db.query(ProxmoxServer).all()
            
            if not servers:
                logger.debug("[MONITORING] No Proxmox servers configured, skipping monitoring")
                return
            
            logger.debug(f"[MONITORING] Starting VM status check for {len(servers)} servers")
            
            for server in servers:
                try:
                    client = self._create_proxmox_client(server)
                    
                    # Explicitly test connection - this will return False if offline
                    ok, error_kind, check_error = client.check_connection()
                    if not ok:
                        raise ProbeFailure(
                            check_error or f"Cannot connect to Proxmox server at {server.hostname}",
                            error_kind,
                        )
                    
                    # Server is online - notify if it was offline before
                    self._notify_server_online(db, server, users)
                    
                    # Get all VMs
                    vms = client.get_vms()
                    logger.debug(f"[MONITORING] Server {server.name}: {len(vms)} VMs found")
                    
                    for vm in vms:
                        vm_key = f"{server.id}:{vm['vmid']}"
                        current_status = vm['status']
                        previous_status = self.last_vm_states.get(vm_key)
                        
                        # Detect status change
                        if previous_status and previous_status != current_status:
                            # Log the status change
                            logger.info(f"[VM STATUS CHANGE] Server: {server.name} | VM: {vm['name']} (ID: {vm['vmid']}) | Status: {previous_status} -> {current_status}")
                            
                            # Determine notification level
                            level = "info"
                            if current_status == "stopped" and previous_status == "running":
                                level = "warning"
                                logger.warning(f"[VM STOPPED] Server: {server.name} | VM: {vm['name']} (ID: {vm['vmid']}) went from running to stopped")
                            elif current_status == "running" and previous_status == "stopped":
                                level = "success"
                                logger.info(f"[VM STARTED] Server: {server.name} | VM: {vm['name']} (ID: {vm['vmid']}) is now running")
                            
                            # Admins and the guest's owner only: other tenants
                            # must not learn about (or be spammed by) foreign VMs.
                            for user in self._vm_recipients(db, users, server, vm['vmid']):
                                NotificationService.notify_vm_status(
                                    db,
                                    user_id=user.id,
                                    vm_id=vm['vmid'],
                                    vm_name=vm['name'],
                                    status=current_status,
                                    level=level,
                                    server_name=server.name,
                                    old_status=previous_status
                                )
                        
                        self.last_vm_states[vm_key] = current_status
                
                except ValueError as e:
                    # Configuration error - don't treat as offline
                    logger.warning(f"[MONITORING] Server {server.name} configuration error: {e}")
                        
                except Exception as e:
                    # Connection error - server is offline
                    error_msg = str(e)
                    logger.error(f"[MONITORING] Server {server.name} connection failed: {error_msg}")
                    self._notify_server_offline(db, server, error_msg, users,
                                                error_kind=getattr(e, 'error_kind', None))
                    
        except Exception as e:
            logger.error(f"[MONITORING] Critical error in VM status monitoring: {e}", exc_info=True)
        finally:
            db.close()
    
    @staticmethod
    def _vm_recipients(db, users: List[User], server: ProxmoxServer, vmid: int) -> List[User]:
        """Users to notify about a guest: global-view users plus its owner."""
        from ..api.proxmox._helpers import can_view_all_instances
        server_ids = {server.id}
        if getattr(server, 'cluster_name', None):
            # The cache keys clustered guests by one member server's id.
            server_ids |= {sid for (sid,) in db.query(ProxmoxServer.id).filter(
                ProxmoxServer.cluster_name == server.cluster_name)}
        owner_ids = {oid for (oid,) in db.query(VMInstance.owner_id).filter(
            VMInstance.vmid == vmid,
            VMInstance.server_id.in_(server_ids),
            VMInstance.deleted_at.is_(None),
            VMInstance.owner_id.isnot(None),
        )}
        return [u for u in users if u.id in owner_ids or can_view_all_instances(u)]

    def run_resource_monitoring(self):
        """Monitor resource usage (CPU, RAM, Disk)"""
        db = SessionLocal()
        try:
            users = db.query(User).filter(User.is_active == True).all()
            servers = db.query(ProxmoxServer).all()
            
            for server in servers:
                try:
                    client = self._create_proxmox_client(server)
                    
                    # Get all VMs with stats
                    vms = client.get_vms()
                    
                    for vm in vms:
                        if vm['status'] != 'running':
                            continue
                        
                        vm_key = f"{server.id}:{vm['vmid']}"
                        
                        try:
                            # The /nodes/{n}/qemu listing already carries cpu/mem/
                            # disk — only fall back to a per-VM call when it doesn't
                            # (was one extra API request per running VM per minute).
                            stats = vm if vm.get('cpu') is not None else (
                                client.get_vm_stats(vm['node'], vm['vmid']) or {})
                            
                            # Check CPU usage
                            if 'cpu' in stats and stats['cpu'] is not None:
                                cpu_percent = stats['cpu'] * 100
                                if cpu_percent > 80:
                                    alert_key = f"{vm_key}:cpu"
                                    if self._should_send_alert(alert_key):
                                        for user in self._vm_recipients(db, users, server, vm['vmid']):
                                            NotificationService.notify_resource_alert(
                                                db,
                                                user_id=user.id,
                                                resource_type="CPU",
                                                resource_name=vm['name'],
                                                usage_percent=cpu_percent,
                                                threshold=80
                                            )
                                        self.last_resource_alerts[alert_key] = datetime.now().timestamp()
                            
                            # Check RAM usage
                            if 'mem' in stats and 'maxmem' in stats:
                                if stats['maxmem'] > 0:
                                    ram_percent = (stats['mem'] / stats['maxmem']) * 100
                                    if ram_percent > 85:
                                        alert_key = f"{vm_key}:ram"
                                        if self._should_send_alert(alert_key):
                                            for user in self._vm_recipients(db, users, server, vm['vmid']):
                                                NotificationService.notify_resource_alert(
                                                    db,
                                                    user_id=user.id,
                                                    resource_type="RAM",
                                                    resource_name=vm['name'],
                                                    usage_percent=ram_percent,
                                                    threshold=85
                                                )
                                            self.last_resource_alerts[alert_key] = datetime.now().timestamp()
                            
                            # Check Disk usage
                            if 'disk' in stats and 'maxdisk' in stats:
                                if stats['maxdisk'] > 0:
                                    disk_percent = (stats['disk'] / stats['maxdisk']) * 100
                                    if disk_percent > 90:
                                        alert_key = f"{vm_key}:disk"
                                        if self._should_send_alert(alert_key):
                                            for user in self._vm_recipients(db, users, server, vm['vmid']):
                                                NotificationService.notify_resource_alert(
                                                    db,
                                                    user_id=user.id,
                                                    resource_type="Disk",
                                                    resource_name=vm['name'],
                                                    usage_percent=disk_percent,
                                                    threshold=90
                                                )
                                            self.last_resource_alerts[alert_key] = datetime.now().timestamp()
                        
                        except Exception as e:
                            logger.error(f"Error getting stats for VM {vm['vmid']}: {e}")
                    
                except Exception as e:
                    logger.error(f"Error monitoring resources for server {server.name}: {e}")
            
        except Exception as e:
            logger.error(f"Error in resource monitoring: {e}")
        finally:
            db.close()
    
    def run_cleanup_expired(self):
        """Clean up expired notifications and old audit logs"""
        db = SessionLocal()
        try:
            count = NotificationService.cleanup_expired(db)
            if count > 0:
                logger.info(f"Cleaned up {count} expired notifications")
        except Exception as e:
            logger.error(f"Error cleaning up notifications: {e}")
        finally:
            db.close()

        db = SessionLocal()
        try:
            LoggingService.cleanup_old_logs(db, days=30)
        except Exception as e:
            logger.error(f"Error cleaning up old audit logs: {e}")
        finally:
            db.close()
    
    def sync_vm_cache(self):
        """
        Sync VM/container data from all Proxmox servers to local database cache.
        This runs periodically to keep the cache fresh.
        
        Deduplication logic:
        - Cluster servers (detected by shared nodes): dedup by vmid within cluster
        - Standalone servers: each server has its own vmid namespace
        - Same vmid on different standalone servers = different VMs
        """
        db = SessionLocal()
        try:
            servers = db.query(ProxmoxServer).filter(ProxmoxServer.is_online == True).all()
            
            if not servers:
                logger.debug("[VM SYNC] No online servers, skipping sync")
                return
            
            logger.debug(f"[VM SYNC] Starting VM cache sync for {len(servers)} servers")
            
            # Load IPAM allocations for IP lookup
            ipam_allocations = db.query(IPAMAllocation).filter(
                IPAMAllocation.status.in_(['allocated', 'reserved'])
            ).all()
            
            # Build IPAM lookups.
            # Только по идентификатору гостя: сопоставление по имени давало чужой
            # адрес одноимённым гостям на разных серверах (два разных "wireguard").
            # У гостя может быть несколько адресов — в кэш инстанса пишем
            # основной (is_primary); без флага берём самую раннюю запись.
            ipam_by_server_vmid = {}  # (server_id, vmid) -> allocation

            for alloc in ipam_allocations:
                if alloc.proxmox_server_id and alloc.proxmox_vmid:
                    key = (alloc.proxmox_server_id, alloc.proxmox_vmid)
                    current = ipam_by_server_vmid.get(key)
                    if current is None or (alloc.is_primary and not current.is_primary):
                        ipam_by_server_vmid[key] = alloc

            # Detect clusters: servers that see multiple nodes are part of a cluster
            # Group servers by their node set
            server_nodes = {}  # server_id -> set of node names
            cluster_map = {}  # server_id -> cluster_id (first server_id in cluster)
            clients_by_server = {}  # server_id -> ProxmoxClient, reused by the collect loop
            
            for server in servers:
                try:
                    client = self._create_proxmox_client(server)
                    clients_by_server[server.id] = client
                    if client.is_connected():
                        nodes = client.get_nodes()
                        node_names = frozenset([n.get('node', '') for n in nodes if n.get('node')])
                        server_nodes[server.id] = node_names
                except Exception as e:
                    logger.debug(f"[VM SYNC] Could not get nodes for {server.name}: {e}")
                    server_nodes[server.id] = frozenset()
            
            # Find clusters: servers with same node set (more than 1 node) are in same cluster
            node_set_to_servers = {}  # frozenset of nodes -> list of server_ids
            for server_id, nodes in server_nodes.items():
                if len(nodes) > 1:  # Only consider multi-node as cluster
                    if nodes not in node_set_to_servers:
                        node_set_to_servers[nodes] = []
                    node_set_to_servers[nodes].append(server_id)
            
            # Assign cluster_id (first server in cluster)
            for nodes, server_ids in node_set_to_servers.items():
                cluster_id = min(server_ids)  # Use smallest server_id as cluster identifier
                for sid in server_ids:
                    cluster_map[sid] = cluster_id
                logger.debug(f"[VM SYNC] Cluster detected: servers {server_ids} share nodes {list(nodes)})")
            
            # Track seen VMs per cluster/server for dedup
            seen_in_cluster = {}  # cluster_id -> set of vmid
            seen_standalone = {}  # server_id -> set of vmid
            
            all_vms_data = []  # List of (server_id, vm_data, vm_type)
            server_clients = {}  # server_id (or cluster_id) -> ProxmoxClient
            sync_time = utcnow()
            
            # Collect all VMs from all servers with proper dedup
            for server in servers:
                try:
                    client = clients_by_server.get(server.id) or self._create_proxmox_client(server)
                    
                    if not client.is_connected():
                        logger.warning(f"[VM SYNC] Server {server.name} not connected, skipping")
                        continue
                    
                    # Get all VMs and containers efficiently in one request
                    resources = client.get_cluster_resources(type_='vm')
                    vms = [res for res in resources if res.get('type') == 'qemu']
                    containers = [res for res in resources if res.get('type') == 'lxc']
                    
                    cluster_id = cluster_map.get(server.id)
                    
                    # Determine which dedup set to use
                    if cluster_id:
                        if cluster_id not in seen_in_cluster:
                            seen_in_cluster[cluster_id] = set()
                        seen_set = seen_in_cluster[cluster_id]
                        use_server_id = cluster_id  # Use cluster_id for all VMs in cluster
                        # Map cluster_id -> client for IP detection later
                        if cluster_id not in server_clients:
                            server_clients[cluster_id] = client
                    else:
                        if server.id not in seen_standalone:
                            seen_standalone[server.id] = set()
                        seen_set = seen_standalone[server.id]
                        use_server_id = server.id
                        server_clients[server.id] = client
                    
                    server_vm_count = 0
                    server_ct_count = 0
                    
                    for vm in vms:
                        if vm.get('template'):
                            continue
                        vmid = vm.get('vmid')
                        if vmid in seen_set:
                            continue
                        seen_set.add(vmid)
                        all_vms_data.append((use_server_id, vm, 'qemu'))
                        server_vm_count += 1
                    
                    for ct in containers:
                        if ct.get('template'):
                            continue
                        vmid = ct.get('vmid')
                        if vmid in seen_set:
                            continue
                        seen_set.add(vmid)
                        all_vms_data.append((use_server_id, ct, 'lxc'))
                        server_ct_count += 1
                    
                    logger.debug(f"[VM SYNC] Server {server.name}: collected {server_vm_count} VMs, {server_ct_count} containers")
                        
                except Exception as e:
                    logger.error(f"[VM SYNC] Error fetching from server {server.name}: {e}")
            
            logger.debug(f"[VM SYNC] Collected {len(all_vms_data)} unique VMs/containers")
            
            # Track all seen (server_id, vmid) pairs for cleanup
            all_seen = set()
            # Track status changes for WebSocket broadcast
            status_changes = []  # list of (server_id, vmid, node, old_status, new_status)
            # Track newly-created VMs for WebSocket broadcast
            created_vms = []  # list of (server_id, vmid, node, name, vm_type)

            # One query for every cached row of the synced servers instead of a
            # SELECT per guest every 10 seconds.
            sync_server_ids = {sid for sid, _, _ in all_vms_data}
            existing_rows = {}
            if sync_server_ids:
                for row in db.query(VMInstance).filter(VMInstance.server_id.in_(sync_server_ids)):
                    existing_rows.setdefault((row.server_id, row.vmid), row)
            
            # Upsert all VMs to database
            for server_id, vm_data, vm_type in all_vms_data:
                try:
                    vmid = vm_data.get('vmid')
                    vm_name = vm_data.get('name', f"{'VM' if vm_type == 'qemu' else 'CT'}-{vmid}")
                    node_name = vm_data.get('node', '')
                    
                    all_seen.add((server_id, vmid))
                    
                    # Get IP from IPAM
                    ipam_alloc = ipam_by_server_vmid.get((server_id, vmid))
                    ip_address = ipam_alloc.ip_address if ipam_alloc else None

                    # If no IPAM IP, try to detect from Proxmox (LXC config or QEMU guest agent).
                    # Троттлим: адрес гостя меняется куда реже, чем идёт синк.
                    detect_key = (server_id, vmid)
                    detect_due = (time.monotonic() - self.last_ip_detect.get(detect_key, 0.0)) >= self.IP_DETECT_TTL
                    if not ip_address and detect_due:
                        self.last_ip_detect[detect_key] = time.monotonic()
                        try:
                            px_client = server_clients.get(server_id)
                            if px_client and node_name:
                                if vm_type == 'lxc':
                                    # Статика из netN + живые адреса запущенного
                                    # контейнера (DHCP виден только там)
                                    for iface in px_client.get_container_interfaces(
                                        node_name, vmid,
                                        include_live=vm_data.get('status') == 'running',
                                    ):
                                        for ip_info in iface.get('ips') or []:
                                            if ip_info.get('type') != 'ipv4':
                                                continue
                                            addr = ip_info.get('address', '')
                                            if addr:
                                                ip_address = addr
                                                break
                                        if ip_address:
                                            break
                                elif vm_type == 'qemu' and vm_data.get('status') == 'running':
                                    result = px_client.proxmox.nodes(node_name).qemu(vmid).agent('network-get-interfaces').get()
                                    if result and 'result' in result:
                                        for iface in result['result']:
                                            if iface.get('name') == 'lo':
                                                continue
                                            for ip_info in (iface.get('ip-addresses') or []):
                                                if ip_info.get('ip-address-type') == 'ipv4':
                                                    addr = ip_info.get('ip-address', '')
                                                    if addr and not addr.startswith('127.'):
                                                        ip_address = addr
                                                        break
                                            if ip_address:
                                                break
                        except Exception:
                            pass
                    
                    # Get OS type
                    if vm_type == 'qemu':
                        os_type = vm_data.get('ostype', 'QEMU/KVM')
                    else:
                        os_type = vm_data.get('ostype', 'Linux')
                        if os_type:
                            os_type = os_type.capitalize()
                    
                    new_status = vm_data.get('status', 'unknown')
                    
                    # Find existing by (server_id, vmid)
                    existing = existing_rows.get((server_id, vmid))
                    
                    if existing:
                        # Detect status change for WS broadcast
                        if existing.status != new_status:
                            status_changes.append((server_id, vmid, node_name, existing.status, new_status))
                        # If this VM was previously soft-deleted but reappeared,
                        # treat it as a new VM for broadcast purposes
                        if existing.deleted_at is not None:
                            created_vms.append((server_id, vmid, node_name, vm_name, vm_type))
                        # Update existing entry
                        existing.name = vm_name
                        existing.node = node_name
                        existing.status = new_status
                        existing.cores = vm_data.get('cpus', vm_data.get('maxcpu'))
                        # maxmem/maxdisk приходят из Proxmox в БАЙТАХ, а колонки
                        # memory/disk_size по соглашению хранятся в MB/GB
                        # (так пишут create/resize). Конвертируем, иначе синк
                        # затирает корректные значения сырыми байтами.
                        existing.memory = _bytes_to_mb(vm_data.get('maxmem'))
                        existing.disk_size = _bytes_to_gb(vm_data.get('maxdisk'))
                        existing.os_type = os_type
                        existing.vm_type = vm_type
                        existing.is_template = bool(vm_data.get('template'))
                        # Only overwrite ip_address if we have a value;
                        # IPAM and detected IPs take priority; otherwise preserve existing
                        if ip_address:
                            existing.ip_address = ip_address
                        # last_sync_at alone changing would UPDATE every row on
                        # every 10 s tick; refresh it at most once a minute.
                        last = make_tz_aware(existing.last_sync_at)
                        if last is None or (sync_time - last).total_seconds() >= 60 or db.is_modified(existing):
                            existing.last_sync_at = sync_time
                        existing.deleted_at = None
                    else:
                        # Create new entry
                        new_vm = VMInstance(
                            server_id=server_id,
                            vmid=vmid,
                            node=node_name,
                            vm_type=vm_type,
                            name=vm_name,
                            status=new_status,
                            cores=vm_data.get('cpus', vm_data.get('maxcpu')),
                            # см. комментарий выше: Proxmox отдаёт байты, храним MB/GB
                            memory=_bytes_to_mb(vm_data.get('maxmem')),
                            disk_size=_bytes_to_gb(vm_data.get('maxdisk')),
                            os_type=os_type,
                            is_template=bool(vm_data.get('template')),
                            ip_address=ip_address,
                            last_sync_at=sync_time
                        )
                        db.add(new_vm)
                        existing_rows[(server_id, vmid)] = new_vm
                        created_vms.append((server_id, vmid, node_name, vm_name, vm_type))
                        
                except Exception as e:
                    logger.error(f"[VM SYNC] Error processing VM {vmid}: {e}")
            
            # Commit all changes
            try:
                db.commit()
            except Exception as e:
                logger.error(f"[VM SYNC] Error committing changes: {e}")
                db.rollback()
                return
            
            # Не копим троттлинг-метки по гостям, которых больше нет
            self.last_ip_detect = {
                key: seen_at for key, seen_at in self.last_ip_detect.items() if key in all_seen
            }

            # Mark VMs that no longer exist as deleted
            # Get all active VMs from synced servers
            synced_server_ids = set(server.id for server in servers)
            # Also include cluster IDs for proper matching
            for server in servers:
                if server.id in cluster_map:
                    synced_server_ids.add(cluster_map[server.id])
            
            active_vms = db.query(VMInstance).filter(
                VMInstance.deleted_at.is_(None),
                VMInstance.server_id.in_(synced_server_ids)
            ).all()
            
            deleted_count = 0
            released_ips = 0
            deleted_broadcasts = []  # list of (server_id, vmid, node, name, vm_type)
            for vm in active_vms:
                if (vm.server_id, vm.vmid) not in all_seen:
                    vm.deleted_at = sync_time
                    deleted_count += 1
                    deleted_broadcasts.append((vm.server_id, vm.vmid, vm.node, vm.name, vm.vm_type))
                    logger.debug(f"[VM SYNC] Marked VM {vm.name} (server={vm.server_id}, vmid={vm.vmid}) as deleted")
                    
                    # Release IPAM allocation for deleted VM
                    try:
                        from app.ipam_service import IPAMService
                        ipam = IPAMService(db)
                        released, released_ip = ipam.release_ip_by_vmid(
                            proxmox_server_id=vm.server_id,
                            proxmox_vmid=vm.vmid,
                            released_by="system",
                            reason=f"VM {vm.name} ({vm.vmid}) no longer exists on Proxmox"
                        )
                        if released:
                            released_ips += 1
                            logger.debug(f"[VM SYNC] Released IPAM IP {released_ip} for deleted VM {vm.name}")
                    except Exception as e:
                        logger.warning(f"[VM SYNC] Failed to release IPAM for VM {vm.vmid}: {e}")
            
            db.commit()
            logger.debug(f"[VM SYNC] Cache sync completed. Processed {len(all_vms_data)} VMs/containers, marked {deleted_count} as deleted, released {released_ips} IPs")
            
            # Broadcast status changes to all connected WebSocket clients
            if status_changes:
                try:
                    from app.websocket_manager import ws_manager, run_async_safe
                    for srv_id, vmid, node_name, old_status, new_status in status_changes:
                        logger.info(f"[VM SYNC] Status change: server={srv_id} vmid={vmid} {old_status} -> {new_status}")
                        run_async_safe(ws_manager.broadcast({
                            "type": "vm_status_update",
                            "server_id": srv_id,
                            "vmid": vmid,
                            "node": node_name,
                            "status": new_status,
                        }))
                except Exception as _we:
                    logger.warning(f"[VM SYNC] Failed to broadcast status changes: {_we}")

            # Broadcast deletions so connected clients can update their lists in real time
            if deleted_broadcasts:
                try:
                    from app.websocket_manager import ws_manager, run_async_safe
                    for srv_id, vmid, node_name, vm_name, vm_type in deleted_broadcasts:
                        logger.info(f"[VM SYNC] VM deleted: server={srv_id} vmid={vmid} name={vm_name}")
                        # Broadcast goes to every connected user: no guest
                        # names (other tenants' data), ids are enough to refetch.
                        run_async_safe(ws_manager.broadcast({
                            "type": "vm_deleted",
                            "server_id": srv_id,
                            "vmid": vmid,
                            "node": node_name,
                            "vm_type": vm_type,
                        }))
                except Exception as _we:
                    logger.warning(f"[VM SYNC] Failed to broadcast deletions: {_we}")

            # Broadcast newly-created VMs so clients can refetch and show them
            if created_vms:
                try:
                    from app.websocket_manager import ws_manager, run_async_safe
                    for srv_id, vmid, node_name, vm_name, vm_type in created_vms:
                        logger.info(f"[VM SYNC] VM created: server={srv_id} vmid={vmid} name={vm_name}")
                        run_async_safe(ws_manager.broadcast({
                            "type": "vm_created",
                            "server_id": srv_id,
                            "vmid": vmid,
                            "node": node_name,
                            "vm_type": vm_type,
                        }))
                except Exception as _we:
                    logger.warning(f"[VM SYNC] Failed to broadcast creations: {_we}")
            
        except Exception as e:
            logger.error(f"[VM SYNC] Critical error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    def run_upid_task_sync(self) -> None:
        """
        Poll Proxmox API for status of all 'running' ProxmoxTask records.
        Updates DB status + log, then broadcasts WS update.
        Runs every 5 seconds via APScheduler.
        """
        db = SessionLocal()
        try:
            running_tasks = (
                db.query(ProxmoxTask)
                .filter(ProxmoxTask.status == "running")
                .all()
            )
            if not running_tasks:
                return

            logger.debug(f"[UPID SYNC] Checking {len(running_tasks)} running UPID task(s)")

            for task in running_tasks:
                if not task.server_id or not task.node:
                    continue
                try:
                    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == task.server_id).first()
                    if not server or not server.is_online:
                        continue

                    client = self._create_proxmox_client(server)
                    if not client.is_connected():
                        continue

                    # --- status -----------------------------------------------
                    # Транзитный сбой (сеть, перезагрузка ноды) не должен
                    # помечать живую задачу как failed: считаем ПОДРЯД идущие
                    # неудачи и финализируем только после UPID_FAIL_THRESHOLD.
                    try:
                        proxmox_status = client.get_task_status(task.node, task.upid, raise_on_error=True)
                        fetch_error = None
                    except Exception as fe:
                        proxmox_status = None
                        fetch_error = str(fe)

                    if not proxmox_status:
                        fails = self.upid_fail_counts.get(task.id, 0) + 1
                        self.upid_fail_counts[task.id] = fails
                        if fails < self.UPID_FAIL_THRESHOLD:
                            logger.debug(
                                f"[UPID SYNC] Task #{task.id}: status fetch failed "
                                f"({fails}/{self.UPID_FAIL_THRESHOLD}), will retry: {fetch_error}"
                            )
                            continue
                        logger.warning(
                            f"[UPID SYNC] Cannot fetch status for task #{task.id} "
                            f"(UPID={task.upid[:30]}...) after {fails} attempts — marking as failed: {fetch_error}"
                        )
                        task.status = "failed"
                        task.exit_status = (fetch_error or "Invalid UPID")[:200]
                        task.completed_at = utcnow()
                        db.commit()
                        self.upid_fail_counts.pop(task.id, None)
                        try:
                            broadcast_task_update(task.user_id, "task_update", task.to_dict())
                        except Exception:
                            pass
                        continue

                    # Успешный опрос — сбрасываем счётчик неудач
                    self.upid_fail_counts.pop(task.id, None)

                    pve_status = proxmox_status.get("status", "running")

                    # --- log --------------------------------------------------
                    try:
                        log_entries = client.get_task_log(task.node, task.upid, 0, 500) or []
                        if isinstance(log_entries, list):
                            task.log_text = "\n".join(
                                entry.get("t", "") if isinstance(entry, dict) else str(entry)
                                for entry in log_entries
                            )
                            # Extract a live progress % from the log (qmrestore etc.)
                            parsed = parse_task_progress(task.log_text)
                            if parsed is not None:
                                task.progress = parsed
                    except Exception:
                        pass

                    # --- finalise if stopped ----------------------------------
                    if pve_status == "stopped":
                        exit_status = proxmox_status.get("exitstatus", "") if proxmox_status else ""
                        task.exit_status = exit_status
                        task.status = "completed" if exit_status == "OK" else "failed"
                        task.completed_at = utcnow()
                        # A successfully finished task is 100% done even if the last
                        # log line didn't print a percentage.
                        if exit_status == "OK" and task.progress is not None:
                            task.progress = 100
                        logger.info(f"[UPID SYNC] Task #{task.id} finished: {task.status} ({exit_status})")

                    db.commit()

                    # --- WS broadcast -----------------------------------------
                    try:
                        broadcast_task_update(task.user_id, "task_update", task.to_dict())
                    except Exception as _wb:
                        logger.debug(f"[UPID SYNC] WS broadcast skipped: {_wb}")

                except Exception as e:
                    logger.error(f"[UPID SYNC] Error syncing task #{task.id} ({task.upid[:30]}): {e}")

        except Exception as e:
            logger.error(f"[UPID SYNC] Critical error: {e}", exc_info=True)
        finally:
            db.close()

    def _should_send_alert(self, alert_key: str, cooldown_minutes: int = 30) -> bool:
        """
        Check if alert should be sent (rate limiting)
        
        Args:
            alert_key: Unique alert identifier
            cooldown_minutes: Minimum time between same alerts
        
        Returns:
            True if alert should be sent
        """
        last_alert = self.last_resource_alerts.get(alert_key)
        if not last_alert:
            return True
        
        cooldown_seconds = cooldown_minutes * 60
        return (datetime.now().timestamp() - last_alert) > cooldown_seconds

    def check_for_panel_updates(self):
        """
        Check for panel updates and notify admin users if a new version is available.
        Runs periodically (every 6 hours) via scheduler.
        """
        try:
            from app.services.update_service import check_for_updates
        except ImportError:
            from backend.app.services.update_service import check_for_updates
        
        db = SessionLocal()
        try:
            logger.debug("[UPDATE CHECK] Checking for panel updates...")
            
            # Run async check_for_updates
            result = run_async(check_for_updates())
            
            if result.get("error") or result.get("disabled"):
                logger.debug(f"[UPDATE CHECK] Skipped: {result.get('error', 'disabled')}")
                return
            
            if not result.get("update_available"):
                logger.debug(f"[UPDATE CHECK] No updates available. Current: {result.get('current_version')}")
                return
            
            current_version = result.get("current_version", "unknown")
            new_version = result.get("latest_version", "unknown")
            changelog = result.get("changelog")
            
            logger.info(f"[UPDATE CHECK] New version available: {new_version} (current: {current_version})")
            
            # Get all admin users to notify
            users = db.query(User).filter(
                User.is_active == True,
                User.is_admin == True
            ).all()
            
            if not users:
                logger.debug("[UPDATE CHECK] No admin users to notify")
                return
            
            # Check if we already notified about this version
            for user in users:
                # Check if notification already exists for this user and version
                existing = db.query(Notification).filter(
                    Notification.user_id == user.id,
                    Notification.source_id == f"update_{new_version}",
                    Notification.type == "update"
                ).first()
                
                if existing:
                    logger.debug(f"[UPDATE CHECK] User {user.id} already notified about version {new_version}")
                    continue
                
                # Create notification for this user
                try:
                    NotificationService.notify_update_available(
                        db=db,
                        user_id=user.id,
                        current_version=current_version,
                        new_version=new_version,
                        changelog=changelog
                    )
                    logger.debug(f"[UPDATE CHECK] Notified user {user.username} about update to {new_version}")
                except Exception as e:
                    logger.error(f"[UPDATE CHECK] Failed to notify user {user.id}: {e}")
            
            db.commit()
            logger.debug(f"[UPDATE CHECK] Completed. Notified {len(users)} admin users about version {new_version}")
            
        except Exception as e:
            logger.error(f"[UPDATE CHECK] Error checking for updates: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    def cleanup_server_state(self, server_id: int):
        """Remove all in-memory state for a deleted server to prevent stale data"""
        self.last_server_states.pop(server_id, None)
        self.last_server_alerts.pop(server_id, None)
        # Remove all VM state entries belonging to this server
        stale_keys = [k for k in self.last_vm_states if k.startswith(f"{server_id}:")]
        for key in stale_keys:
            del self.last_vm_states[key]
        stale_alert_keys = [k for k in self.last_resource_alerts if k.startswith(f"{server_id}:")]
        for key in stale_alert_keys:
            del self.last_resource_alerts[key]
        logger.info(f"[MONITORING] Cleared in-memory state for deleted server {server_id}")


# Global worker instance
monitoring_worker = MonitoringWorker()


def start_monitoring_worker():
    """Start background monitoring tasks using APScheduler"""
    from datetime import datetime as _dt
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    
    # coalesce: a job that fell behind (slow Proxmox, GC pause) runs once instead
    # of firing every missed tick back-to-back; max_instances=1 keeps a slow
    # sync from overlapping itself.
    scheduler = BackgroundScheduler(job_defaults={
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': 30,
    })
    
    # Server availability check - every 30 seconds
    # Sends notifications on offline, repeats every 10 minutes, notifies immediately on recovery
    scheduler.add_job(
        monitoring_worker.run_server_availability_check,
        trigger=IntervalTrigger(seconds=30),
        id='server_availability_check',
        name='Check server availability',
        replace_existing=True
    )
    
    # VM status monitoring - every 30 seconds
    scheduler.add_job(
        monitoring_worker.run_vm_status_monitoring,
        trigger=IntervalTrigger(seconds=30),
        id='vm_status_monitoring',
        name='Monitor VM status changes',
        replace_existing=True
    )
    
    # Resource monitoring - every 60 seconds
    scheduler.add_job(
        monitoring_worker.run_resource_monitoring,
        trigger=IntervalTrigger(seconds=60),
        id='resource_monitoring',
        name='Monitor resource usage',
        replace_existing=True
    )
    
    # Update check - every 6 hours
    scheduler.add_job(
        monitoring_worker.check_for_panel_updates,
        trigger=IntervalTrigger(hours=6),
        id='update_check',
        name='Check for panel updates',
        replace_existing=True
    )
    
    # VM cache sync - every 10 seconds
    scheduler.add_job(
        monitoring_worker.sync_vm_cache,
        trigger=IntervalTrigger(seconds=10),
        id='vm_cache_sync',
        name='Sync VM cache from Proxmox',
        replace_existing=True,
        # First run right away, on the scheduler thread — see below.
        next_run_time=_dt.now(),
    )
    
    # Task queue processing - every 5 seconds
    try:
        from app.services.task_queue_service import process_task_queue, recover_interrupted_tasks
        # Восстановление задач, прерванных рестартом: без этого зависшая
        # 'running'-задача навсегда блокирует обработку очереди.
        try:
            recovered = recover_interrupted_tasks()
            if recovered:
                logger.info(f"Recovered {recovered} interrupted bulk task(s) after restart")
        except Exception as e:
            logger.warning(f"Interrupted task recovery failed: {e}")
        scheduler.add_job(
            process_task_queue,
            trigger=IntervalTrigger(seconds=5),
            id='task_queue_processing',
            name='Process bulk operation queue',
            replace_existing=True
        )
        logger.info("Task queue processor registered")
    except ImportError as e:
        logger.warning(f"Task queue service not available: {e}")

    # Proxmox UPID task sync - every 5 seconds
    scheduler.add_job(
        monitoring_worker.run_upid_task_sync,
        trigger=IntervalTrigger(seconds=5),
        id='upid_task_sync',
        name='Sync Proxmox UPID task statuses',
        replace_existing=True
    )
    
    # Cleanup expired notifications - every 6 hours
    scheduler.add_job(
        monitoring_worker.run_cleanup_expired,
        trigger=IntervalTrigger(hours=6),
        id='cleanup_notifications',
        name='Clean up expired notifications',
        replace_existing=True
    )

    # Cleanup expired Proxmox connection cache - every hour
    try:
        from app.proxmox import cleanup_expired_connections
        scheduler.add_job(
            cleanup_expired_connections,
            trigger=IntervalTrigger(hours=1),
            id='proxmox_cache_cleanup',
            name='Cleanup expired Proxmox connections',
            replace_existing=True
        )
        logger.info("Proxmox cache cleanup task registered")
    except ImportError as e:
        logger.warning(f"Proxmox cache cleanup not available: {e}")
    
    # The initial VM cache sync used to run inline here, i.e. inside the
    # FastAPI lifespan: with a slow or unreachable Proxmox node the panel did
    # not accept requests (nor pass its healthcheck) until every server timed
    # out. It now runs immediately on the scheduler (next_run_time above).
    
    # Run initial update check on startup (delayed by 60 seconds to let the app fully start)
    def delayed_update_check():
        import time
        time.sleep(60)
        try:
            logger.info("Running initial update check...")
            monitoring_worker.check_for_panel_updates()
        except Exception as e:
            logger.warning(f"Initial update check failed: {e}")
    
    import threading
    threading.Thread(target=delayed_update_check, daemon=True).start()
    
    scheduler.start()
    logger.info("Background monitoring worker started")
    
    return scheduler
