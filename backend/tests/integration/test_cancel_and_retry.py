"""Cancellation and manual retry (Phase 7): the API routes, the worker stopping at a stage
boundary, deterministic races (with the blocked-connection helper), and the job detail's
can_cancel / can_retry fields."""

import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import JobEvent, OutboxMessage, StageCheckpoint
from app.db.repositories.outbox import enqueue_retry
from app.domain.enums import AttemptOutcome, EventType, JobStatus
from app.domain.errors import JobError
from app.domain.retry import RetryPolicy
from app.services.errors import ErrorCode, ServiceError
from app.services.execution import DropReason, JobExecutionService, RunOutcome
from app.services.jobs import JobService, cancel_job, retry_job
from tests.db_utils import backend_pid, wait_until_blocked_by
from tests.factories import make_job, make_outbox, utcnow
from tests.integration.test_execution import (
    StageThatCalls,
    load,
    make_service,
    new_job,
    session_factory,  # noqa: F401  (fixture)
)

# Importing `api_settings` overrides it for EVERY test in this module (as in test_retries): uploads
# up to 1 MiB and fault injection on. The resume test needs real PDFs; no other test here relies on
# either setting.
from tests.integration.test_retries import (  # noqa: F401  (fixtures)
    api_settings,
    stage_events,
    submit,
    worker,
)

S = JobStatus


def outbox_of(engine, job_id) -> list[OutboxMessage]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.job_id == job_id)
                .order_by(OutboxMessage.id)
            )
        )


def in_background(target, *args) -> tuple[threading.Thread, dict]:
    """Run target(*args) in a thread; its result or exception lands in the returned dict."""
    box: dict = {}

    def run():
        try:
            box["result"] = target(*args)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


# --- Cancel: the route ----------------------------------------------------------------------------


def test_cancelling_a_queued_job_removes_its_pending_message(api, engine):
    with Session(engine) as session, session.begin():
        job = make_job(session)
        make_outbox(session, job)
        job_id = job.id

    response = api.post(f"/v1/jobs/{job_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled" and body["finished_at"] and body["cancel_requested_at"]
    assert (body["can_cancel"], body["cancel_blocked_reason"]) == (False, "already_finished")
    assert outbox_of(engine, job_id) == []
    _, _, events = load(engine, job_id)
    assert events[-1].payload == {
        "requested_by": "api",
        "outbox_removed": 1,
        "from": "queued",
        "to": "cancelled",
    }


def test_a_cancelled_queued_job_never_runs_even_if_its_message_was_delivered(
    api,
    engine,
    session_factory,  # noqa: F811
):
    with Session(engine) as session, session.begin():
        job = make_job(session)
        make_outbox(session, job, published_at=utcnow())  # already in Redis
        job_id = job.id

    assert api.post(f"/v1/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    [published] = outbox_of(engine, job_id)  # history of a sent message stays
    assert published.published_at is not None

    result = make_service(session_factory).run(job_id)  # the message arrives anyway

    assert (result.drop_reason, result.job_status) == (DropReason.NOT_CLAIMABLE, S.CANCELLED)
    assert api.get(f"/v1/jobs/{job_id}").json()["attempts"] == []


def test_cancelling_during_retry_backoff_cancels_at_once(api, engine, session_factory):  # noqa: F811
    with Session(engine) as session, session.begin():
        job = make_job(session, status=S.RETRY_WAIT, retry_at=utcnow() + timedelta(seconds=5))
        enqueue_retry(session, job.id, available_at=job.retry_at)
        job_id = job.id

    body = api.post(f"/v1/jobs/{job_id}/cancel").json()

    assert body["status"] == "cancelled" and body["retry_at"] is None
    assert outbox_of(engine, job_id) == []
    assert make_service(session_factory).run(job_id).drop_reason == DropReason.NOT_CLAIMABLE


@pytest.mark.parametrize("status", [S.SUCCEEDED, S.FAILED, S.CANCELLED, S.CANCEL_REQUESTED])
def test_cancelling_a_finished_or_already_cancelling_job_changes_nothing(api, engine, status):
    job_id = new_job(engine, status)
    before = api.get(f"/v1/jobs/{job_id}").json()

    response = api.post(f"/v1/jobs/{job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["last_event_seq"] == before["last_event_seq"]
    expected = "already_cancel_requested" if status == S.CANCEL_REQUESTED else "already_finished"
    assert response.json()["cancel_blocked_reason"] == expected


def test_repeated_cancels_are_no_ops(api, engine):
    job_id = new_job(engine)
    first = api.post(f"/v1/jobs/{job_id}/cancel").json()
    again = [api.post(f"/v1/jobs/{job_id}/cancel").json() for _ in range(3)]
    assert {r["status"] for r in again} == {"cancelled"}
    assert {r["last_event_seq"] for r in again} == {first["last_event_seq"]}


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-uuid"])
@pytest.mark.parametrize("action", ["cancel", "retry"])
def test_actions_on_an_unknown_job_are_404(api, job_id, action):
    response = api.post(f"/v1/jobs/{job_id}/{action}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


# --- Cancel: the worker ---------------------------------------------------------------------------


def test_cancelling_a_running_job_stops_it_at_the_next_stage_boundary(
    api,
    engine,
    session_factory,  # noqa: F811
):
    job_id = new_job(engine)
    responses = []

    def first(context):
        responses.append(api.post(f"/v1/jobs/{job_id}/cancel").json())
        context.report_progress(100)  # the stage in progress still finishes normally

    def second(context):
        raise AssertionError("the second stage must never start")

    result = make_service(
        session_factory, StageThatCalls(first, name="first"), StageThatCalls(second, name="second")
    ).run(job_id)

    [during] = responses
    assert during["status"] == "cancel_requested" and during["lease"]["attempt_no"] == 1
    assert (during["can_cancel"], during["cancel_blocked_reason"]) == (
        False,
        "already_cancel_requested",
    )
    assert result.outcome == RunOutcome.CANCELLED
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.CANCELLED and job.finished_at is not None
    assert attempt.outcome == AttemptOutcome.CANCELLED and attempt.ended_at is not None
    assert [(e.event_type, e.payload.get("stage") or e.payload.get("to")) for e in events[-2:]] == [
        (EventType.STAGE_COMPLETED, "first"),
        (EventType.JOB_STATUS_CHANGED, "cancelled"),
    ]
    assert events[-1].payload["before_stage"] == "second"
    assert events[-1].attempt_no == 1
    assert "second" not in [e.payload.get("stage") for e in events]


def test_a_job_cancelled_right_after_its_claim_runs_no_stage(
    engine,
    session_factory,  # noqa: F811
    monkeypatch,
):
    job_id = new_job(engine)
    original_claim = JobExecutionService.claim

    def claim_then_user_cancels(self, *args, **kwargs):
        claimed = original_claim(self, *args, **kwargs)
        with Session(engine) as session, session.begin():
            cancel_job(session, job_id)  # lands before the first stage starts
        return claimed

    monkeypatch.setattr(JobExecutionService, "claim", claim_then_user_cancels)

    def first(context):
        raise AssertionError("no stage may start")

    result = make_service(session_factory, StageThatCalls(first, name="first")).run(job_id)

    assert result.outcome == RunOutcome.CANCELLED
    job, [attempt], events = load(engine, job_id)
    assert (job.status, attempt.outcome) == (S.CANCELLED, AttemptOutcome.CANCELLED)
    assert events[-1].payload["before_stage"] == "first"
    assert EventType.STAGE_STARTED not in [e.event_type for e in events]


# --- Cancel: races (deterministic, with the blocked-connection helper) ---------------------------


def test_a_cancel_committed_while_the_last_stage_completes_is_superseded_by_success(
    engine,
    session_factory,  # noqa: F811
):
    job_id = new_job(engine)
    in_stage, go = threading.Event(), threading.Event()

    def last_stage(context):
        in_stage.set()
        assert go.wait(10)

    thread, box = in_background(
        make_service(session_factory, StageThatCalls(last_stage)).run, job_id
    )
    assert in_stage.wait(10)
    with Session(engine) as session, session.begin():
        assert cancel_job(session, job_id)  # running -> cancel_requested; job row locked
        go.set()
        wait_until_blocked_by(engine, backend_pid(session))  # the worker waits for our commit
    thread.join(10)

    assert box["result"].outcome == RunOutcome.SUCCEEDED
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.SUCCEEDED and job.cancel_requested_at is not None
    assert attempt.outcome == AttemptOutcome.SUCCEEDED
    assert events[-1].event_type == EventType.JOB_CANCEL_SUPERSEDED
    assert events[-1].payload == {"outcome": "succeeded", "reason": "completed_before_cancel"}


def test_a_cancel_arriving_while_success_is_being_recorded_changes_nothing(
    engine,
    session_factory,  # noqa: F811
    monkeypatch,
):
    job_id = new_job(engine)
    holding, go = threading.Event(), threading.Event()
    worker_pid = {}
    original = JobExecutionService._move_to_outcome

    def hold_the_finish(self, session, claim, decide, changes=None):
        original(self, session, claim, decide, changes)  # job row now locked by the worker
        worker_pid["pid"] = backend_pid(session)
        holding.set()
        assert go.wait(10)

    monkeypatch.setattr(JobExecutionService, "_move_to_outcome", hold_the_finish)
    finisher, finisher_box = in_background(make_service(session_factory).run, job_id)
    assert holding.wait(10)
    cancel, cancel_box = in_background(JobService(session_factory).cancel, job_id)
    wait_until_blocked_by(engine, worker_pid["pid"])  # the cancel waits for the worker's commit
    go.set()
    finisher.join(10)
    cancel.join(10)

    assert finisher_box["result"].outcome == RunOutcome.SUCCEEDED
    assert cancel_box["result"] is False  # it saw `succeeded`: nothing to cancel
    job, _, events = load(engine, job_id)
    assert job.status == S.SUCCEEDED and job.cancel_requested_at is None
    assert EventType.JOB_CANCEL_SUPERSEDED not in [e.event_type for e in events]


def test_a_permanent_failure_racing_a_cancel_still_ends_failed(
    engine,
    session_factory,  # noqa: F811
):
    job_id = new_job(engine)
    in_stage, go = threading.Event(), threading.Event()

    def failing_stage(context):
        in_stage.set()
        assert go.wait(10)
        raise JobError("pdf_corrupt")

    thread, box = in_background(
        make_service(session_factory, StageThatCalls(failing_stage)).run, job_id
    )
    assert in_stage.wait(10)
    with Session(engine) as session, session.begin():
        assert cancel_job(session, job_id)
        go.set()
        wait_until_blocked_by(engine, backend_pid(session))
    thread.join(10)

    assert box["result"].outcome == RunOutcome.FAILED
    job, [attempt], events = load(engine, job_id)
    assert job.status == S.FAILED and job.error_class == "pdf_corrupt"
    assert attempt.outcome == AttemptOutcome.FAILED_PERMANENT
    assert events[-1].payload == {"outcome": "failed", "reason": "failed_before_cancel"}


# --- Job detail: what the user may do -----------------------------------------------------------


def test_detail_shows_the_lease_while_a_worker_holds_the_job(api, engine, session_factory):  # noqa: F811
    job_id = new_job(engine)
    assert api.get(f"/v1/jobs/{job_id}").json()["lease"] is None
    make_service(session_factory).claim(job_id, None)

    body = api.get(f"/v1/jobs/{job_id}").json()

    assert body["lease"]["attempt_no"] == 1 and body["lease"]["expired"] is False
    assert body["lease"]["worker_id"] == "worker@test-host:4242"
    assert body["lease"]["lease_expires_at"] > body["lease"]["heartbeat_at"]
    assert (body["can_cancel"], body["can_retry"], body["retry_blocked_reason"]) == (
        True,
        False,
        "not_failed",
    )
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "UPDATE job_attempts SET started_at = now() - interval '2 minutes', "
                "heartbeat_at = now() - interval '2 minutes', "
                "lease_expires_at = now() - interval '1 minute' WHERE job_id = :job"
            ),
            {"job": job_id},
        )
    assert api.get(f"/v1/jobs/{job_id}").json()["lease"]["expired"] is True


@pytest.mark.parametrize(
    ("overrides", "can_retry", "reason"),
    [
        ({}, True, None),
        ({"error_class": "pdf_no_text"}, False, "input_error"),
        ({"attempt_count": 18, "max_attempts": 18}, False, "retry_limit_reached"),
    ],
)
def test_detail_says_whether_and_why_a_failed_job_can_be_retried(
    api, engine, overrides, can_retry, reason
):
    job_id = new_job(engine, S.FAILED, **overrides)
    body = api.get(f"/v1/jobs/{job_id}").json()
    assert (body["can_retry"], body["retry_blocked_reason"]) == (can_retry, reason)
    assert (body["can_cancel"], body["cancel_blocked_reason"]) == (False, "already_finished")


# --- Manual retry -------------------------------------------------------------------------------


def failed_job(engine, **overrides) -> uuid.UUID:
    values = {
        "attempt_count": 3,
        "max_attempts": 3,
        "error_class": "provider_timeout",
        "error_summary": "The summarization provider did not respond in time. Gave up after 3 "
        "attempts.",
        "current_stage": "summarize",
        "progress_percent": 70,
    }
    return new_job(engine, S.FAILED, **{**values, **overrides})


def test_manual_retry_reopens_the_job_with_a_fresh_budget_and_a_clean_run(
    api,
    engine,
    session_factory,  # noqa: F811
):
    job_id = failed_job(engine)

    response = api.post(f"/v1/jobs/{job_id}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert (body["attempt_count"], body["max_attempts"], body["manual_retry_count"]) == (3, 6, 1)
    for cleared in ("error_class", "error_summary", "finished_at", "retry_at", "current_stage"):
        assert body[cleared] is None, cleared
    assert body["progress_percent"] == 0 and body["cancel_requested_at"] is None
    assert (body["can_retry"], body["retry_blocked_reason"], body["can_cancel"]) == (
        False,
        "not_failed",
        True,
    )
    [message] = outbox_of(engine, job_id)
    assert message.published_at is None
    _, _, events = load(engine, job_id)
    assert [e.event_type for e in events[-2:]] == [
        EventType.JOB_STATUS_CHANGED,
        EventType.JOB_MANUAL_RETRY,
    ]
    assert events[-1].payload == {
        "manual_retry_count": 1,
        "previous_error_class": "provider_timeout",
        "attempt_count": 3,
        "max_attempts": 6,
    }

    result = make_service(session_factory).run(job_id)
    assert (result.outcome, result.attempt_no) == (RunOutcome.SUCCEEDED, 4)
    _, [attempt], _ = load(engine, job_id)
    assert attempt.trigger == "manual_retry"


@pytest.mark.parametrize(
    ("status", "overrides", "reason"),
    [
        (S.FAILED, {"error_class": "pdf_no_text"}, "input_error"),
        (S.FAILED, {"error_class": "zip_bomb"}, "input_error"),
        (S.QUEUED, {}, "not_failed"),
        (S.RUNNING, {}, "not_failed"),
        (S.SUCCEEDED, {}, "not_failed"),
        (S.CANCELLED, {}, "not_failed"),
    ],
)
def test_ineligible_retries_get_a_409_with_the_reason(api, engine, status, overrides, reason):
    job_id = new_job(engine, status, **overrides)
    before = api.get(f"/v1/jobs/{job_id}").json()

    response = api.post(f"/v1/jobs/{job_id}/retry")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "job_not_retryable"
    assert error["details"] == {"reason": reason, "status": status}
    assert error["message"]
    assert api.get(f"/v1/jobs/{job_id}").json()["last_event_seq"] == before["last_event_seq"]


@pytest.mark.parametrize(("attempt_count", "allowed"), [(17, True), (18, False)])
def test_the_retry_cap_edge(api, engine, attempt_count, allowed):
    """17 + 3 = 20 attempts is the database's limit and still allowed; 18 + 3 would exceed it."""
    job_id = failed_job(engine, attempt_count=attempt_count, max_attempts=attempt_count)
    response = api.post(f"/v1/jobs/{job_id}/retry")
    if allowed:
        assert response.status_code == 200 and response.json()["max_attempts"] == 20
    else:
        assert response.status_code == 409
        assert response.json()["error"]["details"]["reason"] == "retry_limit_reached"


def test_a_retry_clears_a_stale_cancel_timestamp(api, engine):
    """A job that failed while a cancel was pending (failure superseded the cancel) keeps its
    cancel time; the new run must not inherit it (or it would cancel itself)."""
    job_id = failed_job(engine, cancel_requested_at=utcnow())
    body = api.post(f"/v1/jobs/{job_id}/retry").json()
    assert body["status"] == "queued" and body["cancel_requested_at"] is None


def test_backoff_after_a_manual_retry_starts_from_the_first_retry_again(
    api,
    engine,
    session_factory,  # noqa: F811
):
    job_id = failed_job(engine)
    api.post(f"/v1/jobs/{job_id}/retry")

    def transient(context):
        raise JobError("provider_timeout")

    service = JobExecutionService(
        session_factory,
        worker_id="worker@test:1",
        lease=timedelta(seconds=30),
        stages_for=lambda _workflow: [StageThatCalls(transient)],
        retry_policy=RetryPolicy(base_seconds=5, max_seconds=300, random=lambda: 0.0),
    )
    result = service.run(job_id)

    assert (result.outcome, result.attempt_no) == (RunOutcome.RETRY_SCHEDULED, 4)
    _, _, events = load(engine, job_id)
    # 2.5 s, the first retry's minimum (lifetime attempt 4 would have given 20 s).
    assert events[-1].payload["retry_in_seconds"] == 2.5


def test_a_second_retry_request_waits_for_the_first_and_gets_a_409(
    engine,
    session_factory,  # noqa: F811
):
    job_id = failed_job(engine)
    with Session(engine) as session, session.begin():
        retry_job(session, job_id)  # first request, not committed yet; job row locked
        second, box = in_background(JobService(session_factory).retry, job_id)
        wait_until_blocked_by(engine, backend_pid(session))
    second.join(10)

    assert isinstance(box["error"], ServiceError)
    assert box["error"].code == ErrorCode.JOB_NOT_RETRYABLE
    assert box["error"].details["reason"] == "not_failed"
    assert [m.published_at for m in outbox_of(engine, job_id)] == [None]  # one queued run
    _, _, events = load(engine, job_id)
    assert [e.event_type for e in events].count(EventType.JOB_MANUAL_RETRY) == 1


def test_repeated_retry_requests_through_the_api(api, engine):
    job_id = failed_job(engine)
    assert api.post(f"/v1/jobs/{job_id}/retry").status_code == 200
    second = api.post(f"/v1/jobs/{job_id}/retry")
    assert second.status_code == 409
    assert second.json()["error"]["details"] == {"reason": "not_failed", "status": "queued"}
    assert len(outbox_of(engine, job_id)) == 1


class BrokenProvider:
    """A provider with a bug: every call raises an unexpected error (-> internal_error)."""

    name, model = "stub", "stub-extractive-v1"

    def summarize(self, text, options):
        raise RuntimeError("provider bug")


def test_a_manual_retry_resumes_from_verified_checkpoints(api, engine, worker):  # noqa: F811
    job_id = submit(api)
    first = worker(job_id, provider=BrokenProvider())
    assert (first.outcome, first.error_class) == (RunOutcome.FAILED, "internal_error")

    assert api.post(f"/v1/jobs/{job_id}/retry").status_code == 200
    second = worker(job_id)

    assert (second.outcome, second.attempt_no) == (RunOutcome.SUCCEEDED, 2)
    with Session(engine) as session:
        events = session.scalars(
            select(JobEvent).where(JobEvent.job_id == uuid.UUID(job_id)).order_by(JobEvent.seq)
        ).all()
        checkpoints = dict(
            session.execute(
                select(StageCheckpoint.stage, StageCheckpoint.attempt_no).where(
                    StageCheckpoint.job_id == uuid.UUID(job_id)
                )
            ).all()
        )
    assert stage_events(events, 2) == [
        ("stage.skipped", "validate_pdf"),
        ("stage.skipped", "extract_text"),
        ("stage.skipped", "chunk_document"),
        ("stage.started", "summarize"),
        ("stage.started", "write_artifacts"),
    ]
    assert checkpoints == {
        "validate_pdf": 1,
        "extract_text": 1,
        "chunk_document": 1,
        "summarize": 2,
    }
