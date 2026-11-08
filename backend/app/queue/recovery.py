"""The recovery loop: finds jobs that nobody will finish on their own, and puts them back on track.

It runs in the dispatcher process, in its own thread with its own stop flag, next to (but
independent of) the dispatch loop (see app/queue/dispatcher.py main()). It imports no Celery and
no HTTP code (a test enforces it): everything it does is database work, and the messages it needs
go out through the outbox like any others.

Each pass (Recovery.run_once) does two things, each job in its own short transaction:

1. Expired leases. A worker that was killed (kill -9, out of memory, container stopped, the Celery
   hard time limit) stops renewing its attempt's lease. For every open attempt whose
   `lease_expires_at` is in the past (database clock):
   - lock the attempt (FOR UPDATE SKIP LOCKED), close it as `lease_expired`, and record
     `attempt.lease_expired`;
   - lock the job and decide with the state machine (status_after_lease_expired):
     attempts left -> `retry_wait` (backoff, and an outbox row due at the same database instant,
     exactly like a transient failure in the worker); budget used -> `failed`
     ("... Gave up after N attempts."); a cancel was requested -> `cancelled`.
   Lock order is attempt, then job: the same as a worker reporting progress or finishing, so
   recovery and a worker that is alive after all can never deadlock. If that worker renews or
   finishes first, the attempt is no longer expired (or no longer open) and recovery leaves it
   alone. If recovery closes the attempt first, the worker's next renewal matches no row: it is
   fenced out and writes nothing more.

2. Lost messages. A claimable job (`queued`, or `retry_wait` whose retry is overdue) with no open
   attempt and nothing in the outbox that is unpublished or was published recently (within
   LOST_MESSAGE_GRACE_SECONDS) has evidently lost its message (Redis lost it, or a worker died
   holding it before claiming). Recovery inserts a new outbox row and records `job.requeued`. A
   duplicate message is harmless: the worker's claim drops it.

Safe to run twice or concurrently: SKIP LOCKED gives each pass different rows, and once a row is
handled it no longer matches (the attempt is closed; the job has a fresh outbox row). Every
decision uses the database clock (`now()`), never this machine's clock.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import attempts as attempt_rows
from app.db.repositories import jobs as job_rows
from app.db.repositories.attempts import ExpiredAttempt
from app.db.repositories.jobs import LostMessageJob
from app.db.repositories.outbox import enqueue_retry, enqueue_run_job
from app.domain.dispatch import dispatch_backoff_seconds
from app.domain.enums import AttemptOutcome, EventType, JobStatus
from app.domain.errors import ERRORS, gave_up_summary
from app.domain.retry import RetryPolicy
from app.domain.state_machine import run_attempt_no, status_after_lease_expired

logger = logging.getLogger("tasklane.recovery")

LEASE_EXPIRED = "lease_expired"
EXECUTING_STATUSES = frozenset({JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED})


@dataclass(frozen=True)
class RecoveryResult:
    expired: int  # attempts closed as lease_expired
    requeued: int  # jobs given a new message


class Recovery:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        retry_policy: RetryPolicy,
        lost_message_grace: timedelta,
        batch_size: int = 100,
    ) -> None:
        self.session_factory = session_factory
        self.retry_policy = retry_policy
        self.lost_message_grace = lost_message_grace
        self.batch_size = batch_size  # per kind and pass; the next pass continues

    def run_once(self) -> RecoveryResult:
        return RecoveryResult(self.expire_leases(), self.requeue_lost_messages())

    # --- Expired leases ----------------------------------------------------------------------

    def expire_leases(self) -> int:
        handled, failed = 0, []
        while handled + len(failed) < self.batch_size:
            attempt = None
            try:
                with self.session_factory.begin() as session:
                    attempt = attempt_rows.lock_next_expired_attempt(session, skip=failed)
                    if attempt is None:
                        break
                    self._expire(session, attempt)
            except Exception:
                if attempt is None:
                    raise  # couldn't even look (database down): the loop backs off
                # One row that can't be handled must not block all the others: roll it back,
                # skip it for the rest of this pass, and try it again next pass.
                logger.exception(
                    "could not recover an expired attempt; skipping it this pass",
                    extra={"job_id": str(attempt.job_id), "attempt_no": attempt.attempt_no},
                )
                failed.append(attempt.id)
                continue
            handled += 1
        return handled

    def _expire(self, session: Session, attempt: ExpiredAttempt) -> None:
        message = ERRORS[LEASE_EXPIRED].message
        # We hold the attempt's row lock, so it is still open: close_attempt can't miss.
        attempt_rows.close_attempt(
            session,
            attempt.id,
            AttemptOutcome.LEASE_EXPIRED,
            error_class=LEASE_EXPIRED,
            error_summary=message,
        )
        job_rows.append_event(  # also locks the job row (attempt first, then job)
            session,
            attempt.job_id,
            EventType.ATTEMPT_LEASE_EXPIRED,
            {
                "attempt_no": attempt.attempt_no,
                "worker_id": attempt.worker_id,
                "last_heartbeat_at": attempt.heartbeat_at.isoformat(),
                "lease_expired_at": attempt.lease_expires_at.isoformat(),
            },
            attempt_no=attempt.attempt_no,
        )
        job = job_rows.lock_job(session, attempt.job_id)
        assert job is not None  # the attempt references it, and we hold its lock
        status = JobStatus(job.status)
        log = {
            "job_id": str(attempt.job_id),
            "attempt_no": attempt.attempt_no,
            "worker_id": attempt.worker_id,
        }
        if status not in EXECUTING_STATUSES:
            # Shouldn't happen (a worker closes its attempt in the same transaction that finishes
            # the job), but if it does, closing the stray attempt is all there is to do.
            logger.warning(
                "closed an expired attempt of a job that is not executing",
                extra={**log, "job_status": status},
            )
            return
        attempt_count, max_attempts = job.attempt_count, job.max_attempts
        target = status_after_lease_expired(status, attempt_count, max_attempts)
        changes: dict[str, Any] = {}
        payload: dict[str, Any] = {"attempt_no": attempt.attempt_no, "reason": LEASE_EXPIRED}
        retry_at = None
        if target == JobStatus.RETRY_WAIT:
            delay = self.retry_policy.delay_seconds(
                run_attempt_no(attempt.attempt_no, max_attempts)
            )
            # One database-clock instant for the job and its outbox row (as in the worker).
            retry_at = session.execute(select(func.now() + timedelta(seconds=delay))).scalar_one()
            changes = {"error_class": LEASE_EXPIRED, "error_summary": message, "retry_at": retry_at}
            payload.update(
                error_class=LEASE_EXPIRED,
                retry_at=retry_at.isoformat(),
                retry_in_seconds=round(delay, 3),
            )
        elif target == JobStatus.FAILED:
            changes = {
                "error_class": LEASE_EXPIRED,
                "error_summary": gave_up_summary(message, attempt_count),
            }
            payload.update(error_class=LEASE_EXPIRED, gave_up_after_attempts=attempt_count)
        result = job_rows.transition_job(
            session,
            attempt.job_id,
            from_status=status,
            to_status=target,
            changes=changes,
            event_payload=payload,
            attempt_no=attempt.attempt_no,
        )
        if not result.applied:  # impossible while we hold the job lock; never half-recover
            raise RuntimeError(f"job {attempt.job_id} changed status while locked")
        if retry_at is not None:
            enqueue_retry(session, attempt.job_id, available_at=retry_at)
        logger.warning("expired lease recovered", extra={**log, "job_status": target})

    # --- Lost messages -----------------------------------------------------------------------

    def requeue_lost_messages(self) -> int:
        handled, failed = 0, []
        while handled + len(failed) < self.batch_size:
            job = None
            try:
                with self.session_factory.begin() as session:
                    job = job_rows.lock_next_lost_message_job(
                        session, self.lost_message_grace, skip=failed
                    )
                    if job is None:
                        break
                    self._requeue(session, job)
            except Exception:
                if job is None:
                    raise
                logger.exception(
                    "could not requeue a lost message; skipping it this pass",
                    extra={"job_id": str(job.id)},
                )
                failed.append(job.id)
                continue
            handled += 1
            logger.warning(
                "lost message: job requeued",
                extra={"job_id": str(job.id), "job_status": job.status},
            )
        return handled

    def _requeue(self, session: Session, job: LostMessageJob) -> None:
        enqueue_run_job(session, job.id)
        job_rows.append_event(
            session,
            job.id,
            EventType.JOB_REQUEUED,
            {
                "reason": "lost_message",
                "status": job.status.value,
                "last_published_at": (
                    job.last_published_at.isoformat() if job.last_published_at else None
                ),
            },
        )


def run_forever(
    recovery: Recovery,
    *,
    interval_seconds: float,
    stop: threading.Event,
    backoff: Callable[[int], float] = dispatch_backoff_seconds,
) -> None:
    """Run a pass every `interval_seconds` until `stop` is set; back off while the database is
    unavailable. A pass in progress finishes before the loop exits."""
    consecutive_failures = 0
    while not stop.is_set():
        try:
            recovery.run_once()
        except Exception:
            consecutive_failures += 1
            logger.exception("recovery pass failed; backing off")
            stop.wait(backoff(consecutive_failures))
            continue
        consecutive_failures = 0
        stop.wait(interval_seconds)


def start_in_thread(
    recovery: Recovery, *, interval_seconds: float, stop: threading.Event
) -> threading.Thread:
    """Run the loop in a background thread (the dispatcher process uses this)."""
    thread = threading.Thread(
        target=run_forever,
        args=(recovery,),
        kwargs={"interval_seconds": interval_seconds, "stop": stop},
        name="recovery",
        daemon=True,
    )
    thread.start()
    return thread
