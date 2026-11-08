"""Job status changes and event appends.

Two rules make these safe when several processes (API, workers, recovery) touch the same job:

1. **Conditional updates.** A status change is one statement:
       UPDATE jobs SET status = :to ... WHERE id = :id AND status = :from RETURNING ...
   Postgres locks the row while updating it. If two processes try the same change, the second
   waits for the first to commit, then re-checks its WHERE clause against the *new* row. The
   status no longer matches, so it updates zero rows and gets nothing back from RETURNING.
   That empty result is how the caller learns it lost the race. (MongoDB equivalent:
   `findOneAndUpdate({_id, status: "queued"}, ...)` returning null.)

2. **Per-job event numbers.** Every event takes the next number from `jobs.last_event_seq`, which
   is incremented by an UPDATE on the job row. That UPDATE holds the row lock until commit, so
   for any one job the numbers become visible in the same order they were handed out, with no
   gaps. SSE replay ("send me everything after event N") depends on this.

Functions here never commit. The caller commits, so it can combine several writes (for example,
a status change plus an outbox row) into one all-or-nothing transaction.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, exists, func, insert, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from app.db.models import Artifact, Job, JobAttempt, JobEvent, OutboxMessage, Upload
from app.domain.enums import EventType, JobStatus
from app.domain.listing import JobListPosition
from app.domain.state_machine import cancel_supersedes, ensure_transition, is_terminal

jobs = Job.__table__
job_events = JobEvent.__table__

# Columns a caller may set alongside a status change. Status, timestamps and the event counter
# are managed here so the table's CHECK constraints are always satisfied consistently.
ALLOWED_CHANGES = frozenset(
    {
        "current_stage",
        "progress_percent",
        "retry_at",
        "error_class",
        "error_summary",
        "attempt_count",
        "max_attempts",
        "manual_retry_count",
    }
)


class JobNotFoundError(LookupError):
    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"job {job_id} does not exist")
        self.job_id = job_id


@dataclass(frozen=True)
class TransitionResult:
    applied: bool
    # The job's status after the call: the new status if applied, otherwise the status that
    # another process left it in (so the caller can decide what to do next).
    status: JobStatus
    # Sequence number of the `job.status_changed` event, when applied.
    event_seq: int | None = None


def transition_job(
    session: Session,
    job_id: uuid.UUID,
    *,
    from_status: JobStatus,
    to_status: JobStatus,
    changes: Mapping[str, Any] | None = None,
    event_payload: Mapping[str, Any] | None = None,
    attempt_no: int | None = None,
    only_if_retry_due: bool = False,
) -> TransitionResult:
    """Move a job from `from_status` to `to_status` if, and only if, it is still in `from_status`.

    Raises IllegalTransitionError (before touching the database) if the change is not in the state
    machine, and JobNotFoundError if the job doesn't exist. Returns `applied=False` if the job
    wasn't in `from_status`.

    only_if_retry_due: additionally require `retry_at <= now()` (database clock). Used when a
    worker claims a `retry_wait` job, so an old duplicate message can't start a retry before its
    backoff has passed. If the retry isn't due, the result is `applied=False` with the current
    status (`retry_wait`).

    Assumes the default READ COMMITTED isolation level. (Under REPEATABLE READ or SERIALIZABLE,
    Postgres raises a serialization error instead of returning zero rows.)
    """
    ensure_transition(from_status, to_status)
    _require_job_id(job_id)
    if only_if_retry_due and from_status != JobStatus.RETRY_WAIT:
        raise ValueError("only_if_retry_due applies only to transitions from retry_wait")
    values = _transition_values(from_status, to_status, dict(changes or {}))
    # These statements bypass the ORM, which only auto-flushes before ORM statements. Send any
    # pending ORM changes first (e.g. a just-added Job), so the UPDATE can see them.
    session.flush()

    # A cancel request overtaken by success or permanent failure gets a second, explanatory event.
    superseded = cancel_supersedes(from_status, to_status)
    events_added = 2 if superseded else 1
    values["last_event_seq"] = jobs.c.last_event_seq + events_added

    conditions = [jobs.c.id == job_id, jobs.c.status == from_status.value]
    if only_if_retry_due:
        conditions.append(jobs.c.retry_at <= func.now())
    new_last_seq = session.execute(
        update(jobs).where(*conditions).values(**values).returning(jobs.c.last_event_seq)
    ).scalar_one_or_none()

    if new_last_seq is None:
        # Zero rows matched: the job is missing, or its latest committed status isn't
        # `from_status`. Two ways to get here:
        # - Another transaction changed the row and committed while we waited for its lock;
        #   Postgres re-checked our WHERE against the new row and it no longer matched.
        # - The committed status already differed, so the row was skipped without waiting
        #   (even if some other transaction holds an uncommitted change to it).
        # The read below is the latest *committed* status. It may change again a moment later,
        # so callers treat it as advice: re-decide from it, never assume it is final.
        current = session.execute(
            select(jobs.c.status).where(jobs.c.id == job_id)
        ).scalar_one_or_none()
        if current is None:
            raise JobNotFoundError(job_id)
        _expire_cached_job(session, job_id)
        return TransitionResult(applied=False, status=JobStatus(current))

    status_seq = new_last_seq - events_added + 1
    _insert_event(
        session,
        job_id,
        status_seq,
        EventType.JOB_STATUS_CHANGED,
        # from/to go last so a caller's payload can't overwrite them.
        {**(event_payload or {}), "from": from_status.value, "to": to_status.value},
        attempt_no,
    )
    if superseded:
        reason = (
            "completed_before_cancel"
            if to_status == JobStatus.SUCCEEDED
            else "failed_before_cancel"
        )
        _insert_event(
            session,
            job_id,
            status_seq + 1,
            EventType.JOB_CANCEL_SUPERSEDED,
            {"outcome": to_status.value, "reason": reason},
            attempt_no,
        )
    _expire_cached_job(session, job_id)
    return TransitionResult(applied=True, status=to_status, event_seq=status_seq)


def append_event(
    session: Session,
    job_id: uuid.UUID,
    event_type: EventType | str,
    payload: Mapping[str, Any] | None = None,
    attempt_no: int | None = None,
) -> int:
    """Append an event to a job's stream and return its sequence number.

    The counter UPDATE locks the job row until the caller commits, so keep the transaction short.
    """
    _require_job_id(job_id)
    session.flush()  # see transition_job: send pending ORM changes before bypassing the ORM
    seq = session.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(last_event_seq=jobs.c.last_event_seq + 1)
        .returning(jobs.c.last_event_seq)
    ).scalar_one_or_none()
    if seq is None:
        raise JobNotFoundError(job_id)
    _insert_event(session, job_id, seq, event_type, payload or {}, attempt_no)
    _expire_cached_job(session, job_id)
    return seq


def _require_job_id(job_id: uuid.UUID | None) -> None:
    if job_id is None:
        # A Job added to the session gets its default id only when flushed. Set Job.id
        # explicitly (Job(id=uuid.uuid4(), ...)) or flush before passing job.id here.
        raise ValueError("job_id is None: set Job.id explicitly or flush the session first")


def _transition_values(
    from_status: JobStatus, to_status: JobStatus, changes: dict[str, Any]
) -> dict[str, Any]:
    unknown = set(changes) - ALLOWED_CHANGES
    if unknown:
        raise ValueError(f"cannot set {sorted(unknown)} through transition_job")
    if to_status == JobStatus.RETRY_WAIT and changes.get("retry_at") is None:
        raise ValueError("moving to retry_wait requires changes['retry_at']")
    if to_status == JobStatus.FAILED and not changes.get("error_summary"):
        raise ValueError("moving to failed requires changes['error_summary']")
    reopening = is_terminal(from_status) and not is_terminal(to_status)
    if reopening and changes.get("max_attempts") is None:
        # Without a new budget, a job that used all its attempts could be queued but never
        # claimed. Use can_manual_retry(...).new_max_attempts.
        raise ValueError("manual retry requires changes['max_attempts']")

    values: dict[str, Any] = {**changes, "status": to_status.value, "updated_at": func.now()}
    if to_status != JobStatus.RETRY_WAIT:
        values["retry_at"] = None
    if to_status == JobStatus.SUCCEEDED:
        # A success clears errors left by earlier failed attempts (they stay in the attempt
        # history and events).
        values["error_class"] = None
        values["error_summary"] = None
    if is_terminal(to_status):
        values["finished_at"] = func.now()
    elif reopening:
        # Reopened by manual retry: a fresh run. The old error, progress, and any cancel request
        # live on in the attempt history and events, not on the job.
        values["finished_at"] = None
        values["cancel_requested_at"] = None
        values["current_stage"] = None
        values["progress_percent"] = 0
        values.setdefault("error_class", None)
        values.setdefault("error_summary", None)
    if to_status == JobStatus.RUNNING:
        values["started_at"] = func.coalesce(jobs.c.started_at, func.now())
    if to_status in (JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED):
        values["cancel_requested_at"] = func.coalesce(jobs.c.cancel_requested_at, func.now())
    return values


def _insert_event(
    session: Session,
    job_id: uuid.UUID,
    seq: int,
    event_type: EventType | str,
    payload: Mapping[str, Any],
    attempt_no: int | None,
) -> None:
    session.execute(
        insert(job_events).values(
            job_id=job_id,
            seq=seq,
            event_type=str(event_type),
            payload=dict(payload),
            attempt_no=attempt_no,
        )
    )


def _expire_cached_job(session: Session, job_id: uuid.UUID) -> None:
    """These functions bypass the ORM. If a Job object for this row is loaded in the session,
    mark it stale so the next attribute access reloads it from the database."""
    cached = session.identity_map.get(identity_key(Job, job_id))
    if cached is not None:
        session.expire(cached)


def update_progress(session: Session, job_id: uuid.UUID, *, stage: str, percent: int) -> bool:
    """Record the current stage and progress of a job that is still executing.

    Only applies while the job is `running` or `cancel_requested`; returns False otherwise (the
    job was finished or moved by someone else, so the worker should stop).
    """
    _require_job_id(job_id)
    session.flush()
    updated = session.execute(
        update(jobs)
        .where(
            jobs.c.id == job_id,
            jobs.c.status.in_([JobStatus.RUNNING.value, JobStatus.CANCEL_REQUESTED.value]),
        )
        .values(current_stage=stage, progress_percent=percent, updated_at=func.now())
        .returning(jobs.c.id)
    ).scalar_one_or_none()
    _expire_cached_job(session, job_id)
    return updated is not None


@dataclass(frozen=True)
class LostMessageJob:
    id: uuid.UUID
    status: JobStatus
    last_published_at: datetime | None


def lock_next_lost_message_job(
    session: Session, grace: timedelta, skip: Sequence[uuid.UUID] = ()
) -> LostMessageJob | None:
    """Lock one job that should be running soon but evidently has no message on its way.

    All of these must hold (database clock):
    - claimable: `queued`, or `retry_wait` whose retry has been due for longer than `grace`;
    - it hasn't changed status within `grace` (a job queued a moment ago is not lost);
    - no open attempt (no worker is on it);
    - no outbox row that is unpublished (the dispatcher will still send it) or was published
      within `grace` (that message may still be waiting in Redis for a free worker).
    FOR UPDATE SKIP LOCKED, so concurrent recovery passes each take different jobs. Every change
    that could make a job stop matching (a claim, a cancel, a requeue) locks or updates the job
    row, so the check holds until the caller commits.
    """
    outbox = OutboxMessage.__table__
    attempts = JobAttempt.__table__
    cutoff = func.now() - grace
    conditions = [
        or_(
            jobs.c.status == JobStatus.QUEUED.value,
            (jobs.c.status == JobStatus.RETRY_WAIT.value) & (jobs.c.retry_at <= cutoff),
        ),
        jobs.c.updated_at <= cutoff,
        ~exists().where(attempts.c.job_id == jobs.c.id, attempts.c.ended_at.is_(None)),
        ~exists().where(
            outbox.c.job_id == jobs.c.id,
            or_(outbox.c.published_at.is_(None), outbox.c.published_at > cutoff),
        ),
    ]
    if skip:
        conditions.append(jobs.c.id.not_in(list(skip)))
    row = session.execute(
        select(jobs.c.id, jobs.c.status, _last_published(outbox))
        .where(*conditions)
        .order_by(jobs.c.updated_at)
        .limit(1)
        .with_for_update(of=jobs, skip_locked=True)
    ).one_or_none()
    return LostMessageJob(row.id, JobStatus(row.status), row.last_published) if row else None


def _last_published(outbox: Any) -> Any:
    return (
        select(func.max(outbox.c.published_at))
        .where(outbox.c.job_id == jobs.c.id)
        .scalar_subquery()
        .label("last_published")
    )


def lock_job(session: Session, job_id: uuid.UUID) -> Job | None:
    """Load the job with its row locked (FOR UPDATE) until the caller's transaction ends."""
    session.flush()
    return session.execute(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


# --- Creating and reading jobs --------------------------------------------------------------------


def insert_job_if_key_unused(session: Session, values: Mapping[str, Any]) -> uuid.UUID | None:
    """Insert a job unless its idempotency key is already taken. Returns the new id, or None.

    `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` lets the unique index decide races:
    if another transaction inserted the same key and hasn't finished, this statement waits for
    it. If that transaction commits, this one inserts nothing (returns None); if it rolls back,
    this insert goes ahead. No exception is raised, so the surrounding transaction stays usable
    (a caught IntegrityError would abort the whole Postgres transaction).
    """
    if values.get("id") is None:
        raise ValueError("insert_job_if_key_unused requires an explicit job id")
    statement = (
        pg_insert(jobs)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[jobs.c.idempotency_key])
        .returning(jobs.c.id)
    )
    return session.execute(statement).scalar_one_or_none()


def get_job_by_idempotency_key(session: Session, key: str) -> Job | None:
    return session.scalars(select(Job).where(Job.idempotency_key == key)).one_or_none()


def list_jobs_statement(
    *,
    statuses: Sequence[str] = (),
    workflow_types: Sequence[str] = (),
    after: JobListPosition | None = None,
    limit: int,
) -> Select[tuple[Job]]:
    """Newest-first job list with keyset pagination.

    `(created_at, id) < (after.created_at, after.job_id)` means "strictly after the previous
    page's last row" in this ordering. It stays correct when new jobs are inserted (they sort
    before the cursor, so they never shift later pages), and the index can jump straight to it.
    """
    statement = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)
    if statuses:
        statement = statement.where(Job.status.in_(list(statuses)))
    if workflow_types:
        statement = statement.where(Job.workflow_type.in_(list(workflow_types)))
    if after is not None:
        statement = statement.where(
            tuple_(Job.created_at, Job.id) < tuple_(after.created_at, after.job_id)
        )
    return statement


def list_jobs(
    session: Session,
    *,
    statuses: Sequence[str] = (),
    workflow_types: Sequence[str] = (),
    after: JobListPosition | None = None,
    limit: int,
) -> list[Job]:
    statement = list_jobs_statement(
        statuses=statuses, workflow_types=workflow_types, after=after, limit=limit
    )
    return list(session.scalars(statement))


@dataclass(frozen=True)
class JobDetail:
    job: Job
    upload: Upload
    attempts: list[JobAttempt]
    artifacts: list[Artifact]


def get_job_detail(session: Session, job_id: uuid.UUID) -> JobDetail | None:
    job = session.get(Job, job_id)
    if job is None:
        return None
    upload = session.get(Upload, job.upload_id)
    attempts = list(
        session.scalars(
            select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt_no)
        )
    )
    artifacts = list(
        session.scalars(
            select(Artifact).where(Artifact.job_id == job_id).order_by(Artifact.artifact_type)
        )
    )
    return JobDetail(job=job, upload=upload, attempts=attempts, artifacts=artifacts)
