"""write_file_commands: payloads must fit one shell argument (MAX_ARG_STRLEN)."""
import base64
import re

from app.ssh_client import write_file_commands

_MAX_ARG_STRLEN = 128 * 1024
_CHUNK_RE = re.compile(r"printf '%s' '([A-Za-z0-9+/=]*)' \| base64 -d (>>?) /tmp/x$")


def _replay(cmds):
    out = b""
    for cmd in cmds:
        m = _CHUNK_RE.search(cmd)
        assert m, cmd
        chunk = base64.b64decode(m.group(1))
        out = chunk if m.group(2) == ">" else out + chunk
    return out


def test_large_payload_is_split_below_argument_limit():
    data = bytes(range(256)) * 2048  # 512 KiB, the App Store per-file limit
    cmds = write_file_commands("/tmp/x", data)

    assert len(cmds) > 1
    assert all(len(c.encode()) < _MAX_ARG_STRLEN for c in cmds)
    assert _replay(cmds) == data


def test_small_and_empty_payloads():
    assert _replay(write_file_commands("/tmp/x", b"hello")) == b"hello"
    assert len(write_file_commands("/tmp/x", b"hello")) == 1
    assert write_file_commands("/tmp/x", b"") == ["umask 077 && : > /tmp/x"]
