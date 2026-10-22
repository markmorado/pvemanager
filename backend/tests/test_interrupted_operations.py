"""Operations run in threads of the backend process are failed after a restart."""
import app.db
from app.models import AppOperation, DeployTask, InstalledApp, ScriptExecution, TaskQueue
from app.services import task_queue_service


def test_restart_fails_operations_nothing_will_finish(db_session, seed_users, monkeypatch):
    monkeypatch.setattr(task_queue_service, "SessionLocal", app.db.SessionLocal)
    uid = seed_users["user"].id
    db_session.add_all([
        DeployTask(status="running", name="vm1", template_id=1, server_id=1, user_id=uid),
        DeployTask(status="pending", name="vm2", template_id=1, server_id=1, user_id=uid),
        DeployTask(status="completed", name="vm3", template_id=1, server_id=1, user_id=uid),
        AppOperation(type="install", status="running", user_id=uid),
        ScriptExecution(server_id=1, target_type="node", status="running", user_id=uid),
        TaskQueue(task_type="bulk_start", status="running", user_id=uid, total_items=1,
                  completed_items=0, failed_items=0, task_data=[], results=[]),
        InstalledApp(name="nginx", status="installing", owner_id=uid),
        InstalledApp(name="ok", status="running", owner_id=uid),
    ])
    db_session.commit()

    assert task_queue_service.recover_interrupted_tasks() == 6

    db_session.expire_all()
    assert sorted(t.status for t in db_session.query(DeployTask)) == ["completed", "failed", "failed"]
    assert db_session.query(AppOperation).one().status == "failed"
    assert db_session.query(ScriptExecution).one().status == "failed"
    assert db_session.query(TaskQueue).one().status == "failed"
    assert {a.name: a.status for a in db_session.query(InstalledApp)} == {"nginx": "error", "ok": "running"}
