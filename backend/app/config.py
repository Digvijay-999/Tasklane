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
    # How long the placeholder stage takes (the repo_index workflow, until Phase 6).
    dummy_stage_seconds: float = Field(default=3.0, ge=0, le=3600)

    # --- PDF workflow (Phase 5) ---
    # Limits on untrusted PDFs (enforced in a separate parser process; see workflows/pdf).
    pdf_max_pages: int = Field(default=500, ge=1, le=100_000)
    pdf_max_text_chars: int = Field(default=2_000_000, ge=1_000)
    pdf_validate_timeout_seconds: float = Field(default=60, gt=0, le=3600)
    pdf_extract_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    pdf_parser_memory_mb: int = Field(default=1024, ge=256, le=16_384)
    pdf_parser_output_max_mb: int = Field(default=64, ge=1, le=4096)
    # Chunking (characters; ~4 characters per token).
    chunk_target_chars: int = Field(default=4000, ge=200)
    chunk_max_chars: int = Field(default=6000, ge=200)
    chunk_overlap_chars: int = Field(default=400, ge=0)
    # Summarization.
    provider: Literal["stub"] = "stub"
    provider_timeout_seconds: float = Field(default=20, gt=0, le=600)
    summarize_timeout_seconds: float = Field(default=900, gt=0, le=7200)
    summary_chunk_max_chars: int = Field(default=600, ge=100)
    summary_final_max_chars: int = Field(default=2000, ge=200)
    summary_combine_max_input_chars: int = Field(default=12000, ge=1000)

    @model_validator(mode="after")
    def _visibility_timeout_exceeds_lease(self) -> "Settings":
        if self.celery_visibility_timeout_seconds <= self.lease_seconds:
            raise ValueError(
                "CELERY_VISIBILITY_TIMEOUT_SECONDS must be longer than LEASE_SECONDS, or Redis "
                "would redeliver messages for work that is still legitimately leased"
            )
        return self

    @model_validator(mode="after")
    def _lease_safe_timeouts(self) -> "Settings":
        # A provider call can't renew the lease while it waits, so it must end well before the
        # lease would lapse (stages renew the lease after every call).
        if self.provider_timeout_seconds > self.lease_seconds / 2:
            raise ValueError("PROVIDER_TIMEOUT_SECONDS must be at most half of LEASE_SECONDS")
        stage_limits = (
            self.pdf_validate_timeout_seconds
            + self.pdf_extract_timeout_seconds
            + self.summarize_timeout_seconds
        )
        if self.celery_task_time_limit_seconds <= stage_limits:
            raise ValueError(
                "CELERY_TASK_TIME_LIMIT_SECONDS must be longer than the PDF stage time limits "
                "combined, so a stage's own limit always fires first"
            )
        # The parser writes extracted text as ASCII-escaped JSON lines: up to 12 bytes per
        # character (a surrogate pair), plus a little per page. A PDF within the text limit must
        # never hit the output-file limit instead (it would fail as pdf_too_complex).
        needed = self.pdf_max_text_chars * 12 + self.pdf_max_pages * 64
        if needed > self.pdf_parser_output_max_mb * 1024 * 1024:
            raise ValueError(
                "PDF_PARSER_OUTPUT_MAX_MB is too small for PDF_MAX_TEXT_CHARS "
                f"(needs at least {needed // (1024 * 1024) + 1} MB)"
            )
        # Fail at startup, not on the first job, if the chunking or summary sizes don't fit.
        from app.workflows.pdf.chunking import ChunkingSettings
        from app.workflows.pdf.summarize import SummarySettings

        ChunkingSettings(
            target_chars=self.chunk_target_chars,
            max_chars=self.chunk_max_chars,
            overlap_chars=self.chunk_overlap_chars,
        )
        SummarySettings(
            chunk_max_chars=self.summary_chunk_max_chars,
            final_max_chars=self.summary_final_max_chars,
            combine_max_input_chars=self.summary_combine_max_input_chars,
        )
        return self

    @property
    def keep_alive_interval_seconds(self) -> float:
        """How often long waits renew the lease: a quarter of it."""
        return self.lease_seconds / 4


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process and reuse them."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
