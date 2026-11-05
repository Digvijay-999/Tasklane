"""The worker's job execution (claim, stages, outcome) against the real database.

These call JobExecutionService directly; the Celery round trip is covered by the end-to-end tests.
"""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobAttempt, JobEvent
from app.db.repositories import attempts as attempt_rows
from app.db.repositories.jobs import transition_job
from app.db.session import create_session_factory
from app.domain.enums import AttemptOutcome, ErrorClass, EventType, JobStatus
from app.services.execution import (
    INTERNAL_ERROR_SUMMARY,
    DropReason,
    JobExecutionService,
    RunOutcome,
)
from app.workflows.dummy import DummyStage
from tests.db_utils import backend_pid, wait_until_blocked_by
from tests.factories import make_attempt, make_job, utcnow

S = JobStatus
WORKER = "worker@test-host:4242"


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


def instant_dummy() -> DummyStage:
    return DummyStage(duration_seconds=0, sleep=lambda _: None)


def make_service(session_factory, *stages, worker_id: str = WORKER) -> JobExecutionService:
    stage_list = list(stages) or [instant_dummy()]
    return JobExecutionService(
        session_factory,
        worker_id=worker_id,
        lease=timedelta(seconds=30),
        stages_for=lambda _workflow: stage_list,
    )


def new_job(engine, status: JobStatus = S.QUEUED, **overrides) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        return make_job(session, status=status, **overrides).id


def load(engine, job_id) -> tuple[Job, list[JobAttempt], list[JobEvent]]:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        attempts = list(
            session.scalars(
                select(JobAttempt)
                .where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.attempt_no)
            )
        )
        events = list(
            session.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq)
            )
        )
        session.expunge_all()
        return job, attempts, events


class StageThatCalls:
    """A stage that runs `action(context)` and then finishes."""

    def __init__(self, action, name="custom"):
        self.action = action
        self.name = name

    def run(self, context):
        self.action(context)


# --- Successful run -------------------------------------------------------------------------------


def test_queued_job_is_claimed_run_and_succeeds(engine, session_factory):
    job_id = new_job(engine)

    result = make_service(session_factory).run(job_id, task_id="celery-task-1")

    assert (result.outcome, result.attempt_no) == (RunOutcome.SUCCEEDED, 1)
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.SUCCEEDED
    assert (job.attempt_count, job.progress_percent, job.current_stage) == (1, 100, "dummy")
    assert job.started_at is not None and job.finished_at is not None
    assert attempt.outcome == AttemptOutcome.SUCCEEDED and attempt.ended_at is not None
    assert (attempt.trigger, attempt.worker_id, attempt.task_id) == (
        "initial",
        WORKER,
        "celery-task-1",
    )
    assert attempt.lease_expires_at > attempt.started_at

    assert [(e.event_type, e.payload.get("to") or e.payload.get("percent")) for e in events] == [
        (EventType.JOB_STATUS_CHANGED, "running"),
        (EventType.ATTEMPT_STARTED, None),
        (EventType.STAGE_STARTED, None),
        (EventType.STAGE_PROGRESS, 20),
        (EventType.STAGE_PROGRESS, 40),
        (EventType.STAGE_PROGRESS, 60),
        (EventType.STAGE_PROGRESS, 80),
        (EventType.STAGE_PROGRESS, 100),
        (EventType.STAGE_COMPLETED, None),
        (EventType.JOB_STATUS_CHANGED, "succeeded"),
    ]
    assert [e.seq for e in events] == list(range(1, 11))  # gap-free
    assert all(e.attempt_no == 1 for e in events)
    assert events[1].payload == {"attempt_no": 1, "trigger": "initial", "worker_id": WORKER}


def test_progress_renews_the_lease(engine, session_factory):
    job_id = new_job(engine)
    leases = []

    def report_twice(context):
        for percent in (50, 100):
            context.report_progress(percent)
            with Session(engine) as session:
                leases.append(
                    session.scalar(
                        select(JobAttempt.lease_expires_at).where(JobAttempt.job_id == job_id)
                    )
                )

    make_service(session_factory, StageThatCalls(report_twice)).run(job_id)
    assert leases[1] > leases[0]  # each report pushed the lease further out


@pytest.mark.parametrize(
    ("status", "overrides", "trigger"),
    [
        (S.RETRY_WAIT, {"retry_at": utcnow() - timedelta(seconds=1)}, "auto_retry"),
        # Queued again after an earlier failed attempt (a manual retry).
        (S.QUEUED, {"attempt_count": 1, "max_attempts": 4, "started_at": utcnow()}, "manual_retry"),
    ],
)
def test_attempt_trigger_reflects_why_the_job_is_running(
    engine, session_factory, status, overrides, trigger
):
    job_id = new_job(engine, status, **overrides)
    with Session(engine) as session, session.begin():
        make_attempt(
            session,
            session.get(Job, job_id),
            attempt_no=1,
            ended_at=utcnow(),
            outcome=AttemptOutcome.FAILED_TRANSIENT,
        )

    result = make_service(session_factory).run(job_id)

    assert result.outcome == RunOutcome.SUCCEEDED
    _, attempts, _ = load(engine, job_id)
    assert [(a.attempt_no, a.trigger) for a in attempts] == [(1, "initial"), (2, trigger)]


# --- Duplicate and stale messages ----------------------------------------------------------------


def _snapshot(engine, job_id):
    job, attempts, events = load(engine, job_id)
    return job.status, job.last_event_seq, job.updated_at, len(attempts), len(events)


@pytest.mark.parametrize(
    "status",
    [S.RUNNING, S.CANCEL_REQUESTED, S.SUCCEEDED, S.FAILED, S.CANCELLED],
    ids=lambda s: s.value,
)
def test_message_for_a_job_that_is_not_claimable_is_dropped_without_writes(
    engine, session_factory, status
):
    job_id = new_job(engine, status)
    before = _snapshot(engine, job_id)

    result = make_service(session_factory).run(job_id)

    assert (result.outcome, result.drop_reason, result.job_status) == (
        RunOutcome.DROPPED,
        DropReason.NOT_CLAIMABLE,
        status,
    )
    assert _snapshot(engine, job_id) == before


def test_job_cancelled_while_queued_is_not_run(engine, session_factory, api):
    """The cancel route arrives in Phase 7; here the job is cancelled directly in the database."""
    job_id = new_job(engine)
    with Session(engine) as session, session.begin():
        transition_job(session, job_id, from_status=S.QUEUED, to_status=S.CANCELLED)
    before = _snapshot(engine, job_id)

    result = make_service(session_factory).run(job_id)

    assert result.drop_reason == DropReason.NOT_CLAIMABLE
    assert _snapshot(engine, job_id) == before
    assert api.get(f"/v1/jobs/{job_id}").json()["attempts"] == []


def test_retry_that_is_not_due_yet_is_dropped(engine, session_factory):
    job_id = new_job(engine, S.RETRY_WAIT, retry_at=utcnow() + timedelta(minutes=5))
    before = _snapshot(engine, job_id)

    result = make_service(session_factory).run(job_id)

    assert (result.outcome, result.drop_reason) == (RunOutcome.DROPPED, DropReason.RETRY_NOT_DUE)
    assert _snapshot(engine, job_id) == before


def test_message_for_a_missing_job_is_dropped(session_factory):
    result = make_service(session_factory).run(uuid.uuid4())
    assert (result.outcome, result.drop_reason) == (RunOutcome.DROPPED, DropReason.JOB_NOT_FOUND)


def test_second_delivery_after_success_is_dropped(engine, session_factory):
    job_id = new_job(engine)
    service = make_service(session_factory)
    assert service.run(job_id).outcome == RunOutcome.SUCCEEDED
    assert service.run(job_id).drop_reason == DropReason.NOT_CLAIMABLE
    _, attempts, _ = load(engine, job_id)
    assert len(attempts) == 1


def test_concurrent_duplicate_deliveries_run_the_job_once(engine, session_factory):
    job_id = new_job(engine)
    services = [make_service(session_factory, worker_id=f"worker@test:{n}") for n in (1, 2)]
    barrier = threading.Barrier(2)
    results = []

    def deliver(service):
        barrier.wait()
        results.append(service.run(job_id))

    threads = [threading.Thread(target=deliver, args=(s,)) for s in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(r.outcome for r in results) == [RunOutcome.DROPPED, RunOutcome.SUCCEEDED]
    job, attempts, _ = load(engine, job_id)
    assert job.status == S.SUCCEEDED and len(attempts) == 1


def test_lost_claim_race_writes_nothing(engine, session_factory):
    """Another worker's claim is in flight (uncommitted); ours waits on the row lock, then
    sees the job running and drops its message."""
    job_id = new_job(engine)
    outcome = {}

    with Session(engine) as other_worker:
        transition_job(
            other_worker,
            job_id,
            from_status=S.QUEUED,
            to_status=S.RUNNING,
            changes={"attempt_count": 1},
        )

        def our_worker():
            outcome["result"] = make_service(session_factory).run(job_id)

        thread = threading.Thread(target=our_worker)
        thread.start()
        wait_until_blocked_by(engine, backend_pid(other_worker))
        other_worker.commit()
    thread.join(timeout=10)
    assert not thread.is_alive()

    result = outcome["result"]
    # Our claim waited on the row lock, then found the job already running.
    assert (result.outcome, result.drop_reason, result.job_status) == (
        RunOutcome.DROPPED,
        DropReason.NOT_CLAIMABLE,
        S.RUNNING,
    )
    job, attempts, events = load(engine, job_id)
    assert attempts == []  # we inserted no attempt
    assert [e.event_type for e in events] == [EventType.JOB_STATUS_CHANGED]  # only theirs


# --- Failures -------------------------------------------------------------------------------------


@pytest.mark.parametrize("progress_first", [False, True])
def test_unexpected_exception_fails_the_job_with_a_safe_summary(
    engine, session_factory, progress_first, caplog
):
    job_id = new_job(engine)

    def explode(context):
        if progress_first:
            context.report_progress(50)
        raise RuntimeError("secret detail: api_key=sk-123")

    result = make_service(session_factory, StageThatCalls(explode)).run(job_id)

    assert result.outcome == RunOutcome.FAILED
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.FAILED and job.finished_at is not None
    assert (job.error_class, job.error_summary) == (
        ErrorClass.INTERNAL_ERROR,
        INTERNAL_ERROR_SUMMARY,
    )
    assert attempt.outcome == AttemptOutcome.FAILED_PERMANENT
    assert (attempt.error_class, attempt.error_summary) == (
        ErrorClass.INTERNAL_ERROR,
        INTERNAL_ERROR_SUMMARY,
    )
    assert events[-1].payload["to"] == "failed"
    stored = repr((job.error_summary, attempt.error_summary, [e.payload for e in events]))
    assert "sk-123" not in stored  # the exception text only goes to the logs
    assert "sk-123" in caplog.text


def test_failure_while_cancel_requested_still_ends_failed(engine, session_factory):
    job_id = new_job(engine)

    def cancel_then_explode(context):
        with Session(engine) as session, session.begin():
            transition_job(session, job_id, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
        raise RuntimeError("boom")

    make_service(session_factory, StageThatCalls(cancel_then_explode)).run(job_id)
    job, _, events = load(engine, job_id)
    assert job.status == S.FAILED  # a definite outcome wins over the pending cancel
    assert events[-1].event_type == EventType.JOB_CANCEL_SUPERSEDED


# --- Fencing and cancellation ---------------------------------------------------------------------


def test_worker_whose_attempt_was_closed_stops_without_writing(engine, session_factory):
    """Simulates Phase 7 recovery closing a (presumed dead) attempt while the worker still runs."""
    job_id = new_job(engine)

    def lose_ownership_then_report(context):
        with Session(engine) as session, session.begin():
            attempt_id = session.scalar(select(JobAttempt.id).where(JobAttempt.job_id == job_id))
            attempt_rows.close_attempt(session, attempt_id, AttemptOutcome.LEASE_EXPIRED)
        context.report_progress(50)  # fenced: raises
        raise AssertionError("unreachable")

    result = make_service(session_factory, StageThatCalls(lose_ownership_then_report)).run(job_id)

    assert result.outcome == RunOutcome.FENCED
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.RUNNING  # untouched: deciding what happens next is recovery's job
    assert attempt.outcome == AttemptOutcome.LEASE_EXPIRED
    assert EventType.STAGE_PROGRESS not in [e.event_type for e in events]
    assert EventType.STAGE_COMPLETED not in [e.event_type for e in events]


def test_cancel_requested_during_the_last_stage_is_superseded_by_success(engine, session_factory):
    job_id = new_job(engine)

    def request_cancel(context):
        with Session(engine) as session, session.begin():
            transition_job(session, job_id, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
        context.report_progress(100)

    result = make_service(session_factory, StageThatCalls(request_cancel)).run(job_id)

    assert result.outcome == RunOutcome.SUCCEEDED
    job, _, events = load(engine, job_id)
    assert job.status == S.SUCCEEDED and job.cancel_requested_at is not None
    assert [e.event_type for e in events[-2:]] == [
        EventType.JOB_STATUS_CHANGED,
        EventType.JOB_CANCEL_SUPERSEDED,
    ]
    assert events[-1].payload["reason"] == "completed_before_cancel"


# --- Failure-path edge cases (review follow-ups) ---------------------------------------------


def test_if_recording_the_failure_fails_the_error_propagates_and_the_job_stays_running(
    engine, session_factory, monkeypatch
):
    """E.g. the database goes down mid-job. The Celery task then retries the message (see
    test_queue_units); the redelivery is dropped because the job is `running`, and the job waits
    for lease recovery (Phase 7)."""
    from sqlalchemy.exc import OperationalError

    job_id = new_job(engine)

    def database_down(*args, **kwargs):
        raise OperationalError("UPDATE job_attempts", {}, Exception("connection lost"))

    def explode(context):
        monkeypatch.setattr(attempt_rows, "close_attempt", database_down)
        raise RuntimeError("stage failed")

    with pytest.raises(OperationalError):
        make_service(session_factory, StageThatCalls(explode)).run(job_id)

    job, [attempt], _ = load(engine, job_id)
    assert job.status == S.RUNNING
    assert attempt.ended_at is None


def test_finishing_retries_when_a_cancel_request_lands_mid_transaction(
    engine, session_factory, monkeypatch
):
    """A user's cancel can commit between reading the status and the conditional update that
    finishes the job; the finish re-reads and applies the right outcome instead of giving up."""
    import app.services.execution as execution

    job_id = new_job(engine)
    original = execution.job_rows.transition_job
    injected = []

    def cancel_first_then_transition(session, jid, *, from_status, to_status, **kwargs):
        if from_status == S.RUNNING and to_status == S.FAILED and not injected:
            injected.append(True)
            with Session(engine) as other, other.begin():  # the user's cancel request
                original(other, jid, from_status=S.RUNNING, to_status=S.CANCEL_REQUESTED)
        return original(session, jid, from_status=from_status, to_status=to_status, **kwargs)

    monkeypatch.setattr(execution.job_rows, "transition_job", cancel_first_then_transition)

    def explode(context):
        raise RuntimeError("boom")

    result = make_service(session_factory, StageThatCalls(explode)).run(job_id)

    assert injected and result.outcome == RunOutcome.FAILED
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.FAILED  # permanent failure beats the pending cancel
    assert attempt.outcome == AttemptOutcome.FAILED_PERMANENT
    assert [e.event_type for e in events[-3:]] == [
        EventType.JOB_STATUS_CHANGED,  # running -> cancel_requested (the user)
        EventType.JOB_STATUS_CHANGED,  # cancel_requested -> failed (the worker, second try)
        EventType.JOB_CANCEL_SUPERSEDED,
    ]
