"""
Notification models for SQLAlchemy
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.sql import func
from backend.app.db import Base


class Notification(Base):
    """Notification model"""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(50), nullable=False)  # vm_status, resource_alert, system, etc
    level = Column(String(20), nullable=False)  # critical, warning, info, success
    title = Column(String(255), nullable=False)
    message = Column(Text)
    data = Column(JSON)  # Additional structured data
    link = Column(String(500))  # Deep link to related resource
    source = Column(String(50))  # proxmox, ipam, system, docker
    source_id = Column(String(100))  # VM ID, Network ID, etc
    read = Column(Boolean, default=False)
    read_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))  # Auto-delete old notifications

    def to_dict(self):
        """Convert to dictionary"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type,
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "data": self.data,
            "link": self.link,
            "source": self.source,
            "source_id": self.source_id,
            "read": self.read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class NotificationPreference(Base):
    """User notification preferences"""
    __tablename__ = "notification_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    email_enabled = Column(Boolean, default=False)
    email_critical_only = Column(Boolean, default=True)
    telegram_enabled = Column(Boolean, default=False)
    telegram_chat_id = Column(String(100))
    webhook_url = Column(String(500))
    notification_levels = Column(JSON, default=["critical", "warning", "info", "success"])
    notification_types = Column(JSON, default=["vm_status", "resource_alert", "system", "update"])
    quiet_hours_start = Column(String(5))  # HH:MM format
    quiet_hours_end = Column(String(5))  # HH:MM format
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self):
        """Convert to dictionary"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "email_enabled": self.email_enabled,
            "email_critical_only": self.email_critical_only,
            "telegram_enabled": self.telegram_enabled,
            "telegram_chat_id": self.telegram_chat_id,
            "webhook_url": self.webhook_url,
            "notification_levels": self.notification_levels,
            "notification_types": self.notification_types,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
