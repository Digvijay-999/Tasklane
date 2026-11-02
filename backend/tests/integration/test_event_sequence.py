"""Per-job event sequence numbers: gapless, ordered, and safe for SSE replay."""

import threading
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent
from app.db.repositories.jobs import JobNotFoundError, append_event, transition_job
from app.domain.enums import JobStatus
from tests.db_utils import backend_pid, wait_until_blocked_by
from tests.factories import make_job


def _seqs(session: Session, job_id: uuid.UUID) -> list[int]:
    return list(
        session.scalars(
            select(JobEvent.seq).where(JobEvent.job_id == job_id).order_by(JobEvent.seq)
        )
    )


def test_numbers_start_at_one_and_have_no_gaps(session):
    job = make_job(session)
    session.commit()

    append_event(session, job.id, "job.created")
    transition_job(session, job.id, from_status=JobStatus.QUEUED, to_status=JobStatus.RUNNING)
    for stage in ("validate_pdf", "extract_text", "chunk_document"):
        append_event(session, job.id, "stage.started", {"stage": stage})
    session.commit()

    assert _seqs(session, job.id) == [1, 2, 3, 4, 5]
    session.refresh(job)
    assert job.last_event_seq == 5


def test_numbering_is_per_job(session):
    first, second = make_job(session), make_job(session)
    session.commit()
    for _ in range(3):
        append_event(session, first.id, "stage.started")
    append_event(session, second.id, "stage.started")
    session.commit()
    assert _seqs(session, first.id) == [1, 2, 3]
    assert _seqs(session, second.id) == [1]


def test_rolled_back_event_leaves_no_gap(session):
    job = make_job(session)
    session.commit()

    append_event(session, job.id, "stage.started")
    session.commit()
    append_event(session, job.id, "stage.started")
    session.rollback()  # the counter increment is undone together with the event
    append_event(session, job.id, "stage.completed")
    session.commit()

    assert _seqs(session, job.id) == [1, 2]


def test_append_to_missing_job_raises(session):
    with pytest.raises(JobNotFoundError):
        append_event(session, uuid.uuid4(), "stage.started")


def test_concurrent_writers_get_distinct_gapless_numbers(engine):
    with Session(engine) as setup:
        job_id = make_job(setup).id
        setup.commit()

    threads_count, per_thread = 4, 10
    errors: list[Exception] = []

    def writer() -> None:
        try:
            for _ in range(per_thread):
                with Session(engine) as session:
                    append_event(session, job_id, "stage.progress")
                    session.commit()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads), "a writer thread hung"
    assert errors == []
    with Session(engine) as check:
        assert _seqs(check, job_id) == list(range(1, threads_count * per_thread + 1))


def test_a_later_event_cannot_become_visible_before_an_earlier_one(engine):
    """The SSE replay guarantee. While event 1 is uncommitted, event 2 can't even be created, so
    no reader can ever see 2 without 1 and then skip 1 on reconnect."""
    with Session(engine) as setup:
        job_id = make_job(setup).id
        setup.commit()

    second: dict = {}

    def write_second() -> None:
        with Session(engine) as session:
            second["seq"] = append_event(session, job_id, "stage.completed")
            session.commit()

    with Session(engine) as first:
        assert append_event(first, job_id, "stage.started") == 1  # uncommitted

        thread = threading.Thread(target=write_second)
        thread.start()
        # The second writer is blocked on the job row lock that `first` holds.
        wait_until_blocked_by(engine, backend_pid(first))

        with Session(engine) as reader:
            assert _seqs(reader, job_id) == []  # nothing visible yet, in particular not 2

        first.commit()
    thread.join(timeout=5)

    assert not thread.is_alive(), "second writer never finished"
    assert second["seq"] == 2
    with Session(engine) as reader:
        assert _seqs(reader, job_id) == [1, 2]


def test_writers_for_different_jobs_do_not_block_each_other(engine):
    with Session(engine) as setup:
        busy_id, other_id = make_job(setup).id, make_job(setup).id
        setup.commit()

    with Session(engine) as holder:
        append_event(holder, busy_id, "stage.started")  # holds busy job's row lock

        with Session(engine) as other:
            # Fail fast instead of hanging if this ever did wait on a lock.
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            assert append_event(other, other_id, "stage.started") == 1
            other.commit()
        holder.commit()

    with Session(engine) as check:
        assert check.get(Job, busy_id).last_event_seq == 1


def test_append_with_job_id_none_is_rejected_with_a_clear_message(session):
    with pytest.raises(ValueError, match="job_id is None"):
        append_event(session, None, "stage.started")


def test_append_to_a_just_added_unflushed_job(session):
    """The Phase 3 pattern: add a job and its first event in one transaction."""
    from app.db.models import Job
    from tests.factories import make_upload, sha256_hex

    upload = make_upload(session)
    job = Job(
        id=uuid.uuid4(),
        workflow_type="pdf_ingest",
        upload_id=upload.id,
        input_sha256=upload.sha256,
        idempotency_key="key-new-job",
        request_hash=sha256_hex(2),
    )
    session.add(job)  # INSERT still pending; append_event must flush it first
    assert append_event(session, job.id, "job.created") == 1
    session.commit()
    assert _seqs(session, job.id) == [1]
