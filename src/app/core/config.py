"""Core configuration and lifecycle settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with fail-fast validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server basic settings
    APP_ENV: Literal["development", "production", "test"] = "development"
    APP_NAME: str = "scrap-monitoring-backend"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    DEBUG: bool = False

    # CORS settings
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Database URL
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./test_scrap_monitoring.db",
        description="Async database connection URL",
    )

    # Edge Device Authentication
    EDGE_API_KEY: str = Field(
        default="dev-edge-secret-key-12345",
        description="Edge device shared authentication secret",
    )

    # Scrap Pit & Thresholds
    DEFAULT_WARNING_THRESHOLD: float = Field(
        default=75.0,
        ge=0.0,
        le=100.0,
        description="Default scrap level warning threshold (%)",
    )
    DEFAULT_CRITICAL_THRESHOLD: float = Field(
        default=85.0,
        ge=0.0,
        le=100.0,
        description="Default scrap level critical collection threshold (%)",
    )

    # Alert Cooldown (default: 30 minutes = 1800s)
    ALERT_COOLDOWN_SECONDS: int = Field(
        default=1800,
        ge=0,
        description="Alert cooldown timer in seconds to prevent spam",
    )

    # External Alert notification settings (Optional)
    ALERT_SMTP_HOST: str | None = None
    ALERT_SMTP_PORT: int = 587
    ALERT_SMTP_USER: str | None = None
    ALERT_SMTP_PASSWORD: str | None = None
    ALERT_RECIPIENT_EMAIL: str | None = None

    @field_validator("CORS_ORIGINS")
    @classmethod
    def validate_cors_origins(cls, v: str) -> str:
        """Validate and clean CORS origins string."""
        origins = [origin.strip() for origin in v.split(",") if origin.strip()]
        if not origins:
            return "*"
        return ",".join(origins)

    @property
    def cors_origin_list(self) -> list[str]:
        """Return CORS origins as a list of strings."""
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    def validate_startup_credentials(self) -> None:
        """Fail-fast check for production environment credentials."""
        if self.APP_ENV == "production":
            if "sqlite" in self.DATABASE_URL:
                raise ValueError("Production mode requires an RDBMS (e.g. PostgreSQL), not SQLite.")
            if self.EDGE_API_KEY == "dev-edge-secret-key-12345":
                raise ValueError("Production mode requires a secure, non-default EDGE_API_KEY.")


@lru_cache
def get_settings() -> Settings:
    """Singleton getter for application settings."""
    settings = Settings()
    return settings
