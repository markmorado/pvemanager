"""
App Engine (M2) — жизненный цикл приложения поверх пайплайна M0.

Установка: клон золотого шаблона → pct push compose/.env → docker compose up →
health-check, с журналом шагов (app_operations), статусами (installed_apps),
шифрованием env-секретов (Fernet) и прогрессом по WebSocket.

Переиспуёт app/appstore/pipeline.py (poc_install / poc_teardown).
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from apscheduler.triggers.interval import IntervalTrigger

from ..appstore.pipeline import (
    APP_DIR, APP_DATA_DIR, InstallSpec, PocError, _NAME_RE, _node_ssh, _shq,
    poc_install, poc_teardown, poc_update,
)
from ..appstore.golden_template import GoldenSpec, build_golden_template
from ..api.proxmox._helpers import _get_proxmox_client, get_next_vmid, soft_delete_vm_instance
from ..api.settings import get_setting, set_setting
from ..config import settings
from ..db import SessionLocal
from ..ipam_service import IPAMService
from ..models import AppOperation, CatalogApp, InstalledApp, IPAMNetwork, ProxmoxServer
from ..websocket_manager import broadcast_task_update

_engine_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="appstore_engine")


class EngineError(RuntimeError):
    """Ошибка бизнес-валидации движка (человекочитаемая)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Журнал операции + WS ──────────────────────────────────────────────────────

def _record_step(operation_id: int, user_id: int, step: str, progress: int) -> None:
    """Добавить шаг в журнал операции и разослать по WebSocket (своя сессия)."""
    db = SessionLocal()
    try:
        op = db.get(AppOperation, operation_id)
        if not op:
            return
        log = list(op.steps_log or [])
        log.append({"step": step, "progress": progress, "ts": _utcnow().isoformat()})
        op.steps_log = log
        op.progress = progress
        db.commit()
        try:
            broadcast_task_update(user_id, "appstore_operation_update", op.to_dict())
        except Exception:
            pass
    finally:
        db.close()


def _finish_op(operation_id: int, user_id: int, status: str, error: Optional[str] = None) -> None:
    db = SessionLocal()
    try:
        op = db.get(AppOperation, operation_id)
        if not op:
            return
        op.status = status
        op.finished_at = _utcnow()
        if error:
            op.error_text = error[:2000]
        if status == "completed":
            op.progress = 100
        db.commit()
        try:
            broadcast_task_update(user_id, "appstore_operation_update", op.to_dict())
        except Exception:
            pass
    finally:
        db.close()


# ── Генерация env (ТЗ 5.3 + 6.2) ──────────────────────────────────────────────

def _generate_random(length: int) -> str:
    length = max(8, int(length or 32))
    # hex → безопасно для .env (нет спецсимволов), длина = length
    return _secrets.token_hex(length // 2 + 1)[:length]


def build_env(app: CatalogApp, form_answers: dict) -> tuple[dict, dict]:
    """Собрать .env: платформенные переменные + ответы формы + random-секреты.

    Возвращает (env, secrets_shown) — secrets_shown показываются пользователю один раз.
    """
    form_answers = form_answers or {}
    env = {
        "APP_PORT": str(app.port or 80),
        "APP_DATA_DIR": APP_DATA_DIR,
        "APP_PROTOCOL": "http",
        "APP_EXPOSED": "false",
        "APP_ID": app.app_id,
        "TZ": settings.TZ,
        "ROOT_FOLDER_HOST": APP_DATA_DIR,
    }
    secrets_shown: dict = {}

    # Umbrel: рантайм генерирует APP_PASSWORD (app password) сам, а compose
    # ссылается на неё для паролей/ключей (ENCRYPTION_KEY, JWT_SECRET и т.п.).
    # Без неё docker compose подставит пустую строку и приложение упадёт или
    # останется без пароля. Генерируем здесь, чтобы значение сохранилось в
    # env_encrypted и показалось пользователю один раз.
    if (app.source or "runtipi") == "umbrel":
        env["APP_PASSWORD"] = _generate_random(32)
        secrets_shown["APP_PASSWORD"] = env["APP_PASSWORD"]

    for field in (app.form_fields or []):
        ev = field.get("env_variable")
        if not ev:
            continue
        ftype = (field.get("type") or "text").lower()

        if ftype == "random":
            val = _generate_random(field.get("min") or 32)
            env[ev] = val
            secrets_shown[ev] = val
            continue

        val = form_answers.get(ev)
        if val is None or val == "":
            if field.get("required"):
                raise EngineError(f"Поле '{field.get('label') or ev}' обязательно")
            val = field.get("default", "")

        if ftype == "boolean":
            val = "true" if str(val).lower() in ("1", "true", "yes", "on") else "false"
        val = str(val)
        env[ev] = val
        if ftype == "password":
            secrets_shown[ev] = val

    return env, secrets_shown


# ── Золотой шаблон (Workstream B) ─────────────────────────────────────────────

# Ключ PanelSettings, куда сохраняется volid собранного золотого шаблона.
_GOLDEN_SETTING_KEY = "appstore_golden_template"


def _golden_key(server_id: int) -> str:
    return f"{_GOLDEN_SETTING_KEY}:{server_id}"


def _resolve_golden_template(db, server_id: int, ostemplate: Optional[str] = None) -> Optional[str]:
    """Определить golden vztmpl для конкретного сервера.

    Приоритет: явный override → сохранённый для ЭТОГО server_id → старый общий
    ключ (обратная совместимость с настройкой до per-server хранения) →
    env-дефолт. vztmpl — файл на storage конкретного Proxmox-сервера, поэтому
    шаблон, собранный на сервере A, не существует на сервере B — нельзя просто
    отдавать один и тот же сохранённый volid для всех серверов.
    """
    return (
        ostemplate
        or get_setting(db, _golden_key(server_id))
        or get_setting(db, _GOLDEN_SETTING_KEY)
        or settings.APPSTORE_GOLDEN_TEMPLATE
    )


def prepare_golden_template(db, user, *, server_id: int, node: Optional[str] = None,
                            template_storage: str = "local",
                            rootfs_storage: str = "local-lvm",
                            bridge: str = "vmbr0", force: bool = False) -> dict:
    """Запустить сборку золотого шаблона в фоне. Возвращает operation_id."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise EngineError("Proxmox сервер не найден")

    op = _new_operation(db, None, "golden_template", user.id, server_id=server_id)
    _engine_executor.submit(
        _run_golden_template, op.id, user.id, server_id, node,
        template_storage, rootfs_storage, bridge, force,
    )
    return {"operation_id": op.id}


def _run_golden_template(operation_id, user_id, server_id, node,
                         template_storage, rootfs_storage, bridge, force):
    db = SessionLocal()
    try:
        def on_step(step: str, progress: int) -> None:
            _record_step(operation_id, user_id, step, progress)

        spec = GoldenSpec(
            server_id=server_id, node=node, template_storage=template_storage,
            rootfs_storage=rootfs_storage, bridge=bridge, force=force,
        )
        result = build_golden_template(db, spec, on_step)

        # Сохраняем volid ПОД ЭТИМ СЕРВЕРОМ — install() на других серверах не
        # должен получить чужой (недоступный на их storage) шаблон.
        try:
            set_setting(db, _golden_key(server_id), result.volid,
                        description=f"App Store: золотой LXC-шаблон сервера #{server_id}")
        except Exception as e:
            logger.warning(f"[appstore-engine] save golden template setting: {e}")

        _finish_op(operation_id, user_id, "completed")
        try:
            broadcast_task_update(user_id, "appstore_golden_template_done",
                                  {"volid": result.volid, "node": result.node,
                                   "status": result.status, "server_id": server_id})
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[appstore-engine] golden template failed: {e}")
        _finish_op(operation_id, user_id, "failed", error=str(e))
    finally:
        db.close()


def golden_template_status(db, server_id: int) -> dict:
    """Текущий шаблон конкретного сервера (БД/env) + последняя операция сборки на нём."""
    op = (
        db.query(AppOperation)
        .filter(AppOperation.type == "golden_template", AppOperation.server_id == server_id)
        .order_by(AppOperation.started_at.desc())
        .first()
    )
    return {
        "configured_template": _resolve_golden_template(db, server_id),
        "env_template": settings.APPSTORE_GOLDEN_TEMPLATE,
        "saved_template": get_setting(db, _golden_key(server_id)),
        "last_operation": op.to_dict() if op else None,
    }


# ── Установка ─────────────────────────────────────────────────────────────────

def _allocate_ipam(db, *, network_id: int, pool_id: Optional[int], name: str,
                   server_id: int, vmid: int, node: Optional[str],
                   allocated_by: Optional[str]) -> tuple[str, str]:
    """Выделить IP из IPAM для приложения. Возвращает (ip_config, static_ip).

    ip_config — строка для net0 ("ip=<addr>/<mask>,gw=<gw>"). Бросает EngineError
    при отсутствии свободного адреса, чтобы ошибка сразу вернулась пользователю.
    """
    ipam = IPAMService(db)
    allocation, error = ipam.auto_allocate_ip(
        network_id=network_id, pool_id=pool_id,
        resource_type="ct", resource_name=name,
        proxmox_server_id=server_id, proxmox_vmid=vmid, proxmox_node=node,
        allocated_by=allocated_by, notes=f"App Store: {name}",
    )
    if error or not allocation:
        raise EngineError(f"Не удалось выделить IP из IPAM: {error or 'нет свободных адресов'}")

    ip = allocation.ip_address
    net = db.query(IPAMNetwork).filter(IPAMNetwork.id == network_id).first()
    mask = "24"
    gateway = None
    if net:
        if net.network and "/" in net.network:
            mask = net.network.split("/")[1]
        gateway = net.gateway or None
    ip_config = f"ip={ip}/{mask},gw={gateway}" if gateway else f"ip={ip}/{mask}"
    return ip_config, ip


def install(db, user, *, app_id: str, name: str, form_answers: dict,
            server_id: int, node: Optional[str] = None, cores: int = 2,
            memory: int = 2048, disk: int = 8, storage: str = "local-lvm",
            bridge: str = "vmbr0", ostemplate: Optional[str] = None,
            port: Optional[int] = None,
            ipam_network_id: Optional[int] = None,
            ipam_pool_id: Optional[int] = None) -> dict:
    """Синхронная часть: валидация + запись в БД. Пайплайн — в пуле потоков."""
    app = db.query(CatalogApp).filter(CatalogApp.app_id == app_id).first()
    if not app:
        raise EngineError("Приложение не найдено в каталоге")
    if not app.available:
        raise EngineError("Приложение помечено недоступным")
    if not app.compose_yaml:
        raise EngineError("У приложения отсутствует docker-compose.yml")
    if not _NAME_RE.match(name or ""):
        raise EngineError("Недопустимое имя приложения (a-z, 0-9, дефис, 1..63)")

    golden = _resolve_golden_template(db, server_id, ostemplate)
    if not golden:
        raise EngineError("Не настроен золотой шаблон LXC — подготовьте его "
                          "(App Store → золотой шаблон) или задайте APPSTORE_GOLDEN_TEMPLATE")

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise EngineError("Proxmox сервер не найден")

    # Каждое приложение — отдельный LXC владельца: квоты действуют так же, как
    # при обычном деплое (иначе App Store был обходом лимитов).
    from .quota_service import check_quota
    check_quota(db, user.id, add_cores=cores, add_memory_mb=memory, add_disk_gb=disk)

    # Кастомный порт из формы имеет приоритет над портом каталога (app.port).
    if port is not None and not (1 <= port <= 65535):
        raise EngineError("Порт должен быть в диапазоне 1..65535")
    effective_port = port if port else app.port

    env, secrets_shown = build_env(app, form_answers)
    # Учётные данные по умолчанию из манифеста (Umbrel) — показываем сразу
    # после установки: у многих приложений это единственный способ узнать
    # логин/пароль админа. deterministic → пароль = APP_PASSWORD (см. build_env).
    creds = app.default_credentials or {}
    if creds.get("username"):
        secrets_shown["USERNAME"] = creds["username"]
    default_pwd = env.get("APP_PASSWORD") if creds.get("deterministic") else creds.get("password")
    if default_pwd:
        secrets_shown["PASSWORD"] = default_pwd
    # APP_PORT в окружении должен соответствовать выбранному порту.
    if port:
        env["APP_PORT"] = str(port)
    vmid = get_next_vmid(db, server_id)
    # Proxmox nextid не резервирует номер: пока предыдущая установка ещё не
    # создала CT, nextid возвращает тот же VMID. Вторая установка тогда падает
    # на create, а её retry (идемпотентность по vmid) «подхватывает» ЧУЖОЙ
    # контейнер и заливает в него свой compose. Сдвигаемся мимо VMID, уже
    # занятых записями installed_apps этого сервера.
    taken = {
        v for (v,) in db.query(InstalledApp.vmid)
        .filter(InstalledApp.server_id == server_id, InstalledApp.vmid.isnot(None))
        .all()
    }
    while vmid in taken:
        vmid += 1

    # IPAM (опционально): выделяем адрес до старта пайплайна, чтобы ошибка
    # (нет свободных IP) вернулась пользователю синхронно.
    ip_config = None
    static_ip = None
    if ipam_network_id:
        ip_config, static_ip = _allocate_ipam(
            db, network_id=ipam_network_id, pool_id=ipam_pool_id, name=name,
            server_id=server_id, vmid=vmid, node=node,
            allocated_by=getattr(user, "username", None),
        )

    ia = InstalledApp(
        app_id=app_id, server_id=server_id, vmid=vmid, node=node, name=name,
        version_installed=str(app.version) if app.version is not None else None,
        tipi_version_installed=app.tipi_version,
        env_encrypted=json.dumps(env), ip=static_ip, port=effective_port, status="installing",
        owner_id=user.id,
        # Параметры создания CT — для повтора установки: если контейнер так и
        # не был создан, retry должен пересоздать его с ТЕМИ ЖЕ ресурсами,
        # storage/bridge и статическим IP из IPAM, а не с дефолтами.
        install_params={
            "cores": cores, "memory": memory, "disk": disk,
            "storage": storage, "bridge": bridge, "ip_config": ip_config,
            "port_explicit": bool(port),
        },
    )
    db.add(ia)
    db.commit()
    db.refresh(ia)

    op = _new_operation(db, ia.id, "install", user.id)

    _engine_executor.submit(
        _run_install, ia.id, op.id, user.id, app.compose_yaml, env, golden,
        cores, memory, disk, server_id, node, name, effective_port, ia.version_installed, vmid,
        storage, bridge, ip_config, static_ip, app.source or "runtipi",
        app.data_files or {}, bool(port),
    )
    return {"installed_app_id": ia.id, "operation_id": op.id, "vmid": vmid, "secrets": secrets_shown}


def _new_operation(db, installed_app_id: int, op_type: str, user_id: int,
                   server_id: Optional[int] = None) -> AppOperation:
    op = AppOperation(
        installed_app_id=installed_app_id, type=op_type, server_id=server_id,
        status="running", progress=0, steps_log=[], user_id=user_id,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def _run_install(installed_app_id, operation_id, user_id, compose_yaml, env, ostemplate,
                 cores, memory, disk, server_id, node, name, port, version, vmid,
                 storage, bridge, ip_config=None, static_ip=None, source="runtipi",
                 data_files=None, port_explicit=False):
    """Тело установки в пуле потоков."""
    db = SessionLocal()
    try:
        spec = InstallSpec(
            server_id=server_id, ostemplate=ostemplate, name=name,
            compose_yaml=compose_yaml, port=port or 80, port_explicit=port_explicit,
            env_vars=env,
            node=node, vmid=vmid, cores=cores, memory=memory, disk=disk,
            storage=storage, bridge=bridge, ip_config=ip_config, static_ip=static_ip,
            source=source or "runtipi", data_files=data_files or {},
        )

        def on_step(step: str, progress: int) -> None:
            _record_step(operation_id, user_id, step, progress)

        result = poc_install(db, spec, on_step)

        ia = db.get(InstalledApp, installed_app_id)
        if ia:
            ia.vmid = result.vmid
            ia.node = result.node
            ia.ip = result.ip
            ia.port = spec.port
            ia.version_installed = version
            ia.status = "running" if result.status == "running" else "error"
            db.commit()

        if result.status == "running":
            _finish_op(operation_id, user_id, "completed")
        else:
            _finish_op(operation_id, user_id, "failed",
                       error="Health-check не прошёл (приложение не ответило по HTTP)")
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            ia = db.get(InstalledApp, installed_app_id)
            if ia:
                ia.status = "error"
                ia.vmid = vmid  # сохранить vmid для идемпотентного повтора
                db.commit()
        except Exception:
            pass
        logger.error(f"[appstore-engine] install #{installed_app_id} failed: {e}")
        _finish_op(operation_id, user_id, "failed", error=str(e))
    finally:
        db.close()


def retry_install(db, user, installed_app_id: int) -> dict:
    """Повтор установки (ТЗ 6.1): контейнер не пересоздаётся (проверка по vmid)."""
    ia = db.get(InstalledApp, installed_app_id)
    if not ia:
        raise EngineError("Установка не найдена")
    if ia.status not in ("error", "installing"):
        raise EngineError(f"Повтор недоступен из статуса '{ia.status}'")

    app = db.query(CatalogApp).filter(CatalogApp.app_id == ia.app_id).first()
    compose = app.compose_yaml if app else None
    if not compose:
        raise EngineError("compose приложения недоступен для повтора")
    golden = _resolve_golden_template(db, ia.server_id)
    if not golden:
        raise EngineError("Не настроен золотой шаблон LXC — подготовьте его "
                          "(App Store → золотой шаблон) или задайте APPSTORE_GOLDEN_TEMPLATE")

    env = ia.env_dict()
    ia.status = "installing"
    db.commit()

    op = _new_operation(db, ia.id, "install", user.id)
    # Повтор идемпотентен по vmid: существующий контейнер не пересоздаётся
    # (тогда params/ip_config игнорируются pайплайном). Если же CT так и не был
    # создан — пересоздаём с исходными параметрами установки (install_params),
    # включая статический IP из IPAM; для старых записей без install_params
    # остаются прежние дефолты. Известный IP (ia.ip) передаём как static_ip,
    # чтобы не ждать выдачи DHCP впустую.
    params = ia.install_params or {}
    _engine_executor.submit(
        _run_install, ia.id, op.id, user.id, compose, env, golden,
        params.get("cores", 2), params.get("memory", 2048), params.get("disk", 8),
        ia.server_id, ia.node, ia.name, ia.port, ia.version_installed, ia.vmid,
        params.get("storage", "local-lvm"), params.get("bridge", "vmbr0"),
        params.get("ip_config"), ia.ip, (app.source or "runtipi"),
        (app.data_files or {}), bool(params.get("port_explicit")),
    )
    return {"installed_app_id": ia.id, "operation_id": op.id}


# ── Удаление (нужно для обработки сбоя M2; полный lifecycle — M3) ──────────────

def delete_app(db, user, installed_app_id: int) -> dict:
    ia = db.get(InstalledApp, installed_app_id)
    if not ia:
        raise EngineError("Установка не найдена")

    op = _new_operation(db, ia.id, "delete", user.id)
    ia.status = "deleting"
    db.commit()

    _engine_executor.submit(_run_delete, ia.id, op.id, user.id, ia.server_id, ia.vmid, ia.node)
    return {"operation_id": op.id}


def _run_delete(installed_app_id, operation_id, user_id, server_id, vmid, node):
    db = SessionLocal()
    try:
        def on_step(step: str, progress: int) -> None:
            _record_step(operation_id, user_id, step, progress)

        if vmid and server_id:
            # Не глотаем ошибку teardown: если Proxmox не подтвердил реальное
            # завершение удаления (см. poc_teardown/_TEARDOWN_WAIT_TIMEOUT),
            # ресурсы ноды могут быть ещё заняты — пробрасываем наверх, чтобы
            # НЕ помечать установку удалённой раньше времени (иначе следующая
            # установка того же приложения словит гонку блокировки VMID).
            poc_teardown(db, server_id, vmid, node, on_step)
            # Освобождаем IP в IPAM (no-op, если приложение ставилось по DHCP).
            try:
                IPAMService(db).release_ip_by_vmid(
                    server_id, vmid, released_by="appstore",
                    reason=f"App Store: удаление приложения #{installed_app_id}")
            except Exception as e:
                logger.warning(f"[appstore-engine] IPAM release vmid={vmid}: {e}")
            # Помечаем кэш-запись инстанса удалённой сразу, чтобы контейнер
            # исчез из списка Инстансов немедленно, не дожидаясь фонового воркера.
            try:
                soft_delete_vm_instance(db, server_id, vmid)
            except Exception as e:
                logger.warning(f"[appstore-engine] soft-delete vm_instance vmid={vmid}: {e}")

        # финальное событие ДО удаления записи (каскад удалит app_operations)
        _finish_op(operation_id, user_id, "completed")

        ia = db.get(InstalledApp, installed_app_id)
        if ia:
            db.delete(ia)
            db.commit()
        try:
            broadcast_task_update(user_id, "appstore_app_deleted", {"installed_app_id": installed_app_id})
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[appstore-engine] delete #{installed_app_id} failed: {e}")
        _finish_op(operation_id, user_id, "failed", error=str(e))
        # Оставляем InstalledApp на месте (ресурсы ноды не подтверждены
        # освобождёнными) — статус "error" позволяет повторить удаление позже.
        ia = db.get(InstalledApp, installed_app_id)
        if ia:
            ia.status = "error"
            db.commit()
    finally:
        db.close()


# ── Обновление и откат (M4) ───────────────────────────────────────────────────

_SNAP_PREFIX = "preupd"


def _snap_name(version: Optional[str], ts: datetime) -> str:
    """Имя снапшота pre-update: буквы/цифры/подчёркивание, ≤ 40, начинается с буквы."""
    ver = re.sub(r"[^A-Za-z0-9]", "", str(version or "0"))[:12] or "0"
    stamp = ts.strftime("%Y%m%d%H%M%S")
    return f"{_SNAP_PREFIX}_{ver}_{stamp}"[:40]


def _prune_snapshots(client, node: str, vmid: int, keep: str) -> None:
    """Оставить только последний pre-update снапшот (ТЗ 6.3), старые удалить."""
    try:
        snaps = client.get_container_snapshots(node, vmid)
    except Exception:
        return
    for s in snaps or []:
        name = s.get("name")
        if not name or name == "current" or name == keep:
            continue
        if name.startswith(_SNAP_PREFIX + "_"):
            try:
                res = client.delete_container_snapshot(node, vmid, name)
                upid = res.get("upid") if isinstance(res, dict) else None
                if upid:
                    client.wait_for_task(node, upid, timeout=120)
            except Exception as e:
                logger.warning(f"[appstore-engine] prune snapshot {name}: {e}")


def update(db, user, installed_app_id: int, form_answers: Optional[dict] = None) -> dict:
    """Обновление приложения: снапшот → pull → up → health (ТЗ 6.3)."""
    ia = db.get(InstalledApp, installed_app_id)
    if not ia:
        raise EngineError("Установка не найдена")
    if ia.status not in ("running", "stopped", "update_failed"):
        raise EngineError(f"Обновление недоступно из статуса '{ia.status}'")
    if not ia.vmid or not ia.server_id or not ia.node:
        raise EngineError("У приложения нет привязанного контейнера")

    app = db.query(CatalogApp).filter(CatalogApp.app_id == ia.app_id).first()
    if not app or not app.compose_yaml:
        raise EngineError("Свежая версия приложения недоступна в каталоге")

    op = _new_operation(db, ia.id, "update", user.id)
    _engine_executor.submit(
        _run_update, ia.id, op.id, user.id, app.compose_yaml,
        str(app.version) if app.version is not None else None, app.tipi_version,
        form_answers or {}, (app.source or "runtipi"), (app.data_files or {}),
    )
    return {"installed_app_id": ia.id, "operation_id": op.id}


def _run_update(installed_app_id, operation_id, user_id, compose_yaml,
                new_version, new_tipi, form_answers, source="runtipi",
                data_files=None):
    db = SessionLocal()
    try:
        ia = db.get(InstalledApp, installed_app_id)
        if not ia:
            _finish_op(operation_id, user_id, "failed", error="Установка исчезла")
            return
        server, client = _client_for(db, ia.server_id)

        def on_step(step: str, progress: int) -> None:
            _record_step(operation_id, user_id, step, progress)

        # 1. Снапшот pre-update-<version>-<timestamp>
        on_step("Создание снапшота перед обновлением...", 15)
        snapname = _snap_name(ia.version_installed, _utcnow())
        snap = client.create_container_snapshot(
            ia.node, ia.vmid, snapname, description="pvemanager-appstore pre-update")
        if not (isinstance(snap, dict) and snap.get("success")):
            err = snap.get("error") if isinstance(snap, dict) else "snapshot failed"
            ia.status = "update_failed"
            db.commit()
            _finish_op(operation_id, user_id, "failed", error=f"Снапшот не создан: {err}")
            return

        ia.last_snapshot = snapname
        ia.snapshot_version = ia.version_installed
        ia.snapshot_tipi_version = ia.tipi_version_installed
        ia.status = "updating"
        db.commit()

        # 2. env: сохраняем прежний, доливаем новые ответы формы (ТЗ 6.3)
        env = ia.env_dict()
        for k, v in (form_answers or {}).items():
            env[k] = str(v)

        # 3-4. push нового compose → pull && up → health
        status, eff_port = poc_update(
            server, client, ia.vmid, ia.node, compose_yaml,
            env, ia.port or 80, ia.ip, on_step,
            source=source or "runtipi", data_files=data_files or {},
            port_explicit=bool((ia.install_params or {}).get("port_explicit")),
        )

        if status == "running":
            ia.status = "running"
            ia.version_installed = new_version
            ia.tipi_version_installed = new_tipi
            ia.update_available = False
            ia.env_encrypted = json.dumps(env)
            # Нормализация compose могла опубликовать другой порт — фиксируем
            # его в БД, иначе url/health-check будут указывать не туда.
            if eff_port:
                ia.port = eff_port
            db.commit()
            _prune_snapshots(client, ia.node, ia.vmid, keep=snapname)  # хранить последний 1
            _finish_op(operation_id, user_id, "completed")
        else:
            ia.status = "update_failed"
            db.commit()
            _finish_op(operation_id, user_id, "failed",
                       error="Health-check после обновления не прошёл — доступен откат")
    except Exception as e:
        try:
            db.rollback()
            ia = db.get(InstalledApp, installed_app_id)
            if ia:
                ia.status = "update_failed"
                db.commit()
        except Exception:
            pass
        logger.error(f"[appstore-engine] update #{installed_app_id} failed: {e}")
        _finish_op(operation_id, user_id, "failed", error=str(e))
    finally:
        db.close()


def rollback(db, user, installed_app_id: int) -> dict:
    """Откат обновления восстановлением снапшота (ТЗ 6.4)."""
    ia = db.get(InstalledApp, installed_app_id)
    if not ia:
        raise EngineError("Установка не найдена")
    if not ia.last_snapshot:
        raise EngineError("Нет pre-update снапшота для отката")
    if not ia.vmid or not ia.server_id or not ia.node:
        raise EngineError("У приложения нет привязанного контейнера")

    op = _new_operation(db, ia.id, "rollback", user.id)
    _engine_executor.submit(_run_rollback, ia.id, op.id, user.id)
    return {"installed_app_id": ia.id, "operation_id": op.id}


def _run_rollback(installed_app_id, operation_id, user_id):
    db = SessionLocal()
    try:
        ia = db.get(InstalledApp, installed_app_id)
        if not ia:
            _finish_op(operation_id, user_id, "failed", error="Установка исчезла")
            return
        server, client = _client_for(db, ia.server_id)
        snapname = ia.last_snapshot

        def on_step(step: str, progress: int) -> None:
            _record_step(operation_id, user_id, step, progress)

        on_step("Остановка контейнера...", 20)
        try:
            upid = client.stop_container(ia.node, ia.vmid)
            if upid:
                client.wait_for_task(ia.node, upid, timeout=60)
        except Exception:
            pass

        on_step(f"Восстановление снапшота {snapname}...", 45)
        res = client.rollback_container_snapshot(ia.node, ia.vmid, snapname, start=True)
        upid = res.get("upid") if isinstance(res, dict) else None
        if isinstance(res, dict) and not res.get("success"):
            ia.status = "update_failed"
            db.commit()
            _finish_op(operation_id, user_id, "failed",
                       error=f"Откат не удался: {res.get('error')}")
            return
        if upid:
            client.wait_for_task(ia.node, upid, timeout=300)

        on_step("Запуск контейнера...", 70)
        try:
            client.start_container(ia.node, ia.vmid)
        except Exception:
            pass

        on_step(f"Health-check http://{ia.ip}:{ia.port} ...", 88)
        from ..appstore.pipeline import _health_check
        ok = _health_check(ia.ip, ia.port or 80, 180) if ia.ip else False

        # вернуть прежнюю версию в БД (ТЗ 6.4)
        if ia.snapshot_version is not None:
            ia.version_installed = ia.snapshot_version
            ia.tipi_version_installed = ia.snapshot_tipi_version
        ia.status = "running" if ok else "stopped"
        db.commit()
        _finish_op(operation_id, user_id, "completed" if ok else "failed",
                   error=None if ok else "Контейнер восстановлен, но health-check не прошёл")
    except Exception as e:
        try:
            db.rollback()
            ia = db.get(InstalledApp, installed_app_id)
            if ia:
                ia.status = "update_failed"
                db.commit()
        except Exception:
            pass
        logger.error(f"[appstore-engine] rollback #{installed_app_id} failed: {e}")
        _finish_op(operation_id, user_id, "failed", error=str(e))
    finally:
        db.close()


# ── Управление жизненным циклом (M3): start / stop / restart / logs ───────────

def _client_for(db, server_id: int):
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise EngineError("Proxmox сервер не найден")
    return server, _get_proxmox_client(server)


def lifecycle_action(db, user, installed_app_id: int, action: str) -> dict:
    """Start / stop / restart LXC приложения (ТЗ 6.5)."""
    if action not in ("start", "stop", "restart"):
        raise EngineError(f"Неизвестное действие: {action}")
    ia = db.get(InstalledApp, installed_app_id)
    if not ia:
        raise EngineError("Установка не найдена")
    if not ia.vmid or not ia.server_id or not ia.node:
        raise EngineError("У приложения нет привязанного контейнера")

    _server, client = _client_for(db, ia.server_id)
    if action == "start":
        upid = client.start_container(ia.node, ia.vmid)
        new_status = "running"
    elif action == "stop":
        upid = client.stop_container(ia.node, ia.vmid)
        new_status = "stopped"
    else:  # restart
        upid = client.restart_container(ia.node, ia.vmid)
        new_status = "running"

    if upid:
        try:
            client.wait_for_task(ia.node, upid, timeout=60)
        except Exception:
            pass

    ia.status = new_status
    db.commit()
    db.refresh(ia)
    try:
        broadcast_task_update(user.id, "appstore_operation_update",
                              {"kind": "appstore", "installed_app_id": ia.id,
                               "type": action, "status": "completed", "progress": 100})
    except Exception:
        pass
    return ia.to_dict()


def get_logs(db, installed_app_id: int, tail: int = 200) -> str:
    """docker compose logs --tail N через pct exec (ТЗ 6.5, по запросу)."""
    ia = db.get(InstalledApp, installed_app_id)
    if not ia or not ia.vmid or not ia.server_id:
        raise EngineError("У приложения нет привязанного контейнера")
    server, client = _client_for(db, ia.server_id)
    ssh = _node_ssh(server, client)
    if not ssh.connect():
        raise EngineError(ssh.last_error or "SSH к ноде не установлен")
    try:
        tail = max(1, min(int(tail or 200), 2000))
        inner = f"cd {APP_DIR} && docker compose logs --tail {tail} --no-color 2>&1"
        out, _code = ssh.execute(
            f"pct exec {ia.vmid} -- /bin/bash -lc {_shq(inner)}", return_exit_code=True)
        return out or ""
    finally:
        ssh.close()


# ── Реконсиляция состояния (ТЗ 6.6) ───────────────────────────────────────────

_RECONCILE_STATES = ("running", "stopped", "orphaned")


def reconcile(db) -> dict:
    """Сверить статус LXC в Proxmox с БД. Пропавший контейнер → 'orphaned'."""
    apps = db.query(InstalledApp).filter(InstalledApp.status.in_(_RECONCILE_STATES)).all()
    changed = 0
    for ia in apps:
        if not ia.vmid or not ia.server_id or not ia.node:
            continue
        try:
            _server, client = _client_for(db, ia.server_id)
            if not client.is_connected():
                # Сервер целиком недоступен (сеть/креды) — это ничего не говорит
                # о судьбе контейнера; не помечать все приложения orphaned.
                continue
            st = client.get_container_status(ia.node, ia.vmid)
        except Exception:
            continue

        if not st:
            new_status = "orphaned"
        else:
            s = (st.get("status") or "").lower()
            new_status = "running" if s == "running" else "stopped" if s == "stopped" else ia.status

        if new_status != ia.status:
            ia.status = new_status
            changed += 1
    if changed:
        db.commit()
    return {"checked": len(apps), "changed": changed}


def _scheduled_reconcile() -> None:
    db = SessionLocal()
    try:
        reconcile(db)
    except Exception as e:
        logger.warning(f"[appstore-engine] scheduled reconcile failed: {e}")
    finally:
        db.close()


def start_reconcile_scheduler():
    """Периодическая реконсиляция каждые 60с (ТЗ 6.6). Из lifespan."""
    from .backup_scheduler import get_scheduler

    aps = get_scheduler()
    aps.add_job(
        _scheduled_reconcile,
        trigger=IntervalTrigger(seconds=60),
        id="appstore_reconcile",
        replace_existing=True,
        misfire_grace_time=30,
    )
    if not aps.running:
        aps.start()
    logger.info("App Store reconcile scheduler registered (every 60s)")
    return aps
