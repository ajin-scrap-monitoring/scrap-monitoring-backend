"""Core configuration and lifecycle settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.app.core.edge_v2_security import EdgeV2Device
from src.app.schemas.monitoring_v2 import MonitoringSource


class Settings(BaseSettings):
    """Application settings with fail-fast validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # Server basic settings
    APP_ENV: Literal["development", "production", "test"] = "development"
    APP_NAME: str = "scrap-monitoring-backend"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    DEBUG: bool = False

    # Initial account passwords are used only when the auth user table is empty.
    ADMIN_PASSWORD: SecretStr | None = Field(default=None, repr=False)
    OPERATOR_PASSWORD: SecretStr | None = Field(default=None, repr=False)

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

    # V2 device-scoped credentials: JSON list supplied only through private configuration.
    EDGE_V2_DEVICES: list[EdgeV2Device] = Field(default_factory=list, repr=False)

    # Opt-in dashboard assignments; v1 keeps its existing data source and contract.
    EDGE_V2_MONITORING_SOURCES: list[MonitoringSource] = Field(default_factory=list)
    EDGE_V2_ASSESSMENT_TTL_SECONDS: int = Field(default=30, ge=1, le=3600)
    # V2 operational rules are opt-in; explicit pit thresholds still take precedence.
    EDGE_V2_OPERATIONS_ENABLED: bool = False
    EDGE_V2_CRITICAL_THRESHOLD: float = Field(default=90.0, gt=5.0, le=100.0)

    @model_validator(mode="after")
    def validate_monitoring_sources(self) -> "Settings":
        devices = {(device.site_id, device.edge_id) for device in self.EDGE_V2_DEVICES}
        pits: set[str] = set()
        assigned: set[tuple[str, str]] = set()
        for source in self.EDGE_V2_MONITORING_SOURCES:
            device = (source.site_id, source.edge_id)
            if source.pit_id in pits or device in assigned:
                raise ValueError("Dashboard assignments must be one-to-one")
            if device not in devices:
                raise ValueError("Dashboard source must reference a configured v2 device")
            pits.add(source.pit_id)
            assigned.add(device)
        return self

    @field_validator("EDGE_V2_DEVICES")
    @classmethod
    def validate_v2_devices(cls, devices: list[EdgeV2Device]) -> list[EdgeV2Device]:
        seen: set[str] = set()
        sensors: dict[tuple[str, str], tuple[str, str]] = {}
        for device in devices:
            token = device.token.get_secret_value()
            if token in seen:
                raise ValueError("Each v2 token must identify exactly one device")
            seen.add(token)
            key = (device.site_id, device.edge_id)
            if key in sensors and sensors[key] != device.sensor_ids:
                raise ValueError("Rotated tokens must use the same sensor IDs")
            sensors[key] = device.sensor_ids
        return devices

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

    # WebRTC / WHEP Media Server Integration
    MEDIA_SERVER_WHEP_URL: str | None = Field(
        default=None,
        description="Upstream Media Server WHEP endpoint base URL (e.g. http://scrap-server-01.tail2e96c5.ts.net:8889)",
    )
    WHEP_SESSION_TTL_SECONDS: int = Field(
        default=1800,
        description="WebRTC playback session time-to-live in seconds (default 30m)",
    )
    WHEP_MOCK_FALLBACK: bool = Field(
        default=True,
        description="Allow fallback to mock SDP answer when upstream media server is unavailable",
    )

    # Camera media v1 control API; credentials never appear in browser responses.
    MEDIA_SERVICE_URL: str | None = None
    MEDIA_API_TOKEN: SecretStr | None = None
    MEDIA_CAMERA_ID: str = Field(
        default="camera-main", max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$"
    )
    MEDIA_PIT_ID: str = Field(default="pit-01", min_length=1, max_length=50)
    MEDIA_REQUEST_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0.0, le=30.0)
    MEDIA_POLL_INTERVAL_SECONDS: float = Field(default=2.0, ge=1.0, le=60.0)

    @model_validator(mode="after")
    def validate_media_configuration(self) -> "Settings":
        import re
        from urllib.parse import urlsplit

        if self.MEDIA_SERVICE_URL == "":
            self.MEDIA_SERVICE_URL = None
        if self.MEDIA_API_TOKEN and not self.MEDIA_API_TOKEN.get_secret_value():
            self.MEDIA_API_TOKEN = None
        if bool(self.MEDIA_SERVICE_URL) != bool(self.MEDIA_API_TOKEN):
            raise ValueError("MEDIA_SERVICE_URL and MEDIA_API_TOKEN must be configured together")
        if self.MEDIA_SERVICE_URL:
            parsed = urlsplit(self.MEDIA_SERVICE_URL)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path.rstrip("/") != "/api/v1"
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            ):
                raise ValueError("MEDIA_SERVICE_URL must be an HTTP(S) base URL ending in /api/v1")
            token = self.MEDIA_API_TOKEN.get_secret_value() if self.MEDIA_API_TOKEN else ""
            if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
                raise ValueError("MEDIA_API_TOKEN must use the Bearer token character set")
        return self

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
