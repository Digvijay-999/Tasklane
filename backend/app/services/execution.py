"""Running a job inside a worker: claim it, run its stages, record the outcome.

Called by the Celery task (app/queue/tasks.py) with nothing but a job id. Celery may deliver the
same message more than once, and a message may arrive long after it was sent, so the first step
is always a **claim** against the database, which is the source of truth:

1. Claim (one transaction): the job must be `queued`, or `retry_wait` with its retry due. A
   conditional update moves it to `running` and counts the attempt; an attempt row with a lease
   is inserted; `attempt.started` is recorded. Anything else (already running, finished,
   cancelled, not due, lost the race to another worker) means the message is a duplicate or
   stale: it is **dropped without writing anything**.
2. Stages: each progress report renews the lease and records progress and an event. If the
   attempt was closed by someone else (recovery, Phase 7), the worker is *fenced out*: it stops
   without writing anything further.
3. Finish (one transaction): close the attempt and move the job to `succeeded` (or record the
   failure).

Phase 4 failure handling: any unexpected exception fails the job permanently with error class
`internal_error` (no retries yet; Phase 5 adds transient/permanent classification). If even that
can't be recorded (e.g. the database is down), or the worker process dies, the job stays
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

from app.db.models import Job
from app.db.repositories import attempts as attempt_rows
from app.db.repositories import jobs as job_rows
from app.domain.enums import (
    AttemptOutcome,
    AttemptTrigger,
    ErrorClass,
    EventType,
    JobStatus,
    WorkflowType,
)
from app.domain.state_machine import (
    CLAIMABLE_STATUSES,
    FailureKind,
    status_after_failure,
    status_after_success,
)
from app.workflows.base import Stage

logger = logging.getLogger(__name__)

INTERNAL_ERROR_SUMMARY = "An unexpected error occurred while running the job."
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


@dataclass(frozen=True)
class ClaimedAttempt:
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    workflow_type: WorkflowType


class JobExecutionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str,
        lease: timedelta,
        stages_for: Callable[[WorkflowType], list[Stage]],
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.lease = lease
        self.stages_for = stages_for

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
        try:
            last_stage = self._run_stages(claim)
            self._succeed(claim, last_stage)
        except AttemptFencedError:
            logger.warning("attempt fenced out; stopping without further writes", extra=log_extra)
            return RunResult(RunOutcome.FENCED, attempt_no=claim.attempt_no)
        except Exception:
            # The traceback goes to the logs; the database only gets a safe summary.
            logger.exception("job failed with an unexpected error", extra=log_extra)
            outcome = self._fail_permanently(claim)
            return RunResult(outcome, attempt_no=claim.attempt_no)
        logger.info("job succeeded", extra=log_extra)
        return RunResult(RunOutcome.SUCCEEDED, attempt_no=claim.attempt_no)

    # --- Claim -----------------------------------------------------------------------------------

    def claim(self, job_id: uuid.UUID, task_id: str | None) -> ClaimedAttempt | RunResult:
        with self.session_factory.begin() as session:
            # FOR UPDATE: lock the job row for this short transaction, so the status, attempt
            # count, and trigger read here can't change before the update below. A concurrent
            # claimer waits here, then sees the job already `running` and drops its message.
            row = session.execute(
                select(Job.status, Job.attempt_count, Job.workflow_type)
                .where(Job.id == job_id)
                .with_for_update()
            ).one_or_none()
            if row is None:
                return RunResult(RunOutcome.DROPPED, DropReason.JOB_NOT_FOUND)
            status = JobStatus(row.status)
            if status not in CLAIMABLE_STATUSES:
                return RunResult(RunOutcome.DROPPED, DropReason.NOT_CLAIMABLE, job_status=status)
            workflow_type = WorkflowType(row.workflow_type)  # validate before changing anything

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
        return ClaimedAttempt(
            job_id=job_id,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            workflow_type=workflow_type,
        )

    # --- Stages ----------------------------------------------------------------------------------

    def _run_stages(self, claim: ClaimedAttempt) -> str:
        stages = self.stages_for(claim.workflow_type)
        for index, stage in enumerate(stages):
            context = _AttemptContext(self, claim, stage.name, index, len(stages))
            context.record(EventType.STAGE_STARTED, {"stage": stage.name}, stage_percent=0)
            started = time.monotonic()
            stage.run(context)
            duration_ms = round((time.monotonic() - started) * 1000)
            context.record(
                EventType.STAGE_COMPLETED,
                {"stage": stage.name, "duration_ms": duration_ms},
                stage_percent=100,
            )
        return stages[-1].name if stages else ""

    # --- Finish ----------------------------------------------------------------------------------

    def _succeed(self, claim: ClaimedAttempt, last_stage: str) -> None:
        with self.session_factory.begin() as session:
            if not attempt_rows.close_attempt(session, claim.attempt_id, AttemptOutcome.SUCCEEDED):
                raise AttemptFencedError
            if last_stage:
                job_rows.update_progress(session, claim.job_id, stage=last_stage, percent=100)
            # status_after_success: a pending cancel request is superseded by the success.
            self._move_to_outcome(session, claim, status_after_success)

    def _fail_permanently(self, claim: ClaimedAttempt) -> RunOutcome:
        try:
            with self.session_factory.begin() as session:
                closed = attempt_rows.close_attempt(
                    session,
                    claim.attempt_id,
                    AttemptOutcome.FAILED_PERMANENT,
                    error_class=ErrorClass.INTERNAL_ERROR,
                    error_summary=INTERNAL_ERROR_SUMMARY,
                )
                if not closed:
                    raise AttemptFencedError
                self._move_to_outcome(
                    session,
                    claim,
                    lambda current: status_after_failure(
                        current, FailureKind.PERMANENT, attempt_count=0, max_attempts=0
                    ),
                    changes={
                        "error_class": ErrorClass.INTERNAL_ERROR,
                        "error_summary": INTERNAL_ERROR_SUMMARY,
                    },
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


class _AttemptContext:
    """StageContext for one stage of one attempt: records progress, renews the lease."""

    def __init__(
        self,
        service: JobExecutionService,
        claim: ClaimedAttempt,
        stage_name: str,
        stage_index: int,
        stage_count: int,
    ) -> None:
        self.service = service
        self.claim = claim
        self.stage_name = stage_name
        self.stage_index = stage_index
        self.stage_count = stage_count

    def report_progress(self, percent: int) -> None:
        percent = max(0, min(100, percent))
        self.record(
            EventType.STAGE_PROGRESS, {"stage": self.stage_name, "percent": percent}, percent
        )

    def record(self, event_type: EventType, payload: dict[str, object], stage_percent: int) -> None:
        """One short transaction: renew the lease (fencing check), update progress, add event."""
        overall = round((self.stage_index * 100 + stage_percent) / self.stage_count)
        with self.service.session_factory.begin() as session:
            if not attempt_rows.renew_lease(session, self.claim.attempt_id, self.service.lease):
                raise AttemptFencedError
            if not job_rows.update_progress(
                session, self.claim.job_id, stage=self.stage_name, percent=overall
            ):
                raise AttemptFencedError
            job_rows.append_event(
                session, self.claim.job_id, event_type, payload, attempt_no=self.claim.attempt_no
            )
