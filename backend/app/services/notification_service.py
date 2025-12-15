"""
Notification Service - Business logic for notifications
"""

from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc
from loguru import logger


def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime"""
    return datetime.now(timezone.utc)


from ..models import Notification, NotificationPreference, User, PanelSettings
from ..schemas import (
    NotificationCreate,
    NotificationUpdate,
    NotificationPreferenceUpdate
)
from ..i18n import t


def get_panel_language(db: Session) -> str:
    """Get panel language from settings"""
    setting = db.query(PanelSettings).filter(PanelSettings.key == "language").first()
    return setting.value if setting else "ru"


class NotificationService:
    """Service for managing notifications"""

    @staticmethod
    def create_notification(
        db: Session,
        notification_data: NotificationCreate
    ) -> Notification:
        """
        Create a new notification
        
        Args:
            db: Database session
            notification_data: Notification data
            
        Returns:
            Created notification
        """
        user_id = notification_data.user_id
        level = notification_data.level
        title = notification_data.title
        
        # Check user preferences
        prefs = NotificationService.get_user_preferences(db, user_id)
        
        if not prefs or not prefs.enabled:
            # User has disabled notifications
            logger.info(f"[NOTIFICATION SKIPPED] User {user_id}: notifications disabled | Level: {level} | Title: {title}")
            return None
        
        # Check if notification level is allowed
        if notification_data.level not in prefs.notification_levels:
            logger.info(f"[NOTIFICATION SKIPPED] User {user_id}: level '{level}' not in allowed levels {prefs.notification_levels} | Title: {title}")
            return None
        
        # Check if notification type is allowed
        if notification_data.type not in prefs.notification_types:
            logger.info(f"[NOTIFICATION SKIPPED] User {user_id}: type '{notification_data.type}' not in allowed types | Title: {title}")
            return None
        
        # Check quiet hours
        if NotificationService._is_quiet_hours(prefs):
            # Only critical notifications during quiet hours
            if notification_data.level != "critical":
                logger.info(f"[NOTIFICATION SKIPPED] User {user_id}: quiet hours active, only critical allowed | Level: {level} | Title: {title}")
                return None
        
        # Create notification
        notification = Notification(
            user_id=notification_data.user_id,
            type=notification_data.type,
            level=notification_data.level,
            title=notification_data.title,
            message=notification_data.message,
            data=notification_data.data,
            link=notification_data.link,
            source=notification_data.source,
            source_id=notification_data.source_id,
            expires_at=notification_data.expires_at or (utcnow() + timedelta(days=7))
        )
        
        db.add(notification)
        db.commit()
        db.refresh(notification)
        
        logger.info(f"[NOTIFICATION CREATED] ID: {notification.id} | User: {user_id} | Level: {level} | Type: {notification_data.type} | Title: {title}")
        
        return notification

    @staticmethod
    def get_user_notifications(
        db: Session,
        user_id: int,
        unread_only: bool = False,
        level: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> tuple[List[Notification], int]:
        """
        Get user notifications with filters
        
        Args:
            db: Database session
            user_id: User ID
            unread_only: Only unread notifications
            level: Filter by level (critical, warning, info, success)
            limit: Maximum number of notifications
            offset: Offset for pagination
            
        Returns:
            Tuple of (notifications, total_count)
        """
        query = db.query(Notification).filter(Notification.user_id == user_id)
        
        if unread_only:
            query = query.filter(Notification.read == False)
        
        if level:
            query = query.filter(Notification.level == level)
        
        # Filter out expired notifications
        query = query.filter(
            or_(
                Notification.expires_at.is_(None),
                Notification.expires_at > utcnow()
            )
        )
        
        total = query.count()
        notifications = query.order_by(desc(Notification.created_at)).limit(limit).offset(offset).all()
        
        return notifications, total

    @staticmethod
    def get_unread_count(db: Session, user_id: int) -> int:
        """Get count of unread notifications"""
        return db.query(Notification).filter(
            and_(
                Notification.user_id == user_id,
                Notification.read == False,
                or_(
                    Notification.expires_at.is_(None),
                    Notification.expires_at > utcnow()
                )
            )
        ).count()

    @staticmethod
    def mark_as_read(
        db: Session,
        notification_id: int,
        user_id: int
    ) -> Optional[Notification]:
        """Mark notification as read"""
        notification = db.query(Notification).filter(
            and_(
                Notification.id == notification_id,
                Notification.user_id == user_id
            )
        ).first()
        
        if notification and not notification.read:
            notification.read = True
            notification.read_at = utcnow()
            db.commit()
            db.refresh(notification)
        
        return notification

    @staticmethod
    def mark_all_as_read(db: Session, user_id: int) -> int:
        """Mark all notifications as read for user"""
        count = db.query(Notification).filter(
            and_(
                Notification.user_id == user_id,
                Notification.read == False
            )
        ).update({
            "read": True,
            "read_at": utcnow()
        })
        db.commit()
        return count

    @staticmethod
    def delete_notification(
        db: Session,
        notification_id: int,
        user_id: int
    ) -> bool:
        """Delete a notification"""
        notification = db.query(Notification).filter(
            and_(
                Notification.id == notification_id,
                Notification.user_id == user_id
            )
        ).first()
        
        if notification:
            db.delete(notification)
            db.commit()
            return True
        return False

    @staticmethod
    def delete_all_read(db: Session, user_id: int) -> int:
        """Delete all read notifications for user"""
        count = db.query(Notification).filter(
            and_(
                Notification.user_id == user_id,
                Notification.read == True
            )
        ).delete()
        db.commit()
        return count

    @staticmethod
    def cleanup_expired(db: Session) -> int:
        """Delete all expired notifications"""
        count = db.query(Notification).filter(
            and_(
                Notification.expires_at.isnot(None),
                Notification.expires_at < utcnow()
            )
        ).delete()
        db.commit()
        return count

    # Preferences management
    
    @staticmethod
    def get_user_preferences(
        db: Session,
        user_id: int
    ) -> Optional[NotificationPreference]:
        """Get user notification preferences"""
        prefs = db.query(NotificationPreference).filter(
            NotificationPreference.user_id == user_id
        ).first()
        
        # Create default preferences if not exist
        if not prefs:
            prefs = NotificationPreference(
                user_id=user_id,
                enabled=True,
                email_enabled=False,
                email_critical_only=True,
                telegram_enabled=False,
                notification_levels=["critical", "warning", "info", "success"],
                notification_types=["vm_status", "resource_alert", "system", "ipam", "template"]
            )
            db.add(prefs)
            db.commit()
            db.refresh(prefs)
        
        return prefs

    @staticmethod
    def update_user_preferences(
        db: Session,
        user_id: int,
        prefs_data: NotificationPreferenceUpdate
    ) -> NotificationPreference:
        """Update user notification preferences"""
        prefs = NotificationService.get_user_preferences(db, user_id)
        
        # Update fields
        update_data = prefs_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(prefs, field, value)
        
        prefs.updated_at = utcnow()
        db.commit()
        db.refresh(prefs)
        
        return prefs

    # Helper methods
    
    @staticmethod
    def _is_quiet_hours(prefs: NotificationPreference) -> bool:
        """Check if current time is in quiet hours"""
        if not prefs.quiet_hours_start or not prefs.quiet_hours_end:
            return False
        
        now = utcnow().time()
        start = datetime.strptime(prefs.quiet_hours_start, "%H:%M").time()
        end = datetime.strptime(prefs.quiet_hours_end, "%H:%M").time()
        
        if start <= end:
            return start <= now <= end
        else:
            # Crosses midnight
            return now >= start or now <= end

    # Convenience methods for creating specific notification types
    
    @staticmethod
    def notify_vm_status(
        db: Session,
        user_id: int,
        vm_id: int,
        vm_name: str,
        status: str,
        level: str = "info",
        server_name: str = "",
        old_status: str = ""
    ) -> Optional[Notification]:
        """Create VM status notification"""
        lang = get_panel_language(db)
        
        # Determine title and message based on status
        if status == "running" and old_status == "stopped":
            title = t("notify_vm_started_title", lang, vm_name=vm_name)
            message = t("notify_vm_started_message", lang, vm_name=vm_name, server_name=server_name)
        elif status == "stopped" and old_status == "running":
            title = t("notify_vm_stopped_title", lang, vm_name=vm_name)
            message = t("notify_vm_stopped_message", lang, vm_name=vm_name, server_name=server_name)
        else:
            title = t("notify_vm_status_change_title", lang, vm_name=vm_name)
            message = t("notify_vm_status_change_message", lang, vm_name=vm_name, old_status=old_status, new_status=status)
        
        return NotificationService.create_notification(
            db,
            NotificationCreate(
                user_id=user_id,
                type="vm_status",
                level=level,
                title=title,
                message=message,
                data={"vm_id": vm_id, "vm_name": vm_name, "status": status, "old_status": old_status},
                link=f"/vms/instance/{vm_id}",
                source="proxmox",
                source_id=str(vm_id)
            )
        )

    @staticmethod
    def notify_resource_alert(
        db: Session,
        user_id: int,
        resource_type: str,
        resource_name: str,
        usage_percent: float,
        threshold: float
    ) -> Optional[Notification]:
        """Create resource usage alert"""
        lang = get_panel_language(db)
        level = "critical" if usage_percent >= 90 else "warning"
        
        title = t("notify_resource_alert_title", lang, resource_type=resource_type)
        message = t("notify_resource_alert_message", lang,
                   resource_name=resource_name,
                   usage=f"{usage_percent:.1f}",
                   threshold=f"{threshold:.0f}")
        
        return NotificationService.create_notification(
            db,
            NotificationCreate(
                user_id=user_id,
                type="resource_alert",
                level=level,
                title=title,
                message=message,
                data={
                    "resource_type": resource_type,
                    "resource_name": resource_name,
                    "usage": usage_percent,
                    "threshold": threshold
                },
                source="system"
            )
        )

    @staticmethod
    def notify_system_event(
        db: Session,
        user_id: int,
        title: str,
        message: str,
        level: str = "info",
        data: Optional[dict] = None
    ) -> Optional[Notification]:
        """Create generic system notification"""
        return NotificationService.create_notification(
            db,
            NotificationCreate(
                user_id=user_id,
                type="system",
                level=level,
                title=title,
                message=message,
                data=data,
                source="system"
            )
        )

    # ============================================
    # Async methods for multi-channel notifications
    # ============================================
    
    @staticmethod
    async def create_and_send(
        db: Session,
        user_id: int,
        notification_type: str,
        level: str,
        title: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        link: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        force_send: bool = False
    ) -> Optional[Notification]:
        """
        Create notification and send through enabled channels (Email, Telegram)
        
        Args:
            db: Database session
            user_id: User ID
            notification_type: Type of notification
            level: Level (critical, warning, info, success)
            title: Notification title
            message: Notification message
            data: Additional data
            link: Optional link
            source: Source system
            source_id: Source entity ID
            force_send: If True, send to external channels even if "Critical Only" is enabled
            
        Returns:
            Created notification
        """
        # Create in-app notification
        notification = NotificationService.create_notification(
            db,
            NotificationCreate(
                user_id=user_id,
                type=notification_type,
                level=level,
                title=title,
                message=message,
                data=data,
                link=link,
                source=source,
                source_id=source_id
            )
        )
        
        if not notification:
            logger.warning(f"[NOTIFICATION] In-app notification was not created for user {user_id} | Level: {level} | Title: {title}")
            return None
        
        # Get user preferences for external channels
        prefs = NotificationService.get_user_preferences(db, user_id)
        if not prefs or not prefs.enabled:
            logger.info(f"[EXTERNAL CHANNELS SKIPPED] User {user_id}: preferences disabled | Notification ID: {notification.id}")
            return notification
        
        # Get user for email
        user = db.query(User).filter(User.id == user_id).first()
        
        # Prepare notification dict for formatting
        notification_dict = {
            "title": title,
            "message": message,
            "level": level,
            "source": source or "system",
            "created_at": notification.created_at.strftime("%Y-%m-%d %H:%M:%S") if notification.created_at else "now"
        }
        
        # Log channel delivery attempt details
        logger.info(f"[EXTERNAL CHANNELS] Processing notification {notification.id} | Level: {level} | Email enabled: {prefs.email_enabled} | Telegram enabled: {prefs.telegram_enabled} | Critical only: {prefs.email_critical_only} | Force send: {force_send}")
        
        # Send Email
        if prefs.email_enabled and user and user.email:
            # Check critical only setting (bypass if force_send is True)
            if prefs.email_critical_only and level != "critical" and not force_send:
                logger.info(f"[EMAIL SKIPPED] User {user_id}: critical_only=True but level='{level}' | Notification ID: {notification.id}")
            else:
                try:
                    from .notification_channels import get_email_channel
                    email_ch = get_email_channel()
                    logger.info(f"[EMAIL] Attempting to send | To: {user.email} | SMTP configured: {email_ch.is_configured()} | Host: {email_ch.smtp_host}")
                    if email_ch.is_configured():
                        subject, body, html_body, embedded_images = email_ch.format_notification(notification_dict)
                        result = await email_ch.send(user.email, subject, body, html_body, embedded_images)
                        if result:
                            logger.info(f"[EMAIL SUCCESS] Sent to {user.email} | Notification ID: {notification.id}")
                        else:
                            logger.error(f"[EMAIL FAILED] Could not send to {user.email} | Notification ID: {notification.id}")
                    else:
                        logger.warning(f"[EMAIL SKIPPED] SMTP not configured | Host: {email_ch.smtp_host} | User: {email_ch.smtp_user}")
                except Exception as e:
                    logger.error(f"[EMAIL ERROR] Failed to send to {user.email}: {e} | Notification ID: {notification.id}")
        else:
            if not prefs.email_enabled:
                logger.debug(f"[EMAIL SKIPPED] User {user_id}: email_enabled=False")
            elif not user or not user.email:
                logger.warning(f"[EMAIL SKIPPED] User {user_id}: no email address configured")
        
        # Send Telegram
        if prefs.telegram_enabled and prefs.telegram_chat_id:
            # Check critical only for Telegram too if set (bypass if force_send is True)
            if prefs.email_critical_only and level != "critical" and not force_send:
                logger.info(f"[TELEGRAM SKIPPED] User {user_id}: critical_only=True but level='{level}' | Notification ID: {notification.id}")
            else:
                try:
                    from .notification_channels import get_telegram_channel
                    tg_ch = get_telegram_channel()
                    logger.info(f"[TELEGRAM] Attempting to send | Chat ID: {prefs.telegram_chat_id} | Bot configured: {tg_ch.is_configured()}")
                    if tg_ch.is_configured():
                        tg_message, logo_bytes = tg_ch.format_notification(notification_dict)
                        if logo_bytes:
                            result = await tg_ch.send_photo(prefs.telegram_chat_id, logo_bytes, tg_message)
                        else:
                            result = await tg_ch.send(prefs.telegram_chat_id, tg_message)
                        if result:
                            logger.info(f"[TELEGRAM SUCCESS] Sent to {prefs.telegram_chat_id} | Notification ID: {notification.id}")
                        else:
                            logger.error(f"[TELEGRAM FAILED] Could not send to {prefs.telegram_chat_id} | Notification ID: {notification.id}")
                    else:
                        logger.warning(f"[TELEGRAM SKIPPED] Bot not configured")
                except Exception as e:
                    logger.error(f"[TELEGRAM ERROR] Failed to send to {prefs.telegram_chat_id}: {e} | Notification ID: {notification.id}")
        else:
            if not prefs.telegram_enabled:
                logger.debug(f"[TELEGRAM SKIPPED] User {user_id}: telegram_enabled=False")
            elif not prefs.telegram_chat_id:
                logger.warning(f"[TELEGRAM SKIPPED] User {user_id}: no telegram_chat_id configured")
        
        return notification

    @staticmethod
    async def send_test_notification(
        db: Session,
        user_id: int,
        channel: str = "all"
    ) -> Dict[str, Any]:
        """
        Send test notification through specified channels
        
        Args:
            db: Database session
            user_id: User ID
            channel: Channel to test (all, email, telegram, inapp)
            
        Returns:
            Dict with results for each channel
        """
        results = {"inapp": False, "email": False, "telegram": False, "errors": []}
        
        user = db.query(User).filter(User.id == user_id).first()
        prefs = NotificationService.get_user_preferences(db, user_id)
        
        # Get panel language
        lang = get_panel_language(db)
        
        # Use local time instead of UTC
        local_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        test_notification = {
            "title": t("notify_test_title", lang),
            "message": t("notify_test_message", lang),
            "level": "info",
            "source": "system",
            "created_at": local_time
        }
        
        # In-app notification
        if channel in ["all", "inapp"]:
            try:
                notification = NotificationService.create_notification(
                    db,
                    NotificationCreate(
                        user_id=user_id,
                        type="system",
                        level="info",
                        title=test_notification["title"],
                        message=test_notification["message"],
                        source="system"
                    )
                )
                results["inapp"] = notification is not None
            except Exception as e:
                results["errors"].append(f"In-app: {str(e)}")
        
        # Email
        if channel in ["all", "email"]:
            try:
                from .notification_channels import get_email_channel
                email_ch = get_email_channel()
                if email_ch.is_configured():
                    if user and user.email:
                        subject, body, html_body, embedded_images = email_ch.format_notification(test_notification)
                        results["email"] = await email_ch.send(user.email, subject, body, html_body, embedded_images)
                    else:
                        results["errors"].append("Email: User has no email address")
                else:
                    results["errors"].append("Email: SMTP not configured")
            except Exception as e:
                results["errors"].append(f"Email: {str(e)}")
        
        # Telegram
        if channel in ["all", "telegram"]:
            try:
                from .notification_channels import get_telegram_channel
                tg_ch = get_telegram_channel()
                if tg_ch.is_configured():
                    if prefs and prefs.telegram_chat_id:
                        tg_message, logo_bytes = tg_ch.format_notification(test_notification)
                        if logo_bytes:
                            results["telegram"] = await tg_ch.send_photo(prefs.telegram_chat_id, logo_bytes, tg_message)
                        else:
                            results["telegram"] = await tg_ch.send(prefs.telegram_chat_id, tg_message)
                    else:
                        results["errors"].append("Telegram: Chat ID not configured")
                else:
                    results["errors"].append("Telegram: Bot token not configured")
            except Exception as e:
                results["errors"].append(f"Telegram: {str(e)}")
        
        return results

    @staticmethod
    async def get_channels_status(db: Session, user_id: int) -> Dict[str, Any]:
        """
        Get status of notification channels
        
        Args:
            db: Database session
            user_id: User ID
            
        Returns:
            Dict with channel status information
        """
        from .notification_channels import get_email_channel, get_telegram_channel
        
        user = db.query(User).filter(User.id == user_id).first()
        prefs = NotificationService.get_user_preferences(db, user_id)
        
        email_ch = get_email_channel()
        tg_ch = get_telegram_channel()
        
        # Get Telegram bot info if configured
        telegram_bot_info = None
        if tg_ch.is_configured():
            try:
                telegram_bot_info = await tg_ch.get_bot_info()
            except Exception:
                pass
        
        return {
            "email": {
                "configured": email_ch.is_configured(),
                "enabled": prefs.email_enabled if prefs else False,
                "user_email": user.email if user else None,
                "smtp_host": email_ch.smtp_host if email_ch.is_configured() else None,
                "critical_only": prefs.email_critical_only if prefs else True
            },
            "telegram": {
                "configured": tg_ch.is_configured(),
                "enabled": prefs.telegram_enabled if prefs else False,
                "chat_id": prefs.telegram_chat_id if prefs else None,
                "bot_info": telegram_bot_info
            },
            "inapp": {
                "configured": True,
                "enabled": prefs.enabled if prefs else True
            },
            "quiet_hours": {
                "start": prefs.quiet_hours_start if prefs else None,
                "end": prefs.quiet_hours_end if prefs else None
            }
        }
