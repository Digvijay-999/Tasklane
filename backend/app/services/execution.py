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
   Before a stage runs, its checkpoint from an earlier attempt is verified; if it holds, the
   stage is skipped (`stage.skipped`) and the retry resumes after it (app/domain/checkpoints.py).
3. Finish (one transaction): close the attempt and move the job to `succeeded`, or record the
   failure.

Failure handling: every exception is classified (`_classify`):
- a JobError carries its own class (an encrypted PDF, a provider timeout, ...);
- database connection trouble -> `database_unavailable`; deadlocks and serialization failures
  -> `database_conflict`; disk trouble -> `storage_unavailable` (all transient);
- anything else -> `internal_error` (permanent: retrying an unknown bug would only repeat it).
A transient failure moves the job to `retry_wait` with a backoff while attempts remain, in the
same transaction as the retry's outbox row (`_fail`). If even the failure can't be recorded (the
database is down), or the worker process dies, the job stays `running` with an open attempt until
its lease expires; recovering it is Phase 7's job.
"""

import errno
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Job, Upload
from app.db.repositories import artifacts as artifact_rows
from app.db.repositories import attempts as attempt_rows
from app.db.repositories import checkpoints as checkpoint_rows
from app.db.repositories import jobs as job_rows
from app.db.repositories import provider_calls as provider_call_rows
from app.db.repositories.outbox import enqueue_retry
from app.domain.checkpoints import (
    Checkpoint,
    OutputFile,
    ReuseDecision,
    input_fingerprint,
    reuse_decision,
)
from app.domain.enums import (
    AttemptOutcome,
    AttemptTrigger,
    EventType,
    JobStatus,
    WorkflowType,
)
from app.domain.errors import ERRORS, ErrorKind, JobError, gave_up_summary
from app.domain.retry import RetryPolicy
from app.domain.state_machine import (
    CLAIMABLE_STATUSES,
    FailureKind,
    status_after_failure,
    status_after_success,
)
from app.providers.base import ProviderCallRecord
from app.providers.faults import fault_plan
from app.storage.local import LocalStorage
from app.storage.workspace import JobWorkspace
from app.workflows.base import ArtifactOutput, JobInput, Stage, UploadInput

logger = logging.getLogger(__name__)

INTERNAL_ERROR = "internal_error"
INTERNAL_ERROR_SUMMARY = ERRORS[INTERNAL_ERROR].message
EXECUTING_STATUSES = frozenset({JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED})

# Postgres SQLSTATEs meaning "this transaction lost a race; running it again will work".
DEADLOCK_DETECTED = "40P01"
SERIALIZATION_FAILURE = "40001"
# Disk trouble that may clear up: full, I/O error, read-only remount, quota, busy.
FAIL_TRANSACTION_TRIES = 3
STORAGE_ERRNOS = frozenset({errno.ENOSPC, errno.EIO, errno.EROFS, errno.EDQUOT, errno.EAGAIN})


class AttemptFencedError(Exception):
    """This worker's attempt was closed by someone else (or the job left `running`): stop."""


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"  # transient failure: the job waits in retry_wait
    CANCELLED = "cancelled"  # transient failure while a cancel request was pending
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
    error_class: str | None = None  # for FAILED, RETRY_SCHEDULED, CANCELLED


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
    transient: bool


class JobExecutionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str,
        lease: timedelta,
        stages_for: Callable[[WorkflowType], list[Stage]],
        storage: LocalStorage | None = None,
        retry_policy: RetryPolicy | None = None,
        fault_injection_enabled: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.lease = lease
        self.stages_for = stages_for
        self.storage = storage
        self.retry_policy = retry_policy or RetryPolicy()
        self.fault_injection_enabled = fault_injection_enabled

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
            progress.finishing = True  # a failure from here on is not any stage's fault
            self._succeed(claim, progress)
        except AttemptFencedError:
            logger.warning("attempt fenced out; stopping without further writes", extra=log_extra)
            return RunResult(RunOutcome.FENCED, attempt_no=claim.attempt_no)
        except Exception as exc:
            failure = _classify(exc, None if progress.finishing else progress.current_stage)
            failure_log = {
                **log_extra,
                "error_class": failure.error_class,
                "stage": failure.stage,
                "transient": failure.transient,
            }
            if failure.error_class == INTERNAL_ERROR:
                # Unexpected: the traceback goes to the logs; the database gets a safe summary.
                logger.exception("job attempt failed with an unexpected error", extra=failure_log)
            else:
                logger.warning(
                    "job attempt failed",
                    extra={**failure_log, "exception_type": type(exc).__name__},
                )
            return RunResult(
                self._fail(claim, failure),
                attempt_no=claim.attempt_no,
                error_class=failure.error_class,
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
        if fault_plan(claim.job_input.options) and not self.fault_injection_enabled:
            # The API accepted the option, so the flag was on there; it is off here. Fail clearly
            # instead of quietly running without the faults the job asked for.
            raise JobError("fault_injection_disabled")
        stages = self.stages_for(claim.workflow_type)
        weights = [max(1, int(getattr(stage, "weight", 1))) for stage in stages]
        total = sum(weights) or 1
        checkpoints = self._load_checkpoints(claim, stages)
        upstream: list[OutputFile] = []  # files written by the stages so far, in order
        earlier_stage_ran = False
        done = 0
        for stage, weight in zip(stages, weights, strict=True):
            tracker.current_stage = stage.name
            context = _AttemptContext(
                self, claim, tracker, stage.name, done / total, weight / total
            )
            done += weight
            plan = self._checkpoint_plan(stage, claim, context, upstream)
            if plan is not None:
                decision = reuse_decision(
                    checkpoints.get(stage.name),
                    stage_version=plan.stage_version,
                    fingerprint=plan.fingerprint,
                    expected_paths=plan.output_paths,
                    earlier_stage_ran=earlier_stage_ran,
                    describe=context.describe_file,
                )
                if decision == ReuseDecision.REUSE:
                    context.record(
                        EventType.STAGE_SKIPPED,
                        {"stage": stage.name, "reason": "checkpoint_reused"},
                        stage_percent=100,
                    )
                    upstream.extend(checkpoints[stage.name].outputs)
                    continue
                if decision != ReuseDecision.NO_CHECKPOINT:
                    logger.info(
                        "checkpoint not reused; running the stage again",
                        extra={
                            "job_id": str(claim.job_id),
                            "stage": stage.name,
                            "reason": str(decision),
                        },
                    )
            earlier_stage_ran = True
            context.record(EventType.STAGE_STARTED, {"stage": stage.name}, stage_percent=0)
            started = time.monotonic()
            stage.run(context)
            duration_ms = round((time.monotonic() - started) * 1000)
            completed_checkpoint = None
            if plan is not None:
                outputs = [context.describe_file(path) for path in plan.output_paths]
                written = [output for output in outputs if output is not None]
                if len(written) != len(outputs):
                    raise RuntimeError(f"stage {stage.name} did not write all of its outputs")
                upstream.extend(written)
                completed_checkpoint = (plan, written)
            context.record(
                EventType.STAGE_COMPLETED,
                {"stage": stage.name, "duration_ms": duration_ms},
                stage_percent=100,
                checkpoint=completed_checkpoint,
            )

    def _load_checkpoints(
        self, claim: ClaimedAttempt, stages: list[Stage]
    ) -> dict[str, Checkpoint]:
        if claim.attempt_no == 1 or not any(_checkpointable(stage) for stage in stages):
            return {}  # a first attempt has nothing to resume from
        with self.session_factory() as session:
            return checkpoint_rows.get_checkpoints(session, claim.job_id)

    def _checkpoint_plan(
        self,
        stage: Stage,
        claim: ClaimedAttempt,
        context: "_AttemptContext",
        upstream: list[OutputFile],
    ) -> "_CheckpointPlan | None":
        """What this stage's checkpoint must match, or None if the stage isn't checkpointed
        (no `stage_version`: e.g. the dummy stage and write_artifacts)."""
        if not _checkpointable(stage) or self.storage is None:
            return None
        version = int(stage.stage_version)  # type: ignore[attr-defined]
        params = stage.fingerprint_params(claim.job_input)  # type: ignore[attr-defined]
        return _CheckpointPlan(
            stage_version=version,
            fingerprint=input_fingerprint(
                stage=stage.name, stage_version=version, params=params, upstream=upstream
            ),
            output_paths=list(stage.output_paths(context.workspace)),  # type: ignore[attr-defined]
        )

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
        """Record a failed attempt and decide what happens to the job, in ONE transaction:

        1. close our attempt (fencing: if it was already closed, write nothing at all);
        2. append `stage.failed`, then lock the job row and read its status and attempt budget;
        3. status_after_failure(): transient with attempts left -> retry_wait; transient with none
           left -> failed ("... Gave up after N attempts."); permanent -> failed; transient while
           a cancel is pending -> cancelled;
        4. for retry_wait: the job's retry_at and the retry's outbox available_at get the SAME
           timestamp, taken from the database clock in this transaction, so the message can
           never arrive before the job is due (it would be dropped as "retry not due").
        Lock order: attempt, then job (the same as progress reports; Phase 7 recovery too).

        A deadlock or serialization failure rolls the whole transaction back, so it is safe to
        run again; after a few tries the error reaches the task layer.
        """
        kind = FailureKind.TRANSIENT if failure.transient else FailureKind.PERMANENT
        for remaining in range(FAIL_TRANSACTION_TRIES - 1, -1, -1):
            try:
                return self._record_failure(claim, failure, kind)
            except DBAPIError as exc:
                if remaining == 0 or not _is_conflict(exc):
                    raise
                logger.warning(
                    "recording a failure hit a database conflict; trying again",
                    extra={"job_id": str(claim.job_id), "attempt_no": claim.attempt_no},
                )
        raise AssertionError("unreachable")

    def _record_failure(
        self, claim: ClaimedAttempt, failure: _Failure, kind: FailureKind
    ) -> RunOutcome:
        outcome = (
            AttemptOutcome.FAILED_TRANSIENT
            if failure.transient
            else AttemptOutcome.FAILED_PERMANENT
        )
        try:
            with self.session_factory.begin() as session:
                closed = attempt_rows.close_attempt(
                    session,
                    claim.attempt_id,
                    outcome,
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
                        {
                            "stage": failure.stage,
                            "error_class": failure.error_class,
                            "retryable": failure.transient,
                        },
                        attempt_no=claim.attempt_no,
                    )
                return self._apply_failure(session, claim, failure, kind)
        except AttemptFencedError:
            return RunOutcome.FENCED

    def _apply_failure(
        self, session: Session, claim: ClaimedAttempt, failure: _Failure, kind: FailureKind
    ) -> RunOutcome:
        # stage.failed already locked the job row (it bumps the event counter); FOR UPDATE also
        # covers failures outside any stage. The loop is defensive: if the conditional update
        # still loses to a concurrent status change, re-read and decide again.
        for _ in range(3):
            row = session.execute(
                select(Job.status, Job.attempt_count, Job.max_attempts)
                .where(Job.id == claim.job_id)
                .with_for_update()
            ).one()
            status = JobStatus(row.status)
            if status not in EXECUTING_STATUSES:
                raise AttemptFencedError  # someone else already finished or moved the job
            target = status_after_failure(status, kind, row.attempt_count, row.max_attempts)
            changes: dict[str, Any] = {
                "error_class": failure.error_class,
                "error_summary": failure.summary,
            }
            payload: dict[str, Any] = {
                "attempt_no": claim.attempt_no,
                "error_class": failure.error_class,
            }
            retry_at = None
            if target == JobStatus.RETRY_WAIT:
                delay = self.retry_policy.delay_seconds(claim.attempt_no)
                # now() is frozen for the whole transaction: one database-clock instant.
                retry_at = session.execute(
                    select(func.now() + timedelta(seconds=delay))
                ).scalar_one()
                changes["retry_at"] = retry_at
                payload["retry_at"] = retry_at.isoformat()
                payload["retry_in_seconds"] = round(delay, 3)
            elif target == JobStatus.FAILED and failure.transient:
                changes["error_summary"] = gave_up_summary(failure.summary, row.attempt_count)
                payload["gave_up_after_attempts"] = row.attempt_count
            result = job_rows.transition_job(
                session,
                claim.job_id,
                from_status=status,
                to_status=target,
                changes=changes,
                event_payload=payload,
                attempt_no=claim.attempt_no,
            )
            if not result.applied:
                continue
            if retry_at is not None:
                enqueue_retry(session, claim.job_id, available_at=retry_at)
                return RunOutcome.RETRY_SCHEDULED
            return RunOutcome.CANCELLED if target == JobStatus.CANCELLED else RunOutcome.FAILED
        raise AttemptFencedError

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
        self.finishing = False  # every stage is done; the success is being recorded
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

    def describe_file(self, path: str) -> OutputFile | None:
        """A file's current size and SHA-256, or None if it doesn't exist."""
        workspace = self.workspace
        if not workspace.exists(path):
            return None
        return OutputFile(path, workspace.size(path), workspace.sha256(path))

    def record(
        self,
        event_type: EventType,
        payload: dict[str, object],
        stage_percent: int,
        checkpoint: "tuple[_CheckpointPlan, list[OutputFile]] | None" = None,
    ) -> None:
        """One short transaction: renew the lease (fencing check), update progress, add event,
        and, when a stage completes, record its checkpoint."""
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
            if checkpoint is not None:
                plan, outputs = checkpoint
                checkpoint_rows.upsert_checkpoint(
                    session,
                    job_id=self.claim.job_id,
                    stage=self.stage_name,
                    stage_version=plan.stage_version,
                    attempt_no=self.claim.attempt_no,
                    input_fingerprint=plan.fingerprint,
                    outputs=outputs,
                )

    def _renew(self, session: Session) -> None:
        if not attempt_rows.renew_lease(session, self.claim.attempt_id, self.service.lease):
            raise AttemptFencedError


@dataclass(frozen=True)
class _CheckpointPlan:
    stage_version: int
    fingerprint: str
    output_paths: list[str]


def _checkpointable(stage: Stage) -> bool:
    return getattr(stage, "stage_version", None) is not None


def _classify(exc: BaseException, stage: str | None) -> _Failure:
    """Turn any exception from a running attempt into an error class and a safe message."""
    if isinstance(exc, JobError):
        return _Failure(exc.error_class, exc.safe_message, stage, exc.kind == ErrorKind.TRANSIENT)
    error_class = INTERNAL_ERROR
    if _is_conflict(exc):
        error_class = "database_conflict"
    elif isinstance(exc, (OperationalError, InterfaceError, PoolTimeoutError)):
        error_class = "database_unavailable"
    elif isinstance(exc, OSError) and exc.errno in STORAGE_ERRNOS:
        error_class = "storage_unavailable"
    spec = ERRORS[error_class]
    return _Failure(error_class, spec.message, stage, spec.kind == ErrorKind.TRANSIENT)


def _is_conflict(exc: BaseException) -> bool:
    """A deadlock or serialization failure: the transaction lost a race and can run again."""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return isinstance(exc, DBAPIError) and sqlstate in (DEADLOCK_DETECTED, SERIALIZATION_FAILURE)
