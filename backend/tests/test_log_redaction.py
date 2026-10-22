"""Query-string secrets of WebSocket URLs must not reach the logs."""
import logging

from app.main import _RedactSecretsFilter, _redact_secrets


def test_ws_tokens_are_redacted():
    line = '1.2.3.4:5 - "WebSocket /proxmox/ws/vnc/1/pve/qemu/100?port=5900&vncticket=PVEVNC:abc&auth_ticket=h1&token=eyJ.x.y" [accepted]'

    out = _redact_secrets(line)

    assert "eyJ.x.y" not in out and "PVEVNC:abc" not in out and "h1" not in out
    assert "port=5900" in out and "token=***" in out


def test_filter_rewrites_uvicorn_records():
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                               '%s - "WebSocket %s" [accepted]', ("1.2.3.4", "/ws/tasks?token=secret"), None)

    assert _RedactSecretsFilter().filter(record) is True
    assert "secret" not in record.getMessage()
