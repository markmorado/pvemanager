"""
Template helpers
Jinja2 context processors and filters
"""

import os
from fastapi import Request
from .i18n import I18nService
from .config import settings


# Path to custom logo
CUSTOM_LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "img", "logo.png")


def get_language(request: Request) -> str:
    """Get current language from request state"""
    return getattr(request.state, 'lang', 'ru')


def custom_logo_exists() -> bool:
    """Check if custom logo file exists"""
    return os.path.isfile(CUSTOM_LOGO_PATH)


def add_i18n_context(request: Request, context: dict) -> dict:
    """
    Add i18n variables to template context
    
    Args:
        request: FastAPI request
        context: Existing template context
    
    Returns:
        Updated context with i18n variables
    """
    lang = get_language(request)
    
    # Add language info
    context['lang'] = lang
    context['translations'] = I18nService.get_all(lang)
    context['t'] = lambda key, **kwargs: I18nService.get(key, lang, **kwargs)
    context['_'] = context['t']  # Alias for templates using _()
    
    # Add timezone
    context['timezone'] = settings.TZ
    
    # Add panel name and custom logo check
    context['panel_name'] = settings.PANEL_NAME
    context['custom_logo_exists'] = custom_logo_exists()
    
    return context
