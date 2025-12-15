"""
API routes for notifications
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import get_current_user, PermissionChecker
from ..models import User
from ..services.notification_service import NotificationService
from ..schemas import (
    NotificationResponse,
    NotificationListResponse,
    NotificationUpdate,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/", response_model=NotificationListResponse)
async def get_notifications(
    unread_only: bool = Query(False, description="Only unread notifications"),
    level: Optional[str] = Query(None, description="Filter by level: critical, warning, info, success"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """
    Get user notifications with filters
    
    - **unread_only**: Return only unread notifications
    - **level**: Filter by notification level
    - **limit**: Maximum number of notifications (1-100)
    - **offset**: Offset for pagination
    """
    notifications, total = NotificationService.get_user_notifications(
        db,
        user_id=current_user.id,
        unread_only=unread_only,
        level=level,
        limit=limit,
        offset=offset
    )
    
    unread_count = NotificationService.get_unread_count(db, current_user.id)
    
    return {
        "total": total,
        "unread_count": unread_count,
        "notifications": [n.to_dict() for n in notifications]
    }


@router.get("/unread-count")
async def get_unread_count(
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Get count of unread notifications"""
    count = NotificationService.get_unread_count(db, current_user.id)
    return {"count": count}


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_as_read(
    notification_id: int,
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Mark notification as read"""
    notification = NotificationService.mark_as_read(
        db,
        notification_id=notification_id,
        user_id=current_user.id
    )
    
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    return notification.to_dict()


@router.post("/mark-all-read")
async def mark_all_notifications_as_read(
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Mark all notifications as read"""
    count = NotificationService.mark_all_as_read(db, current_user.id)
    return {"message": f"{count} notifications marked as read"}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: int,
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Delete a notification"""
    success = NotificationService.delete_notification(
        db,
        notification_id=notification_id,
        user_id=current_user.id
    )
    
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    
    return {"message": "Notification deleted"}


@router.delete("/read/all")
async def delete_all_read_notifications(
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Delete all read notifications"""
    count = NotificationService.delete_all_read(db, current_user.id)
    return {"message": f"{count} notifications deleted"}


# Preferences endpoints

@router.get("/preferences", response_model=NotificationPreferenceResponse)
async def get_notification_preferences(
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Get user notification preferences"""
    prefs = NotificationService.get_user_preferences(db, current_user.id)
    return prefs.to_dict()


@router.put("/preferences", response_model=NotificationPreferenceResponse)
async def update_notification_preferences(
    prefs_data: NotificationPreferenceUpdate,
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """Update user notification preferences"""
    prefs = NotificationService.update_user_preferences(
        db,
        user_id=current_user.id,
        prefs_data=prefs_data
    )
    return prefs.to_dict()


# ============================================
# Channel management endpoints
# ============================================

@router.get("/channels/status")
async def get_channels_status(
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """
    Get status of all notification channels
    
    Returns configuration and status for:
    - Email (SMTP)
    - Telegram
    - In-app notifications
    """
    status = await NotificationService.get_channels_status(db, current_user.id)
    return status


@router.post("/test")
async def send_test_notification(
    channel: str = Query("all", description="Channel to test: all, email, telegram, inapp"),
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """
    Send a test notification through specified channel(s)
    
    - **channel**: Which channel to test (all, email, telegram, inapp)
    """
    if channel not in ["all", "email", "telegram", "inapp"]:
        raise HTTPException(status_code=400, detail="Invalid channel. Use: all, email, telegram, inapp")
    
    results = await NotificationService.send_test_notification(db, current_user.id, channel)
    return results


@router.post("/telegram/verify")
async def verify_telegram_chat(
    chat_id: str = Query(..., description="Telegram chat ID to verify"),
    current_user: User = Depends(PermissionChecker("notifications.manage")),
    db: Session = Depends(get_db)
):
    """
    Verify Telegram chat ID and save to preferences
    
    Steps to get your chat_id:
    1. Start a conversation with @userinfobot in Telegram
    2. It will tell you your chat ID
    3. Make sure you've sent /start to your notification bot first
    """
    from ..services.notification_channels import get_telegram_channel
    
    tg_ch = get_telegram_channel()
    if not tg_ch.is_configured():
        raise HTTPException(
            status_code=400, 
            detail="Telegram бот не настроен. Добавьте токен в Настройки → Уведомления."
        )
    
    # Try to send test message
    success = await tg_ch.verify_chat_id(chat_id)
    
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Failed to send message to this chat_id. Make sure you've started the bot with /start"
        )
    
    # Save chat_id to preferences
    prefs = NotificationService.get_user_preferences(db, current_user.id)
    prefs.telegram_chat_id = chat_id
    prefs.telegram_enabled = True
    db.commit()
    
    return {
        "success": True,
        "message": "Telegram verified and enabled!",
        "chat_id": chat_id
    }


@router.get("/telegram/bot-info")
async def get_telegram_bot_info(
    current_user: User = Depends(PermissionChecker("notifications.manage"))
):
    """Get information about the configured Telegram bot"""
    from ..services.notification_channels import get_telegram_channel
    
    tg_ch = get_telegram_channel()
    if not tg_ch.is_configured():
        return {"configured": False, "bot_info": None}
    
    bot_info = await tg_ch.get_bot_info()
    return {
        "configured": True,
        "bot_info": bot_info
    }
