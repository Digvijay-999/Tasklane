"""Test data builders. Each `make_*` inserts one valid row (flushes, doesn't commit).

Pass keyword overrides to change any column, including deliberately invalid values when a test
wants to prove a constraint rejects them.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Artifact, Job, JobAttempt, JobEvent, OutboxMessage, Upload
from app.domain.enums import (
    ArtifactType,
    AttemptTrigger,
    JobStatus,
    OutboxTask,
    UploadKind,
    UploadStatus,
    WorkflowType,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_hex(n: int = 0) -> str:
    """A well-formed (64 lowercase hex chars) fake SHA-256."""
    return f"{n:064x}"


def _add(session: Session, obj: Any) -> Any:
    session.add(obj)
    session.flush()  # send the INSERT now, so constraint errors surface at this call
    return obj


def make_upload(session: Session, **overrides: Any) -> Upload:
    upload_id = overrides.pop("id", None) or uuid.uuid4()
    values: dict[str, Any] = {
        "id": upload_id,
        "storage_path": f"uploads/{upload_id}/original",
        "original_filename": "sample.pdf",
        "kind": UploadKind.PDF,
        "content_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": sha256_hex(1),
        "status": UploadStatus.READY,
        "expires_at": utcnow() + timedelta(days=7),
    }
    values.update(overrides)
    return _add(session, Upload(**values))


def consistent_fields(status: JobStatus) -> dict[str, Any]:
    """Columns a job in `status` must have to satisfy the table's CHECK constraints."""
    now = utcnow()
    fields: dict[str, Any] = {}
    if status != JobStatus.QUEUED:
        fields["started_at"] = now
        fields["attempt_count"] = 1
    if status == JobStatus.RETRY_WAIT:
        fields["retry_at"] = now + timedelta(seconds=30)
    if status in (JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED):
        fields["cancel_requested_at"] = now
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
        fields["finished_at"] = now
    if status == JobStatus.FAILED:
        fields["error_class"] = "provider_timeout"
        fields["error_summary"] = "The summarizer timed out."
    return fields


def make_job(
    session: Session,
    *,
    upload: Upload | None = None,
    status: JobStatus = JobStatus.QUEUED,
    **overrides: Any,
) -> Job:
    upload = upload or make_upload(session)
    values: dict[str, Any] = {
        "workflow_type": WorkflowType.PDF_INGEST,
        "status": status,
        "upload_id": upload.id,
        "input_sha256": upload.sha256,
        "idempotency_key": f"key-{uuid.uuid4()}",
        "request_hash": sha256_hex(2),
        **consistent_fields(status),
    }
    values.update(overrides)
    return _add(session, Job(**values))


def make_attempt(session: Session, job: Job, **overrides: Any) -> JobAttempt:
    values: dict[str, Any] = {
        "job_id": job.id,
        "attempt_no": 1,
        "trigger": AttemptTrigger.INITIAL,
        "worker_id": "worker-test-1",
        "lease_expires_at": utcnow() + timedelta(seconds=60),
    }
    values.update(overrides)
    return _add(session, JobAttempt(**values))


def make_event(session: Session, job: Job, **overrides: Any) -> JobEvent:
    """Insert an event row directly (bypassing append_event) for constraint tests."""
    values: dict[str, Any] = {
        "job_id": job.id,
        "seq": 1,
        "event_type": "job.created",
        "payload": {},
    }
    values.update(overrides)
    return _add(session, JobEvent(**values))


def make_artifact(session: Session, job: Job, **overrides: Any) -> Artifact:
    artifact_type = overrides.pop("artifact_type", ArtifactType.SUMMARY)
    values: dict[str, Any] = {
        "job_id": job.id,
        "attempt_no": 1,
        "artifact_type": artifact_type,
        "storage_path": f"artifacts/{job.id}/{artifact_type}-{uuid.uuid4().hex[:8]}.json",
        "filename": f"{artifact_type}.json",
        "size_bytes": 10,
        "content_type": "application/json",
        "sha256": sha256_hex(3),
    }
    values.update(overrides)
    return _add(session, Artifact(**values))


def make_outbox(session: Session, job: Job, **overrides: Any) -> OutboxMessage:
    values: dict[str, Any] = {
        "job_id": job.id,
        "task_name": OutboxTask.RUN_JOB,
        "payload": {"job_id": str(job.id)},
    }
    values.update(overrides)
    return _add(session, OutboxMessage(**values))
