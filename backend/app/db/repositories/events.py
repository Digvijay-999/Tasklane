"""Reading a job's event stream (Phase 8). Writing events lives in jobs.py (append_event)."""

import uuid

from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent


def get_event_position(session: Session, job_id: uuid.UUID) -> Row[tuple[str, int]] | None:
    """(status, last_event_seq) of the job, or None if it doesn't exist. No lock."""
    return session.execute(
        select(Job.status, Job.last_event_seq).where(Job.id == job_id)
    ).one_or_none()


def list_events(
    session: Session, job_id: uuid.UUID, *, after: int, up_to: int, limit: int
) -> list[JobEvent]:
    """Events with after < seq <= up_to, in seq order (never by timestamp). Uses the primary
    key (job_id, seq) as the index."""
    return list(
        session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.seq > after, JobEvent.seq <= up_to)
            .order_by(JobEvent.seq)
            .limit(limit)
        )
    )
