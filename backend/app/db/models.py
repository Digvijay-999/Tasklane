"""Database tables, defined as SQLAlchemy models.

The constraints declared here describe the schema; the migrations in `backend/migrations/` are
what actually create it. A test checks that the two agree. Each constraint's comment says which
reliability guarantee it protects.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.enums import (
    ArtifactType,
    AttemptOutcome,
    AttemptTrigger,
    JobStatus,
    OutboxTask,
    ProviderCallPurpose,
    UploadKind,
    UploadRejectionReason,
    UploadStatus,
    WorkflowType,
)
from app.domain.state_machine import MAX_ATTEMPTS_LIMIT, TERMINAL_STATUSES

SHA256_PATTERN = "^[0-9a-f]{64}$"


def one_of(column: str, values: type[StrEnum] | frozenset[StrEnum]) -> str:
    """SQL for `column IN ('a', 'b', ...)`, sorted so the output is stable."""
    quoted = ", ".join(f"'{value}'" for value in sorted(str(v) for v in values))
    return f"{column} IN ({quoted})"


def now() -> Any:
    return func.now()


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    storage_path: Mapped[str]  # generated, relative to the storage root; never user-supplied
    original_filename: Mapped[str]  # for display only
    kind: Mapped[str]
    content_type: Mapped[str]  # detected from the file, not trusted from the client
    # Size and hash are unknown while the file is still streaming in (status 'receiving'), so
    # they are nullable; ck_uploads_ready_has_content requires them once the upload is ready.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=UploadStatus.RECEIVING.value)
    rejection_reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=now())
    expires_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]

    __table_args__ = (
        # Two uploads can never share one file on disk.
        UniqueConstraint("storage_path"),
        CheckConstraint(one_of("kind", UploadKind), name="kind"),
        CheckConstraint(one_of("status", UploadStatus), name="status"),
        # No empty files recorded as valid input.
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="sha256_format"),
        CheckConstraint("char_length(original_filename) BETWEEN 1 AND 255", name="filename_length"),
        # Retention can't delete a file the moment it arrives.
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        # A file the UI says exists is still on disk, and vice versa.
        CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)", name="deleted_consistent"
        ),
        # A ready upload always has its size and hash, so jobs can rely on them.
        CheckConstraint(
            "status <> 'ready' OR (sha256 IS NOT NULL AND size_bytes IS NOT NULL)",
            name="ready_has_content",
        ),
        # A rejected upload says why; a reason is only kept on rejected (or later deleted) rows.
        CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL", name="rejected_has_reason"
        ),
        CheckConstraint(
            "status IN ('deleted', 'rejected') OR rejection_reason IS NULL",
            name="reason_only_when_rejected",
        ),
        CheckConstraint(one_of("rejection_reason", UploadRejectionReason), name="rejection_reason"),
        # Retention sweeps look for expired, not-yet-deleted uploads.
        Index(
            "ix_uploads_expires_at_pending",
            "expires_at",
            postgresql_where=text("status <> 'deleted'"),
        ),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_type: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=JobStatus.QUEUED.value)
    current_stage: Mapped[str | None]
    progress_percent: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id", ondelete="RESTRICT"))
    input_sha256: Mapped[str]  # copied from the upload at submission time
    options: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    idempotency_key: Mapped[str]
    request_hash: Mapped[str]  # SHA-256 of the normalized submission request
    attempt_count: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, server_default="3")
    manual_retry_count: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    retry_at: Mapped[datetime | None]  # when a retry_wait job becomes due (shown in the UI)
    cancel_requested_at: Mapped[datetime | None]  # the cancellation flag, plus when it was set
    error_class: Mapped[str | None]
    error_summary: Mapped[str | None]  # user-safe text only; tracebacks stay in logs
    last_event_seq: Mapped[int] = mapped_column(server_default="0")  # per-job event counter
    created_at: Mapped[datetime] = mapped_column(server_default=now())
    updated_at: Mapped[datetime] = mapped_column(server_default=now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        # Idempotent submission: two requests with the same key can never both create a job.
        UniqueConstraint("idempotency_key"),
        CheckConstraint(one_of("status", JobStatus), name="status"),
        CheckConstraint(one_of("workflow_type", WorkflowType), name="workflow_type"),
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="progress_range"),
        # Bounded retries: no code path can start more attempts than the budget allows.
        CheckConstraint(
            f"max_attempts BETWEEN 1 AND {MAX_ATTEMPTS_LIMIT} "
            "AND attempt_count BETWEEN 0 AND max_attempts",
            name="attempt_budget",
        ),
        # A job a worker may claim always has an attempt left (migration 0004), so the claim's
        # `attempt_count + 1` can never exceed the budget: a job out of attempts must be failed,
        # never queued or left waiting to retry.
        CheckConstraint(
            "status NOT IN ('queued', 'retry_wait') OR attempt_count < max_attempts",
            name="claimable_has_attempt_left",
        ),
        CheckConstraint("manual_retry_count >= 0", name="manual_retry_count_nonneg"),
        # A job looks finished exactly when it has a finish time.
        CheckConstraint(
            f"({one_of('status', TERMINAL_STATUSES)}) = (finished_at IS NOT NULL)",
            name="finished_iff_terminal",
        ),
        # Every failed job carries an explanation a reviewer can read.
        # (coalesce matters: a bare `char_length(NULL) > 0` is NULL, and CHECK treats NULL as a
        # pass, so a NULL summary would slip through.)
        CheckConstraint(
            "status <> 'failed' OR coalesce(char_length(error_summary), 0) > 0",
            name="failed_has_error",
        ),
        # A waiting job always knows when it will run again.
        CheckConstraint(
            "status <> 'retry_wait' OR retry_at IS NOT NULL", name="retry_wait_has_retry_at"
        ),
        CheckConstraint(
            "status NOT IN ('cancel_requested', 'cancelled') OR cancel_requested_at IS NOT NULL",
            name="cancel_has_timestamp",
        ),
        # ...and an active job never carries a stale cancel request (e.g. one left over from
        # before a manual retry), which would otherwise make the new run cancel itself.
        CheckConstraint(
            "status NOT IN ('queued', 'retry_wait', 'running') OR cancel_requested_at IS NULL",
            name="no_cancel_while_active",
        ),
        CheckConstraint(f"input_sha256 ~ '{SHA256_PATTERN}'", name="input_sha256_format"),
        CheckConstraint(f"request_hash ~ '{SHA256_PATTERN}'", name="request_hash_format"),
        CheckConstraint("char_length(idempotency_key) BETWEEN 1 AND 255", name="key_length"),
        CheckConstraint("last_event_seq >= 0", name="last_event_seq_nonneg"),
        CheckConstraint("jsonb_typeof(options) = 'object'", name="options_object"),
        Index("ix_jobs_upload_id", "upload_id"),  # makes the RESTRICT check on upload deletes fast
    )


# Indexes for GET /v1/jobs (migration 0002). The list is ordered newest first with the id as a
# tie-breaker, and pages with a keyset condition `(created_at, id) < (cursor values)`, so each
# index has exactly that order; the filtered variants put the filter column first.
Index("ix_jobs_created_at_id", Job.created_at.desc(), Job.id.desc())
Index("ix_jobs_status_created_at_id", Job.status, Job.created_at.desc(), Job.id.desc())
Index(
    "ix_jobs_workflow_type_created_at_id",
    Job.workflow_type,
    Job.created_at.desc(),
    Job.id.desc(),
)


class JobAttempt(Base):
    """One execution of a job by a worker. The lease and heartbeat live here."""

    __tablename__ = "job_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(SmallInteger)  # 1, 2, 3... never reused
    trigger: Mapped[str]
    worker_id: Mapped[str]
    task_id: Mapped[str | None]  # Celery message ID, for tracing
    started_at: Mapped[datetime] = mapped_column(server_default=now())
    heartbeat_at: Mapped[datetime] = mapped_column(server_default=now())
    lease_expires_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    outcome: Mapped[str | None]
    error_class: Mapped[str | None]
    error_summary: Mapped[str | None]

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(one_of("trigger", AttemptTrigger), name="trigger"),
        CheckConstraint(one_of("outcome", AttemptOutcome), name="outcome"),
        # An attempt is either still open, or closed with a recorded outcome.
        CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ended_iff_outcome"),
        CheckConstraint("lease_expires_at > started_at", name="lease_after_start"),
        CheckConstraint("heartbeat_at >= started_at", name="heartbeat_after_start"),
        # At most ONE live attempt per job. If Celery delivers a message twice and two workers
        # race to start the same job, the second insert fails here.
        Index(
            "uq_job_attempts_one_open_per_job",
            "job_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        # Lets the recovery loop find expired leases without scanning finished attempts.
        Index(
            "ix_job_attempts_open_lease_expires_at",
            "lease_expires_at",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    # Per-job sequence number (1, 2, 3, ...) allocated from jobs.last_event_seq while the job row
    # is locked. Together with job_id it is the primary key: each number exists once per job, and
    # the key doubles as the index SSE replay reads from.
    seq: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str]
    attempt_no: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=now())
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))

    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("event_type ~ '^[a-z_]+\\.[a-z_]+$'", name="event_type_format"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(SmallInteger)  # which attempt produced it
    artifact_type: Mapped[str]
    storage_path: Mapped[str]
    filename: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str]
    sha256: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=now())

    __table_args__ = (
        # No duplicate artifacts: a repeated stage replaces the row instead of adding another.
        UniqueConstraint("job_id", "artifact_type"),
        UniqueConstraint("storage_path"),
        CheckConstraint(one_of("artifact_type", ArtifactType), name="artifact_type"),
        CheckConstraint("size_bytes >= 0", name="size_nonneg"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="sha256_format"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
    )


class OutboxMessage(Base):
    """A task waiting to be published to the queue, written in the same transaction as the job."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)  # dispatch order
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    task_name: Mapped[str]
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=now())
    # The "available after" time. Retries are new rows with available_at = now() + backoff;
    # the dispatcher ignores rows until they are due.
    available_at: Mapped[datetime] = mapped_column(server_default=now())
    published_at: Mapped[datetime | None]
    dispatch_attempts: Mapped[int] = mapped_column(server_default="0")
    last_dispatch_error: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(one_of("task_name", OutboxTask), name="task_name"),
        CheckConstraint("dispatch_attempts >= 0", name="dispatch_attempts_nonneg"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        # No double-queuing: at most one unpublished message per job. Concurrent recovery,
        # automatic retry, and manual retry can't all queue the same job.
        Index(
            "uq_outbox_one_pending_per_job",
            "job_id",
            unique=True,
            postgresql_where=text("published_at IS NULL"),
        ),
        # The dispatcher's query: unpublished rows that are due, oldest first.
        Index(
            "ix_outbox_pending_available_at",
            "available_at",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index("ix_outbox_job_id", "job_id"),  # makes cascading job deletes fast
    )


class ProviderCall(Base):
    """One call to a summarization provider (success or failure), for usage and debugging."""

    __tablename__ = "provider_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(SmallInteger)
    stage: Mapped[str]
    chunk_id: Mapped[str | None]  # set for per-chunk calls
    purpose: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(server_default=now())
    latency_ms: Mapped[int]
    success: Mapped[bool] = mapped_column(Boolean)
    error_class: Mapped[str | None]
    input_chars: Mapped[int]
    output_chars: Mapped[int | None]
    # Token counts only when the provider reports them (never estimated).
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    total_tokens: Mapped[int | None]

    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(one_of("purpose", ProviderCallPurpose), name="purpose"),
        CheckConstraint("latency_ms >= 0", name="latency_nonneg"),
        CheckConstraint(
            "input_chars >= 0 AND (output_chars IS NULL OR output_chars >= 0)",
            name="chars_nonneg",
        ),
        CheckConstraint(
            "coalesce(prompt_tokens, 0) >= 0 AND coalesce(completion_tokens, 0) >= 0 "
            "AND coalesce(total_tokens, 0) >= 0",
            name="tokens_nonneg",
        ),
        # A call either succeeded, or failed with a recorded error class.
        CheckConstraint("success = (error_class IS NULL)", name="success_iff_no_error"),
        Index("ix_provider_calls_job_id", "job_id"),
    )


class StageCheckpoint(Base):
    """A stage that finished during some attempt of a job, and the files it wrote (migration
    0004). A retry skips the stage if the checkpoint still verifies; see app/domain/checkpoints.py.
    """

    __tablename__ = "stage_checkpoints"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(primary_key=True)  # one checkpoint per stage per job
    stage_version: Mapped[int] = mapped_column(SmallInteger)
    attempt_no: Mapped[int] = mapped_column(SmallInteger)  # the attempt that wrote it
    input_fingerprint: Mapped[str]
    # [{"path": ..., "size_bytes": ..., "sha256": ...}, ...]: every file the stage wrote
    outputs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB())
    created_at: Mapped[datetime] = mapped_column(server_default=now())
    updated_at: Mapped[datetime] = mapped_column(server_default=now())

    __table_args__ = (
        CheckConstraint("stage_version >= 1", name="stage_version_positive"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(f"input_fingerprint ~ '{SHA256_PATTERN}'", name="input_fingerprint_format"),
        # A checkpoint lists at least one file; there is nothing to verify otherwise. (CASE,
        # because jsonb_array_length() raises an error, not a violation, on a non-array, and SQL
        # doesn't promise to evaluate AND from left to right.)
        CheckConstraint(
            "CASE WHEN jsonb_typeof(outputs) = 'array' "
            "THEN jsonb_array_length(outputs) > 0 ELSE false END",
            name="outputs_nonempty",
        ),
    )
