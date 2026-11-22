"""Observability end to end (Phase 10): real API, dispatcher and worker processes.

- Acceptance #10: for a failed job, the attempt history, the error, the (absent) artifacts, the
  trace and the log lines are all findable, and the worker's log lines carry the job's trace id.
- One trace per job across processes: API -> outbox -> dispatcher -> attempt -> stages -> provider
  calls, with a retry hanging under the attempt that caused it.
- Worker availability follows the real worker.
- An unreachable OTLP collector changes nothing for jobs, requests or streams.
- Document text, file contents and archive file names reach neither logs nor spans.
"""

import time
import uuid

import httpx
import pytest

from devtools import pdf_samples, zip_samples
from devtools.sse_client import iter_sse, open_stream
from tests import telemetry
from tests.integration import test_e2e_pipeline as pipeline_tests
from tests.integration import test_events_e2e as events_tests

pytestmark = pytest.mark.e2e

api_settings = pipeline_tests.api_settings
start_pipeline = pipeline_tests.start_pipeline
api_server = events_tests.api_server
submit_job, wait_until, job_status = (
    pipeline_tests.submit_job,
    pipeline_tests.wait_until,
    pipeline_tests.job_status,
)
PDF_STAGES = pipeline_tests.PDF_STAGES
UNREACHABLE = "http://192.0.2.1:4318"  # TEST-NET-1: connections hang until the timeout


def in_trace(records: list[dict], trace_id: str) -> list[dict]:
    return [r for r in records if r["trace_id"] == trace_id]


def named(records: list[dict], name: str) -> list[dict]:
    return sorted((r for r in records if r["name"] == name), key=lambda r: r["start_ns"])


def test_a_failed_job_is_fully_findable_from_its_trace_id(api, start_pipeline):
    """Acceptance #10 with a fault-injected job that fails after three transient errors."""
    pipeline = start_pipeline()
    faults = {"fault_injection": {"mode": "transient", "failures": 3}}
    job_id = submit_job(api, "pdf_ingest", options=faults)
    wait_until(lambda: job_status(api, job_id) == "failed", 120, "job failed")

    # 1. The job: attempt history, error, outputs and the trace id, from GET /v1/jobs/{id}.
    detail = api.get(f"/v1/jobs/{job_id}").json()
    trace_id = detail["trace_id"]
    assert trace_id and len(trace_id) == 32
    assert [(a["attempt_no"], a["outcome"]) for a in detail["attempts"]] == [
        (1, "failed_transient"),
        (2, "failed_transient"),
        (3, "failed_transient"),
    ]
    assert detail["error_class"] == "injected_transient"
    assert detail["error_summary"].endswith("Gave up after 3 attempts.")
    assert detail["artifacts"] == []

    # 2. The trace: every attempt of the job, from both processes, shares that trace id.
    spans = in_trace(pipeline.spans(), trace_id)
    attempts = named(spans, "job.attempt")
    assert [a["attributes"]["attempt_no"] for a in attempts] == [1, 2, 3]
    assert all(a["status"] == "ERROR" for a in attempts)
    assert all(a["attributes"]["error_class"] == "injected_transient" for a in attempts)
    assert [a["attributes"]["outcome"] for a in attempts] == [
        "retry_scheduled",
        "retry_scheduled",
        "failed",
    ]
    assert {a["service"] for a in attempts} == {"tasklane-worker"}
    assert {p["service"] for p in named(spans, "outbox.publish")} == {"tasklane-dispatcher"}
    creates = named(telemetry.captured(), "job.create")  # the in-process API's span
    assert [c["trace_id"] for c in creates] == [trace_id]

    # 3. The logs: the worker's lines for this job all carry the same trace id.
    job_lines = [r for r in pipeline.worker_records() if r.get("job_id") == job_id]
    assert len(job_lines) >= 6  # claimed + failed, per attempt
    assert {r.get("trace_id") for r in job_lines} == {trace_id}
    failed = [r for r in job_lines if r["message"] == "job attempt failed"]
    assert [r["attempt_no"] for r in failed] == [1, 2, 3]
    assert all(r["worker_id"].startswith("worker@") for r in failed)
    dispatcher_lines = [
        r for r in pipeline.records(pipeline.dispatcher_log) if r.get("job_id") == job_id
    ]
    assert dispatcher_lines and {r.get("trace_id") for r in dispatcher_lines} == {trace_id}


def test_one_trace_connects_request_dispatch_attempts_stages_and_provider_calls(
    api, start_pipeline
):
    pipeline = start_pipeline()
    faults = {"fault_injection": {"mode": "transient", "failures": 1}}
    job_id = submit_job(api, "pdf_ingest", options=faults)
    wait_until(lambda: job_status(api, job_id) == "succeeded", 120, "job succeeded")
    trace_id = api.get(f"/v1/jobs/{job_id}").json()["trace_id"]

    api_spans = in_trace(telemetry.captured(), trace_id)
    worker_spans = in_trace(pipeline.spans(), trace_id)
    [server] = named(api_spans, "POST /v1/jobs")
    [create] = named(api_spans, "job.create")
    publish1, publish2 = named(worker_spans, "outbox.publish")
    attempt1, attempt2 = named(worker_spans, "job.attempt")
    assert create["parent_span_id"] == server["span_id"]
    assert publish1["parent_span_id"] == create["span_id"]  # across the outbox row
    assert attempt1["parent_span_id"] == publish1["span_id"]  # across the Celery headers
    assert publish2["parent_span_id"] == attempt1["span_id"]  # the retry, caused by attempt 1
    assert attempt2["parent_span_id"] == publish2["span_id"]
    assert attempt2["attributes"]["outcome"] == "succeeded"

    stages = [r for r in worker_spans if r["parent_span_id"] == attempt2["span_id"]]
    stage_names = [
        r["attributes"]["stage"]
        for r in sorted(stages, key=lambda r: r["start_ns"])
        if r["name"].startswith("stage ")
    ]
    assert stage_names == PDF_STAGES
    [summarize] = [r for r in stages if r["name"] == "stage summarize"]
    calls = [r for r in worker_spans if r["name"] == "provider.summarize"]
    assert calls and all(c["attributes"]["provider"] == "stub" for c in calls)
    assert any(c["parent_span_id"] == summarize["span_id"] for c in calls)


def test_worker_availability_follows_the_real_worker(api, start_pipeline):
    def alive() -> int:
        return api.get("/v1/overview").json()["workers"]["alive"]

    assert alive() == 0
    pipeline = start_pipeline()  # WORKER_CONCURRENCY=2: two child processes report
    wait_until(lambda: alive() == 2, 30, "two worker processes alive")
    metrics = api.get("/metrics").text
    assert "tasklane_workers_alive 2.0" in metrics

    pipeline.stop()  # a clean stop marks each process stopped at once
    wait_until(lambda: alive() == 0, 15, "no worker alive")
    assert "tasklane_workers_alive 0.0" in api.get("/metrics").text


def test_an_unreachable_collector_changes_nothing(api, start_pipeline, api_server):
    collector = {"OTEL_EXPORTER_OTLP_ENDPOINT": UNREACHABLE, "OTEL_EXPORT_TIMEOUT_SECONDS": "1"}
    pipeline = start_pipeline(**collector)
    started = time.monotonic()
    job_id = submit_job(api, "pdf_ingest")
    wait_until(lambda: job_status(api, job_id) == "succeeded", 60, "job succeeded")
    assert time.monotonic() - started < 30

    # A real API process exporting to the same dead collector serves requests and streams.
    server = api_server(otel_exporter_otlp_endpoint=UNREACHABLE, otel_export_timeout_seconds=1)
    for _ in range(20):
        t0 = time.monotonic()
        response = httpx.get(f"{server.url}/v1/jobs/{job_id}", timeout=5)
        assert response.status_code == 200
        assert time.monotonic() - t0 < 1.0
    assert response.json()["trace_id"]
    connection, stream = open_stream(
        server.events_url(uuid.UUID(job_id)), after=None, last_event_id=None, timeout=10
    )
    try:
        messages = list(iter_sse(stream))
    finally:
        connection.close()
    assert messages[-1].event == "stream.end"

    # The processes still stop promptly (span export at exit gives up after its timeout).
    t0 = time.monotonic()
    pipeline.stop()
    assert time.monotonic() - t0 < 20


MARKER = "ZEBRA-MARKER-7f3a"


def test_document_text_file_contents_and_archive_names_reach_neither_logs_nor_spans(
    api, start_pipeline
):
    pipeline = start_pipeline()
    pdf = pdf_samples.text_pdf([[f"Confidential paragraph {MARKER} " * 20] * 3])
    archive = zip_samples.build_zip(
        [
            (f"src/{MARKER}_module.py", f"SECRET = '{MARKER}'\nprint('{MARKER}')\n".encode()),
            ("README.md", f"# {MARKER} project\n".encode() * 20),
        ]
    )
    jobs = [submit_job(api, "pdf_ingest", pdf), submit_job(api, "repo_index", archive)]
    for job_id in jobs:
        wait_until(lambda j=job_id: job_status(api, j) == "succeeded", 90, "job succeeded")

    for log in (pipeline.worker_log, pipeline.dispatcher_log):
        assert MARKER not in pipeline.read(log)
    assert MARKER not in pipeline.span_file.read_text()
    assert MARKER not in str(telemetry.captured())
