"""GET /metrics (Phase 10): Prometheus format, the label allowlist, exact values computed from a
known database state, worker availability on and off, and a database outage."""

from datetime import timedelta

import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.orm import Session

from app.db.repositories.jobs import append_event
from app.db.session import create_session_factory
from app.domain.enums import AttemptOutcome, JobStatus, WorkflowType
from app.observability.metrics import ALLOWED_LABELS
from app.services.metrics import DatabaseMetrics
from app.services.worker_presence import WorkerCounters, WorkerPresence
from tests.factories import (
    make_attempt,
    make_job,
    make_outbox,
    make_provider_call,
    make_worker_heartbeat,
    utcnow,
)

S = JobStatus


def scrape(api) -> dict:
    response = api.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    families = {f.name: f for f in text_string_to_metric_families(response.text)}
    return families


def value(families: dict, sample: str, **labels) -> float:
    for family in families.values():
        for s in family.samples:
            if s.name == sample and s.labels == {k: str(v) for k, v in labels.items()}:
                return s.value
    raise KeyError(f"{sample} {labels}")


def status_change(session, job, to, **payload):
    append_event(session, job.id, "job.status_changed", {"from": "x", "to": to, **payload})


@pytest.fixture
def known_state(engine):
    """Three jobs with a known history (every number asserted below comes from here)."""
    base = utcnow() - timedelta(hours=1)
    with Session(engine) as session, session.begin():
        a = make_job(session, status=S.SUCCEEDED)  # pdf: failed once, retried, succeeded
        b = make_job(session, status=S.FAILED)  # pdf: failed permanently, manually retried once
        c = make_job(  # repo: queued for 10 minutes, its message was requeued once
            session,
            status=S.QUEUED,
            workflow_type=WorkflowType.REPO_INDEX,
            updated_at=utcnow() - timedelta(minutes=10),
        )
        status_change(session, a, "running")
        status_change(session, a, "retry_wait", error_class="provider_timeout")
        append_event(session, a.id, "attempt.lease_expired", {"attempt_no": 1})
        append_event(session, a.id, "stage.completed", {"stage": "validate_pdf", "duration_ms": 40})
        append_event(session, a.id, "stage.completed", {"stage": "summarize", "duration_ms": 1500})
        status_change(session, a, "succeeded")
        status_change(session, b, "failed", error_class="pdf_no_text")
        append_event(session, b.id, "job.manual_retry", {"manual_retry_count": 1})
        append_event(session, c.id, "job.requeued", {"reason": "lost_message"})

        def attempt(job, no, queued_s, run_s, outcome):
            started = base + timedelta(minutes=no)
            make_attempt(
                session,
                job,
                attempt_no=no,
                queued_at=started - timedelta(seconds=queued_s),
                started_at=started,
                heartbeat_at=started,
                lease_expires_at=started + timedelta(minutes=5),
                ended_at=started + timedelta(seconds=run_s),
                outcome=outcome,
            )

        attempt(a, 1, 3.0, 2, AttemptOutcome.FAILED_TRANSIENT)
        attempt(a, 2, 0.2, 20, AttemptOutcome.SUCCEEDED)
        attempt(b, 1, 0.5, 1, AttemptOutcome.FAILED_PERMANENT)

        make_provider_call(session, a, latency_ms=200, prompt_tokens=10, completion_tokens=4)
        make_provider_call(session, a, latency_ms=3000, prompt_tokens=5, completion_tokens=1)
        make_provider_call(
            session,
            a,
            latency_ms=20,
            success=False,
            error_class="provider_timeout",
            output_chars=None,
        )

        make_outbox(session, a, published_at=utcnow(), dispatch_attempts=3)  # 2 failed publishes
        make_outbox(  # failing: tried once, pushed into the future by the backoff
            session, c, dispatch_attempts=1, available_at=utcnow() + timedelta(seconds=30)
        )
        make_outbox(session, b, available_at=utcnow() + timedelta(minutes=5))  # a scheduled retry

        make_worker_heartbeat(session, fenced_attempts=2, heartbeat_failures=1)  # alive
        make_worker_heartbeat(  # killed an hour ago
            session,
            last_seen_at=utcnow() - timedelta(hours=1),
            heartbeat_thread_deaths=1,
        )
        make_worker_heartbeat(session, stopped_at=utcnow())  # stopped cleanly


def test_metric_values_after_known_jobs(api, known_state):
    f = scrape(api)
    pdf, repo = "pdf_ingest", "repo_index"
    assert value(f, "tasklane_metrics_database_up") == 1
    assert value(f, "tasklane_jobs_created_total", workflow_type=pdf) == 2
    assert value(f, "tasklane_jobs_created_total", workflow_type=repo) == 1
    assert value(f, "tasklane_jobs", status="succeeded") == 1
    assert value(f, "tasklane_jobs", status="failed") == 1
    assert value(f, "tasklane_jobs", status="queued") == 1
    assert value(f, "tasklane_jobs", status="running") == 0
    assert value(f, "tasklane_jobs_finished_total", workflow_type=pdf, status="succeeded") == 1
    assert value(f, "tasklane_jobs_finished_total", workflow_type=pdf, status="failed") == 1
    assert value(f, "tasklane_jobs_finished_total", workflow_type=repo, status="cancelled") == 0
    assert (
        value(f, "tasklane_job_retries_total", workflow_type=pdf, error_class="provider_timeout")
        == 1
    )
    assert value(f, "tasklane_manual_retries_total", workflow_type=pdf) == 1
    assert value(f, "tasklane_recoveries_total", workflow_type=pdf, outcome="lease_expired") == 1
    assert value(f, "tasklane_recoveries_total", workflow_type=repo, outcome="lost_message") == 1

    # Histograms: cumulative buckets, count and sum.
    assert value(f, "tasklane_queue_wait_seconds_count", workflow_type=pdf) == 3
    assert value(f, "tasklane_queue_wait_seconds_sum", workflow_type=pdf) == pytest.approx(3.7)
    assert value(f, "tasklane_queue_wait_seconds_bucket", workflow_type=pdf, le="0.25") == 1
    assert value(f, "tasklane_queue_wait_seconds_bucket", workflow_type=pdf, le="0.5") == 2
    assert value(f, "tasklane_queue_wait_seconds_bucket", workflow_type=pdf, le="5.0") == 3
    assert value(f, "tasklane_queue_wait_seconds_bucket", workflow_type=pdf, le="+Inf") == 3
    runtime = "tasklane_workflow_runtime_seconds"
    assert value(f, f"{runtime}_sum", workflow_type=pdf, outcome="succeeded") == 20
    assert value(f, f"{runtime}_count", workflow_type=pdf, outcome="failed_transient") == 1
    assert value(f, f"{runtime}_bucket", workflow_type=pdf, outcome="succeeded", le="10.0") == 0
    stage = "tasklane_stage_duration_seconds"
    assert value(f, f"{stage}_sum", workflow_type=pdf, stage="summarize") == 1.5
    assert value(f, f"{stage}_count", workflow_type=pdf, stage="validate_pdf") == 1

    model = "stub-extractive-v1"
    calls = "tasklane_provider_calls_total"
    assert value(f, calls, provider="stub", model=model, outcome="success") == 2
    assert value(f, calls, provider="stub", model=model, outcome="failure") == 1
    assert (
        value(
            f,
            "tasklane_provider_call_failures_total",
            provider="stub",
            model=model,
            error_class="provider_timeout",
        )
        == 1
    )
    assert value(f, "tasklane_provider_prompt_tokens_total", provider="stub", model=model) == 15
    assert value(f, "tasklane_provider_completion_tokens_total", provider="stub", model=model) == 5
    latency = "tasklane_provider_call_duration_seconds"
    assert value(f, f"{latency}_count", provider="stub", model=model, outcome="success") == 2
    assert value(f, f"{latency}_sum", provider="stub", model=model, outcome="success") == 3.2

    assert value(f, "tasklane_oldest_queued_job_age_seconds") >= 599
    assert value(f, "tasklane_outbox_pending_messages") == 1
    assert value(f, "tasklane_outbox_scheduled_messages") == 1
    assert value(f, "tasklane_dispatch_failures_total") == 2  # published rows only

    assert value(f, "tasklane_workers_alive") == 1
    assert value(f, "tasklane_worker_fenced_attempts_total") == 2
    assert value(f, "tasklane_worker_heartbeat_failures_total") == 1
    assert value(f, "tasklane_worker_heartbeat_thread_deaths_total") == 1


def test_every_metric_has_only_allowlisted_labels_and_is_documented(api, known_state):
    # Open (and finish) an event stream first, so the API-local stream metrics exist too.
    job_id = api.get("/v1/jobs").json()["items"][0]["id"]
    with api.stream("GET", f"/v1/jobs/{job_id}/events") as response:
        "".join(response.iter_text())
    families = scrape(api)
    names = set(families)
    assert {"tasklane_sse_streams_open", "tasklane_sse_streams_closed"} <= names
    for family in families.values():
        assert family.name.startswith("tasklane_"), family.name
        assert family.documentation, f"{family.name} has no HELP text"
        assert family.type in {"counter", "gauge", "histogram"}, family.name
        for sample in family.samples:
            labels = set(sample.labels) - {"le"}
            assert labels <= ALLOWED_LABELS, (sample.name, labels - ALLOWED_LABELS)
            assert not any(len(v) == 36 and v.count("-") == 4 for v in sample.labels.values()), (
                sample
            )  # no UUIDs (job, upload or attempt ids) as label values


def test_metrics_format_is_prometheus_text(api):
    response = api.get("/metrics")
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    lines = response.text.splitlines()
    assert "# TYPE tasklane_jobs gauge" in lines
    assert "# TYPE tasklane_queue_wait_seconds histogram" in lines
    assert not [line for line in lines if "otel_scope" in line or "target_info" in line]
    assert not [line for line in lines if line.startswith(("python_", "process_"))]


# --- Worker availability -------------------------------------------------------------------------


def test_worker_availability_on_and_off(api, engine, api_settings):
    def overview_workers():
        return api.get("/v1/overview").json()["workers"]

    assert overview_workers() == {"alive": 0, "last_seen_seconds": None}
    counters = WorkerCounters()
    counters.add("fenced_attempts")
    presence = WorkerPresence(
        create_session_factory(engine), "worker@test-host:99", interval=60, counters=counters
    )
    assert presence.report()
    workers = overview_workers()
    assert workers["alive"] == 1 and workers["last_seen_seconds"] < 5
    families = scrape(api)
    assert value(families, "tasklane_workers_alive") == 1
    assert value(families, "tasklane_worker_fenced_attempts_total") == 1

    counters.add("heartbeat_failures", 2)
    presence.stop()  # a clean shutdown marks the row stopped at once
    assert overview_workers()["alive"] == 0
    families = scrape(api)
    assert value(families, "tasklane_workers_alive") == 0
    assert value(families, "tasklane_worker_heartbeat_failures_total") == 2  # kept after stop


def test_a_worker_that_stopped_reporting_counts_as_gone(api, engine):
    with Session(engine) as session, session.begin():
        make_worker_heartbeat(session, last_seen_at=utcnow() - timedelta(seconds=16))
    workers = api.get("/v1/overview").json()["workers"]
    assert workers["alive"] == 0  # 3 x 5 s presence interval = 15 s
    assert 15 < workers["last_seen_seconds"] < 60


def test_a_restarted_worker_with_the_same_id_keeps_its_predecessors_counters(engine, api):
    """Same host name and pid after a restart: a new row, so totals never go down."""
    factory = create_session_factory(engine)
    first_counters, second_counters = WorkerCounters(), WorkerCounters()
    first_counters.add("fenced_attempts", 3)
    first = WorkerPresence(factory, "worker@host:1", interval=60, counters=first_counters)
    first.report()
    first.stop()
    WorkerPresence(factory, "worker@host:1", interval=60, counters=second_counters).report()
    assert api.get("/v1/overview").json()["workers"]["alive"] == 1
    assert value(scrape(api), "tasklane_worker_fenced_attempts_total") == 3


def test_a_database_outage_still_serves_metrics_with_database_up_0():
    def broken_factory():
        raise ConnectionError("database down")

    broken_factory.begin = broken_factory  # type: ignore[attr-defined]
    collector = DatabaseMetrics(broken_factory, worker_stale_after=timedelta(seconds=15))
    families = list(collector.collect())
    assert [f.name for f in families] == ["tasklane_metrics_database_up"]
    assert families[0].samples[0].value == 0
