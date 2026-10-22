"""
M0 PoC install-пайплайн: clone (create from golden vztmpl) → push → exec → health-check.

Ядро переиспользует существующую инфраструктуру панели:
- `_get_proxmox_client()` / `create_lxc_container()` / `wait_for_task()` /
  `start_container()` / `stop_container()` / `delete_container()` — Proxmox API;
- `SSHClient` (root@node, тот же паттерн, что в proxmox/mixins/cluster.py) — для
  `pct exec` / `pct push` с полным выводом (нужно для логов и наблюдаемости шагов).

Это seed будущего services/appstore_engine.py. Прогресс отдаётся через callback
`on_step(step, progress)` — в реальном движке он станет WS-broadcast'ом.

Безопасность (S-4): содержимое docker-compose.yml и .env НЕ конкатенируется в
shell-строку. Файлы доставляются на ноду через base64→файл→`pct push`, поэтому
произвольный ввод не может вырваться в шелл. Имя приложения валидируется regex.
"""

from __future__ import annotations

import base64
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import yaml
from loguru import logger
from sqlalchemy.orm import Session

from ..api.proxmox._helpers import _get_proxmox_client, get_next_vmid
from ..config import settings
from ..models import ProxmoxServer
from ..ssh_client import SSHClient, write_file_commands

# Docker-сеть, на которую ссылаются compose-файлы Runtipi как external.
TIPI_NETWORK = "tipi_main_network"
APP_DIR = "/opt/app"           # сюда кладём docker-compose.yml + .env
APP_DATA_DIR = "/opt/app-data"  # значение APP_DATA_DIR для приложений

# Имя = hostname LXC: буквы/цифры/дефис, 1..63.
_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

StepCb = Callable[[str, int], None]


class PocError(RuntimeError):
    """Ошибка пайплайна с человекочитаемым шагом, на котором упали."""


@dataclass
class InstallSpec:
    server_id: int
    ostemplate: str                       # golden vztmpl, напр. local:vztmpl/golden-docker_12_amd64.tar.zst
    name: str                             # hostname / имя приложения
    compose_yaml: str                     # содержимое docker-compose.yml
    port: int = 3001                      # порт для health-check
    # Порт выбран пользователем явно (форма мастера), а не взят из каталога.
    # Влияет на нормализацию compose: явный порт публикуется как есть (в т.ч.
    # 80), а каталожные 0/80 считаются «не задано» → публикуем внутренний.
    port_explicit: bool = False
    env_vars: Dict[str, str] = field(default_factory=dict)
    node: Optional[str] = None            # если None — выбирается автоматически
    vmid: Optional[int] = None            # если задан — использовать его (идемпотентный повтор)
    cores: int = 2
    memory: int = 2048                    # МБ
    swap: int = 512                       # МБ
    disk: int = 8                         # ГБ
    storage: str = "local-lvm"
    bridge: str = "vmbr0"
    features: str = "nesting=1,keyctl=1"  # обязательно для Docker в unprivileged LXC
    health_timeout: int = 300             # сек
    ip_timeout: int = 60                  # сек ожидания IP после старта
    # IPAM: статическая конфигурация сети. Если ip_config задан — используется
    # вместо ip=dhcp при создании net0; static_ip — заранее известный адрес
    # (тогда не ждём выдачи по DHCP).
    ip_config: Optional[str] = None       # напр. "ip=10.0.0.5/24,gw=10.0.0.1"
    static_ip: Optional[str] = None       # напр. "10.0.0.5"
    source: str = "runtipi"               # источник каталога: runtipi | umbrel
    # Явный DNS для контейнера (через пробел). None → settings.APPSTORE_NAMESERVER —
    # без этого CT наследует резолвер ноды, который может быть недоступен из сети
    # контейнера (напр. Tailscale-адрес), и `docker compose up` падает на pull образов.
    nameserver: Optional[str] = None
    # Вспомогательные файлы приложения из репозитория каталога
    # ({relpath: {"b64": ..., "exec": bool}}, см. CatalogApp.data_files) —
    # доставляются в ${APP_DATA_DIR}/<relpath> ДО compose up, иначе bind-mount'ы
    # на них получают пустышки и приложение падает (пустой entrypoint.sh/конфиг).
    data_files: Dict[str, dict] = field(default_factory=dict)


@dataclass
class InstallResult:
    vmid: int
    node: str
    ip: Optional[str]
    url: Optional[str]
    status: str


def _noop(step: str, progress: int) -> None:
    logger.info(f"[appstore-poc] {progress:3d}% {step}")


# ── SSH-хелперы (root@node), идентичный паттерн cluster.py ────────────────────

def _node_ssh(server: ProxmoxServer, client) -> SSHClient:
    ssh_host = (server.ip_address or server.hostname or "").split(":")[0]
    return SSHClient(
        hostname=ssh_host,
        username="root",
        password=getattr(client, "_password", None),
        timeout=30,  # запас на подключение; per-command таймаут задаётся в _run
    )


# Таймаут по умолчанию для быстрых команд на ноде (pct exec/push, mkdir и т.п.).
# Долгие шаги (docker compose pull/up) передают увеличенный таймаут явно.
_CMD_TIMEOUT = 60
_DOCKER_TIMEOUT = 600
# Ожидание завершения задачи Proxmox на удаление CT. Долгий таймаут — при
# wipe_after_delete на LVM-хранилищах затирание диска нулями может занимать
# много минут; если сдаться раньше, VMID можно ошибочно счесть свободным и
# словить гонку блокировки конфига при немедленном пересоздании.
_TEARDOWN_WAIT_TIMEOUT = 1800


_LAYER_PROGRESS_RE = re.compile(
    r"^\s*[0-9a-f]{12}\s+(Pulling|Waiting|Downloading|Verifying Checksum|"
    r"Download complete|Extracting|Pull complete|Already exists)\b"
)


def _strip_pull_noise(output: str) -> str:
    """Убрать построчный progress-шум `docker compose pull/up` (на каждый слой
    образа своя строка Downloading/Extracting и т.п., перерисовывающаяся много
    раз). На тяжёлых многослойных образах этот шум может занять весь хвост
    вывода, вытеснив из среза саму причину падения (см. _run).
    """
    lines = [ln for ln in output.splitlines() if not _LAYER_PROGRESS_RE.match(ln)]
    return "\n".join(lines)


def _run(ssh: SSHClient, cmd: str, *, what: str, timeout: int = _CMD_TIMEOUT) -> str:
    """Выполнить команду на ноде, вернуть stdout. Бросить PocError при exit!=0.

    При таймауте execute() вернёт exit_code=-1 → бросаем PocError (установка
    завершается с ошибкой, а не зависает бесконечно).
    """
    output, exit_code = ssh.execute(cmd, return_exit_code=True, timeout=timeout)
    if exit_code == -1:
        raise PocError(f"{what}: таймаут (>{timeout}с) или обрыв SSH")
    if exit_code != 0:
        # Хвост, а не голова: для многословных команд (docker compose pull/up)
        # реальная причина падения — последние строки, а не progress-шум
        # "Pulling/Downloading" в начале. Раньше срез [:2000] обрезал именно
        # причину, оставляя в UI один прогресс-бар без объяснения. Но и хвост
        # может целиком утонуть в построчном progress-шуме на тяжёлых образах
        # (много слоёв) — поэтому сначала вычищаем его, если после этого
        # что-то остаётся.
        text = (output or "").strip()
        filtered = _strip_pull_noise(text).strip()
        tail = filtered or text
        raise PocError(f"{what}: exit={exit_code}: {tail[-4000:]}")
    return output or ""


def _pct(vmid: int, inner: str) -> str:
    """pct exec в контейнере через bash -lc (inner фиксированный, без польз. ввода)."""
    return f"pct exec {vmid} -- /bin/bash -lc {_shq(inner)}"


def _shq(s: str) -> str:
    """Одинарные кавычки для shell (безопасно для фиксированных строк)."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _to_int(v) -> Optional[int]:
    """Аккуратно привести значение к int (int или строка-число), иначе None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _find_main_service(services: Dict) -> Optional[str]:
    """Определить главный сервис Runtipi-compose.

    Приоритет: сервис с `x-runtipi.is_main: true`. Если такого нет, но сервис
    единственный — берём его. Иначе None (не можем однозначно определить).
    """
    for name, svc in services.items():
        if isinstance(svc, dict):
            rt = svc.get("x-runtipi")
            if isinstance(rt, dict) and rt.get("is_main"):
                return name
    if len(services) == 1:
        return next(iter(services))
    return None


def _compose_env_get(env, key: str) -> Optional[str]:
    """Достать значение переменной из секции environment (dict или list-формат)."""
    if isinstance(env, dict):
        v = env.get(key)
        return str(v) if v is not None else None
    if isinstance(env, list):
        pref = key + "="
        for item in env:
            s = str(item)
            if s.startswith(pref):
                return s[len(pref):]
    return None


# Сервисы рантайма Umbrel, которые не запустятся вне Umbrel и должны быть
# вырезаны из compose перед standalone-запуском в LXC.
def _is_umbrel_infra_service(name: str) -> bool:
    low = name.lower()
    return low == "app_proxy" or low == "tor" or low.endswith("_tor") or low.endswith("-tor")


def _strip_dangling_depends(services: Dict, removed: set) -> None:
    """Убрать depends_on-ссылки на удалённые сервисы (иначе compose up падает)."""
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        dep = svc.get("depends_on")
        if isinstance(dep, list):
            new = [d for d in dep if d not in removed]
            if new:
                svc["depends_on"] = new
            else:
                svc.pop("depends_on", None)
        elif isinstance(dep, dict):
            for r in removed:
                dep.pop(r, None)
            if not dep:
                svc.pop("depends_on", None)


def _umbrel_main_service(services: Dict, app_host: Optional[str]) -> Optional[str]:
    """Определить главный сервис Umbrel-compose.

    Приоритет — сервис, на который указывает app_proxy (APP_HOST). Затем —
    единственный сервис, затем первый с образом.
    """
    if not services:
        return None
    if app_host:
        for name in services:
            if name == app_host or name in app_host.split("_") or name in app_host:
                return name
    if len(services) == 1:
        return next(iter(services))
    for name, svc in services.items():
        if isinstance(svc, dict) and svc.get("image"):
            return name
    return next(iter(services))


def _prepare_umbrel(doc: dict) -> tuple[Optional[str], Optional[int], bool]:
    """Адаптировать Umbrel-compose: снять порт из app_proxy, вырезать рантайм
    Umbrel, определить главный сервис.

    Возвращает (main_service, internal_port, changed).
    """
    services = doc.get("services")
    if not isinstance(services, dict):
        return None, None, False

    # Порт контейнера и целевой сервис задаёт app_proxy Umbrel.
    ap = services.get("app_proxy")
    internal: Optional[int] = None
    app_host: Optional[str] = None
    if isinstance(ap, dict):
        env = ap.get("environment")
        internal = _to_int(_compose_env_get(env, "APP_PORT"))
        app_host = _compose_env_get(env, "APP_HOST")

    # Вырезать app_proxy/tor — их образов у нас нет, они завесят/уронят up.
    removed = {n for n in list(services.keys()) if _is_umbrel_infra_service(n)}
    for n in removed:
        services.pop(n, None)
    if removed:
        _strip_dangling_depends(services, removed)

    main = _umbrel_main_service(services, app_host)
    return main, internal, bool(removed)


def _resolve_main_and_port(doc: dict, services: Dict, source: str
                           ) -> tuple[Optional[str], Optional[int], bool]:
    """Определить (главный_сервис, внутренний_порт, changed) по источнику."""
    if source == "umbrel":
        return _prepare_umbrel(doc)
    # runtipi: главный сервис и порт объявлены через x-runtipi.
    main = _find_main_service(services)
    internal = None
    if main is not None:
        svc = services.get(main)
        rt = svc.get("x-runtipi") if isinstance(svc, dict) else None
        internal = _to_int(rt.get("internal_port")) if isinstance(rt, dict) else None
    return main, internal, False


def _published_host_port(entry, host_port: Optional[int]) -> Optional[int]:
    """Хост-порт из записи `ports:` compose.

    Понимает short-syntax ('8080:80', '${APP_PORT:-3001}:3001',
    '127.0.0.1:8080:80') и long-syntax ({published: ..., target: ...}).
    Переменную APP_PORT подставляем значением host_port — именно его кладём
    в .env, т.е. так её раскроет и docker compose. Раньше '${APP_PORT...'
    парсился как не-число → health-check шёл на внутренний порт, который
    наружу не опубликован, и установка ложно помечалась unhealthy.
    """
    env = {"APP_PORT": str(host_port)} if host_port else {}
    if isinstance(entry, dict):
        pub = entry.get("published")
        return _to_int(_subst_env(str(pub), env)) if pub is not None else None
    parts = _subst_env(str(entry), env).split(":")
    return _to_int(parts[-2]) if len(parts) >= 2 else None


def _normalize_compose(compose_yaml: str, host_port: Optional[int] = None,
                       *, source: str = "runtipi",
                       port_explicit: bool = False) -> tuple[str, Optional[int]]:
    """Привести compose к самодостаточному виду для standalone-запуска в LXC.

    Делает то, что в родном рантайме (Runtipi/Umbrel) выполняет их оркестратор:

    1. Объявляет упомянутые, но необъявленные сети (напр. `tipi_main_network`)
       как external — иначе `docker compose up` падает с «refers to undefined
       network …». Сеть создаётся заранее на ноде (`docker network create`).

    2. Для Umbrel: вырезает сервисы рантайма (app_proxy, tor) — их образов у нас
       нет, а порт контейнера берётся из `app_proxy.environment.APP_PORT`.

    3. Публикует порт главного сервиса на хост LXC. И Runtipi (порт в
       `x-runtipi.internal_port`), и Umbrel (порт в app_proxy) обычно НЕ имеют
       секции `ports:` — наружу его пробрасывал бы их прокси, которого у нас нет.
       Без публикации приложение недоступно по IP контейнера (health-check не
       проходит). Поэтому главному сервису добавляем `ports: ["<host>:<internal>"]`.

    Возвращает `(normalized_yaml, published_port)`. При любой ошибке разбора —
    `(compose_yaml, None)` (используем исходный compose как есть).
    """
    try:
        doc = yaml.safe_load(compose_yaml)
        if not isinstance(doc, dict):
            return compose_yaml, None

        services = doc.get("services")
        if not isinstance(services, dict):
            return compose_yaml, None

        # Источнико-специфичная подготовка (для Umbrel мутирует services).
        main, internal, changed = _resolve_main_and_port(doc, services, source)
        services = doc.get("services")
        if not isinstance(services, dict):
            return compose_yaml, None

        # (1) Необъявленные сети → external.
        referenced: set[str] = set()
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            nets = svc.get("networks")
            if isinstance(nets, list):
                referenced.update(str(n) for n in nets)
            elif isinstance(nets, dict):
                referenced.update(str(n) for n in nets.keys())

        if referenced:
            top = doc.get("networks")
            if not isinstance(top, dict):
                top = {}
            for net in referenced:
                if net not in top:
                    top[net] = {"external": True}
                    changed = True
            if changed:
                doc["networks"] = top

        # Для Umbrel, если порт контейнера не удалось снять из app_proxy, но
        # известен порт из каталога — используем его как внутренний.
        if source == "umbrel" and internal is None and host_port:
            internal = host_port

        # (2) Публикация порта главного сервиса на хост LXC.
        published_port: Optional[int] = None
        svc = services.get(main) if main is not None else None
        if isinstance(svc, dict):
            if internal:
                # Уважаем уже объявленную публикацию (не переопределяем).
                if not svc.get("ports"):
                    # host_port из каталога, если задан и не дефолтные 80/0; иначе
                    # публикуем на том же номере, что слушает контейнер. Порт,
                    # явно выбранный пользователем в мастере, уважаем всегда
                    # (в т.ч. 80 — для каталога это «не задано», для юзера нет).
                    pub = host_port if (host_port and (port_explicit or host_port not in (0, 80))) else internal
                    svc["ports"] = [f"{pub}:{internal}"]
                    published_port = pub
                    changed = True
                else:
                    ports = svc.get("ports")
                    if isinstance(ports, list) and ports:
                        published_port = _published_host_port(ports[0], host_port) or internal
            elif svc.get("ports"):
                # Внутренний порт неизвестен, но публикация уже есть в compose.
                ports = svc.get("ports")
                if isinstance(ports, list) and ports:
                    published_port = _published_host_port(ports[0], host_port)

        if not changed:
            return compose_yaml, published_port

        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), published_port
    except Exception as e:
        logger.warning(f"[appstore-poc] normalize compose failed, using raw: {e}")
        return compose_yaml, None


def _platform_env(env: Dict[str, str], ip: str, source: str) -> Dict[str, str]:
    """Заполнить IP-зависимые платформенные переменные окружения (ТЗ 5.3).

    Общие: APP_DOMAIN/APP_HOST/APP_IP. runtipi → LOCAL_DOMAIN. umbrel →
    DEVICE_DOMAIN_NAME/DEVICE_HOSTNAME/APP_SEED (частые ссылки в их compose).
    """
    env.setdefault("APP_DOMAIN", ip)
    env.setdefault("APP_HOST", ip)
    env["APP_IP"] = ip
    env.setdefault("APP_DATA_DIR", APP_DATA_DIR)  # некоторые compose ссылаются на неё в volumes:
    if source == "umbrel":
        env.setdefault("DEVICE_DOMAIN_NAME", f"{ip}.local")
        env.setdefault("DEVICE_HOSTNAME", "umbrel")
        env.setdefault("APP_SEED", "pvemanager")
        # Umbrel генерирует APP_PASSWORD сам (deterministic app password) —
        # compose-файлы ссылаются на неё для паролей/ключей. Обычно её задаёт
        # build_env движка (чтобы значение сохранилось и показалось
        # пользователю); здесь — страховка для standalone-запуска пайплайна.
        env.setdefault("APP_PASSWORD", secrets.token_hex(16))
    else:
        env.setdefault("LOCAL_DOMAIN", "tipi.local")  # стандартная переменная Runtipi
    return env


_VAR_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


def _subst_env(value: str, env: Dict[str, str]) -> str:
    """Мини-версия подстановки переменных docker compose:
    ${VAR}, ${VAR-def}, ${VAR:-def}, $VAR → env[VAR] | default | ''.
    """
    def _rep(m: re.Match) -> str:
        var = m.group(1) or m.group(3)
        val = env.get(var)
        if val:
            return val
        return m.group(2) or ""
    return _VAR_RE.sub(_rep, value)


def _bind_mount_hosts(compose_yaml: str, app_dir: str, env: Dict[str, str]) -> list:
    """Хост-пути bind-mount'ов из `volumes:` всех сервисов (без именованных volume).

    Официальные образы (mariadb, postgres и т.п.) в контейнере обычно пишут в
    точку монтирования от имени непривилегированного пользователя (напр.
    mysql, uid 999). Docker при первом `up` создаёт отсутствующую хост-директорию
    сам — но от root, с 0755, поэтому такой пользователь получает Permission
    denied. Возвращаем список путей, чтобы pre-create их с правами 777 (ТЗ:
    изоляция на уровне LXC, разграничение владельца внутри контейнера не
    требуется — важна лишь возможность писать).
    """
    try:
        doc = yaml.safe_load(compose_yaml)
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    services = doc.get("services")
    if not isinstance(services, dict):
        return []

    hosts: list = []
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes")
        if not isinstance(vols, list):
            continue
        for v in vols:
            if not isinstance(v, str) or ":" not in v:
                continue
            # Именованный volume ("dbdata:/var/lib/mysql") — без '/', '.', '~', '$'
            # в начале; управляется Docker'ом, пропускаем.
            if not (v.startswith(("/", ".", "~", "$"))):
                continue
            # Подстановка ДО split(':'): внутри ${VAR:-default} есть ':',
            # который иначе рвёт строку не на границе source:target.
            resolved = _subst_env(v, env).split(":", 1)[0]
            if not resolved:
                continue
            if not resolved.startswith("/"):
                resolved = f"{app_dir}/{resolved.lstrip('./')}"
            hosts.append(resolved)
    # uniq, сохраняя порядок
    return list(dict.fromkeys(hosts))


def _is_file_mount(source: str) -> bool:
    """Эвристика: source похож на файл, а не директорию (по имени — есть точка,
    напр. `.env`, `config.yml`, `db.sqlite`). Docker при отсутствующем source
    создаёт директорию — для файловых bind-mount'ов (напр. AnythingLLM монтирует
    `.env` как файл) это ломает `docker compose up` ошибкой "not a directory:
    Are you trying to mount a directory onto a file?".

    Скрытые имена (`.config`, `.n8n`, `.ssh`) — почти всегда ДИРЕКТОРИИ:
    ведущая точка не считается расширением. Файлы среди них — только `.env*`.
    """
    name = source.rsplit("/", 1)[-1]
    if name.startswith("."):
        return name.lower().startswith(".env")
    return "." in name


def _prepare_bind_mounts(ssh: SSHClient, vmid: int, compose_yaml: str,
                         app_dir: str, env: Dict[str, str]) -> None:
    """Pre-create хост-путей bind-mount'ов с правами 777/666 (см. _bind_mount_hosts).

    Директории — `mkdir -p`; файлоподобные source (см. _is_file_mount) — `touch`
    родительский путь как файл, иначе Docker создаст там директорию сам.
    """
    hosts = _bind_mount_hosts(compose_yaml, app_dir, env)
    if not hosts:
        return
    dirs = [h for h in hosts if not _is_file_mount(h)]
    files = [h for h in hosts if _is_file_mount(h)]
    cmds = []
    if dirs:
        q = " ".join(_shq(h) for h in dirs)
        cmds.append(f"mkdir -p {q} && chmod -R 777 {q}")
    for f in files:
        parent = f.rsplit("/", 1)[0] if "/" in f else "."
        qf, qp = _shq(f), _shq(parent)
        # rmdir — самовосстановление после старого бага: если source уже был
        # ошибочно создан Docker'ом как ПУСТАЯ директория в прошлой попытке,
        # убираем её перед touch; иначе Docker не может смонтировать файл поверх
        # директории ("not a directory: ... mount a directory onto a file").
        # Непустую директорию не трогаем (там могут быть данные приложения —
        # ошибка эвристики не должна приводить к потере данных).
        # chmod 777, а не 666: доставленные из каталога скрипты (entrypoint.sh
        # и т.п.) должны сохранить/получить exec-бит, иначе контейнер падает
        # с "permission denied" на entrypoint.
        cmds.append(
            f"mkdir -p {qp}; if [ -d {qf} ]; then rmdir {qf} 2>/dev/null || true; fi; "
            f"[ -e {qf} ] || touch {qf}; [ -d {qf} ] || chmod 777 {qf}"
        )
    _run(ssh, _pct(vmid, " && ".join(cmds)), what="prepare bind-mount dirs")


def _push_file(ssh: SSHClient, vmid: int, content, dest: str, *,
               perms: Optional[str] = None) -> None:
    """Доставить файл в контейнер без shell-инъекций: base64 → файл на ноде → pct push.

    content — str (utf-8) или bytes (бинарные файлы каталога).
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    # Random name: .env carries app secrets, a guessable /tmp path could be
    # pre-planted as a symlink on the node.
    tmp = f"/tmp/appstore_{vmid}_{secrets.token_hex(8)}"
    for cmd in write_file_commands(tmp, raw):
        _run(ssh, cmd, what=f"write {dest} on node")
    perms_arg = f" --perms {perms}" if perms else ""
    _run(ssh, f"pct push {vmid} {tmp} {dest}{perms_arg}", what=f"pct push {dest}")
    _run(ssh, f"rm -f {tmp}", what="cleanup temp")


# Имя relpath data-файла: только безопасные символы (dest не квотируется в
# `pct push`), без '..' и ведущих '/' — защита от path traversal и инъекций.
_DATA_RELPATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-@]*$")


# Плейсхолдер Runtipi-шаблона: {{VAR}} / {{ VAR }} в файлах data/*.template.
_TPL_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _render_template(blob: bytes, env: Dict[str, str]) -> bytes:
    """Рендер Runtipi-шаблона: {{VAR}} → env[VAR] (отсутствующие → '')."""
    text = blob.decode("utf-8", "replace")
    text = _TPL_VAR_RE.sub(lambda m: str(env.get(m.group(1), "")), text)
    return text.encode("utf-8")


def _push_data_files(ssh: SSHClient, vmid: int, data_files: Dict[str, dict],
                     env: Dict[str, str]) -> None:
    """Доставить файлы приложения из каталога в ${APP_DATA_DIR}/<relpath>.

    Compose bind-mount'ит их (напр. ${APP_DATA_DIR}/data/nginx.conf,
    ${APP_DATA_DIR}/entrypoint.sh) — без доставки на их месте оказались бы
    пустые файлы-заглушки, и приложение падает на старте. Exec-бит
    сохраняется (скрипты); прочим файлам даём 666 (владелец внутри
    контейнера не разграничивается — см. _bind_mount_hosts).

    Файлы `*.template` (конвенция Runtipi) рендерятся ({{VAR}} → env) и
    кладутся под именем без суффикса — именно его монтирует compose.
    """
    safe: Dict[str, bytes] = {}
    execs: Dict[str, bool] = {}
    for relpath, meta in (data_files or {}).items():
        if not isinstance(meta, dict) or not meta.get("b64"):
            continue
        if not _DATA_RELPATH_RE.match(relpath) or ".." in relpath:
            logger.warning(f"[appstore-poc] skip data file with unsafe path: {relpath!r}")
            continue
        try:
            blob = base64.b64decode(meta["b64"])
        except Exception:
            logger.warning(f"[appstore-poc] skip data file {relpath}: битый base64")
            continue
        if relpath.endswith(".template"):
            relpath = relpath[: -len(".template")]
            blob = _render_template(blob, env)
        safe[relpath] = blob
        execs[relpath] = bool(meta.get("exec"))
    if not safe:
        return

    # Заранее создаём все родительские директории одним pct exec.
    dirs = sorted({f"{APP_DATA_DIR}/{rp}".rsplit("/", 1)[0] for rp in safe})
    _run(ssh, _pct(vmid, "mkdir -p " + " ".join(_shq(d) for d in dirs)),
         what="mkdir data-file dirs")

    for relpath, blob in safe.items():
        _push_file(ssh, vmid, blob, f"{APP_DATA_DIR}/{relpath}",
                   perms="0777" if execs[relpath] else "0666")


def _env_file_value(v) -> str:
    """Значение для строки .env: одна строка на переменную + кавычение.

    dotenv-парсер docker compose считает ` #` в незакавыченном значении началом
    inline-комментария и обрезает хвост — пароль вида "pa#ss" молча превращался
    в "pa". Такие значения (а также с ведущими/хвостовыми пробелами) оборачиваем
    в двойные кавычки. Остальные оставляем как есть, чтобы не менять поведение
    подстановки ($VAR) для существующих приложений.
    """
    s = str(v).replace("\r", " ").replace("\n", " ")
    if "#" in s or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_file_content(env: Dict[str, str]) -> str:
    # A key with '=' or a newline would smuggle extra variables into .env.
    return "".join(
        f"{k}={_env_file_value(v)}\n" for k, v in env.items() if _ENV_KEY_RE.match(str(k))
    )


def _wait_ip(ssh: SSHClient, vmid: int, timeout: int) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, code = ssh.execute(f"pct exec {vmid} -- hostname -I", return_exit_code=True)
        if code == 0 and out and out.strip():
            ip = out.strip().split()[0]
            if ip and not ip.startswith("127."):
                return ip
        time.sleep(3)
    return None


# ── Выбор ноды (как в _do_lxc_deploy) ─────────────────────────────────────────

def _resolve_node(client, requested: Optional[str]) -> str:
    if requested:
        return requested
    nodes = client.get_nodes()
    if not nodes:
        raise PocError("Не найдено нод Proxmox")
    for n in nodes:
        try:
            for s in client.proxmox.nodes(n["node"]).storage.get():
                if "vztmpl" in s.get("content", ""):
                    return n["node"]
        except Exception:
            pass
    return nodes[0]["node"]


# ── Основной пайплайн ─────────────────────────────────────────────────────────

def poc_install(db: Session, spec: InstallSpec, on_step: StepCb = _noop) -> InstallResult:
    if not _NAME_RE.match(spec.name):
        raise PocError(f"Недопустимое имя приложения: {spec.name!r} (a-z, 0-9, дефис)")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == spec.server_id).first()
    if not server:
        raise PocError(f"Proxmox сервер id={spec.server_id} не найден")

    on_step("Подключение к Proxmox...", 5)
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise PocError("Не удалось подключиться к Proxmox")

    node = _resolve_node(client, spec.node)
    vmid = spec.vmid or get_next_vmid(db, server.id)

    # Идемпотентность повтора (ТЗ 6.1): если контейнер с этим vmid уже создан —
    # не пересоздаём, продолжаем с доставки/запуска compose.
    already_exists = False
    if spec.vmid:
        try:
            already_exists = bool(client.get_container_status(node, vmid))
        except Exception:
            already_exists = False

    if already_exists:
        on_step(f"Контейнер VMID {vmid} уже существует — повтор без пересоздания", 40)
    else:
        on_step(f"Создание LXC VMID {vmid} на ноде {node}...", 20)
        ip_cfg = spec.ip_config or "ip=dhcp"
        net0 = f"name=eth0,bridge={spec.bridge},{ip_cfg},firewall=1"
        try:
            upid = client.create_lxc_container(
                node=node,
                vmid=vmid,
                ostemplate=spec.ostemplate,
                hostname=spec.name,
                cores=spec.cores,
                memory=spec.memory,
                swap=spec.swap,
                storage=spec.storage,
                rootfs_size=spec.disk,
                net0=net0,
                features=spec.features,       # nesting=1,keyctl=1 — критично для Docker
                unprivileged=True,
                start_after_create=False,
                description=f"pvemanager-appstore: {spec.name}",
                nameserver=spec.nameserver or settings.APPSTORE_NAMESERVER,
            )
        except Exception as e:
            raise PocError(f"Ошибка создания контейнера: {e}") from e
        if not upid:
            raise PocError("Proxmox не вернул UPID создания контейнера")

        on_step("Ожидание завершения создания...", 35)
        if not client.wait_for_task(node, upid, timeout=300):
            raise PocError("Таймаут создания контейнера (>5 мин)")

    on_step("Запуск контейнера...", 45)
    client.start_container(node, vmid)

    ssh = _node_ssh(server, client)
    if not ssh.connect():
        raise PocError(ssh.last_error or "SSH к ноде не установлен (нужен root-доступ к ноде для pct exec/push)")
    try:
        on_step("Ожидание IP-адреса...", 55)
        # Статический IP из IPAM известен заранее — подтверждаем через контейнер,
        # но при таймауте всё равно используем выделенный адрес.
        ip = _wait_ip(ssh, vmid, spec.ip_timeout) or spec.static_ip
        if not ip:
            raise PocError(f"Контейнер не получил IP за {spec.ip_timeout}с")

        on_step("Подготовка каталогов и Docker-сети...", 65)
        _run(ssh, _pct(vmid, f"mkdir -p {APP_DIR} {APP_DATA_DIR}"), what="mkdir app dirs")
        _run(
            ssh,
            _pct(vmid, f"docker network inspect {TIPI_NETWORK} >/dev/null 2>&1 || "
                       f"docker network create {TIPI_NETWORK}"),
            what="ensure docker network",
        )

        on_step("Доставка docker-compose.yml и .env (pct push)...", 75)
        normalized, pub_port = _normalize_compose(spec.compose_yaml, spec.port,
                                                  source=spec.source,
                                                  port_explicit=spec.port_explicit)
        if pub_port:
            # Порт, реально опубликованный на хосте LXC → на нём и проверяем/URL.
            spec.port = pub_port
        _push_file(ssh, vmid, normalized, f"{APP_DIR}/docker-compose.yml")
        # IP-зависимые платформенные переменные (ТЗ 5.3) заполняем здесь — IP уже известен.
        env = _platform_env(dict(spec.env_vars), ip, spec.source)
        _push_file(ssh, vmid, _env_file_content(env), f"{APP_DIR}/.env")
        # Файлы приложения из репозитория каталога (конфиги/скрипты) — ДО
        # подготовки bind-mount'ов, чтобы на их местах не появились пустышки.
        _push_data_files(ssh, vmid, spec.data_files, env)
        # Bind-mount директории заранее и с правами 777 — иначе Docker создаст
        # их сам от root при первом `up`, и процессы в контейнере (напр.
        # mariadb от uid mysql) получат Permission denied на запись.
        _prepare_bind_mounts(ssh, vmid, normalized, APP_DIR, env)

        on_step("Запуск docker compose up -d...", 85)
        _run(
            ssh,
            _pct(vmid, f"cd {APP_DIR} && docker compose --env-file .env up -d 2>&1"),
            what="docker compose up",
            timeout=_DOCKER_TIMEOUT,  # тянет образы — может идти минуты
        )

        on_step(f"Health-check http://{ip}:{spec.port} ...", 92)
        ok = _health_check(ip, spec.port, spec.health_timeout)
        status = "running" if ok else "unhealthy"
        url = f"http://{ip}:{spec.port}"
        on_step(f"Готово ({status}): {url}", 100)
        return InstallResult(vmid=vmid, node=node, ip=ip, url=url, status=status)
    finally:
        ssh.close()


def _health_check(ip: str, port: int, timeout: int) -> bool:
    """Успех = любой HTTP-ответ (включая 3xx/401). Ретраи каждые 5с до timeout."""
    import httpx

    deadline = time.time() + timeout
    url = f"http://{ip}:{port}"
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=5, follow_redirects=False)
            logger.info(f"[appstore-poc] health {url} -> HTTP {r.status_code}")
            return True
        except Exception:
            time.sleep(5)
    return False


def poc_update(server, client, vmid: int, node: str, compose_yaml: str,
               env_vars: Dict[str, str], port: int, ip: str,
               on_step: StepCb = _noop, health_timeout: int = 300,
               source: str = "runtipi",
               data_files: Optional[Dict[str, dict]] = None,
               port_explicit: bool = False) -> tuple[str, int]:
    """Обновление приложения в существующем контейнере (M4): доставка нового
    docker-compose.yml → docker compose pull && up -d → health-check.

    Возвращает ('running' | 'unhealthy', эффективный_порт). Порт возвращаем,
    потому что нормализация compose может опубликовать порт, отличный от
    сохранённого в БД (ia.port), — иначе URL/health в БД укажут не туда.
    """
    ssh = _node_ssh(server, client)
    if not ssh.connect():
        raise PocError(ssh.last_error or "SSH к ноде не установлен (нужен root-доступ для pct exec/push)")
    try:
        on_step("Доставка нового docker-compose.yml...", 45)
        _run(ssh, _pct(vmid, f"mkdir -p {APP_DIR} {APP_DATA_DIR}"), what="mkdir app dirs")
        _run(
            ssh,
            _pct(vmid, f"docker network inspect {TIPI_NETWORK} >/dev/null 2>&1 || "
                       f"docker network create {TIPI_NETWORK}"),
            what="ensure docker network",
        )
        normalized, pub_port = _normalize_compose(compose_yaml, port, source=source,
                                                  port_explicit=port_explicit)
        if pub_port:
            port = pub_port
        _push_file(ssh, vmid, normalized, f"{APP_DIR}/docker-compose.yml")
        env = _platform_env(dict(env_vars), ip, source)
        _push_file(ssh, vmid, _env_file_content(env), f"{APP_DIR}/.env")
        _push_data_files(ssh, vmid, data_files or {}, env)
        _prepare_bind_mounts(ssh, vmid, normalized, APP_DIR, env)

        on_step("docker compose pull && up -d...", 65)
        _run(
            ssh,
            _pct(vmid, f"cd {APP_DIR} && docker compose --env-file .env pull 2>&1 && "
                       f"docker compose --env-file .env up -d 2>&1"),
            what="docker compose pull/up",
            timeout=_DOCKER_TIMEOUT,  # pull больших образов — может идти минуты
        )

        on_step(f"Health-check http://{ip}:{port} ...", 88)
        return ("running" if _health_check(ip, port, health_timeout) else "unhealthy"), port
    finally:
        ssh.close()


def poc_teardown(db: Session, server_id: int, vmid: int, node: Optional[str] = None,
                 on_step: StepCb = _noop) -> None:
    """Остановить и удалить контейнер PoC (для повторяемого прогона)."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise PocError(f"Proxmox сервер id={server_id} не найден")
    client = _get_proxmox_client(server)
    node = node or _resolve_node(client, None)

    on_step(f"Остановка LXC {vmid}...", 30)
    try:
        upid = client.stop_container(node, vmid, force=True)
        if upid:
            client.wait_for_task(node, upid, timeout=120)
    except Exception as e:
        logger.warning(f"stop_container: {e}")

    on_step(f"Удаление LXC {vmid}...", 70)
    try:
        upid = client.delete_container(node, vmid, force=True)
    except Exception as e:
        # CT уже отсутствует (удалён вручную в Proxmox / статус orphaned) —
        # считаем teardown успешным, иначе запись в БД нельзя удалить никогда:
        # каждый повтор удаления падал бы на этом же месте.
        msg = str(e).lower()
        if "does not exist" in msg or "no such" in msg:
            on_step(f"LXC {vmid} уже отсутствует в Proxmox — пропускаем", 100)
            return
        raise
    if upid:
        # Ждём реального завершения задачи, а не просто отдаём таймаут наверх:
        # иначе caller решит, что ресурсы освобождены, пока нода ещё занята
        # (напр. wipe диска) — и следующая установка с тем же VMID словит
        # "can't lock file ... got timeout" (см. инцидент с VMID 102).
        done = client.wait_for_task(node, upid, timeout=_TEARDOWN_WAIT_TIMEOUT)
        if not done:
            raise PocError(
                f"Удаление LXC {vmid}: Proxmox не подтвердил завершение задачи "
                f"за {_TEARDOWN_WAIT_TIMEOUT}с (нода может ещё выполнять wipe "
                f"диска) — ресурсы не считаются освобождёнными, повторите позже"
            )
    on_step("Удалено", 100)
