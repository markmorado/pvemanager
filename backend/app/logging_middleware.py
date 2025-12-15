"""
HTTP Request Logging Middleware
Automatically logs all API requests to the audit log with enhanced details
"""

import time
import uuid
import json
from typing import Callable, Optional
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from loguru import logger

from .db import SessionLocal
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
        "/api/logs",  # Don't log requests to view logs
        "/api/virtual-machines",  # High frequency polling
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
                # Extract session ID from token (first 16 chars of hash)
                import hashlib
                session_id = hashlib.sha256(token.encode()).hexdigest()[:16]
        except:
            pass
        
        # Capture request body for important operations
        request_body = None
        should_log_body = any(path.startswith(p) for p in self.LOG_BODY_PATHS)
        if should_log_body and request.method in ["POST", "PUT", "PATCH"]:
            try:
                body_bytes = await request.body()
                if len(body_bytes) <= self.MAX_BODY_SIZE:
                    try:
                        request_body = json.loads(body_bytes)
                    except:
                        request_body = {"_raw": body_bytes.decode('utf-8', errors='replace')[:1000]}
            except:
                pass
        
        # Get query params
        query_params = str(request.query_params) if request.query_params else None
        
        # Process request
        response = await call_next(request)
        
        # Calculate duration
        duration_ms = int((time.time() - start_time) * 1000)
        
        # Determine if we should log this request
        should_log = self.enable_api_logging
        
        # Check quiet paths - only log errors or slow requests
        for quiet_path in self.QUIET_PATHS:
            if path.startswith(quiet_path):
                should_log = response.status_code >= 400 or duration_ms > 5000
                break
        
        # Log to database (async-safe)
        if should_log and (path.startswith("/api/") or path.startswith("/proxmox/") or path.startswith("/ipam/")):
            try:
                db = SessionLocal()
                try:
                    error_message = None
                    error_traceback = None
                    if response.status_code >= 400:
                        error_message = f"HTTP {response.status_code}"
                    
                    # Determine log level
                    level = LoggingService.DEBUG
                    if response.status_code >= 500:
                        level = LoggingService.ERROR
                    elif response.status_code >= 400:
                        level = LoggingService.WARNING
                    elif duration_ms > 3000:
                        level = LoggingService.WARNING  # Slow requests
                    
                    # Determine action from path and method
                    action = self._get_action_from_request(request.method, path)
                    
                    # Create detailed message
                    message = f"{request.method} {path}"
                    if duration_ms > 1000:
                        message += f" (slow: {duration_ms}ms)"
                    message += f" -> {response.status_code}"
                    
                    LoggingService.log(
                        db=db,
                        level=level,
                        category=LoggingService.API,
                        action=action,
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
                        response_status=response.status_code,
                        duration_ms=duration_ms,
                        success=response.status_code < 400,
                        error_message=error_message,
                        error_traceback=error_traceback
                    )
                finally:
                    db.close()
            except Exception as e:
                logger.warning(f"Failed to log API request: {e}")
        
        # Add request ID to response headers for debugging
        response.headers["X-Request-ID"] = request_id
        
        return response
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, considering proxies"""
        # Check X-Forwarded-For header (from reverse proxy)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        # Check X-Real-IP header
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # Fall back to direct client IP
        if request.client:
            return request.client.host
        
        return "unknown"
    
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
