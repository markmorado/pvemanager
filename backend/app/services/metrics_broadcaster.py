"""
Metrics Broadcaster — asyncio background tasks that push live metrics
to WebSocket channel subscribers.

Channels:
  dashboard_metrics                          — all servers / VMs overview (2 s)
  server_metrics:{server_id}_{node}          — single Proxmox node status (2 s)
  instance_metrics:{server_id}_{vmid}_{type}_{node}  — VM / LXC status (1 s)

On-demand:
  handle_rrd_request(ws, request_id, action, params, db_factory)
      action = "get_rrd"       → VM / container RRD data
      action = "get_node_rrd"  → node RRD data

NOTE: All Proxmox API calls are synchronous (proxmoxer / requests).  They are
always executed in a thread-pool via asyncio.to_thread() so they never block
the event loop.
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional, Tuple

from loguru import logger

# ──────────────────────────────────────────────────────────────────────────────
# In-memory caches
# ──────────────────────────────────────────────────────────────────────────────

# Last payload broadcast on each channel — sent immediately to new subscribers
_metrics_cache: Dict[str, Any] = {}
# RRD data cache: key = "{server_id}_{node}", value = (timestamp, rrd_data_list)
_rrd_cache: Dict[str, Tuple[float, list]] = {}
_RRD_TTL = 30.0  # seconds between actual RRD fetches from Proxmox

# I/O rate tracking for per-instance detail channel
_io_prev: Dict[str, Tuple[float, Dict[str, float]]] = {}

# Per-disk info cache: channel → (monotonic_ts, disks_list); refreshed every 30 s
_disk_cache: Dict[str, Tuple[float, list]] = {}
_DISK_CACHE_TTL = 30.0

# I/O rate tracking for instances-list channel: key = "server_id:vmid"
_list_io_prev: Dict[str, Tuple[float, Dict[str, float]]] = {}

# Last-good per-server dashboard payload: server_id → {vms, containers, nodes}.
# Used so a transient Proxmox timeout on one poll cycle does not blank the
# dashboard (readings would otherwise flash to zero and back every 2 s).
_server_last_good: Dict[int, dict] = {}


def get_cached_metrics(channel: str) -> Optional[Any]:
    """Return the last cached payload for a channel, or None."""
    return _metrics_cache.get(channel)


def _merge_server_cache(server_id: int, base: dict) -> dict:
    """
    Fill empty vms/containers/nodes from the last successful poll for this
    server, and remember any non-empty values for future degraded cycles.
    Keeps the dashboard stable when a single poll returns partial data.
    """
    cached = _server_last_good.get(server_id, {})
    for key in ("vms", "containers", "nodes"):
        if base.get(key):
            cached[key] = base[key]
        elif cached.get(key):
            base[key] = cached[key]
    base["vms_count"] = len(base.get("vms") or [])
    base["containers_count"] = len(base.get("containers") or [])
    _server_last_good[server_id] = cached
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Synchronous helpers (run inside a thread via asyncio.to_thread)
# ──────────────────────────────────────────────────────────────────────────────

def _get_ws_manager():
    from ..websocket_manager import ws_manager
    return ws_manager


def _get_all_servers(db_factory):
    """Return all ProxmoxServer rows (synchronous DB query)."""
    from ..models import ProxmoxServer
    db = db_factory()
    try:
        return db.query(ProxmoxServer).all()
    finally:
        db.close()


def _fetch_server_resources_with_nodes(server) -> dict:
    """
    Pull VMs + containers + per-node CPU/RAM/Disk status + cached RRD slices.
    Runs in a thread pool — safe to block.
    """
    from ..api.proxmox._helpers import _get_proxmox_client

    base: dict = {
        "id": server.id,
        "name": server.name,
        "ip": server.ip_address,
        "vms": [],
        "containers": [],
        "vms_count": 0,
        "containers_count": 0,
        "nodes": [],
    }

    # Клиент строим через from_server — учитывает нестандартный порт (!= 8006)
    # и выбор token/password. Ручная сборка по ip_address ломала дашборд для
    # серверов на кастомном порту.
    try:
        client = _get_proxmox_client(server)
    except Exception as exc:
        base["error"] = str(exc)
        return _merge_server_cache(server.id, base)

    # ── VMs + containers ──────────────────────────────────────────────────
    try:
        resources = client.get_all_resources()
        vms = resources.get("vms", [])
        containers = resources.get("containers", [])
        base["vms"] = vms
        base["containers"] = containers
        base["vms_count"] = len(vms)
        base["containers_count"] = len(containers)
    except Exception as exc:
        # VM/container fetch failed — fall through to node fetch and let the
        # last-good cache backfill vms/containers at the end.
        base["error"] = str(exc)

    # ── Node status + RRD mini-slices ─────────────────────────────────────
    try:
        if not client.is_connected():
            return _merge_server_cache(server.id, base)
        nodes_raw = client.get_nodes()
        now = time.monotonic()
        nodes_out = []
        for n in nodes_raw:
            node_name = n.get("node", "")
            if not node_name:
                continue
            st = client.get_node_status(node_name) or {}
            cpu_pct = round((st.get("cpu", 0) or 0) * 100, 1)
            mem = st.get("memory") or {}
            ram_pct = round((mem.get("used", 0) / mem.get("total", 1)) * 100, 1) if mem.get("total") else 0
            rootfs = st.get("rootfs") or {}
            disk_pct = round((rootfs.get("used", 0) / rootfs.get("total", 1)) * 100, 1) if rootfs.get("total") else 0

            # RRD: fetch only when cache is stale (> _RRD_TTL seconds old)
            cache_key = f"{server.id}_{node_name}"
            if cache_key not in _rrd_cache or now - _rrd_cache[cache_key][0] >= _RRD_TTL:
                try:
                    rrd_resp = client.get_node_rrddata(node_name, "hour")
                    rrd_data = rrd_resp.get("data", []) if isinstance(rrd_resp, dict) else (rrd_resp or [])
                    _rrd_cache[cache_key] = (now, rrd_data)
                except Exception:
                    rrd_data = _rrd_cache.get(cache_key, (0, []))[1]
            else:
                rrd_data = _rrd_cache[cache_key][1]

            # Build slim 24-point arrays for mini-charts
            tail = rrd_data[-24:] if len(rrd_data) > 24 else rrd_data
            rrd_cpu, rrd_ram, rrd_disk = [], [], []
            for pt in tail:
                rrd_cpu.append(round((pt.get("cpu", 0) or 0) * 100, 1))
                mt = pt.get("memtotal") or 0
                mu = pt.get("memused") or 0
                rrd_ram.append(round((mu / mt * 100) if mt > 0 else 0, 1))
                rt = pt.get("roottotal") or 0
                ru = pt.get("rootused") or 0
                rrd_disk.append(round((ru / rt * 100) if rt > 0 else 0, 1))

            nodes_out.append({
                "name": node_name,
                "cpu_pct": cpu_pct,
                "ram_pct": ram_pct,
                "disk_pct": disk_pct,
                "rrd_cpu": rrd_cpu,
                "rrd_ram": rrd_ram,
                "rrd_disk": rrd_disk,
            })
        base["nodes"] = nodes_out
    except Exception as exc:
        logger.debug(f"[metrics_broadcaster] node stats error for server {server.id}: {exc}")
    return _merge_server_cache(server.id, base)


def _fetch_node_status(db_factory, server_id: int, node: str):
    """Return node status dict or None.  Runs in a thread pool."""
    from ..models import ProxmoxServer
    from ..api.proxmox._helpers import _get_proxmox_client
    db = db_factory()
    try:
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
        if not server:
            return None
        client = _get_proxmox_client(server)
    finally:
        db.close()
    if not client.is_connected():
        return None
    return client.get_node_status(node)


def _fetch_instance_status(db_factory, server_id: int, vmid: int, inst_type: str, node: str):
    """Return VM/LXC current status dict or None.  Runs in a thread pool."""
    from ..models import ProxmoxServer
    from ..api.proxmox._helpers import _get_proxmox_client
    db = db_factory()
    try:
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
        if not server:
            return None
        client = _get_proxmox_client(server)
    finally:
        db.close()
    if not client.is_connected():
        return None
    if inst_type == "qemu":
        status = client.get_vm_status(node, vmid)
    else:
        status = client.get_container_status(node, vmid)
    if status is None:
        return None

    # Attach per-disk filesystem info
    channel = f"instance_metrics:{server_id}_{vmid}_{inst_type}_{node}"
    now = time.monotonic()
    cached = _disk_cache.get(channel)
    if cached and (now - cached[0]) < _DISK_CACHE_TTL:
        status["disks"] = cached[1]
    else:
        if inst_type == "qemu":
            disks = client.get_vm_fsinfo(node, vmid)
        else:
            # LXC: rootfs usage is in status itself
            disk_used = status.get("disk", 0) or 0
            disk_total = status.get("maxdisk", 0) or 0
            disks = [{"name": "rootfs", "mountpoint": "/", "used": disk_used, "total": disk_total}] if disk_total else []
        _disk_cache[channel] = (now, disks)
        status["disks"] = disks

    return status


def _fetch_rrd(db_factory, server_id: int, action: str, params: dict):
    """Fetch RRD time-series data.  Runs in a thread pool."""
    from ..models import ProxmoxServer
    from ..api.proxmox._helpers import _get_proxmox_client
    VALID_TIMEFRAMES = {"hour", "day", "week", "month", "year"}
    node: str = params.get("node", "")
    timeframe: str = params.get("timeframe", "hour")
    if timeframe not in VALID_TIMEFRAMES:
        timeframe = "hour"
    db = db_factory()
    try:
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
        if not server:
            raise ValueError("Server not found")
        client = _get_proxmox_client(server)
    finally:
        db.close()
    if not client.is_connected():
        raise ValueError("Cannot connect to Proxmox server")
    if action == "get_node_rrd":
        return client.get_node_rrddata(node, timeframe)
    elif action == "get_rrd":
        vmid = int(params.get("vmid", 0))
        inst_type: str = params.get("inst_type", "qemu")
        if inst_type == "qemu":
            return client.get_vm_rrddata(node, vmid, timeframe)
        return client.get_container_rrddata(node, vmid, timeframe)
    raise ValueError(f"Unknown RRD action: {action}")


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard metrics loop  (push every 2 s)
# ──────────────────────────────────────────────────────────────────────────────

async def dashboard_metrics_loop(db_factory) -> None:
    """Push aggregated VM / container counts + node stats to 'dashboard_metrics' subscribers."""
    wsman = _get_ws_manager()
    while True:
        await asyncio.sleep(2)
        if not wsman.has_channel_subscribers("dashboard_metrics"):
            continue
        try:
            servers = await asyncio.to_thread(_get_all_servers, db_factory)

            results = await asyncio.gather(
                *[asyncio.to_thread(_fetch_server_resources_with_nodes, srv) for srv in servers],
                return_exceptions=True,
            )

            all_resources: dict = {"servers": [], "total_vms": 0, "total_containers": 0}
            for r in results:
                if isinstance(r, Exception):
                    continue
                all_resources["servers"].append(r)
                all_resources["total_vms"] += r.get("vms_count", 0)
                all_resources["total_containers"] += r.get("containers_count", 0)

            payload = {
                "type": "metrics_update",
                "channel": "dashboard_metrics",
                "data": all_resources,
            }
            _metrics_cache["dashboard_metrics"] = payload
            await wsman.broadcast_to_channel("dashboard_metrics", payload)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"[metrics_broadcaster] dashboard loop error: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Server / node metrics loop  (push every 2 s)
# ──────────────────────────────────────────────────────────────────────────────

async def server_metrics_loop(db_factory) -> None:
    """Push node status to 'server_metrics:{server_id}_{node}' subscribers."""
    wsman = _get_ws_manager()
    while True:
        await asyncio.sleep(2)
        channels = wsman.get_channels_matching("server_metrics:")
        if not channels:
            continue

        async def _push_node(channel: str) -> None:
            try:
                # channel format: server_metrics:{server_id}_{node}
                suffix = channel.split(":", 1)[1]
                sep = suffix.index("_")
                server_id = int(suffix[:sep])
                node = suffix[sep + 1:]
                if not node:
                    return
                status = await asyncio.to_thread(
                    _fetch_node_status, db_factory, server_id, node
                )
                if not status:
                    return
                payload = {
                    "type": "metrics_update",
                    "channel": channel,
                    "data": {"status": status},
                }
                _metrics_cache[channel] = payload
                await wsman.broadcast_to_channel(channel, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[metrics_broadcaster] server metrics error on '{channel}': {exc}")

        await asyncio.gather(*[_push_node(ch) for ch in channels], return_exceptions=True)


# ──────────────────────────────────────────────────────────────────────────────
# Instance metrics loop  (push every 5 s)
# ──────────────────────────────────────────────────────────────────────────────

async def instance_metrics_loop(db_factory) -> None:
    """Push VM/LXC status to 'instance_metrics:{server_id}_{vmid}_{type}_{node}' (~1 s)."""
    wsman = _get_ws_manager()
    while True:
        cycle_start = time.monotonic()
        channels = wsman.get_channels_matching("instance_metrics:")
        if not channels:
            await asyncio.sleep(1)
            continue

        async def _push_instance(channel: str) -> None:
            try:
                # channel format: instance_metrics:{server_id}_{vmid}_{type}_{node}
                suffix = channel.split(":", 1)[1]
                parts = suffix.split("_", 3)
                if len(parts) != 4:
                    return
                server_id_str, vmid_str, inst_type, node = parts
                if not node:
                    return
                vmid = int(vmid_str)
                status = await asyncio.to_thread(
                    _fetch_instance_status, db_factory, int(server_id_str), vmid, inst_type, node
                )
                if not status:
                    return

                # Compute I/O rates (bytes/sec) from cumulative counters
                now = time.monotonic()
                prev = _io_prev.get(channel)
                if prev is not None:
                    dt = now - prev[0]
                    if dt > 0:
                        for key in ("diskread", "diskwrite", "netin", "netout"):
                            cur_val = float(status.get(key) or 0)
                            prv_val = float(prev[1].get(key) or 0)
                            delta = cur_val - prv_val
                            # Guard against resets (VM restart resets counters to 0)
                            status[f"{key}_rate"] = max(0.0, delta / dt)
                _io_prev[channel] = (now, {k: float(status.get(k) or 0) for k in ("diskread", "diskwrite", "netin", "netout")})

                payload = {
                    "type": "metrics_update",
                    "channel": channel,
                    "data": status,
                }
                _metrics_cache[channel] = payload
                await wsman.broadcast_to_channel(channel, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[metrics_broadcaster] instance metrics error on '{channel}': {exc}")

        await asyncio.gather(*[_push_instance(ch) for ch in channels], return_exceptions=True)
        # Pace to a 1 s cadence (sleep only the remainder after the work)
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - cycle_start)))


# ──────────────────────────────────────────────────────────────────────────────
# Instances list metrics loop  (push ~every 2 s)
# Pushes a list of live VM/LXC metrics for the instances table page. One channel
# for all clients: 'instances_list_metrics'.
#
# Data source strategy:
#   • cluster/resources is cheap (one call per server) but pvestatd-backed, so its
#     counters only refresh every ~10 s — too coarse for live I/O rates.
#   • status/current (per VM) refreshes every ~1 s — true real-time.
# So we enumerate VMs via cluster/resources, then refresh every RUNNING VM with
# status/current (bounded concurrency) to get live cpu/mem/net/disk counters.
# ──────────────────────────────────────────────────────────────────────────────

# Max concurrent status/current calls per cycle (avoids hammering Proxmox API).
# Higher concurrency keeps the per-cycle work under the 1 s cadence target even
# with dozens of running VMs.
_LIST_LIVE_CONCURRENCY = 16


def _enumerate_instances_for_list(db_factory):
    """Enumerate VMs/LXC across online servers via cluster/resources (one call per
    server). Returns (items, clients) where `clients` maps server_id → ProxmoxClient
    so the loop can refresh running VMs with live status/current."""
    from ..models import ProxmoxServer
    from ..api.proxmox._helpers import _get_proxmox_client
    db = db_factory()
    try:
        servers = db.query(ProxmoxServer).filter(ProxmoxServer.is_online == True).all()
        # Build clients while the session is open (from_server reads server attrs).
        # Per-server guard: один сервер с битой конфигурацией не должен
        # останавливать метрики для всех остальных.
        server_clients = []
        for s in servers:
            try:
                server_clients.append((s.id, _get_proxmox_client(s)))
            except Exception as exc:
                logger.debug(f"[metrics_broadcaster] skip server {s.id} ({s.name}): {exc}")
    finally:
        db.close()

    items: list = []
    clients: dict = {}
    for server_id, client in server_clients:
        clients[server_id] = client
        try:
            for res in client.get_cluster_resources(type_='vm'):
                vmid = res.get('vmid')
                if vmid is None:
                    continue
                items.append({
                    "server_id": server_id,
                    "vmid": vmid,
                    "type": res.get('type'),
                    "node": res.get('node'),
                    "status": res.get('status'),
                    "cpu": res.get('cpu'),
                    "mem": res.get('mem'),
                    "maxmem": res.get('maxmem'),
                    "disk": res.get('disk'),
                    "maxdisk": res.get('maxdisk'),
                    "uptime": res.get('uptime'),
                    "netin": res.get('netin'),
                    "netout": res.get('netout'),
                    "diskread": res.get('diskread'),
                    "diskwrite": res.get('diskwrite'),
                })
        except Exception as exc:
            logger.debug(f"[metrics_broadcaster] instances list enumerate error for server {server_id}: {exc}")
    return items, clients


def _live_instance_status(client, node: str, vmid: int, inst_type: str):
    """Fetch live status/current for one VM/LXC. Returns dict or None on error."""
    try:
        if inst_type == "lxc":
            return client.get_container_status(node, vmid)
        return client.get_vm_status(node, vmid)
    except Exception:
        return None


# Fields refreshed from the live status/current call (override stale cluster/resources)
_LIVE_FIELDS = ("cpu", "mem", "maxmem", "disk", "maxdisk", "uptime",
                "netin", "netout", "diskread", "diskwrite")


async def instances_list_metrics_loop(db_factory) -> None:
    """Push live VM/LXC metrics list to 'instances_list_metrics' subscribers (~2 s)."""
    wsman = _get_ws_manager()
    channel = "instances_list_metrics"
    sem = asyncio.Semaphore(_LIST_LIVE_CONCURRENCY)

    async def _refresh_running(item: dict, clients: dict) -> None:
        if item.get("status") != "running":
            return
        client = clients.get(item["server_id"])
        if client is None or not item.get("node"):
            return
        async with sem:
            live = await asyncio.to_thread(
                _live_instance_status, client, item["node"], item["vmid"], item.get("type") or "qemu"
            )
        if live:
            for field in _LIVE_FIELDS:
                val = live.get(field)
                if val is not None:
                    item[field] = val

    while True:
        cycle_start = time.monotonic()
        if not wsman.has_channel_subscribers(channel):
            await asyncio.sleep(1)
            continue
        try:
            items, clients = await asyncio.to_thread(_enumerate_instances_for_list, db_factory)
            if not items:
                await asyncio.sleep(1)
                continue

            # Refresh every running VM with live status/current (1 s-granular counters)
            await asyncio.gather(
                *[_refresh_running(it, clients) for it in items],
                return_exceptions=True,
            )

            # Compute per-item I/O rates from the (now live) cumulative counters
            now = time.monotonic()
            for item in items:
                key = f"{item['server_id']}:{item['vmid']}"
                prev = _list_io_prev.get(key)
                if prev is not None:
                    dt = now - prev[0]
                    if dt > 0:
                        for field in ("diskread", "diskwrite", "netin", "netout"):
                            cur = float(item.get(field) or 0)
                            prv = float(prev[1].get(field) or 0)
                            item[f"{field}_rate"] = max(0.0, (cur - prv) / dt)
                _list_io_prev[key] = (now, {f: float(item.get(f) or 0) for f in ("diskread", "diskwrite", "netin", "netout")})

            payload = {
                "type": "metrics_update",
                "channel": channel,
                "data": {"items": items},
            }
            _metrics_cache[channel] = payload
            await wsman.broadcast_to_channel(channel, payload)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"[metrics_broadcaster] instances list loop error: {exc}")
        # Pace to a 1 s cadence (sleep only the remainder after the work)
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - cycle_start)))



# ──────────────────────────────────────────────────────────────────────────────
# On-demand RRD request handler
# ──────────────────────────────────────────────────────────────────────────────

async def handle_rrd_request(
    websocket,
    request_id: str,
    action: str,
    params: dict,
    db_factory,
) -> None:
    """
    Handle a client-initiated RRD data request and reply directly to the
    requesting WebSocket connection.  The actual Proxmox call runs in a thread.

    Expected params keys:
      server_id   — int
      node        — str
      timeframe   — "hour" | "day" | "week" | "month" | "year"
      vmid        — int  (for get_rrd only)
      inst_type   — "qemu" | "lxc"  (for get_rrd only)
    """
    error_msg = None
    data = None
    try:
        server_id = int(params.get("server_id", 0))
        data = await asyncio.to_thread(_fetch_rrd, db_factory, server_id, action, params)
    except Exception as exc:
        logger.debug(f"[metrics_broadcaster] RRD request error ({action}): {exc}")
        error_msg = str(exc)

    try:
        payload: dict = {"type": "rrd_response", "request_id": request_id, "data": data}
        if error_msg:
            payload["error"] = error_msg
        await websocket.send_text(json.dumps(payload, default=str))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Entry-point — call from lifespan
# ──────────────────────────────────────────────────────────────────────────────

# Strong references to the long-running loops. The event loop only keeps weak
# references to tasks, so a task nobody holds can be garbage-collected mid-run
# and the corresponding metrics stream silently stops.
_background_tasks: set = set()


def _spawn(coro, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(f"[metrics_broadcaster] task {t.get_name()} crashed: {t.exception()!r}")

    task.add_done_callback(_done)


def start_metrics_broadcaster(db_factory) -> None:
    """
    Schedule all metrics broadcasting coroutines as asyncio Tasks.
    Must be called from within a running asyncio event loop (e.g. lifespan).
    """
    _spawn(dashboard_metrics_loop(db_factory), "metrics:dashboard")
    _spawn(server_metrics_loop(db_factory), "metrics:servers")
    _spawn(instance_metrics_loop(db_factory), "metrics:instances")
    _spawn(instances_list_metrics_loop(db_factory), "metrics:instances_list")
    # Import inside function to avoid circular imports
    from .metrics_collector import metrics_collector_loop, metrics_retention_loop
    _spawn(metrics_collector_loop(db_factory), "metrics:collector")
    _spawn(metrics_retention_loop(db_factory), "metrics:retention")
    logger.info("[metrics_broadcaster] Metrics broadcasting tasks started")
