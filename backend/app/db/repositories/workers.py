"""Worker presence rows (worker_heartbeats, migration 0005). Functions never commit."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import extract, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import WorkerHeartbeat

workers = WorkerHeartbeat.__table__
COUNTER_COLUMNS = ("fenced_attempts", "heartbeat_failures", "heartbeat_thread_deaths")


def record_presence(session: Session, worker_id: str, counters: dict[str, int]) -> None:
    """Insert or refresh this worker process's row: last seen now (database clock), running, and
    its counters as they are in memory right now."""
    values = {name: counters.get(name, 0) for name in COUNTER_COLUMNS}
    statement = insert(workers).values(worker_id=worker_id, **values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[workers.c.worker_id],
            set_={"last_seen_at": func.now(), "stopped_at": None, **values},
        )
    )


def mark_stopped(session: Session, worker_id: str, counters: dict[str, int]) -> None:
    session.execute(
        update(workers)
        .where(workers.c.worker_id == worker_id)
        .values(
            stopped_at=func.now(),
            last_seen_at=func.now(),
            **{name: counters.get(name, 0) for name in COUNTER_COLUMNS},
        )
    )


@dataclass(frozen=True)
class WorkerSummary:
    alive: int  # worker processes that reported within the stale window and didn't stop
    last_seen_seconds: float | None  # since any worker process last reported (None: never)
    fenced_attempts: int  # totals over every row, alive or not
    heartbeat_failures: int
    heartbeat_thread_deaths: int


def worker_summary(session: Session, stale_after: timedelta) -> WorkerSummary:
    alive = workers.c.stopped_at.is_(None) & (workers.c.last_seen_at > func.now() - stale_after)
    row = session.execute(
        select(
            func.count().filter(alive),
            extract("epoch", func.now() - func.max(workers.c.last_seen_at)),
            *(func.coalesce(func.sum(workers.c[name]), 0) for name in COUNTER_COLUMNS),
        )
    ).one()
    return WorkerSummary(
        alive=row[0],
        last_seen_seconds=None if row[1] is None else max(0.0, float(row[1])),
        fenced_attempts=int(row[2]),
        heartbeat_failures=int(row[3]),
        heartbeat_thread_deaths=int(row[4]),
    )
