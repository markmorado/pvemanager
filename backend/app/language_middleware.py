"""
Language middleware
Detects user language from settings and adds to request state
"""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import PanelSettings


class LanguageMiddleware(BaseHTTPMiddleware):
    """Middleware to detect and set language"""
    
    async def dispatch(self, request: Request, call_next):
        # Get language from database settings
        lang = "ru"  # default
        
        try:
            db = SessionLocal()
            setting = db.query(PanelSettings).filter(PanelSettings.key == "language").first()
            if setting:
                lang = setting.value
            db.close()
        except Exception:
            pass
        
        # Store language in request state
        request.state.lang = lang
        
        response = await call_next(request)
        return response
