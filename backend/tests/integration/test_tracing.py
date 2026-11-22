"""Traces through the real database (Phase 10): span structure for a job, propagation through the
outbox, and retries, recovery, manual retry and cancel staying in the job's trace.

The Celery round trip is simulated with the real CeleryPublisher and a fake Celery app that
captures the message headers; the real processes are covered in test_observability_e2e.py.
Spans are captured by the in-memory exporter the root conftest installs.
"""

import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, OutboxMessage
from app.db.repositories.jobs import append_event
from app.domain.retry import RetryPolicy
from app.domain.tracing import trace_id_from_traceparent
from app.observability.logging import ContextFilter
from app.queue.dispatcher import CeleryPublisher, Dispatcher
from app.queue.recovery import Recovery
from app.queue.worker_process import workflow_config
from app.services.execution import JobExecutionService, RunOutcome
from app.storage.local import LocalStorage
from app.workflows.registry import stages_for
from devtools import pdf_samples
from tests import telemetry
from tests.factories import make_job
from tests.integration.test_execution import session_factory  # noqa: F401  (fixture)
from tests.integration.test_recovery import expire, lost_job, running_job

PDF_STAGES = ["validate_pdf", "extract_text", "chunk_document", "summarize", "write_artifacts"]
FAST = RetryPolicy(base_seconds=0.001, max_seconds=0.001)
JOB_TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture
def api_settings(api_settings):
    return api_settings.model_copy(
        update={"max_pdf_upload_bytes": 1024 * 1024, "enable_fault_injection": True}
    )


class FakeCelery:
    """Records what CeleryPublisher sends instead of talking to Redis."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_task(self, name, *, kwargs, queue, retry, headers):
        self.sent.append({"name": name, "kwargs": kwargs, "headers": headers})


def submit(api, *, options=None, key=None, body=None) -> dict:
    """Upload a PDF and create a job (or, with `body`, resend an earlier job request)."""
    if body is not None:
        response = api.post("/v1/jobs", json=body, headers={"Idempotency-Key": key})
        assert response.status_code == 200, response.text
        return response.json()
    upload = api.post(
        "/v1/uploads",
        params={"filename": "secret-report-name.pdf"},
        content=pdf_samples.report_pdf(page_count=2),
        headers={"Content-Type": "application/pdf"},
    )
    assert upload.status_code == 201, upload.text
    body = {"workflow_type": "pdf_ingest", "upload_id": upload.json()["id"]}
    if options is not None:
        body["options"] = options
    response = api.post(
        "/v1/jobs", json=body, headers={"Idempotency-Key": key or f"trace-{uuid.uuid4()}"}
    )
    assert response.status_code == 201, response.text
    return {**response.json(), "_body": body}


def dispatch(session_factory) -> list[dict]:  # noqa: F811
    celery = FakeCelery()
    Dispatcher(session_factory, CeleryPublisher(celery, "q"), batch_size=50).run_once()
    return celery.sent


def worker(session_factory, settings, **overrides) -> JobExecutionService:  # noqa: F811
    config = workflow_config(settings)
    return JobExecutionService(
        session_factory,
        worker_id="worker@test:7",
        lease=timedelta(seconds=30),
        stages_for=lambda wt: stages_for(wt, config),
        storage=LocalStorage(settings.storage_root),
        retry_policy=FAST,
        fault_injection_enabled=True,
        **overrides,
    )


def named(records, name) -> list[dict]:
    return [r for r in records if r["name"] == name]


def one(records, name) -> dict:
    [found] = named(records, name)
    return found


def outbox_rows(engine, job_id) -> list[OutboxMessage]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.job_id == uuid.UUID(str(job_id)))
                .order_by(OutboxMessage.id)
            )
        )


def span_id_of(traceparent: str) -> str:
    return traceparent.split("-")[2]


# --- API -> outbox ------------------------------------------------------------------------------


def test_job_creation_stores_its_trace_on_the_job_and_the_outbox_row(api, engine):
    key = f"trace-{uuid.uuid4()}"
    created = submit(api, key=key)
    trace_id = created["trace_id"]
    assert trace_id and len(trace_id) == 32
    assert api.get(f"/v1/jobs/{created['id']}").json()["trace_id"] == trace_id

    records = telemetry.captured()
    server = one(records, "POST /v1/jobs")
    create = one(records, "job.create")
    assert server["trace_id"] == create["trace_id"] == trace_id
    assert create["parent_span_id"] == server["span_id"]
    assert create["attributes"] == {
        "workflow_type": "pdf_ingest",
        "job_id": created["id"],
        "outcome": "created",
    }
    assert server["attributes"]["http.route"] == "/v1/jobs"
    assert server["attributes"]["http.response.status_code"] == 201
    [row] = outbox_rows(engine, created["id"])
    assert row.traceparent and span_id_of(row.traceparent) == create["span_id"]
    with Session(engine) as session:
        assert session.get(Job, uuid.UUID(created["id"])).traceparent == row.traceparent

    # An idempotent replay keeps the original job's trace.
    telemetry.SPANS.clear()
    replay = submit(api, key=key, body=created["_body"])
    assert replay["trace_id"] == trace_id
    assert one(telemetry.captured(), "job.create")["attributes"]["outcome"] == "replayed"


def test_no_upload_file_name_or_query_string_reaches_a_span(api):
    submit(api)
    records = telemetry.captured()
    assert "secret-report-name" not in str(records)
    assert not [r for r in records if "url.query" in r["attributes"]]


def test_database_spans_contain_the_statement_but_never_parameter_values(api):
    key = f"distinctive-param-{uuid.uuid4().hex}"
    submit(api, key=key)
    statements = [
        r["attributes"]["db.statement"] for r in telemetry.captured() if r["name"].startswith("db ")
    ]
    assert any("idempotency_key" in s for s in statements)
    assert any("%(" in s for s in statements)  # placeholders, values sent separately
    assert not any(key in s for s in statements)


# --- outbox -> dispatcher -> worker -------------------------------------------------------------


def test_one_job_story_from_the_request_through_dispatch_retry_and_success(
    api,
    engine,
    session_factory,  # noqa: F811
    api_settings,
):
    created = submit(api, options={"fault_injection": {"mode": "transient", "failures": 1}})
    job_id, trace_id = created["id"], created["trace_id"]
    service = worker(session_factory, api_settings)
    lines: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.addFilter(ContextFilter())
    handler.emit = lines.append  # type: ignore[method-assign]
    logging.getLogger().addHandler(handler)
    try:
        # Attempt 1: the dispatcher continues the job's trace into the message headers.
        [message] = dispatch(session_factory)
        first = service.run(uuid.UUID(job_id), traceparent=message["headers"]["traceparent"])
        assert first.outcome == RunOutcome.RETRY_SCHEDULED
        # Attempt 2: the retry's outbox row carries attempt 1's span.
        [message2] = dispatch(session_factory)
        second = service.run(uuid.UUID(job_id), traceparent=message2["headers"]["traceparent"])
        assert second.outcome == RunOutcome.SUCCEEDED
    finally:
        logging.getLogger().removeHandler(handler)

    records = telemetry.captured()
    in_trace = [r for r in records if r["trace_id"] == trace_id]
    create = one(in_trace, "job.create")
    publish1, publish2 = named(in_trace, "outbox.publish")
    attempt1, attempt2 = named(in_trace, "job.attempt")
    assert publish1["parent_span_id"] == create["span_id"]
    assert publish1["kind"] == "PRODUCER" and attempt1["kind"] == "CONSUMER"
    assert span_id_of(message["headers"]["traceparent"]) == publish1["span_id"]
    assert attempt1["parent_span_id"] == publish1["span_id"]
    assert publish2["parent_span_id"] == attempt1["span_id"]  # the retry was caused by attempt 1
    assert attempt2["parent_span_id"] == publish2["span_id"]

    assert attempt1["status"] == "ERROR"
    assert attempt1["attributes"] | {"queue_wait_seconds": 0} == {
        "job_id": job_id,
        "messaging.system": "celery",
        "attempt_no": 1,
        "workflow_type": "pdf_ingest",
        "trigger": "initial",
        "queue_wait_seconds": 0,
        "outcome": "retry_scheduled",
        "error_class": "injected_transient",
    }
    assert attempt2["attributes"]["outcome"] == "succeeded"
    assert attempt2["attributes"]["trigger"] == "auto_retry"

    # Stages are children of their attempt; attempt 2 reused the checkpoints of attempt 1.
    def stages_of(attempt):
        return {
            r["attributes"]["stage"]: r
            for r in in_trace
            if r["name"].startswith("stage ") and r["parent_span_id"] == attempt["span_id"]
        }

    first_stages, second_stages = stages_of(attempt1), stages_of(attempt2)
    assert list(first_stages) == PDF_STAGES[:4]  # stopped in summarize
    assert first_stages["summarize"]["attributes"]["outcome"] == "failed"
    assert first_stages["summarize"]["attributes"]["error_class"] == "injected_transient"
    assert list(second_stages) == PDF_STAGES
    assert [second_stages[s]["attributes"]["outcome"] for s in PDF_STAGES] == [
        "reused",
        "reused",
        "reused",
        "completed",
        "completed",
    ]

    # Provider calls are children of the summarize stage.
    calls = [r for r in in_trace if r["name"] == "provider.summarize"]
    failed_call = [c for c in calls if c["attributes"]["outcome"] == "failure"]
    assert len(failed_call) == 1
    assert failed_call[0]["parent_span_id"] == first_stages["summarize"]["span_id"]
    assert failed_call[0]["attributes"]["error_class"] == "injected_transient"
    ok_calls = [c for c in calls if c["attributes"]["outcome"] == "success"]
    assert ok_calls and all(
        c["parent_span_id"] == second_stages["summarize"]["span_id"] for c in ok_calls
    )
    assert ok_calls[0]["attributes"]["provider"] == "stub"
    assert {"model", "attempt_no", "job_id", "stage", "purpose"} <= set(ok_calls[0]["attributes"])
    # Database work is traced inside the attempt.
    assert any(r["name"].startswith("db ") for r in in_trace)

    # Every log line written during the attempts carries the job's trace id and its fields.
    job_lines = [r for r in lines if getattr(r, "job_id", None) == job_id]
    assert job_lines
    assert {r.trace_id for r in job_lines} == {trace_id}
    worker_lines = [r for r in job_lines if r.name != "tasklane.dispatcher"]
    assert {r.worker_id for r in worker_lines} == {"worker@test:7"}
    claimed = [r for r in job_lines if r.getMessage() == "job claimed"]
    assert [r.attempt_no for r in claimed] == [1, 2]


def test_a_message_without_trace_headers_still_runs_in_its_own_trace(
    engine,
    session_factory,  # noqa: F811
):
    from tests.integration.test_execution import make_service, new_job

    job_id = new_job(engine)
    result = make_service(session_factory).run(job_id)
    assert result.outcome == RunOutcome.SUCCEEDED
    attempt = one(telemetry.captured(), "job.attempt")
    assert attempt["parent_span_id"] is None


# --- recovery, manual retry, cancel --------------------------------------------------------------


@pytest.fixture
def recovery(session_factory) -> Recovery:  # noqa: F811
    return Recovery(
        session_factory,
        retry_policy=RetryPolicy(base_seconds=5, max_seconds=300, random=lambda: 0.0),
        lost_message_grace=timedelta(seconds=60),
    )


def test_an_expired_lease_is_recovered_inside_the_job_trace(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    job_id = running_job(engine, session_factory, traceparent=JOB_TRACE)
    expire(engine, job_id)
    telemetry.SPANS.clear()

    recovery.run_once()

    action = one(telemetry.captured(), "recovery.lease_expired")
    assert action["trace_id"] == trace_id_from_traceparent(JOB_TRACE)
    assert action["parent_span_id"] == span_id_of(JOB_TRACE)
    assert action["attributes"] == {
        "job_id": str(job_id),
        "attempt_no": 1,
        "outcome": "retry_wait",
    }
    [retry] = [m for m in outbox_rows(engine, job_id) if m.published_at is None]
    assert span_id_of(retry.traceparent) == action["span_id"]  # the next attempt hangs here


def test_a_lost_message_is_requeued_inside_the_job_trace(engine, recovery):
    job_id = lost_job(engine, traceparent=JOB_TRACE)
    telemetry.SPANS.clear()

    recovery.run_once()

    action = one(telemetry.captured(), "recovery.lost_message")
    assert action["trace_id"] == trace_id_from_traceparent(JOB_TRACE)
    assert action["attributes"] == {"job_id": str(job_id), "outcome": "requeued"}
    pending = [m for m in outbox_rows(engine, job_id) if m.published_at is None]
    assert span_id_of(pending[0].traceparent) == action["span_id"]


def test_idle_dispatch_and_recovery_passes_create_no_spans(
    engine,
    session_factory,  # noqa: F811
    recovery,
):
    dispatch(session_factory)
    recovery.run_once()
    assert telemetry.captured() == []


def test_manual_retry_and_cancel_join_the_job_trace_and_link_the_request(
    api,
    engine,
    session_factory,  # noqa: F811
    api_settings,
):
    created = submit(api, options={"fault_injection": {"mode": "permanent"}})
    job_id, trace_id = created["id"], created["trace_id"]
    [message] = dispatch(session_factory)
    worker(session_factory, api_settings).run(
        uuid.UUID(job_id), traceparent=message["headers"]["traceparent"]
    )
    assert api.get(f"/v1/jobs/{job_id}").json()["status"] == "failed"
    telemetry.SPANS.clear()

    assert api.post(f"/v1/jobs/{job_id}/retry").status_code == 200
    records = telemetry.captured()
    request = one(records, "POST /v1/jobs/{job_id}/retry")
    retry = one(records, "job.manual_retry")
    assert retry["trace_id"] == trace_id != request["trace_id"]
    assert retry["links"] == [{"trace_id": request["trace_id"], "span_id": request["span_id"]}]
    assert request["attributes"]["job_id"] == job_id
    pending = [m for m in outbox_rows(engine, job_id) if m.published_at is None]
    assert span_id_of(pending[0].traceparent) == retry["span_id"]

    telemetry.SPANS.clear()
    assert api.post(f"/v1/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    cancel = one(telemetry.captured(), "job.cancel")
    assert cancel["trace_id"] == trace_id
    assert cancel["attributes"]["outcome"] == "cancelled"


# --- event streams -------------------------------------------------------------------------------


def test_one_span_per_event_stream_with_the_number_of_events_sent(api, engine):
    with Session(engine) as session, session.begin():
        job_id = make_job(session, status="succeeded").id
        for _ in range(3):
            append_event(session, job_id, "job.created", {})
    # A finished job's stream replays its events and ends by itself.
    with api.stream("GET", f"/v1/jobs/{job_id}/events") as response:
        body = "".join(response.iter_text())
    assert "stream.end" in body
    records = telemetry.captured()
    stream = one(records, "sse.stream")
    assert stream["attributes"]["outcome"] == "job_finished"
    assert stream["attributes"]["events_sent"] == body.count("\nid: ") + body.startswith("id: ")
    # The polls add no spans: nothing hangs under the stream span.
    assert not [r for r in records if r["parent_span_id"] == stream["span_id"]]
