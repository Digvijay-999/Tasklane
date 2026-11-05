"""Application settings, read from environment variables.

pydantic-settings works like a validated `process.env`: each field is read from the environment
variable of the same name (case-insensitive), converted to the declared type, and the app refuses
to start with a clear error if a required value is missing or invalid.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    # Redis (queue broker). The API only health-checks it; it never publishes tasks directly
    # (the dispatcher does, from the outbox).
    redis_url: str = "redis://localhost:6379/0"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Deadline for the /health/ready checks, also used as the connect timeout for Postgres and
    # Redis. Minimum 2: libpq treats 0 as "wait forever" and rounds 1 up to 2.
    dependency_timeout_seconds: int = Field(default=2, ge=2, le=30)

    # Local file storage for uploads and (later) artifacts. In Docker this is a named volume.
    storage_root: Path = Path("/data/storage")

    # Upload limits, enforced while streaming (Content-Length is only used to fail early).
    max_pdf_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    max_zip_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    # How long an upload can be used to create jobs.
    upload_ttl_hours: int = Field(default=24, ge=1)
    # Limit for JSON request bodies (every route except uploads).
    max_json_body_bytes: int = Field(default=64 * 1024, ge=1024)

    # --- Queue (Phase 4) ---
    # Celery queue name. Tests use their own Redis database and queue, so a running dev worker
    # never receives test messages.
    celery_queue: str = "tasklane"
    # Dispatcher: how often to look for due outbox rows, and how many to publish per pass.
    dispatch_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    dispatch_batch_size: int = Field(default=50, ge=1, le=1000)
    # Seconds the dispatcher waits for Redis when publishing before treating it as down.
    publish_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    # A claimed attempt's lease (renewed on every progress update). If it isn't renewed in
    # time, the worker is presumed dead (recovery acts on that in Phase 7).
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    # Redis redelivers a message that wasn't acknowledged within this time. Must exceed the lease.
    celery_visibility_timeout_seconds: int = Field(default=900, ge=60)
    # Last-resort limit after which Celery kills a task.
    celery_task_time_limit_seconds: int = Field(default=3600, ge=60)
    # Worker processes per worker container.
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    # How long the placeholder stage takes (Phase 4; replaced by real stages in Phases 5-6).
    dummy_stage_seconds: float = Field(default=3.0, ge=0, le=3600)

    @model_validator(mode="after")
    def _visibility_timeout_exceeds_lease(self) -> "Settings":
        if self.celery_visibility_timeout_seconds <= self.lease_seconds:
            raise ValueError(
                "CELERY_VISIBILITY_TIMEOUT_SECONDS must be longer than LEASE_SECONDS, or Redis "
                "would redeliver messages for work that is still legitimately leased"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process and reuse them."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
