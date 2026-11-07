"""End to end: real dispatcher and real Celery worker processes, real Redis, real PostgreSQL.

A job is submitted through the API; the dispatcher publishes it from the outbox; a worker claims
it, runs its stages, and finishes it. No eager mode, no mocks: messages go through JSON
serialization and Redis exactly as in production.

The timing tests use repo_index jobs, which still run the placeholder `dummy` stage (until
Phase 6), because its duration is configurable. The PDF tests run the real pdf_ingest stages.

The processes use the test database, Redis database 15 / queue "tasklane-test", and the test's own
storage folder, so a running dev stack is never involved. Run only these with:  pytest -m e2e
"""

import json
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobAttempt, JobEvent, OutboxMessage, ProviderCall
from app.queue.celery_app import RUN_JOB_TASK, create_celery_app
from devtools import pdf_samples
from tests.integration.conftest import TEST_QUEUE
from tests.samples import zip_bytes

pytestmark = pytest.mark.e2e

BACKEND = Path(__file__).resolve().parents[2]
E2E_RETRY_BASE_SECONDS = 1
PDF_STAGES = ["validate_pdf", "extract_text", "chunk_document", "summarize", "write_artifacts"]


@pytest.fixture
def api_settings(api_settings):
    """Real sample PDFs need more than the shared fixture's 4 KiB upload limit; the retry tests
    need the API to accept the fault_injection option (the worker gets the flag below)."""
    return api_settings.model_copy(
        update={"max_pdf_upload_bytes": 1024 * 1024, "enable_fault_injection": True}
    )


def process_environment(pid: int) -> dict[str, str]:
    """The environment a running process actually has (read from Linux /proc)."""
    raw = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
    return dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)


def wait_until(predicate: Callable[[], object], timeout: float, what: str) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError(f"timed out after {timeout}s waiting for: {what}")


class Pipeline:
    """Starts and stops the dispatcher and worker as separate processes."""

    def __init__(self, env: dict[str, str], log_dir: Path) -> None:
        self.env = env
        self.log_dir = log_dir
        self.processes: list[subprocess.Popen] = []
        self.worker_log = log_dir / "worker.log"

    def _start(self, args: list[str], log: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            args,
            cwd=BACKEND,
            env=self.env,
            stdout=log.open("ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so the whole tree can be stopped
        )
        self.processes.append(process)
        return process

    def start_worker(self) -> None:
        self._start(
            [
                "celery",
                "-A",
                "app.queue.worker_main",
                "worker",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--hostname",
                f"e2e-{uuid.uuid4().hex[:6]}@%h",
            ],
            self.worker_log,
        )
        # Celery logs "<name>@<host> ready." once it is consuming.
        ready = re.compile(r"@\S+ ready\.")
        try:
            wait_until(lambda: ready.search(self.read(self.worker_log)), 60, "worker ready")
        except TimeoutError:
            print(self.read(self.worker_log)[-3000:])  # shown by pytest on failure
            raise

    def start_dispatcher(self) -> None:
        log = self.log_dir / "dispatcher.log"
        self._start(["python", "-m", "app.queue.dispatcher"], log)
        wait_until(lambda: "dispatcher started" in self.read(log), 30, "dispatcher started")

    @staticmethod
    def read(path: Path) -> str:
        return path.read_text(errors="replace") if path.exists() else ""

    def worker_records(self) -> list[dict]:
        records = []
        for line in self.read(self.worker_log).splitlines():
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
        return records

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in self.processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


@pytest.fixture
def start_pipeline(
    queue_settings, dev_settings, broker, tmp_path
) -> Iterator[Callable[..., Pipeline]]:
    pipelines: list[Pipeline] = []
    # Never let test processes read or write the real storage volume.
    assert queue_settings.storage_root != dev_settings.storage_root
    assert queue_settings.storage_root.is_relative_to(tmp_path)

    def start(dummy_stage_seconds: float = 0.5) -> Pipeline:
        env = {
            # No API keys: the pipeline must work with the stub provider alone.
            **{k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()},
            "PROVIDER": "stub",
            "POSTGRES_DB": queue_settings.postgres_db,  # the test database
            "REDIS_URL": queue_settings.redis_url,  # Redis database 15
            "CELERY_QUEUE": TEST_QUEUE,
            "DUMMY_STAGE_SECONDS": str(dummy_stage_seconds),
            "DISPATCH_POLL_SECONDS": "0.2",
            "LEASE_SECONDS": str(queue_settings.lease_seconds),
            "PROVIDER_TIMEOUT_SECONDS": str(queue_settings.provider_timeout_seconds),
            "WORKER_CONCURRENCY": "2",
            "LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
            # Same storage folder as the in-process API, never the dev volume.
            "STORAGE_ROOT": str(queue_settings.storage_root),
            # Fault injection for the retry tests, with 1 s backoff (0.5-1 s, then 1-2 s).
            "ENABLE_FAULT_INJECTION": "true",
            "RETRY_BASE_SECONDS": str(E2E_RETRY_BASE_SECONDS),
        }
        pipeline = Pipeline(env, tmp_path)
        pipelines.append(pipeline)
        pipeline.start_worker()
        pipeline.start_dispatcher()
        return pipeline

    yield start
    for pipeline in pipelines:
        pipeline.stop()


def submit_job(
    api,
    workflow_type: str = "repo_index",
    content: bytes | None = None,
    options: dict | None = None,
) -> str:
    """Upload a file and create a job. Default: a repo_index job (runs the `dummy` stage)."""
    if workflow_type == "repo_index":
        filename, content_type, content = "e2e.zip", "application/zip", content or zip_bytes()
    else:
        filename, content_type = "e2e.pdf", "application/pdf"
        content = content or pdf_samples.report_pdf(page_count=4)
    upload = api.post(
        "/v1/uploads",
        params={"filename": filename},
        content=content,
        headers={"Content-Type": content_type},
    )
    assert upload.status_code == 201, upload.text
    body = {"workflow_type": workflow_type, "upload_id": upload.json()["id"]}
    if options is not None:
        body["options"] = options
    response = api.post("/v1/jobs", json=body, headers={"Idempotency-Key": f"e2e-{uuid.uuid4()}"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def job_status(api, job_id: str) -> str:
    return api.get(f"/v1/jobs/{job_id}").json()["status"]


def events_of(engine, job_id: str) -> list[JobEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(JobEvent).where(JobEvent.job_id == uuid.UUID(job_id)).order_by(JobEvent.seq)
            )
        )


def attempts_of(engine, job_id: str) -> list[JobAttempt]:
    with Session(engine) as session:
        return list(
            session.scalars(select(JobAttempt).where(JobAttempt.job_id == uuid.UUID(job_id)))
        )


def dropped_messages_for(pipeline: Pipeline, job_id: str) -> list[dict]:
    return [
        r
        for r in pipeline.worker_records()
        if r.get("message") == "message dropped" and r.get("job_id") == job_id
    ]


# --- Tests --------------------------------------------------------------------------------------


def test_job_submitted_through_the_api_runs_to_succeeded(api, engine, start_pipeline):
    start_pipeline()
    job_id = submit_job(api)

    wait_until(lambda: job_status(api, job_id) == "succeeded", 60, "job succeeded")

    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["progress_percent"] == 100
    assert detail["current_stage"] == "dummy"
    [attempt] = detail["attempts"]
    assert attempt["outcome"] == "succeeded"
    assert attempt["trigger"] == "initial"
    assert attempt["worker_id"].startswith("worker@")
    assert attempt["task_id"]  # the Celery message id, for tracing

    assert [e.event_type for e in events_of(engine, job_id)] == [
        "job.created",
        "job.status_changed",  # queued -> running
        "attempt.started",
        "stage.started",
        *["stage.progress"] * 5,
        "stage.completed",
        "job.status_changed",  # running -> succeeded
    ]


def test_duplicate_delivery_runs_the_job_only_once(api, engine, queue_settings, start_pipeline):
    pipeline = start_pipeline(dummy_stage_seconds=1.0)
    job_id = submit_job(api)
    # A second message for the same job, as a dispatcher crash or redelivery would produce.
    create_celery_app(queue_settings, for_publishing=True).send_task(
        RUN_JOB_TASK, kwargs={"job_id": job_id}, queue=TEST_QUEUE, retry=False
    )

    wait_until(lambda: job_status(api, job_id) == "succeeded", 60, "job succeeded")
    wait_until(lambda: dropped_messages_for(pipeline, job_id), 30, "duplicate dropped")

    assert len(attempts_of(engine, job_id)) == 1
    [dropped] = dropped_messages_for(pipeline, job_id)
    assert dropped["reason"] in ("not_claimable", "claim_lost")


def test_worker_process_killed_mid_stage_leaves_the_job_running_until_phase_7(
    api, engine, start_pipeline
):
    """Documents the current gap: lease recovery (Phase 7) is what rescues this job."""
    pipeline = start_pipeline(dummy_stage_seconds=6.0)
    job_id = submit_job(api)
    wait_until(lambda: attempts_of(engine, job_id), 60, "job claimed")
    [attempt] = attempts_of(engine, job_id)
    wait_until(lambda: api.get(f"/v1/jobs/{job_id}").json()["progress_percent"] > 0, 30, "progress")

    # Kill the worker *process* running the task (worker_id ends in its pid), like an OOM kill.
    os.kill(int(attempt.worker_id.rsplit(":", 1)[1]), signal.SIGKILL)

    # task_reject_on_worker_lost puts the message back; another process picks it up and drops it,
    # because the job is still `running` under the (dead) attempt.
    wait_until(lambda: dropped_messages_for(pipeline, job_id), 30, "redelivered message dropped")
    time.sleep(1)

    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "running"
    assert 0 < detail["progress_percent"] < 100  # frozen where the process died
    [still_open] = attempts_of(engine, job_id)  # no second attempt was started
    assert still_open.id == attempt.id and still_open.ended_at is None
    assert dropped_messages_for(pipeline, job_id)[0]["job_status"] == "running"


# --- PDF workflow (Phase 5) ----------------------------------------------------------------------


def test_pdf_job_succeeds_with_the_stub_provider_and_no_api_key(api, engine, start_pipeline):
    """Acceptance #9: the full PDF workflow runs with no LLM API key configured."""
    pipeline = start_pipeline()
    assert not any("API_KEY" in name.upper() for name in pipeline.env)
    job_id = submit_job(api, "pdf_ingest")

    wait_until(lambda: job_status(api, job_id) in ("succeeded", "failed"), 90, "job finished")

    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "succeeded", detail
    completed = [
        e.payload["stage"] for e in events_of(engine, job_id) if e.event_type == "stage.completed"
    ]
    assert completed == PDF_STAGES
    downloads = {a["artifact_type"]: api.get(a["download_url"]) for a in detail["artifacts"]}
    assert set(downloads) == {"summary", "manifest"}
    assert all(response.status_code == 200 for response in downloads.values())
    assert downloads["manifest"].json()["summary"]["provider"] == "stub"
    with Session(engine) as session:
        providers = set(
            session.scalars(
                select(ProviderCall.provider).where(ProviderCall.job_id == uuid.UUID(job_id))
            )
        )
    assert providers == {"stub"}


def test_unsupported_pdf_fails_permanently_through_the_real_worker(api, start_pipeline):
    start_pipeline()
    job_id = submit_job(api, "pdf_ingest", pdf_samples.image_only_pdf(page_count=2))

    wait_until(lambda: job_status(api, job_id) == "failed", 90, "job failed")

    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["error_class"] == "pdf_no_text"
    assert len(detail["attempts"]) == 1
    assert detail["artifacts"] == []


def test_worker_and_dispatcher_use_the_test_storage_root_never_the_real_volume(
    api, engine, queue_settings, dev_settings, start_pipeline
):
    """Carry-forward (Phase 5): e2e processes get STORAGE_ROOT from the test settings."""
    pipeline = start_pipeline()
    test_root, real_root = queue_settings.storage_root, dev_settings.storage_root
    job_id = submit_job(api, "pdf_ingest")
    wait_until(lambda: job_status(api, job_id) == "succeeded", 90, "job succeeded")

    # 1. The processes themselves (dispatcher, Celery main process, and the worker child that ran
    #    the job) were started with the test folder.
    [attempt] = attempts_of(engine, job_id)
    worker_child = int(attempt.worker_id.rsplit(":", 1)[1])
    for pid in [p.pid for p in pipeline.processes] + [worker_child]:
        assert process_environment(pid)["STORAGE_ROOT"] == str(test_root)

    # 2. The worker read the upload from, and wrote every file to, the test folder ...
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert (test_root / "uploads" / detail["upload_id"]).is_dir()
    assert (test_root / "work" / job_id / "pages.jsonl").is_file()
    assert (test_root / "artifacts" / job_id / "summary.md").is_file()

    # 3. ... and nothing for this job or its upload exists on the real volume.
    assert not (real_root / "uploads" / detail["upload_id"]).exists()
    assert not (real_root / "work" / job_id).exists()
    assert not (real_root / "artifacts" / job_id).exists()


# --- Retries (Phase 5 Part B) ---------------------------------------------------------------------


def outbox_of(engine, job_id: str) -> list[OutboxMessage]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.job_id == uuid.UUID(job_id))
                .order_by(OutboxMessage.id)
            )
        )


def status_history(events: list[JobEvent]) -> list[str]:
    changes = [e.payload for e in events if e.event_type == "job.status_changed"]
    return [changes[0]["from"]] + [change["to"] for change in changes]


def test_two_transient_failures_are_retried_with_backoff_then_succeed(api, engine, start_pipeline):
    """Acceptance #2: real dispatcher, worker and Redis; the retries travel through the outbox."""
    start_pipeline()
    faults = {"fault_injection": {"mode": "transient", "failures": 2}}
    job_id = submit_job(api, "pdf_ingest", options=faults)

    wait_until(lambda: job_status(api, job_id) in ("succeeded", "failed"), 120, "job finished")

    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "succeeded", detail
    assert [(a["attempt_no"], a["trigger"], a["outcome"]) for a in detail["attempts"]] == [
        (1, "initial", "failed_transient"),
        (2, "auto_retry", "failed_transient"),
        (3, "auto_retry", "succeeded"),
    ]
    events = events_of(engine, job_id)
    assert status_history(events) == [
        "queued",
        "running",
        "retry_wait",
        "running",
        "retry_wait",
        "running",
        "succeeded",
    ]
    # Each retry's message was due at exactly the job's retry_at.
    retries = [
        e.payload
        for e in events
        if e.event_type == "job.status_changed" and e.payload["to"] == "retry_wait"
    ]
    outbox = outbox_of(engine, job_id)
    assert len(outbox) == 3 and all(m.published_at for m in outbox)
    assert [datetime.fromisoformat(r["retry_at"]) for r in retries] == [
        m.available_at for m in outbox[1:]
    ]
    # Equal jitter with a 1 s base: 0.5-1 s, then 1-2 s.
    base = E2E_RETRY_BASE_SECONDS
    first, second = (r["retry_in_seconds"] for r in retries)
    assert base * 0.5 <= first <= base and base <= second <= base * 2
    # The retried attempts resumed after the stages that had already finished.
    skipped = [e.payload["stage"] for e in events if e.event_type == "stage.skipped"]
    assert skipped == ["validate_pdf", "extract_text", "chunk_document"] * 2


def test_three_transient_failures_give_up_after_three_attempts(api, start_pipeline):
    start_pipeline()
    faults = {"fault_injection": {"mode": "transient", "failures": 3}}
    job_id = submit_job(api, "pdf_ingest", options=faults)

    wait_until(lambda: job_status(api, job_id) in ("succeeded", "failed"), 120, "job finished")

    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_summary"].endswith("Gave up after 3 attempts.")
    assert [a["outcome"] for a in detail["attempts"]] == ["failed_transient"] * 3
