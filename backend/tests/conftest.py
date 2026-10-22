"""
Shared pytest fixtures and test setup.

This file overrides the production PostgreSQL database setup with an in-memory
SQLite database and configures mocks for all Proxmox operations.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# --- Environment must be set BEFORE importing app.* ---------------------------
# Use a PostgreSQL URL during import to avoid SQLAlchemy pool parameter TypeErrors (like max_overflow) with SQLite.
os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
os.environ["SECRET_KEY"] = "test-secret-key-do-not-use-in-prod"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["LOG_LEVEL"] = "WARNING"

# Generate a deterministic Fernet key for tests so encrypt/decrypt are stable.
from cryptography.fernet import Fernet  # noqa: E402
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

# Ensure the backend root is on sys.path so ``import app`` works regardless of
# the working directory pytest is launched from.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# pydantic-settings auto-loads ``.env`` from the current working directory.
# Run from a clean temp dir so only env vars are honoured.
_TEST_CWD = Path(tempfile.mkdtemp(prefix="pvemanager-tests-"))
os.chdir(_TEST_CWD)

import pytest  # noqa: E402
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Create an in-memory SQLite engine for testing
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Import app.db (this constructs PostgreSQL engine, avoiding SQLite pool parameter TypeError)
import app.db

# Monkeypatch engine and SessionLocal in app.db with SQLite test engine
app.db.engine = test_engine
app.db.SessionLocal = TestSessionLocal

# Update environment and settings to reflect SQLite for any other components
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
from app.config import settings
settings.DATABASE_URL = "sqlite:///:memory:"

# Import all models to register them with SQLAlchemy Base
import app.models  # noqa: F401

# Disable lifespan to avoid starting background workers and database migration checks during tests
from app.main import app as fastapi_app
from contextlib import asynccontextmanager

@asynccontextmanager
async def dummy_lifespan(app):
    yield

fastapi_app.router.lifespan_context = dummy_lifespan

# --- Reusable fixtures --------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_database():
    """Recreates the SQLite database tables and seeds default data before each test, dropping them after."""
    from app.db import Base, init_default_settings
    from app.models import Role
    
    Base.metadata.create_all(bind=test_engine)
    init_default_settings()
    
    db = TestSessionLocal()
    try:
        admin_role = Role(
            name="admin", 
            display_name="Administrator", 
            permissions={"*": True}, 
            is_system=True
        )
        user_role = Role(
            name="user", 
            display_name="User", 
            permissions={"server:view": True, "vm:view": True},
            is_system=True
        )
        db.add_all([admin_role, user_role])
        db.commit()
    finally:
        db.close()
        
    yield
    
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session():
    """Provides a database session for each test."""
    session = TestSessionLocal()
    yield session
    session.close()


@pytest.fixture
def client(db_session):
    """Provides a TestClient with get_db dependency overridden to the test transaction."""
    from app.db import get_db
    
    def _get_db_override():
        yield db_session
        
    fastapi_app.dependency_overrides[get_db] = _get_db_override
    from fastapi.testclient import TestClient
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def seed_users(db_session):
    """Seeds a test administrator and a standard user into the test database."""
    from app.models import User, Role
    from app.auth import get_password_hash
    
    admin_role = db_session.query(Role).filter(Role.name == "admin").first()
    user_role = db_session.query(Role).filter(Role.name == "user").first()
    
    admin_user = User(
        username="testadmin",
        email="testadmin@example.com",
        hashed_password=get_password_hash("AdminPass123!"),
        is_active=True,
        is_admin=True,
        role=admin_role,
    )
    test_user = User(
        username="testuser",
        email="testuser@example.com",
        hashed_password=get_password_hash("UserPass123!"),
        is_active=True,
        is_admin=False,
        role=user_role,
    )
    
    db_session.add_all([admin_user, test_user])
    db_session.commit()  # Commit so the records are visible to all connections/sessions
    
    return {"admin": admin_user, "user": test_user}


def _get_headers_for_user(db_session, user):
    """Helper to generate JWT headers and create an active session in the database."""
    from app.services.security_service import SecurityService
    from app.auth import create_access_token
    
    session, _ = SecurityService.create_session(
        db_session, user, ip_address="127.0.0.1", user_agent="test-agent"
    )
    token = create_access_token(
        data={"sub": user.username},
        session_token=session.session_token
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(db_session, seed_users):
    """Returns authorization headers for the test admin user."""
    return _get_headers_for_user(db_session, seed_users["admin"])


@pytest.fixture
def user_headers(db_session, seed_users):
    """Returns authorization headers for the test standard user."""
    return _get_headers_for_user(db_session, seed_users["user"])


@pytest.fixture
def mock_proxmox():
    """Mocks ProxmoxClient and get_proxmox_resources globally for tests."""
    from unittest.mock import patch
    
    mock_client_instance = MagicMock()
    mock_client_instance.is_connected.return_value = True
    mock_client_instance.check_connection.return_value = (True, None, None)
    mock_client_instance.get_next_vmid.return_value = 101
    mock_client_instance.get_nodes.return_value = [{"node": "pve1", "uptime": 99999}]
    mock_client_instance.get_all_resources.return_value = {
        "vms": [
            {"vmid": 100, "name": "vm100", "status": "running", "node": "pve1", "template": 0, "type": "qemu"}
        ],
        "containers": [
            {"vmid": 200, "name": "ct200", "status": "stopped", "node": "pve1", "template": 0, "type": "lxc"}
        ]
    }
    mock_client_instance.get_vm_status.return_value = {"status": "running", "uptime": 12345}
    mock_client_instance.get_container_status.return_value = {"status": "stopped"}
    mock_client_instance.get_vm_config.return_value = {"cores": 2, "memory": 2048, "name": "vm100", "node": "pve1"}
    mock_client_instance.get_container_config.return_value = {"cores": 1, "memory": 1024, "name": "ct200", "node": "pve1"}
    mock_client_instance.get_vm_interfaces.return_value = [{"name": "net0", "ip": "192.168.1.100"}]
    mock_client_instance.delete_vm.return_value = "UPID:pve1:00001D4C:00000000:666:qvmdelete:100:testadmin:"
    mock_client_instance.delete_container.return_value = "UPID:pve1:00001D4C:00000000:666:ctdelete:200:testadmin:"
    
    with patch("app.proxmox.client.ProxmoxClient") as mock_client_cls_a, \
         patch("app.proxmox.ProxmoxClient") as mock_client_cls_b, \
         patch("app.api.proxmox._helpers.ProxmoxClient") as mock_client_cls_c, \
         patch("app.api.proxmox._helpers._get_proxmox_client", return_value=mock_client_instance), \
         patch("app.proxmox.get_proxmox_resources") as mock_resources:

        # Clients are now built via the ``ProxmoxClient.from_server`` classmethod
        # (see commit d66928e). Wire both the constructor call and ``from_server``
        # to return the configured instance so every construction path resolves
        # to the same mock.
        for mock_cls in (mock_client_cls_a, mock_client_cls_b, mock_client_cls_c):
            mock_cls.return_value = mock_client_instance
            mock_cls.from_server.return_value = mock_client_instance

        mock_resources.return_value = mock_client_instance.get_all_resources()
        yield {
            "client": mock_client_instance,
            "get_proxmox_resources": mock_resources
        }


# --- Legacy fixtures for compatibility with existing unit tests ---------------

@pytest.fixture
def fake_role_factory():
    """Build a stand-in Role object with a permissions dict."""
    def _make(permissions: dict | None = None, name: str = "tester"):
        return SimpleNamespace(
            name=name,
            permissions=permissions or {},
            has_permission=lambda perm: bool((permissions or {}).get(perm)),
        )
    return _make


@pytest.fixture
def fake_user_factory(fake_role_factory):
    """Build a stand-in User object compatible with auth/rbac code paths."""
    def _make(
        username: str = "alice",
        is_admin: bool = False,
        is_active: bool = True,
        role=None,
        permissions: dict | None = None,
    ):
        if role is None and permissions is not None:
            role = fake_role_factory(permissions=permissions)
        return SimpleNamespace(
            id=1,
            username=username,
            is_admin=is_admin,
            is_active=is_active,
            role=role,
            locked_until=None,
        )
    return _make


@pytest.fixture
def mock_db():
    """Bare MagicMock standing in for a SQLAlchemy session."""
    return MagicMock(name="db_session")
