"""
Автосборка золотого LXC-шаблона (Workstream B).

Автоматизирует ручную процедуру из docs/golden-template.md:
pveam download базового Debian → pct create временного CT → установка
Docker/compose → подготовка каталогов/сети/logrotate → (опц.) проверка nginx →
vzdump в vztmpl → удаление временного CT.

Результат — volid вида "<storage>:vztmpl/golden-docker_12_<arch>.tar.zst",
готовый для APPSTORE_GOLDEN_TEMPLATE.

Переиспользует SSH-инфраструктуру пайплайна установки (root@node,
pct/pvesm/pveam/vzdump). Прогресс отдаётся через callback on_step(step, progress).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from ..api.proxmox._helpers import _get_proxmox_client, get_next_vmid
from ..config import settings
from ..models import ProxmoxServer
from .pipeline import (
    PocError, StepCb, _node_ssh, _noop, _pct, _resolve_node, _run, _wait_ip,
)

# Скрипты, исполняемые ВНУТРИ контейнера через `pct exec ... bash -lc`.
# Важно: без одинарных кавычек (иначе сломают обёртку _shq в _pct).

_DOCKER_INSTALL = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl jq gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable docker
"""

_PREP = r"""
set -e
mkdir -p /opt/app /opt/app-data
docker network inspect tipi_main_network >/dev/null 2>&1 || docker network create tipi_main_network
cat >/etc/docker/daemon.json <<EOF
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
systemctl restart docker
"""

_VALIDATE = r"""
set -e
mkdir -p /opt/app && cd /opt/app
printf "services:\n  web:\n    image: nginx:alpine\n    ports:\n      - \"80:80\"\n" > docker-compose.yml
docker compose up -d
sleep 3
curl -sf http://localhost:80 >/dev/null && echo OK
docker compose down
rm -f /opt/app/docker-compose.yml
"""

_CLEANUP = r"""
set -e
apt-get clean || true
rm -rf /var/lib/apt/lists/*
rm -f /opt/app/docker-compose.yml
"""


_PVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class GoldenSpec:
    server_id: int
    node: Optional[str] = None
    template_storage: str = "local"       # dir-storage для базового и итогового vztmpl
    rootfs_storage: str = "local-lvm"     # storage для rootfs временного CT
    bridge: str = "vmbr0"
    arch: Optional[str] = None            # amd64|arm64; None → settings.APPSTORE_HOST_ARCH
    force: bool = False                   # пересобрать, даже если шаблон уже существует
    vmid: Optional[int] = None            # если None — берётся следующий свободный
    cores: int = 2
    memory: int = 2048                    # МБ
    swap: int = 512                       # МБ
    disk: int = 8                         # ГБ (rootfs временного CT)
    build_timeout: int = 1800             # сек на apt/docker install и vzdump
    validate: bool = True                 # тестовый nginx (DoD ТЗ 4.2)


@dataclass
class GoldenResult:
    volid: str
    node: str
    status: str        # "created" | "exists"


def build_golden_template(db: Session, spec: GoldenSpec,
                          on_step: StepCb = _noop) -> GoldenResult:
    # Interpolated into root shell commands below — accept plain PVE ids only.
    for label, value in (("template_storage", spec.template_storage),
                         ("rootfs_storage", spec.rootfs_storage), ("bridge", spec.bridge)):
        if not _PVE_ID_RE.match(value or ""):
            raise PocError(f"Недопустимое значение {label}: {value!r}")
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == spec.server_id).first()
    if not server:
        raise PocError(f"Proxmox сервер id={spec.server_id} не найден")

    on_step("Подключение к Proxmox...", 5)
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise PocError("Не удалось подключиться к Proxmox")

    node = _resolve_node(client, spec.node)
    arch = (spec.arch or settings.APPSTORE_HOST_ARCH or "amd64").strip().lower()
    target_name = f"golden-docker_12_{arch}.tar.zst"
    target_volid = f"{spec.template_storage}:vztmpl/{target_name}"

    ssh = _node_ssh(server, client)
    if not ssh.connect():
        raise PocError(ssh.last_error or "SSH к ноде не установлен (нужен root-доступ к ноде)")

    vmid: Optional[int] = None
    created_ct = False
    try:
        # Идемпотентность: шаблон уже есть и не запрошена пересборка.
        on_step("Проверка существующего шаблона...", 10)
        existing = _run(ssh, f"pvesm list {spec.template_storage} --content vztmpl",
                        what="list vztmpl", timeout=60)
        if target_name in existing and not spec.force:
            on_step(f"Шаблон уже существует: {target_volid}", 100)
            return GoldenResult(volid=target_volid, node=node, status="exists")

        # 1. Базовый шаблон Debian 12 — актуальное имя из pveam (не хардкодим версию).
        on_step("Поиск и загрузка базового шаблона Debian 12...", 20)
        _run(ssh, "pveam update", what="pveam update", timeout=120)
        base_name = _run(
            ssh,
            "pveam available --section system | awk '{print $2}' | "
            f"grep -E '^debian-12-standard_.*_{arch}\\.tar\\.zst$' | sort -V | tail -1",
            what="find debian template",
        ).strip()
        if not base_name:
            raise PocError(f"Не найден базовый шаблон debian-12-standard для арх. {arch}")

        have = _run(ssh, f"pvesm list {spec.template_storage} --content vztmpl",
                    what="list vztmpl", timeout=60)
        if base_name not in have:
            _run(ssh, f"pveam download {spec.template_storage} {base_name}",
                 what="pveam download", timeout=600)

        # 2. Временный CT из базового шаблона.
        vmid = spec.vmid or get_next_vmid(db, server.id)
        on_step(f"Создание временного CT {vmid}...", 30)
        _run(
            ssh,
            f"pct create {vmid} {spec.template_storage}:vztmpl/{base_name} "
            f"--hostname golden-docker --unprivileged 1 "
            f"--features nesting=1,keyctl=1 "
            f"--cores {spec.cores} --memory {spec.memory} --swap {spec.swap} "
            f"--rootfs {spec.rootfs_storage}:{spec.disk} "
            f"--net0 name=eth0,bridge={spec.bridge},ip=dhcp "
            f"--nameserver \"{settings.APPSTORE_NAMESERVER}\"",
            what="pct create", timeout=300,
        )
        created_ct = True

        on_step("Запуск CT и ожидание сети...", 40)
        _run(ssh, f"pct start {vmid}", what="pct start", timeout=120)
        ip = _wait_ip(ssh, vmid, 120)
        if not ip:
            raise PocError("Временный CT не получил IP за 120с (нет сети для apt/docker)")

        # 3. Установка Docker + утилит (долгий шаг).
        on_step("Установка Docker и Compose...", 55)
        _run(ssh, _pct(vmid, _DOCKER_INSTALL), what="install docker",
             timeout=spec.build_timeout)

        # 4. Каталоги, docker-сеть, logrotate.
        on_step("Подготовка каталогов, сети и logrotate...", 72)
        _run(ssh, _pct(vmid, _PREP), what="prepare template", timeout=300)

        # 5. Проверка DoD: тестовый nginx поднимается с первой попытки.
        if spec.validate:
            on_step("Проверка (тестовый nginx)...", 80)
            _run(ssh, _pct(vmid, _VALIDATE), what="validate nginx", timeout=300)

        # 6. Очистка и остановка.
        on_step("Очистка временного состояния...", 86)
        _run(ssh, _pct(vmid, _CLEANUP), what="cleanup", timeout=120)
        _run(ssh, f"pct stop {vmid}", what="pct stop", timeout=120)

        # 7. Экспорт rootfs в vztmpl через vzdump в каталог кэша storage.
        on_step("Экспорт шаблона (vzdump)...", 90)
        # Используем реальное имя уже скачанного базового шаблона, а не
        # выдуманное "probe": pvesm валидирует имя тома vztmpl по расширению
        # (.tar.gz/.tar.xz/.tar.zst) и роняет "unable to parse directory
        # volume name" на произвольной строке без такого расширения.
        probe = _run(ssh, f"pvesm path {spec.template_storage}:vztmpl/{base_name}",
                     what="resolve vztmpl dir").strip()
        cache_dir = os.path.dirname(probe)
        if not cache_dir:
            raise PocError(f"Не удалось определить каталог vztmpl для {spec.template_storage}")
        _run(ssh, f"vzdump {vmid} --dumpdir {cache_dir} --mode stop --compress zstd",
             what="vzdump export", timeout=spec.build_timeout)

        on_step("Переименование в золотой шаблон...", 96)
        artifact = _run(
            ssh,
            f"ls -t {cache_dir}/vzdump-lxc-{vmid}-*.tar.zst 2>/dev/null | head -1",
            what="find vzdump artifact",
        ).strip()
        if not artifact:
            raise PocError("vzdump не создал ожидаемый архив шаблона")
        _run(ssh, f"mv -f {artifact} {cache_dir}/{target_name}",
             what="rename to golden")

        # 8. Удаление временного CT.
        on_step("Удаление временного CT...", 98)
        _run(ssh, f"pct destroy {vmid} --purge 1", what="destroy temp CT", timeout=120)
        created_ct = False

        on_step(f"Готово: {target_volid}", 100)
        return GoldenResult(volid=target_volid, node=node, status="created")
    except Exception:
        # При сбое пытаемся убрать недособранный временный CT, чтобы не мусорить.
        if created_ct and vmid is not None:
            try:
                _run(ssh, f"pct stop {vmid}", what="cleanup stop", timeout=60)
            except Exception:
                pass
            try:
                _run(ssh, f"pct destroy {vmid} --purge 1", what="cleanup destroy", timeout=120)
            except Exception as e:
                logger.warning(f"[golden-template] cleanup destroy {vmid}: {e}")
        raise
    finally:
        ssh.close()
