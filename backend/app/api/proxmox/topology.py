"""Aggregated network topology: panel -> cluster -> node -> guest.

One endpoint feeds the whole /topology page so the frontend makes a single
request instead of fanning out over servers/nodes/sdn/vms. Everything the graph
draws - plus the bridge/VLAN/MAC data the planned "group by VLAN" view will
need - is collected here.

Kept separate from ``cluster.py``'s ``/api/cluster/topology``, which answers a
different question (which servers form which cluster) and is consumed by the
Cluster page.
"""

import asyncio
import threading
import time as time_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...auth import PermissionChecker
from ...db import get_db
from ...models import IPAMAllocation, IPAMNetwork, ProxmoxServer, User, VMInstance
from ...proxmox import _run_in_executor, parse_guest_nics
from ...schemas import (NetworkTopologyResponse, TopologyBridge, TopologyCluster,
                        TopologyGuest, TopologyNic, TopologyNode,
                        TopologyPanelSummary, TopologyWarning)
from ._helpers import _get_proxmox_client, can_view_all_instances, get_visible_server_ids

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])

# Live cluster/resources + node networks, keyed by server id. Short TTL: the
# page auto-refreshes every 30s and several users may watch it at once.
_topo_cache: Dict[int, Dict[str, Any]] = {}
_topo_lock = threading.Lock()
TOPO_TTL = 15

# Guest configs change rarely, and reading them costs one HTTP call per guest.
_nic_cache: Dict[Tuple[int, int], Dict[str, Any]] = {}
_nic_lock = threading.Lock()
NIC_TTL = 300
NIC_MAX_WORKERS = 8
NIC_MAX_FETCHES = 300  # hard ceiling so a huge cluster cannot hang the request


def _truthy(value: Any) -> bool:
    """Proxmox returns flags as 1/0, "1"/"0" or bools depending on the field."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ==================== Live collection (runs off the request thread) ====================

def _collect_server(server: ProxmoxServer, include_bridges: bool) -> Dict[str, Any]:
    """Everything one Proxmox connection can tell us, in as few calls as possible."""
    client = _get_proxmox_client(server)

    # A single /cluster/resources call carries both node and guest metrics.
    resources = client.get_cluster_resources() or []
    nodes = [r for r in resources if r.get("type") == "node"]
    guests = [r for r in resources if r.get("type") in ("qemu", "lxc")]

    bridges: Dict[str, List[Dict[str, Any]]] = {}
    if include_bridges:
        # Zones/vnets are cluster-wide; fetch once and filter per node below.
        try:
            zones = {z.get("zone"): z for z in (client.get_sdn_zones() or [])}
            vnets = client.get_sdn_vnets() or []
        except Exception as exc:  # SDN is optional and older PVE lacks it
            logger.debug(f"SDN unavailable on {server.name}: {exc}")
            zones, vnets = {}, []

        for node_res in nodes:
            node_name = node_res.get("node")
            if not node_name:
                continue
            entries: List[Dict[str, Any]] = []
            try:
                for iface in client.get_node_networks(node_name) or []:
                    name = iface.get("iface") or iface.get("name")
                    if not name:
                        continue
                    cidr = iface.get("cidr")
                    if not cidr and iface.get("address") and iface.get("netmask"):
                        cidr = "{0}/{1}".format(iface["address"], iface["netmask"])
                    entries.append({
                        "name": name,
                        "type": iface.get("type"),
                        "cidr": cidr,
                        "vlan_aware": _truthy(iface.get("bridge_vlan_aware")),
                        "ports": iface.get("bridge_ports") or iface.get("slaves"),
                        "active": _truthy(iface.get("active")),
                    })
            except Exception as exc:
                logger.debug(f"Could not read networks of {server.name}/{node_name}: {exc}")

            # SDN vnets are attachable as bridges but never appear in /network.
            for vnet in vnets:
                vnet_name = vnet.get("vnet")
                if not vnet_name:
                    continue
                zone_name = vnet.get("zone", "")
                zone_nodes = (zones.get(zone_name, {}).get("nodes") or "").replace(" ", "")
                if zone_nodes and node_name not in zone_nodes.split(","):
                    continue
                entries.append({
                    "name": vnet_name,
                    "type": "vnet",
                    "zone": zone_name,
                    "active": True,
                })
            bridges[node_name] = entries

    return {"nodes": nodes, "guests": guests, "bridges": bridges}


def _fetch_nics(server: ProxmoxServer, targets: List[Tuple[str, int, str]]) -> Dict[int, List[Dict[str, Any]]]:
    """``netN`` cards for the given guests. ``targets`` = [(node, vmid, type)]."""
    if not targets:
        return {}
    client = _get_proxmox_client(server)

    def _one(node: str, vmid: int, vm_type: str) -> Tuple[int, List[Dict[str, Any]]]:
        getter = client.get_vm_config if vm_type == "qemu" else client.get_container_config
        config = getter(node, vmid) or {}
        return vmid, [nic.to_dict() for nic in parse_guest_nics(config)]

    result: Dict[int, List[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(NIC_MAX_WORKERS, len(targets))) as pool:
        futures = [pool.submit(_one, *target) for target in targets]
        for future in as_completed(futures):
            try:
                vmid, nics = future.result()
            except Exception as exc:
                logger.debug(f"Could not read guest config on {server.name}: {exc}")
                continue
            result[vmid] = nics
    return result


# ==================== Helpers ====================

def _split_tags(raw: Optional[str]) -> List[str]:
    """Proxmox stores tags as a ``;``-separated string; the graph wants a list."""
    if not raw:
        return []
    return [t.strip() for t in str(raw).replace(",", ";").split(";") if t.strip()]


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_response() -> NetworkTopologyResponse:
    return NetworkTopologyResponse(
        generated_at=datetime.now(timezone.utc),
        panel=TopologyPanelSummary(),
        clusters=[],
        warnings=[],
    )


# ==================== Endpoint ====================

@router.get("/api/network/topology", response_model=NetworkTopologyResponse)
async def get_network_topology(
    request: Request,
    server_id: Optional[int] = Query(None, description="Limit to a single connection"),
    include_stopped: bool = Query(True),
    include_templates: bool = Query(False),
    include_bridges: bool = Query(True),
    include_nics: bool = Query(False, description="Read every guest config for bridge/VLAN/MAC - one call per guest"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:view")),
):
    """Infrastructure tree for the topology page.

    Always answers 200 (bar a permission denial): an unreachable server
    degrades to cached rows plus a warning rather than failing the page.
    """
    warnings: List[TopologyWarning] = []

    # -- Visibility ----------------------------------------------------------
    # None means "no restriction" and is NOT the same as an empty set.
    visible_ids = get_visible_server_ids(request, db, current_user)
    if visible_ids is not None and not visible_ids:
        return _empty_response()

    servers_q = db.query(ProxmoxServer)
    if visible_ids is not None:
        servers_q = servers_q.filter(ProxmoxServer.id.in_(visible_ids))
    if server_id is not None:
        if visible_ids is not None and server_id not in visible_ids:
            # 404 rather than 403 so the response cannot confirm the server exists.
            raise HTTPException(status_code=404, detail="Proxmox server not found")
        servers_q = servers_q.filter(ProxmoxServer.id == server_id)
    servers = servers_q.order_by(ProxmoxServer.name).all()
    if not servers:
        return _empty_response()
    server_map = {s.id: s for s in servers}

    # -- Ownership: live Proxmox data carries no owner, the DB does ----------
    see_all = can_view_all_instances(current_user)

    db_q = (
        db.query(VMInstance)
        .options(joinedload(VMInstance.owner))
        .filter(VMInstance.deleted_at.is_(None), VMInstance.server_id.in_(server_map.keys()))
    )
    if not see_all:
        db_q = db_q.filter(VMInstance.owner_id == current_user.id)
    db_guests = db_q.all()
    db_by_key: Dict[Tuple[int, int], VMInstance] = {(g.server_id, g.vmid): g for g in db_guests}
    # Pair-scoped, not vmid-scoped: the same vmid on two servers is two guests.
    owned_keys = set(db_by_key.keys())

    # -- IP addresses from IPAM, falling back to the cached instance row ------
    ipam_by_key: Dict[Tuple[int, int], str] = {}
    for alloc in (
        db.query(IPAMAllocation)
        .filter(IPAMAllocation.status.in_(["allocated", "reserved"]))
        .all()
    ):
        if alloc.proxmox_server_id and alloc.proxmox_vmid and alloc.ip_address:
            ipam_by_key[(alloc.proxmox_server_id, alloc.proxmox_vmid)] = alloc.ip_address

    ipam_nets = db.query(IPAMNetwork).filter(
        IPAMNetwork.proxmox_server_id.in_(server_map.keys())
    ).all()
    ipam_by_bridge: Dict[Tuple[int, str], IPAMNetwork] = {
        (n.proxmox_server_id, n.proxmox_bridge): n for n in ipam_nets if n.proxmox_bridge
    }

    # -- Live data, in parallel: one dead cluster must not stall the rest -----
    live: Dict[int, Dict[str, Any]] = {}
    stale_servers: List[ProxmoxServer] = []
    now = time_lib.time()
    with _topo_lock:  # guards the dict only, never a network call
        for server in servers:
            cached = _topo_cache.get(server.id)
            if cached and (now - cached["time"]) < TOPO_TTL:
                live[server.id] = cached["data"]
            elif server.is_online is False:
                # Offline servers contribute nothing but a connection timeout.
                continue
            else:
                stale_servers.append(server)

    if stale_servers:
        tasks = [_run_in_executor(_collect_server, s, include_bridges) for s in stale_servers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for server, result in zip(stale_servers, results):
            if isinstance(result, HTTPException):
                warnings.append(TopologyWarning(
                    server_id=server.id, server_name=server.name,
                    code="no_credentials", message=str(result.detail),
                ))
                continue
            if isinstance(result, BaseException):
                logger.warning(f"Topology collection failed for {server.name}: {result}")
                warnings.append(TopologyWarning(
                    server_id=server.id, server_name=server.name,
                    code="error", message=str(result),
                ))
                continue
            live[server.id] = result
            with _topo_lock:
                _topo_cache[server.id] = {"time": time_lib.time(), "data": result}

    for server in servers:
        if server.id not in live and not any(w.server_id == server.id for w in warnings):
            warnings.append(TopologyWarning(
                server_id=server.id, server_name=server.name,
                code="offline", message=f"'{server.name}' is unreachable - showing cached data",
            ))

    # -- NICs (opt-in): bridge / VLAN / MAC straight from the guest configs ---
    nics_by_key: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    if include_nics:
        pending: Dict[int, List[Tuple[str, int, str]]] = {}
        budget = NIC_MAX_FETCHES
        truncated = False
        now = time_lib.time()
        for sid, data in live.items():
            for guest in data["guests"]:
                vmid = _to_int(guest.get("vmid"))
                node = guest.get("node")
                if vmid is None or not node:
                    continue
                if not see_all and (sid, vmid) not in owned_keys:
                    continue
                with _nic_lock:
                    cached = _nic_cache.get((sid, vmid))
                if cached and (now - cached["time"]) < NIC_TTL:
                    nics_by_key[(sid, vmid)] = cached["data"]
                    continue
                if budget <= 0:
                    truncated = True
                    continue
                budget -= 1
                pending.setdefault(sid, []).append((node, vmid, guest.get("type", "qemu")))

        if truncated:
            warnings.append(TopologyWarning(
                code="nics_truncated",
                message=f"Network card details limited to {NIC_MAX_FETCHES} guests per request",
            ))

        if pending:
            server_ids = list(pending.keys())
            tasks = [_run_in_executor(_fetch_nics, server_map[sid], pending[sid]) for sid in server_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            stamp = time_lib.time()
            for sid, result in zip(server_ids, results):
                if isinstance(result, BaseException):
                    logger.debug(f"NIC collection failed for server {sid}: {result}")
                    continue
                for vmid, nics in result.items():
                    nics_by_key[(sid, vmid)] = nics
                    with _nic_lock:
                        _nic_cache[(sid, vmid)] = {"time": stamp, "data": nics}

    # -- Assemble ------------------------------------------------------------
    def _build_guest(sid: int, node_name: str, res: Optional[Dict[str, Any]],
                     row: Optional[VMInstance]) -> Optional[TopologyGuest]:
        res = res or {}
        vmid = _to_int(res.get("vmid"))
        if vmid is None and row is not None:
            vmid = row.vmid
        if vmid is None:
            return None

        vm_type = res.get("type") or (row.vm_type if row else None) or "qemu"
        is_template = _truthy(res.get("template")) or bool(row and row.is_template)
        if is_template and not include_templates:
            return None
        status = res.get("status") or (row.status if row else None) or "unknown"
        if not include_stopped and status != "running":
            return None

        tags = _split_tags(res.get("tags")) or _split_tags(row.tags if row else None)
        name = res.get("name") or (row.name if row else None) or f"vm-{vmid}"

        return TopologyGuest(
            id=f"guest:{sid}:{vmid}",
            server_id=sid,
            node=node_name,
            vmid=vmid,
            type=vm_type,
            name=name,
            status=status,
            is_template=is_template,
            lock=res.get("lock"),
            cpu=_to_float(res.get("cpu")),
            cores=_to_int(res.get("maxcpu")) or (row.cores if row else None),
            mem=_to_int(res.get("mem")),
            maxmem=_to_int(res.get("maxmem")) or (row.memory if row else None),
            disk=_to_int(res.get("disk")),
            maxdisk=_to_int(res.get("maxdisk")),
            uptime=_to_int(res.get("uptime")),
            tags=tags,
            owner_id=row.owner_id if row else None,
            owner_username=(row.owner.username if row and row.owner else None),
            ip=ipam_by_key.get((sid, vmid)) or (row.ip_address if row else None),
            nics=[
                TopologyNic(**{k: v for k, v in nic.items() if k in TopologyNic.model_fields})
                for nic in nics_by_key.get((sid, vmid), [])
            ],
        )

    # cluster_name groups connections; a server without one stands alone.
    cluster_buckets: Dict[str, Dict[str, Any]] = {}
    for server in servers:
        key = f"cluster:{server.cluster_name}" if server.cluster_name else f"cluster:server:{server.id}"
        bucket = cluster_buckets.setdefault(key, {
            "id": key,
            "name": server.cluster_name or server.name,
            "kind": "cluster" if server.cluster_name else "standalone",
            "server_ids": [],
            "online": False,
            "nodes": {},   # node name -> TopologyNode, deduplicated per cluster
        })
        bucket["server_ids"].append(server.id)
        if server.id in live:
            bucket["online"] = True

        data = live.get(server.id)
        if data is None:
            # Rebuild from the 10s VM cache so an offline server still shows up.
            by_node: Dict[str, List[VMInstance]] = {}
            for row in db_guests:
                if row.server_id != server.id:
                    continue
                by_node.setdefault(row.node or server.name, []).append(row)
            for node_name, node_rows in by_node.items():
                if node_name in bucket["nodes"]:
                    continue
                guests = [g for g in (_build_guest(server.id, node_name, None, r) for r in node_rows) if g]
                guests.sort(key=lambda g: g.vmid)
                bucket["nodes"][node_name] = TopologyNode(
                    id=f"node:{server.id}:{node_name}",
                    server_id=server.id, server_name=server.name, node=node_name,
                    status="unknown", stale=True, guests=guests,
                )
            continue

        guests_by_node: Dict[str, List[Dict[str, Any]]] = {}
        for res in data["guests"]:
            node_name = res.get("node")
            vmid = _to_int(res.get("vmid"))
            if not node_name or vmid is None:
                continue
            if not see_all and (server.id, vmid) not in owned_keys:
                continue
            guests_by_node.setdefault(node_name, []).append(res)

        for node_res in data["nodes"]:
            node_name = node_res.get("node")
            if not node_name or node_name in bucket["nodes"]:
                continue  # same PVE cluster reachable through two connections
            bridges = []
            for entry in data["bridges"].get(node_name, []):
                ipam_net = ipam_by_bridge.get((server.id, entry["name"]))
                bridges.append(TopologyBridge(
                    **entry,
                    ipam_network_id=ipam_net.id if ipam_net else None,
                    ipam_cidr=ipam_net.network if ipam_net else None,
                    ipam_name=ipam_net.name if ipam_net else None,
                ))
            guests = [
                g for g in (
                    _build_guest(server.id, node_name, res,
                                 db_by_key.get((server.id, _to_int(res.get("vmid")))))
                    for res in guests_by_node.get(node_name, [])
                ) if g
            ]
            guests.sort(key=lambda g: g.vmid)
            bucket["nodes"][node_name] = TopologyNode(
                id=f"node:{server.id}:{node_name}",
                server_id=server.id,
                server_name=server.name,
                node=node_name,
                status=node_res.get("status") or "unknown",
                stale=False,
                cpu=_to_float(node_res.get("cpu")),
                maxcpu=_to_int(node_res.get("maxcpu")),
                mem=_to_int(node_res.get("mem")),
                maxmem=_to_int(node_res.get("maxmem")),
                uptime=_to_int(node_res.get("uptime")),
                bridges=bridges,
                guests=guests,
            )

    clusters = [
        TopologyCluster(
            id=b["id"], name=b["name"], kind=b["kind"],
            server_ids=b["server_ids"], online=b["online"],
            nodes=sorted(b["nodes"].values(), key=lambda n: n.node),
        )
        for b in cluster_buckets.values()
    ]
    clusters.sort(key=lambda c: (c.kind != "cluster", c.name.lower()))

    node_count = sum(len(c.nodes) for c in clusters)
    guest_count = sum(len(n.guests) for c in clusters for n in c.nodes)

    return NetworkTopologyResponse(
        generated_at=datetime.now(timezone.utc),
        panel=TopologyPanelSummary(
            cluster_count=len(clusters), node_count=node_count, guest_count=guest_count,
        ),
        clusters=clusters,
        warnings=warnings,
    )
