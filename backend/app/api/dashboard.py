from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ProxmoxServer
from ..template_helpers import add_i18n_context

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_db)):
    total_servers = db.query(ProxmoxServer).count()
    online_servers = db.query(ProxmoxServer).filter(ProxmoxServer.is_online == True).count()
    offline_servers = db.query(ProxmoxServer).filter(ProxmoxServer.is_online == False).count()
    
    recent_servers = db.query(ProxmoxServer).order_by(ProxmoxServer.id.desc()).limit(6).all()
    
    stats = {
        "total_servers": total_servers,
        "online_servers": online_servers,
        "offline_servers": offline_servers,
    }
    
    lang = request.cookies.get("language", "en")
    from ..i18n import t
    
    context = {
        "request": request,
        "stats": stats,
        "recent_servers": recent_servers,
        "page_title": t('nav_dashboard', lang),
    }
    context = add_i18n_context(request, context)
    
    return templates.TemplateResponse("dashboard.html", context)


@router.get("/containers", response_class=HTMLResponse, include_in_schema=False)
def containers(request: Request):
    lang = request.cookies.get("language", "en")
    from ..i18n import t
    
    context = {
        "request": request,
        "page_title": t('nav_docker', lang),
    }
    context = add_i18n_context(request, context)
    return templates.TemplateResponse("containers.html", context)
