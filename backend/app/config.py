import os
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path


def get_version() -> str:
    """Read version from VERSION file"""
    # Try multiple locations for VERSION file
    possible_paths = [
        Path(__file__).parent.parent.parent.parent / "VERSION",  # Development
        Path(__file__).parent.parent / "VERSION",  # Docker /app/VERSION
        Path("/app/VERSION"),  # Docker absolute
    ]
    for version_file in possible_paths:
        if version_file.exists():
            return version_file.read_text().strip()
    return "1.7.0"


class Settings(BaseSettings):
    # Panel name - configurable but not shown in UI by default
    PANEL_NAME: str = Field(default="PVEmanager", env="PANEL_NAME")
    VERSION: str = Field(default_factory=get_version)
    DEBUG: bool = Field(default=False, env="DEBUG")

    # Database settings
    DB_HOST: str = Field(default="localhost", env="DB_HOST")
    DB_PORT: int = Field(default=5432, env="DB_PORT")
    DB_USER: str = Field(default="serverpanel", env="DB_USER")
    DB_PASSWORD: str = Field(default="serverpanel", env="DB_PASSWORD")
    DB_NAME: str = Field(default="serverpanel", env="DB_NAME")
    DATABASE_URL: Optional[str] = Field(default=None, env="DATABASE_URL")

    # Security settings
    SECRET_KEY: str = Field(default="your-secret-key-here", env="SECRET_KEY")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=480, env="ACCESS_TOKEN_EXPIRE_MINUTES")  # 8 hours

    # Logging settings
    LOG_LEVEL: str = Field(default="INFO", env="LOG_LEVEL")
    LOG_FILE: str = Field(default="logs/app.log", env="LOG_FILE")

    # SSH settings
    SSH_TIMEOUT: int = Field(default=10, env="SSH_TIMEOUT")
    DEFAULT_SSH_USER: str = Field(default="root", env="DEFAULT_SSH_USER")
    DEFAULT_SSH_PORT: int = Field(default=22, env="DEFAULT_SSH_PORT")
    
    # Timezone
    TZ: str = Field(default="UTC", env="TZ")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")
        return (
            f"postgresql+psycopg2://{self.DB_USER}:"
            f"{self.DB_PASSWORD}@{self.DB_HOST}:"
            f"{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def get_db_url(self) -> str:
        """Get database URL for alembic and other tools"""
        return self.SQLALCHEMY_DATABASE_URI


settings = Settings()


# Timezone-aware datetime helper
from datetime import datetime, timezone

def utcnow() -> datetime:
    """Get current UTC time as timezone-aware datetime.
    
    Use this instead of datetime.utcnow() to avoid timezone issues
    with PostgreSQL columns that have timezone=True.
    """
    return datetime.now(timezone.utc)
