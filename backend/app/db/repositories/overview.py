"""Read-only queries for the dashboard overview (GET /v1/overview). Functions never commit."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, case, extract, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Job, OutboxMessage
from app.domain.enums import JobStatus

jobs = Job.__table__
outbox = OutboxMessage.__table__


def status_counts_statement() -> Select[tuple[str, int]]:
    """Jobs per status. `status` leads ix_jobs_status_created_at_id, so on a large table Postgres
    can answer the GROUP BY from that index instead of reading every row (tested)."""
    return select(jobs.c.status, func.count()).group_by(jobs.c.status)


def status_counts(session: Session) -> dict[str, int]:
    return {status: count for status, count in session.execute(status_counts_statement())}


@dataclass(frozen=True)
class OldestQueued:
    job_id: uuid.UUID
    queued_seconds: float  # by the database clock


def oldest_queued(session: Session) -> OldestQueued | None:
    """The job that has been `queued` longest. A queued job's updated_at is when it entered
    `queued` (created, or reopened by a manual retry): nothing else updates a queued row."""
    row = session.execute(
        select(jobs.c.id, extract("epoch", func.now() - jobs.c.updated_at).label("age"))
        .where(jobs.c.status == JobStatus.QUEUED.value)
        .order_by(jobs.c.updated_at, jobs.c.id)
        .limit(1)
    ).one_or_none()
    return OldestQueued(row.id, max(0.0, float(row.age))) if row else None


@dataclass(frozen=True)
class OutboxSummary:
    pending: int  # unpublished and due, or already failing: should be in Redis by now
    scheduled: int  # unpublished, never tried, due later (delayed retries)
    failing: int  # unpublished rows whose last publish attempt failed
    max_dispatch_attempts: int
    last_dispatch_error: str | None  # error *type* of the most-retried failing row
    oldest_pending_seconds: float | None


def outbox_summary(session: Session) -> OutboxSummary:
    unpublished = outbox.c.published_at.is_(None)
    failing = outbox.c.dispatch_attempts > 0
    # A failed publish pushes available_at into the future (the dispatcher's backoff), so a
    # failing row is "pending", never "scheduled", and its wait is measured from created_at:
    # otherwise an hour-long Redis outage would look like a message that waited a few seconds.
    pending = or_(outbox.c.available_at <= func.now(), failing)
    waiting_since = case((failing, outbox.c.created_at), else_=outbox.c.available_at)
    counts = session.execute(
        select(
            func.count().filter(pending),
            func.count().filter(~pending),
            func.count().filter(failing),
            func.coalesce(func.max(outbox.c.dispatch_attempts), 0),
            extract("epoch", func.now() - func.min(waiting_since).filter(pending)),
        ).where(unpublished)
    ).one()
    last_error = session.execute(
        select(outbox.c.last_dispatch_error)
        .where(unpublished, outbox.c.dispatch_attempts > 0)
        .order_by(outbox.c.dispatch_attempts.desc(), outbox.c.id)
        .limit(1)
    ).scalar_one_or_none()
    return OutboxSummary(
        pending=counts[0],
        scheduled=counts[1],
        failing=counts[2],
        max_dispatch_attempts=counts[3],
        last_dispatch_error=last_error,
        oldest_pending_seconds=None if counts[4] is None else max(0.0, float(counts[4])),
    )


def database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()
