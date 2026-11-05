"""End to end: real dispatcher and real Celery worker processes, real Redis, real PostgreSQL.

A job is submitted through the API; the dispatcher publishes it from the outbox; a worker claims
it, runs the placeholder stage, and finishes it. No eager mode, no mocks: messages go through
JSON serialization and Redis exactly as in production.

The processes use the test database and Redis database 15 / queue "tasklane-test", so a running
dev stack is never involved. Run only these with:  pytest -m e2e
"""

import json
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import JobAttempt, JobEvent
from app.queue.celery_app import RUN_JOB_TASK, create_celery_app
from tests.integration.conftest import TEST_QUEUE
from tests.samples import pdf_bytes

pytestmark = pytest.mark.e2e

BACKEND = Path(__file__).resolve().parents[2]


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
        wait_until(lambda: ready.search(self.read(self.worker_log)), 60, "worker ready")

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
def start_pipeline(queue_settings, broker, tmp_path) -> Iterator[Callable[..., Pipeline]]:
    pipelines: list[Pipeline] = []

    def start(dummy_stage_seconds: float = 0.5) -> Pipeline:
        env = {
            **os.environ,
            "POSTGRES_DB": queue_settings.postgres_db,  # the test database
            "REDIS_URL": queue_settings.redis_url,  # Redis database 15
            "CELERY_QUEUE": TEST_QUEUE,
            "DUMMY_STAGE_SECONDS": str(dummy_stage_seconds),
            "DISPATCH_POLL_SECONDS": "0.2",
            "LEASE_SECONDS": "30",
            "WORKER_CONCURRENCY": "2",
            "LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
            # Same storage folder as the in-process API, never the dev volume.
            "STORAGE_ROOT": str(queue_settings.storage_root),
        }
        pipeline = Pipeline(env, tmp_path)
        pipelines.append(pipeline)
        pipeline.start_worker()
        pipeline.start_dispatcher()
        return pipeline

    yield start
    for pipeline in pipelines:
        pipeline.stop()


def submit_job(api) -> str:
    upload = api.post(
        "/v1/uploads",
        params={"filename": "e2e.pdf"},
        content=pdf_bytes(),
        headers={"Content-Type": "application/pdf"},
    ).json()
    response = api.post(
        "/v1/jobs",
        json={"workflow_type": "pdf_ingest", "upload_id": upload["id"]},
        headers={"Idempotency-Key": f"e2e-{uuid.uuid4()}"},
    )
    assert response.status_code == 201
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
