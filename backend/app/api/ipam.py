"""
IPAM API Routes
IP Address Management endpoints for networks, pools, and allocations.
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import IPAMNetwork, IPAMPool, IPAMAllocation, IPAMHistory, ProxmoxServer, User, VMInstance
from ..schemas import (
    IPAMNetworkCreate, IPAMNetworkUpdate, IPAMNetworkResponse, IPAMNetworkStats,
    IPAMPoolCreate, IPAMPoolUpdate, IPAMPoolResponse,
    IPAMAllocationCreate, IPAMAllocationUpdate, IPAMAllocationResponse,
    IPAMHistoryResponse, IPAMAutoAllocateRequest
)
from ..ipam_service import IPAMService
from ..auth import get_current_user, PermissionChecker
from ..template_helpers import add_i18n_context

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ==================== HTML Pages ====================

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ipam_dashboard(request: Request):
    """IPAM Dashboard page"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    context = {
        "request": request,
        "page_title": t('nav_ipam', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("ipam_dashboard.html", context)


@router.get("/networks", response_class=HTMLResponse, include_in_schema=False)
async def ipam_networks_page(request: Request):
    """IPAM Networks management page"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    context = {
        "request": request,
        "page_title": t('nav_ipam', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("ipam_networks.html", context)


@router.get("/network/{network_id}", response_class=HTMLResponse, include_in_schema=False)
async def ipam_network_detail(request: Request, network_id: int):
    """IPAM Network detail page with IP grid"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    context = {
        "request": request,
        "network_id": network_id,
        "page_title": t('nav_ipam', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("ipam_network_detail.html", context)


@router.get("/allocations", response_class=HTMLResponse, include_in_schema=False)
async def ipam_allocations_page(request: Request):
    """IPAM Allocations page"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    context = {
        "request": request,
        "page_title": t('nav_ipam', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("ipam_allocations.html", context)


@router.get("/history", response_class=HTMLResponse, include_in_schema=False)
async def ipam_history_page(request: Request):
    """IPAM History page"""
    from ..i18n import t
    lang = request.cookies.get("language", "en")
    
    context = {
        "request": request,
        "page_title": t('nav_ipam', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("ipam_history.html", context)


# ==================== Network API ====================

@router.get("/api/networks", response_model=List[IPAMNetworkResponse])
async def get_networks(
    is_active: Optional[bool] = None,
    proxmox_server_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get all IPAM networks"""
    query = db.query(IPAMNetwork)
    
    if is_active is not None:
        query = query.filter(IPAMNetwork.is_active == is_active)
    
    if proxmox_server_id:
        query = query.filter(IPAMNetwork.proxmox_server_id == proxmox_server_id)
    
    networks = query.order_by(IPAMNetwork.name).all()
    
    # Get server names for networks
    server_ids = [n.proxmox_server_id for n in networks if n.proxmox_server_id]
    servers = {}
    if server_ids:
        server_records = db.query(ProxmoxServer).filter(ProxmoxServer.id.in_(server_ids)).all()
        servers = {s.id: s.name for s in server_records}
    
    # Add computed stats to each network
    ipam = IPAMService(db)
    result = []
    for network in networks:
        stats = ipam.get_network_stats(network.id)
        network_dict = {
            **network.__dict__,
            'total_ips': stats.get('total_ips', 0),
            'used_ips': stats.get('allocated_ips', 0) + stats.get('reserved_ips', 0),
            'available_ips': stats.get('available_ips', 0),
            'utilization_percent': stats.get('utilization_percent', 0),
            'server_name': servers.get(network.proxmox_server_id) if network.proxmox_server_id else None
        }
        result.append(network_dict)
    
    return result


@router.get("/api/networks/{network_id}", response_model=IPAMNetworkResponse)
async def get_network(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get a specific IPAM network"""
    network = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    
    ipam = IPAMService(db)
    stats = ipam.get_network_stats(network_id)
    
    return {
        **network.__dict__,
        'total_ips': stats.get('total_ips', 0),
        'used_ips': stats.get('allocated_ips', 0) + stats.get('reserved_ips', 0),
        'available_ips': stats.get('available_ips', 0),
        'utilization_percent': stats.get('utilization_percent', 0)
    }


@router.get("/api/networks/{network_id}/stats", response_model=IPAMNetworkStats)
async def get_network_stats(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get detailed statistics for a network"""
    ipam = IPAMService(db)
    stats = ipam.get_network_stats(network_id)
    
    if not stats:
        raise HTTPException(status_code=404, detail="Network not found")
    
    return stats


@router.get("/api/networks/{network_id}/ip-map")
async def get_network_ip_map(
    network_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get IP address map for visualization"""
    ipam = IPAMService(db)
    ip_map = ipam.get_network_ip_map(network_id)
    
    if not ip_map:
        network = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
        if not network:
            raise HTTPException(status_code=404, detail="Network not found")
    
    return {"network_id": network_id, "ip_map": ip_map}


@router.post("/api/networks", response_model=IPAMNetworkResponse)
async def create_network(
    network_data: IPAMNetworkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Create a new IPAM network"""
    # Check for duplicate network CIDR
    existing = db.query(IPAMNetwork).filter(IPAMNetwork.network == network_data.network).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Network {network_data.network} already exists")
    
    network = IPAMNetwork(**network_data.model_dump())
    db.add(network)
    db.commit()
    db.refresh(network)
    
    logger.info(f"User {current_user.username} created network {network.name} ({network.network})")
    return network


@router.put("/api/networks/{network_id}", response_model=IPAMNetworkResponse)
async def update_network(
    network_id: int,
    network_data: IPAMNetworkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Update an IPAM network"""
    network = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    
    update_data = network_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(network, field, value)
    
    db.commit()
    db.refresh(network)
    
    logger.info(f"User {current_user.username} updated network {network.name}")
    return network


@router.delete("/api/networks/{network_id}")
async def delete_network(
    network_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Delete an IPAM network"""
    network = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    
    # Check for existing allocations
    alloc_count = db.query(IPAMAllocation).filter(IPAMAllocation.network_id == network_id).count()
    if alloc_count > 0 and not force:
        raise HTTPException(
            status_code=400, 
            detail=f"Network has {alloc_count} allocations. Use force=true to delete anyway."
        )
    
    # Delete related data
    db.query(IPAMAllocation).filter(IPAMAllocation.network_id == network_id).delete()
    db.query(IPAMPool).filter(IPAMPool.network_id == network_id).delete()
    db.query(IPAMHistory).filter(IPAMHistory.network_id == network_id).delete()
    db.delete(network)
    db.commit()
    
    logger.info(f"User {current_user.username} deleted network {network.name}")
    return {"message": f"Network {network.name} deleted"}


# ==================== Pool API ====================

@router.get("/api/pools", response_model=List[IPAMPoolResponse])
async def get_pools(
    network_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get all IP pools"""
    query = db.query(IPAMPool)
    
    if network_id:
        query = query.filter(IPAMPool.network_id == network_id)
    
    if is_active is not None:
        query = query.filter(IPAMPool.is_active == is_active)
    
    pools = query.order_by(IPAMPool.network_id, IPAMPool.range_start).all()
    
    # Add stats to each pool
    ipam = IPAMService(db)
    result = []
    for pool in pools:
        stats = ipam.get_pool_stats(pool.id)
        pool_dict = {
            **pool.__dict__,
            'total_ips': stats.get('total_ips', 0),
            'used_ips': stats.get('used_ips', 0),
            'available_ips': stats.get('available_ips', 0)
        }
        result.append(pool_dict)
    
    return result


@router.post("/api/pools", response_model=IPAMPoolResponse)
async def create_pool(
    pool_data: IPAMPoolCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Create a new IP pool"""
    # Validate network exists
    network = db.query(IPAMNetwork).filter(IPAMNetwork.id == pool_data.network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    
    # Validate range
    ipam = IPAMService(db)
    is_valid, error = ipam.validate_pool_range(
        pool_data.network_id, 
        pool_data.range_start, 
        pool_data.range_end
    )
    if not is_valid:
        raise HTTPException(status_code=400, detail=error)
    
    pool = IPAMPool(**pool_data.model_dump())
    db.add(pool)
    db.commit()
    db.refresh(pool)
    
    logger.info(f"User {current_user.username} created pool {pool.name} in network {network.name}")
    return pool


@router.put("/api/pools/{pool_id}", response_model=IPAMPoolResponse)
async def update_pool(
    pool_id: int,
    pool_data: IPAMPoolUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Update an IP pool"""
    pool = db.query(IPAMPool).filter(IPAMPool.id == pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    # Validate new range if provided
    update_data = pool_data.model_dump(exclude_unset=True)
    if 'range_start' in update_data or 'range_end' in update_data:
        ipam = IPAMService(db)
        is_valid, error = ipam.validate_pool_range(
            pool.network_id,
            update_data.get('range_start', pool.range_start),
            update_data.get('range_end', pool.range_end),
            exclude_pool_id=pool_id
        )
        if not is_valid:
            raise HTTPException(status_code=400, detail=error)
    
    for field, value in update_data.items():
        setattr(pool, field, value)
    
    db.commit()
    db.refresh(pool)
    
    logger.info(f"User {current_user.username} updated pool {pool.name}")
    return pool


@router.delete("/api/pools/{pool_id}")
async def delete_pool(
    pool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Delete an IP pool"""
    pool = db.query(IPAMPool).filter(IPAMPool.id == pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    
    # Update allocations to remove pool reference
    db.query(IPAMAllocation).filter(IPAMAllocation.pool_id == pool_id).update({IPAMAllocation.pool_id: None})
    db.delete(pool)
    db.commit()
    
    logger.info(f"User {current_user.username} deleted pool {pool.name}")
    return {"message": f"Pool {pool.name} deleted"}


# ==================== Allocation API ====================

@router.get("/api/allocations", response_model=List[IPAMAllocationResponse])
async def get_allocations(
    network_id: Optional[int] = None,
    pool_id: Optional[int] = None,
    status: Optional[str] = None,
    resource_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get IP allocations with filtering"""
    query = db.query(IPAMAllocation)
    
    if network_id:
        query = query.filter(IPAMAllocation.network_id == network_id)
    
    if pool_id:
        query = query.filter(IPAMAllocation.pool_id == pool_id)
    
    if status:
        query = query.filter(IPAMAllocation.status == status)
    
    if resource_type:
        query = query.filter(IPAMAllocation.resource_type == resource_type)
    
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (IPAMAllocation.ip_address.like(search_filter)) |
            (IPAMAllocation.resource_name.like(search_filter)) |
            (IPAMAllocation.hostname.like(search_filter))
        )
    
    allocations = query.order_by(IPAMAllocation.ip_address).offset(offset).limit(limit).all()
    
    # Add network/pool names
    networks = {n.id: n.name for n in db.query(IPAMNetwork).all()}
    pools = {p.id: p.name for p in db.query(IPAMPool).all()}
    
    result = []
    for alloc in allocations:
        alloc_dict = {
            **alloc.__dict__,
            'network_name': networks.get(alloc.network_id),
            'pool_name': pools.get(alloc.pool_id) if alloc.pool_id else None
        }
        result.append(alloc_dict)
    
    return result


@router.get("/api/allocations/{allocation_id}", response_model=IPAMAllocationResponse)
async def get_allocation(
    allocation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get a specific allocation"""
    allocation = db.query(IPAMAllocation).filter(IPAMAllocation.id == allocation_id).first()
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    
    network = db.query(IPAMNetwork).filter(IPAMNetwork.id == allocation.network_id).first()
    pool = db.query(IPAMPool).filter(IPAMPool.id == allocation.pool_id).first() if allocation.pool_id else None
    
    return {
        **allocation.__dict__,
        'network_name': network.name if network else None,
        'pool_name': pool.name if pool else None
    }


@router.post("/api/allocations", response_model=IPAMAllocationResponse)
async def create_allocation(
    alloc_data: IPAMAllocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Manually create an IP allocation"""
    ipam = IPAMService(db)
    allocation, error = ipam.allocate_ip(
        ip_address=alloc_data.ip_address,
        network_id=alloc_data.network_id,
        pool_id=alloc_data.pool_id,
        resource_type=alloc_data.resource_type,
        resource_id=alloc_data.resource_id,
        resource_name=alloc_data.resource_name,
        hostname=alloc_data.hostname,
        fqdn=alloc_data.fqdn,
        mac_address=alloc_data.mac_address,
        proxmox_server_id=alloc_data.proxmox_server_id,
        proxmox_vmid=alloc_data.proxmox_vmid,
        proxmox_node=alloc_data.proxmox_node,
        allocation_type=alloc_data.allocation_type,
        status=alloc_data.status,
        allocated_by=current_user.username,
        notes=alloc_data.notes
    )
    
    if error:
        raise HTTPException(status_code=400, detail=error)
    
    return allocation


@router.post("/api/allocations/auto", response_model=IPAMAllocationResponse)
async def auto_allocate_ip(
    request: IPAMAutoAllocateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Automatically allocate the next available IP"""
    ipam = IPAMService(db)
    allocation, error = ipam.auto_allocate_ip(
        network_id=request.network_id,
        pool_id=request.pool_id,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        resource_name=request.resource_name,
        hostname=request.hostname,
        allocated_by=current_user.username,
        notes=request.notes
    )
    
    if error:
        raise HTTPException(status_code=400, detail=error)
    
    return allocation


@router.put("/api/allocations/{allocation_id}", response_model=IPAMAllocationResponse)
async def update_allocation(
    allocation_id: int,
    alloc_data: IPAMAllocationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Update an allocation"""
    allocation = db.query(IPAMAllocation).filter(IPAMAllocation.id == allocation_id).first()
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    
    ipam = IPAMService(db)
    updated, error = ipam.update_allocation(
        allocation.ip_address,
        updated_by=current_user.username,
        **alloc_data.model_dump(exclude_unset=True)
    )
    
    if error:
        raise HTTPException(status_code=400, detail=error)
    
    return updated


@router.delete("/api/allocations/{allocation_id}")
async def delete_allocation(
    allocation_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """Release/delete an IP allocation"""
    allocation = db.query(IPAMAllocation).filter(IPAMAllocation.id == allocation_id).first()
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    
    ipam = IPAMService(db)
    success, error = ipam.release_ip(
        allocation.ip_address,
        released_by=current_user.username,
        reason=reason
    )
    
    if not success:
        raise HTTPException(status_code=400, detail=error)
    
    return {"message": f"IP {allocation.ip_address} released"}


@router.get("/api/allocations/next-available/{network_id}")
async def get_next_available_ip(
    network_id: int,
    pool_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get the next available IP in a network (without allocating)"""
    ipam = IPAMService(db)
    ip = ipam.get_next_available_ip(network_id, pool_id)
    
    if not ip:
        raise HTTPException(status_code=404, detail="No available IP addresses")
    
    return {"next_available_ip": ip}


# ==================== History API ====================

@router.get("/api/history", response_model=List[IPAMHistoryResponse])
async def get_history(
    network_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get IPAM history"""
    query = db.query(IPAMHistory)
    
    if network_id:
        query = query.filter(IPAMHistory.network_id == network_id)
    
    if ip_address:
        query = query.filter(IPAMHistory.ip_address == ip_address)
    
    if action:
        query = query.filter(IPAMHistory.action == action)
    
    return query.order_by(IPAMHistory.performed_at.desc()).limit(limit).all()


@router.get("/api/history/ip/{ip_address}", response_model=List[IPAMHistoryResponse])
async def get_ip_history(
    ip_address: str,
    limit: int = Query(default=50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get history for a specific IP address"""
    ipam = IPAMService(db)
    return ipam.get_ip_history(ip_address, limit)


# ==================== Utility API ====================

@router.get("/api/conflicts")
async def detect_conflicts(
    network_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Detect IP address conflicts"""
    ipam = IPAMService(db)
    conflicts = ipam.detect_conflicts(network_id)
    return {"conflicts": conflicts, "count": len(conflicts)}


@router.get("/api/summary")
async def get_ipam_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.view"))
):
    """Get overall IPAM summary"""
    networks_count = db.query(IPAMNetwork).filter(IPAMNetwork.is_active == True).count()
    pools_count = db.query(IPAMPool).filter(IPAMPool.is_active == True).count()
    
    total_allocated = db.query(IPAMAllocation).filter(IPAMAllocation.status == 'allocated').count()
    total_reserved = db.query(IPAMAllocation).filter(IPAMAllocation.status == 'reserved').count()
    
    # Count by resource type
    vm_count = db.query(IPAMAllocation).filter(IPAMAllocation.resource_type == 'vm').count()
    lxc_count = db.query(IPAMAllocation).filter(IPAMAllocation.resource_type == 'lxc').count()
    physical_count = db.query(IPAMAllocation).filter(IPAMAllocation.resource_type == 'physical').count()
    
    # Recent activity
    recent_allocations = db.query(IPAMHistory).filter(
        IPAMHistory.action == 'allocated'
    ).order_by(IPAMHistory.performed_at.desc()).limit(5).all()
    
    return {
        'networks_count': networks_count,
        'pools_count': pools_count,
        'total_allocated': total_allocated,
        'total_reserved': total_reserved,
        'vm_count': vm_count,
        'lxc_count': lxc_count,
        'physical_count': physical_count,
        'recent_allocations': [
            {
                'ip': h.ip_address,
                'resource': h.resource_name,
                'by': h.performed_by,
                'at': h.performed_at.isoformat() if h.performed_at else None
            } for h in recent_allocations
        ]
    }


# ==================== Maintenance API ====================

@router.get("/api/orphans")
async def get_orphan_allocations(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """
    Get orphan IPAM allocations - IPs allocated to VMs that no longer exist.
    """
    orphans = []
    
    # 1. Allocations with proxmox_vmid but VM doesn't exist or is deleted
    allocations_with_vmid = db.query(IPAMAllocation).filter(
        IPAMAllocation.proxmox_vmid.isnot(None),
        IPAMAllocation.proxmox_server_id.isnot(None),
        IPAMAllocation.status.in_(['allocated', 'reserved'])
    ).all()
    
    for alloc in allocations_with_vmid:
        vm = db.query(VMInstance).filter(
            VMInstance.vmid == alloc.proxmox_vmid,
            VMInstance.server_id == alloc.proxmox_server_id,
            VMInstance.deleted_at.is_(None)
        ).first()
        
        if not vm:
            orphans.append({
                'id': alloc.id,
                'ip_address': alloc.ip_address,
                'resource_name': alloc.resource_name,
                'resource_type': alloc.resource_type,
                'proxmox_vmid': alloc.proxmox_vmid,
                'proxmox_server_id': alloc.proxmox_server_id,
                'reason': 'VM/LXC not found or deleted'
            })
    
    # 2. Allocations to non-existent servers
    allocations_with_server = db.query(IPAMAllocation).filter(
        IPAMAllocation.proxmox_server_id.isnot(None),
        IPAMAllocation.status.in_(['allocated', 'reserved'])
    ).all()
    
    server_ids = {a.proxmox_server_id for a in allocations_with_server}
    existing_servers = {s.id for s in db.query(ProxmoxServer.id).filter(ProxmoxServer.id.in_(server_ids)).all()}
    
    for alloc in allocations_with_server:
        if alloc.proxmox_server_id not in existing_servers:
            # Check if not already in orphans
            if not any(o['id'] == alloc.id for o in orphans):
                orphans.append({
                    'id': alloc.id,
                    'ip_address': alloc.ip_address,
                    'resource_name': alloc.resource_name,
                    'resource_type': alloc.resource_type,
                    'proxmox_vmid': alloc.proxmox_vmid,
                    'proxmox_server_id': alloc.proxmox_server_id,
                    'reason': f'Server {alloc.proxmox_server_id} not found'
                })
    
    return {
        'orphans': orphans,
        'count': len(orphans)
    }


@router.post("/api/cleanup-orphans")
async def cleanup_orphan_allocations(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """
    Clean up orphan IPAM allocations - release IPs for VMs that no longer exist.
    """
    ipam = IPAMService(db)
    released = []
    errors = []
    
    # Get orphans
    orphans_result = await get_orphan_allocations(db, current_user)
    orphans = orphans_result['orphans']
    
    for orphan in orphans:
        try:
            success, error = ipam.release_ip(
                ip_address=orphan['ip_address'],
                released_by=current_user.username,
                reason=f"Orphan cleanup: {orphan['reason']}"
            )
            if success:
                released.append(orphan['ip_address'])
                logger.info(f"Released orphan IP {orphan['ip_address']} by {current_user.username}")
            else:
                errors.append({'ip': orphan['ip_address'], 'error': error})
        except Exception as e:
            errors.append({'ip': orphan['ip_address'], 'error': str(e)})
    
    return {
        'released': released,
        'released_count': len(released),
        'errors': errors,
        'error_count': len(errors)
    }


@router.get("/api/unlinked")
async def get_unlinked_allocations(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """
    Get IPAM allocations without proxmox_vmid that can be linked to existing VMs by name.
    """
    unlinked = []
    
    # Allocations without proxmox_vmid
    allocations = db.query(IPAMAllocation).filter(
        IPAMAllocation.proxmox_vmid.is_(None),
        IPAMAllocation.status.in_(['allocated', 'reserved'])
    ).all()
    
    for alloc in allocations:
        # Try to find matching VM by name
        vm = db.query(VMInstance).filter(
            VMInstance.name.ilike(alloc.resource_name),
            VMInstance.deleted_at.is_(None)
        ).first()
        
        unlinked.append({
            'id': alloc.id,
            'ip_address': alloc.ip_address,
            'resource_name': alloc.resource_name,
            'resource_type': alloc.resource_type,
            'can_link': vm is not None,
            'suggested_vmid': vm.vmid if vm else None,
            'suggested_server_id': vm.server_id if vm else None,
            'suggested_server_name': None  # Will be filled below
        })
        
        if vm:
            server = db.query(ProxmoxServer).filter(ProxmoxServer.id == vm.server_id).first()
            unlinked[-1]['suggested_server_name'] = server.name if server else None
    
    return {
        'unlinked': unlinked,
        'count': len(unlinked),
        'linkable_count': sum(1 for u in unlinked if u['can_link'])
    }


@router.post("/api/link-allocations")
async def link_allocations_to_vms(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("ipam.manage"))
):
    """
    Link IPAM allocations to real VMs by matching resource_name.
    """
    linked = []
    not_found = []
    
    # Get unlinked allocations
    allocations = db.query(IPAMAllocation).filter(
        IPAMAllocation.proxmox_vmid.is_(None),
        IPAMAllocation.status.in_(['allocated', 'reserved'])
    ).all()
    
    for alloc in allocations:
        # Find VM by name (case-insensitive)
        vm = db.query(VMInstance).filter(
            VMInstance.name.ilike(alloc.resource_name),
            VMInstance.deleted_at.is_(None)
        ).first()
        
        if vm:
            # Update allocation with VM info
            alloc.proxmox_vmid = vm.vmid
            alloc.proxmox_server_id = vm.server_id
            alloc.resource_id = vm.vmid
            
            linked.append({
                'ip_address': alloc.ip_address,
                'resource_name': alloc.resource_name,
                'vmid': vm.vmid,
                'server_id': vm.server_id
            })
            logger.info(f"Linked IP {alloc.ip_address} to VM {vm.vmid} on server {vm.server_id}")
        else:
            not_found.append({
                'ip_address': alloc.ip_address,
                'resource_name': alloc.resource_name
            })
    
    if linked:
        db.commit()
    
    return {
        'linked': linked,
        'linked_count': len(linked),
        'not_found': not_found,
        'not_found_count': len(not_found)
    }

