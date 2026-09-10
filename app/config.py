from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """Application configuration loaded from environment variables or defaults."""
    
    app_name: str = "Flock Energy - Urja Meter Ops API"
    app_version: str = "1.0.0"
    debug: bool = False

    # Legacy Urja Portal Credentials & URL
    portal_base_url: str = "https://urja-ops.flockenergy.tech"
    portal_email: str = "operator@urja.local"
    portal_password: str = "urja-ops-2026"

    # HTTP client settings
    http_timeout_seconds: float = 20.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5

    # In-memory caching settings
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300  # 5 minutes default TTL

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
