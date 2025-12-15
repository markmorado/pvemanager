import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from loguru import logger

from .config import settings
from .db import Base, engine, init_db, check_db_connection
from .api import dashboard as dashboard_router
from .api import proxmox as proxmox_router
from .api import auth as auth_router
from .api import templates as templates_router
from .api import ipam as ipam_router
from .api import logs as logs_router
from .api import settings as settings_router
from .api import notifications as notifications_router
from .api import users as users_router
from .logging_middleware import RequestLoggingMiddleware
from .language_middleware import LanguageMiddleware
from .i18n import I18nService
from .template_helpers import add_i18n_context


# Configure logging
def setup_logging():
    """Setup application logging"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Remove default logger
    logger.remove()
    
    # Add console logging
    logger.add(
        sink=lambda msg: print(msg, end=""),
        level=settings.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    
    # Add file logging
    logger.add(
        settings.LOG_FILE,
        level=settings.LOG_LEVEL,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="30 days",
        compression="zip"
    )
    
    # Intercept standard logging
    class InterceptHandler(logging.Handler):
        def emit(self, record):
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            
            frame, depth = logging.currentframe(), 2
            while frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            
            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
    
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    logger.info(f"Starting {settings.PANEL_NAME}")
    
    # Setup logging
    setup_logging()
    
    # Check database connection
    if not check_db_connection():
        logger.error("Database connection failed!")
        raise Exception("Cannot connect to database")
    
    # Initialize database
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    
    # Run database migrations automatically
    try:
        import sys
        from pathlib import Path
        import importlib.util
        
        # Add migrations directory to path
        migrations_path = Path(__file__).parent.parent / 'migrations'
        sys.path.insert(0, str(migrations_path))
        
        from .db import engine
        
        # Load and run unified migrations
        spec = importlib.util.spec_from_file_location(
            "migrations", 
            migrations_path / "migrations.py"
        )
        migrations_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrations_module)
        migrations_module.run_all_migrations(engine)
        logger.info("Database migrations completed")
        
    except Exception as e:
        logger.warning(f"Migration check failed: {e}")
    
    # Start background monitoring worker
    try:
        from .workers.monitoring_worker import start_monitoring_worker
        scheduler = start_monitoring_worker()
        logger.info("Background monitoring worker started")
    except Exception as e:
        logger.warning(f"Background worker startup failed: {e}")
    
    logger.info("Application startup complete")
    yield
    
    # Cleanup
    try:
        if 'scheduler' in locals():
            scheduler.shutdown()
            logger.info("Background worker stopped")
    except Exception as e:
        logger.warning(f"Background worker shutdown failed: {e}")
    
    logger.info("Application shutdown")


def create_app() -> FastAPI:
    """Create FastAPI application"""
    app = FastAPI(
        title=settings.PANEL_NAME,
        description="Server management panel with Proxmox integration",
        lifespan=lifespan,
        debug=settings.DEBUG
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, specify exact origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Add request logging middleware
    app.add_middleware(RequestLoggingMiddleware, enable_api_logging=True)
    
    # Add language detection middleware
    app.add_middleware(LanguageMiddleware)
    
    # Templates
    templates = Jinja2Templates(directory="app/templates")



    # Custom exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"}
        )

    @app.exception_handler(HTTPException)
    async def custom_http_exception_handler(request: Request, exc: HTTPException):
        logger.warning(f"HTTP exception: {exc.status_code} - {exc.detail}")
        return await http_exception_handler(request, exc)

    # Health check endpoint
    @app.get("/health")
    async def health_check():
        """Health check endpoint"""
        db_healthy = check_db_connection()
        return {
            "status": "healthy" if db_healthy else "unhealthy",
            "version": settings.VERSION,
            "database": "connected" if db_healthy else "disconnected"
        }

    # Root redirect
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/dashboard", status_code=302)

    # Legacy redirect for old /servers URL
    @app.get("/servers", include_in_schema=False)
    async def servers_redirect():
        return RedirectResponse(url="/proxmox/vms", status_code=301)
    
    # Direct access to /vms - now shows Proxmox servers
    @app.get("/vms", include_in_schema=False)
    async def vms_redirect():
        return RedirectResponse(url="/proxmox/vms", status_code=301)
    
    # Virtual Machines list page
    @app.get("/virtual-machines", include_in_schema=False)
    async def virtual_machines_page(request: Request):
        """Virtual Machines list page"""
        from .i18n import t
        lang = request.cookies.get("language", "ru")
        
        context = {
            "request": request,
            "page_title": t('nav_vms', lang),
        }
        context = add_i18n_context(request, context)
        return templates.TemplateResponse("virtual_machines.html", context)
    
    # Virtual Machine detail page
    @app.get("/virtual-machines/{server_id}/{vmid}", include_in_schema=False)
    async def virtual_machine_detail_page(request: Request, server_id: int, vmid: int, type: str = "qemu", node: str = ""):
        """Virtual Machine detail page"""
        return RedirectResponse(url=f"/proxmox/server/{server_id}/instance/{vmid}?type={type}&node={node}", status_code=302)
    
    # API for virtual machines list (proxy to proxmox router)
    @app.get("/api/virtual-machines")
    async def api_virtual_machines(request: Request):
        """Proxy to proxmox virtual-machines API"""
        return RedirectResponse(url="/proxmox/api/virtual-machines", status_code=307)

    # Include routers
    app.include_router(auth_router.router, tags=["auth"])
    app.include_router(dashboard_router.router, tags=["dashboard"])
    app.include_router(proxmox_router.router, prefix="/proxmox", tags=["proxmox"])
    app.include_router(templates_router.router, prefix="/templates", tags=["templates"])
    app.include_router(ipam_router.router, prefix="/ipam", tags=["ipam"])
    app.include_router(logs_router.router, prefix="/logs", tags=["logs"])
    app.include_router(settings_router.router, prefix="/settings", tags=["settings"])
    app.include_router(notifications_router.router, tags=["notifications"])
    app.include_router(users_router.router, prefix="/admin", tags=["users", "admin"])

    # Mount static files for noVNC
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
