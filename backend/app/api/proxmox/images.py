"""
API: каталог образов (зеркала) и загрузка образов ВМ/шаблонов на ноду Proxmox.

Phase 1: просмотр встроенного каталога + кастомных зеркал, динамический список
LXC-шаблонов из репозитория Proxmox, выбор целевого хранилища и фоновая загрузка
qcow2 (content=import) / vztmpl с прогрессом через DeployTask + WebSocket.
"""

import asyncio
import ipaddress
import os
import re
import socket
import shutil
import tempfile
from email.message import Message as _EmailMessage
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session
from loguru import logger

from ...db import get_db
from ...models import ProxmoxServer, User, DeployTask, ImageMirror
from ...auth import PermissionChecker
from ...catalog import CATALOG, get_catalog_image
from ._helpers import _get_proxmox_client, PveNode, require_node_name, require_pve_id

from ._helpers import enforce_server_visibility  # noqa: E402

# Every {server_id} route: tenants only reach servers assigned to them
# (directly or through a workspace) — see enforce_server_visibility.
router = APIRouter(dependencies=[Depends(enforce_server_visibility)])


def _sanitize_filename(name: str, default: str = 'upload.iso') -> str:
    """Свести произвольное (клиентское) имя файла к безопасному basename.

    Отбрасывает путь и заменяет всё, кроме букв/цифр/._- на подчёркивание;
    strip('.') нейтрализует '..', '.' и ведущие точки — иначе basename вида
    '..' привёл бы к записи временного файла за пределами tmp_dir.
    """
    name = os.path.basename((name or '').strip().replace('\\', '/'))
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('.')
    return name or default


# ── Schemas ──────────────────────────────────────────────────────────────────

class ImageDownloadResponse(BaseModel):
    task_id: int
    status: str
    name: str


class ImageDownloadRequest(BaseModel):
    node: PveNode
    storage: str
    # Либо ссылка на запись каталога/зеркала...
    source_id: Optional[str] = None
    # ...либо явные параметры произвольного образа:
    kind: Optional[str] = None          # 'qcow2' | 'vztmpl' | 'iso'
    url: Optional[str] = None
    template: Optional[str] = None      # имя шаблона из репозитория (aplinfo)
    filename: Optional[str] = None
    checksum: Optional[str] = None
    checksum_algorithm: Optional[str] = None

    # Авто-конвертация qcow2 → VM-шаблон (Phase 2)
    to_template: bool = False
    vmid: Optional[int] = None            # желаемый VMID шаблона; None → авто-выбор
    disk_storage: Optional[str] = None   # хранилище диска ВМ (content=images); по умолч. = storage
    cores: Optional[int] = None
    memory: Optional[int] = None         # MB
    bridge: Optional[str] = None
    ciuser: Optional[str] = None
    cipassword: Optional[str] = None
    ssh_keys: Optional[str] = None

    @model_validator(mode='after')
    def _check(self):
        if not self.source_id and not (self.url or self.template):
            raise ValueError('Нужен source_id, либо url/template')
        return self


# ── Catalog ──────────────────────────────────────────────────────────────────

@router.get("/api/images/catalog")
def get_image_catalog(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view")),
):
    """Встроенный каталог cloud-образов + включённые кастомные зеркала."""
    mirrors = db.query(ImageMirror).filter(ImageMirror.enabled.is_(True)).all()
    return {
        "builtin": CATALOG,
        "mirrors": [m.to_dict() for m in mirrors],
    }


# ── Custom mirrors CRUD (admin) ──────────────────────────────────────────────

class ImageMirrorBase(BaseModel):
    name: str
    kind: str = 'qcow2'              # 'qcow2' | 'vztmpl' | 'iso'
    os: Optional[str] = None
    version: Optional[str] = None
    arch: str = 'amd64'
    url: Optional[str] = None
    template: Optional[str] = None
    checksum: Optional[str] = None
    checksum_algorithm: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True

    @model_validator(mode='after')
    def _check(self):
        if self.kind not in ('qcow2', 'vztmpl', 'iso'):
            raise ValueError("kind должен быть 'qcow2', 'vztmpl' или 'iso'")
        if not self.url and not self.template:
            raise ValueError('Нужен url или template')
        if self.kind in ('qcow2', 'iso') and not self.url:
            raise ValueError(f'Для {self.kind} нужен url')
        return self


class ImageMirrorUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    os: Optional[str] = None
    version: Optional[str] = None
    arch: Optional[str] = None
    url: Optional[str] = None
    template: Optional[str] = None
    checksum: Optional[str] = None
    checksum_algorithm: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/api/images/mirrors")
def list_image_mirrors(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    """Список всех кастомных зеркал (включая выключенные) — для управления."""
    mirrors = db.query(ImageMirror).order_by(ImageMirror.created_at.desc()).all()
    return [m.to_dict() for m in mirrors]


@router.post("/api/images/mirrors", status_code=201)
def create_image_mirror(
    data: ImageMirrorBase,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    mirror = ImageMirror(**data.model_dump(), created_by=current_user.id)
    db.add(mirror)
    db.commit()
    db.refresh(mirror)
    logger.info(f"User {current_user.username} created image mirror: {mirror.name}")
    return mirror.to_dict()


@router.put("/api/images/mirrors/{mirror_id}")
def update_image_mirror(
    mirror_id: int,
    data: ImageMirrorUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    mirror = db.query(ImageMirror).filter(ImageMirror.id == mirror_id).first()
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(mirror, k, v)
    db.commit()
    db.refresh(mirror)
    return mirror.to_dict()


@router.delete("/api/images/mirrors/{mirror_id}")
def delete_image_mirror(
    mirror_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    mirror = db.query(ImageMirror).filter(ImageMirror.id == mirror_id).first()
    if not mirror:
        raise HTTPException(status_code=404, detail="Зеркало не найдено")
    db.delete(mirror)
    db.commit()
    logger.info(f"User {current_user.username} deleted image mirror #{mirror_id}")
    return {"deleted": True}


@router.get("/api/{server_id}/images/lxc-templates")
def get_lxc_repo_templates(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view")),
):
    """Динамический список LXC-шаблонов из репозитория Proxmox (aplinfo)."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
    try:
        return JSONResponse(content=client.get_available_lxc_templates(node))
    except Exception as e:
        logger.error(f"Error listing LXC repo templates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/{server_id}/images/node-arch")
def get_node_arch(
    server_id: int,
    node: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view")),
):
    """Архитектура ноды (amd64|arm64) по current-kernel.machine из статуса ноды."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    client = _get_proxmox_client(server)
    if not client.is_connected():
        # Нода недоступна — пусть фронт сам решит (дефолт amd64)
        return {"arch": None, "machine": None}
    status = client.get_node_status(node) or {}
    ck = status.get('current-kernel') or {}
    machine = (ck.get('machine') or status.get('machine') or '').lower()
    arch = None
    if machine in ('x86_64', 'amd64'):
        arch = 'amd64'
    elif machine in ('aarch64', 'arm64'):
        arch = 'arm64'
    return {"arch": arch, "machine": machine or None}


@router.get("/api/{server_id}/images/storages")
def get_image_target_storages(
    server_id: int,
    node: str,
    content: str = Query(..., description="iso|vztmpl|images|import"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:view")),
):
    """Целевые хранилища ноды, поддерживающие указанный тип контента."""
    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")
    client = _get_proxmox_client(server)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Cannot connect to Proxmox server")
    try:
        return JSONResponse(content=client.get_download_target_storages(node, content))
    except Exception as e:
        logger.error(f"Error listing target storages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _filename_from_url_response(url: str, headers) -> str:
    """Имя файла из Content-Disposition ответа, иначе — хвост пути (после редиректов)."""
    cd = headers.get('content-disposition')
    if cd:
        msg = _EmailMessage()
        msg['content-disposition'] = cd
        name = msg.get_filename()
        if name:
            return unquote(name.strip())
    path = urlparse(url).path
    return unquote(path.rsplit('/', 1)[-1]) or 'download'


async def _reject_internal_target(request: httpx.Request) -> None:
    """httpx request hook: refuse loopback / link-local / unspecified targets.

    The panel fetches the URL itself, so without this a caller could probe the
    panel's own services (DB, API on localhost) or cloud metadata endpoints
    (169.254.169.254) — on every redirect hop, not just the first URL.
    Private LAN ranges stay allowed: internal mirrors are a legitimate source.
    """
    if request.url.scheme not in ("http", "https"):
        raise httpx.UnsupportedProtocol(f"Scheme not allowed: {request.url.scheme}")
    host = request.url.host
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as e:
        raise httpx.ConnectError(f"Cannot resolve {host}: {e}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
            raise httpx.ConnectError(f"Destination not allowed: {host}")


@router.get("/api/images/resolve-filename")
async def resolve_image_filename(
    url: str = Query(..., description="URL образа для определения реального имени файла"),
    # Only the download dialog (template:manage) needs this; it makes the panel
    # issue requests to caller-chosen URLs, so it is not for read-only roles.
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    """
    Определить настоящее имя файла по произвольному URL.

    Простой хвост пути (как делает фронт на лету) не годится для ссылок вида
    /download?id=123 без расширения — здесь сервер обычно возвращает реальное
    имя в Content-Disposition, поэтому сначала пробуем HEAD и читаем заголовок,
    а если сервер не поддерживает HEAD — открываем GET-поток и сразу же его
    закрываем, не скачивая тело.
    """
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Поддерживаются только http(s)-ссылки")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, max_redirects=5, timeout=10.0, verify=False,
            event_hooks={"request": [_reject_internal_target]},
        ) as hc:
            try:
                resp = await hc.head(url)
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError("HEAD not usable", request=resp.request, response=resp)
                return {"filename": _filename_from_url_response(str(resp.url), resp.headers)}
            except Exception:
                async with hc.stream('GET', url) as stream_resp:
                    return {"filename": _filename_from_url_response(str(stream_resp.url), stream_resp.headers)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось определить имя файла: {e}")


# ── Download ─────────────────────────────────────────────────────────────────

def _resolve_source(db: Session, req: ImageDownloadRequest) -> dict:
    """Свести запрос к набору параметров загрузки: kind/url/template/filename/checksum."""
    if req.source_id:
        # Встроенный каталог
        item = get_catalog_image(req.source_id)
        if item:
            return {
                'kind': item['kind'], 'url': item.get('url'), 'template': None,
                'filename': item.get('filename') or (item.get('url') or '').rsplit('/', 1)[-1],
                'checksum': item.get('checksum'),
                'checksum_algorithm': item.get('checksum_algorithm'),
                'name': item['name'], 'os': item.get('os'), 'version': item.get('version'),
            }
        # Кастомное зеркало: source_id вида "mirror-<id>"
        if req.source_id.startswith('mirror-'):
            try:
                mid = int(req.source_id.split('-', 1)[1])
            except ValueError:
                mid = None
            mirror = db.query(ImageMirror).filter(ImageMirror.id == mid).first() if mid else None
            if mirror:
                fname = (mirror.url or '').rsplit('/', 1)[-1] or mirror.template
                return {
                    'kind': mirror.kind, 'url': mirror.url, 'template': mirror.template,
                    'filename': fname,
                    'checksum': mirror.checksum,
                    'checksum_algorithm': mirror.checksum_algorithm,
                    'name': mirror.name, 'os': mirror.os, 'version': mirror.version,
                }
        raise HTTPException(status_code=404, detail="Источник образа не найден")

    # Произвольный образ из тела запроса
    kind = req.kind or 'qcow2'
    filename = req.filename or (req.url or '').rsplit('/', 1)[-1] or req.template
    if not filename:
        raise HTTPException(status_code=400, detail="Не удалось определить имя файла")
    return {
        'kind': kind, 'url': req.url, 'template': req.template, 'filename': filename,
        'checksum': req.checksum, 'checksum_algorithm': req.checksum_algorithm,
        'name': filename, 'os': None, 'version': None,
    }


@router.post("/api/{server_id}/images/download", response_model=ImageDownloadResponse)
async def download_image(
    server_id: int,
    req: ImageDownloadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    """Запустить фоновую загрузку образа (и опц. конвертацию qcow2 → VM-шаблон)."""
    # Ленивый импорт во избежание циклической зависимости templates <-> api.proxmox
    from ..templates import _deploy_executor
    from .async_ops import _do_image_download_sync, _do_image_template_sync

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    src = _resolve_source(db, req)
    if src['kind'] not in ('qcow2', 'vztmpl', 'iso'):
        raise HTTPException(status_code=400, detail=f"Неизвестный тип образа: {src['kind']}")
    if src['kind'] == 'vztmpl' and not src['template'] and not src['url']:
        raise HTTPException(status_code=400, detail="Для vztmpl нужен url или template")
    if src['kind'] != 'vztmpl' and not src['url']:
        raise HTTPException(status_code=400, detail=f"Для {src['kind']} нужен url")

    to_template = req.to_template and src['kind'] == 'qcow2'
    kind = 'image_template' if to_template else 'image_download'

    task = DeployTask(
        status='pending', step='В очереди...', progress=0,
        kind=kind, name=src['name'],
        server_id=server_id, user_id=current_user.id, node=req.node,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id

    loop = asyncio.get_event_loop()
    if to_template:
        loop.run_in_executor(
            _deploy_executor,
            _do_image_template_sync,
            task_id, server_id, req.node, req.storage, req.disk_storage or req.storage,
            src['url'], src['filename'], src['checksum'], src['checksum_algorithm'],
            src['name'], req.cores or 2, req.memory or 2048, req.bridge or 'vmbr0',
            req.ciuser, req.cipassword, req.ssh_keys, src.get('os'), src.get('version'),
            current_user.id, current_user.username, req.vmid,
        )
    else:
        loop.run_in_executor(
            _deploy_executor,
            _do_image_download_sync,
            task_id, server_id, req.node, req.storage,
            src['kind'], src['filename'], src['url'], src['template'],
            src['checksum'], src['checksum_algorithm'],
            current_user.id, current_user.username,
        )

    logger.info(f"[IMG DL] Task #{task_id} ({kind}) queued: {src['filename']} by {current_user.username}")
    return ImageDownloadResponse(task_id=task_id, status='pending', name=src['name'])


@router.post("/api/{server_id}/images/upload", response_model=ImageDownloadResponse)
async def upload_image_file(
    server_id: int,
    node: str = Form(...),
    storage: str = Form(...),
    content: str = Form('iso'),
    filename: Optional[str] = Form(None),
    checksum: Optional[str] = Form(None),
    checksum_algorithm: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("template:manage")),
):
    """Загрузить локальный файл (ISO/vztmpl) на ноду.

    Сохраняем присланный файл во временный файл на диске бэкенда (basename ==
    итоговое имя в хранилище — так его подхватит client.upload_file через
    file_obj.name), затем фоновый воркер проксирует его в Proxmox через upload
    API и чистит за собой временную директорию.
    """
    from ..templates import _deploy_executor
    from .async_ops import _do_image_upload_sync, _normalize_iso_filename

    server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Proxmox server not found")

    if content not in ('iso', 'vztmpl'):
        raise HTTPException(status_code=400, detail=f"Неподдерживаемый тип контента: {content}")
    # Form fields are not covered by the router's query/path validation.
    require_node_name(node)
    require_pve_id(storage, "storage")

    final_name = _sanitize_filename(filename or file.filename or 'upload.iso')
    if content == 'iso':
        final_name = _normalize_iso_filename(final_name)

    tmp_dir = tempfile.mkdtemp(prefix='pveup_')
    tmp_path = os.path.join(tmp_dir, final_name)
    def _copy_upload() -> int:
        # Starlette has already spooled the body to a temp file; copy it in one
        # worker-thread call (two thread hops per 4 MB chunk for a multi-GB ISO
        # before), keeping the event loop free.
        file.file.seek(0)
        with open(tmp_path, 'wb') as out:
            shutil.copyfileobj(file.file, out, 16 * 1024 * 1024)
            return out.tell()

    size = 0
    try:
        size = await asyncio.to_thread(_copy_upload)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Не удалось сохранить файл: {e}")
    finally:
        await file.close()

    if size == 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Пустой файл")

    task = DeployTask(
        status='pending', step='В очереди...', progress=0,
        kind='image_upload', name=final_name,
        server_id=server_id, user_id=current_user.id, node=node,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_id = task.id

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _deploy_executor, _do_image_upload_sync,
        task_id, server_id, node, storage, content,
        final_name, tmp_path, tmp_dir, checksum, checksum_algorithm,
        current_user.id, current_user.username,
    )

    logger.info(f"[IMG UPLOAD] Task #{task_id} queued: {final_name} ({size} bytes) by {current_user.username}")
    return ImageDownloadResponse(task_id=task_id, status='pending', name=final_name)
