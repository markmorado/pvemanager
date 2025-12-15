"""
Notification Channels - Email (SMTP) and Telegram Bot
Settings are loaded from database (panel_settings table)
"""

import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from typing import Optional, Dict, Any, Tuple
from datetime import datetime
from loguru import logger

try:
    import httpx
except ImportError:
    httpx = None


def get_local_time() -> str:
    """Get current local time formatted string"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_notification_setting(key: str, default: str = None) -> Optional[str]:
    """Get notification setting from database"""
    from ..db import SessionLocal
    from ..models import PanelSettings
    
    db = SessionLocal()
    try:
        setting = db.query(PanelSettings).filter(PanelSettings.key == key).first()
        return setting.value if setting else default
    finally:
        db.close()


def get_panel_name() -> str:
    """Get panel name from database"""
    return get_notification_setting('panel_name', 'PVEmanager') or 'PVEmanager'


def get_logo_base64() -> Optional[str]:
    """Get logo as base64 for email embedding"""
    import base64
    import os
    
    logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'img', 'logo.png')
    
    if os.path.exists(logo_path):
        try:
            with open(logo_path, 'rb') as f:
                logo_data = f.read()
            return base64.b64encode(logo_data).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to read logo: {e}")
    return None


def get_logo_bytes() -> Optional[bytes]:
    """Get logo as bytes for email attachment"""
    import os
    
    logo_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'img', 'logo.png')
    
    if os.path.exists(logo_path):
        try:
            with open(logo_path, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Failed to read logo: {e}")
    return None


class EmailChannel:
    """SMTP Email notification channel - settings from database"""
    
    def __init__(self):
        self._load_settings()
    
    def _load_settings(self):
        """Load SMTP settings from database"""
        self.smtp_host = get_notification_setting('smtp_host')
        self.smtp_port = int(get_notification_setting('smtp_port', '587') or '587')
        self.smtp_user = get_notification_setting('smtp_user')
        self.smtp_password = get_notification_setting('smtp_password')
        self.smtp_from = get_notification_setting('smtp_from') or self.smtp_user
        self.smtp_tls = get_notification_setting('smtp_tls', 'true').lower() == 'true'
        self.enabled = bool(self.smtp_host and self.smtp_user and self.smtp_password)
    
    def reload_settings(self):
        """Reload settings from database"""
        self._load_settings()
    
    def is_configured(self) -> bool:
        """Check if SMTP is configured"""
        self._load_settings()  # Always reload to get latest settings
        return self.enabled
    
    async def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        embedded_images: Optional[Dict[str, bytes]] = None
    ) -> bool:
        """Send email notification
        
        Args:
            to_email: Recipient email
            subject: Email subject
            body: Plain text body
            html_body: HTML body (optional)
            embedded_images: Dict of {cid: image_bytes} for embedding (optional)
        """
        if not self.is_configured():
            logger.warning("SMTP not configured, skipping email notification")
            return False
        
        try:
            # Create message - use 'related' for embedded images
            if embedded_images:
                msg = MIMEMultipart("related")
                msg_alt = MIMEMultipart("alternative")
                msg.attach(msg_alt)
            else:
                msg = MIMEMultipart("alternative")
                msg_alt = msg
            
            msg["Subject"] = subject
            msg["From"] = self.smtp_from
            msg["To"] = to_email
            
            # Add plain text
            msg_alt.attach(MIMEText(body, "plain", "utf-8"))
            
            # Add HTML if provided
            if html_body:
                msg_alt.attach(MIMEText(html_body, "html", "utf-8"))
            
            # Attach embedded images
            if embedded_images:
                for cid, image_data in embedded_images.items():
                    img = MIMEImage(image_data)
                    img.add_header('Content-ID', f'<{cid}>')
                    img.add_header('Content-Disposition', 'inline', filename=f'{cid}.png')
                    msg.attach(img)
            
            # Send in thread pool to not block async
            # Use asyncio.to_thread for better compatibility with FastAPI
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._send_sync, to_email, msg),
                    timeout=15.0  # 15 second timeout for entire operation
                )
            except asyncio.TimeoutError:
                logger.error(f"Email sending to {to_email} timed out after 15 seconds")
                return False
            
            logger.info(f"Email notification sent to {to_email}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False
    
    def _send_sync(self, to_email: str, msg: MIMEMultipart):
        """Synchronous email sending with timeout"""
        context = ssl.create_default_context()
        timeout = 10  # 10 seconds timeout
        
        # Port 465 always uses SSL (SMTPS)
        # Port 587 uses STARTTLS (SMTP with upgrade)
        # Port 25 uses plain SMTP (or STARTTLS if smtp_tls=True)
        use_ssl = self.smtp_port == 465
        
        logger.info(f"Sending email via {self.smtp_host}:{self.smtp_port} (SSL={use_ssl}, TLS={self.smtp_tls})")
        
        if use_ssl:
            # SSL connection (port 465 - SMTPS)
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=timeout) as server:
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_from, to_email, msg.as_string())
        else:
            # STARTTLS connection (port 587, 25, or custom)
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=timeout) as server:
                if self.smtp_tls:
                    server.starttls(context=context)
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_from, to_email, msg.as_string())
    
    def format_notification(self, notification: Dict[str, Any]) -> tuple:
        """Format notification for email - returns (subject, body, html_body, embedded_images)"""
        level_emoji = {
            "critical": "🔴",
            "warning": "🟠",
            "info": "🔵",
            "success": "🟢"
        }
        
        level = notification.get("level", "info")
        emoji = level_emoji.get(level, "🔵")
        title = notification.get("title", "Notification")
        message = notification.get("message", "")
        source = notification.get("source", "system")
        created_at = notification.get("created_at", "now")
        
        # Получаем имя панели
        panel_name = get_panel_name()
        
        subject = f"{emoji} [{panel_name}] {title}"
        
        body = f"""
{title}

{message}

Level: {level.upper()}
Source: {source}
Time: {created_at}

---
{panel_name}
        """.strip()
        
        # Получаем логотип для встраивания
        logo_bytes = get_logo_bytes()
        embedded_images = {}
        
        if logo_bytes:
            embedded_images['logo'] = logo_bytes
            logo_html = f'<img src="cid:logo" alt="{panel_name}" style="max-height: 128px;">'
        else:
            logo_html = f'<span style="font-size: 64px;">🖥️</span>'
        
        html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; color: #eee; padding: 20px; margin: 0; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #16213e; border-radius: 12px; overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #4f46e5 100%); padding: 24px; text-align: center; }}
        .logo {{ display: flex; align-items: center; justify-content: center; }}
        .content {{ padding: 24px; }}
        .level {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; margin-bottom: 16px; }}
        .level.critical {{ background: #f44336; color: white; }}
        .level.warning {{ background: #ff9800; color: white; }}
        .level.info {{ background: #2196f3; color: white; }}
        .level.success {{ background: #4caf50; color: white; }}
        .title {{ font-size: 20px; font-weight: bold; margin-bottom: 12px; color: #fff; }}
        .message {{ color: #b0b0b0; line-height: 1.6; margin-bottom: 20px; }}
        .meta {{ color: #666; font-size: 12px; }}
        .footer {{ padding: 16px 24px; background: #0f0f23; color: #666; font-size: 12px; text-align: center; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">{logo_html}</div>
        </div>
        <div class="content">
            <span class="level {level}">{level.upper()}</span>
            <div class="title">{emoji} {title}</div>
            <div class="message">{message}</div>
            <div class="meta">
                Source: {source} | Time: {created_at}
            </div>
        </div>
        <div class="footer">
            {panel_name} Notifications
        </div>
    </div>
</body>
</html>
        """
        
        return subject, body, html_body, embedded_images


class TelegramChannel:
    """Telegram Bot notification channel - settings from database"""
    
    def __init__(self):
        self._load_settings()
    
    def _load_settings(self):
        """Load Telegram settings from database"""
        self.bot_token = get_notification_setting('telegram_bot_token')
        self.enabled = bool(self.bot_token)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
    
    def reload_settings(self):
        """Reload settings from database"""
        self._load_settings()
    
    def is_configured(self) -> bool:
        """Check if Telegram bot is configured"""
        self._load_settings()  # Always reload to get latest settings
        return self.enabled
    
    async def send(self, chat_id: str, message: str, parse_mode: str = "HTML") -> bool:
        """Send Telegram message"""
        if not self.is_configured():
            logger.warning("Telegram bot not configured, skipping notification")
            return False
        
        if httpx is None:
            logger.error("httpx library not installed")
            return False
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True
                    },
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    logger.info(f"Telegram notification sent to {chat_id}")
                    return True
                else:
                    logger.error(f"Telegram API error: {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
            return False
    
    async def send_photo(self, chat_id: str, photo_bytes: bytes, caption: str, parse_mode: str = "HTML") -> bool:
        """Send Telegram message with photo"""
        if not self.is_configured():
            logger.warning("Telegram bot not configured, skipping notification")
            return False
        
        if httpx is None:
            logger.error("httpx library not installed")
            return False
        
        try:
            async with httpx.AsyncClient() as client:
                files = {'photo': ('logo.png', photo_bytes, 'image/png')}
                data = {
                    'chat_id': chat_id,
                    'caption': caption,
                    'parse_mode': parse_mode
                }
                response = await client.post(
                    f"{self.api_url}/sendPhoto",
                    data=data,
                    files=files,
                    timeout=15.0
                )
                
                if response.status_code == 200:
                    logger.info(f"Telegram photo notification sent to {chat_id}")
                    return True
                else:
                    logger.error(f"Telegram API error: {response.text}")
                    # Fallback to text message
                    return await self.send(chat_id, caption, parse_mode)
                    
        except Exception as e:
            logger.error(f"Failed to send Telegram photo to {chat_id}: {e}")
            # Fallback to text message
            return await self.send(chat_id, caption, parse_mode)
    
    async def get_bot_info(self) -> Optional[Dict[str, Any]]:
        """Get bot information"""
        if not self.is_configured() or httpx is None:
            return None
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.api_url}/getMe", timeout=10.0)
                if response.status_code == 200:
                    return response.json().get("result")
        except Exception as e:
            logger.error(f"Failed to get bot info: {e}")
        return None
    
    async def verify_chat_id(self, chat_id: str) -> bool:
        """Verify chat_id by sending a test message"""
        local_time = get_local_time()
        panel_name = get_panel_name()
        return await self.send(
            chat_id,
            f"✅ <b>{panel_name}</b>\n\n"
            f"Telegram notifications configured successfully!\n\n"
            f"🕐 Server time: {local_time}",
            "HTML"
        )
    
    def format_notification(self, notification: Dict[str, Any]) -> Tuple[str, Optional[bytes]]:
        """Format notification for Telegram - returns (message, logo_bytes)"""
        level_emoji = {
            "critical": "🔴",
            "warning": "🟠", 
            "info": "🔵",
            "success": "🟢"
        }
        
        level = notification.get("level", "info")
        emoji = level_emoji.get(level, "🔵")
        title = notification.get("title", "Notification")
        message = notification.get("message", "")
        source = notification.get("source", "system")
        created_at = notification.get("created_at", "now")
        
        # Получаем имя панели
        panel_name = get_panel_name()
        
        # Получаем логотип
        logo_bytes = get_logo_bytes()
        
        text = f"""
{emoji} <b>[{panel_name}] {title}</b>

{message}

<i>Level:</i> {level.upper()}
<i>Source:</i> {source}
<i>Time:</i> {created_at}
        """.strip()
        
        return text, logo_bytes


# Singleton instances - initialized lazily
_email_channel: Optional[EmailChannel] = None
_telegram_channel: Optional[TelegramChannel] = None


def get_email_channel() -> EmailChannel:
    """Get or create EmailChannel singleton"""
    global _email_channel
    if _email_channel is None:
        _email_channel = EmailChannel()
    else:
        _email_channel.reload_settings()
    return _email_channel


def get_telegram_channel() -> TelegramChannel:
    """Get or create TelegramChannel singleton"""
    global _telegram_channel
    if _telegram_channel is None:
        _telegram_channel = TelegramChannel()
    else:
        _telegram_channel.reload_settings()
    return _telegram_channel


def reload_notification_channels():
    """Force reload all notification channel settings"""
    global _email_channel, _telegram_channel
    if _email_channel:
        _email_channel.reload_settings()
    if _telegram_channel:
        _telegram_channel.reload_settings()
