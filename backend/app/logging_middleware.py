"""
HTTP Request Logging Middleware
Automatically logs all API requests to the audit log with enhanced details
"""

import time
import uuid
import json
from typing import Callable, Optional
from urllib.parse import parse_qsl
from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from loguru import logger

from . import db as _db
from .auth import get_client_ip
from .logging_service import LoggingService


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all HTTP requests with enhanced details"""
    
    # Paths to exclude from detailed logging (too verbose)
    EXCLUDE_PATHS = [
        "/health",
        "/favicon.ico",
        "/static/",
    ]
    
    # Paths that should only log errors (to reduce noise)
    QUIET_PATHS = [
        "/api/logs",           # Don't log requests to view logs
        "/api/virtual-machines",  # High frequency polling
    ]

    # GET-only polling paths — only log on error/slow (dashboard real-time updates)
    POLL_PATHS = [
        "/proxmox/api/resources/all",
        "/proxmox/api/servers",
        "/settings/api/panel",
        "/api/notifications/unread-count",
    ]
    # Path prefixes for node polling (dynamic IDs, e.g. /proxmox/api/4/nodes)
    POLL_PREFIXES = [
        "/proxmox/api/",  # all proxmox data reads
    ]
    
    # Paths to log request body (important operations)
    LOG_BODY_PATHS = [
        "/api/proxmox/",
        "/api/auth/",
        "/api/users/",
        "/api/ipam/",
        "/api/settings/",
    ]
    
    # Max body size to log (bytes)
    MAX_BODY_SIZE = 10000
    
    def __init__(self, app: ASGIApp, enable_api_logging: bool = True):
        super().__init__(app)
        self.enable_api_logging = enable_api_logging
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip excluded paths
        path = request.url.path
        for excluded in self.EXCLUDE_PATHS:
            if path.startswith(excluded):
                return await call_next(request)
        
        # Generate unique request ID for correlation
        request_id = str(uuid.uuid4())
        
        # Store request ID in state for use in handlers
        request.state.request_id = request_id
        
        # Record start time
        start_time = time.time()
        
        # Get client info
        client_ip = self._get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")[:500]
        
        # Try to get username and session from token
        username = None
        session_id = None
        user_id = None
        try:
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                import hashlib
                session_id = hashlib.sha256(token.encode()).hexdigest()[:16]
        except Exception as e:
            logger.debug(f"Could not extract session ID from token: {e}")
        
        # Capture request body for important operations
        request_body = None
        should_log_body = any(path.startswith(p) for p in self.LOG_BODY_PATHS)
        if should_log_body and request.method in ["POST", "PUT", "PATCH"]:
            try:
                body_bytes = await request.body()
                if len(body_bytes) <= self.MAX_BODY_SIZE:
                    request_body = self._parse_body(request.headers.get("content-type", ""), body_bytes)
            except Exception as e:
                logger.debug(f"Could not read request body: {e}")
        
        # Get query params
        query_params = str(request.query_params) if request.query_params else None
        
        # Process request
        response = await call_next(request)
        
        # Calculate duration
        duration_ms = int((time.time() - start_time) * 1000)
        
        # Determine if we should log this request
        should_log = self.enable_api_logging

        # Suppress GET polling traffic — only log errors or very slow requests
        if request.method == "GET":
            for poll_path in self.POLL_PATHS:
                if path == poll_path or path.startswith(poll_path):
                    should_log = response.status_code >= 400 or duration_ms > 5000
                    break
            else:
                for prefix in self.POLL_PREFIXES:
                    if path.startswith(prefix):
                        should_log = response.status_code >= 400 or duration_ms > 5000
                        break

        # Check quiet paths - only log errors or slow requests
        for quiet_path in self.QUIET_PATHS:
            if path.startswith(quiet_path):
                should_log = response.status_code >= 400 or duration_ms > 5000
                break
        
        # Log to database. LoggingService is synchronous (SQLAlchemy commit);
        # run it in the threadpool so a slow DB write does not stall the event
        # loop — and with it every WebSocket console and metrics stream.
        if should_log and (path.startswith("/api/") or path.startswith("/proxmox/") or path.startswith("/ipam/")):
            status_code = response.status_code
            error_message = f"HTTP {status_code}" if status_code >= 400 else None

            # Determine log level
            level = LoggingService.DEBUG
            if status_code >= 500:
                level = LoggingService.ERROR
            elif status_code >= 400:
                level = LoggingService.WARNING
            elif duration_ms > 3000:
                level = LoggingService.WARNING  # Slow requests

            # Create detailed message
            message = f"{request.method} {path}"
            if duration_ms > 1000:
                message += f" (slow: {duration_ms}ms)"
            message += f" -> {status_code}"

            await run_in_threadpool(
                self._write_log,
                level=level,
                category=LoggingService.API,
                action=self._get_action_from_request(request.method, path),
                message=message,
                request_id=request_id,
                session_id=session_id,
                username=username,
                user_id=user_id,
                ip_address=client_ip,
                user_agent=user_agent,
                request_method=request.method,
                request_path=path,
                query_params=query_params,
                request_body=request_body,
                response_status=status_code,
                duration_ms=duration_ms,
                success=status_code < 400,
                error_message=error_message,
                error_traceback=None,
            )
        
        # Add request ID to response headers for debugging
        response.headers["X-Request-ID"] = request_id
        
        return response
    
    @staticmethod
    def _write_log(**fields) -> None:
        try:
            db = _db.SessionLocal()
            try:
                LoggingService.log(db=db, **fields)
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Failed to log API request: {e}")

    @staticmethod
    def _parse_body(content_type: str, body: bytes) -> Optional[dict]:
        """Decode a request body into a dict so LoggingService can mask secrets.

        Form posts (e.g. /api/auth/change-password) used to be stored verbatim
        as ``_raw`` — masking only applies to dict keys, so old and new
        passwords landed in the audit log in plaintext. Bodies that cannot be
        turned into key/value pairs are not logged at all.
        """
        if not body:
            return None
        ctype = content_type.split(";", 1)[0].strip().lower()
        try:
            if ctype == "application/x-www-form-urlencoded":
                return dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
            data = json.loads(body)
            return data if isinstance(data, dict) else {"_items": data}
        except Exception:
            return {"_omitted": f"{len(body)} bytes ({ctype or 'unknown type'})"}

    def _get_client_ip(self, request: Request) -> str:
        """Client IP using the same trusted-proxy rules as authentication."""
        return get_client_ip(request)
    
    def _get_action_from_request(self, method: str, path: str) -> str:
        """Determine action name from HTTP method and path"""
        # Map method to basic action
        method_actions = {
            "GET": "read",
            "POST": "create",
            "PUT": "update",
            "PATCH": "update",
            "DELETE": "delete"
        }
        base_action = method_actions.get(method, method.lower())
        
        # Try to extract resource from path
        path_parts = path.strip("/").split("/")
        if len(path_parts) >= 2:
            resource = path_parts[1] if path_parts[0] in ["api", "proxmox", "ipam"] else path_parts[0]
            # Handle specific actions in path
            if len(path_parts) >= 3:
                last_part = path_parts[-1]
                if last_part in ["start", "stop", "restart", "shutdown", "clone", "migrate", "backup", "console"]:
                    return f"{resource}_{last_part}"
            return f"{resource}_{base_action}"
        
        return f"api_{base_action}"
