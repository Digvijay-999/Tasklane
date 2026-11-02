"""The fixed vocabularies stored in the database.

These enums are the source of truth. The database enforces the same lists with CHECK constraints
(written as frozen snapshots in the migrations), and a test verifies the two stay in sync.

`StrEnum` members *are* strings, so `JobStatus.QUEUED == "queued"` is True and they can be
written to the database directly.
"""

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowType(StrEnum):
    PDF_INGEST = "pdf_ingest"
    REPO_INDEX = "repo_index"


class UploadKind(StrEnum):
    PDF = "pdf"
    ZIP = "zip"


class UploadStatus(StrEnum):
    RECEIVING = "receiving"  # row exists, file still being written (lets cleanup find partials)
    READY = "ready"
    REJECTED = "rejected"
    DELETED = "deleted"  # file removed by retention; the row stays because jobs reference it


class AttemptTrigger(StrEnum):
    """Why an attempt was started."""

    INITIAL = "initial"
    AUTO_RETRY = "auto_retry"
    RECOVERY = "recovery"
    MANUAL_RETRY = "manual_retry"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"
    CANCELLED = "cancelled"
    LEASE_EXPIRED = "lease_expired"


class ArtifactType(StrEnum):
    MANIFEST = "manifest"
    SUMMARY = "summary"
    CHUNKS = "chunks"


class OutboxTask(StrEnum):
    RUN_JOB = "run_job"


class EventType(StrEnum):
    """Event names follow `<subject>.<verb>`; the database checks that format, not this list."""

    JOB_CREATED = "job.created"
    JOB_STATUS_CHANGED = "job.status_changed"
    JOB_CANCEL_SUPERSEDED = "job.cancel_superseded"


class ErrorClass(StrEnum):
    """Error classes known so far. Phase 5 adds the transient/provider classes."""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_INPUT = "unsupported_input"
    LEASE_EXPIRED = "lease_expired"
