"""
Logging Service for PVEmanager
Provides centralized logging functionality for audit, system, and API events
"""

import uuid
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import desc, and_, or_
from loguru import logger

from .models import AuditLog


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


class LoggingService:
    """Centralized logging service for the application"""
    
    # Log levels
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    
    # Log categories
    AUTH = "auth"
    PROXMOX = "proxmox"
    IPAM = "ipam"
    SYSTEM = "system"
    API = "api"
    DOCKER = "docker"
    
    # Sensitive fields to mask in logs
    SENSITIVE_FIELDS = {'password', 'token', 'secret', 'api_key', 'apikey', 'access_token', 'refresh_token'}
    
    @staticmethod
    def generate_request_id() -> str:
        """Generate a unique request ID"""
        return str(uuid.uuid4())
    
    @staticmethod
    def mask_sensitive_data(data: Any, depth: int = 0) -> Any:
        """Mask sensitive fields in data for safe logging"""
        if depth > 10:  # Prevent infinite recursion
            return data
            
        if isinstance(data, dict):
            masked = {}
            for key, value in data.items():
                key_lower = key.lower()
                if any(sensitive in key_lower for sensitive in LoggingService.SENSITIVE_FIELDS):
                    masked[key] = "***MASKED***"
                else:
                    masked[key] = LoggingService.mask_sensitive_data(value, depth + 1)
            return masked
        elif isinstance(data, list):
            return [LoggingService.mask_sensitive_data(item, depth + 1) for item in data]
        return data
    
    @staticmethod
    def truncate_data(data: Any, max_size: int = 5000) -> Any:
        """Truncate large data for storage"""
        if data is None:
            return None
        if isinstance(data, str):
            if len(data) > max_size:
                return data[:max_size] + f"... (truncated, {len(data)} total chars)"
            return data
        if isinstance(data, dict):
            import json
            json_str = json.dumps(data)
            if len(json_str) > max_size:
                return {"_truncated": True, "_size": len(json_str), "_preview": json_str[:500]}
            return data
        return data
    
    @staticmethod
    def log(
        db: Session,
        level: str,
        category: str,
        action: str,
        message: str,
        user_id: Optional[int] = None,
        username: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        resource_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        request_method: Optional[str] = None,
        request_path: Optional[str] = None,
        response_status: Optional[int] = None,
        duration_ms: Optional[int] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        # New enhanced fields
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        geo_location: Optional[str] = None,
        server_id: Optional[int] = None,
        server_name: Optional[str] = None,
        node_name: Optional[str] = None,
        request_body: Optional[Dict[str, Any]] = None,
        response_body: Optional[Dict[str, Any]] = None,
        query_params: Optional[str] = None,
        error_traceback: Optional[str] = None
    ) -> AuditLog:
        """
        Create a new audit log entry with enhanced details
        
        Args:
            db: Database session
            level: Log level (debug, info, warning, error, critical)
            category: Log category (auth, proxmox, ipam, system, api)
            action: Action performed (login, vm_start, etc.)
            message: Human-readable message
            user_id: User ID (optional)
            username: Username (optional)
            ip_address: Client IP address (optional)
            user_agent: Client user agent (optional)
            resource_type: Type of resource (vm, container, etc.)
            resource_id: ID of the resource
            resource_name: Name of the resource
            details: Additional details as JSON
            request_method: HTTP method
            request_path: HTTP path
            response_status: HTTP response status code
            duration_ms: Request duration in milliseconds
            success: Whether the action was successful
            error_message: Error message if failed
            request_id: Unique request ID for correlation
            session_id: User session ID
            geo_location: Geographic location from IP
            server_id: Proxmox server ID
            server_name: Proxmox server name
            node_name: Proxmox node name
            request_body: Request body (sensitive data masked)
            response_body: Response body (truncated)
            query_params: Query string parameters
            error_traceback: Stack trace for errors
            
        Returns:
            Created AuditLog instance
        """
        try:
            # Mask sensitive data in request body
            safe_request_body = None
            if request_body:
                safe_request_body = LoggingService.mask_sensitive_data(request_body)
                safe_request_body = LoggingService.truncate_data(safe_request_body)
            
            # Truncate response body
            safe_response_body = LoggingService.truncate_data(response_body) if response_body else None
            
            # Mask sensitive data in details
            safe_details = LoggingService.mask_sensitive_data(details) if details else None
            
            log_entry = AuditLog(
                level=level,
                category=category,
                action=action,
                message=message,
                request_id=request_id,
                session_id=session_id,
                user_id=user_id,
                username=username,
                ip_address=ip_address,
                user_agent=user_agent,
                geo_location=geo_location,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id else None,
                resource_name=resource_name,
                server_id=server_id,
                server_name=server_name,
                node_name=node_name,
                details=safe_details,
                request_body=safe_request_body,
                response_body=safe_response_body,
                request_method=request_method,
                request_path=request_path,
                query_params=query_params,
                response_status=response_status,
                duration_ms=duration_ms,
                success=success,
                error_message=error_message,
                error_traceback=error_traceback
            )
            db.add(log_entry)
            db.commit()
            db.refresh(log_entry)
            
            # Also log to file/console via loguru with request_id for correlation
            log_method = getattr(logger, level, logger.info)
            log_prefix = f"[{request_id[:8]}] " if request_id else ""
            log_method(f"{log_prefix}[{category.upper()}] {action}: {message}")
            
            return log_entry
        except Exception as e:
            logger.error(f"Failed to create audit log: {e}")
            db.rollback()
            return None
    
    @staticmethod
    def log_auth(
        db: Session,
        action: str,
        username: str,
        ip_address: str,
        success: bool = True,
        error_message: Optional[str] = None,
        user_agent: Optional[str] = None,
        user_id: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> AuditLog:
        """Log authentication events"""
        level = LoggingService.INFO if success else LoggingService.WARNING
        message = f"User '{username}' {action}"
        if not success:
            message += f" failed: {error_message}"
        
        return LoggingService.log(
            db=db,
            level=level,
            category=LoggingService.AUTH,
            action=action,
            message=message,
            user_id=user_id,
            username=username,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            error_message=error_message,
            details=details
        )
    
    @staticmethod
    def log_proxmox_action(
        db: Session,
        action: str,
        resource_type: str,
        resource_id: Any,
        username: Optional[str] = None,
        resource_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        ip_address: Optional[str] = None,
        server_id: Optional[int] = None,
        server_name: Optional[str] = None,
        node_name: Optional[str] = None,
        request_id: Optional[str] = None,
        error_traceback: Optional[str] = None
    ) -> AuditLog:
        """Log Proxmox actions (VM/Container operations)"""
        level = LoggingService.INFO if success else LoggingService.ERROR
        
        action_messages = {
            "start": "started",
            "stop": "stopped",
            "restart": "restarted",
            "shutdown": "shutdown",
            "delete": "deleted",
            "create": "created",
            "clone": "cloned",
            "migrate": "migrated",
            "backup": "backup created",
            "restore": "restored",
            "snapshot": "snapshot created",
            "resize": "disk resized",
            "console": "console accessed",
            "config_update": "config updated"
        }
        
        action_text = action_messages.get(action, action)
        type_text = "VM" if resource_type == "vm" else "Container" if resource_type == "container" else resource_type
        
        message = f"{type_text} {resource_id}"
        if resource_name:
            message += f" ({resource_name})"
        message += f" {action_text}"
        if node_name:
            message += f" on node {node_name}"
        if server_name:
            message += f" [{server_name}]"
        if username:
            message += f" by {username}"
        
        if not success:
            message += f" - ERROR: {error_message}"
        
        # Enhance details with server context
        enhanced_details = details.copy() if details else {}
        if server_id:
            enhanced_details['server_id'] = server_id
        if server_name:
            enhanced_details['server_name'] = server_name
        if node_name:
            enhanced_details['node'] = node_name
        
        return LoggingService.log(
            db=db,
            level=level,
            category=LoggingService.PROXMOX,
            action=f"{resource_type}_{action}",
            message=message,
            username=username,
            ip_address=ip_address,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            server_id=server_id,
            server_name=server_name,
            node_name=node_name,
            details=enhanced_details,
            success=success,
            error_message=error_message,
            request_id=request_id,
            error_traceback=error_traceback
        )
    
    @staticmethod
    def log_ipam_action(
        db: Session,
        action: str,
        resource_type: str,
        resource_id: Any,
        username: Optional[str] = None,
        resource_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None
    ) -> AuditLog:
        """Log IPAM actions"""
        action_messages = {
            "allocate": "выделен IP адрес",
            "release": "освобождён IP адрес",
            "create_network": "создана сеть",
            "delete_network": "удалена сеть",
            "create_pool": "создан пул адресов",
            "delete_pool": "удалён пул адресов"
        }
        
        message = action_messages.get(action, action)
        if resource_name:
            message += f": {resource_name}"
        if username:
            message += f" (пользователь: {username})"
        
        return LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.IPAM,
            action=action,
            message=message,
            username=username,
            ip_address=ip_address,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            details=details
        )
    
    @staticmethod
    def log_api_request(
        db: Session,
        method: str,
        path: str,
        status_code: int,
        duration_ms: int,
        ip_address: Optional[str] = None,
        username: Optional[str] = None,
        user_agent: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> AuditLog:
        """Log API requests"""
        level = LoggingService.DEBUG
        if status_code >= 500:
            level = LoggingService.ERROR
        elif status_code >= 400:
            level = LoggingService.WARNING
        
        success = status_code < 400
        message = f"{method} {path} - {status_code} ({duration_ms}ms)"
        
        return LoggingService.log(
            db=db,
            level=level,
            category=LoggingService.API,
            action="api_request",
            message=message,
            username=username,
            ip_address=ip_address,
            user_agent=user_agent,
            request_method=method,
            request_path=path,
            response_status=status_code,
            duration_ms=duration_ms,
            success=success,
            error_message=error_message
        )
    
    @staticmethod
    def log_system(
        db: Session,
        action: str,
        message: str,
        level: str = "info",
        details: Optional[Dict[str, Any]] = None
    ) -> AuditLog:
        """Log system events"""
        return LoggingService.log(
            db=db,
            level=level,
            category=LoggingService.SYSTEM,
            action=action,
            message=message,
            details=details
        )
    
    @staticmethod
    def get_logs(
        db: Session,
        level: Optional[str] = None,
        category: Optional[str] = None,
        username: Optional[str] = None,
        ip_address: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        search: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> tuple[List[AuditLog], int]:
        """
        Get logs with filtering
        
        Returns:
            Tuple of (logs list, total count)
        """
        query = db.query(AuditLog)
        
        # Apply filters
        if level:
            query = query.filter(AuditLog.level == level)
        if category:
            query = query.filter(AuditLog.category == category)
        if username:
            query = query.filter(AuditLog.username.ilike(f"%{username}%"))
        if ip_address:
            query = query.filter(AuditLog.ip_address.ilike(f"%{ip_address}%"))
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if resource_id:
            query = query.filter(AuditLog.resource_id == resource_id)
        if date_from:
            query = query.filter(AuditLog.created_at >= date_from)
        if date_to:
            query = query.filter(AuditLog.created_at <= date_to)
        if request_id:
            query = query.filter(AuditLog.request_id == request_id)
        if search:
            query = query.filter(
                or_(
                    AuditLog.message.ilike(f"%{search}%"),
                    AuditLog.action.ilike(f"%{search}%"),
                    AuditLog.resource_name.ilike(f"%{search}%"),
                    AuditLog.request_id.ilike(f"%{search}%")
                )
            )
        
        # Get total count
        total = query.count()
        
        # Apply pagination and ordering
        logs = query.order_by(desc(AuditLog.created_at)).offset(offset).limit(limit).all()
        
        return logs, total
    
    @staticmethod
    def get_stats(db: Session, hours: int = 24) -> Dict[str, Any]:
        """Get log statistics for the specified period"""
        from sqlalchemy import func
        
        since = utcnow() - timedelta(hours=hours)
        
        # Total logs
        total = db.query(AuditLog).filter(AuditLog.created_at >= since).count()
        
        # By level
        by_level = {}
        for level in ['debug', 'info', 'warning', 'error', 'critical']:
            count = db.query(AuditLog).filter(
                and_(AuditLog.created_at >= since, AuditLog.level == level)
            ).count()
            by_level[level] = count
        
        # By category
        by_category = {}
        for category in ['auth', 'proxmox', 'ipam', 'system', 'api', 'docker']:
            count = db.query(AuditLog).filter(
                and_(AuditLog.created_at >= since, AuditLog.category == category)
            ).count()
            by_category[category] = count
        
        # Recent errors
        errors = db.query(AuditLog).filter(
            and_(
                AuditLog.created_at >= since,
                AuditLog.level.in_(['error', 'critical'])
            )
        ).order_by(desc(AuditLog.created_at)).limit(5).all()
        
        # Failed logins
        failed_logins = db.query(AuditLog).filter(
            and_(
                AuditLog.created_at >= since,
                AuditLog.category == 'auth',
                AuditLog.success == False
            )
        ).count()
        
        return {
            "period_hours": hours,
            "total": total,
            "by_level": by_level,
            "by_category": by_category,
            "errors_count": by_level.get('error', 0) + by_level.get('critical', 0),
            "failed_logins": failed_logins,
            "recent_errors": [
                {
                    "id": e.id,
                    "level": e.level,
                    "category": e.category,
                    "action": e.action,
                    "message": e.message,
                    "created_at": e.created_at.isoformat() if e.created_at else None
                }
                for e in errors
            ]
        }
    
    @staticmethod
    def cleanup_old_logs(db: Session, days: int = 30) -> int:
        """Delete logs older than specified days"""
        cutoff = utcnow() - timedelta(days=days)
        deleted = db.query(AuditLog).filter(AuditLog.created_at < cutoff).delete()
        db.commit()
        logger.info(f"Cleaned up {deleted} audit logs older than {days} days")
        return deleted
