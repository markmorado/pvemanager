import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Query, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .config import settings
from .db import Base, engine, init_db, check_db_connection
from .auth import PermissionChecker, get_current_user
from .models import User
from .api import dashboard as dashboard_router
from .api import proxmox as proxmox_router
from .api import auth as auth_router
from .api import templates as templates_router
from .api import ipam as ipam_router
from .api import logs as logs_router
from .api import settings as settings_router
from .api import notifications as notifications_router
from .api import users as users_router
from .api import workspaces as workspaces_router
from .api import ssh_keys as ssh_keys_router
from .api import appstore as appstore_router
from .api import scripts as scripts_router
from .logging_middleware import RequestLoggingMiddleware
from .language_middleware import LanguageMiddleware
from .i18n import I18nService


# Browsers cannot set headers on WebSocket requests, so WS tickets and
# console handles travel in the query string (outdated clients may still send
# the session JWT as ?token=) — and uvicorn logs WebSocket connections with it
# ("WebSocket /ws/tasks?ticket=..." [accepted]).
_SECRET_QUERY_RE = re.compile(r"((?:token|ticket|auth_ticket|vncticket|vnc_password)=)[^&\s\"']+")


def _redact_secrets(message: str) -> str:
    if "=" not in message:
        return message
    return _SECRET_QUERY_RE.sub(r"\1***", message)


class _RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _redact_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


# Configure logging
def setup_logging():
    """Setup application logging"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Remove default logger
    logger.remove()

    # Add console logging (stderr keeps logs off stdout, which may carry data)
    import sys
    logger.add(
        sink=sys.stderr,
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
            
            logger.opt(depth=depth, exception=record.exc_info).log(level, _redact_secrets(record.getMessage()))
    
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    # uvicorn's loggers keep their own handlers (propagate=False).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addFilter(_RedactSecretsFilter())

    # Suppress noisy third-party loggers
    for noisy_logger in (
        "apscheduler",
        "apscheduler.scheduler",
        "apscheduler.executors.default",
        "httpx",
        "httpcore",
        "httpcore.http11",
        "httpcore.connection",
        "urllib3",
        "urllib3.connectionpool",
        "multipart",
    ):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # Filter uvicorn access log — suppress GET polling endpoints
    import re as _re
    _POLL_RE = _re.compile(
        r'"GET /(proxmox/api/|settings/api/panel|api/notifications/unread-count|proxmox/api/servers)'
    )

    class _UvicornAccessFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            # Allow all non-200 responses through
            if '" 2' not in msg:
                return True
            return not bool(_POLL_RE.search(msg))

    for uvicorn_logger_name in ("uvicorn.access", "uvicorn"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.addFilter(_UvicornAccessFilter())


# Placeholder values shipped in .env.example / deploy.sh templates. A panel
# started with one of them signs JWTs with a publicly known key, so anyone could
# forge an admin token — refuse to start instead.
_INSECURE_SECRET_KEYS = {
    "your-secret-key-here",
    "your-very-secure-secret-key-change-this-in-production-minimum-32-chars",
}


def _load_or_create_secret_key() -> str:
    """Read the generated JWT key from the data volume, creating it once."""
    import secrets
    key_file = Path(settings.APPSTORE_DATA_DIR).parent / "secret_key"
    try:
        if key_file.exists():
            stored = key_file.read_text().strip()
            if len(stored) >= 32:
                return stored
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        key_file.write_text(key)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        return key
    except OSError as e:
        logger.error(f"Cannot persist generated SECRET_KEY to {key_file}: {e} — sessions reset on restart")
        return secrets.token_hex(32)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    logger.info(f"Starting {settings.PANEL_NAME}")
    
    # Setup logging
    setup_logging()
    
    # Security checks
    if len(settings.SECRET_KEY or "") < 32 and settings.SECRET_KEY not in _INSECURE_SECRET_KEYS:
        # Not fatal: refusing to start would take down existing installs on
        # self-update. Weak keys are brute-forceable offline from any JWT.
        logger.warning("SECRET_KEY is shorter than 32 characters — generate one with `openssl rand -hex 32`.")
    if not settings.SECRET_KEY or settings.SECRET_KEY in _INSECURE_SECRET_KEYS:
        # A placeholder key is public knowledge: anyone could mint an admin
        # JWT. Swap in a generated key persisted on the data volume rather than
        # refusing to start (which would brick installs on self-update).
        settings.SECRET_KEY = _load_or_create_secret_key()
        logger.error(
            "SECRET_KEY was missing or a published placeholder — using a generated key "
            "instead (existing sessions are logged out). Set SECRET_KEY in backend/.env."
        )

    if not settings.FERNET_KEY:
        logger.warning("FERNET_KEY is not set. Sensitive fields (like passwords) will be stored as plaintext.")
        logger.warning("To enable encryption, generate a key and set FERNET_KEY in your .env file.")

    # Production hardening warnings (DEBUG is off in production)
    if not settings.DEBUG:
        if settings.ADMIN_PASSWORD == "admin123":
            logger.warning(
                "ADMIN_PASSWORD uses the insecure default 'admin123'. "
                "Set a strong ADMIN_PASSWORD env var for production."
            )
        if settings.CORS_ORIGINS.strip() == "*":
            logger.warning(
                "CORS_ORIGINS is '*' (any origin). Set an explicit comma-separated "
                "origin list (e.g. https://panel.example.com) for production."
            )
    
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
        from .db import engine
        from migrations.migrations import run_all_migrations
        run_all_migrations(engine)
        logger.info("Database migrations completed")
    except Exception as e:
        logger.warning(f"Migration check failed: {e}")
    
    # Initialize default admin user
    try:
        import init_admin
        init_admin.create_default_admin()
        logger.info("Default admin user initialization completed")
    except Exception as e:
        logger.error(f"Error running admin initialization: {e}")
    
    # Register the main event loop for cross-thread WebSocket broadcasts
    try:
        import asyncio
        from .websocket_manager import set_main_loop
        set_main_loop(asyncio.get_event_loop())
    except Exception as e:
        logger.warning(f"Failed to register main event loop: {e}")

    # Start background monitoring worker
    try:
        from .workers.monitoring_worker import start_monitoring_worker
        scheduler = start_monitoring_worker()
        logger.info("Background monitoring worker started")
    except Exception as e:
        logger.warning(f"Background worker startup failed: {e}")

    # Start backup scheduler
    backup_scheduler = None
    try:
        from .services.backup_scheduler import start_backup_scheduler
        backup_scheduler = start_backup_scheduler()
        logger.info("Backup scheduler started")
    except Exception as e:
        logger.warning(f"Backup scheduler startup failed: {e}")

    # Start WebSocket metrics broadcaster
    try:
        from .services.metrics_broadcaster import start_metrics_broadcaster
        from .db import SessionLocal
        start_metrics_broadcaster(SessionLocal)
    except Exception as e:
        logger.warning(f"Metrics broadcaster startup failed: {e}")

    # Start App Store catalog scheduler (periodic runtipi-appstore sync)
    try:
        from .services.appstore_catalog import start_catalog_scheduler
        start_catalog_scheduler()
        logger.info("App Store catalog scheduler started")
    except Exception as e:
        logger.warning(f"App Store catalog scheduler startup failed: {e}")

    # Start App Store state reconciliation (LXC status vs DB, orphan detection)
    try:
        from .services.appstore_engine import start_reconcile_scheduler
        start_reconcile_scheduler()
        logger.info("App Store reconcile scheduler started")
    except Exception as e:
        logger.warning(f"App Store reconcile scheduler startup failed: {e}")

    logger.info("Application startup complete")
    yield

    # Cleanup
    try:
        if 'scheduler' in locals():
            scheduler.shutdown()
            logger.info("Background worker stopped")
    except Exception as e:
        logger.warning(f"Background worker shutdown failed: {e}")

    try:
        if backup_scheduler:
            backup_scheduler.shutdown()
            logger.info("Backup scheduler stopped")
    except Exception as e:
        logger.warning(f"Backup scheduler shutdown failed: {e}")
    
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
    cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    # allow_credentials=True is incompatible with allow_origins=["*"] per the CORS spec.
    # Disable credentials when wildcard origin is configured.
    allow_credentials = cors_origins != ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add request logging middleware
    app.add_middleware(RequestLoggingMiddleware, enable_api_logging=True)

    # Add language detection middleware
    app.add_middleware(LanguageMiddleware)

    # Security headers middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # setdefault: an endpoint may set a stricter policy (sandboxed catalog logos).
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://esm.sh; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' wss: ws: https://esm.sh https://api.github.com; "
            "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
            "frame-ancestors 'self';"
        ))
        if not settings.DEBUG:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

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

    # Root — API info
    @app.get("/", include_in_schema=False)
    async def root():
        return {"name": settings.PANEL_NAME, "version": settings.VERSION, "docs": "/docs"}

    # API for virtual machines list (proxy to proxmox router)
    @app.get("/api/virtual-machines")
    async def api_virtual_machines(request: Request):
        """Proxy to proxmox virtual-machines API"""
        return RedirectResponse(url="/proxmox/api/virtual-machines", status_code=307)

    # WebSocket endpoint for real-time task updates
    @app.websocket("/ws/tasks")
    async def websocket_tasks(websocket: WebSocket, ticket: str = Query(None)):
        """
        WebSocket endpoint for live task manager updates.
        Auth via ?ticket= from POST /api/auth/ws-ticket (browsers cannot set
        WS headers).
        """
        from .auth import authenticate_ws_session, watch_ws_access
        from .db import SessionLocal
        from .models import TaskQueue, ProxmoxTask
        from .websocket_manager import ws_manager
        from .services.ws_access import authorize_channel, authorize_rrd_request
        import asyncio
        import json

        # --- Authenticate --------------------------------------------------
        # authenticate_ws also validates the bound session and the lock
        # state, so a logged-out / terminated session cannot keep streaming.
        if not ticket:
            await websocket.close(code=4001, reason="Missing ticket")
            return
        db = SessionLocal()
        try:
            user, ws_session = authenticate_ws_session(ticket, db)
            if not user:
                await websocket.close(code=4001, reason="Invalid token")
                return
            user_id = user.id
        finally:
            db.close()

        # --- Connect -------------------------------------------------------
        await ws_manager.connect(websocket, user_id)

        # The socket outlives the handshake check: drop it once the session is
        # logged out / terminated or the account is disabled (4001 = re-auth).
        async def _close_when_revoked() -> None:
            await watch_ws_access(user_id, ws_session)
            try:
                await websocket.close(code=4001, reason="Session ended")
            except Exception:
                pass

        watchdog = asyncio.create_task(_close_when_revoked())

        # In-flight on-demand requests of this socket: held so they are not
        # garbage-collected mid-run, cancelled when the client goes away.
        pending_requests: set = set()

        def _initial_snapshot() -> list:
            db = SessionLocal()
            try:
                bulk = db.query(TaskQueue).filter(TaskQueue.user_id == user_id).order_by(TaskQueue.created_at.desc()).limit(30).all()
                prox = db.query(ProxmoxTask).filter(ProxmoxTask.user_id == user_id).order_by(ProxmoxTask.created_at.desc()).limit(30).all()
                return sorted(
                    [t.to_dict() for t in bulk] + [t.to_dict() for t in prox],
                    key=lambda x: x.get("created_at") or "",
                    reverse=True,
                )[:30]
            finally:
                db.close()

        try:
            # Send initial snapshot (DB work off the event loop)
            all_tasks = await asyncio.to_thread(_initial_snapshot)
            await websocket.send_text(json.dumps({"type": "task_list", "tasks": all_tasks}, default=str))

            # Keep connection alive – wait for client messages / ping-pong
            while True:
                try:
                    raw = await websocket.receive_text()
                    # Legacy plain-text ping
                    if raw == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))
                        continue
                    # JSON message dispatcher
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        # e.g. a bare JSON array used to raise on .get() and drop the socket
                        continue
                    msg_type = msg.get("type", "")
                    if msg_type == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))
                    elif msg_type == "subscribe":
                        channel = msg.get("channel", "")
                        if isinstance(channel, str) and 0 < len(channel) <= 200:
                            allowed, payload_filter = await asyncio.to_thread(
                                authorize_channel, SessionLocal, user_id, channel
                            )
                            if not allowed:
                                await websocket.send_text(json.dumps(
                                    # not "channel": the client routes any message carrying that key
                                    # to the channel handlers, which expect metrics payloads.
                                    {"type": "subscribe_denied", "denied_channel": channel}
                                ))
                                continue
                            ws_manager.subscribe(websocket, channel, payload_filter)
                            # Immediately send cached payload so client sees data without waiting
                            from .services.metrics_broadcaster import get_cached_metrics
                            _cached = get_cached_metrics(channel)
                            if _cached:
                                try:
                                    await websocket.send_text(json.dumps(
                                        ws_manager.filter_payload(websocket, channel, _cached),
                                        default=str,
                                    ))
                                except Exception:
                                    pass
                    elif msg_type == "unsubscribe":
                        channel = msg.get("channel", "")
                        if channel:
                            ws_manager.unsubscribe(websocket, channel)
                    elif msg_type == "request":
                        action = msg.get("action", "")
                        request_id = msg.get("request_id", "")
                        params = msg.get("params") or {}
                        if action in ("get_rrd", "get_node_rrd") and request_id and isinstance(params, dict):
                            from .services.metrics_broadcaster import handle_rrd_request
                            if not await asyncio.to_thread(
                                authorize_rrd_request, SessionLocal, user_id, action, params
                            ):
                                await websocket.send_text(json.dumps({
                                    "type": "rrd_response", "request_id": request_id,
                                    "data": None, "error": "Access denied",
                                }))
                                continue
                            task = asyncio.create_task(
                                handle_rrd_request(
                                    websocket,
                                    request_id,
                                    action,
                                    params,
                                    SessionLocal,
                                )
                            )
                            pending_requests.add(task)
                            task.add_done_callback(pending_requests.discard)
                except WebSocketDisconnect:
                    break
                except Exception:
                    break
        finally:
            watchdog.cancel()
            for task in list(pending_requests):
                task.cancel()
            ws_manager.disconnect(websocket, user_id)

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
    app.include_router(workspaces_router.router, tags=["workspaces"])
    app.include_router(ssh_keys_router.router, tags=["ssh-keys"])
    app.include_router(appstore_router.router, tags=["appstore"])
    app.include_router(scripts_router.router, tags=["scripts"])

    return app


app = create_app()
