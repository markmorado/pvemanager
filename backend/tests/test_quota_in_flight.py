"""Queued creations count against max_instances (parallel deploys bypassed it)."""
import pytest
from fastapi import HTTPException

from app.models import DeployTask, UserQuota, VMInstance
from app.services.quota_service import check_quota


def test_pending_deploy_counts_toward_instance_quota(db_session, seed_users):
    uid = seed_users["user"].id
    db_session.add(UserQuota(user_id=uid, max_instances=1))
    db_session.add(DeployTask(status="pending", kind="deploy", name="first", user_id=uid))
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        check_quota(db_session, uid)
    assert exc.value.status_code == 429


def test_cached_or_finished_tasks_are_not_double_counted(db_session, seed_users):
    uid = seed_users["user"].id
    db_session.add(UserQuota(user_id=uid, max_instances=2))
    db_session.add(VMInstance(server_id=1, vmid=150, node="pve1", vm_type="qemu", name="a",
                              owner_id=uid, status="running"))
    db_session.add_all([
        DeployTask(status="running", kind="deploy", name="a", user_id=uid, vmid=150),  # already cached
        DeployTask(status="failed", kind="deploy", name="b", user_id=uid),
        DeployTask(status="pending", kind="migrate", name="c", user_id=uid),
    ])
    db_session.commit()

    check_quota(db_session, uid)  # 1 instance + 0 in flight + 1 new <= 2
