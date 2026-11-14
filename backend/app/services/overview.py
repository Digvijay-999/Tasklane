"""The dashboard overview: job counts, the oldest queued job, and the outbox's health.

One read-only session; every age is computed by the database clock, so a skewed browser or API
clock can't make a job look older or younger than it is.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import overview as rows
from app.domain.enums import JobStatus


@dataclass(frozen=True)
class Overview:
    counts: dict[str, int]  # every status, zeros included
    oldest_queued: rows.OldestQueued | None
    outbox: rows.OutboxSummary
    database_time: datetime


class OverviewService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def read(self) -> Overview:
        with self.session_factory() as session:
            found = rows.status_counts(session)
            return Overview(
                counts={status.value: found.get(status.value, 0) for status in JobStatus},
                oldest_queued=rows.oldest_queued(session),
                outbox=rows.outbox_summary(session),
                database_time=rows.database_now(session),
            )
