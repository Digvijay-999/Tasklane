"""Two requests with the same Idempotency-Key at the same time.

These call the service directly from threads with their own database connections, so the race
is real. The deterministic tests hold the key with an uncommitted insert, prove the second
request is blocked on it, and then commit or roll back.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent, OutboxMessage
from app.db.repositories.jobs import insert_job_if_key_unused
from app.db.session import create_session_factory
from app.domain.enums import WorkflowType
from app.domain.idempotency import request_hash
from app.services.errors import ErrorCode, ServiceError
from app.services.jobs import CreateJobCommand, CreateJobResult, JobService
from tests.db_utils import backend_pid, wait_until_blocked_by
from tests.factories import make_upload


@pytest.fixture
def service(engine) -> JobService:
    return JobService(create_session_factory(engine))


@pytest.fixture
def upload_id(engine) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        return make_upload(session).id


def _command(key: str, upload_id: uuid.UUID) -> CreateJobCommand:
    sent = {"workflow_type": "pdf_ingest", "upload_id": str(upload_id)}
    return CreateJobCommand(
        idempotency_key=key,
        workflow_type=WorkflowType.PDF_INGEST,
        upload_id=upload_id,
        resolved_options={},
        sent_fields=sent,
    )


def _hold_key(session: Session, command: CreateJobCommand, *, same_request: bool) -> uuid.UUID:
    """Insert a job with the command's key in `session` without committing."""
    job_id = uuid.uuid4()
    fingerprint = request_hash(command.sent_fields) if same_request else "f" * 64
    inserted = insert_job_if_key_unused(
        session,
        {
            "id": job_id,
            "workflow_type": "pdf_ingest",
            "upload_id": command.upload_id,
            "input_sha256": "1".rjust(64, "0"),
            "options": {},
            "idempotency_key": command.idempotency_key,
            "request_hash": fingerprint,
        },
    )
    assert inserted == job_id
    return job_id


class ServiceCall:
    """Run service.create in a thread; the constructor returns once it is blocked by `holder`."""

    def __init__(self, engine, service: JobService, command, holder: Session) -> None:
        self.outcome: dict = {}
        self.thread = threading.Thread(target=self._run, args=(service, command))
        self.thread.start()
        wait_until_blocked_by(engine, backend_pid(holder))

    def _run(self, service: JobService, command: CreateJobCommand) -> None:
        try:
            self.outcome["result"] = service.create(command)
        except Exception as exc:
            self.outcome["error"] = exc

    def result(self) -> CreateJobResult:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "service call never finished"
        if "error" in self.outcome:
            raise self.outcome["error"]
        return self.outcome["result"]


def _counts(engine) -> tuple[int, int, int]:
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Job, JobEvent, OutboxMessage)
        )


def test_second_request_waits_and_replays_when_the_first_commits(engine, service, upload_id):
    command = _command(f"race-{uuid.uuid4()}", upload_id)
    with Session(engine) as holder:
        held_id = _hold_key(holder, command, same_request=True)
        second = ServiceCall(engine, service, command, holder)  # provably blocked on the key
        holder.commit()

    result = second.result()
    assert result.created is False
    assert result.job.id == held_id
    assert _counts(engine) == (1, 0, 0)  # the second request wrote nothing


def test_second_request_creates_the_job_when_the_first_rolls_back(engine, service, upload_id):
    command = _command(f"race-{uuid.uuid4()}", upload_id)
    with Session(engine) as holder:
        held_id = _hold_key(holder, command, same_request=True)
        second = ServiceCall(engine, service, command, holder)
        holder.rollback()

    result = second.result()
    assert result.created is True
    assert result.job.id != held_id
    assert _counts(engine) == (1, 1, 1)  # job, job.created event, outbox row


def test_concurrent_different_request_with_the_same_key_gets_409(engine, service, upload_id):
    command = _command(f"race-{uuid.uuid4()}", upload_id)
    with Session(engine) as holder:
        _hold_key(holder, command, same_request=False)
        second = ServiceCall(engine, service, command, holder)
        holder.commit()

    with pytest.raises(ServiceError) as exc:
        second.result()
    assert exc.value.code == ErrorCode.IDEMPOTENCY_KEY_REUSED


def test_burst_of_identical_requests_creates_exactly_one_job(engine, service, upload_id):
    command = _command(f"burst-{uuid.uuid4()}", upload_id)
    workers = 10
    barrier = threading.Barrier(workers)

    def call() -> CreateJobResult:
        barrier.wait()  # release all requests at the same moment
        return service.create(command)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: call(), range(workers)))

    assert sum(r.created for r in results) == 1
    assert len({r.job.id for r in results}) == 1
    assert _counts(engine) == (1, 1, 1)
