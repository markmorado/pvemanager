"""Regression tests for access-control holes closed in the security audit.

Each test pins one concrete attack that used to work:

* a tenant with ``vm:console`` running commands in somebody else's guest;
* ``user:create`` / ``user:update`` holders minting or taking over admins;
* WebSocket metrics channels and RRD requests for guests the user does not own;
* the impersonation token being redeemable after its session was terminated;
* WebSocket tickets being replayable, outliving their TTL or their session,
  and the session JWT being accepted in WebSocket URLs;
* password-reset links following the attacker-supplied ``Origin`` header;
* script parameters / git sources smuggling shell or git options.
"""
from __future__ import annotations

import pytest

from app.models import ProxmoxServer, Role, User, VMInstance


@pytest.fixture
def server(db_session):
    srv = ProxmoxServer(
        name="pve-sec", hostname="pve-sec.local", ip_address="10.0.0.5", port=8006,
        api_user="root@pam", use_password=True, password="secret", verify_ssl=False,
        is_online=True,
    )
    db_session.add(srv)
    db_session.commit()
    db_session.refresh(srv)
    return srv


@pytest.fixture
def guests(db_session, server, seed_users):
    own = VMInstance(server_id=server.id, vmid=100, node="pve1", vm_type="qemu",
                     name="mine", owner_id=seed_users["user"].id, status="running")
    foreign = VMInstance(server_id=server.id, vmid=101, node="pve1", vm_type="qemu",
                         name="theirs", owner_id=seed_users["admin"].id, status="running")
    db_session.add_all([own, foreign])
    db_session.commit()
    return {"own": own, "foreign": foreign}


def _grant(db_session, user: User, **perms: bool) -> None:
    role = user.role
    role.permissions = {**(role.permissions or {}), **{k.replace("__", ":"): v for k, v in perms.items()}}
    db_session.commit()


# ── Guest command execution (IDOR) ─────────────────────────────────────────

def test_console_user_cannot_execute_in_foreign_vm(client, db_session, seed_users, user_headers,
                                                   server, guests, mock_proxmox):
    _grant(db_session, seed_users["user"], vm__console=True)
    mock_proxmox["client"].execute_command.return_value = {"success": True}

    resp = client.post(
        f"/proxmox/api/{server.id}/vm/101/execute",
        params={"node": "pve1", "command": "id"}, headers=user_headers,
    )

    assert resp.status_code == 403
    mock_proxmox["client"].execute_command.assert_not_called()


def test_console_user_can_execute_in_own_vm(client, db_session, seed_users, user_headers,
                                            server, guests, mock_proxmox):
    _grant(db_session, seed_users["user"], vm__console=True)
    mock_proxmox["client"].execute_command.return_value = {"success": True}

    resp = client.post(
        f"/proxmox/api/{server.id}/vm/100/execute",
        params={"node": "pve1", "command": "id"}, headers=user_headers,
    )

    assert resp.status_code == 200


def test_view_user_cannot_read_foreign_vm_config(client, user_headers, server, guests, mock_proxmox):
    resp = client.get(f"/proxmox/api/{server.id}/vm/101/config", params={"node": "pve1"},
                      headers=user_headers)
    assert resp.status_code == 403


def test_clone_respects_quota(client, db_session, seed_users, user_headers, server, guests, mock_proxmox):
    from app.models import UserQuota
    _grant(db_session, seed_users["user"], vm__create=True)
    db_session.add(UserQuota(user_id=seed_users["user"].id, max_instances=1))
    db_session.commit()

    resp = client.post(f"/proxmox/api/{server.id}/vm/100/clone", params={"node": "pve1"},
                       headers=user_headers, json={"new_name": "copy"})

    assert resp.status_code == 429


def test_tenant_cannot_reach_unassigned_server(client, db_session, seed_users, user_headers,
                                                server, mock_proxmox):
    _grant(db_session, seed_users["user"], node__view=True)
    mock_proxmox["client"].get_node_syslog.return_value = []
    url = f"/proxmox/api/servers/{server.id}/nodes/pve1/syslog"

    assert client.get(url, headers=user_headers).status_code == 404

    seed_users["user"].assigned_servers.append(server)
    db_session.commit()
    assert client.get(url, headers=user_headers).status_code != 404


def test_owner_keeps_access_to_guest_on_unassigned_server(client, user_headers, server, guests,
                                                          mock_proxmox):
    mock_proxmox["client"].get_snapshots.return_value = []
    resp = client.get(f"/proxmox/api/{server.id}/vm/100/snapshots", params={"node": "pve1"},
                      headers=user_headers)
    assert resp.status_code != 404


# ── User management escalation ─────────────────────────────────────────────

def test_user_create_permission_cannot_mint_admin(client, db_session, seed_users, user_headers):
    _grant(db_session, seed_users["user"], user__create=True)

    resp = client.post("/admin/api/users", headers=user_headers, json={
        "username": "evil", "email": "evil@example.com", "password": "Passw0rdX",
        "is_admin": True,
    })

    assert resp.status_code == 403
    assert db_session.query(User).filter(User.username == "evil").first() is None


def test_user_create_permission_cannot_assign_admin_role(client, db_session, seed_users, user_headers):
    _grant(db_session, seed_users["user"], user__create=True)
    admin_role = db_session.query(Role).filter(Role.name == "admin").one()

    resp = client.post("/admin/api/users", headers=user_headers, json={
        "username": "evil2", "email": "evil2@example.com", "password": "Passw0rdX",
        "role_id": admin_role.id,
    })

    assert resp.status_code == 403


def test_user_update_permission_cannot_take_over_role_admin(client, db_session, seed_users, user_headers):
    """An account that is admin through its role (is_admin=False) is still protected."""
    from app.auth import get_password_hash
    _grant(db_session, seed_users["user"], user__update=True)
    admin_role = db_session.query(Role).filter(Role.name == "admin").one()
    role_admin = User(username="roleadmin", email="ra@example.com", is_active=True, is_admin=False,
                      hashed_password=get_password_hash("Orig1nalPass"), role=admin_role)
    db_session.add(role_admin)
    db_session.commit()

    resp = client.post(f"/admin/api/users/{role_admin.id}/reset-password", headers=user_headers,
                       json={"new_password": "Hacked123Pass"})
    assert resp.status_code == 403

    resp = client.put(f"/admin/api/users/{role_admin.id}", headers=user_headers,
                      json={"password": "Hacked123Pass"})
    assert resp.status_code == 403


def test_user_update_permission_cannot_self_assign_admin_role(client, db_session, seed_users, user_headers):
    _grant(db_session, seed_users["user"], user__update=True)
    admin_role = db_session.query(Role).filter(Role.name == "admin").one()

    resp = client.put(f"/admin/api/users/{seed_users['user'].id}", headers=user_headers,
                      json={"role_id": admin_role.id})

    assert resp.status_code == 403


# ── WebSocket channel / RRD authorization ──────────────────────────────────

def test_ws_instance_channel_requires_ownership(db_session, server, guests, seed_users):
    from app.db import SessionLocal
    from app.services.ws_access import authorize_channel

    uid = seed_users["user"].id
    own_ok, _ = authorize_channel(SessionLocal, uid, f"instance_metrics:{server.id}_100_qemu_pve1")
    foreign_ok, _ = authorize_channel(SessionLocal, uid, f"instance_metrics:{server.id}_101_qemu_pve1")
    junk_ok, _ = authorize_channel(SessionLocal, uid, "anything-else")

    assert own_ok is True
    assert foreign_ok is False
    assert junk_ok is False


def test_ws_instances_list_is_filtered_for_tenants(db_session, server, guests, seed_users):
    from app.db import SessionLocal
    from app.services.ws_access import authorize_channel

    ok, payload_filter = authorize_channel(SessionLocal, seed_users["user"].id, "instances_list_metrics")
    assert ok and payload_filter is not None

    payload = {"type": "metrics_update", "data": {"items": [
        {"server_id": server.id, "vmid": 100}, {"server_id": server.id, "vmid": 101},
    ]}}
    assert [i["vmid"] for i in payload_filter(payload)["data"]["items"]] == [100]


def test_ws_rrd_request_requires_ownership(db_session, server, guests, seed_users):
    from app.db import SessionLocal
    from app.services.ws_access import authorize_rrd_request

    uid = seed_users["user"].id
    assert authorize_rrd_request(SessionLocal, uid, "get_rrd", {"server_id": server.id, "vmid": 100})
    assert not authorize_rrd_request(SessionLocal, uid, "get_rrd", {"server_id": server.id, "vmid": 101})


def test_ws_manager_applies_subscriber_filter():
    import asyncio
    import json
    from app.websocket_manager import ConnectionManager

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(json.loads(text))

    mgr = ConnectionManager()
    plain, filtered = _WS(), _WS()
    mgr.subscribe(plain, "ch")
    mgr.subscribe(filtered, "ch", lambda p: {**p, "items": p["items"][:1]})

    asyncio.run(mgr.broadcast_to_channel("ch", {"items": [1, 2]}))

    assert plain.sent == [{"items": [1, 2]}]
    assert filtered.sent == [{"items": [1]}]


# ── Sessions ───────────────────────────────────────────────────────────────

def test_stop_impersonate_rejects_terminated_session(client, db_session, seed_users):
    from app.auth import create_access_token
    from app.services.security_service import SecurityService

    session, _ = SecurityService.create_session(db_session, seed_users["user"], "127.0.0.1", "t")
    token = create_access_token(
        data={"sub": seed_users["user"].username, "impersonator": seed_users["admin"].username},
        session_token=session.session_token,
    )
    SecurityService.terminate_session(db_session, session.session_token)

    resp = client.post("/api/auth/stop-impersonate", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401


def test_logout_session_cannot_open_task_websocket(client, db_session, seed_users):
    from starlette.websockets import WebSocketDisconnect
    from app.auth import create_access_token
    from app.services.security_service import SecurityService

    session, _ = SecurityService.create_session(db_session, seed_users["user"], "127.0.0.1", "t")
    token = create_access_token(data={"sub": seed_users["user"].username},
                                session_token=session.session_token)
    SecurityService.terminate_session(db_session, session.session_token)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/tasks?token={token}") as ws:
            ws.receive_text()


# ── WebSocket tickets ──────────────────────────────────────────────────────

def _ws_ticket(client, headers) -> str:
    resp = client.post("/api/auth/ws-ticket", headers=headers)
    assert resp.status_code == 200
    return resp.json()["ticket"]


def test_ws_ticket_requires_authentication(client):
    assert client.post("/api/auth/ws-ticket").status_code in (401, 403)


def test_ws_ticket_is_single_use(client, db_session, seed_users, user_headers):
    from app.auth import authenticate_ws

    ticket = _ws_ticket(client, user_headers)
    assert user_headers["Authorization"].split()[1] != ticket  # opaque, not the JWT

    user = authenticate_ws(ticket, db_session)
    assert user is not None and user.id == seed_users["user"].id
    assert authenticate_ws(ticket, db_session) is None


def test_ws_ticket_opens_task_websocket_once(client, user_headers):
    from starlette.websockets import WebSocketDisconnect

    ticket = _ws_ticket(client, user_headers)
    with client.websocket_connect(f"/ws/tasks?ticket={ticket}"):
        pass

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/tasks?ticket={ticket}") as ws:
            ws.receive_text()


def test_ws_ticket_expires(client, db_session, user_headers):
    import app.auth as auth_mod

    ticket = _ws_ticket(client, user_headers)
    user_id, session_token, _ = auth_mod._ws_tickets[ticket]
    auth_mod._ws_tickets[ticket] = (user_id, session_token, auth_mod.time.monotonic() - 1)

    assert auth_mod.authenticate_ws(ticket, db_session) is None


def test_ws_ticket_is_bound_to_its_user(client, db_session, seed_users, user_headers, admin_headers):
    from app.auth import authenticate_ws

    user_ticket = _ws_ticket(client, user_headers)
    admin_ticket = _ws_ticket(client, admin_headers)

    assert authenticate_ws(user_ticket, db_session).id == seed_users["user"].id
    assert authenticate_ws(admin_ticket, db_session).id == seed_users["admin"].id
    assert authenticate_ws("forged-ticket", db_session) is None


def test_ws_ticket_of_logged_out_session_is_refused(client, db_session, seed_users):
    from app.auth import authenticate_ws
    from tests.conftest import _get_headers_for_user

    headers = _get_headers_for_user(db_session, seed_users["user"])
    ticket = _ws_ticket(client, headers)
    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert authenticate_ws(ticket, db_session) is None


def test_task_websocket_refuses_jwt_in_query(client, user_headers):
    from starlette.websockets import WebSocketDisconnect

    # The session JWT in ?token= leaked into logs and browser history; a
    # valid one must not open the socket any more.
    jwt_token = user_headers["Authorization"].split()[1]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/tasks?token={jwt_token}") as ws:
            ws.receive_text()


def test_profile_password_change_enforces_policy_and_ends_other_sessions(client, db_session, seed_users):
    from app.models import ActiveSession
    from tests.conftest import _get_headers_for_user

    current = _get_headers_for_user(db_session, seed_users["user"])
    other = _get_headers_for_user(db_session, seed_users["user"])

    weak = client.post("/settings/api/change-password", headers=current, json={
        "current_password": "UserPass123!", "new_password": "weakpw", "confirm_password": "weakpw"})
    assert weak.status_code == 400

    ok = client.post("/settings/api/change-password", headers=current, json={
        "current_password": "UserPass123!", "new_password": "Str0ngerPass", "confirm_password": "Str0ngerPass"})
    assert ok.status_code == 200

    assert client.get("/api/auth/me", headers=current).status_code == 200
    assert client.get("/api/auth/me", headers=other).status_code == 401


def test_password_reset_link_ignores_origin_header(client, db_session, seed_users, monkeypatch):
    from app.api import auth as auth_api
    from app.config import settings

    sent = {}

    async def _capture(to_email, display_name, lang, link, expire_minutes):
        sent["link"] = link

    monkeypatch.setattr(auth_api, "_send_reset_email", _capture)
    monkeypatch.setattr(settings, "PANEL_URL", "https://panel.example.com")

    resp = client.post("/api/auth/forgot-password", json={"email": "testuser@example.com"},
                       headers={"Origin": "https://evil.example"})

    assert resp.status_code == 200
    assert sent["link"].startswith("https://panel.example.com/reset-password?token=")


# ── Script catalog input hardening ─────────────────────────────────────────

def test_render_params_drops_injected_names():
    from app.services.script_engine import render_params

    rendered = render_params("echo ok", {"GOOD": "a'b", "X;touch /tmp/pwn;Y": "1"})

    assert "export GOOD='a'\\''b'" in rendered
    assert "touch" not in rendered


@pytest.mark.parametrize("url", ["--upload-pack=touch /tmp/pwn", "ext::sh -c id", "file:///etc"])
def test_repo_url_rejects_option_and_local_transports(url):
    from app.services.script_catalog_sync import validate_repo_url
    with pytest.raises(ValueError):
        validate_repo_url(url)


@pytest.mark.parametrize("glob", ["/etc/*", "../../*", "a/../../b"])
def test_repo_glob_must_stay_inside_clone(glob):
    from app.services.script_catalog_sync import validate_path_glob
    with pytest.raises(ValueError):
        validate_path_glob(glob)


def test_settings_cache_is_per_key(db_session, monkeypatch):
    """A miss on one key must not keep every other cached value alive forever."""
    from datetime import timedelta
    from app.services import security_service as ss

    SecurityService = ss.SecurityService
    SecurityService._settings_cache.clear()
    SecurityService.update_setting(db_session, "session_timeout_minutes", "15")
    assert SecurityService.get_setting(db_session, "session_timeout_minutes") == "15"

    # Change the row behind the cache's back, then age only that key out.
    from app.models import SecuritySetting
    db_session.query(SecuritySetting).filter(SecuritySetting.key == "session_timeout_minutes") \
        .update({"value": "30"})
    db_session.commit()
    value, at = SecurityService._settings_cache["session_timeout_minutes"]
    SecurityService._settings_cache["session_timeout_minutes"] = (value, at - timedelta(seconds=120))
    SecurityService.get_setting(db_session, "some_other_key", "x")  # unrelated miss

    assert SecurityService.get_setting(db_session, "session_timeout_minutes") == "30"


def test_generated_secret_key_is_persisted(tmp_path, monkeypatch):
    """A placeholder SECRET_KEY is swapped for a stable generated one."""
    from app import main
    from app.config import settings

    monkeypatch.setattr(settings, "APPSTORE_DATA_DIR", str(tmp_path / "appstore"))
    first = main._load_or_create_secret_key()
    second = main._load_or_create_secret_key()

    assert len(first) == 64
    assert first == second
    assert first not in main._INSECURE_SECRET_KEYS


def test_totp_code_cannot_be_replayed():
    import pyotp
    from app.services import two_factor_service as tfa

    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()

    assert tfa.verify_totp(secret, code, replay_key="replay-test") is True
    assert tfa.verify_totp(secret, code, replay_key="replay-test") is False
    # Without a replay key (enrollment confirmation) behaviour is unchanged.
    assert tfa.verify_totp(secret, code) is True


def test_two_factor_secret_encrypted_and_login_code_single_use(client, db_session, seed_users, user_headers):
    import pyotp
    from sqlalchemy import text

    setup = client.post("/api/auth/2fa/setup", headers=user_headers)
    assert setup.status_code == 200
    secret = setup.json()["secret"]

    raw = db_session.execute(text("SELECT two_factor_secret FROM users WHERE username = 'testuser'")).scalar()
    assert raw and raw != secret  # stored as a Fernet token, not the seed

    totp = pyotp.TOTP(secret)
    enable = client.post("/api/auth/2fa/enable", headers=user_headers, json={"code": totp.now()})
    assert enable.status_code == 200

    code = totp.now()
    login = {"username": "testuser", "password": "UserPass123!", "code": code}
    first = client.post("/api/auth/login", json=login)
    assert first.status_code == 200 and first.json().get("access_token")

    replay = client.post("/api/auth/login", json=login)
    assert replay.status_code == 400


def test_wrong_two_factor_codes_lock_the_account(client, db_session, seed_users, user_headers):
    import pyotp

    secret = client.post("/api/auth/2fa/setup", headers=user_headers).json()["secret"]
    client.post("/api/auth/2fa/enable", headers=user_headers, json={"code": pyotp.TOTP(secret).now()})

    for _ in range(5):
        client.post("/api/auth/login", json={"username": "testuser", "password": "UserPass123!",
                                              "code": "000000"})

    db_session.expire_all()
    user = db_session.query(User).filter(User.username == "testuser").one()
    assert user.locked_until is not None


# ── Notifications ──────────────────────────────────────────────────────────

def test_vm_notifications_reach_only_owner_and_admins(db_session, server, guests, seed_users):
    from app.auth import get_password_hash
    from app.workers.monitoring_worker import MonitoringWorker

    db_session.add(User(username="bystander", email="b@example.com", is_active=True,
                        hashed_password=get_password_hash("Bystander1"),
                        role=seed_users["user"].role))
    db_session.commit()
    users = db_session.query(User).filter(User.is_active == True).all()  # noqa: E712

    recipients = MonitoringWorker._vm_recipients(db_session, users, server, 100)

    assert {u.username for u in recipients} == {"testadmin", "testuser"}


# ── Console tickets ────────────────────────────────────────────────────────

def test_vnc_endpoint_does_not_hand_out_pve_ticket(client, db_session, seed_users, user_headers,
                                                   server, guests, monkeypatch):
    import requests
    from unittest.mock import MagicMock
    from app.api.proxmox._helpers import claim_pve_ticket

    _grant(db_session, seed_users["user"], vm__console=True)
    root_ticket = "PVE:root@pam:ROOTTICKET"
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        resp = MagicMock(status_code=200)
        if url.endswith("/access/ticket"):
            resp.json.return_value = {"data": {"ticket": root_ticket, "CSRFPreventionToken": "c"}}
        else:
            resp.json.return_value = {"data": {"port": 5900, "ticket": "vnc", "password": "p"}}
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    resp = client.get(f"/proxmox/api/{server.id}/vm/100/vnc", params={"node": "pve1"},
                      headers=user_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert root_ticket not in resp.text and "host" not in body
    handle = body["auth_ticket"]
    assert claim_pve_ticket(handle, seed_users["admin"].id, server.id, 100) is None

    resp = client.get(f"/proxmox/api/{server.id}/vm/100/vnc", params={"node": "pve1"},
                      headers=user_headers)
    handle = resp.json()["auth_ticket"]
    uid = seed_users["user"].id
    assert claim_pve_ticket(handle, uid, server.id, 100) == root_ticket
    assert claim_pve_ticket(handle, uid, server.id, 100) is None  # single use
    assert all("10.0.0.5:8006/" in url for url in calls)


def test_pve_authority_uses_server_port():
    from types import SimpleNamespace
    from app.api.proxmox._helpers import pve_authority

    assert pve_authority(SimpleNamespace(ip_address="10.0.0.5", port=8443)) == "10.0.0.5:8443"
    assert pve_authority(SimpleNamespace(ip_address="fd00::5", port=None)) == "[fd00::5]:8006"


# ── Templates / App Store ──────────────────────────────────────────────────

def test_deploy_and_template_list_respect_server_assignment(client, db_session, seed_users,
                                                           user_headers, server):
    from app.models import OSTemplate
    _grant(db_session, seed_users["user"], vm__create=True, template__view=True)
    tpl = OSTemplate(group_id=1, server_id=server.id, name="debian", vmid=9000, node="pve1")
    db_session.add(tpl)
    db_session.commit()

    listed = client.get("/templates/api/templates", headers=user_headers)
    assert listed.status_code == 200 and listed.json() == []
    resp = client.post("/templates/api/deploy", headers=user_headers,
                       json={"template_id": tpl.id, "name": "vm1"})
    assert resp.status_code == 404

    seed_users["user"].assigned_servers.append(server)
    db_session.commit()
    assert [t["id"] for t in client.get("/templates/api/templates", headers=user_headers).json()] == [tpl.id]


@pytest.mark.parametrize("field", ["template_storage", "rootfs_storage", "bridge"])
def test_golden_template_rejects_shell_in_names(client, db_session, seed_users, user_headers,
                                                server, field):
    _grant(db_session, seed_users["user"], app__manage=True, app__view=True)
    seed_users["user"].assigned_servers.append(server)
    db_session.commit()

    resp = client.post("/api/appstore/golden-template", headers=user_headers,
                       json={"server_id": server.id, field: "local; touch /tmp/pwn"})

    assert resp.status_code == 422


def test_golden_spec_validates_names_itself():
    from app.appstore.golden_template import GoldenSpec, build_golden_template
    from app.appstore.pipeline import PocError

    with pytest.raises(PocError):
        build_golden_template(None, GoldenSpec(server_id=1, bridge="vmbr0 $(id)"))


def test_catalog_logo_is_served_sandboxed(client, db_session, tmp_path, monkeypatch):
    from app.config import settings
    from app.models import CatalogApp

    monkeypatch.setattr(settings, "APPSTORE_DATA_DIR", str(tmp_path))
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "evil.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    db_session.add(CatalogApp(app_id="evil", name="Evil", logo_path="logos/evil.svg"))
    db_session.commit()

    resp = client.get("/api/appstore/catalog/evil/logo")

    assert resp.status_code == 200
    assert "sandbox" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"


# ── Create requests that reference volumes ─────────────────────────────────

def _lxc_body(server, **over):
    return {"server_id": server.id, "node": "pve1", "name": "ct1",
            "ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst", **over}


def test_lxc_create_checks_server_template_and_privileges(client, db_session, seed_users,
                                                         user_headers, server, mock_proxmox):
    _grant(db_session, seed_users["user"], vm__create=True)
    url = "/proxmox/api/lxc/deploy"

    assert client.post(url, json=_lxc_body(server), headers=user_headers).status_code == 404
    seed_users["user"].assigned_servers.append(server)
    db_session.commit()

    backup = "local:backup/vzdump-lxc-101-2024_01_01-00_00_00.tar.zst"
    assert client.post(url, json=_lxc_body(server, ostemplate=backup),
                       headers=user_headers).status_code == 400
    assert client.post(url, json=_lxc_body(server, unprivileged=False),
                       headers=user_headers).status_code == 403


def test_iso_vm_rejects_non_iso_volumes(client, db_session, seed_users, user_headers, server):
    _grant(db_session, seed_users["user"], vm__create=True)
    seed_users["user"].assigned_servers.append(server)
    db_session.commit()
    body = {"server_id": server.id, "node": "pve1", "name": "vm1", "iso": "local-lvm:vm-101-disk-0"}

    resp = client.post("/proxmox/api/vm/create-from-iso", json=body, headers=user_headers)

    assert resp.status_code == 400
    body.update(iso="local:iso/debian.iso", extra_iso="local-lvm:vm-101-disk-0")
    assert client.post("/proxmox/api/vm/create-from-iso", json=body,
                       headers=user_headers).status_code == 400


def test_refresh_keeps_impersonation_link(client, seed_users, admin_headers):
    imp = client.post(f"/api/auth/impersonate/{seed_users['user'].id}", headers=admin_headers)
    assert imp.status_code == 200, imp.text
    h = {"Authorization": f"Bearer {imp.json()['access_token']}"}

    refreshed = client.post("/api/auth/refresh", headers=h)
    assert refreshed.status_code == 200
    h = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}

    back = client.post("/api/auth/stop-impersonate", headers=h)
    assert back.status_code == 200, back.text
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {back.json()['access_token']}"})
    assert me.json()["username"] == "testadmin"


# ── Identifiers that become Proxmox API path segments ──────────────────────

def test_node_and_path_params_must_be_plain_names(client, db_session, seed_users, user_headers,
                                                  server, guests, mock_proxmox):
    _grant(db_session, seed_users["user"], vm__start=True)
    url = f"/proxmox/api/{server.id}/vm/100/start"

    for node in ("pve1/../x", "pve1#", "pve1?a=b"):
        assert client.post(url, params={"node": node}, headers=user_headers).status_code == 400
    mock_proxmox["client"].start_vm.assert_not_called()

    # An encoded slash either fails routing (404) or the segment check (400).
    resp = client.delete(f"/proxmox/api/{server.id}/vm/100/snapshots/a%2Fb",
                         params={"node": "pve1"}, headers=user_headers)
    assert resp.status_code in (400, 404)
    mock_proxmox["client"].delete_vm_snapshot.assert_not_called()

    from types import SimpleNamespace
    from app.api.proxmox._helpers import _identifier_error
    conn = SimpleNamespace(path_params={"snapname": "a/../b"}, query_params={})
    assert _identifier_error(conn) == "snapname"


def test_body_node_names_are_validated(client, user_headers, server, guests):
    items = [{"server_id": server.id, "vmid": 100, "vm_type": "qemu", "name": "a",
              "node": "pve1/../x"}]
    resp = client.post("/proxmox/api/bulk-operation", headers=user_headers,
                       json={"action": "start", "items": items})
    assert resp.status_code == 422


def test_backup_delete_rejects_unsafe_volid(client, db_session, seed_users, admin_headers,
                                            server, mock_proxmox):
    resp = client.request(
        "DELETE", f"/proxmox/api/backups/{server.id}/backup", headers=admin_headers,
        json={"node": "pve1", "storage": "local", "volid": "local:backup/../../x"},
    )
    assert resp.status_code == 400
    mock_proxmox["client"].delete_backup.assert_not_called()


def test_upid_format_check_accepts_real_upids():
    from app.api.proxmox._helpers import _UPID_RE

    assert _UPID_RE.match("UPID:pve1:0000ABCD:0012ABCD:65A1B2C3:qmstart:100:root@pam:")
    assert _UPID_RE.match("UPID:pve-2:00001234:00001234:65A1B2C3:vzdump::root@pam:")
    assert not _UPID_RE.match("UPID:pve1:0000ABCD:0012ABCD:65A1B2C3:qmstart:100/../x:root@pam:")


# ── Bulk operations ────────────────────────────────────────────────────────

def _bulk(client, headers, server, action, *vmids):
    items = [{"server_id": server.id, "vmid": v, "vm_type": "qemu", "name": str(v),
              "node": "pve1"} for v in vmids]
    return client.post("/proxmox/api/bulk-operation", headers=headers,
                       json={"action": action, "items": items})


def test_bulk_operation_rejects_foreign_guest(client, db_session, seed_users, user_headers,
                                              server, guests):
    from app.models import TaskQueue

    resp = _bulk(client, user_headers, server, "stop", 100, 101)

    assert resp.status_code == 403
    assert db_session.query(TaskQueue).count() == 0


def test_bulk_operation_allows_own_guests_with_action_permission(client, db_session, seed_users,
                                                                user_headers, server, guests):
    # The stock user role has vm:start but not vm:manage: bulk used to be
    # admin-only while single start/stop worked for owners.
    _grant(db_session, seed_users["user"], vm__start=True)
    assert _bulk(client, user_headers, server, "start", 100).status_code == 200
    assert _bulk(client, user_headers, server, "delete", 100).status_code == 403


def test_wildcard_role_counts_as_global_view(db_session, seed_users):
    from app.api.proxmox._helpers import can_view_all_instances

    user = seed_users["user"]
    assert not can_view_all_instances(user)
    _grant(db_session, user, **{"vm__*": True})
    assert can_view_all_instances(user)


# ── Backups ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("volid,vmid", [
    ("local:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst", 101),
    ("nfs:backup/vzdump-lxc-205-2024_01_01-00_00_00.tar.zst", 205),
    ("pbs:backup/vm/300/2024-01-01T00:00:00Z", 300),
    ("local:iso/debian.iso", None),
])
def test_archive_vmid(volid, vmid):
    from app.api.proxmox.backup import archive_vmid
    assert archive_vmid(volid) == vmid


def test_restore_of_foreign_archive_into_own_guest_is_refused(client, db_session, seed_users,
                                                             user_headers, server, guests,
                                                             mock_proxmox):
    _grant(db_session, seed_users["user"], backup__restore=True)
    body = {"server_id": server.id, "node": "pve1", "vmid": 100,
            "archive": "local:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst"}

    assert client.post("/proxmox/api/backups/restore", json=body,
                       headers=user_headers).status_code == 403
    body["new_vmid"] = 900
    body["archive"] = "local:backup/vzdump-qemu-100-2024_01_01-00_00_00.vma.zst"
    assert client.post("/proxmox/api/backups/restore", json=body,
                       headers=user_headers).status_code == 403


def test_bulk_restore_cannot_overwrite_foreign_guest(client, db_session, seed_users, user_headers,
                                                     server, guests, mock_proxmox):
    _grant(db_session, seed_users["user"], backup__restore=True)
    body = {"server_id": server.id, "mode": "in_place", "items": [
        {"node": "pve1", "vmid": 101, "vm_type": "qemu",
         "archive": "local:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst"}]}

    resp = client.post("/proxmox/api/backups/restore-bulk", json=body, headers=user_headers)

    assert resp.status_code == 403
    mock_proxmox["client"].restore_vm.assert_not_called()


def test_tenant_cannot_delete_foreign_backup(client, db_session, seed_users, user_headers,
                                             server, guests, mock_proxmox):
    _grant(db_session, seed_users["user"], backup__delete=True)
    seed_users["user"].assigned_servers.append(server)
    db_session.commit()

    resp = client.request(
        "DELETE", f"/proxmox/api/backups/{server.id}/backup", headers=user_headers,
        json={"node": "pve1", "storage": "local",
              "volid": "local:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst"},
    )

    assert resp.status_code == 403
    mock_proxmox["client"].delete_backup.assert_not_called()


def test_backup_job_vmids_are_authorized(client, db_session, seed_users, user_headers, server, guests):
    _grant(db_session, seed_users["user"], backup__manage=True)
    base = {"server_id": server.id, "node": "pve1", "storage": "local",
            "cron_expression": "0 3 * * *"}
    url = "/proxmox/api/backups/jobs"

    assert client.post(url, json={**base, "vmids": [101]}, headers=user_headers).status_code == 403
    assert client.post(url, json={**base, "vmids": ["all"]}, headers=user_headers).status_code == 403
    assert client.post(url, json={**base, "vmids": ["x"]}, headers=user_headers).status_code == 400
    assert client.post(url, json={**base, "vmids": [100], "cron_expression": "61 * * * *"},
                       headers=user_headers).status_code == 400
    resp = client.post(url, json={**base, "vmids": ["100"]}, headers=user_headers)
    assert resp.status_code == 200
    assert resp.json()["job"]["vmids"] == [100]


def test_backup_job_toggle_and_run_require_ownership(client, db_session, seed_users, user_headers, server):
    from app.models import BackupJob
    _grant(db_session, seed_users["user"], backup__manage=True, backup__create=True)
    job = BackupJob(server_id=server.id, node="pve1", vmids=[101], storage="local",
                    cron_expression="0 3 * * *", enabled=True, owner_id=seed_users["admin"].id)
    db_session.add(job)
    db_session.commit()

    assert client.patch(f"/proxmox/api/backups/jobs/{job.id}/toggle",
                        headers=user_headers).status_code == 403
    assert client.post(f"/proxmox/api/backups/jobs/{job.id}/run-now",
                       headers=user_headers).status_code == 403
    db_session.refresh(job)
    assert job.enabled is True
