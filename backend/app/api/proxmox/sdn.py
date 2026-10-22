from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from loguru import logger
from typing import Optional

from ...db import get_db
from ...models import ProxmoxServer, User, IPAMNetwork
from ...auth import PermissionChecker
from ...logging_service import LoggingService
from ._helpers import _get_proxmox_client
from ...proxmox import _run_in_executor

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


# ==================== SDN (Software Defined Networking) ====================

@router.get("/api/servers/{server_id}/sdn/status")
def get_sdn_status(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """Check if SDN is available on the server"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
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
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """Get all SDN zones"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        zones = client.get_sdn_zones()
        return JSONResponse(content={"zones": zones})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting SDN zones for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/servers/{server_id}/sdn/dns")
def get_sdn_dns(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """Get SDN DNS server entries (zone `dns` option expects one of their IDs)"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    try:
        client = _get_proxmox_client(server)
        return JSONResponse(content={"dns": client.get_sdn_dns()})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting SDN DNS entries for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/zones")
async def create_sdn_zone(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
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
        client = _get_proxmox_client(server)
        
        # Pass additional options from request
        kwargs = {k: v for k, v in data.items() if k not in ['zone', 'type']}
        result = await _run_in_executor(client.create_sdn_zone, zone_name, zone_type, **kwargs)
        
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
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Delete an SDN zone"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
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
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """Get all SDN VNets"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        vnets = client.get_sdn_vnets()
        return JSONResponse(content={"vnets": vnets})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting SDN vnets for server {server_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/vnets")
async def create_sdn_vnet(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
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
        client = _get_proxmox_client(server)
        
        result = await _run_in_executor(client.create_sdn_vnet, vnet_name, zone, tag=tag, alias=alias, vlanaware=vlanaware)
        
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
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Delete an SDN VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
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
    current_user: User = Depends(PermissionChecker("network:view"))
):
    """Get subnets for a VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
        subnets = client.get_sdn_subnets(vnet)
        return JSONResponse(content={"subnets": subnets})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting SDN subnets for vnet {vnet}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/servers/{server_id}/sdn/vnets/{vnet}/subnets")
async def create_sdn_subnet(
    server_id: int,
    vnet: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
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
    create_ipam_network = data.get('create_ipam_network', False)

    if not subnet:
        raise HTTPException(status_code=400, detail="Subnet CIDR is required")

    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.create_sdn_subnet, vnet, subnet, gateway=gateway, snat=snat, dnszoneprefix=dnszoneprefix)

        if result.get('success'):
            logger.info(f"User {current_user.username} created subnet {subnet} in vnet {vnet}")
            response = dict(result)

            # Optionally create a matching IPAMNetwork
            if create_ipam_network:
                existing = db.query(IPAMNetwork).filter(IPAMNetwork.network == subnet).first()
                if existing:
                    response['ipam_network_id'] = existing.id
                    response['ipam_already_existed'] = True
                else:
                    ipam_net = IPAMNetwork(
                        name=f"{vnet} — {subnet}",
                        network=subnet,
                        gateway=gateway or None,
                        proxmox_server_id=server_id,
                        proxmox_bridge=vnet,
                        is_active=True,
                    )
                    db.add(ipam_net)
                    db.commit()
                    db.refresh(ipam_net)
                    response['ipam_network_id'] = ipam_net.id
                    response['ipam_created'] = True
                    logger.info(
                        f"User {current_user.username} auto-created IPAM network "
                        f"{subnet} (id={ipam_net.id}) linked to vnet {vnet}"
                    )

            return JSONResponse(content=response)
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
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Apply pending SDN changes"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    
    try:
        client = _get_proxmox_client(server)
        
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


@router.put("/api/servers/{server_id}/sdn/zones/{zone}")
async def update_sdn_zone(
    server_id: int,
    zone: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Update an existing SDN zone"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.update_sdn_zone, zone, **data)
        if result.get('success'):
            logger.info(f"User {current_user.username} updated SDN zone: {zone}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to update zone'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating SDN zone {zone}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/servers/{server_id}/sdn/vnets/{vnet}")
async def update_sdn_vnet(
    server_id: int,
    vnet: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Update an existing SDN VNet"""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    data = await request.json()
    try:
        client = _get_proxmox_client(server)
        result = await _run_in_executor(client.update_sdn_vnet, vnet, **data)
        if result.get('success'):
            logger.info(f"User {current_user.username} updated SDN vnet: {vnet}")
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to update vnet'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating SDN vnet {vnet}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/servers/{server_id}/sdn/vnets/{vnet}/subnets/{subnet_cidr:path}")
def delete_sdn_subnet(
    server_id: int,
    vnet: str,
    subnet_cidr: str,
    delete_ipam_network: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("network:manage"))
):
    """Delete a subnet from a VNet. subnet_cidr uses '-' instead of '/' (e.g. 10.0.0.0-24)."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    # Accept both dash-encoded and slash CIDR
    subnet_slash = subnet_cidr.replace("-", "/") if "-" in subnet_cidr else subnet_cidr

    try:
        client = _get_proxmox_client(server)
        result = client.delete_sdn_subnet(vnet, subnet_slash)
        if result.get('success'):
            logger.info(f"User {current_user.username} deleted subnet {subnet_slash} from vnet {vnet}")
            response = dict(result)

            if delete_ipam_network:
                ipam_net = db.query(IPAMNetwork).filter(
                    IPAMNetwork.network == subnet_slash,
                    IPAMNetwork.proxmox_server_id == server_id,
                ).first()
                if ipam_net:
                    db.delete(ipam_net)
                    db.commit()
                    response['ipam_network_deleted'] = True
                    logger.info(
                        f"User {current_user.username} deleted IPAM network "
                        f"{subnet_slash} (id={ipam_net.id})"
                    )

            return JSONResponse(content=response)
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to delete subnet'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting SDN subnet {subnet_slash}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
