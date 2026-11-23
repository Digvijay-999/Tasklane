"""Phase 12 hardening against the real database and Redis: a vanished worker can't hold row locks
for long (idle_in_transaction_session_timeout), and recovery doesn't send duplicate messages for
jobs that are only waiting behind a long queue."""

import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InternalError

from app.db.repositories.outbox import DueMessage
from app.db.session import create_db_engine, create_session_factory
from app.domain.enums import AttemptOutcome
from app.domain.retry import RetryPolicy
from app.queue.broker import create_broker_client
from app.queue.celery_app import create_celery_app
from app.queue.dispatcher import CeleryPublisher
from app.queue.recovery import Recovery, RecoveryResult
from app.services.execution import _classify, database_unavailable
from tests.integration.conftest import TEST_QUEUE
from tests.integration.test_execution import load
from tests.integration.test_recovery import GRACE, expire, lost_job, running_job


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


def recovery_for(session_factory, **kwargs) -> Recovery:
    return Recovery(
        session_factory,
        retry_policy=RetryPolicy(base_seconds=5, max_seconds=300, random=lambda: 0.0),
        lost_message_grace=GRACE,
        **kwargs,
    )


# --- A vanished worker's locks --------------------------------------------------------------------


def test_every_connection_has_an_idle_in_transaction_timeout(engine):
    with engine.connect() as connection:
        assert (
            connection.execute(text("SHOW idle_in_transaction_session_timeout")).scalar() == "1min"
        )


def test_a_vanished_worker_cannot_block_recovery_for_long(
    engine,
    session_factory,
    test_settings,
):
    """A worker host disappears while its transaction holds the attempt's row lock (the server
    never hears from it again). Recovery skips the locked row (SKIP LOCKED) until PostgreSQL
    ends the idle session, then recovers the job as usual."""
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)
    vanished_engine = create_db_engine(
        test_settings.model_copy(update={"db_idle_in_transaction_timeout_seconds": 1})
    )
    vanished = vanished_engine.connect()
    try:
        vanished.begin()
        vanished.execute(
            text("SELECT id FROM job_attempts WHERE job_id = :job FOR UPDATE"), {"job": job_id}
        )
        recovery = recovery_for(session_factory)

        assert recovery.run_once() == RecoveryResult(0, 0)  # locked: skipped, not waited for

        deadline = time.monotonic() + 10
        while (result := recovery.run_once()).expired == 0 and time.monotonic() < deadline:
            time.sleep(0.2)
        assert result.expired == 1
        _job, [attempt], _events = load(engine, job_id)
        assert attempt.outcome == AttemptOutcome.LEASE_EXPIRED
        # The server ended the vanished session; a worker that comes back finds its connection
        # gone, and classifies that as the transient database_unavailable.
        with pytest.raises(InternalError) as ended:
            vanished.execute(text("SELECT 1"))
        assert _classify(ended.value, "summarize").error_class == "database_unavailable"
        assert database_unavailable(ended.value)  # the task layer redelivers the message
    finally:
        vanished.invalidate()
        vanished_engine.dispose()


# --- Long queues ----------------------------------------------------------------------------------


def test_queue_depth_reads_the_key_celery_publishes_to(queue_settings, broker):
    """The recovery's queue-depth check (LLEN <queue>) counts what the dispatcher publishes."""
    client = create_broker_client(queue_settings)
    publisher = CeleryPublisher(create_celery_app(queue_settings, for_publishing=True), TEST_QUEUE)
    assert client.llen(TEST_QUEUE) == 0
    publisher.publish(
        DueMessage(id=1, job_id=uuid.uuid4(), task_name="run_job", payload={}, dispatch_attempts=0)
    )
    assert client.llen(TEST_QUEUE) == 1


def test_while_messages_wait_in_the_queue_a_message_counts_as_lost_only_later(
    engine,
    session_factory,
):
    # Grace 1 minute; this job's message went out 3 minutes ago.
    job_id = lost_job(engine, published_ago=timedelta(minutes=3))
    depth = {"value": 25}
    recovery = recovery_for(session_factory, queue_depth=lambda: depth["value"])

    # 25 messages are still waiting: this job's may be one of them. With a long queue the grace
    # is 4 minutes, so no duplicate yet.
    assert recovery.run_once() == RecoveryResult(0, 0)
    # The queue drained and the job still hasn't started: its message was lost after all.
    depth["value"] = 0
    assert recovery.run_once() == RecoveryResult(0, 1)
    _job, _attempts, events = load(engine, job_id)
    assert events[-1].event_type == "job.requeued"


def test_a_queue_that_never_drains_does_not_hide_a_lost_message_forever(
    engine,
    session_factory,
):
    lost_job(engine, published_ago=timedelta(minutes=10))  # past even the long-queue grace (4)
    recovery = recovery_for(session_factory, queue_depth=lambda: 1000)
    assert recovery.run_once() == RecoveryResult(0, 1)


def test_when_redis_is_down_the_lost_message_check_still_runs(
    engine,
    session_factory,
):
    lost_job(engine)

    def unreachable() -> int:
        raise ConnectionError("redis down")

    recovery = recovery_for(session_factory, queue_depth=unreachable)
    assert recovery.run_once() == RecoveryResult(0, 1)
