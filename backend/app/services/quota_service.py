"""
Quota Service - Per-user resource limits (quotas)

Enforces per-user limits on the number of instances and the summed vCPU, RAM
and disk across a user's VMs/LXC. Current usage is computed from the
``vm_instances`` cache (column ``owner_id``), where ``memory`` is stored in MB
and ``disk_size`` in GB by convention (see the monitoring worker conversions),
so no unit conversion is needed here.

A missing ``UserQuota`` row — or a ``NULL`` column — means *unlimited* for that
metric, so users without an explicit quota (including admins) are never blocked.
"""

from typing import Dict
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import VMInstance, UserQuota


def get_user_usage(db: Session, user_id: int) -> Dict[str, int]:
    """Return current resource usage for a user from the vm_instances cache.

    Only non-deleted instances owned by the user are counted.
    """
    row = (
        db.query(
            func.count(VMInstance.id),
            func.coalesce(func.sum(VMInstance.cores), 0),
            func.coalesce(func.sum(VMInstance.memory), 0),
            func.coalesce(func.sum(VMInstance.disk_size), 0),
        )
        .filter(
            VMInstance.owner_id == user_id,
            VMInstance.deleted_at.is_(None),
            VMInstance.is_template.isnot(True),
        )
        .one()
    )
    return {
        "instances": int(row[0] or 0),
        "cores": int(row[1] or 0),
        "memory_mb": int(row[2] or 0),
        "disk_gb": int(row[3] or 0),
    }


# DeployTask kinds that end with a new instance (LXC creation uses the default).
_CREATING_KINDS = ('deploy', 'clone', 'vm_iso')


def _in_flight_creations(db: Session, owner_id: int) -> int:
    """Queued/running creations not yet in vm_instances.

    The instance row appears only after the clone/create finishes (a minute
    or more), so parallel requests all passed a max_instances check against
    the same usage. Tasks whose guest is already cached are not counted twice.
    """
    from ..models import DeployTask, InstalledApp
    rows = (
        db.query(DeployTask.vmid)
        .filter(
            DeployTask.user_id == owner_id,
            DeployTask.kind.in_(_CREATING_KINDS),
            DeployTask.status.in_(('pending', 'running')),
        )
        .all()
    )
    # App Store installs: the LXC is created by a background job.
    rows += (
        db.query(InstalledApp.vmid)
        .filter(InstalledApp.owner_id == owner_id, InstalledApp.status == 'installing')
        .all()
    )
    if not rows:
        return 0
    cached = {
        v for (v,) in db.query(VMInstance.vmid).filter(
            VMInstance.owner_id == owner_id, VMInstance.deleted_at.is_(None),
        )
    }
    return sum(1 for (vmid,) in rows if vmid is None or vmid not in cached)


def check_quota(
    db: Session,
    owner_id: int,
    add_cores: int = 0,
    add_memory_mb: int = 0,
    add_disk_gb: int = 0,
) -> None:
    """Raise HTTP 429 if deploying a new instance would exceed the user's quota.

    Each metric is checked independently; a ``NULL`` limit (or no quota row)
    skips that metric.
    """
    quota = db.query(UserQuota).filter(UserQuota.user_id == owner_id).first()
    if quota is None:
        return

    usage = get_user_usage(db, owner_id)
    in_flight = _in_flight_creations(db, owner_id)

    checks = [
        (quota.max_instances, usage["instances"] + in_flight + 1, "инстансов", "instances"),
        (quota.max_cores, usage["cores"] + (add_cores or 0), "vCPU (ядер)", "vCPU"),
        (quota.max_memory_mb, usage["memory_mb"] + (add_memory_mb or 0), "MB памяти", "MB of RAM"),
        (quota.max_disk_gb, usage["disk_gb"] + (add_disk_gb or 0), "GB диска", "GB of disk"),
    ]

    for limit, projected, unit_ru, unit_en in checks:
        if limit is not None and projected > limit:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Превышена квота: {projected} > {limit} {unit_ru}. "
                    f"Quota exceeded: {projected} > {limit} {unit_en}."
                ),
            )
