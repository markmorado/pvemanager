from .base import *

class TaskQueue(Base):
    """Background task queue for bulk operations"""
    __tablename__ = "task_queue"

    id = Column(Integer, primary_key=True, index=True)
    task_type = Column(String(50), nullable=False, index=True)
    status = Column(String(20), nullable=False, default='pending', index=True)
    
    user_id = Column(Integer, nullable=False, index=True)
    
    total_items = Column(Integer, nullable=False, default=0)
    completed_items = Column(Integer, nullable=False, default=0)
    failed_items = Column(Integer, nullable=False, default=0)
    
    task_data = Column(JSON, nullable=False)
    results = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index('idx_task_status_created', 'status', 'created_at'),
        Index('idx_task_user_status', 'user_id', 'status'),
    )

    def __repr__(self):
        return f"<TaskQueue(id={self.id}, type='{self.task_type}', status='{self.status}')>"
    
    @property
    def progress_percent(self) -> int:
        if self.total_items == 0:
            return 0
        return int((self.completed_items + self.failed_items) / self.total_items * 100)
    
    def to_dict(self) -> dict:
        _desc_map = {
            'bulk_start':    'Массовый запуск ВМ/LXC',
            'bulk_stop':     'Массовая остановка ВМ/LXC',
            'bulk_restart':  'Массовый перезапуск ВМ/LXC',
            'bulk_delete':   'Массовое удаление ВМ/LXC',
            'bulk_shutdown': 'Массовое выключение ВМ/LXC',
        }
        return {
            'id': self.id,
            'kind': 'bulk',
            'user_id': self.user_id,
            'task_type': self.task_type,
            'description': _desc_map.get(self.task_type, self.task_type),
            'status': self.status,
            'total_items': self.total_items,
            'completed_items': self.completed_items,
            'failed_items': self.failed_items,
            'processed_items': self.completed_items + self.failed_items,
            'progress_percent': self.progress_percent,
            'results': self.results,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }

class ProxmoxTask(Base):
    """Tracks long-running Proxmox tasks identified by UPID."""
    __tablename__ = "proxmox_tasks"

    id = Column(Integer, primary_key=True, index=True)
    upid = Column(String(200), nullable=False, index=True)
    server_id = Column(Integer, ForeignKey("proxmox_servers.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    node = Column(String(100), nullable=True)
    vmid = Column(Integer, nullable=True)
    vm_type = Column(String(10), nullable=True)
    action = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="running", index=True)
    exit_status = Column(String(100), nullable=True)
    log_text = Column(Text, nullable=True)
    # 0-100 percentage parsed from the task log (e.g. qmrestore "progress N%").
    # NULL when the task type does not report progress.
    progress = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'kind': 'proxmox',
            'upid': self.upid,
            'server_id': self.server_id,
            'user_id': self.user_id,
            'node': self.node,
            'vmid': self.vmid,
            'vm_type': self.vm_type,
            'action': self.action,
            'description': self.description,
            'status': self.status,
            'exit_status': self.exit_status,
            # log_text is deliberately left out: this dict is sent in task lists
            # (polled every few seconds) and on every WS task_update tick. The
            # log is served on demand by /api/proxmox-tasks/{id}/log.
            'progress': self.progress,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }

    def __repr__(self):
        return f"<ProxmoxTask(id={self.id}, upid='{self.upid[:30]}...', status='{self.status}')>"

class DeployTask(Base):
    """Tracks async VM deployment tasks from templates"""
    __tablename__ = "deploy_tasks"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String(20), nullable=False, default='pending', index=True)
    kind = Column(String(30), nullable=False, default='deploy', index=True)
    step = Column(String(200), nullable=True)
    progress = Column(Integer, nullable=False, default=0)

    name = Column(String(100), nullable=False)
    template_id = Column(Integer, nullable=True)
    server_id = Column(Integer, nullable=True)
    target_server_id = Column(Integer, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    vmid = Column(Integer, nullable=True)
    node = Column(String(100), nullable=True)

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index('idx_deploy_task_user_status', 'user_id', 'status'),
    )

    def to_dict(self) -> dict:
        kind = self.kind or 'deploy'
        descriptions = {
            'deploy': f"Создание {self.name}",
            'vm_iso': f"Создание ВМ под ISO: {self.name}",
            'reinstall': f"Переустановка {self.name}",
            'clone': f"Клонирование {self.name}",
            'change_password': f"Смена пароля: {self.name}",
            'image_download': f"Загрузка образа: {self.name}",
            'image_template': f"Шаблон из образа: {self.name}",
            'remote_migrate': f"Remote-миграция: {self.name}",
        }
        return {
            'id': self.id,
            'kind': kind,
            'status': self.status,
            'step': self.step,
            'current_step': self.step,
            'progress': self.progress,
            'progress_percent': self.progress,
            'description': descriptions.get(kind, self.name),
            'name': self.name,
            'template_id': self.template_id,
            'server_id': self.server_id,
            'target_server_id': self.target_server_id,
            'vmid': self.vmid,
            'node': self.node,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }

    def __repr__(self):
        return f"<DeployTask(id={self.id}, kind='{self.kind}', name='{self.name}', status='{self.status}')>"
