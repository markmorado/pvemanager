"""
Logs API endpoints
View and manage audit logs
"""

from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..db import get_db
from ..auth import get_current_user, PermissionChecker
from ..models import User, AuditLog
from ..logging_service import LoggingService
from ..template_helpers import add_i18n_context


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ==================== Schemas ====================

class LogResponse(BaseModel):
    id: int
    level: str
    category: str
    action: str
    message: str
    username: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    details: Optional[dict] = None
    request_method: Optional[str] = None
    request_path: Optional[str] = None
    response_status: Optional[int] = None
    duration_ms: Optional[int] = None
    success: bool
    error_message: Optional[str] = None
    created_at: datetime
    # Enhanced logging fields
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    geo_location: Optional[str] = None
    server_id: Optional[int] = None
    server_name: Optional[str] = None
    node_name: Optional[str] = None
    request_body: Optional[dict] = None
    response_body: Optional[dict] = None
    query_params: Optional[str] = None
    error_traceback: Optional[str] = None
    
    class Config:
        from_attributes = True


class LogsListResponse(BaseModel):
    logs: List[LogResponse]
    total: int
    page: int
    limit: int
    pages: int


class LogStatsResponse(BaseModel):
    period_hours: int
    total: int
    by_level: dict
    by_category: dict
    errors_count: int
    failed_logins: int
    recent_errors: List[dict]


# ==================== HTML Endpoints ====================

@router.get("/", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Render logs page"""
    from ..i18n import t
    lang = request.cookies.get("language", "ru")
    context = {
        "request": request,
        "title": t("nav_logs", lang),
        "page_title": f"📋 {t('system_logs', lang)}"
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("logs.html", context)


# ==================== API Endpoints ====================

@router.get("/api/logs", response_model=LogsListResponse)
async def get_logs(
    level: Optional[str] = Query(None, description="Filter by log level"),
    category: Optional[str] = Query(None, description="Filter by category"),
    username: Optional[str] = Query(None, description="Filter by username"),
    ip: Optional[str] = Query(None, description="Filter by IP address"),
    resource_type: Optional[str] = Query(None, description="Filter by resource type"),
    resource_id: Optional[str] = Query(None, description="Filter by resource ID"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD)"),
    search: Optional[str] = Query(None, description="Search in message"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=500, description="Items per page"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("logs.view"))
):
    """Get logs with filtering and pagination"""
    
    # Parse dates
    parsed_date_from = None
    parsed_date_to = None
    
    if date_from:
        try:
            parsed_date_from = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD")
    
    if date_to:
        try:
            parsed_date_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)  # Include the whole day
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD")
    
    # Calculate offset
    offset = (page - 1) * limit
    
    # Get logs
    logs, total = LoggingService.get_logs(
        db=db,
        level=level,
        category=category,
        username=username,
        ip_address=ip,
        resource_type=resource_type,
        resource_id=resource_id,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        search=search,
        limit=limit,
        offset=offset
    )
    
    # Calculate total pages
    pages = (total + limit - 1) // limit if total > 0 else 1
    
    return LogsListResponse(
        logs=[LogResponse.model_validate(log) for log in logs],
        total=total,
        page=page,
        limit=limit,
        pages=pages
    )


@router.get("/api/stats", response_model=LogStatsResponse)
async def get_log_stats(
    hours: int = Query(24, ge=1, le=720, description="Period in hours"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("logs.view"))
):
    """Get log statistics for the specified period"""
    stats = LoggingService.get_stats(db, hours=hours)
    return LogStatsResponse(**stats)


@router.delete("/api/logs")
async def cleanup_logs(
    days: int = Query(30, ge=1, le=365, description="Delete logs older than this many days"),
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("logs.delete"))
):
    """Delete old logs (admin only)"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can delete logs")
    
    deleted = LoggingService.cleanup_old_logs(db, days=days)
    
    # Log this action
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.SYSTEM,
        action="logs_cleanup",
        message=f"Удалено {deleted} логов старше {days} дней",
        username=current_user.username
    )
    
    return {"deleted": deleted, "message": f"Удалено {deleted} логов старше {days} дней"}


@router.get("/api/levels")
async def get_log_levels():
    """Get available log levels"""
    return {
        "levels": [
            {"value": "debug", "label": "Debug", "color": "#6c757d"},
            {"value": "info", "label": "Info", "color": "#0dcaf0"},
            {"value": "warning", "label": "Warning", "color": "#ffc107"},
            {"value": "error", "label": "Error", "color": "#dc3545"},
            {"value": "critical", "label": "Critical", "color": "#dc3545"}
        ]
    }


@router.get("/api/categories")
async def get_log_categories():
    """Get available log categories"""
    return {
        "categories": [
            {"value": "auth", "label": "🔐 Авторизация"},
            {"value": "proxmox", "label": "🖥️ Proxmox"},
            {"value": "ipam", "label": "🌐 IPAM"},
            {"value": "system", "label": "⚙️ Система"},
            {"value": "api", "label": "🔌 API"},
            {"value": "docker", "label": "🐳 Docker"}
        ]
    }
