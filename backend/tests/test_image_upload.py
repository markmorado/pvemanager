"""Image upload hands the whole file to the background worker."""
import os
import threading

from app.models import ProxmoxServer


def test_upload_copies_file_for_worker(client, db_session, admin_headers, monkeypatch):
    from app.api.proxmox import async_ops

    srv = ProxmoxServer(name="pve-up", hostname="pve-up.local", ip_address="10.0.0.9", port=8006,
                        api_user="root@pam", use_password=True, password="x", verify_ssl=False)
    db_session.add(srv)
    db_session.commit()

    seen = {}
    done = threading.Event()

    def fake_worker(task_id, server_id, node, storage, content, name, tmp_path, tmp_dir, *rest):
        with open(tmp_path, "rb") as f:
            seen["data"] = f.read()
        seen["name"] = os.path.basename(tmp_path)
        done.set()

    monkeypatch.setattr(async_ops, "_do_image_upload_sync", fake_worker)
    payload = os.urandom(3 * 1024 * 1024 + 17)

    resp = client.post(
        f"/proxmox/api/{srv.id}/images/upload", headers=admin_headers,
        data={"node": "pve1", "storage": "local", "content": "iso"},
        files={"file": ("debian.iso", payload, "application/octet-stream")},
    )

    assert resp.status_code == 200, resp.text
    assert done.wait(10)
    assert seen["data"] == payload and seen["name"] == "debian.iso"
