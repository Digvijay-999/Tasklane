"""Creating and reading jobs.

Job creation is idempotent (see docs/api.md and app/domain/idempotency.py):
1. Validate the Idempotency-Key format.
2. If a job with this key exists: same request fingerprint -> return it (replay); different
   fingerprint -> 409. Replays skip upload validation, so a client gets its original job back
   even if the upload has expired since.
3. Otherwise validate the upload (locked FOR SHARE), then in ONE transaction:
   INSERT the job ... ON CONFLICT (idempotency_key) DO NOTHING, append the `job.created` event,
   and insert the outbox row. If the insert hit a conflict, a concurrent request with the same
   key won the race: re-read its job and treat this as a replay (or 409).
The unique index on idempotency_key is what guarantees correctness; step 2 is only a shortcut.

Cancel and manual retry (Phase 7) each run in ONE transaction that first locks the job row
(FOR UPDATE), then asks the state machine what to do from the status it sees. Two concurrent
requests therefore run one after the other, and the second sees the first one's result: a
repeated cancel is a no-op, a repeated retry gets 409 `not_failed`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Artifact, Job, JobAttempt, Upload
from app.db.repositories import jobs as job_rows
from app.db.repositories import uploads as upload_rows
from app.db.repositories.outbox import delete_pending, enqueue_run_job
from app.domain.enums import EventType, JobStatus, UploadStatus, WorkflowType
from app.domain.idempotency import KEY_FORMAT_DESCRIPTION, is_valid_key, request_hash
from app.domain.listing import JobListPosition
from app.domain.state_machine import (
    ATTEMPTS_PER_RUN,
    CancelDecision,
    ManualRetryDecision,
    can_manual_retry,
    cancel_decision,
)
from app.domain.uploads import required_upload_kind
from app.providers.faults import OPTION_NAME as FAULT_INJECTION_OPTION
from app.services.errors import ErrorCode, ServiceError


@dataclass(frozen=True)
class CreateJobCommand:
    idempotency_key: str | None
    workflow_type: WorkflowType
    upload_id: uuid.UUID
    # Options with defaults filled in: what the job will run with.
    resolved_options: dict[str, Any]
    # Exactly the fields the client sent (normalized types, no defaults): what gets hashed.
    sent_fields: dict[str, Any]


@dataclass(frozen=True)
class CreateJobResult:
    job: Job
    created: bool  # False when this was an idempotent replay


@dataclass(frozen=True)
class JobPage:
    items: list[Job]
    has_more: bool


@dataclass(frozen=True)
class JobDetail:
    """A job with its upload, attempts (by number) and artifacts (by type), for the detail view,
    plus what the user may do with it, decided by the state machine (so no client guesses)."""

    job: Job
    upload: Upload
    attempts: list[JobAttempt]
    artifacts: list[Artifact]
    database_now: datetime  # the database clock when this was read (lease expiry is judged by it)
    cancel: CancelDecision
    retry: ManualRetryDecision

    @property
    def open_attempt(self) -> JobAttempt | None:
        return next((a for a in self.attempts if a.ended_at is None), None)


class JobService:
    def __init__(
        self, session_factory: sessionmaker[Session], *, fault_injection_enabled: bool = False
    ) -> None:
        self.session_factory = session_factory
        self.fault_injection_enabled = fault_injection_enabled

    # --- Create ----------------------------------------------------------------------------------

    def create(self, command: CreateJobCommand) -> CreateJobResult:
        key = _require_valid_key(command.idempotency_key)
        fingerprint = request_hash(command.sent_fields)

        with self.session_factory.begin() as session:
            existing = job_rows.get_job_by_idempotency_key(session, key)
            if existing is not None:
                return _replay(existing, fingerprint)

            if FAULT_INJECTION_OPTION in command.resolved_options:
                self._require_fault_injection_enabled()
            upload = _validated_upload(session, command)
            job_id = uuid.uuid4()
            inserted = job_rows.insert_job_if_key_unused(
                session,
                {
                    "id": job_id,
                    "workflow_type": command.workflow_type.value,
                    "upload_id": upload.id,
                    "input_sha256": upload.sha256,
                    "options": command.resolved_options,
                    "idempotency_key": key,
                    "request_hash": fingerprint,
                    "max_attempts": ATTEMPTS_PER_RUN,
                },
            )
            if inserted is None:
                # A concurrent request with this key committed first. This new statement sees
                # its committed row.
                winner = job_rows.get_job_by_idempotency_key(session, key)
                if winner is None:  # it was deleted in between; vanishingly unlikely
                    raise RuntimeError("idempotency conflict but no job found for the key")
                return _replay(winner, fingerprint)

            job_rows.append_event(
                session,
                job_id,
                EventType.JOB_CREATED,
                {"workflow_type": command.workflow_type.value, "upload_id": str(upload.id)},
            )
            enqueue_run_job(session, job_id)
            job = session.get(Job, job_id)
            assert job is not None
        # Leaving the `begin()` block committed the job, event, and outbox row together.
        return CreateJobResult(job=job, created=True)

    def _require_fault_injection_enabled(self) -> None:
        # Never silently ignore the option: a job that asked for failures and quietly didn't get
        # them would make a retry test look like it passed.
        if not self.fault_injection_enabled:
            raise ServiceError(
                ErrorCode.FAULT_INJECTION_DISABLED,
                "This job asks for fault injection, but fault injection is disabled on this "
                "server. It is a development feature, enabled with ENABLE_FAULT_INJECTION=true.",
            )

    # --- Read ------------------------------------------------------------------------------------

    def list(
        self,
        *,
        statuses: list[str],
        workflow_types: list[str],
        after: JobListPosition | None,
        limit: int,
    ) -> JobPage:
        with self.session_factory() as session:
            rows = job_rows.list_jobs(
                session,
                statuses=statuses,
                workflow_types=workflow_types,
                after=after,
                limit=limit + 1,  # one extra row tells us whether another page exists
            )
        return JobPage(items=rows[:limit], has_more=len(rows) > limit)

    def detail(self, job_id: uuid.UUID) -> JobDetail:
        with self.session_factory() as session:
            found = job_rows.get_job_detail(session, job_id)
            database_now = session.execute(select(func.now())).scalar_one()
        if found is None:
            raise ServiceError(ErrorCode.JOB_NOT_FOUND, "No job exists with this id.")
        job = found.job
        return JobDetail(
            job=job,
            upload=found.upload,
            attempts=found.attempts,
            artifacts=found.artifacts,
            database_now=database_now,
            cancel=cancel_decision(JobStatus(job.status)),
            retry=can_manual_retry(JobStatus(job.status), job.error_class, job.attempt_count),
        )

    # --- Cancel and manual retry ------------------------------------------------------------------

    def cancel(self, job_id: uuid.UUID) -> bool:
        """Cancel a job (see cancel_job). Returns True if anything changed."""
        with self.session_factory.begin() as session:
            return cancel_job(session, job_id)

    def retry(self, job_id: uuid.UUID) -> None:
        """Manually retry a failed job (see retry_job)."""
        with self.session_factory.begin() as session:
            retry_job(session, job_id)


def cancel_job(session: Session, job_id: uuid.UUID) -> bool:
    """In the caller's transaction: lock the job and apply cancel_decision().

    - queued / retry_wait -> cancelled at once, and its unpublished outbox message is deleted. A
      message that was already published is dropped by the worker's claim (not claimable).
    - running -> cancel_requested; the worker stops at the next stage boundary.
    - cancel_requested or finished -> nothing changes (returns False).
    """
    job = job_rows.lock_job(session, job_id)
    if job is None:
        raise ServiceError(ErrorCode.JOB_NOT_FOUND, "No job exists with this id.")
    status = JobStatus(job.status)
    decision = cancel_decision(status)
    if decision.target is None:
        return False
    payload: dict[str, Any] = {"requested_by": "api"}
    if decision.target == JobStatus.CANCELLED:
        payload["outbox_removed"] = delete_pending(session, job_id)
    result = job_rows.transition_job(
        session, job_id, from_status=status, to_status=decision.target, event_payload=payload
    )
    if not result.applied:  # impossible while we hold the row lock
        raise RuntimeError(f"job {job_id} changed status while locked")
    return True


def retry_job(session: Session, job_id: uuid.UUID) -> None:
    """In the caller's transaction: lock the job, check can_manual_retry(), and reopen it.

    failed -> queued on the same job, with a fresh budget of ATTEMPTS_PER_RUN automatic attempts
    (max_attempts = attempt_count + 3) and a clean run (transition_job clears the error, progress,
    finish time and cancel timestamp), a `job.manual_retry` event, and a new outbox message.
    Checkpoints are kept: the first attempt resumes after the stages whose outputs still verify.
    """
    job = job_rows.lock_job(session, job_id)
    if job is None:
        raise ServiceError(ErrorCode.JOB_NOT_FOUND, "No job exists with this id.")
    decision = can_manual_retry(JobStatus(job.status), job.error_class, job.attempt_count)
    if not decision.allowed:
        assert decision.reason is not None and decision.message is not None
        raise ServiceError(
            ErrorCode.JOB_NOT_RETRYABLE,
            decision.message,
            {"reason": decision.reason.value, "status": job.status},
        )
    previous_error, attempt_count = job.error_class, job.attempt_count
    manual_retry_count = job.manual_retry_count + 1
    result = job_rows.transition_job(
        session,
        job_id,
        from_status=JobStatus.FAILED,
        to_status=JobStatus.QUEUED,
        changes={
            "max_attempts": decision.new_max_attempts,
            "manual_retry_count": manual_retry_count,
        },
        event_payload={"manual_retry_count": manual_retry_count},
    )
    if not result.applied:  # impossible while we hold the row lock
        raise RuntimeError(f"job {job_id} changed status while locked")
    job_rows.append_event(
        session,
        job_id,
        EventType.JOB_MANUAL_RETRY,
        {
            "manual_retry_count": manual_retry_count,
            "previous_error_class": previous_error,
            "attempt_count": attempt_count,
            "max_attempts": decision.new_max_attempts,
        },
    )
    enqueue_run_job(session, job_id)


def _require_valid_key(key: str | None) -> str:
    if key is None:
        raise ServiceError(
            ErrorCode.IDEMPOTENCY_KEY_MISSING,
            "The Idempotency-Key header is required when creating a job.",
            {"format": KEY_FORMAT_DESCRIPTION},
        )
    if not is_valid_key(key):
        raise ServiceError(
            ErrorCode.IDEMPOTENCY_KEY_INVALID,
            f"The Idempotency-Key header must be {KEY_FORMAT_DESCRIPTION}.",
            {"format": KEY_FORMAT_DESCRIPTION},
        )
    return key


def _replay(job: Job, fingerprint: str) -> CreateJobResult:
    if job.request_hash != fingerprint:
        raise ServiceError(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            "This Idempotency-Key was already used with a different request. "
            "Use a new key for a new request.",
            {"job_id": str(job.id)},
        )
    return CreateJobResult(job=job, created=False)


def _validated_upload(session: Session, command: CreateJobCommand) -> Upload:
    found = upload_rows.lock_upload_for_job(session, command.upload_id)
    if found is None:
        raise ServiceError(ErrorCode.UPLOAD_NOT_FOUND, "No upload exists with this upload_id.")
    upload = found.upload
    if upload.status != UploadStatus.READY:
        raise ServiceError(
            ErrorCode.UPLOAD_NOT_READY,
            f"The upload can't be used because its status is '{upload.status}'.",
            {"upload_status": upload.status},
        )
    if found.expired:
        raise ServiceError(
            ErrorCode.UPLOAD_EXPIRED,
            "The upload has expired. Upload the file again.",
            {"expired_at": upload.expires_at.isoformat()},
        )
    required = required_upload_kind(command.workflow_type)
    if upload.kind != required:
        raise ServiceError(
            ErrorCode.UPLOAD_KIND_MISMATCH,
            f"The {command.workflow_type.value} workflow needs a {required.value.upper()} "
            f"upload, but this upload is a {upload.kind.upper()}.",
            {"required_kind": required.value, "upload_kind": upload.kind},
        )
    return upload
