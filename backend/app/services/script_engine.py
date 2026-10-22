"""
Движок выполнения скриптов из каталога (backend/app/models/scripts.py).

Переиспользует существующую инфраструктуру:
- SSHClient (root@node) — выполнение на ноде и `pct exec` для LXC (тот же
  паттерн, что и backend/app/appstore/pipeline.py: base64 -> файл -> exec,
  без конкатенации пользовательского ввода в shell-команду).
- ProxmoxClient.execute_script() — выполнение на VM через QEMU guest agent
  (backend/app/proxmox/client.py), уже используется в
  POST /api/{server_id}/vm/{vmid}/execute-script.

Безопасность параметров: значения параметров НЕ подставляются в тело скрипта
текстово. Вместо этого перед скриптом добавляется блок `export VAR='...'` с
экранированием одинарных кавычек, скрипт читает их как обычные переменные
окружения — тот же принцип, что и .env в appstore pipeline.
"""

from __future__ import annotations

import re
import secrets
import shlex
from dataclasses import dataclass
from typing import Dict, Optional

from loguru import logger

from ..models import ProxmoxServer
from ..proxmox import ProxmoxClient
from ..ssh_client import SSHClient, write_file_commands

_CMD_TIMEOUT = 60

# Community-скрипты (build.func и т.п.) вызывают `clear` и печатают цветной
# вывод, которым нужен установленный TERM. Панель запускает скрипт без PTY
# (get_pty=False), поэтому TERM не наследуется — и `clear` падает с
# "TERM environment variable not set", обрывая весь скрипт. Задаём безопасный
# дефолт префиксом команды (надёжнее env paramiko: sshd AcceptEnv его режет).
_SCRIPT_ENV = "TERM=xterm"


class ScriptExecError(RuntimeError):
    """Ошибка выполнения скрипта с человекочитаемым сообщением."""


@dataclass
class ExecResult:
    success: bool
    output: str
    exit_code: int
    error: Optional[str] = None


def _shell_quote(value: str) -> str:
    """Обернуть значение в одинарные кавычки, безопасно для shell."""
    return "'" + str(value).replace("'", "'\\''") + "'"


_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_INTERPRETER_RE = re.compile(r"^/[A-Za-z0-9_./+-]{1,99}$")


def is_valid_param_name(name: str) -> bool:
    return bool(_PARAM_NAME_RE.match(name or ""))


def is_valid_interpreter(path: str) -> bool:
    return bool(_INTERPRETER_RE.match(path or "")) and ".." not in path


def _tmp_name(prefix: str) -> str:
    # Unpredictable name: a guest user cannot pre-plant a symlink or swap the
    # file between push and exec, and parallel runs do not collide.
    return f"/tmp/{prefix}_{secrets.token_hex(8)}"


def render_params(content: str, params: Dict[str, str]) -> str:
    """Добавить блок export-переменных перед телом скрипта.

    Скрипт обращается к параметрам как к обычным переменным окружения
    ($NAME), значения никогда не конкатенируются в исполняемые команды.
    """
    if not params:
        return content
    lines = ["#!/bin/sh", "# --- параметры, подставленные каталогом скриптов ---"]
    for name, value in params.items():
        # Имена приходят и из метаданных git-репозиториев — строка вида
        # `X;cmd;Y` стала бы командой. Невалидные имена отбрасываем.
        if not is_valid_param_name(name):
            logger.warning(f"[script_engine] skipping invalid parameter name {name!r}")
            continue
        lines.append(f"export {name}={_shell_quote('' if value is None else value)}")
    lines.append("# --- конец параметров ---")
    return "\n".join(lines) + "\n" + content


def _node_ssh(server: ProxmoxServer, client: ProxmoxClient) -> SSHClient:
    ssh_host = (server.ip_address or server.hostname or "").split(":")[0]
    return SSHClient(
        hostname=ssh_host,
        username="root",
        password=getattr(client, "_password", None),
        timeout=30,
    )


def _run(ssh: SSHClient, cmd: str, *, what: str, timeout: int = _CMD_TIMEOUT) -> str:
    output, exit_code = ssh.execute(cmd, return_exit_code=True, timeout=timeout)
    if exit_code == -1:
        raise ScriptExecError(f"{what}: таймаут (>{timeout}с) или обрыв SSH")
    if exit_code != 0:
        raise ScriptExecError(f"{what}: exit={exit_code}: {(output or '').strip()[:2000]}")
    return output or ""


def run_on_node(server: ProxmoxServer, client: ProxmoxClient, script: str,
                interpreter: str, timeout: int) -> ExecResult:
    """Выполнить скрипт на хосте Proxmox (root@node)."""
    ssh = _node_ssh(server, client)
    if not ssh.connect():
        return ExecResult(success=False, output="", exit_code=-1,
                           error=ssh.last_error or f"Не удалось подключиться по SSH к {server.name}")

    if not is_valid_interpreter(interpreter):
        return ExecResult(success=False, output="", exit_code=-1,
                          error=f"Недопустимый интерпретатор: {interpreter!r}")
    tmp = _tmp_name("pve_script")
    try:
        for cmd in write_file_commands(tmp, script.encode("utf-8")):
            _run(ssh, cmd, what="write script on node")
        _run(ssh, f"chmod 700 {tmp}", what="write script on node")
        output, exit_code = ssh.execute(f"{_SCRIPT_ENV} {shlex.quote(interpreter)} {tmp}",
                                        return_exit_code=True, timeout=timeout)
        return ExecResult(success=exit_code == 0, output=output or "", exit_code=exit_code)
    except ScriptExecError as e:
        return ExecResult(success=False, output="", exit_code=-1, error=str(e))
    finally:
        try:
            ssh.execute(f"rm -f {tmp}", timeout=10)
        except Exception:
            pass


def run_on_lxc(server: ProxmoxServer, client: ProxmoxClient, node: str, vmid: int,
               script: str, interpreter: str, timeout: int) -> ExecResult:
    """Выполнить скрипт внутри LXC через `pct exec` (SSH ноды, нет guest agent для LXC)."""
    ssh = _node_ssh(server, client)
    if not ssh.connect():
        return ExecResult(success=False, output="", exit_code=-1,
                           error=ssh.last_error or f"Не удалось подключиться по SSH к {server.name}")

    if not is_valid_interpreter(interpreter):
        return ExecResult(success=False, output="", exit_code=-1,
                          error=f"Недопустимый интерпретатор: {interpreter!r}")
    vmid = int(vmid)
    tmp_node = _tmp_name(f"pve_script_{vmid}")
    tmp_ct = _tmp_name("pve_script") + ".sh"
    try:
        for cmd in write_file_commands(tmp_node, script.encode("utf-8")):
            _run(ssh, cmd, what="write script on node")
        _run(ssh, f"pct push {vmid} {tmp_node} {tmp_ct} --perms 0700", what="pct push script")
        output, exit_code = ssh.execute(
            f"pct exec {vmid} -- env {_SCRIPT_ENV} {shlex.quote(interpreter)} {tmp_ct}",
            return_exit_code=True, timeout=timeout,
        )
        return ExecResult(success=exit_code == 0, output=output or "", exit_code=exit_code)
    except ScriptExecError as e:
        return ExecResult(success=False, output="", exit_code=-1, error=str(e))
    finally:
        try:
            ssh.execute(f"rm -f {tmp_node}", timeout=10)
            ssh.execute(f"pct exec {vmid} -- rm -f {tmp_ct}", timeout=10)
        except Exception:
            pass


def run_on_qemu(client: ProxmoxClient, node: str, vmid: int, script: str,
                interpreter: str, timeout: int) -> ExecResult:
    """Выполнить скрипт внутри VM через QEMU guest agent."""
    result = client.execute_script(node, vmid, script, interpreter=interpreter, timeout=timeout)
    if not result.get("success"):
        return ExecResult(
            success=False, output=result.get("stdout", "") or "",
            exit_code=result.get("exit_code", -1),
            error=result.get("error") or result.get("stderr"),
        )
    return ExecResult(
        success=result.get("exit_code", 0) == 0,
        output=result.get("stdout", "") or "",
        exit_code=result.get("exit_code", 0),
        error=result.get("stderr") or None,
    )


def execute(server: ProxmoxServer, *, target_type: str, node: Optional[str],
            vmid: Optional[int], vm_type: Optional[str], script: str,
            interpreter: str, timeout: int) -> ExecResult:
    """Точка входа: выполнить скрипт на нужной цели.

    target_type: "node" (хост Proxmox) | "guest" (VM/LXC).
    vm_type (только для guest): "qemu" | "lxc".
    """
    try:
        client = ProxmoxClient.from_server(server)
    except Exception as e:
        return ExecResult(success=False, output="", exit_code=-1,
                           error=f"Не удалось создать Proxmox-клиент: {e}")

    try:
        if target_type == "node":
            return run_on_node(server, client, script, interpreter, timeout)
        if target_type == "guest":
            if vmid is None or not node:
                return ExecResult(success=False, output="", exit_code=-1,
                                   error="Для guest-скрипта нужны node и vmid")
            if vm_type == "lxc":
                return run_on_lxc(server, client, node, vmid, script, interpreter, timeout)
            return run_on_qemu(client, node, vmid, script, interpreter, timeout)
        return ExecResult(success=False, output="", exit_code=-1,
                           error=f"Неизвестный target_type: {target_type}")
    except Exception as e:
        logger.error(f"[script_engine] execute failed: {e}")
        return ExecResult(success=False, output="", exit_code=-1, error=str(e))
