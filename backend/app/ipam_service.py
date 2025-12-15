"""
IPAM Service - IP Address Management Service
Provides functions for managing IP addresses, networks, pools, and allocations.
"""

import ipaddress
import logging
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from .models import IPAMNetwork, IPAMPool, IPAMAllocation, IPAMHistory


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


logger = logging.getLogger(__name__)


class IPAMService:
    """Service class for IP Address Management operations"""
    
    def __init__(self, db: Session):
        self.db = db
    
    # ==================== Network Operations ====================
    
    def get_network_stats(self, network_id: int) -> Dict[str, Any]:
        """
        Get statistics for a network including IP utilization.
        
        Args:
            network_id: Network ID
            
        Returns:
            Dictionary with network statistics
        """
        network = self.db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
        if not network:
            return {}
        
        # Parse network CIDR
        try:
            net = ipaddress.ip_network(network.network, strict=False)
            # Exclude network and broadcast addresses for usable IPs
            total_ips = net.num_addresses - 2 if net.num_addresses > 2 else net.num_addresses
        except ValueError:
            total_ips = 0
        
        # Count allocations by status and type
        allocations = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.network_id == network_id
        ).all()
        
        allocated_ips = len([a for a in allocations if a.status == 'allocated'])
        reserved_ips = len([a for a in allocations if a.status == 'reserved'])
        
        vms_count = len([a for a in allocations if a.resource_type == 'vm'])
        lxc_count = len([a for a in allocations if a.resource_type == 'lxc'])
        physical_count = len([a for a in allocations if a.resource_type == 'physical'])
        other_count = len([a for a in allocations if a.resource_type not in ('vm', 'lxc', 'physical', None)])
        
        pools_count = self.db.query(IPAMPool).filter(
            IPAMPool.network_id == network_id,
            IPAMPool.is_active == True
        ).count()
        
        available_ips = total_ips - allocated_ips - reserved_ips
        utilization = (allocated_ips + reserved_ips) / total_ips * 100 if total_ips > 0 else 0
        
        return {
            'network_id': network_id,
            'network_name': network.name,
            'network_cidr': network.network,
            'total_ips': total_ips,
            'allocated_ips': allocated_ips,
            'reserved_ips': reserved_ips,
            'available_ips': available_ips,
            'utilization_percent': round(utilization, 2),
            'pools_count': pools_count,
            'vms_count': vms_count,
            'lxc_count': lxc_count,
            'physical_count': physical_count,
            'other_count': other_count
        }
    
    def get_network_ip_map(self, network_id: int) -> List[Dict[str, Any]]:
        """
        Get a map of all IP addresses in a network with their status.
        
        Args:
            network_id: Network ID
            
        Returns:
            List of IP address info dictionaries
        """
        network = self.db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
        if not network:
            return []
        
        try:
            net = ipaddress.ip_network(network.network, strict=False)
        except ValueError:
            return []
        
        # Get all allocations for this network
        allocations = {
            a.ip_address: a for a in 
            self.db.query(IPAMAllocation).filter(IPAMAllocation.network_id == network_id).all()
        }
        
        ip_map = []
        for ip in net.hosts():  # Excludes network and broadcast
            ip_str = str(ip)
            allocation = allocations.get(ip_str)
            
            # Check if this is the gateway
            is_gateway = ip_str == network.gateway
            
            ip_info = {
                'ip_address': ip_str,
                'is_gateway': is_gateway,
                'status': 'gateway' if is_gateway else (allocation.status if allocation else 'available'),
                'resource_type': allocation.resource_type if allocation else None,
                'resource_name': allocation.resource_name if allocation else None,
                'hostname': allocation.hostname if allocation else None,
                'allocation_id': allocation.id if allocation else None,
                'proxmox_vmid': allocation.proxmox_vmid if allocation else None,
                'last_seen': allocation.last_seen.isoformat() if allocation and allocation.last_seen else None
            }
            ip_map.append(ip_info)
        
        return ip_map
    
    # ==================== IP Allocation Operations ====================
    
    def get_next_available_ip(
        self, 
        network_id: int, 
        pool_id: Optional[int] = None,
        prefer_sequential: bool = True
    ) -> Optional[str]:
        """
        Get the next available IP address from a network or specific pool.
        
        Args:
            network_id: Network ID
            pool_id: Optional specific pool ID
            prefer_sequential: If True, prefer sequential IPs; if False, can use any gap
            
        Returns:
            Available IP address string or None if no IPs available
        """
        network = self.db.query(IPAMNetwork).filter(
            IPAMNetwork.id == network_id,
            IPAMNetwork.is_active == True
        ).first()
        
        if not network:
            logger.warning(f"Network {network_id} not found or inactive")
            return None
        
        # Get pool(s) to search
        if pool_id:
            pools = self.db.query(IPAMPool).filter(
                IPAMPool.id == pool_id,
                IPAMPool.network_id == network_id,
                IPAMPool.is_active == True,
                IPAMPool.auto_assign == True
            ).all()
        else:
            pools = self.db.query(IPAMPool).filter(
                IPAMPool.network_id == network_id,
                IPAMPool.is_active == True,
                IPAMPool.auto_assign == True
            ).order_by(IPAMPool.id).all()
        
        if not pools:
            logger.warning(f"No active auto-assign pools found for network {network_id}")
            return None
        
        # Get all allocated IPs in this network
        allocated_ips = set(
            a.ip_address for a in 
            self.db.query(IPAMAllocation).filter(
                IPAMAllocation.network_id == network_id,
                IPAMAllocation.status.in_(['allocated', 'reserved'])
            ).all()
        )
        
        # Also exclude gateway
        if network.gateway:
            allocated_ips.add(network.gateway)
        
        # Search through pools for available IP
        for pool in pools:
            try:
                start_ip = ipaddress.ip_address(pool.range_start)
                end_ip = ipaddress.ip_address(pool.range_end)
                
                for ip_int in range(int(start_ip), int(end_ip) + 1):
                    ip = str(ipaddress.ip_address(ip_int))
                    if ip not in allocated_ips:
                        logger.info(f"Found available IP: {ip} in pool {pool.name}")
                        return ip
                        
            except ValueError as e:
                logger.error(f"Invalid IP range in pool {pool.id}: {e}")
                continue
        
        logger.warning(f"No available IPs found in network {network_id}")
        return None
    
    def allocate_ip(
        self,
        ip_address: str,
        network_id: int,
        pool_id: Optional[int] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[int] = None,
        resource_name: Optional[str] = None,
        hostname: Optional[str] = None,
        fqdn: Optional[str] = None,
        mac_address: Optional[str] = None,
        proxmox_server_id: Optional[int] = None,
        proxmox_vmid: Optional[int] = None,
        proxmox_node: Optional[str] = None,
        allocation_type: str = "static",
        status: str = "allocated",
        allocated_by: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Tuple[Optional[IPAMAllocation], Optional[str]]:
        """
        Allocate an IP address.
        
        Args:
            ip_address: IP address to allocate
            network_id: Network ID
            ... other optional parameters
            
        Returns:
            Tuple of (allocation object, error message)
        """
        # Validate IP is not already allocated
        existing = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.ip_address == ip_address
        ).first()
        
        if existing:
            return None, f"IP {ip_address} is already allocated to {existing.resource_name or 'unknown'}"
        
        # Validate IP belongs to the network
        network = self.db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
        if not network:
            return None, f"Network {network_id} not found"
        
        try:
            net = ipaddress.ip_network(network.network, strict=False)
            ip = ipaddress.ip_address(ip_address)
            if ip not in net:
                return None, f"IP {ip_address} is not within network {network.network}"
        except ValueError as e:
            return None, f"Invalid IP or network: {e}"
        
        # Check if IP is the gateway
        if ip_address == network.gateway:
            return None, f"IP {ip_address} is the gateway for this network"
        
        # Auto-detect pool if not specified
        if not pool_id:
            pools = self.db.query(IPAMPool).filter(
                IPAMPool.network_id == network_id,
                IPAMPool.is_active == True
            ).all()
            
            for pool in pools:
                try:
                    start = ipaddress.ip_address(pool.range_start)
                    end = ipaddress.ip_address(pool.range_end)
                    if start <= ip <= end:
                        pool_id = pool.id
                        break
                except ValueError:
                    continue
        
        # Build FQDN if not provided
        if not fqdn and hostname and network.dns_domain:
            fqdn = f"{hostname}.{network.dns_domain}"
        
        # Create allocation
        allocation = IPAMAllocation(
            network_id=network_id,
            pool_id=pool_id,
            ip_address=ip_address,
            mac_address=mac_address,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            proxmox_server_id=proxmox_server_id,
            proxmox_vmid=proxmox_vmid,
            proxmox_node=proxmox_node,
            status=status,
            allocation_type=allocation_type,
            hostname=hostname,
            fqdn=fqdn,
            allocated_by=allocated_by,
            notes=notes
        )
        
        self.db.add(allocation)
        
        # Log to history
        self._log_history(
            ip_address=ip_address,
            network_id=network_id,
            action="allocated",
            new_value={
                'resource_type': resource_type,
                'resource_name': resource_name,
                'status': status
            },
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            performed_by=allocated_by
        )
        
        self.db.commit()
        self.db.refresh(allocation)
        
        logger.info(f"Allocated IP {ip_address} to {resource_name or 'unknown'} by {allocated_by or 'system'}")
        return allocation, None
    
    def auto_allocate_ip(
        self,
        network_id: int,
        pool_id: Optional[int] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[int] = None,
        resource_name: Optional[str] = None,
        hostname: Optional[str] = None,
        proxmox_server_id: Optional[int] = None,
        proxmox_vmid: Optional[int] = None,
        proxmox_node: Optional[str] = None,
        allocated_by: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Tuple[Optional[IPAMAllocation], Optional[str]]:
        """
        Automatically allocate the next available IP address.
        
        Returns:
            Tuple of (allocation object, error message)
        """
        ip_address = self.get_next_available_ip(network_id, pool_id)
        
        if not ip_address:
            return None, f"No available IP addresses in network {network_id}"
        
        return self.allocate_ip(
            ip_address=ip_address,
            network_id=network_id,
            pool_id=pool_id,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            hostname=hostname,
            proxmox_server_id=proxmox_server_id,
            proxmox_vmid=proxmox_vmid,
            proxmox_node=proxmox_node,
            allocated_by=allocated_by,
            notes=notes
        )
    
    def release_ip(
        self,
        ip_address: str,
        released_by: Optional[str] = None,
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Release an IP address allocation.
        
        Args:
            ip_address: IP address to release
            released_by: Username performing the release
            reason: Reason for release
            
        Returns:
            Tuple of (success, error message)
        """
        allocation = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.ip_address == ip_address
        ).first()
        
        if not allocation:
            return False, f"IP {ip_address} is not allocated"
        
        # Store old data for history
        old_data = {
            'resource_type': allocation.resource_type,
            'resource_name': allocation.resource_name,
            'status': allocation.status,
            'proxmox_vmid': allocation.proxmox_vmid
        }
        
        # Log to history before deletion
        self._log_history(
            ip_address=ip_address,
            network_id=allocation.network_id,
            action="released",
            old_value=old_data,
            resource_type=allocation.resource_type,
            resource_id=allocation.resource_id,
            resource_name=allocation.resource_name,
            performed_by=released_by,
            notes=reason
        )
        
        self.db.delete(allocation)
        self.db.commit()
        
        logger.info(f"Released IP {ip_address} by {released_by or 'system'}")
        return True, None
    
    def release_ip_by_vmid(
        self,
        proxmox_server_id: int,
        proxmox_vmid: int,
        released_by: Optional[str] = None,
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Release IP allocation by Proxmox VM/container ID.
        Used when VM/container is deleted.
        
        Args:
            proxmox_server_id: Proxmox server ID
            proxmox_vmid: VM/container ID
            released_by: Username performing the release
            reason: Reason for release
            
        Returns:
            Tuple of (success, error message or released IP)
        """
        # First try to find by proxmox_server_id and proxmox_vmid
        allocation = self.find_allocation_by_resource(proxmox_server_id, proxmox_vmid)
        
        # Fallback: search by resource_id if proxmox fields are not set
        if not allocation:
            allocation = self.db.query(IPAMAllocation).filter(
                IPAMAllocation.resource_id == proxmox_vmid,
                IPAMAllocation.status.in_(['allocated', 'reserved'])
            ).first()
            if allocation:
                logger.debug(f"Found allocation by resource_id={proxmox_vmid} (legacy)")
        
        if not allocation:
            logger.debug(f"No IPAM allocation found for server {proxmox_server_id}, vmid {proxmox_vmid}")
            return False, None  # Not an error, just no allocation to release
        
        ip_address = allocation.ip_address
        success, error = self.release_ip(ip_address, released_by, reason)
        
        if success:
            logger.info(f"Auto-released IP {ip_address} for deleted VM/container {proxmox_vmid}")
            return True, ip_address
        
        return False, error
    
    def update_allocation(
        self,
        ip_address: str,
        updated_by: Optional[str] = None,
        **kwargs
    ) -> Tuple[Optional[IPAMAllocation], Optional[str]]:
        """
        Update an existing IP allocation.
        
        Args:
            ip_address: IP address to update
            updated_by: Username performing update
            **kwargs: Fields to update
            
        Returns:
            Tuple of (updated allocation, error message)
        """
        allocation = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.ip_address == ip_address
        ).first()
        
        if not allocation:
            return None, f"IP {ip_address} is not allocated"
        
        # Store old values for history
        old_value = {}
        new_value = {}
        
        allowed_fields = [
            'pool_id', 'mac_address', 'resource_type', 'resource_id', 'resource_name',
            'proxmox_server_id', 'proxmox_vmid', 'proxmox_node', 'status', 'allocation_type',
            'hostname', 'fqdn', 'dns_ptr_record', 'expires_at', 'notes', 'last_seen'
        ]
        
        for field, value in kwargs.items():
            if field in allowed_fields and hasattr(allocation, field):
                old_val = getattr(allocation, field)
                if old_val != value:
                    old_value[field] = old_val
                    new_value[field] = value
                    setattr(allocation, field, value)
        
        if old_value:
            self._log_history(
                ip_address=ip_address,
                network_id=allocation.network_id,
                action="modified",
                old_value=old_value,
                new_value=new_value,
                resource_type=allocation.resource_type,
                resource_id=allocation.resource_id,
                resource_name=allocation.resource_name,
                performed_by=updated_by
            )
        
        self.db.commit()
        self.db.refresh(allocation)
        
        return allocation, None
    
    def find_allocation_by_resource(
        self,
        proxmox_server_id: int,
        proxmox_vmid: int
    ) -> Optional[IPAMAllocation]:
        """Find active allocation by Proxmox VM/container"""
        return self.db.query(IPAMAllocation).filter(
            IPAMAllocation.proxmox_server_id == proxmox_server_id,
            IPAMAllocation.proxmox_vmid == proxmox_vmid,
            IPAMAllocation.status.in_(['allocated', 'reserved'])
        ).first()
    
    def release_all_by_server(self, proxmox_server_id: int, released_by: str = None) -> int:
        """
        Release all IP allocations for a Proxmox server.
        Used when server is deleted.
        
        Args:
            proxmox_server_id: Proxmox server ID
            released_by: Username performing the release
            
        Returns:
            Number of released allocations
        """
        allocations = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.proxmox_server_id == proxmox_server_id,
            IPAMAllocation.status.in_(['allocated', 'reserved'])
        ).all()
        
        released_count = 0
        for alloc in allocations:
            try:
                self._log_history(
                    ip_address=alloc.ip_address,
                    network_id=alloc.network_id,
                    action="released",
                    old_value={
                        'resource_type': alloc.resource_type,
                        'resource_name': alloc.resource_name,
                        'proxmox_vmid': alloc.proxmox_vmid
                    },
                    resource_type=alloc.resource_type,
                    resource_id=alloc.resource_id,
                    resource_name=alloc.resource_name,
                    performed_by=released_by,
                    notes=f"Proxmox server {proxmox_server_id} deleted"
                )
                self.db.delete(alloc)
                released_count += 1
                logger.info(f"Released IP {alloc.ip_address} (server {proxmox_server_id} deleted)")
            except Exception as e:
                logger.warning(f"Failed to release IP {alloc.ip_address}: {e}")
        
        if released_count > 0:
            self.db.commit()
        
        return released_count
    
    # ==================== Pool Operations ====================
    
    def get_pool_stats(self, pool_id: int) -> Dict[str, Any]:
        """Get statistics for a pool"""
        pool = self.db.query(IPAMPool).filter(IPAMPool.id == pool_id).first()
        if not pool:
            return {}
        
        try:
            start = ipaddress.ip_address(pool.range_start)
            end = ipaddress.ip_address(pool.range_end)
            total_ips = int(end) - int(start) + 1
        except ValueError:
            total_ips = 0
        
        used_ips = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.pool_id == pool_id,
            IPAMAllocation.status.in_(['allocated', 'reserved'])
        ).count()
        
        return {
            'pool_id': pool_id,
            'pool_name': pool.name,
            'total_ips': total_ips,
            'used_ips': used_ips,
            'available_ips': total_ips - used_ips
        }
    
    def validate_pool_range(
        self,
        network_id: int,
        range_start: str,
        range_end: str,
        exclude_pool_id: Optional[int] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that a pool range is valid and doesn't overlap with existing pools.
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        network = self.db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
        if not network:
            return False, "Network not found"
        
        try:
            net = ipaddress.ip_network(network.network, strict=False)
            start = ipaddress.ip_address(range_start)
            end = ipaddress.ip_address(range_end)
        except ValueError as e:
            return False, f"Invalid IP format: {e}"
        
        # Validate range is within network
        if start not in net or end not in net:
            return False, f"Range must be within network {network.network}"
        
        # Validate start <= end
        if start > end:
            return False, "Range start must be less than or equal to range end"
        
        # Check for overlaps with existing pools
        existing_pools = self.db.query(IPAMPool).filter(
            IPAMPool.network_id == network_id,
            IPAMPool.id != exclude_pool_id if exclude_pool_id else True
        ).all()
        
        for pool in existing_pools:
            try:
                pool_start = ipaddress.ip_address(pool.range_start)
                pool_end = ipaddress.ip_address(pool.range_end)
                
                # Check overlap
                if not (end < pool_start or start > pool_end):
                    return False, f"Range overlaps with existing pool '{pool.name}' ({pool.range_start}-{pool.range_end})"
            except ValueError:
                continue
        
        return True, None
    
    # ==================== Sync & Scan Operations ====================
    
    def sync_from_proxmox_vm(
        self,
        network_id: int,
        proxmox_server_id: int,
        vmid: int,
        vm_name: str,
        vm_type: str,
        ip_address: str,
        node: str,
        mac_address: Optional[str] = None,
        synced_by: str = "system"
    ) -> Tuple[Optional[IPAMAllocation], Optional[str]]:
        """
        Sync/create allocation for a Proxmox VM.
        Called when discovering VMs from Proxmox.
        """
        # Check if already allocated
        existing = self.db.query(IPAMAllocation).filter(
            IPAMAllocation.ip_address == ip_address
        ).first()
        
        if existing:
            # Update existing allocation
            if existing.proxmox_vmid != vmid or existing.proxmox_server_id != proxmox_server_id:
                return self.update_allocation(
                    ip_address,
                    updated_by=synced_by,
                    proxmox_server_id=proxmox_server_id,
                    proxmox_vmid=vmid,
                    proxmox_node=node,
                    resource_type=vm_type,
                    resource_name=vm_name,
                    mac_address=mac_address,
                    last_seen=utcnow()
                )
            else:
                # Just update last_seen
                existing.last_seen = utcnow()
                self.db.commit()
                return existing, None
        
        # Create new allocation
        return self.allocate_ip(
            ip_address=ip_address,
            network_id=network_id,
            resource_type=vm_type,
            resource_name=vm_name,
            proxmox_server_id=proxmox_server_id,
            proxmox_vmid=vmid,
            proxmox_node=node,
            mac_address=mac_address,
            allocated_by=synced_by,
            notes="Auto-synced from Proxmox"
        )
    
    def detect_conflicts(self, network_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Detect IP address conflicts.
        
        Returns:
            List of conflict info dictionaries
        """
        # This would typically involve scanning the network
        # For now, we check for duplicate allocations
        conflicts = []
        
        query = self.db.query(IPAMAllocation)
        if network_id:
            query = query.filter(IPAMAllocation.network_id == network_id)
        
        allocations = query.all()
        
        # Group by IP
        ip_groups = {}
        for alloc in allocations:
            if alloc.ip_address not in ip_groups:
                ip_groups[alloc.ip_address] = []
            ip_groups[alloc.ip_address].append(alloc)
        
        # Find duplicates
        for ip, allocs in ip_groups.items():
            if len(allocs) > 1:
                conflicts.append({
                    'ip_address': ip,
                    'type': 'duplicate_allocation',
                    'allocations': [
                        {
                            'id': a.id,
                            'resource_name': a.resource_name,
                            'resource_type': a.resource_type,
                            'proxmox_vmid': a.proxmox_vmid
                        } for a in allocs
                    ]
                })
        
        return conflicts
    
    # ==================== History Operations ====================
    
    def _log_history(
        self,
        ip_address: str,
        network_id: Optional[int],
        action: str,
        old_value: Optional[Dict] = None,
        new_value: Optional[Dict] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[int] = None,
        resource_name: Optional[str] = None,
        performed_by: Optional[str] = None,
        notes: Optional[str] = None
    ):
        """Log an action to IPAM history"""
        history = IPAMHistory(
            ip_address=ip_address,
            network_id=network_id,
            action=action,
            old_value=old_value,
            new_value=new_value,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            performed_by=performed_by or "system",
            notes=notes
        )
        self.db.add(history)
    
    def get_ip_history(
        self,
        ip_address: str,
        limit: int = 50
    ) -> List[IPAMHistory]:
        """Get history for a specific IP address"""
        return self.db.query(IPAMHistory).filter(
            IPAMHistory.ip_address == ip_address
        ).order_by(IPAMHistory.performed_at.desc()).limit(limit).all()
    
    def get_recent_history(
        self,
        network_id: Optional[int] = None,
        limit: int = 100
    ) -> List[IPAMHistory]:
        """Get recent IPAM history"""
        query = self.db.query(IPAMHistory)
        if network_id:
            query = query.filter(IPAMHistory.network_id == network_id)
        return query.order_by(IPAMHistory.performed_at.desc()).limit(limit).all()
