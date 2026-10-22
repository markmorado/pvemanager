"""Smoke tests for ``app.auth`` — pure functions only (no DB)."""

from __future__ import annotations

import importlib
from datetime import timedelta

import jwt
import pytest
from fastapi import HTTPException


@pytest.fixture(scope="module")
def auth():
    return importlib.import_module("app.auth")


@pytest.fixture(scope="module")
def settings():
    from app.config import settings as s
    return s


class TestPasswordHashing:
    @pytest.mark.smoke
    def test_hash_then_verify(self, auth):
        pw = "Correct-Horse-Battery-Staple-1!"
        hashed = auth.get_password_hash(pw)
        assert hashed != pw
        assert auth.verify_password(pw, hashed) is True

    def test_verify_rejects_wrong_password(self, auth):
        hashed = auth.get_password_hash("right-password")
        assert auth.verify_password("wrong-password", hashed) is False

    def test_hash_is_salted(self, auth):
        # Two hashes of the same password must differ (random salt).
        a = auth.get_password_hash("same")
        b = auth.get_password_hash("same")
        assert a != b


class TestJWT:
    @pytest.mark.smoke
    def test_create_then_decode(self, auth):
        token = auth.create_access_token({"sub": "alice"})
        payload = auth.decode_access_token(token)
        assert payload["sub"] == "alice"
        assert "exp" in payload and "iat" in payload

    def test_session_binding_round_trips(self, auth):
        token = auth.create_access_token({"sub": "bob"}, session_token="sess-xyz")
        payload = auth.decode_access_token(token)
        assert payload["session"] == "sess-xyz"

    def test_expired_token_raises_401(self, auth):
        token = auth.create_access_token({"sub": "x"}, expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc:
            auth.decode_access_token(token)
        assert exc.value.status_code == 401

    def test_tampered_token_raises_401(self, auth):
        token = auth.create_access_token({"sub": "x"})
        tampered = token + "tamper"
        with pytest.raises(HTTPException) as exc:
            auth.decode_access_token(tampered)
        assert exc.value.status_code == 401

    def test_token_signed_with_wrong_secret_raises_401(self, auth, settings):
        bogus = jwt.encode({"sub": "x"}, "different-secret", algorithm=settings.ALGORITHM)
        with pytest.raises(HTTPException) as exc:
            auth.decode_access_token(bogus)
        assert exc.value.status_code == 401


class TestPermissionHelpers:
    def test_check_permission_true_for_admin(self, auth, fake_user_factory):
        admin = fake_user_factory(is_admin=True)
        assert auth.check_permission(admin, "vm:delete") is True

    def test_check_permission_false_when_role_lacks_it(self, auth, fake_user_factory):
        user = fake_user_factory(permissions={"vm:view": True})
        assert auth.check_permission(user, "vm:delete") is False

    def test_require_permission_raises_403(self, auth, fake_user_factory):
        user = fake_user_factory(permissions={})
        with pytest.raises(HTTPException) as exc:
            auth.require_permission(user, "vm:delete")
        assert exc.value.status_code == 403


class TestClientIPParsing:
    def _request_with_headers(self, headers: dict, client_host: str = "10.0.0.1"):
        # Minimal stand-in mimicking starlette.Request surface used by get_client_ip.
        from types import SimpleNamespace

        # headers must be lower-cased keys; .get is what get_client_ip uses.
        return SimpleNamespace(
            headers={k.lower(): v for k, v in headers.items()},
            client=SimpleNamespace(host=client_host),
        )

    def test_uses_proxy_appended_xff_entry(self, auth):
        # nginx's $proxy_add_x_forwarded_for appends the real peer; left-most
        # entries come from the client and must not be trusted.
        req = self._request_with_headers({"X-Forwarded-For": "1.2.3.4, 203.0.113.5"})
        assert auth.get_client_ip(req) == "203.0.113.5"

    def test_prefers_x_real_ip(self, auth):
        req = self._request_with_headers(
            {"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "1.2.3.4, 203.0.113.7"}
        )
        assert auth.get_client_ip(req) == "203.0.113.7"

    def test_ignores_proxy_headers_from_public_peer(self, auth):
        # A client hitting the API directly cannot choose its own address.
        req = self._request_with_headers(
            {"X-Real-IP": "10.9.9.9", "X-Forwarded-For": "10.9.9.9"},
            client_host="8.8.4.4",
        )
        assert auth.get_client_ip(req) == "8.8.4.4"

    def test_rejects_spoofed_xff(self, auth):
        # Non-IP value must be ignored — fall back to client.host.
        req = self._request_with_headers(
            {"X-Forwarded-For": "not-an-ip; rm -rf /"},
            client_host="10.0.0.99",
        )
        assert auth.get_client_ip(req) == "10.0.0.99"
