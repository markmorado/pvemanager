"""
Background monitoring worker for generating notifications
Uses APScheduler for periodic tasks
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from loguru import logger


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


try:
    from backend.app.db import SessionLocal
    from backend.app.models import User, ProxmoxServer, PanelSettings, VMInstance, IPAMAllocation, Notification
    from backend.app.services.notification_service import NotificationService
    from backend.app.proxmox_client import ProxmoxClient
    from backend.app.schemas import NotificationCreate
    from backend.app.i18n import t
except ImportError:
    from app.db import SessionLocal
    from app.models import User, ProxmoxServer, PanelSettings, VMInstance, IPAMAllocation, Notification
    from app.services.notification_service import NotificationService
    from app.proxmox_client import ProxmoxClient
    from app.schemas import NotificationCreate
    from app.i18n import t


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


class MonitoringWorker:
    """Background worker for monitoring and generating notifications"""
    
    def __init__(self):
        self.last_vm_states: Dict[str, str] = {}  # vm_id -> status
        self.last_resource_alerts: Dict[str, float] = {}  # resource_id -> last_alert_time
        self.last_server_states: Dict[int, bool] = {}  # server_id -> is_online
        self.last_server_alerts: Dict[int, float] = {}  # server_id -> last_alert_time
    
    def _create_proxmox_client(self, server: ProxmoxServer) -> ProxmoxClient:
        """Create ProxmoxClient from server model"""
        # ALWAYS prefer ip_address over hostname (hostname may not be DNS-resolvable)
        host = server.ip_address or server.hostname
        if server.port and server.port != 8006:
            host = f"{host}:{server.port}"
        
        # Determine authentication method
        if server.use_password and server.password:
            # Password authentication
            return ProxmoxClient(
                host=host,
                user=server.api_user,
                password=server.password,
                verify_ssl=server.verify_ssl
            )
        elif server.api_token_name and server.api_token_value:
            # API Token authentication
            return ProxmoxClient(
                host=host,
                user=server.api_user,
                token_name=server.api_token_name,
                token_value=server.api_token_value,
                verify_ssl=server.verify_ssl
            )
        else:
            raise ValueError(f"Server {server.name} has no valid authentication configured")
    
    def _notify_server_offline(self, db, server: ProxmoxServer, error: str, users: List[User]):
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
                    if client.is_connected():
                        # Server is online
                        was_offline = self.last_server_states.get(server.id) == False
                        
                        # Update server status in DB
                        if not server.is_online or was_offline:
                            server.is_online = True
                            server.last_error = None
                            server.last_check = utcnow()
                            db.commit()
                        
                        # Notify immediately if server recovered
                        if was_offline:
                            self._notify_server_recovered(db, server, users)
                        
                        self.last_server_states[server.id] = True
                    else:
                        raise ConnectionError("API not responding")
                        
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
                            logger.warning(f"[SERVER CHECK] Server {server.name} went OFFLINE: {error_msg}")
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
                    if not client.is_connected():
                        raise ConnectionError(f"Cannot connect to Proxmox server at {server.hostname}")
                    
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
                            
                            # Notify all users (in production, filter by permissions)
                            for user in users:
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
                    self._notify_server_offline(db, server, error_msg, users)
                    
        except Exception as e:
            logger.error(f"[MONITORING] Critical error in VM status monitoring: {e}", exc_info=True)
        finally:
            db.close()
    
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
                            # Get VM statistics
                            stats = client.get_vm_stats(vm['node'], vm['vmid'])
                            
                            # Check CPU usage
                            if 'cpu' in stats and stats['cpu'] is not None:
                                cpu_percent = stats['cpu'] * 100
                                if cpu_percent > 80:
                                    alert_key = f"{vm_key}:cpu"
                                    if self._should_send_alert(alert_key):
                                        for user in users:
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
                                            for user in users:
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
                                            for user in users:
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
        """Clean up expired notifications"""
        db = SessionLocal()
        try:
            count = NotificationService.cleanup_expired(db)
            if count > 0:
                logger.info(f"Cleaned up {count} expired notifications")
        except Exception as e:
            logger.error(f"Error cleaning up notifications: {e}")
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
            
            logger.info(f"[VM SYNC] Starting VM cache sync for {len(servers)} servers")
            
            # Load IPAM allocations for IP lookup
            ipam_allocations = db.query(IPAMAllocation).filter(
                IPAMAllocation.status.in_(['allocated', 'reserved'])
            ).all()
            
            # Build IPAM lookups
            ipam_by_server_vmid = {}  # (server_id, vmid) -> allocation
            ipam_by_name = {}  # hostname/resource_name -> allocation
            
            for alloc in ipam_allocations:
                if alloc.proxmox_server_id and alloc.proxmox_vmid:
                    ipam_by_server_vmid[(alloc.proxmox_server_id, alloc.proxmox_vmid)] = alloc
                if alloc.hostname:
                    ipam_by_name[alloc.hostname.lower()] = alloc
                if alloc.resource_name:
                    ipam_by_name[alloc.resource_name.lower()] = alloc
            
            # Detect clusters: servers that see multiple nodes are part of a cluster
            # Group servers by their node set
            server_nodes = {}  # server_id -> set of node names
            cluster_map = {}  # server_id -> cluster_id (first server_id in cluster)
            
            for server in servers:
                try:
                    client = self._create_proxmox_client(server)
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
                logger.info(f"[VM SYNC] Cluster detected: servers {server_ids} share nodes {list(nodes)})")
            
            # Track seen VMs per cluster/server for dedup
            seen_in_cluster = {}  # cluster_id -> set of vmid
            seen_standalone = {}  # server_id -> set of vmid
            
            all_vms_data = []  # List of (server_id, vm_data, vm_type)
            sync_time = utcnow()
            
            # Collect all VMs from all servers with proper dedup
            for server in servers:
                try:
                    client = self._create_proxmox_client(server)
                    
                    if not client.is_connected():
                        logger.warning(f"[VM SYNC] Server {server.name} not connected, skipping")
                        continue
                    
                    # Get all VMs and containers
                    vms = client.get_vms()
                    containers = client.get_containers()
                    
                    cluster_id = cluster_map.get(server.id)
                    
                    # Determine which dedup set to use
                    if cluster_id:
                        if cluster_id not in seen_in_cluster:
                            seen_in_cluster[cluster_id] = set()
                        seen_set = seen_in_cluster[cluster_id]
                        use_server_id = cluster_id  # Use cluster_id for all VMs in cluster
                    else:
                        if server.id not in seen_standalone:
                            seen_standalone[server.id] = set()
                        seen_set = seen_standalone[server.id]
                        use_server_id = server.id
                    
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
                    
                    logger.info(f"[VM SYNC] Server {server.name}: collected {server_vm_count} VMs, {server_ct_count} containers")
                        
                except Exception as e:
                    logger.error(f"[VM SYNC] Error fetching from server {server.name}: {e}")
            
            logger.info(f"[VM SYNC] Collected {len(all_vms_data)} unique VMs/containers")
            
            # Track all seen (server_id, vmid) pairs for cleanup
            all_seen = set()
            
            # Upsert all VMs to database
            for server_id, vm_data, vm_type in all_vms_data:
                try:
                    vmid = vm_data.get('vmid')
                    vm_name = vm_data.get('name', f"{'VM' if vm_type == 'qemu' else 'CT'}-{vmid}")
                    node_name = vm_data.get('node', '')
                    
                    all_seen.add((server_id, vmid))
                    
                    # Get IP from IPAM
                    ipam_alloc = (
                        ipam_by_server_vmid.get((server_id, vmid)) or 
                        ipam_by_name.get(vm_name.lower())
                    )
                    ip_address = ipam_alloc.ip_address if ipam_alloc else None
                    
                    # Get OS type
                    if vm_type == 'qemu':
                        os_type = vm_data.get('ostype', 'QEMU/KVM')
                    else:
                        os_type = vm_data.get('ostype', 'Linux')
                        if os_type:
                            os_type = os_type.capitalize()
                    
                    # Find existing by (server_id, vmid)
                    existing = db.query(VMInstance).filter(
                        VMInstance.server_id == server_id,
                        VMInstance.vmid == vmid
                    ).first()
                    
                    if existing:
                        # Update existing entry
                        existing.name = vm_name
                        existing.node = node_name
                        existing.status = vm_data.get('status', 'unknown')
                        existing.cores = vm_data.get('cpus', vm_data.get('maxcpu'))
                        existing.memory = vm_data.get('maxmem')
                        existing.disk_size = vm_data.get('maxdisk')
                        existing.os_type = os_type
                        existing.vm_type = vm_type
                        existing.is_template = bool(vm_data.get('template'))
                        existing.ip_address = ip_address
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
                            status=vm_data.get('status', 'unknown'),
                            cores=vm_data.get('cpus', vm_data.get('maxcpu')),
                            memory=vm_data.get('maxmem'),
                            disk_size=vm_data.get('maxdisk'),
                            os_type=os_type,
                            is_template=bool(vm_data.get('template')),
                            ip_address=ip_address,
                            last_sync_at=sync_time
                        )
                        db.add(new_vm)
                        
                except Exception as e:
                    logger.error(f"[VM SYNC] Error processing VM {vmid}: {e}")
            
            # Commit all changes
            try:
                db.commit()
            except Exception as e:
                logger.error(f"[VM SYNC] Error committing changes: {e}")
                db.rollback()
                return
            
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
            for vm in active_vms:
                if (vm.server_id, vm.vmid) not in all_seen:
                    vm.deleted_at = sync_time
                    deleted_count += 1
                    logger.info(f"[VM SYNC] Marked VM {vm.name} (server={vm.server_id}, vmid={vm.vmid}) as deleted")
                    
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
                            logger.info(f"[VM SYNC] Released IPAM IP {released_ip} for deleted VM {vm.name}")
                    except Exception as e:
                        logger.warning(f"[VM SYNC] Failed to release IPAM for VM {vm.vmid}: {e}")
            
            db.commit()
            logger.info(f"[VM SYNC] Cache sync completed. Processed {len(all_vms_data)} VMs/containers, marked {deleted_count} as deleted, released {released_ips} IPs")
            
        except Exception as e:
            logger.error(f"[VM SYNC] Critical error: {e}", exc_info=True)
            db.rollback()
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
            logger.info("[UPDATE CHECK] Checking for panel updates...")
            
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
                    logger.info(f"[UPDATE CHECK] Notified user {user.username} about update to {new_version}")
                except Exception as e:
                    logger.error(f"[UPDATE CHECK] Failed to notify user {user.id}: {e}")
            
            db.commit()
            logger.info(f"[UPDATE CHECK] Completed. Notified {len(users)} admin users about version {new_version}")
            
        except Exception as e:
            logger.error(f"[UPDATE CHECK] Error checking for updates: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()


# Global worker instance
monitoring_worker = MonitoringWorker()


def start_monitoring_worker():
    """Start background monitoring tasks using APScheduler"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    
    scheduler = BackgroundScheduler()
    
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
    
    # VM cache sync - every 30 seconds
    scheduler.add_job(
        monitoring_worker.sync_vm_cache,
        trigger=IntervalTrigger(seconds=30),
        id='vm_cache_sync',
        name='Sync VM cache from Proxmox',
        replace_existing=True
    )
    
    # Task queue processing - every 5 seconds
    try:
        from app.services.task_queue_service import process_task_queue
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
    
    # Cleanup expired notifications - every 6 hours
    scheduler.add_job(
        monitoring_worker.run_cleanup_expired,
        trigger=IntervalTrigger(hours=6),
        id='cleanup_notifications',
        name='Clean up expired notifications',
        replace_existing=True
    )
    
    # Run initial VM cache sync on startup
    try:
        logger.info("Running initial VM cache sync...")
        monitoring_worker.sync_vm_cache()
    except Exception as e:
        logger.warning(f"Initial VM cache sync failed: {e}")
    
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
