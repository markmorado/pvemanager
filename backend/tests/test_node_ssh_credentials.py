"""Root SSH to a node must work for servers that use API-token auth.

Token auto-provisioning keeps the root password (encrypted) for SSH, but the
client built by ``from_server`` used to drop it, so App Store / golden-template
SSH failed with "No authentication methods available".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import paramiko

from app import ssh_client as sc
from app.proxmox.client import ProxmoxClient


def _server(**overrides):
    base = dict(
        ip_address="10.0.0.3", hostname="pve", port=8006, api_user="root@pam",
        api_token_name="pvemanager", api_token_value="secret",
        use_password=False, password="rootpw", verify_ssl=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(server):
    with patch.object(ProxmoxClient, "__init__", lambda self, **kw: setattr(self, "_password", kw.get("password"))):
        return ProxmoxClient.from_server(server)


def test_token_client_keeps_stored_password_for_ssh():
    assert _build(_server())._password == "rootpw"


def test_token_client_without_password_has_none():
    assert _build(_server(password=None))._password is None


def test_password_client_uses_password():
    assert _build(_server(use_password=True))._password == "rootpw"


def test_no_auth_methods_fails_fast_with_hint(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "SSH_KNOWN_HOSTS_FILE", str(tmp_path / "kh"))
    sc.SSHClient._connections.clear()
    calls = []

    def fake_connect(self, **kwargs):
        calls.append(kwargs)
        raise paramiko.SSHException("No authentication methods available")

    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect)
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)

    ssh = sc.SSHClient("10.0.0.3", username="root")
    assert ssh.connect() is False
    assert len(calls) == 1  # no pointless retries
    assert "no credentials" in ssh.last_error
