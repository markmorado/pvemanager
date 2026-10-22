"""
Backup Scheduler Service
========================
APScheduler-based service for running scheduled backup jobs defined in the database.
Runs vzdump on the configured Proxmox server at the specified cron times.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..db import SessionLocal
from ..models import BackupJob, ProxmoxServer
from ..proxmox import ProxmoxClient


# Singleton scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


class BackupScheduler:
    """Wrapper around APScheduler for managing backup jobs"""

    def __init__(self):
        self._aps = get_scheduler()

    def _job_id(self, job_id: int) -> str:
        return f"backup_job_{job_id}"

    def add_job_from_model(self, job: BackupJob) -> None:
        """Register a BackupJob in APScheduler"""
        if not job.enabled:
            return
        job_id_str = self._job_id(job.id)

        # Parse cron expression (5 or 6 fields)
        parts = job.cron_expression.strip().split()
        if len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
            trigger = CronTrigger(
                minute=minute, hour=hour,
                day=day, month=month, day_of_week=day_of_week,
                timezone="UTC",
            )
        else:
            logger.warning(f"BackupJob {job.id}: invalid cron expression '{job.cron_expression}'")
            return

        # Remove existing job if any
        if self._aps.get_job(job_id_str):
            self._aps.remove_job(job_id_str)

        self._aps.add_job(
            run_backup_job,
            trigger=trigger,
            args=[job.id],
            id=job_id_str,
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info(f"Scheduled backup job {job.id} with cron '{job.cron_expression}'")

    def update_job_from_model(self, job: BackupJob) -> None:
        """Update (replace) a job in APScheduler"""
        self.remove_job(job.id)
        if job.enabled:
            self.add_job_from_model(job)

    def remove_job(self, job_id: int) -> None:
        """Remove a job from APScheduler"""
        job_id_str = self._job_id(job_id)
        if self._aps.get_job(job_id_str):
            self._aps.remove_job(job_id_str)
            logger.info(f"Removed backup job {job_id} from scheduler")

    def load_all_from_db(self) -> int:
        """Load and schedule all enabled jobs from the database on startup"""
        db = SessionLocal()
        count = 0
        try:
            jobs = db.query(BackupJob).filter(BackupJob.enabled.is_(True)).all()
            for job in jobs:
                try:
                    self.add_job_from_model(job)
                    count += 1
                except Exception as e:
                    logger.warning(f"Could not schedule backup job {job.id}: {e}")
        finally:
            db.close()
        logger.info(f"Loaded {count} backup jobs from database")
        return count

    def start(self) -> None:
        if not self._aps.running:
            self._aps.start()
            logger.info("Backup scheduler started")

    def shutdown(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)
            logger.info("Backup scheduler stopped")


# Module-level singleton
scheduler = BackupScheduler()


async def run_backup_job(job_id: int) -> None:
    """Execute a single backup job. Called by APScheduler or manual trigger.

    The work is blocking (DB + Proxmox HTTP per guest); APScheduler runs this
    coroutine on the application's event loop, so it goes to a worker thread.
    """
    await asyncio.to_thread(_run_backup_job_sync, job_id)


def _run_backup_job_sync(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.query(BackupJob).filter(BackupJob.id == job_id).first()
        if not job:
            logger.warning(f"Backup job {job_id} not found, skipping")
            return

        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == job.server_id).first()
        if not server:
            logger.error(f"Backup job {job_id}: server {job.server_id} not found")
            _update_job_status(db, job, "failed", "Server not found")
            return

        _update_job_status(db, job, "running")
        logger.info(f"Running backup job {job_id} for vmids={job.vmids} on {server.name}/{job.node}")

        try:
            client = _build_client(server)
        except Exception as e:
            _update_job_status(db, job, "failed", str(e))
            return

        vmids = job.vmids
        if vmids == ["all"] or vmids == "all":
            target_vmids = ["all"]
        else:
            try:
                target_vmids = [int(v) for v in vmids] if vmids else []
            except (TypeError, ValueError):
                # Left "running" forever otherwise (jobs created before validation).
                _update_job_status(db, job, "failed", f"Invalid vmids: {vmids!r}")
                return

        errors = []
        last_upid = None

        for vmid in (target_vmids if target_vmids != ["all"] else ["all"]):
            try:
                result = client.create_backup(
                    node=job.node,
                    # "all" пробрасываем как есть: create_backup превратит его в
                    # параметр vzdump all=1 (vmid=0 PVE отвергает с ошибкой)
                    vmid=vmid,
                    storage=job.storage,
                    mode=job.mode,
                    compress=job.compress,
                    remove=1 if job.keep_last else 0,
                    keep_last=job.keep_last,
                    notes=job.notes,
                )
                if result.get("success"):
                    last_upid = result.get("upid")
                    logger.info(f"Backup job {job_id} VMID {vmid}: started, UPID={last_upid}")
                else:
                    errors.append(f"VMID {vmid}: {result.get('error', 'unknown error')}")
            except Exception as e:
                errors.append(f"VMID {vmid}: {str(e)}")

        if errors:
            _update_job_status(db, job, "failed", "; ".join(errors), last_upid)
        else:
            _update_job_status(db, job, "success", None, last_upid)

        # Send notification to all admins
        try:
            from ..services.notification_service import NotificationService
            from ..models import User
            status_text = "успешно" if not errors else "с ошибками"
            level = "success" if not errors else "warning"
            admins = db.query(User).filter(User.is_admin == True).all()  # noqa: E712
            for admin in admins:
                NotificationService.notify_system_event(
                    db=db,
                    user_id=admin.id,
                    title=f"Резервное копирование #{job_id}",
                    message=f"Задание завершено {status_text}. VMIDs: {vmids}",
                    level=level,
                )
        except Exception as e:
            logger.warning(f"Could not send backup notification: {e}")

    finally:
        db.close()


def _build_client(server: ProxmoxServer) -> ProxmoxClient:
    """Build a ProxmoxClient from a ProxmoxServer model.

    Идём через from_server — он учитывает нестандартный порт (!= 8006) и
    корректно выбирает token/password. Ручная сборка по ip_address ломала
    бэкапы для серверов на кастомном порту.
    """
    return ProxmoxClient.from_server(server)


def _update_job_status(db, job: BackupJob, status: str,
                       error: str = None, upid: str = None) -> None:
    """Update job tracking fields in DB"""
    try:
        job.last_run_at = datetime.now(timezone.utc)
        job.last_status = status
        job.last_error = error
        if upid:
            job.last_upid = upid
        db.commit()
    except Exception as e:
        logger.warning(f"Could not update backup job {job.id} status: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def start_backup_scheduler() -> BackupScheduler:
    """Start the backup scheduler and load jobs from DB. Called from app lifespan."""
    scheduler.load_all_from_db()
    scheduler.start()
    return scheduler
