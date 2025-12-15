import logging
from typing import Generator, AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.engine import Engine
import time

from .config import settings

logger = logging.getLogger(__name__)

# Configure SQLAlchemy engine with connection pooling
engine = create_engine(
    settings.SQLALCHEMY_DATABASE_URI,
    echo=settings.DEBUG,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # Validates connections before use
    pool_recycle=3600,   # Recycle connections every hour
    connect_args={
        "connect_timeout": 10,
        "application_name": settings.PANEL_NAME,
    }
)

# Add connection event listeners for monitoring
@event.listens_for(Engine, "before_cursor_execute")
def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    context._query_start_time = time.time()
    if settings.DEBUG:
        logger.debug("Start Query: %s", statement)


@event.listens_for(Engine, "after_cursor_execute")  
def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    total = time.time() - context._query_start_time
    if settings.DEBUG:
        logger.debug("Query Complete in %.3f seconds", total)
    if total > 1.0:  # Log slow queries
        logger.warning("Slow query detected (%.3f seconds): %s", total, statement)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        logger.error(f"Database session error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


@asynccontextmanager
async def get_async_db() -> AsyncGenerator[Session, None]:
    """Async context manager for database session"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        logger.error(f"Database session error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Initialize database tables"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized successfully")
        
        # Initialize default panel settings if not exist
        init_default_settings()
        
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise


def init_default_settings() -> None:
    """Initialize default panel settings"""
    try:
        from .models import PanelSettings
        
        db = SessionLocal()
        try:
            # Check if settings already exist
            existing = db.query(PanelSettings).count()
            if existing > 0:
                logger.info(f"Panel settings already initialized ({existing} settings found)")
                return
            
            # Add default settings
            defaults = [
                PanelSettings(
                    key="refresh_interval",
                    value="5",
                    description="Интервал обновления данных на панели (секунды)"
                ),
                PanelSettings(
                    key="log_retention_days",
                    value="30",
                    description="Срок хранения логов перед автоматической очисткой (дни)"
                ),
                PanelSettings(
                    key="language",
                    value="ru",
                    description="Язык интерфейса панели (ru/en)"
                ),
            ]
            
            for setting in defaults:
                db.add(setting)
            
            db.commit()
            logger.info(f"Added {len(defaults)} default panel settings")
            
        finally:
            db.close()
            
    except Exception as e:
        logger.warning(f"Failed to initialize default settings: {e}")
        # Don't raise - this is not critical for app startup


def check_db_connection() -> bool:
    """Check if database connection is working"""
    try:
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False
