"""
Каталог скриптов — CRUD своих скриптов + управление git-источниками.

  GET    /api/scripts                     — список каталога
  POST   /api/scripts                     — создать свой скрипт (source=manual)
  GET    /api/scripts/{id}                — детали + содержимое
  PUT    /api/scripts/{id}                — редактировать (только manual)
  DELETE /api/scripts/{id}                — удалить (только manual)
  GET    /api/scripts/repos               — список git-источников
  POST   /api/scripts/repos               — добавить git-источник
  POST   /api/scripts/repos/{id}/sync     — запустить синк
  DELETE /api/scripts/repos/{id}          — удалить источник (и его скрипты)
"""

from __future__ import annotations

import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ...auth import PermissionChecker
from ...db import get_db
from ...models import ScriptCatalog, ScriptGitRepo, User
from ...services import script_catalog_sync, script_engine

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,98}[a-z0-9])?$")


class ParamField(BaseModel):
    name: str
    label: str
    type: str = "string"
    required: bool = False
    default: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        # Parameters become `export NAME=...` lines in the rendered script.
        if not script_engine.is_valid_param_name(v):
            raise ValueError("Parameter name must match [A-Za-z_][A-Za-z0-9_]*")
        return v


def _check_interpreter(v: Optional[str]) -> Optional[str]:
    if v is not None and not script_engine.is_valid_interpreter(v):
        raise ValueError("Interpreter must be an absolute path like /bin/bash")
    return v


class ScriptCreateRequest(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    category: Optional[str] = None
    target_type: str = Field(default="guest", pattern="^(node|guest)$")
    interpreter: str = "/bin/bash"
    content: str
    params_schema: List[ParamField] = []

    _interpreter = field_validator("interpreter")(_check_interpreter)


class ScriptUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target_type: Optional[str] = Field(default=None, pattern="^(node|guest)$")
    interpreter: Optional[str] = None
    content: Optional[str] = None
    params_schema: Optional[List[ParamField]] = None

    _interpreter = field_validator("interpreter")(_check_interpreter)


class RepoCreateRequest(BaseModel):
    url: str
    branch: str = "main"
    path_glob: str = "**/*.sh"
    metadata_format: str = Field(default="header-comment", pattern="^(header-comment|community-scripts-ct)$")
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        return script_catalog_sync.validate_repo_url(v)

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        return script_catalog_sync.validate_branch(v)

    @field_validator("path_glob")
    @classmethod
    def _check_glob(cls, v: str) -> str:
        return script_catalog_sync.validate_path_glob(v)


@router.get("/api/scripts")
def list_scripts(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:view")),
):
    items = db.query(ScriptCatalog).order_by(ScriptCatalog.name).all()
    return {"items": [s.to_dict() for s in items]}


@router.post("/api/scripts")
def create_script(
    req: ScriptCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    if not _SLUG_RE.match(req.slug):
        raise HTTPException(status_code=400, detail="Некорректный slug (a-z0-9-)")
    if db.query(ScriptCatalog).filter(ScriptCatalog.slug == req.slug).first():
        raise HTTPException(status_code=409, detail="Скрипт с таким slug уже существует")

    script = ScriptCatalog(
        name=req.name, slug=req.slug, description=req.description,
        category=req.category, target_type=req.target_type,
        interpreter=req.interpreter or "/bin/bash", content=req.content,
        params_schema=[p.model_dump() for p in req.params_schema],
        source="manual", created_by=current_user.id,
    )
    db.add(script)
    db.commit()
    db.refresh(script)
    return script.to_dict(with_content=True)


@router.get("/api/scripts/repos")
def list_repos(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    items = db.query(ScriptGitRepo).order_by(ScriptGitRepo.id).all()
    return {"items": [r.to_dict() for r in items]}


@router.post("/api/scripts/repos")
def create_repo(
    req: RepoCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    repo = ScriptGitRepo(
        url=req.url, branch=req.branch, path_glob=req.path_glob,
        metadata_format=req.metadata_format, enabled=req.enabled,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo.to_dict()


@router.post("/api/scripts/repos/{repo_id}/sync")
def sync_repo(
    repo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    repo = db.get(ScriptGitRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Источник не найден")
    result = script_catalog_sync.sync_git_repo(db, repo)
    return {"success": "error" not in result, **result}


@router.delete("/api/scripts/repos/{repo_id}")
def delete_repo(
    repo_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    repo = db.get(ScriptGitRepo, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Источник не найден")
    db.delete(repo)
    db.commit()
    return {"success": True}


@router.get("/api/scripts/{script_id}")
def get_script(
    script_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:view")),
):
    script = db.get(ScriptCatalog, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Скрипт не найден")
    return script.to_dict(with_content=True)


@router.put("/api/scripts/{script_id}")
def update_script(
    script_id: int,
    req: ScriptUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    script = db.get(ScriptCatalog, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Скрипт не найден")
    if script.source != "manual":
        raise HTTPException(status_code=400, detail="Скрипты из git-источника нельзя редактировать вручную")

    for field in ("name", "description", "category", "target_type", "interpreter", "content"):
        value = getattr(req, field)
        if value is not None:
            setattr(script, field, value)
    if req.params_schema is not None:
        script.params_schema = [p.model_dump() for p in req.params_schema]

    db.commit()
    db.refresh(script)
    return script.to_dict(with_content=True)


@router.delete("/api/scripts/{script_id}")
def delete_script(
    script_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("script:manage")),
):
    script = db.get(ScriptCatalog, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Скрипт не найден")
    if script.source != "manual":
        raise HTTPException(status_code=400, detail="Скрипты из git-источника удаляются вместе с источником")
    db.delete(script)
    db.commit()
    return {"success": True}
