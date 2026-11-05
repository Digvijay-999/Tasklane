"""The outbox dispatcher against real PostgreSQL and real Redis (no worker running)."""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import OutboxMessage
from app.db.repositories import outbox as outbox_rows
from app.db.session import create_session_factory
from app.queue.celery_app import RUN_JOB_TASK, create_celery_app
from app.queue.dispatcher import CeleryPublisher, Dispatcher
from app.services.execution import JobExecutionService, RunOutcome
from app.workflows.dummy import DummyStage
from tests.broker_utils import queued_messages
from tests.factories import make_job, make_outbox, utcnow
from tests.integration.conftest import TEST_QUEUE


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def publisher(queue_settings, broker):
    return CeleryPublisher(create_celery_app(queue_settings, for_publishing=True), TEST_QUEUE)


@pytest.fixture
def dispatcher(session_factory, publisher):
    return Dispatcher(session_factory, publisher, batch_size=50)


def _queued_job_with_message(engine, **outbox_overrides) -> tuple[uuid.UUID, int]:
    with Session(engine) as session, session.begin():
        job = make_job(session)
        message = make_outbox(session, job, **outbox_overrides)
        return job.id, message.id


def _row(engine, message_id: int) -> OutboxMessage:
    with Session(engine) as session:
        return session.get(OutboxMessage, message_id)


# --- Publishing -----------------------------------------------------------------------------------


def test_due_message_is_published_as_json_and_marked(engine, dispatcher, broker):
    job_id, message_id = _queued_job_with_message(engine)

    result = dispatcher.run_once()

    assert (result.published, result.failed, result.broker_error) == (1, 0, None)
    row = _row(engine, message_id)
    assert row.published_at is not None
    assert row.dispatch_attempts == 1
    assert row.last_dispatch_error is None

    [message] = queued_messages(broker, TEST_QUEUE)
    assert message.task == RUN_JOB_TASK
    assert message.content_type == "application/json"
    assert message.kwargs == {"job_id": str(job_id)}  # nothing but the job id
    assert message.headers["outbox_id"] == message_id


def test_messages_not_yet_due_are_left_alone(engine, dispatcher, broker):
    _, later = _queued_job_with_message(engine, available_at=utcnow() + timedelta(hours=1))
    assert dispatcher.run_once().published == 0
    assert _row(engine, later).published_at is None
    assert queued_messages(broker, TEST_QUEUE) == []


def test_already_published_messages_are_not_sent_again(engine, dispatcher, broker):
    _queued_job_with_message(engine, published_at=utcnow())
    assert dispatcher.run_once().published == 0
    assert queued_messages(broker, TEST_QUEUE) == []


def test_oldest_due_messages_go_first_in_batches(engine, session_factory, publisher, broker):
    ids = [
        _queued_job_with_message(engine, available_at=utcnow() - timedelta(minutes=10 - i))[1]
        for i in range(3)
    ]
    small = Dispatcher(session_factory, publisher, batch_size=2)

    assert small.run_once().published == 2
    assert [_row(engine, i).published_at is not None for i in ids] == [True, True, False]
    assert small.run_once().published == 1
    assert len(queued_messages(broker, TEST_QUEUE)) == 3


def test_due_check_uses_the_database_clock(engine, dispatcher):
    """available_at is compared with Postgres now(), not the dispatcher's clock."""
    with Session(engine) as session:
        db_now = session.scalar(select(func.now()))
    _, message_id = _queued_job_with_message(engine, available_at=db_now + timedelta(seconds=30))
    assert dispatcher.run_once().published == 0
    assert _row(engine, message_id).published_at is None


# --- Two dispatchers: SKIP LOCKED -----------------------------------------------------------------


def test_locked_rows_are_skipped_not_waited_for(engine, dispatcher, broker):
    _, locked_id = _queued_job_with_message(engine)
    _, free_id = _queued_job_with_message(engine)

    with Session(engine) as other_dispatcher:
        # Another dispatcher is mid-pass and holds a lock on one row.
        other_dispatcher.execute(
            select(OutboxMessage.id).where(OutboxMessage.id == locked_id).with_for_update()
        )
        finished = threading.Event()
        results = []

        def run_pass():
            results.append(dispatcher.run_once())
            finished.set()

        thread = threading.Thread(target=run_pass)
        thread.start()
        # SKIP LOCKED means the pass completes without waiting for the lock holder.
        assert finished.wait(timeout=5), "dispatcher waited on a locked row"
        thread.join()
        other_dispatcher.rollback()

    assert results[0].published == 1
    assert _row(engine, free_id).published_at is not None
    assert _row(engine, locked_id).published_at is None  # left for whoever holds it


def test_two_dispatchers_never_publish_the_same_row(engine, session_factory, publisher, broker):
    ids = [_queued_job_with_message(engine)[1] for _ in range(4)]
    first_published = threading.Event()
    release_first = threading.Event()

    class SlowPublisher:
        """Publishes, then pauses mid-batch while its transaction still holds the row locks."""

        def publish(self, message):
            publisher.publish(message)
            first_published.set()
            assert release_first.wait(timeout=10)

    first = Dispatcher(session_factory, SlowPublisher(), batch_size=2)
    second = Dispatcher(session_factory, publisher, batch_size=50)

    thread = threading.Thread(target=first.run_once)
    thread.start()
    assert first_published.wait(timeout=5)
    second_result = second.run_once()  # runs while `first` holds locks on its 2 rows
    release_first.set()
    thread.join(timeout=10)

    assert second_result.published == 2
    assert all(_row(engine, i).published_at is not None for i in ids)
    published_job_ids = [m.kwargs["job_id"] for m in queued_messages(broker, TEST_QUEUE)]
    assert len(published_job_ids) == 4 and len(set(published_job_ids)) == 4  # each exactly once


# --- Redis unavailable ----------------------------------------------------------------------------


def test_redis_down_records_the_error_backs_off_and_keeps_the_job_queued(
    engine, session_factory, queue_settings, api
):
    down = queue_settings.model_copy(update={"redis_url": "redis://redis:1/15"})  # closed port
    dispatcher = Dispatcher(
        session_factory,
        CeleryPublisher(create_celery_app(down, for_publishing=True), TEST_QUEUE),
        batch_size=50,
    )
    job_id, message_id = _queued_job_with_message(engine)

    result = dispatcher.run_once()

    assert result.published == 0 and result.failed == 1
    assert result.broker_error
    row = _row(engine, message_id)
    assert row.published_at is None
    assert row.dispatch_attempts == 1
    assert row.last_dispatch_error == result.broker_error  # the error type, no details
    with Session(engine) as session:
        delay = session.scalar(
            text("SELECT EXTRACT(EPOCH FROM available_at - now()) FROM outbox WHERE id = :id"),
            {"id": message_id},
        )
    assert 0 < delay <= 1  # first failure: retry in 1 s

    # The job is still visible through the API, and still queued.
    assert api.get(f"/v1/jobs/{job_id}").json()["status"] == "queued"

    # A second failure doubles the delay.
    with Session(engine) as session, session.begin():
        session.execute(
            text("UPDATE outbox SET available_at = now() WHERE id = :id"), {"id": message_id}
        )
    dispatcher.run_once()
    row = _row(engine, message_id)
    assert row.dispatch_attempts == 2
    with Session(engine) as session:
        delay = session.scalar(
            text("SELECT EXTRACT(EPOCH FROM available_at - now()) FROM outbox WHERE id = :id"),
            {"id": message_id},
        )
    assert 1 < delay <= 2


def test_redis_failure_mid_batch_keeps_what_was_published(
    engine, session_factory, publisher, broker
):
    ids = [_queued_job_with_message(engine)[1] for _ in range(3)]
    calls = {"n": 0}

    class FailsOnSecond:
        def publish(self, message):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("broker went away")
            publisher.publish(message)

    result = Dispatcher(session_factory, FailsOnSecond(), batch_size=50).run_once()

    assert (result.published, result.failed, result.broker_error) == (1, 2, "ConnectionError")
    rows = [_row(engine, i) for i in ids]
    assert rows[0].published_at is not None
    assert all(r.published_at is None and r.dispatch_attempts == 1 for r in rows[1:])


# --- Crash between publishing and marking ---------------------------------------------------------


def test_crash_after_publish_before_commit_causes_a_duplicate_that_the_worker_drops(
    engine, session_factory, dispatcher, broker, monkeypatch
):
    job_id, message_id = _queued_job_with_message(engine)

    # Simulate a crash after the message reached Redis but before the row was marked: the
    # dispatcher's transaction never commits.
    def crash(*args, **kwargs):
        raise RuntimeError("dispatcher crashed")

    monkeypatch.setattr(outbox_rows, "mark_published", crash)
    with pytest.raises(RuntimeError):
        dispatcher.run_once()
    monkeypatch.undo()

    assert _row(engine, message_id).published_at is None  # rolled back
    assert len(queued_messages(broker, TEST_QUEUE)) == 1

    # The next pass publishes the same row again: a duplicate message.
    assert dispatcher.run_once().published == 1
    messages = queued_messages(broker, TEST_QUEUE)
    assert [m.kwargs["job_id"] for m in messages] == [str(job_id), str(job_id)]

    # The worker runs both deliveries; the job runs exactly once.
    service = JobExecutionService(
        session_factory,
        worker_id="worker@test:1",
        lease=timedelta(seconds=30),
        stages_for=lambda _: [DummyStage(duration_seconds=0, sleep=lambda s: None)],
    )
    outcomes = [service.run(job_id).outcome for _ in messages]
    assert outcomes == [RunOutcome.SUCCEEDED, RunOutcome.DROPPED]
    with Session(engine) as session:
        assert session.scalar(text("SELECT count(*) FROM job_attempts")) == 1
