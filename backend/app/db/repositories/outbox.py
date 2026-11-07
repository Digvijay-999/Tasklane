"""Outbox rows: tasks waiting to be published to the queue by the dispatcher.

Functions never commit; the caller owns the transaction.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import OutboxMessage
from app.domain.enums import OutboxTask

outbox = OutboxMessage.__table__


def enqueue_run_job(
    session: Session, job_id: uuid.UUID, *, available_at: datetime | None = None
) -> int | None:
    """Add a `run_job` message for a job, unless one is already waiting to be published.

    Returns the new message id, or None if the job already had an unpublished message (the
    partial unique index uq_outbox_one_pending_per_job allows only one). Written in the caller's
    transaction, so it commits together with the job change that caused it.
    """
    statement = (
        insert(outbox)
        .values(
            job_id=job_id,
            task_name=OutboxTask.RUN_JOB.value,
            payload={"job_id": str(job_id)},
            available_at=available_at if available_at is not None else func.now(),
        )
        .on_conflict_do_nothing(
            index_elements=[outbox.c.job_id],
            index_where=outbox.c.published_at.is_(None),
        )
        .returning(outbox.c.id)
    )
    return session.execute(statement).scalar_one_or_none()


def enqueue_retry(session: Session, job_id: uuid.UUID, *, available_at: datetime) -> int:
    """The message for an automatic retry, due at `available_at` (the job's retry_at).

    If the job already has an unpublished message (an odd case, e.g. one due earlier), that row is
    pushed back to the retry time instead of being kept as it is: published early, it would only
    be dropped by the claim as "retry not due", and the job would wait for recovery.
    """
    statement = insert(outbox).values(
        job_id=job_id,
        task_name=OutboxTask.RUN_JOB.value,
        payload={"job_id": str(job_id)},
        available_at=available_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[outbox.c.job_id],
        index_where=outbox.c.published_at.is_(None),
        set_={
            "available_at": func.greatest(outbox.c.available_at, statement.excluded.available_at)
        },
    ).returning(outbox.c.id)
    return session.execute(statement).scalar_one()


@dataclass(frozen=True)
class DueMessage:
    id: int
    job_id: uuid.UUID
    task_name: str
    payload: dict[str, Any]
    dispatch_attempts: int


def claim_due_messages(session: Session, limit: int) -> list[DueMessage]:
    """Lock and return up to `limit` unpublished messages that are due, oldest first.

    FOR UPDATE SKIP LOCKED: the rows stay locked until the caller's transaction ends, and any
    other dispatcher running the same query skips them instead of waiting. Two dispatchers can
    therefore never publish the same row at the same time.
    """
    rows = session.execute(
        select(
            outbox.c.id,
            outbox.c.job_id,
            outbox.c.task_name,
            outbox.c.payload,
            outbox.c.dispatch_attempts,
        )
        .where(outbox.c.published_at.is_(None), outbox.c.available_at <= func.now())
        .order_by(outbox.c.available_at, outbox.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    return [DueMessage(*row) for row in rows]


def mark_published(session: Session, message_ids: Sequence[int]) -> None:
    if not message_ids:
        return
    session.execute(
        update(outbox)
        .where(outbox.c.id.in_(list(message_ids)))
        .values(
            published_at=func.now(),
            dispatch_attempts=outbox.c.dispatch_attempts + 1,
            last_dispatch_error=None,
        )
    )


def mark_dispatch_failed(
    session: Session, message_id: int, *, error_type: str, retry_in: timedelta
) -> None:
    """Record a failed publish: count it, remember the error type, and delay the next try."""
    session.execute(
        update(outbox)
        .where(outbox.c.id == message_id)
        .values(
            dispatch_attempts=outbox.c.dispatch_attempts + 1,
            last_dispatch_error=error_type,
            # clock_timestamp() is the real current time; now() is frozen at the start of
            # the transaction, which began before the (possibly slow) failed publish.
            available_at=func.clock_timestamp() + retry_in,
        )
    )
