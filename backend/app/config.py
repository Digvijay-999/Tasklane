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
    # env_ignore_empty: an empty variable (Compose turns `${NAME:-}` into "") means "use the
    # default".
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

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
    # A claimed attempt's lease, renewed by the worker's heartbeat thread. If it isn't renewed in
    # time, the worker is presumed dead and recovery retries (or fails) the job.
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    # How often the heartbeat renews the lease. Empty = a quarter of the lease (the default);
    # at most a third, so two missed beats in a row still leave the lease alive.
    heartbeat_interval_seconds: float | None = Field(default=None, ge=0.5)
    # Redis redelivers a message that wasn't acknowledged within this time. Must exceed the lease.
    celery_visibility_timeout_seconds: int = Field(default=900, ge=60)
    # Last-resort limit after which Celery kills a task.
    celery_task_time_limit_seconds: int = Field(default=3600, ge=60)
    # Worker processes per worker container.
    worker_concurrency: int = Field(default=2, ge=1, le=32)

    # --- Recovery (Phase 7), runs inside the dispatcher process ---
    # How often recovery looks for expired leases and lost messages.
    recovery_interval_seconds: float = Field(default=5, gt=0, le=300)
    # A claimable job whose last message went out longer ago than this, with no worker on it and
    # nothing waiting in the outbox, is sent again (its message was probably lost). Shorter
    # recovers faster; longer sends fewer harmless duplicates for jobs that are just waiting
    # behind a long queue.
    lost_message_grace_seconds: float = Field(default=300, ge=5, le=86_400)

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
    # Development and tests only: the stub provider waits this long per call (simulated
    # latency), so a job runs long enough to watch it or to stop a worker in the middle of it.
    stub_delay_seconds: float = Field(default=0, ge=0, le=60)

    # --- Repository ZIP workflow (Phase 6) ---
    # Limits on untrusted ZIPs, enforced in a separate process (see workflows/repo). Sizes are
    # counted from the real decompressed bytes; sizes declared in the ZIP are never trusted.
    zip_max_entries: int = Field(default=5000, ge=1, le=100_000)
    zip_max_total_bytes: int = Field(default=500 * 1024 * 1024, ge=1024)
    zip_max_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    zip_max_compression_ratio: int = Field(default=100, ge=2, le=100_000)
    # Ratios are checked only once an entry (or the whole archive) has produced this many bytes:
    # a small, very repetitive text file compresses extremely well and is harmless.
    zip_ratio_check_min_bytes: int = Field(default=1024 * 1024, ge=0)
    zip_max_path_length: int = Field(default=240, ge=16, le=4096)
    zip_max_path_depth: int = Field(default=20, ge=1, le=1000)
    zip_validate_timeout_seconds: float = Field(default=60, gt=0, le=3600)
    zip_extract_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    zip_sandbox_memory_mb: int = Field(default=512, ge=128, le=16_384)
    # Folder names whose contents are never indexed (comma-separated, matched case-insensitively
    # at any depth). Empty REPO_EXCLUDED_DIRS keeps the default list.
    repo_excluded_dirs: str = ""
    # Upper limit on summarization calls for one repository; above it the job fails with
    # repo_too_large before any call is made.
    repo_max_provider_calls: int = Field(default=1000, ge=1, le=100_000)

    # --- Retries (Phase 5) ---
    # Backoff before automatic retry n: equal jitter over min(base * 2^(n-1), max) seconds, so
    # the first retry is 2.5-5 s away and the second 5-10 s with the defaults.
    retry_base_seconds: float = Field(default=5, gt=0, le=3600)
    retry_max_seconds: float = Field(default=300, gt=0, le=86_400)
    # Development only: lets a job ask for injected provider failures (option
    # "fault_injection"). Off by default; the API refuses the option while it is off.
    enable_fault_injection: bool = False

    @model_validator(mode="after")
    def _visibility_timeout_exceeds_lease(self) -> "Settings":
        if self.celery_visibility_timeout_seconds <= self.lease_seconds:
            raise ValueError(
                "CELERY_VISIBILITY_TIMEOUT_SECONDS must be longer than LEASE_SECONDS, or Redis "
                "would redeliver messages for work that is still legitimately leased"
            )
        return self

    @model_validator(mode="after")
    def _recovery_timings(self) -> "Settings":
        if self.heartbeat_seconds > self.lease_seconds / 3:
            raise ValueError(
                "HEARTBEAT_INTERVAL_SECONDS must be at most a third of LEASE_SECONDS, so a missed "
                "heartbeat or two doesn't let a healthy worker's lease expire"
            )
        if self.recovery_interval_seconds > self.lease_seconds:
            raise ValueError(
                "RECOVERY_INTERVAL_SECONDS must not exceed LEASE_SECONDS, or recovery would notice "
                "a dead worker much later than its lease says"
            )
        if self.lost_message_grace_seconds < 2 * max(
            self.recovery_interval_seconds, self.dispatch_poll_seconds
        ):
            raise ValueError(
                "LOST_MESSAGE_GRACE_SECONDS must be at least twice RECOVERY_INTERVAL_SECONDS and "
                "DISPATCH_POLL_SECONDS, or messages still on their way would count as lost"
            )
        return self

    @model_validator(mode="after")
    def _retry_backoff_range(self) -> "Settings":
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("RETRY_MAX_SECONDS must be at least RETRY_BASE_SECONDS")
        return self

    @model_validator(mode="after")
    def _lease_safe_timeouts(self) -> "Settings":
        # A provider call can't renew the lease while it waits, so it must end well before the
        # lease would lapse (stages renew the lease after every call).
        if self.provider_timeout_seconds > self.lease_seconds / 2:
            raise ValueError("PROVIDER_TIMEOUT_SECONDS must be at most half of LEASE_SECONDS")
        if self.stub_delay_seconds >= self.provider_timeout_seconds:
            raise ValueError("STUB_DELAY_SECONDS must be shorter than PROVIDER_TIMEOUT_SECONDS")
        stage_limits = (
            max(
                self.pdf_validate_timeout_seconds + self.pdf_extract_timeout_seconds,
                self.zip_validate_timeout_seconds + self.zip_extract_timeout_seconds,
            )
            + self.summarize_timeout_seconds
        )
        if self.celery_task_time_limit_seconds <= stage_limits:
            raise ValueError(
                "CELERY_TASK_TIME_LIMIT_SECONDS must be longer than the stage time limits of "
                "each workflow combined, so a stage's own limit always fires first"
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

    @model_validator(mode="after")
    def _zip_limits_fit(self) -> "Settings":
        if self.zip_max_file_bytes > self.zip_max_total_bytes:
            raise ValueError("ZIP_MAX_FILE_BYTES must not exceed ZIP_MAX_TOTAL_BYTES")
        # The ZIP child holds one file in memory while counting its bytes (plus a copy when
        # writing it out), on top of Python itself. Too little memory would make every large
        # file fail as zip_too_complex instead of being skipped as too_large.
        needed_mb = 128 + 3 * self.zip_max_file_bytes // (1024 * 1024)
        if self.zip_sandbox_memory_mb < needed_mb:
            raise ValueError(
                f"ZIP_SANDBOX_MEMORY_MB is too small for ZIP_MAX_FILE_BYTES (needs {needed_mb})"
            )
        if self.zip_max_total_bytes > 64 * 1024**3:
            raise ValueError("ZIP_MAX_TOTAL_BYTES is limited to 64 GiB")
        self.repo_excluded_dir_names  # noqa: B018  (parses the list: fail at startup if invalid)
        return self

    @property
    def repo_excluded_dir_names(self) -> frozenset[str]:
        # Imported here, like the PDF checks above, so reading settings doesn't load workflows.
        from app.workflows.repo.filters import DEFAULT_EXCLUDED_DIRS, FilterRules

        return FilterRules.parse_excluded_dirs(self.repo_excluded_dirs or DEFAULT_EXCLUDED_DIRS)

    @property
    def heartbeat_seconds(self) -> float:
        """How often the heartbeat thread renews the lease: HEARTBEAT_INTERVAL_SECONDS, or a
        quarter of the lease."""
        return self.heartbeat_interval_seconds or self.lease_seconds / 4


@lru_cache
def get_settings() -> Settings:
    """Read settings once per process and reuse them."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
