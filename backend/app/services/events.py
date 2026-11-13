"""Reading a job's events for replay (SSE and the history route).

Why reading "seq > cursor" in a loop never misses an event: each event's seq is allocated from
jobs.last_event_seq while the job row is locked, and committed in the same transaction as the
event. So seq N+1 can't exist until N is committed, and a committed last_event_seq of N means
every event up to N is committed and visible. Each read first takes the job's last_event_seq,
then the events up to it: the second statement sees at least what the first one did.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import JobEvent
from app.db.repositories.events import get_event_position, list_events
from app.domain.enums import JobStatus
from app.domain.state_machine import is_terminal
from app.services.errors import ErrorCode, ServiceError


@dataclass(frozen=True)
class EventPage:
    status: JobStatus
    last_seq: int  # the job's latest event number when this page was read
    events: list[JobEvent]  # after < seq <= last_seq, in seq order, at most `limit`

    @property
    def finished(self) -> bool:
        return is_terminal(self.status)


class EventService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def last_seq(self, job_id: uuid.UUID) -> int:
        with self.session_factory() as session:
            return _position(session, job_id)[1]

    def page(self, job_id: uuid.UUID, *, after: int, limit: int) -> EventPage:
        """One short read (the connection goes back to the pool before this returns)."""
        with self.session_factory() as session:
            status, last_seq = _position(session, job_id)
            events = (
                list_events(session, job_id, after=after, up_to=last_seq, limit=limit)
                if after < last_seq
                else []
            )
        return EventPage(status=JobStatus(status), last_seq=last_seq, events=events)


def _position(session: Session, job_id: uuid.UUID) -> tuple[str, int]:
    position = get_event_position(session, job_id)
    if position is None:
        raise ServiceError(ErrorCode.JOB_NOT_FOUND, "No job exists with this id.")
    return position.status, position.last_event_seq
