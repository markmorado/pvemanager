from fastapi import APIRouter, Depends
from .servers import router as servers_router
from .sdn import router as sdn_router
from .snapshots import router as snapshots_router
from .vms import router as vms_router
from .tasks import router as tasks_router
from .backup import router as backup_router
from .cluster import router as cluster_router
from .networks import router as networks_router
from .lxc import router as lxc_router
from .iso_vm import router as iso_vm_router
from .images import router as images_router
from .firewall import router as firewall_router
from .pools import router as pools_router
from .disks import router as disks_router
from .access import router as access_router
from .node_admin import router as node_admin_router
from .topology import router as topology_router
from ._helpers import get_next_vmid, save_vm_instance, get_vm_instance, validate_pve_identifiers

# Applies to every included route, WebSockets too: path segments / node names
# go into Proxmox API URLs (see validate_pve_identifiers).
router = APIRouter(dependencies=[Depends(validate_pve_identifiers)])
router.include_router(servers_router)
router.include_router(sdn_router)
router.include_router(snapshots_router)
router.include_router(vms_router)
router.include_router(tasks_router)
router.include_router(backup_router)
router.include_router(cluster_router)
router.include_router(networks_router)
router.include_router(lxc_router)
router.include_router(iso_vm_router)
router.include_router(images_router)
router.include_router(firewall_router)
router.include_router(pools_router)
router.include_router(disks_router)
router.include_router(access_router)
router.include_router(node_admin_router)
router.include_router(topology_router)
