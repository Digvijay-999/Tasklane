"""Job attempts: one row per execution, holding the worker's lease. Functions never commit.

Lease and fencing: an attempt is "open" while `ended_at IS NULL`. A worker renews its lease with
`UPDATE ... WHERE id = :attempt AND ended_at IS NULL`. If that matches nothing, someone else
(recovery, in Phase 7) has closed the attempt, so the worker must stop writing: it no longer owns
the job. This is called *fencing*.
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, insert, update
from sqlalchemy.orm import Session

from app.db.models import JobAttempt
from app.domain.enums import AttemptOutcome, AttemptTrigger

attempts = JobAttempt.__table__


def insert_attempt(
    session: Session,
    *,
    job_id: uuid.UUID,
    attempt_no: int,
    trigger: AttemptTrigger,
    worker_id: str,
    task_id: str | None,
    lease: timedelta,
) -> uuid.UUID:
    """Open a new attempt with a lease measured from the database clock.

    The partial unique index uq_job_attempts_one_open_per_job makes a second open attempt for the
    same job impossible, even if two workers got this far at once.
    """
    attempt_id = uuid.uuid4()
    session.execute(
        insert(attempts).values(
            id=attempt_id,
            job_id=job_id,
            attempt_no=attempt_no,
            trigger=trigger.value,
            worker_id=worker_id,
            task_id=task_id,
            started_at=func.now(),
            heartbeat_at=func.now(),
            lease_expires_at=func.now() + lease,
        )
    )
    return attempt_id


def renew_lease(session: Session, attempt_id: uuid.UUID, lease: timedelta) -> bool:
    """Extend an open attempt's lease. Returns False if the attempt is closed (fenced out)."""
    renewed = session.execute(
        update(attempts)
        .where(attempts.c.id == attempt_id, attempts.c.ended_at.is_(None))
        .values(heartbeat_at=func.now(), lease_expires_at=func.now() + lease)
        .returning(attempts.c.id)
    ).scalar_one_or_none()
    return renewed is not None


def close_attempt(
    session: Session,
    attempt_id: uuid.UUID,
    outcome: AttemptOutcome,
    *,
    error_class: str | None = None,
    error_summary: str | None = None,
) -> bool:
    """Close an open attempt with its outcome. Returns False if it was already closed."""
    closed = session.execute(
        update(attempts)
        .where(attempts.c.id == attempt_id, attempts.c.ended_at.is_(None))
        .values(
            ended_at=func.now(),
            outcome=outcome.value,
            error_class=error_class,
            error_summary=error_summary,
        )
        .returning(attempts.c.id)
    ).scalar_one_or_none()
    return closed is not None
