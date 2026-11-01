"""Application settings, read from environment variables.

pydantic-settings works like a validated `process.env`: each field is read from the environment
variable of the same name (case-insensitive), converted to the declared type, and the app refuses
to start with a clear error if a required value is missing or invalid.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # PostgreSQL. The connection URL is assembled in app/db/session.py so that special characters
    # in the password are escaped correctly.
    postgres_user: str
    postgres_password: SecretStr  # SecretStr prints as '**********' in logs and errors
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Redis (queue broker). The API only health-checks it; it never publishes tasks directly.
    redis_url: str = "redis://localhost:6379/0"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Deadline for the /health/ready checks, also used as the connect timeout for Postgres and
    # Redis. Minimum 2: libpq treats 0 as "wait forever" and rounds 1 up to 2.
    dependency_timeout_seconds: int = Field(default=2, ge=2, le=30)


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process and reuse them."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
