"""
Синхронизация каталога скриптов из git-репозитория (аналог appstore_catalog.py
sync_catalog(), но проще: bash-файлы с метаданными в заголовочных комментариях
вместо отдельных манифестов).

Конвенция метаданных (первые строки файла, до первой не-комментарной строки
после shebang):

    #!/bin/bash
    # name: Очистка старых логов
    # description: Удаляет журналы старше 30 дней в /var/log
    # target: node
    # category: maintenance
    # param: DAYS|Дней хранить|number|false|30

Файлы без `# name:` каталогизируются по имени файла.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from ..config import utcnow
from ..models import ScriptCatalog, ScriptGitRepo

_META_RE = re.compile(r"^#\s*(name|description|target|category)\s*:\s*(.*)$", re.IGNORECASE)
_PARAM_RE = re.compile(r"^#\s*param\s*:\s*(.*)$", re.IGNORECASE)

_CS_APP_RE = re.compile(r'^APP="([^"]+)"')
_CS_VAR_RE = re.compile(r'^(var_\w+)="\$\{var_\w+:-([^}]*)\}"$')


def _parse_script_metadata(text: str, fallback_name: str) -> dict:
    name = fallback_name
    description = None
    target = "guest"
    category = None
    params: List[dict] = []

    for line in text.splitlines()[:60]:
        stripped = line.strip()
        if not stripped.startswith("#"):
            if stripped and not stripped.startswith("#!"):
                break
            continue
        m = _META_RE.match(stripped)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "name":
                name = val
            elif key == "description":
                description = val
            elif key == "target":
                target = val if val in ("node", "guest") else "guest"
            elif key == "category":
                category = val
            continue
        pm = _PARAM_RE.match(stripped)
        if pm:
            parts = [p.strip() for p in pm.group(1).split("|")]
            # name|label|type|required|default
            p_name = parts[0] if len(parts) > 0 else None
            if not p_name:
                continue
            params.append({
                "name": p_name,
                "label": parts[1] if len(parts) > 1 and parts[1] else p_name,
                "type": parts[2] if len(parts) > 2 and parts[2] else "string",
                "required": (parts[3].lower() == "true") if len(parts) > 3 else False,
                "default": parts[4] if len(parts) > 4 else None,
            })

    return {
        "name": name,
        "description": description,
        "target_type": target,
        "category": category,
        "params_schema": params,
    }


def _parse_community_scripts_metadata(text: str, fallback_name: str) -> dict:
    """Метаданные ct/*.sh из community-scripts/ProxmoxVE: APP="..." + var_X="${var_X:-default}".

    В самих ct-скриптах нет полей name/description в привычном виде: имя
    приложения берётся из `APP=`, а параметры — из строк `var_X="${var_X:-DEF}"`
    (тот самый bash-паттерн "переменная окружения с дефолтом", который upstream
    читает при headless-запуске). Эти var_X переносятся в params_schema один в
    один по имени, чтобы существующий execute-движок (export VAR=value перед
    телом скрипта) сработал без изменений.
    """
    name = fallback_name
    params: List[dict] = []

    for line in text.splitlines():
        stripped = line.strip()
        m = _CS_APP_RE.match(stripped)
        if m:
            name = m.group(1)
            continue
        pm = _CS_VAR_RE.match(stripped)
        if pm:
            p_name, default = pm.group(1), pm.group(2)
            label = p_name.removeprefix("var_").replace("_", " ").capitalize()
            params.append({
                "name": p_name,
                "label": label,
                "type": "number" if default.lstrip("-").isdigit() else "string",
                "required": False,
                "default": default,
            })

    return {
        "name": name,
        "description": f"Community-Scripts LXC installer: {name}",
        "target_type": "node",
        "category": "LXC (community-scripts)",
        "params_schema": params,
    }


# Remote URLs accepted for script sources. Anything else is rejected before it
# reaches git: a value starting with "-" would be parsed as an option
# (e.g. --upload-pack=<cmd> runs a command in the panel container), and
# transports like ext::/file:: give local command execution or file access.
_REPO_URL_RE = re.compile(r"^(https?|ssh|git)://[^\s]+$|^[\w.-]+@[\w.-]+:[^\s]+$")
_BRANCH_RE = re.compile(r"^[\w][\w./-]{0,199}$")


def validate_repo_url(url: str) -> str:
    url = (url or "").strip()
    if not _REPO_URL_RE.match(url):
        raise ValueError("URL must be https://, ssh://, git:// or user@host:path")
    return url


def validate_branch(branch: str) -> str:
    branch = (branch or "").strip()
    if not _BRANCH_RE.match(branch) or ".." in branch:
        raise ValueError("Invalid branch name")
    return branch


def validate_path_glob(path_glob: str) -> str:
    """Glob must stay inside the clone: no absolute paths, no '..' segments."""
    path_glob = (path_glob or "").strip()
    norm = path_glob.replace("\\", "/")
    if not norm or norm.startswith("/") or os.path.isabs(path_glob) or ".." in norm.split("/"):
        raise ValueError("path_glob must be a relative pattern inside the repository")
    return path_glob


def _clone_repo(repo: ScriptGitRepo, dest: str) -> None:
    url = validate_repo_url(repo.url)
    branch = validate_branch(repo.branch)
    subprocess.run(
        ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never",
         "clone", "--depth", "1", "--branch", branch, "--", url, dest],
        check=True, capture_output=True, timeout=120, text=True,
    )


def sync_git_repo(db: Session, repo: ScriptGitRepo) -> dict:
    """Синхронизировать один git-источник. Идемпотентно (upsert по slug)."""
    created = updated = skipped = errors = disappeared = 0
    tmp_dir = tempfile.mkdtemp(prefix="pve_script_repo_")
    try:
        try:
            _clone_repo(repo, tmp_dir)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or str(e))[:1000]
            repo.last_sync_error = err
            db.commit()
            return {"error": err, "created": 0, "updated": 0, "skipped": 0, "errors": 1}
        except subprocess.TimeoutExpired:
            repo.last_sync_error = "git clone timeout (>120s)"
            db.commit()
            return {"error": repo.last_sync_error, "created": 0, "updated": 0, "skipped": 0, "errors": 1}
        except ValueError as e:
            repo.last_sync_error = str(e)
            db.commit()
            return {"error": repo.last_sync_error, "created": 0, "updated": 0, "skipped": 0, "errors": 1}

        try:
            path_glob = validate_path_glob(repo.path_glob)
        except ValueError as e:
            repo.last_sync_error = str(e)
            db.commit()
            return {"error": repo.last_sync_error, "created": 0, "updated": 0, "skipped": 0, "errors": 1}

        root = os.path.realpath(tmp_dir)
        pattern = os.path.join(tmp_dir, path_glob)
        files = _glob.glob(pattern, recursive=True)

        is_community_scripts = repo.metadata_format == "community-scripts-ct"
        record_source = "community-scripts" if is_community_scripts else "git"

        # One query instead of one per file.
        existing_by_slug = {
            s.slug: s for s in db.query(ScriptCatalog).filter(ScriptCatalog.git_repo_id == repo.id)
        }

        seen_slugs: List[str] = []
        for path in files:
            # A repository may carry symlinks (e.g. to /proc/self/environ) —
            # reading through them would import panel secrets into the catalog.
            if os.path.islink(path) or not os.path.isfile(path):
                skipped += 1
                continue
            real = os.path.realpath(path)
            if os.path.commonpath([root, real]) != root:
                skipped += 1
                continue
            rel_path = os.path.relpath(path, tmp_dir)
            slug = f"git-{repo.id}-{rel_path}".replace("/", "-").replace("\\", "-")
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                errors += 1
                logger.warning(f"[script-sync] read {rel_path} failed: {e}")
                continue

            if is_community_scripts:
                meta = _parse_community_scripts_metadata(content, fallback_name=os.path.basename(rel_path))
            else:
                meta = _parse_script_metadata(content, fallback_name=os.path.basename(rel_path))

            existing = existing_by_slug.get(slug)
            if existing is None:
                # Slug may belong to a record created before git_repo_id was set.
                existing = db.query(ScriptCatalog).filter(ScriptCatalog.slug == slug).first()
            if existing:
                existing.name = meta["name"]
                existing.description = meta["description"]
                existing.target_type = meta["target_type"]
                existing.category = meta["category"]
                existing.params_schema = meta["params_schema"]
                existing.content = content
                existing.git_path = rel_path
                existing.source = record_source
                updated += 1
            else:
                db.add(ScriptCatalog(
                    slug=slug, name=meta["name"], description=meta["description"],
                    target_type=meta["target_type"], category=meta["category"],
                    params_schema=meta["params_schema"], content=content,
                    source=record_source, git_repo_id=repo.id, git_path=rel_path,
                ))
                created += 1
            seen_slugs.append(slug)
        db.commit()

        # Удалить из этого источника то, что исчезло из репозитория.
        stale = db.query(ScriptCatalog).filter(
            ScriptCatalog.git_repo_id == repo.id,
            ~ScriptCatalog.slug.in_(seen_slugs) if seen_slugs else True,
        ).all()
        for s in stale:
            db.delete(s)
            disappeared += 1
        db.commit()

        repo.last_synced_at = utcnow()
        repo.last_sync_error = None
        db.commit()

        return {"created": created, "updated": updated, "skipped": skipped,
                "errors": errors, "disappeared": disappeared}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
