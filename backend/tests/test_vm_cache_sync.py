"""sync_vm_cache: upsert from Proxmox, keep ownership, soft-delete vanished guests."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.models import ProxmoxServer, VMInstance
from app.workers.monitoring_worker import MonitoringWorker


@pytest.fixture
def server(db_session):
    srv = ProxmoxServer(
        name="pve-sync", hostname="pve-sync.local", ip_address="10.0.0.9", port=8006,
        api_user="root@pam", use_password=True, password="secret", verify_ssl=False,
        is_online=True,
    )
    db_session.add(srv)
    db_session.commit()
    db_session.refresh(srv)
    return srv


def _fake_client(resources):
    client = MagicMock()
    client.is_connected.return_value = True
    client.get_nodes.return_value = [{"node": "pve1"}]
    client.get_cluster_resources.return_value = resources
    client.get_container_interfaces.return_value = []
    return client


def _run_sync(monkeypatch, resources):
    worker = MonitoringWorker()
    client = _fake_client(resources)
    monkeypatch.setattr(worker, "_create_proxmox_client", lambda server: client)
    worker.sync_vm_cache()
    return client


def test_sync_upserts_and_preserves_owner(db_session, server, seed_users, monkeypatch):
    owner = seed_users["user"]
    db_session.add(VMInstance(server_id=server.id, vmid=100, node="pve1", vm_type="qemu",
                              name="old-name", status="stopped", owner_id=owner.id))
    db_session.commit()

    _run_sync(monkeypatch, [
        {"vmid": 100, "type": "qemu", "name": "web", "node": "pve1", "status": "running",
         "maxmem": 2 * 1024 ** 3, "maxdisk": 10 * 1024 ** 3, "maxcpu": 2},
        {"vmid": 200, "type": "lxc", "name": "ct", "node": "pve1", "status": "stopped"},
    ])

    db_session.expire_all()
    rows = {r.vmid: r for r in db_session.query(VMInstance).filter_by(server_id=server.id)}
    assert set(rows) == {100, 200}
    assert rows[100].name == "web" and rows[100].status == "running"
    assert rows[100].owner_id == owner.id
    assert rows[100].memory == 2048
    assert rows[200].vm_type == "lxc"


def test_sync_soft_deletes_vanished_guest(db_session, server, monkeypatch):
    db_session.add(VMInstance(server_id=server.id, vmid=300, node="pve1", vm_type="qemu",
                              name="gone", status="running"))
    db_session.commit()

    _run_sync(monkeypatch, [{"vmid": 301, "type": "qemu", "name": "stays", "node": "pve1",
                             "status": "running"}])

    db_session.expire_all()
    gone = db_session.query(VMInstance).filter_by(server_id=server.id, vmid=300).one()
    assert gone.deleted_at is not None


def test_sync_creates_client_once_per_server(db_session, server, monkeypatch):
    worker = MonitoringWorker()
    client = _fake_client([])
    created = []

    def _create(srv):
        created.append(srv.id)
        return client

    monkeypatch.setattr(worker, "_create_proxmox_client", _create)
    worker.sync_vm_cache()

    assert created == [server.id]
