"""
Settings API endpoints
User account settings and panel configuration
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from ..db import get_db
from ..models import User, PanelSettings
from ..auth import (get_current_user, get_current_user_optional, get_password_hash, verify_password,
                    PermissionChecker, get_current_session_token)
from ..config import utcnow
from ..crypto import encrypt_value
from ..logging_service import LoggingService
from ..i18n import I18nService, t
from ..services.logo_service import LogoService
from ..services.update_service import (
    get_current_version, 
    check_for_updates, 
    perform_update, 
    get_update_status,
    reset_update_status
)


def get_local_time() -> str:
    """Get current local time formatted string"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


router = APIRouter()


# ==================== Helper Functions ====================

def get_setting(db: Session, key: str, default: str = None) -> str:
    """Get setting value from database"""
    setting = db.query(PanelSettings).filter(PanelSettings.key == key).first()
    if setting:
        return setting.value
    return default


def set_setting(db: Session, key: str, value: str, description: str = None):
    """Set setting value in database"""
    setting = db.query(PanelSettings).filter(PanelSettings.key == key).first()
    if setting:
        setting.value = value
        if description:
            setting.description = description
    else:
        setting = PanelSettings(key=key, value=value, description=description)
        db.add(setting)
    db.commit()


# ==================== Schemas ====================

class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = Field(None, max_length=100)
    email: Optional[EmailStr] = None
    ssh_public_key: Optional[str] = Field(None, max_length=10000, description="SSH public key for VM/LXC deployment")
    language: Optional[str] = Field(None, pattern='^(ru|en)$')


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)
    confirm_password: str = Field(..., min_length=6)


class PanelSettingsResponse(BaseModel):
    panel_name: str
    log_retention_days: int
    language: str  # ru or en


class UpdatePanelSettingsRequest(BaseModel):
    panel_name: Optional[str] = Field(None, min_length=1, max_length=100)
    log_retention_days: Optional[int] = Field(None, ge=1, le=365)
    language: Optional[str] = Field(None, pattern='^(ru|en)$')


# ==================== Logo Protection ====================

@router.get("/api/logo.png")
async def get_protected_logo():
    """
    Get protected logo image.
    Returns the verified original logo or a fallback if tampered.
    """
    logo_bytes = LogoService.get_logo_bytes()
    return Response(
        content=logo_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Logo-Protected": "true"
        }
    )


# ==================== User Account Settings ====================

@router.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_current_user)
):
    """Get current user profile"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_admin": current_user.is_admin,
        "is_active": current_user.is_active,
        "ssh_public_key": current_user.ssh_public_key,
        "language": current_user.language or 'ru',
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        "last_login": current_user.last_login.isoformat() if current_user.last_login else None
    }


@router.get("/api/quota")
async def get_my_quota(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the current user's own resource quota and usage (self-service)."""
    from ..models import UserQuota
    from ..services.quota_service import get_user_usage

    quota = db.query(UserQuota).filter(UserQuota.user_id == current_user.id).first()
    usage = get_user_usage(db, current_user.id)
    return {
        "user_id": current_user.id,
        "max_instances": quota.max_instances if quota else None,
        "max_cores": quota.max_cores if quota else None,
        "max_memory_mb": quota.max_memory_mb if quota else None,
        "max_disk_gb": quota.max_disk_gb if quota else None,
        "used_instances": usage["instances"],
        "used_cores": usage["cores"],
        "used_memory_mb": usage["memory_mb"],
        "used_disk_gb": usage["disk_gb"],
    }


@router.put("/api/profile")
async def update_profile(
    data: UpdateProfileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update user profile"""
    updated_fields = []
    
    if data.full_name is not None:
        current_user.full_name = data.full_name
        updated_fields.append("full_name")
    
    if data.email is not None:
        # Check if email is already taken by another user
        existing = db.query(User).filter(
            User.email == data.email,
            User.id != current_user.id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email уже используется другим пользователем"
            )
        current_user.email = data.email
        updated_fields.append("email")
    
    if data.ssh_public_key is not None:
        # Validate SSH key format (basic validation)
        ssh_key = data.ssh_public_key.strip()
        if "\n" in ssh_key or "\r" in ssh_key:
            # Written verbatim into authorized_keys / cloud-init: one key per line.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SSH-ключ должен быть одной строкой"
            )
        if ssh_key and not ssh_key.startswith(('ssh-rsa', 'ssh-ed25519', 'ecdsa-sha2', 'ssh-dss')):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Неверный формат SSH ключа. Ключ должен начинаться с ssh-rsa, ssh-ed25519, ecdsa-sha2 или ssh-dss"
            )
        current_user.ssh_public_key = ssh_key if ssh_key else None
        updated_fields.append("ssh_public_key")

    if data.language is not None:
        current_user.language = data.language
        updated_fields.append("language")
    
    if updated_fields:
        db.commit()
        db.refresh(current_user)
        
        # Log profile update
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.AUTH,
            action="profile_update",
            message=f"Пользователь {current_user.username} обновил профиль: {', '.join(updated_fields)}",
            username=current_user.username,
            user_id=current_user.id
        )
    
    return {
        "message": "Профиль обновлён",
        "updated_fields": updated_fields
    }


@router.post("/api/change-password")
async def change_password(
    data: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    session_token: str = Depends(get_current_session_token),
):
    """Change user password (the profile page uses this endpoint)"""
    
    # Verify current password
    if not verify_password(data.current_password, current_user.hashed_password):
        LoggingService.log(
            db=db,
            level=LoggingService.WARNING,
            category=LoggingService.AUTH,
            action="password_change_failed",
            message=f"Неудачная попытка смены пароля (неверный текущий пароль)",
            username=current_user.username,
            user_id=current_user.id,
            success=False
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Неверный текущий пароль"
        )
    
    # Check new password confirmation
    if data.new_password != data.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Новые пароли не совпадают"
        )
    
    # Same password policy as every other password-setting path — this one
    # used to accept anything of 6+ characters.
    from ..services.security_service import SecurityService
    is_valid, errors = SecurityService.validate_password(
        db, data.new_password, request.cookies.get("language") or current_user.language or "en"
    )
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="; ".join(errors))

    # Update password
    current_user.hashed_password = get_password_hash(data.new_password)
    current_user.last_password_change = utcnow()
    current_user.require_password_change = False
    db.commit()

    # End every other session: whoever may have used the old password is out.
    SecurityService.terminate_all_user_sessions(db, current_user.id, except_token=session_token)
    
    # Log password change
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.AUTH,
        action="password_change",
        message=f"Пользователь {current_user.username} изменил пароль",
        username=current_user.username,
        user_id=current_user.id,
        success=True
    )
    
    return {"message": "Пароль успешно изменён"}


# ==================== Panel Settings ====================

@router.get("/api/panel", response_model=PanelSettingsResponse)
async def get_panel_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:view"))
):
    """Get panel settings"""
    from ..config import settings
    
    # Get settings from database or use defaults
    panel_name = get_setting(db, "panel_name", settings.PANEL_NAME)
    log_retention_days = get_setting(db, "log_retention_days", "30")
    language = get_setting(db, "language", "ru")

    return {
        "panel_name": panel_name,
        "log_retention_days": int(log_retention_days),
        "language": language
    }


@router.put("/api/panel")
async def update_panel_settings(
    data: UpdatePanelSettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:update"))
):
    """Update panel settings (requires settings.panel permission)"""
    updated_fields = []
    
    # Save settings to database
    if data.panel_name is not None:
        set_setting(db, "panel_name", data.panel_name, "Название панели")
        updated_fields.append(f"panel_name={data.panel_name}")

    if data.log_retention_days is not None:
        set_setting(db, "log_retention_days", str(data.log_retention_days), "Срок хранения логов (дни)")
        updated_fields.append(f"log_retention_days={data.log_retention_days}")
    
    if data.language is not None:
        set_setting(db, "language", data.language, "Язык интерфейса панели")
        updated_fields.append(f"language={data.language}")
    
    if updated_fields:
        # Log settings change
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.SYSTEM,
            action="panel_settings_update",
            message=f"Администратор {current_user.username} обновил настройки панели: {', '.join(updated_fields)}",
            username=current_user.username,
            user_id=current_user.id,
            details=data.dict(exclude_none=True)
        )
    
    return {
        "message": "Настройки панели обновлены",
        "updated_fields": updated_fields
    }


@router.post("/api/cleanup-logs")
async def cleanup_old_logs(
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Manually cleanup old logs (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут очищать логи"
        )
    
    deleted = LoggingService.cleanup_old_logs(db, days=days)
    
    # Log the cleanup action
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.SYSTEM,
        action="logs_cleanup",
        message=f"Администратор {current_user.username} очистил {deleted} логов старше {days} дней",
        username=current_user.username,
        user_id=current_user.id,
        details={"deleted_count": deleted, "older_than_days": days}
    )
    
    return {
        "message": f"Удалено {deleted} логов старше {days} дней",
        "deleted": deleted
    }


# ==================== Translations API ====================

@router.get("/api/translations/{lang}")
async def get_translations(lang: str):
    """Get all translations for specific language"""
    if lang not in ["ru", "en"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported language. Use 'ru' or 'en'"
        )
    
    return I18nService.get_all(lang)


# ==================== Security Settings API ====================

class SecuritySettingsRequest(BaseModel):
    single_session_enabled: Optional[bool] = None


@router.get("/api/security-settings")
async def get_security_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get security settings (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут просматривать настройки безопасности"
        )
    
    from ..services.security_service import SecurityService
    
    return {
        "single_session_enabled": SecurityService.get_setting_bool(db, "single_session_enabled", False)
    }


@router.put("/api/security-settings")
async def update_security_settings(
    data: SecuritySettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update security settings (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут изменять настройки безопасности"
        )
    
    from ..services.security_service import SecurityService
    
    updated_fields = []
    
    if data.single_session_enabled is not None:
        SecurityService.update_setting(db, "single_session_enabled", str(data.single_session_enabled).lower())
        updated_fields.append(f"single_session_enabled={data.single_session_enabled}")
    
    if updated_fields:
        # Log settings change
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.AUTH,
            action="security_settings_update",
            message=f"Администратор {current_user.username} обновил настройки безопасности: {', '.join(updated_fields)}",
            username=current_user.username,
            user_id=current_user.id,
            details=data.model_dump(exclude_none=True)
        )
    
    return {
        "message": "Настройки безопасности обновлены",
        "updated_fields": updated_fields
    }


# ==================== Notification Channel Settings API ====================

class SMTPSettingsRequest(BaseModel):
    host: Optional[str] = Field(None, max_length=255)
    port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = Field(None, max_length=255)
    password: Optional[str] = Field(None, max_length=255)
    from_email: Optional[str] = Field(None, max_length=255)
    tls: Optional[bool] = None


class TelegramSettingsRequest(BaseModel):
    telegram_bot_token: Optional[str] = Field(None, max_length=255)


@router.get("/api/notification-channels")
async def get_notification_channel_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get notification channel settings (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут просматривать настройки уведомлений"
        )
    
    return {
        "smtp": {
            "host": get_setting(db, "smtp_host", ""),
            "port": int(get_setting(db, "smtp_port", "587") or "587"),
            "username": get_setting(db, "smtp_user", ""),
            "password": "***" if get_setting(db, "smtp_password") else "",
            "from_email": get_setting(db, "smtp_from", ""),
            "tls": get_setting(db, "smtp_tls", "true").lower() == "true"
        },
        "telegram": {
            "bot_token": "***" if get_setting(db, "telegram_bot_token") else "",
            "configured": bool(get_setting(db, "telegram_bot_token"))
        }
    }


@router.put("/api/notification-channels/smtp")
async def update_smtp_settings(
    data: SMTPSettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update SMTP settings (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут изменять настройки SMTP"
        )
    
    updated_fields = []

    if data.host is not None:
        set_setting(db, "smtp_host", data.host, "SMTP сервер")
        updated_fields.append("smtp_host")

    if data.port is not None:
        set_setting(db, "smtp_port", str(data.port), "SMTP порт")
        updated_fields.append("smtp_port")

    if data.username is not None:
        set_setting(db, "smtp_user", data.username, "SMTP пользователь")
        updated_fields.append("smtp_user")

    if data.password is not None:
        # Encrypted at rest like server passwords (read back in notification_channels).
        set_setting(db, "smtp_password", encrypt_value(data.password), "SMTP пароль")
        updated_fields.append("smtp_password")

    if data.from_email is not None:
        set_setting(db, "smtp_from", data.from_email, "Email отправителя")
        updated_fields.append("smtp_from")

    if data.tls is not None:
        set_setting(db, "smtp_tls", str(data.tls).lower(), "Использовать TLS")
        updated_fields.append("smtp_tls")
    
    if updated_fields:
        # Reload notification channels
        from ..services.notification_channels import reload_notification_channels
        reload_notification_channels()
        
        # Log settings change
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.SYSTEM,
            action="smtp_settings_update",
            message=f"Администратор {current_user.username} обновил настройки SMTP: {', '.join(updated_fields)}",
            username=current_user.username,
            user_id=current_user.id
        )
    
    return {
        "message": "Настройки SMTP обновлены",
        "updated_fields": updated_fields
    }


@router.put("/api/notification-channels/telegram")
async def update_telegram_settings(
    data: TelegramSettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update Telegram bot settings (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут изменять настройки Telegram"
        )
    
    updated_fields = []
    
    if data.telegram_bot_token is not None:
        set_setting(db, "telegram_bot_token", encrypt_value(data.telegram_bot_token), "Токен Telegram бота")
        updated_fields.append("telegram_bot_token")
    
    if updated_fields:
        # Reload notification channels
        from ..services.notification_channels import reload_notification_channels
        reload_notification_channels()
        
        # Log settings change
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.SYSTEM,
            action="telegram_settings_update",
            message=f"Администратор {current_user.username} обновил настройки Telegram",
            username=current_user.username,
            user_id=current_user.id
        )
    
    return {
        "message": "Настройки Telegram обновлены",
        "updated_fields": updated_fields
    }


@router.post("/api/notification-channels/smtp/test")
async def test_smtp_connection(
    test_email: str = Query(..., description="Email для отправки тестового письма"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Test SMTP connection by sending a test email (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут тестировать SMTP"
        )
    
    from ..services.notification_channels import get_email_channel
    
    email_channel = get_email_channel()
    if not email_channel.is_configured():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP не настроен. Заполните все необходимые поля."
        )
    
    success = await email_channel.send(
        to_email=test_email,
        subject="🔧 Тестовое письмо - PVEmanager",
        body=f"Тестовое письмо от PVEmanager.\n\nВремя: {get_local_time()}\nОтправлено пользователем: {current_user.username}",
        html_body=f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a2e; color: #fff;">
            <h2 style="color: #667eea;">🔧 Тестовое письмо</h2>
            <p>SMTP настройки работают корректно!</p>
            <p>Время: {get_local_time()}</p>
            <p>Отправлено пользователем: {current_user.username}</p>
            <hr style="border-color: #333;">
            <p style="color: #888; font-size: 12px;">PVEmanager</p>
        </div>
        """
    )
    
    if success:
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.SYSTEM,
            action="smtp_test",
            message=f"Администратор {current_user.username} отправил тестовое письмо на {test_email}",
            username=current_user.username,
            user_id=current_user.id
        )
        return {"success": True, "message": f"Тестовое письмо отправлено на {test_email}"}
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось отправить тестовое письмо. Проверьте настройки SMTP."
        )


@router.post("/api/notification-channels/telegram/test")
async def test_telegram_bot(
    chat_id: str = Query(..., description="Telegram Chat ID для отправки тестового сообщения"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Test Telegram bot by sending a test message (admin only)"""
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Только администраторы могут тестировать Telegram бота"
        )
    
    from ..services.notification_channels import get_telegram_channel, get_local_time
    
    tg_channel = get_telegram_channel()
    if not tg_channel.is_configured():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telegram бот не настроен. Укажите токен бота."
        )
    
    success = await tg_channel.send(
        chat_id=chat_id,
        message=f"🔧 <b>Тестовое сообщение</b>\n\nTelegram бот работает корректно!\n\n🕐 Время: {get_local_time()}\n👤 Отправлено: {current_user.username}\n\n🖥️ <i>PVEmanager</i>",
        parse_mode="HTML"
    )
    
    if success:
        LoggingService.log(
            db=db,
            level=LoggingService.INFO,
            category=LoggingService.SYSTEM,
            action="telegram_test",
            message=f"Администратор {current_user.username} отправил тестовое сообщение в Telegram (chat_id: {chat_id})",
            username=current_user.username,
            user_id=current_user.id
        )
        return {"success": True, "message": f"Тестовое сообщение отправлено в Telegram (chat_id: {chat_id})"}
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось отправить тестовое сообщение. Проверьте токен бота и chat_id."
        )


# ==================== System Updates ====================

@router.get("/api/version")
async def get_version():
    """Get current panel version"""
    return {"version": get_current_version()}


@router.get("/api/updates/repository")
async def get_repository_url(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Get configured git repository URL"""
    setting = db.query(PanelSettings).filter(PanelSettings.key == "git_repository_url").first()
    if setting:
        return {"repository_url": setting.value}
    return {"repository_url": "https://git.tzim.uz/markmorado/pvemanager"}


@router.put("/api/updates/repository")
async def set_repository_url(
    repository_url: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Set git repository URL for updates"""
    # The update pulls, builds and runs this code as root on the host: over
    # plain http anyone on the path could substitute it.
    if not repository_url.startswith('https://'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository URL must start with https://"
        )
    
    set_setting(db, "git_repository_url", repository_url, "Git repository URL for updates")
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.SYSTEM,
        action="update_repository_changed",
        message=f"Administrator {current_user.username} changed update repository to: {repository_url}",
        username=current_user.username,
        user_id=current_user.id
    )
    
    return {"message": "Repository URL updated", "repository_url": repository_url}


@router.get("/api/updates/check")
async def check_updates(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Check for available updates (admin only)"""
    result = await check_for_updates()
    
    LoggingService.log(
        db=db,
        level=LoggingService.INFO,
        category=LoggingService.SYSTEM,
        action="update_check",
        message=f"Проверка обновлений: текущая {result['current_version']}, последняя {result.get('latest_version', 'unknown')}",
        username=current_user.username,
        user_id=current_user.id
    )
    
    return result


@router.get("/api/updates/status")
async def updates_status():
    """Get current update status (no auth required for banner check)"""
    return get_update_status()


@router.post("/api/updates/perform")
async def perform_system_update(
    db: Session = Depends(get_db),
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Perform system update (admin only)"""
    
    LoggingService.log(
        db=db,
        level=LoggingService.WARNING,
        category=LoggingService.SYSTEM,
        action="update_start",
        message=f"Администратор {current_user.username} запустил обновление системы",
        username=current_user.username,
        user_id=current_user.id
    )
    
    result = await perform_update()
    
    if not result["success"]:
        LoggingService.log(
            db=db,
            level=LoggingService.ERROR,
            category=LoggingService.SYSTEM,
            action="update_failed",
            message=f"Ошибка обновления: {result.get('error')}",
            username=current_user.username,
            user_id=current_user.id
        )
    
    return result


@router.post("/api/updates/reset")
async def reset_update(
    current_user: User = Depends(PermissionChecker("setting:manage"))
):
    """Reset update status (admin only)"""
    reset_update_status()
    return {"success": True, "message": "Update status reset"}

