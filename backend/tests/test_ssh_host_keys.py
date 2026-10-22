"""SSH host keys: trust on first use, refuse a changed key, reset by an admin."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import paramiko
import pytest

from app import ssh_client as sc


@pytest.fixture(autouse=True)
def known_hosts(tmp_path, monkeypatch):
    from app.config import settings
    path = tmp_path / "ssh_known_hosts"
    monkeypatch.setattr(settings, "SSH_KNOWN_HOSTS_FILE", str(path))
    monkeypatch.setattr(settings, "SSH_REJECT_UNKNOWN_HOSTS", False)
    sc.SSHClient._connections.clear()
    sc.SSHClient._connection_times.clear()
    return path


def _key():
    return paramiko.ECDSAKey.generate()


def test_first_connection_pins_key(known_hosts):
    key = _key()
    client = paramiko.SSHClient()

    sc._TrustOnFirstUsePolicy().missing_host_key(client, "10.0.0.5", key)

    assert known_hosts.is_file()
    pinned = sc.pinned_host_keys("10.0.0.5")
    assert pinned == [{"type": key.get_name(), "fingerprint": sc.key_fingerprint(key)}]
    assert client.get_host_keys().lookup("10.0.0.5")


def test_non_default_port_uses_bracket_entry():
    assert sc.host_key_entry("10.0.0.5", 22) == "10.0.0.5"
    assert sc.host_key_entry("10.0.0.5", 2222) == "[10.0.0.5]:2222"


def test_pinned_key_is_loaded_and_tofu_policy_set_on_connect():
    key = _key()
    sc.pin_host_key("10.0.0.6", key)
    seen = {}

    def fake_connect(self, **kwargs):
        seen["known"] = self.get_host_keys().lookup("10.0.0.6")
        seen["policy"] = type(self._policy).__name__

    with patch.object(paramiko.SSHClient, "connect", fake_connect):
        assert sc.SSHClient("10.0.0.6", password="x").connect() is True

    assert seen["known"] and seen["known"][key.get_name()] == key
    assert seen["policy"] == "_TrustOnFirstUsePolicy"


def test_changed_key_is_refused_without_retries():
    expected, presented = _key(), _key()
    calls = MagicMock(side_effect=paramiko.BadHostKeyException("10.0.0.7", presented, expected))

    with patch.object(paramiko.SSHClient, "connect", calls):
        ssh = sc.SSHClient("10.0.0.7", password="x", max_retries=3)
        assert ssh.connect() is False

    assert calls.call_count == 1
    assert "does not match the pinned key" in ssh.last_error
    assert sc.key_fingerprint(presented) in ssh.last_error


def test_forget_removes_pin():
    sc.pin_host_key("10.0.0.8", _key())
    sc.pin_host_key("10.0.0.9", _key())

    assert sc.forget_host_key("10.0.0.8") == 1
    assert sc.pinned_host_keys("10.0.0.8") == []
    assert sc.pinned_host_keys("10.0.0.9") != []
    assert sc.forget_host_key("10.0.0.8") == 0


def test_strict_mode_keeps_reject_policy(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SSH_REJECT_UNKNOWN_HOSTS", True)
    seen = {}

    def fake_connect(self, **kwargs):
        seen["policy"] = type(self._policy).__name__

    with patch.object(paramiko.SSHClient, "connect", fake_connect):
        sc.SSHClient("10.0.0.10", password="x").connect()

    assert seen["policy"] == "RejectPolicy"


def test_reset_endpoint(client, admin_headers, db_session):
    from app.models import ProxmoxServer
    srv = ProxmoxServer(name="pve-hk", hostname="pve-hk.local", ip_address="10.0.0.11", port=8006,
                        api_user="root@pam", use_password=True, password="secret", verify_ssl=False)
    db_session.add(srv)
    db_session.commit()
    sc.pin_host_key("10.0.0.11", _key())

    shown = client.get(f"/proxmox/api/servers/{srv.id}/ssh-host-key", headers=admin_headers)
    assert shown.status_code == 200
    by_host = {h["host"]: h["keys"] for h in shown.json()["hosts"]}
    assert by_host["10.0.0.11"] and by_host["pve-hk.local"] == []

    reset = client.delete(f"/proxmox/api/servers/{srv.id}/ssh-host-key", headers=admin_headers)
    assert reset.status_code == 200 and reset.json()["removed"] == 1
    assert sc.pinned_host_keys("10.0.0.11") == []
