"""Centralized environment-driven configuration.

Nothing here is required to be set for the default offline demo -- every
field has a safe default that keeps the gateway fully functional against the
mock providers with no external services.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    use_fake_redis_if_unreachable: bool = True

    cache_similarity_threshold: float = 0.92
    cache_ttl_seconds: int = 3600

    circuit_failure_threshold: int = 3
    circuit_window_seconds: float = 60.0
    circuit_recovery_timeout_s: float = 5.0
    circuit_max_recovery_timeout_s: float = 60.0

    jury_score_threshold: float = 0.4

    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
