"""The recovery loop (app/queue/recovery.py) against the real database: expired leases, lost
messages, concurrency (two loops, a worker finishing at the same instant) and duplicate delivery
around recovery. The Celery round trip is in the e2e tests."""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent, OutboxMessage
from app.db.repositories.jobs import transition_job
from app.domain.enums import AttemptOutcome, EventType, JobStatus
from app.domain.errors import JobError
from app.domain.retry import RetryPolicy
from app.queue.recovery import Recovery, RecoveryResult, run_forever
from app.services.execution import DropReason, RunOutcome
from tests.factories import make_job, make_outbox, utcnow
from tests.integration.test_execution import (
    StageThatCalls,
    _snapshot,
    load,
    make_service,
    new_job,
    session_factory,  # noqa: F401  (fixture)
)

S = JobStatus
GRACE = timedelta(seconds=60)


@pytest.fixture
def recovery(session_factory) -> Recovery:  # noqa: F811
    # random() = 0: the shortest delay of each backoff range (2.5 s for the first retry).
    return Recovery(
        session_factory,
        retry_policy=RetryPolicy(base_seconds=5, max_seconds=300, random=lambda: 0.0),
        lost_message_grace=GRACE,
    )


def expire(engine, job_id: uuid.UUID) -> None:
    """Make the job's open attempt look abandoned: its lease ran out a minute ago."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE job_attempts SET started_at = now() - interval '2 minutes', "
                "heartbeat_at = now() - interval '2 minutes', "
                "lease_expires_at = now() - interval '1 minute' "
                "WHERE job_id = :job AND ended_at IS NULL"
            ),
            {"job": job_id},
        )


def running_job(engine, session_factory, **overrides) -> uuid.UUID:  # noqa: F811
    """A job claimed by a worker that then vanished (claim committed, nothing after it)."""
    job_id = new_job(engine, **overrides)
    claim = make_service(session_factory, worker_id="worker@gone:1").claim(job_id, None)
    assert hasattr(claim, "attempt_id"), claim  # claimed, not dropped
    return job_id


def outbox_of(engine, job_id) -> list[OutboxMessage]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.job_id == job_id)
                .order_by(OutboxMessage.id)
            )
        )


def make_due(engine, job_id) -> None:
    with Session(engine) as session, session.begin():
        session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(retry_at=func.now() - timedelta(milliseconds=1))
        )
        session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.job_id == job_id, OutboxMessage.published_at.is_(None))
            .values(published_at=func.now())
        )


# --- Expired leases -------------------------------------------------------------------------------


def test_expired_lease_with_attempts_left_moves_the_job_to_retry_wait(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)

    assert recovery.run_once() == RecoveryResult(expired=1, requeued=0)

    job, [attempt], events = load(engine, job_id)
    assert job.status == S.RETRY_WAIT
    assert (job.error_class, job.attempt_count) == ("lease_expired", 1)
    assert "stopped responding" in job.error_summary
    assert attempt.outcome == AttemptOutcome.LEASE_EXPIRED and attempt.ended_at is not None
    assert attempt.error_class == "lease_expired"
    [message] = outbox_of(engine, job_id)
    assert message.published_at is None
    assert message.available_at == job.retry_at  # one database instant, as in the worker
    assert [e.event_type for e in events[-2:]] == [
        EventType.ATTEMPT_LEASE_EXPIRED,
        EventType.JOB_STATUS_CHANGED,
    ]
    lease_event, status_event = events[-2:]
    assert lease_event.attempt_no == 1 and lease_event.payload["worker_id"] == "worker@gone:1"
    assert set(lease_event.payload) == {
        "attempt_no",
        "worker_id",
        "last_heartbeat_at",
        "lease_expired_at",
    }
    assert status_event.payload["reason"] == "lease_expired"
    assert status_event.payload["retry_in_seconds"] == 2.5  # first retry: 2.5-5 s


def test_the_recovered_job_runs_again_as_a_recovery_attempt(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)
    recovery.run_once()
    make_due(engine, job_id)

    result = make_service(session_factory).run(job_id)

    assert (result.outcome, result.attempt_no) == (RunOutcome.SUCCEEDED, 2)
    job, attempts, _ = load(engine, job_id)
    assert job.status == S.SUCCEEDED and job.error_class is None
    assert [(a.outcome, a.trigger) for a in attempts] == [
        ("lease_expired", "initial"),
        ("succeeded", "recovery"),
    ]


def test_expired_lease_with_no_attempts_left_fails_the_job(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_id = new_job(engine, S.RETRY_WAIT, attempt_count=2, retry_at=utcnow() - timedelta(1))
    make_service(session_factory).claim(job_id, None)  # attempt 3 of 3
    expire(engine, job_id)

    assert recovery.run_once().expired == 1

    job, [attempt], events = load(engine, job_id)
    assert job.status == S.FAILED and job.finished_at is not None
    assert job.error_summary.endswith("Gave up after 3 attempts.")
    assert attempt.attempt_no == 3 and attempt.outcome == AttemptOutcome.LEASE_EXPIRED
    assert events[-1].payload["gave_up_after_attempts"] == 3
    assert outbox_of(engine, job_id) == []


def test_expired_lease_while_cancel_requested_cancels_the_job(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_id = running_job(engine, session_factory)
    with Session(engine) as session, session.begin():
        transition_job(session, job_id, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
    expire(engine, job_id)

    assert recovery.run_once().expired == 1

    job, [attempt], events = load(engine, job_id)
    assert job.status == S.CANCELLED and job.cancel_requested_at is not None
    assert attempt.outcome == AttemptOutcome.LEASE_EXPIRED
    assert events[-1].payload["to"] == "cancelled"
    assert outbox_of(engine, job_id) == []


def test_a_live_lease_is_left_alone(engine, session_factory, recovery):  # noqa: F811
    job_id = running_job(engine, session_factory)
    before = _snapshot(engine, job_id)
    assert recovery.run_once() == RecoveryResult(0, 0)
    assert _snapshot(engine, job_id) == before


def test_recovery_run_twice_acts_once(engine, session_factory, recovery):  # noqa: F811
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)
    assert recovery.run_once().expired == 1
    after_first = _snapshot(engine, job_id)
    assert recovery.run_once() == RecoveryResult(0, 0)
    assert _snapshot(engine, job_id) == after_first


def test_two_recovery_loops_at_once_handle_each_job_exactly_once(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_ids = [running_job(engine, session_factory) for _ in range(20)]
    for job_id in job_ids:
        expire(engine, job_id)
    start = threading.Barrier(2)
    counts: list[int] = []
    errors: list[BaseException] = []

    def loop() -> None:
        try:
            start.wait()
            counts.append(recovery.expire_leases())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=loop) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == [] and sum(counts) == 20
    with Session(engine) as session:
        per_job = session.execute(
            select(JobEvent.job_id, func.count())
            .where(JobEvent.event_type == EventType.ATTEMPT_LEASE_EXPIRED)
            .group_by(JobEvent.job_id)
        ).all()
        pending = session.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None))
        )
    assert sorted(count for _, count in per_job) == [1] * 20
    assert pending == 20  # one retry message per job, no more


@pytest.mark.parametrize("finish", ["succeed", "transient_failure"])
def test_a_worker_finishing_while_recovery_runs_never_deadlocks(
    engine,
    session_factory,  # noqa: F811
    recovery,
    finish,
):
    """Lock order: worker and recovery both lock the attempt, then the job. Run the worker's
    finish and a recovery pass at the same instant many times; every round must end with exactly
    one of them in charge, and no deadlock error anywhere."""
    for _ in range(15):
        job_id = new_job(engine)
        results = _finish_and_recover_at_once(engine, session_factory, recovery, job_id, finish)
        job, [attempt], _ = load(engine, job_id)
        assert attempt.error_class != "database_conflict"  # a deadlock would show up here
        if results["recovery"] == 1:  # recovery closed the attempt first
            assert results["worker"] == RunOutcome.FENCED
            assert attempt.outcome == AttemptOutcome.LEASE_EXPIRED
            assert job.status == S.RETRY_WAIT and job.error_class == "lease_expired"
        else:  # the worker renewed or finished first
            expected = {
                "succeed": (RunOutcome.SUCCEEDED, AttemptOutcome.SUCCEEDED, S.SUCCEEDED),
                "transient_failure": (
                    RunOutcome.RETRY_SCHEDULED,
                    AttemptOutcome.FAILED_TRANSIENT,
                    S.RETRY_WAIT,
                ),
            }[finish]
            assert (results["worker"], attempt.outcome, job.status) == expected


def _finish_and_recover_at_once(engine, session_factory, recovery, job_id, finish):  # noqa: F811
    both_ready = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def last_stage(context):
        expire(engine, job_id)  # to recovery, this worker now looks dead
        both_ready.wait()
        if finish == "transient_failure":
            raise JobError("provider_timeout")

    def work():
        try:
            service = make_service(session_factory, StageThatCalls(last_stage))
            results["worker"] = service.run(job_id).outcome
        except BaseException as exc:
            errors.append(exc)

    def recover():
        try:
            both_ready.wait()
            results["recovery"] = recovery.expire_leases()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work), threading.Thread(target=recover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    return results


def test_the_loop_survives_a_failing_pass_and_stops_when_asked(monkeypatch, recovery):
    calls = []

    def flaky_pass():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database down")
        stop.set()
        return RecoveryResult(0, 0)

    monkeypatch.setattr(recovery, "run_once", flaky_pass)
    stop = threading.Event()
    run_forever(recovery, interval_seconds=0.01, stop=stop, backoff=lambda _: 0.01)
    assert len(calls) == 2


# --- Lost messages --------------------------------------------------------------------------------


def lost_job(engine, *, status=S.QUEUED, published_ago=timedelta(minutes=10), **overrides):
    """A job whose last message went out `published_ago` (None: never had one) and that hasn't
    changed since."""
    old = utcnow() - timedelta(minutes=10)
    with Session(engine) as session, session.begin():
        job = make_job(session, status=status, updated_at=old, created_at=old, **overrides)
        if published_ago is not None:
            make_outbox(session, job, published_at=utcnow() - published_ago, created_at=old)
        return job.id


def requeue_events(engine, job_id) -> list[JobEvent]:
    _, _, events = load(engine, job_id)
    return [e for e in events if e.event_type == EventType.JOB_REQUEUED]


def test_a_lost_message_is_requeued_exactly_once(engine, recovery):
    job_id = lost_job(engine)

    assert recovery.run_once() == RecoveryResult(expired=0, requeued=1)
    assert recovery.run_once() == RecoveryResult(0, 0)  # a message is pending now

    published, pending = outbox_of(engine, job_id)
    assert pending.published_at is None
    [event] = requeue_events(engine, job_id)
    assert event.payload["reason"] == "lost_message" and event.payload["status"] == "queued"
    assert event.payload["last_published_at"] == published.published_at.isoformat()

    # Published again, the new message is "recent": still nothing to do within the grace period.
    make_due(engine, job_id)
    assert recovery.run_once() == RecoveryResult(0, 0)


@pytest.mark.parametrize(
    ("description", "kwargs"),
    [
        ("message still waiting in the outbox", {"published_ago": None, "pending": True}),
        ("published within the grace period", {"published_ago": timedelta(seconds=10)}),
        ("retry not overdue", {"status": S.RETRY_WAIT, "retry_at_in": timedelta(seconds=30)}),
    ],
)
def test_no_requeue_while_a_message_may_still_arrive(engine, recovery, description, kwargs):
    pending = kwargs.pop("pending", False)
    retry_at_in = kwargs.pop("retry_at_in", None)
    if retry_at_in is not None:
        kwargs["retry_at"] = utcnow() + retry_at_in
    job_id = lost_job(engine, **kwargs)
    if pending:
        with Session(engine) as session, session.begin():
            make_outbox(session, session.get(Job, job_id))
    assert recovery.run_once() == RecoveryResult(0, 0), description


def test_a_freshly_queued_job_is_not_lost_yet(engine, recovery):
    new_job(engine)  # created now, message never published yet: not this loop's business
    assert recovery.run_once() == RecoveryResult(0, 0)


def test_an_overdue_retry_whose_message_was_lost_is_requeued(engine, recovery):
    job_id = lost_job(engine, status=S.RETRY_WAIT, retry_at=utcnow() - timedelta(minutes=5))
    assert recovery.run_once().requeued == 1
    [event] = requeue_events(engine, job_id)
    assert event.payload["status"] == "retry_wait"


def test_a_running_job_is_never_requeued(engine, session_factory, recovery):  # noqa: F811
    job_id = running_job(engine, session_factory)
    with Session(engine) as session, session.begin():
        session.execute(
            update(OutboxMessage).values(published_at=func.now() - timedelta(minutes=10))
        )
    assert recovery.run_once() == RecoveryResult(0, 0)
    assert requeue_events(engine, job_id) == []


def test_two_loops_requeue_each_lost_job_once(engine, recovery):
    job_ids = [lost_job(engine) for _ in range(10)]
    start = threading.Barrier(2)
    counts: list[int] = []

    def loop():
        start.wait()
        counts.append(recovery.requeue_lost_messages())

    threads = [threading.Thread(target=loop) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sum(counts) == 10
    for job_id in job_ids:
        assert len(requeue_events(engine, job_id)) == 1
        assert [m.published_at is None for m in outbox_of(engine, job_id)] == [False, True]


# --- Duplicate delivery around recovery (acceptance #7) -------------------------------------------


def attempts_started(engine, job_id) -> list[int]:
    _, _, events = load(engine, job_id)
    return [e.attempt_no for e in events if e.event_type == EventType.ATTEMPT_STARTED]


def test_a_duplicate_during_a_running_attempt_changes_nothing(
    engine,
    session_factory,  # noqa: F811
):
    job_id = running_job(engine, session_factory)
    before = _snapshot(engine, job_id)

    result = make_service(session_factory).run(job_id)

    assert (result.drop_reason, result.job_status) == (DropReason.NOT_CLAIMABLE, S.RUNNING)
    assert _snapshot(engine, job_id) == before


def test_the_old_message_redelivered_after_recovery_waits_for_the_backoff(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    """Redis redelivers the dead worker's message (visibility timeout) while the job waits in
    retry_wait: the claim refuses it until the retry is due."""
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)
    recovery.run_once()
    before = _snapshot(engine, job_id)

    result = make_service(session_factory).run(job_id)

    assert result.drop_reason == DropReason.RETRY_NOT_DUE
    assert _snapshot(engine, job_id) == before


def test_duplicate_deliveries_after_recovery_start_one_attempt(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)
    recovery.run_once()
    make_due(engine, job_id)
    start = threading.Barrier(2)
    outcomes = []

    def deliver(n):
        start.wait()
        outcomes.append(make_service(session_factory, worker_id=f"worker@dup:{n}").run(job_id))

    threads = [threading.Thread(target=deliver, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(r.outcome for r in outcomes) == [RunOutcome.DROPPED, RunOutcome.SUCCEEDED]
    assert attempts_started(engine, job_id) == [1, 2]
    _, attempts, events = load(engine, job_id)
    assert [a.attempt_no for a in attempts] == [1, 2]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))  # gap-free, no doubles


def test_recovery_after_a_duplicate_already_restarted_the_job_does_nothing(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    """A redelivered message claimed the due retry; a later recovery pass must leave the new,
    live attempt alone."""
    job_id = running_job(engine, session_factory)
    expire(engine, job_id)
    recovery.run_once()
    make_due(engine, job_id)
    make_service(session_factory).claim(job_id, None)  # attempt 2, alive
    before = _snapshot(engine, job_id)

    assert recovery.run_once() == RecoveryResult(0, 0)
    assert _snapshot(engine, job_id) == before
    _, attempts, _ = load(engine, job_id)
    assert [a.ended_at is None for a in attempts] == [False, True]


def test_one_row_that_cannot_be_recovered_does_not_block_the_others(
    engine,
    session_factory,  # noqa: F811
    recovery,
    monkeypatch,
):
    broken, healthy = (running_job(engine, session_factory) for _ in range(2))
    for job_id in (broken, healthy):
        expire(engine, job_id)
    lost = lost_job(engine)
    real_expire = recovery._expire

    def expire_or_fail(session, attempt):
        if attempt.job_id == broken:
            raise RuntimeError("a bug that only this row triggers")
        real_expire(session, attempt)

    monkeypatch.setattr(recovery, "_expire", expire_or_fail)

    assert recovery.run_once() == RecoveryResult(expired=1, requeued=1)

    assert load(engine, healthy)[0].status == S.RETRY_WAIT
    assert len(requeue_events(engine, lost)) == 1
    job, [attempt], _ = load(engine, broken)  # rolled back, untouched, tried again next pass
    assert job.status == S.RUNNING and attempt.ended_at is None
    monkeypatch.setattr(recovery, "_expire", real_expire)
    assert recovery.run_once().expired == 1
