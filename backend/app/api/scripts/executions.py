"""
Запуск скриптов и журнал выполнений.

  POST /api/scripts/{id}/execute        — запустить скрипт
  GET  /api/scripts/executions          — история запусков текущего пользователя (админ — все)
  GET  /api/scripts/executions/{id}     — детали запуска (вывод, exit code)
"""

from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, defer

from ...auth import PermissionChecker, check_permission
from ...db import SessionLocal, get_db
from ...models import ProxmoxServer, ScriptCatalog, ScriptExecution, User
from ...services import script_engine
from ...config import utcnow

router = APIRouter()

_MAX_OUTPUT = 200_000  # см. SCRIPT_EXECUTION_OUTPUT_LIMIT в models/scripts.py


def _run_execution_bg(execution_id: int, server_id: int, target_type: str,
                      node: Optional[str], vmid: Optional[int],
                      vm_type: Optional[str], rendered: str,
                      interpreter: str, timeout: int) -> None:
    """Фоновое выполнение скрипта: community-скрипты (build.func) идут минуты и
    не помещаются в таймаут прокси/HTTP-запроса. Эндпоинт возвращает execution
    со статусом `running` сразу, а результат дописывается сюда; фронтенд поллит
    статус до завершения. Своя DB-сессия — сессия запроса уже закрыта.
    """
    db = SessionLocal()
    try:
        server = db.get(ProxmoxServer, server_id)
        execution = db.get(ScriptExecution, execution_id)
        if not server or not execution:
            logger.error(f"[scripts] bg exec {execution_id}: server/execution пропал")
            return
        try:
            result = script_engine.execute(
                server, target_type=target_type, node=node, vmid=vmid,
                vm_type=vm_type, script=rendered, interpreter=interpreter,
                timeout=timeout,
            )
            execution.status = "success" if result.success else "failed"
            execution.exit_code = result.exit_code
            execution.output = (result.output or "")[:_MAX_OUTPUT]
            execution.error_text = result.error
        except Exception as e:  # защита фонового потока: не оставляем running навсегда
            logger.exception(f"[scripts] bg exec {execution_id} failed: {e}")
            execution.status = "failed"
            execution.exit_code = -1
            execution.error_text = str(e)
        execution.finished_at = utcnow()
        db.commit()
    finally:
        db.close()


class ExecuteRequest(BaseModel):
    server_id: int
    # Goes into Proxmox API paths (guest agent): plain node names only.
    node: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
    vmid: Optional[int] = None
    vm_type: Optional[str] = Field(default=None, pattern="^(qemu|lxc)$")
    params: dict = {}
    # Верхняя граница поднята под долгие community-скрипты (build.func ставит
    # Java/OpenSearch/MongoDB и т.п. — это минуты). Выполнение фоновое, так что
    # длинный таймаут не держит HTTP-запрос. 0 → взять дефолт по типу скрипта.
    timeout: int = Field(default=0, ge=0, le=3600)


# Дефолтные таймауты выполнения по источнику скрипта (сек), когда клиент не
# задал timeout явно (req.timeout == 0). Community-скрипты создают LXC и тянут
# образы — им нужен большой запас; обычные скрипты остаются короткими.
_DEFAULT_TIMEOUT = 60
_COMMUNITY_TIMEOUT = 1800


def _can_admin(user: User) -> bool:
    return bool(getattr(user, "is_admin", False)) or user.has_permission("script:manage")


@router.post("/api/scripts/{script_id}/execute")
def execute_script(
    script_id: int,
    req: ExecuteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:execute")),
):
    from ..proxmox._helpers import get_visible_server_ids, require_vm_access

    script = db.get(ScriptCatalog, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Скрипт не найден")

    server = db.get(ProxmoxServer, req.server_id)
    visible = get_visible_server_ids(request, db, current_user) if server else None
    if not server or (visible is not None and server.id not in visible):
        raise HTTPException(status_code=404, detail="Сервер не найден")

    if script.target_type == "guest" and (req.vmid is None or not req.node or not req.vm_type):
        raise HTTPException(status_code=400, detail="Для guest-скрипта нужны node, vmid и vm_type")

    # script:execute alone used to allow running anything anywhere: a tenant
    # could target other users' guests or get a root shell on the host.
    if script.target_type == "node":
        if not check_permission(current_user, "node:manage"):
            raise HTTPException(status_code=403, detail="Для запуска на ноде нужно право node:manage")
    else:
        require_vm_access(db, current_user, server.id, req.vmid)

    # Валидация обязательных параметров по схеме скрипта.
    schema = script.params_schema or []
    params = dict(req.params or {})
    for field in schema:
        name = field.get("name")
        if not name:
            continue
        if name not in params or params[name] in (None, ""):
            if field.get("required"):
                raise HTTPException(status_code=400, detail=f"Параметр '{name}' обязателен")
            if field.get("default") is not None:
                params[name] = field["default"]
    params = {k: str(v) for k, v in params.items() if k in {f.get("name") for f in schema}}

    execution = ScriptExecution(
        script_id=script.id, script_name=script.name,
        target_type=script.target_type, server_id=server.id,
        node=req.node, vmid=req.vmid, params_used=params,
        status="running", user_id=current_user.id,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    rendered = script_engine.render_params(script.content, params)

    # Эффективный таймаут: явное значение клиента, иначе дефолт по источнику
    # (community-скрипты идут минуты — см. _COMMUNITY_TIMEOUT).
    is_community = getattr(script, "source", None) == "community-scripts"
    timeout = req.timeout or (_COMMUNITY_TIMEOUT if is_community else _DEFAULT_TIMEOUT)

    # Выполнение — в фоновом потоке: долгие community-скрипты (build.func) не
    # укладываются в таймаут HTTP/прокси (nginx рвёт на 60с → 504). Возвращаем
    # execution со статусом `running`, фронтенд поллит его до завершения.
    threading.Thread(
        target=_run_execution_bg,
        args=(execution.id, server.id, script.target_type, req.node, req.vmid,
              req.vm_type, rendered, script.interpreter, timeout),
        daemon=True,
    ).start()

    return execution.to_dict()


@router.get("/api/scripts/executions")
def list_executions(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:view")),
):
    # Output (up to 200 KB per run) is only needed by the detail endpoint.
    q = db.query(ScriptExecution).options(defer(ScriptExecution.output))
    if not _can_admin(current_user):
        q = q.filter(ScriptExecution.user_id == current_user.id)
    items = q.order_by(ScriptExecution.started_at.desc()).limit(max(1, min(limit, 200))).all()
    return {"items": [e.to_dict(with_output=False) for e in items]}


@router.get("/api/scripts/executions/{execution_id}")
def get_execution(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:view")),
):
    execution = db.get(ScriptExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if not _can_admin(current_user) and execution.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Нет доступа к этому запуску")
    return execution.to_dict()
