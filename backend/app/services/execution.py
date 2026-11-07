"""Running a job inside a worker: claim it, run its stages, record the outcome.

Called by the Celery task (app/queue/tasks.py) with nothing but a job id. Celery may deliver the
same message more than once, and a message may arrive long after it was sent, so the first step
is always a **claim** against the database, which is the source of truth:

1. Claim (one transaction): the job must be `queued`, or `retry_wait` with its retry due. A
   conditional update moves it to `running` and counts the attempt; an attempt row with a lease
   is inserted; `attempt.started` is recorded. Anything else (already running, finished,
   cancelled, not due, lost the race to another worker) means the message is a duplicate or
   stale: it is **dropped without writing anything**.
2. Stages: every report from a stage (progress, keep-alive, provider call, artifact) renews the
   lease in the same short transaction as its write. If the attempt was closed by someone else
   (recovery, Phase 7), the worker is *fenced out*: it stops without writing anything further.
3. Finish (one transaction): close the attempt and move the job to `succeeded`, or record the
   failure.

Failure handling (Phase 5 Part A): a JobError (an expected failure such as an encrypted PDF) fails
the job with its own error class and safe message; any other exception fails it with
`internal_error`. Nothing is retried yet (Part B adds transient errors and retries). If even the
failure can't be recorded (e.g. the database is down), or the worker process dies, the job stays
`running` with an open attempt until its lease expires; recovering it is Phase 7's job.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Job, Upload
from app.db.repositories import artifacts as artifact_rows
from app.db.repositories import attempts as attempt_rows
from app.db.repositories import jobs as job_rows
from app.db.repositories import provider_calls as provider_call_rows
from app.domain.enums import (
    AttemptOutcome,
    AttemptTrigger,
    EventType,
    JobStatus,
    WorkflowType,
)
from app.domain.errors import ERRORS, JobError
from app.domain.state_machine import (
    CLAIMABLE_STATUSES,
    FailureKind,
    status_after_failure,
    status_after_success,
)
from app.providers.base import ProviderCallRecord
from app.storage.local import LocalStorage
from app.storage.workspace import JobWorkspace
from app.workflows.base import ArtifactOutput, JobInput, Stage, UploadInput

logger = logging.getLogger(__name__)

INTERNAL_ERROR = "internal_error"
INTERNAL_ERROR_SUMMARY = ERRORS[INTERNAL_ERROR].message
EXECUTING_STATUSES = frozenset({JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED})


class AttemptFencedError(Exception):
    """This worker's attempt was closed by someone else (or the job left `running`): stop."""


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DROPPED = "dropped"  # duplicate or stale message: nothing was written
    FENCED = "fenced"  # lost ownership mid-run: stopped without further writes


class DropReason(StrEnum):
    JOB_NOT_FOUND = "job_not_found"
    NOT_CLAIMABLE = "not_claimable"  # running, finished, cancelled...
    RETRY_NOT_DUE = "retry_not_due"
    # The conditional update matched nothing. With the job row locked during the claim this is a
    # safety net; a concurrent claimer normally shows up as NOT_CLAIMABLE (job already running).
    CLAIM_LOST = "claim_lost"


@dataclass(frozen=True)
class RunResult:
    outcome: RunOutcome
    drop_reason: DropReason | None = None
    job_status: JobStatus | None = None  # for DROPPED: the status that caused it
    attempt_no: int | None = None
    error_class: str | None = None  # for FAILED


@dataclass(frozen=True)
class ClaimedAttempt:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    workflow_type: WorkflowType
    job_input: JobInput


@dataclass(frozen=True)
class _Failure:
    error_class: str
    summary: str
    stage: str | None


class JobExecutionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str,
        lease: timedelta,
        stages_for: Callable[[WorkflowType], list[Stage]],
        storage: LocalStorage | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.lease = lease
        self.stages_for = stages_for
        self.storage = storage

    # --- Entry point -----------------------------------------------------------------------------

    def run(self, job_id: uuid.UUID, task_id: str | None = None) -> RunResult:
        claim = self.claim(job_id, task_id)
        if isinstance(claim, RunResult):  # dropped
            logger.info(
                "message dropped",
                extra={
                    "job_id": str(job_id),
                    "reason": claim.drop_reason,
                    "job_status": claim.job_status,
                    "worker_id": self.worker_id,
                    "task_id": task_id,
                },
            )
            return claim

        log_extra = {
            "job_id": str(job_id),
            "attempt_no": claim.attempt_no,
            "worker_id": self.worker_id,
        }
        logger.info("job claimed", extra=log_extra)
        progress = _StageTracker()
        try:
            self._run_stages(claim, progress)
            self._succeed(claim, progress)
        except AttemptFencedError:
            logger.warning("attempt fenced out; stopping without further writes", extra=log_extra)
            return RunResult(RunOutcome.FENCED, attempt_no=claim.attempt_no)
        except JobError as error:
            # An expected failure (e.g. an encrypted PDF): its class and safe message are recorded.
            logger.warning(
                "job failed",
                extra={
                    **log_extra,
                    "error_class": error.error_class,
                    "stage": progress.current_stage,
                },
            )
            failure = _Failure(error.error_class, error.safe_message, progress.current_stage)
            return RunResult(
                self._fail(claim, failure),
                attempt_no=claim.attempt_no,
                error_class=error.error_class,
            )
        except Exception:
            # Unexpected: the traceback goes to the logs; the database only gets a safe summary.
            logger.exception("job failed with an unexpected error", extra=log_extra)
            failure = _Failure(INTERNAL_ERROR, INTERNAL_ERROR_SUMMARY, progress.current_stage)
            return RunResult(
                self._fail(claim, failure), attempt_no=claim.attempt_no, error_class=INTERNAL_ERROR
            )
        logger.info("job succeeded", extra=log_extra)
        return RunResult(RunOutcome.SUCCEEDED, attempt_no=claim.attempt_no)

    # --- Claim -----------------------------------------------------------------------------------

    def claim(self, job_id: uuid.UUID, task_id: str | None) -> ClaimedAttempt | RunResult:
        with self.session_factory.begin() as session:
            # FOR UPDATE: lock the job row for this short transaction, so the status, attempt
            # count, and trigger read here can't change before the update below. A concurrent
            # claimer waits here, then sees the job already `running` and drops its message.
            row = session.execute(
                select(
                    Job.status,
                    Job.attempt_count,
                    Job.workflow_type,
                    Job.options,
                    Job.upload_id,
                )
                .where(Job.id == job_id)
                .with_for_update(of=Job)
            ).one_or_none()
            if row is None:
                return RunResult(RunOutcome.DROPPED, DropReason.JOB_NOT_FOUND)
            status = JobStatus(row.status)
            if status not in CLAIMABLE_STATUSES:
                return RunResult(RunOutcome.DROPPED, DropReason.NOT_CLAIMABLE, job_status=status)
            workflow_type = WorkflowType(row.workflow_type)  # validate before changing anything
            upload = session.get(Upload, row.upload_id)

            attempt_no = row.attempt_count + 1
            if status == JobStatus.RETRY_WAIT:
                trigger = AttemptTrigger.AUTO_RETRY
            elif row.attempt_count == 0:
                trigger = AttemptTrigger.INITIAL
            else:
                trigger = AttemptTrigger.MANUAL_RETRY  # queued again after an earlier failure

            result = job_rows.transition_job(
                session,
                job_id,
                from_status=status,
                to_status=JobStatus.RUNNING,
                changes={"attempt_count": attempt_no},
                event_payload={"attempt_no": attempt_no, "worker_id": self.worker_id},
                attempt_no=attempt_no,
                only_if_retry_due=status == JobStatus.RETRY_WAIT,
            )
            if not result.applied:
                reason = (
                    DropReason.RETRY_NOT_DUE
                    if result.status == JobStatus.RETRY_WAIT
                    else DropReason.CLAIM_LOST
                )
                return RunResult(RunOutcome.DROPPED, reason, job_status=result.status)

            attempt_id = attempt_rows.insert_attempt(
                session,
                job_id=job_id,
                attempt_no=attempt_no,
                trigger=trigger,
                worker_id=self.worker_id,
                task_id=task_id,
                lease=self.lease,
            )
            job_rows.append_event(
                session,
                job_id,
                EventType.ATTEMPT_STARTED,
                {"attempt_no": attempt_no, "trigger": trigger.value, "worker_id": self.worker_id},
                attempt_no=attempt_no,
            )
            job_input = JobInput(
                job_id=job_id,
                attempt_no=attempt_no,
                workflow_type=workflow_type.value,
                upload=UploadInput(
                    upload_id=upload.id,
                    storage_path=upload.storage_path,
                    sha256=upload.sha256 or "",
                    size_bytes=upload.size_bytes or 0,
                    original_filename=upload.original_filename,
                ),
                options=dict(row.options or {}),
            )
        return ClaimedAttempt(
            job_id=job_id,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            workflow_type=workflow_type,
            job_input=job_input,
        )

    # --- Stages ----------------------------------------------------------------------------------

    def _run_stages(self, claim: ClaimedAttempt, tracker: "_StageTracker") -> None:
        stages = self.stages_for(claim.workflow_type)
        weights = [max(1, int(getattr(stage, "weight", 1))) for stage in stages]
        total = sum(weights) or 1
        done = 0
        for stage, weight in zip(stages, weights, strict=True):
            tracker.current_stage = stage.name
            context = _AttemptContext(
                self, claim, tracker, stage.name, done / total, weight / total
            )
            context.record(EventType.STAGE_STARTED, {"stage": stage.name}, stage_percent=0)
            started = time.monotonic()
            stage.run(context)
            duration_ms = round((time.monotonic() - started) * 1000)
            context.record(
                EventType.STAGE_COMPLETED,
                {"stage": stage.name, "duration_ms": duration_ms},
                stage_percent=100,
            )
            done += weight

    # --- Finish ----------------------------------------------------------------------------------

    def _succeed(self, claim: ClaimedAttempt, tracker: "_StageTracker") -> None:
        """Close the attempt, register the artifacts, and finish the job in ONE transaction, so
        artifacts become visible exactly when the job succeeds (never for a failed or running
        job)."""
        with self.session_factory.begin() as session:
            if not attempt_rows.close_attempt(session, claim.attempt_id, AttemptOutcome.SUCCEEDED):
                raise AttemptFencedError
            for artifact in tracker.artifacts.values():
                artifact_rows.upsert_artifact(
                    session,
                    job_id=claim.job_id,
                    attempt_no=claim.attempt_no,
                    artifact_type=artifact.artifact_type,
                    storage_path=artifact.relative_path,
                    filename=artifact.filename,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                )
            if tracker.current_stage:
                job_rows.update_progress(
                    session, claim.job_id, stage=tracker.current_stage, percent=100
                )
            # status_after_success: a pending cancel request is superseded by the success.
            self._move_to_outcome(session, claim, status_after_success)

    def _fail(self, claim: ClaimedAttempt, failure: _Failure) -> RunOutcome:
        """Record a failed attempt and fail the job (Part A: every failure is permanent)."""
        try:
            with self.session_factory.begin() as session:
                closed = attempt_rows.close_attempt(
                    session,
                    claim.attempt_id,
                    AttemptOutcome.FAILED_PERMANENT,
                    error_class=failure.error_class,
                    error_summary=failure.summary,
                )
                if not closed:
                    raise AttemptFencedError
                if failure.stage:
                    job_rows.append_event(
                        session,
                        claim.job_id,
                        EventType.STAGE_FAILED,
                        {"stage": failure.stage, "error_class": failure.error_class},
                        attempt_no=claim.attempt_no,
                    )
                self._move_to_outcome(
                    session,
                    claim,
                    lambda current: status_after_failure(
                        current, FailureKind.PERMANENT, attempt_count=0, max_attempts=0
                    ),
                    changes={"error_class": failure.error_class, "error_summary": failure.summary},
                )
        except AttemptFencedError:
            return RunOutcome.FENCED
        return RunOutcome.FAILED

    def _move_to_outcome(
        self,
        session: Session,
        claim: ClaimedAttempt,
        decide: Callable[[JobStatus], JobStatus],
        changes: dict[str, object] | None = None,
    ) -> None:
        """Transition the job from whatever executing status it is in now to `decide(status)`.

        The job may move between `running` and `cancel_requested` at any moment (a user's cancel
        request), so if the conditional update loses, re-read and try again a few times.
        """
        for _ in range(3):
            current = session.execute(select(Job.status).where(Job.id == claim.job_id)).scalar_one()
            status = JobStatus(current)
            if status not in EXECUTING_STATUSES:
                raise AttemptFencedError  # someone else already finished or moved the job
            result = job_rows.transition_job(
                session,
                claim.job_id,
                from_status=status,
                to_status=decide(status),
                changes=changes,
                event_payload={"attempt_no": claim.attempt_no},
                attempt_no=claim.attempt_no,
            )
            if result.applied:
                return
        raise AttemptFencedError


class _StageTracker:
    """Per-run state: which stage is running (so a failure can name it) and the artifacts the
    stages produced (registered only when the job succeeds)."""

    def __init__(self) -> None:
        self.current_stage: str | None = None
        self.artifacts: dict[str, ArtifactOutput] = {}  # by artifact type; a re-save replaces


class _AttemptContext:
    """StageContext for one stage of one attempt. Every write renews the lease (fencing check)
    in the same short transaction."""

    def __init__(
        self,
        service: JobExecutionService,
        claim: ClaimedAttempt,
        tracker: _StageTracker,
        stage_name: str,
        progress_start: float,
        progress_share: float,
    ) -> None:
        self.service = service
        self.claim = claim
        self.tracker = tracker
        self.stage_name = stage_name
        self.progress_start = progress_start
        self.progress_share = progress_share
        self.job = claim.job_input

    @property
    def workspace(self) -> JobWorkspace:
        if self.service.storage is None:
            raise RuntimeError("this execution service was created without storage")
        return JobWorkspace(self.service.storage, self.claim.job_id)

    # --- StageContext ---------------------------------------------------------------------------

    def report_progress(self, percent: int) -> None:
        percent = max(0, min(100, percent))
        self.record(
            EventType.STAGE_PROGRESS, {"stage": self.stage_name, "percent": percent}, percent
        )

    def keep_alive(self) -> None:
        with self.service.session_factory.begin() as session:
            self._renew(session)

    def record_provider_call(self, call: ProviderCallRecord) -> None:
        usage = call.usage
        with self.service.session_factory.begin() as session:
            self._renew(session)
            provider_call_rows.insert_provider_call(
                session,
                job_id=self.claim.job_id,
                attempt_no=self.claim.attempt_no,
                stage=self.stage_name,
                purpose=call.purpose.value,
                provider=call.provider,
                model=call.model,
                latency_ms=call.latency_ms,
                success=call.success,
                input_chars=call.input_chars,
                output_chars=call.output_chars,
                chunk_id=call.chunk_id,
                error_class=call.error_class,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
            )

    def save_artifact(self, artifact: ArtifactOutput) -> None:
        """Remember the artifact; its row is written when the job succeeds (see _succeed)."""
        self.keep_alive()  # fencing check: a fenced-out worker stops here
        self.tracker.artifacts[artifact.artifact_type] = artifact

    # --- Internals ------------------------------------------------------------------------------

    def record(self, event_type: EventType, payload: dict[str, object], stage_percent: int) -> None:
        """One short transaction: renew the lease (fencing check), update progress, add event."""
        overall = round((self.progress_start + self.progress_share * stage_percent / 100) * 100)
        with self.service.session_factory.begin() as session:
            self._renew(session)
            if not job_rows.update_progress(
                session, self.claim.job_id, stage=self.stage_name, percent=overall
            ):
                raise AttemptFencedError
            job_rows.append_event(
                session, self.claim.job_id, event_type, payload, attempt_no=self.claim.attempt_no
            )

    def _renew(self, session: Session) -> None:
        if not attempt_rows.renew_lease(session, self.claim.attempt_id, self.service.lease):
            raise AttemptFencedError
