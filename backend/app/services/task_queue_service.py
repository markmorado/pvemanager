"""
Task Queue Service for bulk VM/Container operations
Processes tasks from the queue in background
"""

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from loguru import logger
from sqlalchemy.orm import Session


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


try:
    from backend.app.db import SessionLocal
    from backend.app.models import TaskQueue, ProxmoxServer, User
    from backend.app.proxmox_client import ProxmoxClient
except ImportError:
    from app.db import SessionLocal
    from app.models import TaskQueue, ProxmoxServer, User
    from app.proxmox_client import ProxmoxClient


class TaskQueueService:
    """Service for managing and processing bulk operation tasks"""
    
    # Valid task types
    TASK_TYPES = ['bulk_start', 'bulk_stop', 'bulk_restart', 'bulk_delete', 'bulk_shutdown']
    
    @staticmethod
    def create_task(
        db: Session,
        task_type: str,
        user_id: int,
        items: List[Dict[str, Any]]
    ) -> TaskQueue:
        """
        Create a new bulk operation task
        
        Args:
            db: Database session
            task_type: Type of operation (bulk_start, bulk_stop, etc.)
            user_id: ID of user who initiated
            items: List of VMs/containers to process
                   Each item: {server_id, vmid, vm_type, name, node}
        """
        if task_type not in TaskQueueService.TASK_TYPES:
            raise ValueError(f"Invalid task type: {task_type}")
        
        if not items:
            raise ValueError("No items provided for bulk operation")
        
        task = TaskQueue(
            task_type=task_type,
            status='pending',
            user_id=user_id,
            total_items=len(items),
            completed_items=0,
            failed_items=0,
            task_data=items,
            results=[]
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        
        logger.info(f"[TASK QUEUE] Created task #{task.id}: {task_type} with {len(items)} items")
        return task
    
    @staticmethod
    def get_pending_tasks(db: Session) -> List[TaskQueue]:
        """Get all pending tasks ordered by creation time"""
        return db.query(TaskQueue).filter(
            TaskQueue.status == 'pending'
        ).order_by(TaskQueue.created_at.asc()).all()
    
    @staticmethod
    def get_running_tasks(db: Session) -> List[TaskQueue]:
        """Get all currently running tasks"""
        return db.query(TaskQueue).filter(
            TaskQueue.status == 'running'
        ).all()
    
    @staticmethod
    def get_user_tasks(db: Session, user_id: int, limit: int = 20) -> List[TaskQueue]:
        """Get recent tasks for a user"""
        return db.query(TaskQueue).filter(
            TaskQueue.user_id == user_id
        ).order_by(TaskQueue.created_at.desc()).limit(limit).all()
    
    @staticmethod
    def get_active_tasks(db: Session) -> List[TaskQueue]:
        """Get all active (pending or running) tasks"""
        return db.query(TaskQueue).filter(
            TaskQueue.status.in_(['pending', 'running'])
        ).order_by(TaskQueue.created_at.asc()).all()
    
    @staticmethod
    def cancel_task(db: Session, task_id: int, user_id: int) -> bool:
        """Cancel a pending task"""
        task = db.query(TaskQueue).filter(
            TaskQueue.id == task_id,
            TaskQueue.user_id == user_id,
            TaskQueue.status == 'pending'
        ).first()
        
        if not task:
            return False
        
        task.status = 'cancelled'
        task.completed_at = utcnow()
        db.commit()
        
        logger.info(f"[TASK QUEUE] Task #{task_id} cancelled by user {user_id}")
        return True


class TaskQueueProcessor:
    """Processor that executes tasks from the queue"""
    
    def __init__(self):
        self._proxmox_clients: Dict[int, ProxmoxClient] = {}
    
    def _get_proxmox_client(self, db: Session, server_id: int) -> Optional[ProxmoxClient]:
        """Get or create Proxmox client for server"""
        if server_id in self._proxmox_clients:
            client = self._proxmox_clients[server_id]
            if client.is_connected():
                return client
        
        server = db.query(ProxmoxServer).filter(ProxmoxServer.id == server_id).first()
        if not server:
            return None
        
        try:
            host = server.ip_address or server.hostname
            if server.port and server.port != 8006:
                host = f"{host}:{server.port}"
            
            if server.use_password and server.password:
                client = ProxmoxClient(
                    host=host,
                    user=server.api_user,
                    password=server.password,
                    verify_ssl=server.verify_ssl
                )
            elif server.api_token_name and server.api_token_value:
                client = ProxmoxClient(
                    host=host,
                    user=server.api_user,
                    token_name=server.api_token_name,
                    token_value=server.api_token_value,
                    verify_ssl=server.verify_ssl
                )
            else:
                return None
            
            self._proxmox_clients[server_id] = client
            return client
        except Exception as e:
            logger.error(f"[TASK QUEUE] Failed to connect to server {server_id}: {e}")
            return None
    
    def _execute_vm_action(
        self, 
        client: ProxmoxClient, 
        action: str, 
        vmid: int, 
        vm_type: str, 
        node: str
    ) -> tuple[bool, str]:
        """Execute action on VM/container"""
        try:
            if vm_type == 'lxc':
                if action == 'start':
                    client.start_container(node, vmid)
                elif action == 'stop':
                    client.stop_container(node, vmid)
                elif action == 'restart':
                    client.restart_container(node, vmid)
                elif action == 'shutdown':
                    # Shutdown = graceful stop
                    client.stop_container(node, vmid)
                elif action == 'delete':
                    client.delete_container(node, vmid)
                else:
                    return False, f"Unknown action: {action}"
            else:  # qemu
                if action == 'start':
                    client.start_vm(node, vmid)
                elif action == 'stop':
                    client.stop_vm(node, vmid)
                elif action == 'restart':
                    client.restart_vm(node, vmid)
                elif action == 'shutdown':
                    # Shutdown = graceful stop
                    client.stop_vm(node, vmid)
                elif action == 'delete':
                    client.delete_vm(node, vmid)
                else:
                    return False, f"Unknown action: {action}"
            
            return True, "OK"
        except Exception as e:
            return False, str(e)
    
    def process_task(self, task: TaskQueue) -> None:
        """Process a single task"""
        db = SessionLocal()
        try:
            # Mark as running
            task = db.query(TaskQueue).filter(TaskQueue.id == task.id).first()
            if not task or task.status != 'pending':
                return
            
            task.status = 'running'
            task.started_at = utcnow()
            db.commit()
            
            logger.info(f"[TASK QUEUE] Starting task #{task.id}: {task.task_type}")
            
            # Determine action from task type
            action_map = {
                'bulk_start': 'start',
                'bulk_stop': 'stop',
                'bulk_restart': 'restart',
                'bulk_shutdown': 'shutdown',
                'bulk_delete': 'delete',
            }
            action = action_map.get(task.task_type)
            
            if not action:
                task.status = 'failed'
                task.error_message = f"Unknown task type: {task.task_type}"
                task.completed_at = utcnow()
                db.commit()
                return
            
            results = []
            items = task.task_data or []
            
            for item in items:
                server_id = item.get('server_id')
                vmid = item.get('vmid')
                vm_type = item.get('vm_type', 'qemu')
                node = item.get('node', '')
                name = item.get('name', f'{vm_type.upper()}-{vmid}')
                
                client = self._get_proxmox_client(db, server_id)
                if not client:
                    results.append({
                        'server_id': server_id,
                        'vmid': vmid,
                        'name': name,
                        'success': False,
                        'message': 'Failed to connect to Proxmox server'
                    })
                    task.failed_items += 1
                else:
                    success, message = self._execute_vm_action(client, action, vmid, vm_type, node)
                    results.append({
                        'server_id': server_id,
                        'vmid': vmid,
                        'name': name,
                        'success': success,
                        'message': message
                    })
                    if success:
                        task.completed_items += 1
                    else:
                        task.failed_items += 1
                
                # Update progress
                task.results = results
                db.commit()
            
            # Mark as completed
            task.status = 'completed'
            task.completed_at = utcnow()
            db.commit()
            
            logger.info(f"[TASK QUEUE] Completed task #{task.id}: {task.completed_items} success, {task.failed_items} failed")
            
        except Exception as e:
            logger.error(f"[TASK QUEUE] Task #{task.id} failed with error: {e}")
            try:
                task.status = 'failed'
                task.error_message = str(e)
                task.completed_at = utcnow()
                db.commit()
            except:
                pass
        finally:
            db.close()
    
    def process_pending_tasks(self) -> int:
        """Process all pending tasks. Returns number of tasks processed."""
        db = SessionLocal()
        try:
            # Get pending tasks
            pending_tasks = TaskQueueService.get_pending_tasks(db)
            
            if not pending_tasks:
                return 0
            
            # Check if there's already a running task
            running_tasks = TaskQueueService.get_running_tasks(db)
            if running_tasks:
                logger.debug(f"[TASK QUEUE] {len(running_tasks)} task(s) already running, skipping")
                return 0
            
            # Process first pending task
            task = pending_tasks[0]
            db.close()  # Close before processing (will create new session)
            
            self.process_task(task)
            return 1
            
        except Exception as e:
            logger.error(f"[TASK QUEUE] Error processing pending tasks: {e}")
            return 0
        finally:
            try:
                db.close()
            except:
                pass


# Global processor instance
task_processor = TaskQueueProcessor()


def process_task_queue():
    """Function to be called by scheduler to process queue"""
    return task_processor.process_pending_tasks()
