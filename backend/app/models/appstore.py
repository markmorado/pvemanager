from .base import *

import json as _json

# Статусы установленного приложения (ТЗ 8)
INSTALLED_STATUSES = (
    "installing", "running", "stopped", "error",
    "updating", "update_failed", "orphaned", "deleting",
)


class CatalogApp(Base):
    """
    Приложение из каталога App Store (импорт из runtipi/runtipi-appstore).

    Наполняется Catalog Service (services/appstore_catalog.py). Публичные
    метаданные — шифрование не требуется. Секреты появляются только на этапе
    установки (installed_apps.env_encrypted, M2).
    """
    __tablename__ = "catalog_apps"

    # id приложения из runtipi (config.json → "id"), напр. "uptime-kuma".
    # Для источников кроме runtipi app_id префиксуется ("umbrel-<id>"), чтобы
    # не было коллизий PK между каталогами разных источников.
    app_id = Column(String(100), primary_key=True, index=True)
    name = Column(String(200), nullable=False)

    # источник каталога: "runtipi" | "umbrel". Определяет адаптацию compose
    # при установке (публикация порта, вырезание рантайм-сервисов источника).
    source = Column(String(50), nullable=False, default="runtipi", index=True)

    # version — строка тега docker-образа ("2", "1.23.1"); tipi_version — целочисленный
    # инкремент версии рецепта (используется для update_available в M2).
    version = Column(String(50), nullable=True)
    tipi_version = Column(Integer, nullable=True)

    categories = Column(JSON, nullable=False, default=list)
    short_desc = Column(Text, nullable=True)
    description_md = Column(Text, nullable=True)

    # относительный путь логотипа внутри APPSTORE_DATA_DIR (напр. "logos/uptime-kuma.jpg")
    logo_path = Column(String(300), nullable=True)
    port = Column(Integer, nullable=True)

    # массив объектов form_fields из config.json (см. ТЗ 6.2)
    form_fields = Column(JSON, nullable=False, default=list)
    compose_yaml = Column(Text, nullable=True)
    # Вспомогательные файлы приложения из репозитория каталога, которые compose
    # bind-mount'ит из ${APP_DATA_DIR} (runtipi: apps/<id>/data/**, umbrel: файлы
    # папки приложения). Без них ~60% приложений получают пустые конфиги/скрипты
    # и падают на старте. Формат: {relpath: {"b64": <содержимое>, "exec": bool}}.
    data_files = Column(JSON, nullable=True)
    # Учётные данные по умолчанию из манифеста источника (umbrel-app.yml:
    # defaultUsername/defaultPassword/deterministicPassword). deterministic=true →
    # реальный пароль — это сгенерированный при установке APP_PASSWORD.
    # Формат: {"username": str|None, "password": str|None, "deterministic": bool}.
    default_credentials = Column(JSON, nullable=True)
    architectures = Column(JSON, nullable=False, default=list)

    available = Column(Boolean, nullable=False, default=True, index=True)
    unavailable_reason = Column(String(200), nullable=True)
    deprecated = Column(Boolean, nullable=False, default=False)
    # config.json → dynamic_config: приложение имеет docker-compose.json (новый формат).
    # В MVP всё равно используем docker-compose.yml; флаг сохраняем на будущее (риск п.13 ТЗ).
    dynamic_config = Column(Boolean, nullable=False, default=False)

    source_url = Column(String(500), nullable=True)
    author = Column(String(200), nullable=True)

    synced_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self, *, light: bool = False, with_compose: bool = False) -> dict:
        data = {
            "app_id": self.app_id,
            "name": self.name,
            "source": self.source or "runtipi",
            "version": self.version,
            "tipi_version": self.tipi_version,
            "categories": self.categories or [],
            "short_desc": self.short_desc,
            "port": self.port,
            "architectures": self.architectures or [],
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "deprecated": self.deprecated,
            "dynamic_config": self.dynamic_config,
            "source_url": self.source_url,
            "author": self.author,
            "logo_url": f"/api/appstore/catalog/{self.app_id}/logo" if self.logo_path else None,
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
        }
        if not light:
            # тяжёлые поля — только для страницы приложения
            data["form_fields"] = self.form_fields or []
            data["description_md"] = self.description_md
        if with_compose:
            data["compose_yaml"] = self.compose_yaml
        return data

    def __repr__(self):
        return f"<CatalogApp(app_id='{self.app_id}', name='{self.name}', v='{self.version}')>"


class InstalledApp(Base):
    """
    Установленное приложение = 1 LXC-контейнер с Docker Compose внутри (ТЗ 8).

    env_encrypted — JSON-строка переменных .env (включая сгенерированные секреты),
    хранится зашифрованной (Fernet) через EncryptedString.
    """
    __tablename__ = "installed_apps"

    id = Column(Integer, primary_key=True, index=True)
    # ссылка на каталог (мягкая — приложение может исчезнуть из каталога)
    app_id = Column(String(100), index=True, nullable=True)
    server_id = Column(Integer, index=True, nullable=True)
    vmid = Column(Integer, index=True, nullable=True)
    node = Column(String(100), nullable=True)

    name = Column(String(200), nullable=False)  # пользовательское имя / hostname LXC
    version_installed = Column(String(50), nullable=True)
    tipi_version_installed = Column(Integer, nullable=True)

    env_encrypted = Column(EncryptedString, nullable=True)  # JSON строки .env (зашифровано)
    ip = Column(String(64), nullable=True)
    port = Column(Integer, nullable=True)

    status = Column(String(20), nullable=False, default="installing", index=True)
    update_available = Column(Boolean, nullable=False, default=False)
    last_snapshot = Column(String(200), nullable=True)
    # Параметры создания CT ({cores, memory, disk, storage, bridge, ip_config}) —
    # чтобы повтор установки пересоздал контейнер с исходными настройками,
    # а не с дефолтами (NULL у записей, созданных до появления колонки).
    install_params = Column(JSON, nullable=True)
    # версия/tipi_version, зафиксированные в last_snapshot — для отката версии в БД (M4)
    snapshot_version = Column(String(50), nullable=True)
    snapshot_tipi_version = Column(Integer, nullable=True)

    owner_id = Column(Integer, index=True, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def env_dict(self) -> dict:
        if not self.env_encrypted:
            return {}
        try:
            return _json.loads(self.env_encrypted)
        except Exception:
            return {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "app_id": self.app_id,
            "server_id": self.server_id,
            "vmid": self.vmid,
            "node": self.node,
            "name": self.name,
            "version_installed": self.version_installed,
            "tipi_version_installed": self.tipi_version_installed,
            "ip": self.ip,
            "port": self.port,
            "url": f"http://{self.ip}:{self.port}" if self.ip and self.port else None,
            "status": self.status,
            "update_available": self.update_available,
            "last_snapshot": self.last_snapshot,
            "owner_id": self.owner_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<InstalledApp(id={self.id}, name='{self.name}', vmid={self.vmid}, status='{self.status}')>"


# Человекочитаемые названия операций для общего списка «Задачи».
APP_OPERATION_LABELS = {
    "install": "Установка приложения",
    "update": "Обновление приложения",
    "rollback": "Откат приложения",
    "delete": "Удаление приложения",
    "start": "Запуск приложения",
    "stop": "Остановка приложения",
    "restart": "Перезапуск приложения",
    "golden_template": "Подготовка золотого шаблона",
}


class AppOperation(Base):
    """Журнал операции над приложением (install/update/rollback/delete/...) — ТЗ 8."""
    __tablename__ = "app_operations"

    id = Column(Integer, primary_key=True, index=True)
    installed_app_id = Column(Integer, ForeignKey("installed_apps.id", ondelete="CASCADE"), index=True, nullable=True)
    # Только для операций без installed_app_id (напр. golden_template) — чтобы
    # различать прогресс/историю сборки шаблона по серверам Proxmox.
    server_id = Column(Integer, ForeignKey("proxmox_servers.id", ondelete="CASCADE"), index=True, nullable=True)
    type = Column(String(20), nullable=False)          # install/update/rollback/delete/start/stop/restart
    status = Column(String(20), nullable=False, default="running")  # running/completed/failed
    progress = Column(Integer, nullable=False, default=0)
    steps_log = Column(JSON, nullable=False, default=list)  # [{step, progress, ts}]
    error_text = Column(Text, nullable=True)
    # Indexed: /api/all-tasks filters by user every few seconds (migration 45).
    user_id = Column(Integer, nullable=True, index=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": "appstore",
            "installed_app_id": self.installed_app_id,
            "server_id": self.server_id,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "steps_log": self.steps_log or [],
            "error_text": self.error_text,
            "user_id": self.user_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def to_task_dict(self, *, app_name: Optional[str] = None,
                     node: Optional[str] = None,
                     server_id: Optional[int] = None) -> dict:
        """Представление операции для общего списка «Задачи» (/api/all-tasks).

        Поля приведены к общему виду с TaskQueue/ProxmoxTask/DeployTask:
        description, step/current_step, error_message, created_at, progress.
        """
        label = APP_OPERATION_LABELS.get(self.type, self.type)
        steps = self.steps_log or []
        last_step = steps[-1].get("step") if steps else None
        return {
            "id": self.id,
            "kind": "appstore",
            "type": self.type,
            "description": f"{label}: {app_name}" if app_name else label,
            "status": self.status,
            "progress": self.progress,
            "step": last_step,
            "current_step": last_step,
            "error_message": self.error_text,
            "installed_app_id": self.installed_app_id,
            "user_id": self.user_id,
            "node": node,
            "server_id": server_id,
            "created_at": self.started_at.isoformat() if self.started_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def __repr__(self):
        return f"<AppOperation(id={self.id}, type='{self.type}', status='{self.status}')>"
