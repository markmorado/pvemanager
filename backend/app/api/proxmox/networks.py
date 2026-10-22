from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from loguru import logger

from ...db import get_db
from ...models import ProxmoxServer, User, IPAMNetwork
from ...auth import PermissionChecker
from ._helpers import _get_proxmox_client
from ...proxmox import _run_in_executor

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


# ==================== Node list ====================

@router.get("/api/servers/{server_id}/nodes")
def get_server_nodes(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """List nodes for a Proxmox server"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)
        nodes = client.get_nodes()
        return JSONResponse(content={"nodes": nodes})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting nodes for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Node Network Interfaces ====================

@router.get("/api/servers/{server_id}/nodes/{node}/networks")
def list_node_networks(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """List all network interfaces on a node, with IPAM linkage info."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)
        ifaces = client.get_node_networks(node)

        # Fetch all IPAM networks linked to this server once — O(1) round-trips
        ipam_nets = (
            db.query(IPAMNetwork)
            .filter(IPAMNetwork.proxmox_server_id == server_id)
            .all()
        )
        # Build lookup: bridge_name → ipam_network
        bridge_to_ipam = {n.proxmox_bridge: n for n in ipam_nets if n.proxmox_bridge}

        enriched = []
        for iface in ifaces:
            iface_name = iface.get("iface") or iface.get("name", "")
            ipam_net = bridge_to_ipam.get(iface_name)
            record = dict(iface)
            if ipam_net:
                record["ipam_network_id"] = ipam_net.id
                record["ipam_cidr"] = ipam_net.network
                record["ipam_name"] = ipam_net.name
            enriched.append(record)

        # SDN vnets are usable as bridges in VM/LXC NICs but are not returned
        # by /nodes/{node}/network. They belong to a zone, not a node — include
        # only vnets whose zone covers this node (empty zone nodes = all nodes).
        sdn_vnets = []
        zones = {z.get("zone"): z for z in client.get_sdn_zones()}
        for vnet in client.get_sdn_vnets():
            name = vnet.get("vnet", "")
            if not name:
                continue
            zone_name = vnet.get("zone", "")
            zone_nodes = (zones.get(zone_name, {}).get("nodes") or "").replace(" ", "")
            if zone_nodes and node not in zone_nodes.split(","):
                continue
            record = {
                "iface": name,
                "type": "vnet",
                "zone": zone_name,
                "comments": vnet.get("alias") or f"SDN ({zone_name})",
            }
            ipam_net = bridge_to_ipam.get(name)
            if ipam_net:
                record["ipam_network_id"] = ipam_net.id
                record["ipam_cidr"] = ipam_net.network
                record["ipam_name"] = ipam_net.name
            sdn_vnets.append(record)

        return JSONResponse(content={"node": node, "interfaces": enriched, "sdn_vnets": sdn_vnets})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing node {node} networks for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/networks")
async def create_node_network(
    server_id: int,
    node: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Create a new network interface on a node."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    iface_type = data.pop("type", None)
    if not iface_type:
        raise HTTPException(status_code=400, detail="Interface type is required")

    iface_name = data.get("iface", "")
    if not iface_name:
        raise HTTPException(status_code=400, detail="Interface name (iface) is required")

    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.create_node_network, node, iface_type, **data)
        if result.get("success"):
            logger.info(
                f"User {current_user.username} created {iface_type} interface "
                f"{iface_name} on {server.name}/{node}"
            )
            return JSONResponse(content=result)
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to create interface"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating node interface: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/nodes/{node}/networks/{iface}")
async def update_node_network(
    server_id: int,
    node: str,
    iface: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Update a network interface on a node."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.update_node_network, node, iface, **data)
        if result.get("success"):
            logger.info(
                f"User {current_user.username} updated interface {iface} "
                f"on {server.name}/{node}"
            )
            return JSONResponse(content=result)
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to update interface"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating node interface {iface}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/nodes/{node}/networks/{iface}")
def delete_node_network(
    server_id: int,
    node: str,
    iface: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Delete a network interface from a node."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)
        result = client.delete_node_network(node, iface)
        if result.get("success"):
            logger.info(
                f"User {current_user.username} deleted interface {iface} "
                f"from {server.name}/{node}"
            )
            return JSONResponse(content=result)
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to delete interface"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting node interface {iface}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/networks/apply")
def apply_node_network(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Apply pending network configuration changes on a node."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)
        result = client.apply_node_network_config(node)
        if result.get("success"):
            logger.info(
                f"User {current_user.username} applied network config "
                f"on {server.name}/{node}"
            )
            return JSONResponse(content=result)
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to apply network config"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error applying node network config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/nodes/{node}/networks/revert")
def revert_node_network(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Revert pending network configuration changes on a node."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)
        result = client.revert_node_network_config(node)
        if result.get("success"):
            logger.info(
                f"User {current_user.username} reverted network config "
                f"on {server.name}/{node}"
            )
            return JSONResponse(content=result)
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to revert network config"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reverting node network config: {e}")
        raise HTTPException(status_code=500, detail=str(e))
