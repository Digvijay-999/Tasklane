"""Read-only aggregates behind the database-derived metrics on GET /metrics. Functions never
commit.

Everything is grouped by bounded columns only (workflow type, status, stage, error class,
provider, model, outcome); ids are never selected. Histograms are computed in SQL as cumulative
bucket counts (`count(*) FILTER (WHERE value <= bound)`), the shape Prometheus expects.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Float, case, cast, extract, func, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobAttempt, JobEvent, OutboxMessage, ProviderCall
from app.domain.enums import EventType

jobs = Job.__table__
attempts = JobAttempt.__table__
events = JobEvent.__table__
outbox = OutboxMessage.__table__
provider_calls = ProviderCall.__table__


@dataclass(frozen=True)
class HistogramRow:
    labels: tuple[str, ...]
    cumulative: list[int]  # count of values <= each bucket bound, in bound order
    count: int
    total: float


def _histogram(
    session: Session,
    value: Any,
    labels: Sequence[Any],
    buckets: Sequence[float],
    *,
    joins: Sequence[tuple[Any, Any]] = (),
    where: Sequence[Any] = (),
    from_: Any,
) -> list[HistogramRow]:
    present = value.is_not(None)  # GREATEST(NULL, 0) is 0: a missing value must not count
    value = func.greatest(value, 0.0)  # clock skew can't make a duration negative
    query = select(
        *labels,
        *(func.count().filter(value <= bound) for bound in buckets),
        func.count(),
        func.coalesce(func.sum(value), 0.0),
    ).select_from(from_)
    for table, condition in joins:
        query = query.join(table, condition)
    query = query.where(present, *where, *(label.is_not(None) for label in labels))
    query = query.group_by(*labels)
    width = len(labels)
    return [
        HistogramRow(
            labels=tuple(str(v) for v in row[:width]),
            cumulative=[int(v) for v in row[width : width + len(buckets)]],
            count=int(row[width + len(buckets)]),
            total=float(row[width + len(buckets) + 1]),
        )
        for row in session.execute(query)
    ]


def queue_wait(session: Session, buckets: Sequence[float]) -> list[HistogramRow]:
    """Per attempt: from the job becoming claimable (queued, or its retry due) to the claim."""
    return _histogram(
        session,
        extract("epoch", attempts.c.started_at - attempts.c.queued_at),
        [jobs.c.workflow_type],
        buckets,
        from_=attempts,
        joins=[(jobs, jobs.c.id == attempts.c.job_id)],
        where=[attempts.c.queued_at.is_not(None)],
    )


def workflow_runtime(session: Session, buckets: Sequence[float]) -> list[HistogramRow]:
    """Per finished attempt: claim to end, by how it ended."""
    return _histogram(
        session,
        extract("epoch", attempts.c.ended_at - attempts.c.started_at),
        [jobs.c.workflow_type, attempts.c.outcome],
        buckets,
        from_=attempts,
        joins=[(jobs, jobs.c.id == attempts.c.job_id)],
        where=[attempts.c.ended_at.is_not(None)],
    )


def stage_duration(session: Session, buckets: Sequence[float]) -> list[HistogramRow]:
    """Per completed stage run, from the duration_ms its stage.completed event recorded."""
    return _histogram(
        session,
        cast(events.c.payload["duration_ms"].astext, Float) / 1000.0,
        [jobs.c.workflow_type, events.c.payload["stage"].astext],
        buckets,
        from_=events,
        joins=[(jobs, jobs.c.id == events.c.job_id)],
        where=[events.c.event_type == EventType.STAGE_COMPLETED.value],
    )


def _call_outcome() -> Any:
    return case((provider_calls.c.success, "success"), else_="failure")


def provider_latency(session: Session, buckets: Sequence[float]) -> list[HistogramRow]:
    return _histogram(
        session,
        provider_calls.c.latency_ms / 1000.0,
        [provider_calls.c.provider, provider_calls.c.model, _call_outcome()],
        buckets,
        from_=provider_calls,
    )


def jobs_created(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(jobs.c.workflow_type, func.count()).group_by(jobs.c.workflow_type)
    )
    return {workflow: count for workflow, count in rows}


def jobs_by_status(session: Session) -> dict[str, int]:
    return dict(session.execute(select(jobs.c.status, func.count()).group_by(jobs.c.status)).all())


@dataclass(frozen=True)
class EventCount:
    workflow_type: str
    event_type: str
    to_status: str | None
    error_class: str | None
    count: int


def event_counts(session: Session) -> list[EventCount]:
    """The events behind the job counters: status changes (finished, retries), manual retries,
    expired leases and requeued lost messages, counted by workflow, target status and error
    class."""
    payload = events.c.payload
    counted = [
        EventType.JOB_STATUS_CHANGED.value,
        EventType.JOB_MANUAL_RETRY.value,
        EventType.ATTEMPT_LEASE_EXPIRED.value,
        EventType.JOB_REQUEUED.value,
    ]
    group = [
        jobs.c.workflow_type,
        events.c.event_type,
        payload["to"].astext,
        payload["error_class"].astext,
    ]
    rows = session.execute(
        select(*group, func.count())
        .select_from(events.join(jobs, jobs.c.id == events.c.job_id))
        .where(events.c.event_type.in_(counted))
        .group_by(*group)
    )
    return [EventCount(*row) for row in rows]


@dataclass(frozen=True)
class ProviderCallCount:
    provider: str
    model: str
    error_class: str | None  # None for successful calls
    calls: int
    prompt_tokens: int
    completion_tokens: int


def provider_call_counts(session: Session) -> list[ProviderCallCount]:
    group = [provider_calls.c.provider, provider_calls.c.model, provider_calls.c.error_class]
    rows = session.execute(
        select(
            *group,
            func.count(),
            func.coalesce(func.sum(provider_calls.c.prompt_tokens), 0),
            func.coalesce(func.sum(provider_calls.c.completion_tokens), 0),
        ).group_by(*group)
    )
    return [ProviderCallCount(*row) for row in rows]


@dataclass(frozen=True)
class OutboxCounts:
    pending: int  # unpublished and due, or already failing
    scheduled: int  # unpublished, never tried, due later
    dispatch_failures: int  # failed attempts before each published message went out


def outbox_counts(session: Session) -> OutboxCounts:
    failing = outbox.c.dispatch_attempts > 0
    unpublished = outbox.c.published_at.is_(None)
    pending = unpublished & ((outbox.c.available_at <= func.now()) | failing)
    # Counted on published rows only (dispatch_attempts - 1: the last attempt succeeded).
    # Published rows are never deleted while their job exists, so the total never drops;
    # unpublished rows can be (a cancelled queued job), and their failures show up as
    # outbox_pending_messages meanwhile.
    published = outbox.c.published_at.is_not(None)
    row = session.execute(
        select(
            func.count().filter(pending),
            func.count().filter(unpublished & ~pending),
            func.coalesce(func.sum(outbox.c.dispatch_attempts - 1).filter(published), 0),
        )
    ).one()
    return OutboxCounts(pending=row[0], scheduled=row[1], dispatch_failures=max(0, int(row[2])))
