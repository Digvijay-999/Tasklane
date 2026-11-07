"""Automatic retries, resume from checkpoints, and fault injection, at the service level: real
database, real PDF stages, real parser process. (The Celery round trip is in the e2e tests.)

Between attempts the tests do what the dispatcher would: mark due outbox rows as published.
"""

import dataclasses
import errno
import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import Job, JobAttempt, JobEvent, OutboxMessage, ProviderCall, StageCheckpoint
from app.db.repositories import jobs as job_rows
from app.db.repositories.outbox import enqueue_retry
from app.db.session import create_session_factory
from app.domain.enums import JobStatus
from app.domain.errors import JobError
from app.domain.retry import RetryPolicy
from app.main import create_app
from app.providers.base import ProviderUnavailable
from app.providers.stub import StubProvider
from app.queue.worker_process import workflow_config
from app.services.execution import DropReason, JobExecutionService, RunOutcome
from app.storage.local import LocalStorage
from app.workflows.registry import stages_for
from devtools import pdf_samples
from tests.factories import make_job, make_outbox

# Retries become due almost at once (0.5-1 ms), so a test can run attempt after attempt.
FAST = RetryPolicy(base_seconds=0.001, max_seconds=0.001)
PDF_STAGES = ["validate_pdf", "extract_text", "chunk_document", "summarize", "write_artifacts"]
TRANSIENT = {"mode": "transient"}


@pytest.fixture
def api_settings(api_settings):
    return api_settings.model_copy(
        update={"max_pdf_upload_bytes": 1024 * 1024, "enable_fault_injection": True}
    )


def submit(api, *, options=None, pdf=None) -> str:
    upload = api.post(
        "/v1/uploads",
        params={"filename": "report.pdf"},
        content=pdf or pdf_samples.report_pdf(page_count=4),
        headers={"Content-Type": "application/pdf"},
    )
    assert upload.status_code == 201, upload.text
    body = {"workflow_type": "pdf_ingest", "upload_id": upload.json()["id"]}
    if options is not None:
        body["options"] = options
    job = api.post("/v1/jobs", json=body, headers={"Idempotency-Key": f"retry-{uuid.uuid4()}"})
    assert job.status_code == 201, job.text
    return job.json()["id"]


@pytest.fixture
def worker(engine, api_settings):
    """run(job_id): one attempt, as a worker would run it (after 'publishing' due messages)."""

    def run(
        job_id: str,
        *,
        provider=None,
        stages=None,
        policy=FAST,
        fault_injection_enabled=True,
        **setting_overrides,
    ):
        settings = api_settings.model_copy(update=setting_overrides)
        config = workflow_config(settings)
        if provider is not None:
            config = dataclasses.replace(config, provider=provider)
        publish_due(engine, job_id)
        service = JobExecutionService(
            create_session_factory(engine),
            worker_id="worker@test:1",
            lease=timedelta(seconds=settings.lease_seconds),
            stages_for=(lambda wt: stages) if stages else (lambda wt: stages_for(wt, config)),
            storage=LocalStorage(settings.storage_root),
            retry_policy=policy,
            fault_injection_enabled=fault_injection_enabled,
        )
        return service.run(uuid.UUID(job_id))

    return run


def publish_due(engine, job_id: str) -> None:
    """What the dispatcher does: mark the job's due, unpublished outbox rows as published."""
    with Session(engine) as session, session.begin():
        session.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.job_id == uuid.UUID(job_id),
                OutboxMessage.published_at.is_(None),
                OutboxMessage.available_at <= func.now(),
            )
            .values(published_at=func.now(), dispatch_attempts=1)
        )


def run_until_finished(worker, job_id: str, **kwargs) -> list[RunOutcome]:
    outcomes = []
    for _ in range(10):
        result = worker(job_id, **kwargs)
        if result.outcome == RunOutcome.DROPPED and result.drop_reason == DropReason.RETRY_NOT_DUE:
            continue  # the (1 ms) backoff hadn't quite passed yet
        outcomes.append(result.outcome)
        if result.outcome != RunOutcome.RETRY_SCHEDULED:
            return outcomes
    raise AssertionError(f"job did not finish: {outcomes}")


def load(engine, job_id: str):
    jid = uuid.UUID(job_id)
    with Session(engine) as session:
        job = session.get(Job, jid)
        attempts = session.scalars(
            select(JobAttempt).where(JobAttempt.job_id == jid).order_by(JobAttempt.attempt_no)
        ).all()
        events = session.scalars(
            select(JobEvent).where(JobEvent.job_id == jid).order_by(JobEvent.seq)
        ).all()
        outbox = session.scalars(
            select(OutboxMessage).where(OutboxMessage.job_id == jid).order_by(OutboxMessage.id)
        ).all()
        return job, list(attempts), list(events), list(outbox)


def statuses(events) -> list[str]:
    """The job's status history, from its status-change events."""
    changes = [e.payload for e in events if e.event_type == "job.status_changed"]
    return [changes[0]["from"]] + [change["to"] for change in changes]


def stage_events(events, attempt_no: int) -> list[tuple[str, str]]:
    return [
        (e.event_type, e.payload["stage"])
        for e in events
        if e.attempt_no == attempt_no and e.event_type in ("stage.started", "stage.skipped")
    ]


# --- Acceptance #2: transient failures are retried with backoff, then succeed or give up ---------


def test_two_transient_failures_then_success(api, engine, worker):
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 2}})

    outcomes = run_until_finished(worker, job_id)

    assert outcomes == [
        RunOutcome.RETRY_SCHEDULED,
        RunOutcome.RETRY_SCHEDULED,
        RunOutcome.SUCCEEDED,
    ]
    job, attempts, events, outbox = load(engine, job_id)
    assert statuses(events) == [
        "queued",
        "running",
        "retry_wait",
        "running",
        "retry_wait",
        "running",
        "succeeded",
    ]
    assert [(a.attempt_no, a.trigger, a.outcome) for a in attempts] == [
        (1, "initial", "failed_transient"),
        (2, "auto_retry", "failed_transient"),
        (3, "auto_retry", "succeeded"),
    ]
    assert [a.error_class for a in attempts] == ["injected_transient", "injected_transient", None]
    assert (job.status, job.error_class, job.error_summary, job.retry_at) == (
        "succeeded",
        None,
        None,
        None,
    )
    assert len(outbox) == 3 and all(m.published_at for m in outbox)  # initial + 2 retries
    assert {a["artifact_type"] for a in api.get(f"/v1/jobs/{job_id}").json()["artifacts"]} == {
        "summary",
        "manifest",
    }


def test_three_transient_failures_use_up_the_budget(api, engine, worker):
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 3}})

    outcomes = run_until_finished(worker, job_id)

    assert outcomes == [RunOutcome.RETRY_SCHEDULED, RunOutcome.RETRY_SCHEDULED, RunOutcome.FAILED]
    job, attempts, events, outbox = load(engine, job_id)
    assert job.status == "failed"
    assert job.error_class == "injected_transient"
    assert job.error_summary == (
        "Injected transient failure (fault injection test). Gave up after 3 attempts."
    )
    assert [a.outcome for a in attempts] == ["failed_transient"] * 3
    assert events[-1].payload["gave_up_after_attempts"] == 3
    assert all(m.published_at for m in outbox) and len(outbox) == 3  # no 4th message


def test_retry_at_equals_the_outbox_available_at(api, engine, worker):
    """Both come from the same database-clock instant, so the retry message can never arrive
    before the job is due (and be dropped as 'retry not due')."""
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 1}})
    policy = RetryPolicy(base_seconds=5, max_seconds=300, random=lambda: 0.5)  # 3.75 s

    assert worker(job_id, policy=policy).outcome == RunOutcome.RETRY_SCHEDULED

    job, attempts, events, outbox = load(engine, job_id)
    pending = [m for m in outbox if m.published_at is None]
    assert len(pending) == 1
    assert job.status == "retry_wait"
    assert job.retry_at == pending[0].available_at
    change = events[-1].payload
    assert (change["from"], change["to"]) == ("running", "retry_wait")
    assert change["retry_in_seconds"] == 3.75
    assert change["retry_at"] == job.retry_at.isoformat()
    assert change["error_class"] == "injected_transient"
    failed = [e for e in events if e.event_type == "stage.failed"]
    assert [f.payload for f in failed] == [
        {"stage": "summarize", "error_class": "injected_transient", "retryable": True}
    ]
    # Delivered early (e.g. a duplicate), the retry is refused without writing anything.
    early = worker(job_id, policy=policy)
    assert (early.outcome, early.drop_reason) == (RunOutcome.DROPPED, DropReason.RETRY_NOT_DUE)


def test_the_retry_upsert_pushes_back_an_earlier_pending_message(engine):
    with Session(engine) as session, session.begin():
        job = make_job(session)
        first = make_outbox(session, job)  # pending, due now
        later = session.execute(select(func.now() + timedelta(seconds=30))).scalar_one()
        assert enqueue_retry(session, job.id, available_at=later) == first.id  # same row
        sooner = session.execute(select(func.now() + timedelta(seconds=5))).scalar_one()
        enqueue_retry(session, job.id, available_at=sooner)  # never moves it earlier
        session.expire_all()
        assert session.get(OutboxMessage, first.id).available_at == later


# --- Acceptance #3: permanent errors never retry --------------------------------------------------


def test_an_injected_permanent_failure_is_not_retried(api, engine, worker):
    job_id = submit(api, options={"fault_injection": {"mode": "permanent"}})

    assert run_until_finished(worker, job_id) == [RunOutcome.FAILED]

    job, attempts, events, outbox = load(engine, job_id)
    assert (job.status, job.error_class) == ("failed", "injected_permanent")
    assert job.error_summary == "Injected permanent failure (fault injection test)."
    assert [a.outcome for a in attempts] == ["failed_permanent"]
    assert "retry_wait" not in statuses(events)
    assert len(outbox) == 1


@pytest.mark.parametrize(
    "pdf",
    [pdf_samples.encrypted_pdf(), pdf_samples.image_only_pdf(), pdf_samples.corrupt_pdf()],
    ids=["encrypted", "scanned", "corrupt"],
)
def test_input_errors_are_not_retried_even_with_attempts_left(api, engine, worker, pdf):
    job_id = submit(api, pdf=pdf)
    assert run_until_finished(worker, job_id) == [RunOutcome.FAILED]
    job, attempts, events, outbox = load(engine, job_id)
    assert job.max_attempts == 3 and len(attempts) == 1 and len(outbox) == 1
    assert "retry_wait" not in statuses(events)


# --- Fault injection guards -----------------------------------------------------------------------


def test_the_api_refuses_fault_injection_when_it_is_disabled(api, api_settings):
    off = api_settings.model_copy(update={"enable_fault_injection": False})
    with TestClient(create_app(off)) as client:
        upload = client.post(
            "/v1/uploads",
            params={"filename": "r.pdf"},
            content=pdf_samples.report_pdf(),
            headers={"Content-Type": "application/pdf"},
        ).json()
        response = client.post(
            "/v1/jobs",
            json={
                "workflow_type": "pdf_ingest",
                "upload_id": upload["id"],
                "options": {"fault_injection": {"mode": "transient", "failures": 2}},
            },
            headers={"Idempotency-Key": f"fi-{uuid.uuid4()}"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "fault_injection_disabled"
        assert client.get("/v1/jobs").json()["items"] == []  # nothing was created


@pytest.mark.parametrize(
    "fault_injection",
    [
        {"mode": "sometimes"},
        {"mode": "transient"},  # failures is required
        {"mode": "transient", "failures": 0},
        {"mode": "transient", "failures": 21},
        {"mode": "permanent", "failures": 2},
        {"mode": "transient", "failures": 1, "extra": True},
    ],
)
def test_malformed_fault_injection_options_are_rejected(api, fault_injection):
    upload = api.post(
        "/v1/uploads",
        params={"filename": "r.pdf"},
        content=pdf_samples.report_pdf(),
        headers={"Content-Type": "application/pdf"},
    ).json()
    response = api.post(
        "/v1/jobs",
        json={
            "workflow_type": "pdf_ingest",
            "upload_id": upload["id"],
            "options": {"fault_injection": fault_injection},
        },
        headers={"Idempotency-Key": f"fi-{uuid.uuid4()}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_options_that_are_off_are_not_stored(api):
    job_id = submit(api)
    assert api.get(f"/v1/jobs/{job_id}").json()["options"] == {}
    job_id = submit(api, options={"fault_injection": {"mode": "permanent"}})
    assert api.get(f"/v1/jobs/{job_id}").json()["options"] == {
        "fault_injection": {"mode": "permanent"}
    }


def test_the_worker_checks_the_flag_again(api, engine, worker):
    """Accepted by an API with the flag on, run by a worker with it off: fail clearly."""
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 1}})

    result = worker(job_id, fault_injection_enabled=False)

    assert (result.outcome, result.error_class) == (RunOutcome.FAILED, "fault_injection_disabled")
    job, attempts, events, _ = load(engine, job_id)
    assert job.error_summary == (
        "The job asks for fault injection, but fault injection is disabled on the worker."
    )
    assert [a.outcome for a in attempts] == ["failed_permanent"]
    assert not [e for e in events if e.event_type.startswith("stage.")]  # nothing ran


# --- Resume from checkpoints ----------------------------------------------------------------------


def test_a_retry_skips_stages_whose_checkpoints_verify(api, engine, worker):
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 1}})

    assert run_until_finished(worker, job_id) == [RunOutcome.RETRY_SCHEDULED, RunOutcome.SUCCEEDED]

    _, _, events, _ = load(engine, job_id)
    assert stage_events(events, 1) == [("stage.started", s) for s in PDF_STAGES[:4]]
    assert stage_events(events, 2) == [
        ("stage.skipped", "validate_pdf"),
        ("stage.skipped", "extract_text"),
        ("stage.skipped", "chunk_document"),
        ("stage.started", "summarize"),
        ("stage.started", "write_artifacts"),
    ]
    skipped = [e.payload for e in events if e.event_type == "stage.skipped"]
    assert all(p["reason"] == "checkpoint_reused" for p in skipped)
    with Session(engine) as session:
        rows = session.execute(
            select(StageCheckpoint.stage, StageCheckpoint.attempt_no).where(
                StageCheckpoint.job_id == uuid.UUID(job_id)
            )
        ).all()
    assert dict(rows) == {
        "validate_pdf": 1,
        "extract_text": 1,
        "chunk_document": 1,
        "summarize": 2,  # write_artifacts has no checkpoint
    }


@pytest.mark.parametrize(
    ("damage", "first_rerun"),
    [
        ("edit pages.jsonl", "extract_text"),
        ("delete chunks.jsonl", "chunk_document"),
        ("edit validate.json", "validate_pdf"),
    ],
)
def test_a_tampered_checkpoint_reruns_that_stage_and_everything_after(
    api, engine, worker, api_settings, damage, first_rerun
):
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 1}})
    assert worker(job_id).outcome == RunOutcome.RETRY_SCHEDULED

    action, name = damage.split(" ")
    path = LocalStorage(api_settings.storage_root).absolute(f"work/{job_id}/{name}")
    if action == "delete":
        path.unlink()
    else:  # same size, different content: only the SHA-256 can tell
        data = bytearray(path.read_bytes())
        data[2] = ord("X") if data[2] != ord("X") else ord("Y")
        path.write_bytes(bytes(data))

    assert run_until_finished(worker, job_id) == [RunOutcome.SUCCEEDED]

    _, _, events, _ = load(engine, job_id)
    cut = PDF_STAGES.index(first_rerun)
    assert stage_events(events, 2) == [
        *[("stage.skipped", s) for s in PDF_STAGES[:cut]],
        *[("stage.started", s) for s in PDF_STAGES[cut:]],
    ]


def test_a_changed_setting_invalidates_the_checkpoints_that_depend_on_it(api, engine, worker):
    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 1}})
    assert worker(job_id).outcome == RunOutcome.RETRY_SCHEDULED

    # Chunk sizes changed between attempts (e.g. a redeploy): validate and extract still apply.
    outcomes = run_until_finished(worker, job_id, chunk_target_chars=3000, chunk_max_chars=5000)

    assert outcomes == [RunOutcome.SUCCEEDED]
    _, _, events, _ = load(engine, job_id)
    assert stage_events(events, 2) == [
        ("stage.skipped", "validate_pdf"),
        ("stage.skipped", "extract_text"),
        ("stage.started", "chunk_document"),
        ("stage.started", "summarize"),
        ("stage.started", "write_artifacts"),
    ]


class FlakyProvider:
    """The stub, except that call number `fail_on` (counting across attempts) is unavailable."""

    def __init__(self, fail_on: int) -> None:
        self.inner = StubProvider()
        self.name, self.model = self.inner.name, self.inner.model
        self.fail_on = fail_on
        self.calls = 0

    def summarize(self, text, options):
        self.calls += 1
        if self.calls == self.fail_on:
            raise ProviderUnavailable("503 from the provider")
        return self.inner.summarize(text, options)


def chunk_ids(settings, job_id: str) -> set[str]:
    path = LocalStorage(settings.storage_root).absolute(f"work/{job_id}/chunks.jsonl")
    return {json.loads(line)["chunk_id"] for line in path.read_text().splitlines() if line}


def test_chunks_already_summarized_are_not_sent_to_the_provider_again(
    api, engine, worker, api_settings
):
    job_id = submit(api, pdf=pdf_samples.report_pdf(page_count=6))
    provider = FlakyProvider(fail_on=3)
    small_chunks = {"chunk_target_chars": 800, "chunk_max_chars": 1200, "chunk_overlap_chars": 100}

    outcomes = run_until_finished(worker, job_id, provider=provider, **small_chunks)

    assert outcomes == [RunOutcome.RETRY_SCHEDULED, RunOutcome.SUCCEEDED]
    job, attempts, _, _ = load(engine, job_id)
    assert attempts[0].error_class == "provider_unavailable"
    with Session(engine) as session:
        calls = session.execute(
            select(ProviderCall.attempt_no, ProviderCall.chunk_id, ProviderCall.success).where(
                ProviderCall.job_id == uuid.UUID(job_id), ProviderCall.purpose == "chunk"
            )
        ).all()
    first_ok = {c.chunk_id for c in calls if c.attempt_no == 1 and c.success}
    first_failed = {c.chunk_id for c in calls if c.attempt_no == 1 and not c.success}
    second = [c.chunk_id for c in calls if c.attempt_no == 2]
    all_chunks = chunk_ids(api_settings, job_id)
    assert len(first_ok) == 2 and len(first_failed) == 1 and len(all_chunks) > 3
    assert first_ok.isdisjoint(second)  # never sent again
    assert sorted(second) == sorted(all_chunks - first_ok)  # the rest, each exactly once
    assert job.status == "succeeded"
    # Resuming gives exactly the result a clean run gives.
    clean_id = submit(api, pdf=pdf_samples.report_pdf(page_count=6))
    assert run_until_finished(worker, clean_id, **small_chunks) == [RunOutcome.SUCCEEDED]
    storage = LocalStorage(api_settings.storage_root)
    resumed = storage.absolute(f"work/{job_id}/summary.json").read_bytes()
    assert resumed == storage.absolute(f"work/{clean_id}/summary.json").read_bytes()


# --- Database errors inside a stage are transient -------------------------------------------------


class _PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__("driver detail")
        self.sqlstate = sqlstate


class RaiseOnce:
    """A stage that raises `exc` on attempt 1 and does nothing afterwards."""

    name = "custom"

    def __init__(self, exc):
        self.exc = exc

    def run(self, context):
        if context.job.attempt_no == 1:
            raise self.exc


@pytest.mark.parametrize(
    ("exc", "error_class"),
    [
        (OperationalError("SELECT 1", {}, _PgError("08006")), "database_unavailable"),
        (OperationalError("UPDATE jobs", {}, _PgError("40P01")), "database_conflict"),
        (OSError(errno.ENOSPC, "No space left on device"), "storage_unavailable"),
    ],
    ids=["connection-lost", "deadlock", "disk-full"],
)
def test_database_errors_inside_a_stage_are_retried(engine, worker, exc, error_class):
    with Session(engine) as session, session.begin():
        job_id = str(make_job(session).id)

    outcomes = run_until_finished(worker, job_id, stages=[RaiseOnce(exc)])

    assert outcomes == [RunOutcome.RETRY_SCHEDULED, RunOutcome.SUCCEEDED]
    _, attempts, _, _ = load(engine, job_id)
    assert [(a.outcome, a.error_class) for a in attempts] == [
        ("failed_transient", error_class),
        ("succeeded", None),
    ]
    assert "driver detail" not in (attempts[0].error_summary or "")


def test_a_transient_failure_while_a_cancel_is_pending_ends_cancelled(engine, worker):
    with Session(engine) as session, session.begin():
        job_id = str(make_job(session).id)

    def cancel_then_fail(context):
        with Session(engine) as other, other.begin():  # the user's cancel request
            job_rows.transition_job(
                other,
                context.job.job_id,
                from_status=JobStatus.RUNNING,
                to_status=JobStatus.CANCEL_REQUESTED,
            )
        raise JobError("provider_timeout")

    stage = type("CancelThenFail", (), {"name": "custom", "run": staticmethod(cancel_then_fail)})()
    result = worker(job_id, stages=[stage])

    assert result.outcome == RunOutcome.CANCELLED
    job, attempts, _, outbox = load(engine, job_id)
    assert job.status == "cancelled"
    assert [a.outcome for a in attempts] == ["failed_transient"]
    assert all(m.published_at for m in outbox)  # no retry message


# --- Edge cases of recording a failure ------------------------------------------------------------


def test_a_fenced_worker_records_nothing_for_a_transient_error(engine, worker):
    """Someone else (Phase 7 recovery) closed the attempt: no retry, no events, no message."""
    with Session(engine) as session, session.begin():
        job_id = str(make_job(session).id)

    def lose_the_job_then_fail(context):
        with Session(engine) as other, other.begin():
            other.execute(
                update(JobAttempt)
                .where(JobAttempt.job_id == context.job.job_id, JobAttempt.ended_at.is_(None))
                .values(ended_at=func.now(), outcome="lease_expired")
            )
        raise JobError("provider_timeout")

    stage = type(
        "LoseThenFail", (), {"name": "custom", "run": staticmethod(lose_the_job_then_fail)}
    )()
    result = worker(job_id, stages=[stage])

    assert result.outcome == RunOutcome.FENCED
    job, attempts, events, outbox = load(engine, job_id)
    assert job.status == "running"  # left for recovery (Phase 7)
    assert [a.outcome for a in attempts] == ["lease_expired"]
    assert not [e for e in events if e.event_type == "stage.failed"]
    assert all(m.published_at for m in outbox)


def test_a_permanent_failure_beats_a_pending_cancel(engine, worker):
    with Session(engine) as session, session.begin():
        job_id = str(make_job(session).id)

    def cancel_then_fail(context):
        with Session(engine) as other, other.begin():
            job_rows.transition_job(
                other,
                context.job.job_id,
                from_status=JobStatus.RUNNING,
                to_status=JobStatus.CANCEL_REQUESTED,
            )
        raise JobError("pdf_corrupt")

    stage = type("CancelThenFail", (), {"name": "custom", "run": staticmethod(cancel_then_fail)})()
    assert worker(job_id, stages=[stage]).outcome == RunOutcome.FAILED

    job, _, events, _ = load(engine, job_id)
    assert job.status == "failed"
    assert [e.event_type for e in events[-3:]] == [
        "stage.failed",
        "job.status_changed",  # cancel_requested -> failed
        "job.cancel_superseded",
    ]


def test_a_failure_while_recording_success_is_not_blamed_on_a_stage(
    api, engine, worker, monkeypatch
):
    """E.g. the database drops the connection at the final commit: retried, no stage.failed."""
    import app.services.execution as execution

    job_id = submit(api)
    original = execution.artifact_rows.upsert_artifact
    calls = []

    def flaky_upsert(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("INSERT INTO artifacts", {}, _PgError("08006"))
        return original(*args, **kwargs)

    monkeypatch.setattr(execution.artifact_rows, "upsert_artifact", flaky_upsert)

    assert run_until_finished(worker, job_id) == [RunOutcome.RETRY_SCHEDULED, RunOutcome.SUCCEEDED]
    job, attempts, events, _ = load(engine, job_id)
    assert attempts[0].error_class == "database_unavailable"
    assert not [e for e in events if e.event_type == "stage.failed"]
    assert job.status == "succeeded"


def test_a_deadlock_while_recording_the_failure_is_retried(api, engine, worker, monkeypatch):
    import app.services.execution as execution

    job_id = submit(api, options={"fault_injection": {**TRANSIENT, "failures": 1}})
    original = execution.attempt_rows.close_attempt
    calls = []

    def deadlock_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("UPDATE job_attempts", {}, _PgError("40P01"))
        return original(*args, **kwargs)

    monkeypatch.setattr(execution.attempt_rows, "close_attempt", deadlock_once)

    assert worker(job_id).outcome == RunOutcome.RETRY_SCHEDULED
    job, attempts, _, _ = load(engine, job_id)
    assert job.status == "retry_wait" and attempts[0].outcome == "failed_transient"


def test_replaying_a_fault_injection_request_works_after_the_flag_is_turned_off(api, api_settings):
    """An idempotent replay returns the existing job; it doesn't re-check the flag."""
    upload = api.post(
        "/v1/uploads",
        params={"filename": "r.pdf"},
        content=pdf_samples.report_pdf(),
        headers={"Content-Type": "application/pdf"},
    ).json()
    body = {
        "workflow_type": "pdf_ingest",
        "upload_id": upload["id"],
        "options": {"fault_injection": {"mode": "permanent"}},
    }
    key = {"Idempotency-Key": f"fi-{uuid.uuid4()}"}
    created = api.post("/v1/jobs", json=body, headers=key)
    assert created.status_code == 201

    off = api_settings.model_copy(update={"enable_fault_injection": False})
    with TestClient(create_app(off)) as client:
        replay = client.post("/v1/jobs", json=body, headers=key)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == created.json()["id"]
