from .base import *

# target_type скрипта: где он выполняется.
# "node"  — на хосте Proxmox (root@node по SSH)
# "guest" — внутри VM (QEMU guest agent) или LXC (pct exec через SSH ноды)
SCRIPT_TARGET_TYPES = ("node", "guest")
SCRIPT_SOURCES = ("manual", "git", "community-scripts")
SCRIPT_EXECUTION_STATUSES = ("running", "success", "failed")
# Формат метаданных, по которому синкается git-источник:
# "header-comment"       — заголовочные bash-комментарии (# name: / # param: ...)
# "community-scripts-ct" — ct/*.sh из community-scripts/ProxmoxVE (APP=, var_*)
SCRIPT_REPO_METADATA_FORMATS = ("header-comment", "community-scripts-ct")


class ScriptGitRepo(Base):
    """Git-источник каталога скриптов (аналог CatalogProvider для appstore)."""
    __tablename__ = "script_git_repos"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(500), nullable=False)
    branch = Column(String(100), nullable=False, default="main")
    # glob относительно корня репозитория, напр. "scripts/**/*.sh"
    path_glob = Column(String(300), nullable=False, default="**/*.sh")
    # см. SCRIPT_REPO_METADATA_FORMATS
    metadata_format = Column(String(30), nullable=False, default="header-comment")
    enabled = Column(Boolean, nullable=False, default=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "branch": self.branch,
            "path_glob": self.path_glob,
            "metadata_format": self.metadata_format,
            "enabled": self.enabled,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "last_sync_error": self.last_sync_error,
        }

    def __repr__(self):
        return f"<ScriptGitRepo(id={self.id}, url='{self.url}')>"


class ScriptCatalog(Base):
    """Скрипт каталога — свой (manual) или синхронизированный из git."""
    __tablename__ = "script_catalog"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(200), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True, index=True)

    target_type = Column(String(20), nullable=False, default="guest")  # node | guest
    interpreter = Column(String(100), nullable=False, default="/bin/bash")
    content = Column(Text, nullable=False)

    # [{name, label, type: string|number|bool, required, default}]
    params_schema = Column(JSON, nullable=False, default=list)

    source = Column(String(20), nullable=False, default="manual", index=True)  # manual | git
    git_repo_id = Column(Integer, ForeignKey("script_git_repos.id", ondelete="CASCADE"), nullable=True, index=True)
    git_path = Column(String(500), nullable=True)

    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def to_dict(self, *, with_content: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "category": self.category,
            "target_type": self.target_type,
            "interpreter": self.interpreter,
            "params_schema": self.params_schema or [],
            "source": self.source,
            "git_repo_id": self.git_repo_id,
            "git_path": self.git_path,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if with_content:
            data["content"] = self.content
        return data

    def __repr__(self):
        return f"<ScriptCatalog(id={self.id}, slug='{self.slug}', target_type='{self.target_type}')>"


SCRIPT_EXECUTION_OUTPUT_LIMIT = 200_000  # символов, защита от раздувания БД


class ScriptExecution(Base):
    """Журнал запуска скрипта (аналог AppOperation)."""
    __tablename__ = "script_executions"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("script_catalog.id", ondelete="SET NULL"), nullable=True, index=True)
    script_name = Column(String(200), nullable=True)  # снимок имени на момент запуска

    target_type = Column(String(20), nullable=False)  # node | guest
    server_id = Column(Integer, nullable=True, index=True)
    node = Column(String(100), nullable=True)
    vmid = Column(Integer, nullable=True)  # None для target_type="node"

    params_used = Column(JSON, nullable=False, default=dict)
    status = Column(String(20), nullable=False, default="running", index=True)
    exit_code = Column(Integer, nullable=True)
    output = Column(Text, nullable=True)
    error_text = Column(Text, nullable=True)

    user_id = Column(Integer, nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self, *, with_output: bool = True) -> dict:
        data = {
            "id": self.id,
            "kind": "script",
            "script_id": self.script_id,
            "script_name": self.script_name,
            "target_type": self.target_type,
            "server_id": self.server_id,
            "node": self.node,
            "vmid": self.vmid,
            "params_used": self.params_used or {},
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "error_text": self.error_text,
            "user_id": self.user_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
        if not with_output:
            data.pop("output")
        return data

    def to_task_dict(self) -> dict:
        """Представление для общего списка «Задачи» (/api/all-tasks)."""
        return {
            "id": self.id,
            "kind": "script",
            "type": "script_execution",
            "description": f"Скрипт: {self.script_name or self.script_id}",
            "status": self.status,
            "progress": 100 if self.status != "running" else 50,
            "step": None,
            "current_step": None,
            "error_message": self.error_text,
            "user_id": self.user_id,
            "node": self.node,
            "server_id": self.server_id,
            "created_at": self.started_at.isoformat() if self.started_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    def __repr__(self):
        return f"<ScriptExecution(id={self.id}, script_id={self.script_id}, status='{self.status}')>"
