"""
App Store — эндпоинты каталога (M1).

  GET  /api/appstore/catalog            — список карточек (поиск/категория/пагинация)
  GET  /api/appstore/catalog/meta       — метаданные (last_synced_at, count, категории)
  GET  /api/appstore/catalog/{app_id}   — деталь приложения (описание, форма, compose)
  GET  /api/appstore/catalog/{app_id}/logo   — логотип из кэша
  POST /api/appstore/catalog/sync       — ручная синхронизация (admin)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...auth import PermissionChecker
from ...config import settings
from ...db import SessionLocal, get_db
from ...models import CatalogApp, User
from ...services import appstore_engine as engine
from ...services.appstore_catalog import get_catalog_meta, sync_catalog

router = APIRouter()


@router.get("/api/appstore/catalog")
def list_catalog(
    q: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    available_only: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: object = Depends(PermissionChecker("app:view")),
):
    query = db.query(CatalogApp)
    if available_only:
        query = query.filter(CatalogApp.available.is_(True))
    if source:
        query = query.filter(CatalogApp.source == source.strip().lower())
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(
            CatalogApp.name.ilike(like),
            CatalogApp.short_desc.ilike(like),
            CatalogApp.app_id.ilike(like),
        ))

    rows = query.order_by(CatalogApp.name.asc()).all()

    # Фильтр по категории — в Python: categories хранится как JSON-массив строк,
    # это надёжнее JSONB-операторов на generic JSON-колонке (независимо от диалекта).
    if category:
        rows = [r for r in rows if category in (r.categories or [])]

    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [a.to_dict(light=True) for a in page],
    }


@router.get("/api/appstore/catalog/meta")
def catalog_meta(
    db: Session = Depends(get_db),
    _: object = Depends(PermissionChecker("app:view")),
):
    return get_catalog_meta(db)


@router.get("/api/appstore/catalog/{app_id}")
def get_app(
    app_id: str,
    db: Session = Depends(get_db),
    _: object = Depends(PermissionChecker("app:view")),
):
    app = db.query(CatalogApp).filter(CatalogApp.app_id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="Приложение не найдено в каталоге")
    return app.to_dict(with_compose=True)


@router.get("/api/appstore/catalog/{app_id}/logo")
def get_app_logo(
    app_id: str,
    db: Session = Depends(get_db),
):
    # Публичный: логотипы — брендовые картинки, не секреты; тег <img> не шлёт JWT.
    app = db.query(CatalogApp).filter(CatalogApp.app_id == app_id).first()
    if not app or not app.logo_path:
        raise HTTPException(status_code=404, detail="Логотип не найден")
    base = Path(settings.APPSTORE_DATA_DIR).resolve()
    path = (base / app.logo_path).resolve()
    # защита от path traversal: файл обязан лежать внутри data-каталога
    if base not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл логотипа отсутствует")
    # Logos come from third-party catalog repos and are often SVG, served from
    # the panel's origin: opened directly, a script inside would run next to the
    # session token. The sandbox CSP keeps <img> rendering and blocks scripts.
    return FileResponse(str(path), headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=86400",
    })


@router.post("/api/appstore/catalog/sync")
async def trigger_sync(
    _: object = Depends(PermissionChecker("app:manage")),
):
    """Ручной запуск синхронизации каталога. Блокирующий синк — в пуле потоков."""
    def _do() -> dict:
        db = SessionLocal()
        try:
            return sync_catalog(db)
        finally:
            db.close()

    try:
        stats = await asyncio.get_event_loop().run_in_executor(None, _do)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Синхронизация не удалась: {e}")
    return {"success": True, "stats": stats}


# ── Золотой шаблон (Workstream B) ─────────────────────────────────────────────

_PVE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


class GoldenTemplateRequest(BaseModel):
    # These go into root shell commands on the node (pct create / pvesm).
    server_id: int
    node: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
    template_storage: str = Field(default="local", pattern=_PVE_ID)    # dir-storage для итогового vztmpl
    rootfs_storage: str = Field(default="local-lvm", pattern=_PVE_ID)  # storage для rootfs временного CT
    bridge: str = Field(default="vmbr0", pattern=_PVE_ID)
    force: bool = False                 # пересобрать, даже если шаблон уже есть


@router.post("/api/appstore/golden-template")
def build_golden_template(
    req: GoldenTemplateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("app:manage")),
):
    """Собрать золотой LXC-шаблон на ноде (фоновая операция). Прогресс — по WebSocket."""
    from ..proxmox._helpers import get_visible_server_ids
    visible = get_visible_server_ids(request, db, current_user)
    if visible is not None and req.server_id not in visible:
        raise HTTPException(status_code=404, detail="Proxmox сервер не найден")
    try:
        result = engine.prepare_golden_template(
            db, current_user, server_id=req.server_id, node=req.node,
            template_storage=req.template_storage, rootfs_storage=req.rootfs_storage,
            bridge=req.bridge, force=req.force,
        )
    except engine.EngineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, **result}


@router.get("/api/appstore/golden-template/status")
def golden_template_status(
    server_id: int = Query(...),
    db: Session = Depends(get_db),
    _: object = Depends(PermissionChecker("app:view")),
):
    """Текущий шаблон конкретного сервера (БД/env) и последняя операция сборки на нём.

    vztmpl — файл на storage конкретного Proxmox-сервера, поэтому статус
    обязательно скопирован по server_id (шаблон сервера A не существует и не
    виден на сервере B).
    """
    return engine.golden_template_status(db, server_id)
