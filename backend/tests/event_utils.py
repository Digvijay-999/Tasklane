"""Helpers for the event stream tests (Phase 8): create jobs and events straight in the database,
and parse SSE text with the same parser as devtools/sse_client.py."""

import uuid
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.repositories.jobs import append_event, transition_job
from app.domain.enums import JobStatus
from devtools.sse_client import SseMessage, iter_sse
from tests.factories import make_job


def new_job(engine: Engine, status: JobStatus = JobStatus.RUNNING, events: int = 0) -> uuid.UUID:
    """A committed job (no events yet unless `events` > 0)."""
    with Session(engine) as session:
        job_id = make_job(session, status=status).id
        session.commit()
    if events:
        add_events(engine, job_id, events)
    return job_id


def add_events(
    engine: Engine,
    job_id: uuid.UUID,
    count: int = 1,
    event_type: str = "stage.progress",
    payload: dict[str, Any] | None = None,
) -> list[int]:
    """Append `count` events in one committed transaction; returns their seqs."""
    with Session(engine) as session:
        seqs = [
            append_event(session, job_id, event_type, payload or {"stage": "summarize", "i": i})
            for i in range(count)
        ]
        session.commit()
    return seqs


def finish_job(engine: Engine, job_id: uuid.UUID, to: JobStatus = JobStatus.SUCCEEDED) -> None:
    """running -> succeeded/failed/cancelled, with its job.status_changed event."""
    changes: dict[str, Any] = {}
    if to == JobStatus.FAILED:
        changes = {"error_class": "provider_timeout", "error_summary": "The summarizer timed out."}
    with Session(engine) as session:
        result = transition_job(
            session, job_id, from_status=JobStatus.RUNNING, to_status=to, changes=changes
        )
        assert result.applied
        session.commit()


def parse_sse(text: str) -> list[SseMessage]:
    return list(iter_sse(line.encode() for line in text.splitlines(keepends=True)))


def event_seqs(messages: list[SseMessage]) -> list[int]:
    return [int(m.id) for m in messages if m.id is not None and m.data is not None]
