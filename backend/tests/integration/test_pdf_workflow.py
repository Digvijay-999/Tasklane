"""The pdf_ingest workflow end to end at the service level: real database, real stages, real
parser process, stub provider. (The Celery round trip is covered by the e2e tests.)

Acceptance criterion #3 ("a permanent input/validation error does not retry") is checked here
for every unsupported input: one attempt, no retry_wait, no new outbox message.
"""

import dataclasses
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import JobAttempt, JobEvent, OutboxMessage, ProviderCall
from app.db.session import create_session_factory
from app.domain.errors import ERRORS
from app.providers.base import SummaryResult
from app.queue.worker_process import workflow_config
from app.services.execution import JobExecutionService, RunOutcome
from app.storage.local import LocalStorage
from app.workflows.registry import stages_for
from devtools import pdf_samples

PDF_STAGES = ["validate_pdf", "extract_text", "chunk_document", "summarize", "write_artifacts"]


@pytest.fixture
def api_settings(api_settings):
    """The shared fixture's 4 KiB upload limit is too small for real sample PDFs."""
    return api_settings.model_copy(update={"max_pdf_upload_bytes": 1024 * 1024})


def submit(api, pdf: bytes, filename: str = "report.pdf") -> str:
    upload = api.post(
        "/v1/uploads",
        params={"filename": filename},
        content=pdf,
        headers={"Content-Type": "application/pdf"},
    )
    assert upload.status_code == 201, upload.text
    job = api.post(
        "/v1/jobs",
        json={"workflow_type": "pdf_ingest", "upload_id": upload.json()["id"]},
        headers={"Idempotency-Key": f"pdf-{uuid.uuid4()}"},
    )
    assert job.status_code == 201, job.text
    return job.json()["id"]


@pytest.fixture
def run_job(engine, api_settings):
    """Run a job the way a worker would, with optional settings overrides.

    `provider` replaces the stub; `extra_stage` runs after the PDF stages."""

    def run(job_id: str, *, provider=None, extra_stage=None, **overrides):
        settings = api_settings.model_copy(update=overrides)
        config = workflow_config(settings)
        if provider is not None:
            config = dataclasses.replace(config, provider=provider)

        def stages(workflow_type):
            return stages_for(workflow_type, config) + ([extra_stage] if extra_stage else [])

        service = JobExecutionService(
            create_session_factory(engine),
            worker_id="worker@test:1",
            lease=timedelta(seconds=settings.lease_seconds),
            stages_for=stages,
            storage=LocalStorage(settings.storage_root),
        )
        return service.run(uuid.UUID(job_id))

    return run


def events(engine, job_id: str) -> list[JobEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(JobEvent).where(JobEvent.job_id == uuid.UUID(job_id)).order_by(JobEvent.seq)
            )
        )


def count(engine, model, job_id: str) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count()).select_from(model).where(model.job_id == uuid.UUID(job_id))
        )


# --- Success --------------------------------------------------------------------------------------


def test_text_pdf_runs_every_stage_and_produces_downloadable_artifacts(api, engine, run_job):
    job_id = submit(api, pdf_samples.report_pdf(page_count=6))

    result = run_job(job_id)

    assert result.outcome == RunOutcome.SUCCEEDED
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["progress_percent"] == 100
    assert detail["current_stage"] == "write_artifacts"

    started = [
        e.payload["stage"] for e in events(engine, job_id) if e.event_type == "stage.started"
    ]
    completed = [
        e.payload["stage"] for e in events(engine, job_id) if e.event_type == "stage.completed"
    ]
    assert started == completed == PDF_STAGES

    artifacts = {a["artifact_type"]: a for a in detail["artifacts"]}
    assert set(artifacts) == {"summary", "manifest"}
    summary = api.get(artifacts["summary"]["download_url"])
    manifest = api.get(artifacts["manifest"]["download_url"]).json()
    assert summary.status_code == 200
    assert summary.text.startswith("# Summary of report.pdf")
    assert manifest["schema"] == "tasklane.pdf_manifest/1"
    assert manifest["input"]["pages"] == 6
    assert manifest["input"]["pages_without_text"] == 0
    assert manifest["summary"]["provider"] == "stub"
    assert manifest["chunking"]["chunks"] >= 2
    assert manifest["artifacts"][0]["sha256"] == artifacts["summary"]["sha256"]


def test_provider_calls_are_recorded(api, engine, run_job):
    job_id = submit(api, pdf_samples.report_pdf(page_count=6))
    run_job(job_id)

    with Session(engine) as session:
        calls = list(
            session.scalars(select(ProviderCall).where(ProviderCall.job_id == uuid.UUID(job_id)))
        )
    assert calls
    assert {c.provider for c in calls} == {"stub"}
    assert all(c.success and c.error_class is None for c in calls)
    assert all(c.stage == "summarize" and c.attempt_no == 1 for c in calls)
    chunk_calls = [c for c in calls if c.purpose == "chunk"]
    assert chunk_calls and all(c.chunk_id for c in chunk_calls)
    assert [c.purpose for c in calls].count("combine") >= 1
    assert all(c.total_tokens is None for c in calls)  # the stub reports no usage


def test_pdf_with_only_an_owner_password_succeeds(api, run_job):
    job_id = submit(api, pdf_samples.encrypted_pdf(user_password=""), "locked.pdf")
    assert run_job(job_id).outcome == RunOutcome.SUCCEEDED
    manifest_url = next(
        a["download_url"]
        for a in api.get(f"/v1/jobs/{job_id}").json()["artifacts"]
        if a["artifact_type"] == "manifest"
    )
    assert api.get(manifest_url).json()["input"]["owner_password_only"] is True


# --- Permanent failures (acceptance #3) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("pdf", "error_class", "stage"),
    [
        (pdf_samples.image_only_pdf(page_count=3), "pdf_no_text", "extract_text"),
        (pdf_samples.encrypted_pdf(), "pdf_encrypted", "validate_pdf"),
        (pdf_samples.corrupt_pdf(), "pdf_corrupt", "validate_pdf"),
    ],
    ids=["scanned", "encrypted", "corrupt"],
)
def test_unsupported_pdf_fails_once_with_a_clear_message(
    api, engine, run_job, pdf, error_class, stage
):
    job_id = submit(api, pdf)

    result = run_job(job_id)

    assert (result.outcome, result.error_class) == (RunOutcome.FAILED, error_class)
    detail = api.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_class"] == error_class
    assert detail["error_summary"] == ERRORS[error_class].message  # the fixed, safe text
    [attempt] = detail["attempts"]
    assert attempt["outcome"] == "failed_permanent"
    assert attempt["error_class"] == error_class
    assert detail["artifacts"] == []

    history = events(engine, job_id)
    failed = [e for e in history if e.event_type == "stage.failed"]
    assert [(e.payload["stage"], e.payload["error_class"]) for e in failed] == [
        (stage, error_class)
    ]
    # No retry: never retry_wait, and no second outbox message.
    assert not any(e.payload.get("to") == "retry_wait" for e in history)
    assert count(engine, OutboxMessage, job_id) == 1
    assert count(engine, JobAttempt, job_id) == 1


def test_too_many_pages(api, run_job):
    job_id = submit(api, pdf_samples.report_pdf(page_count=4))
    result = run_job(job_id, pdf_max_pages=2)
    assert result.error_class == "pdf_too_many_pages"
    assert api.get(f"/v1/jobs/{job_id}").json()["error_summary"] == (
        "The PDF has 4 pages; the limit is 2."
    )


def test_too_much_text(api, run_job):
    job_id = submit(api, pdf_samples.report_pdf(page_count=4))
    result = run_job(job_id, pdf_max_text_chars=1000)
    assert result.error_class == "pdf_text_too_large"


@pytest.mark.parametrize("damage", ["deleted", "modified"])
def test_missing_or_changed_upload_file_fails_clearly(api, engine, run_job, api_settings, damage):
    job_id = submit(api, pdf_samples.report_pdf())
    upload_id = api.get(f"/v1/jobs/{job_id}").json()["upload_id"]
    path = LocalStorage(api_settings.storage_root).absolute(f"uploads/{upload_id}/source.pdf")
    if damage == "deleted":
        path.unlink()
    else:
        path.write_bytes(pdf_samples.report_pdf(page_count=2))

    assert run_job(job_id).error_class == "input_unavailable"


def test_failure_details_stay_out_of_the_database(api, engine, run_job):
    job_id = submit(api, pdf_samples.corrupt_pdf())
    run_job(job_id)
    stored = json.dumps([e.payload for e in events(engine, job_id)])
    detail = api.get(f"/v1/jobs/{job_id}").json()
    for text in (stored, detail["error_summary"], detail["attempts"][0]["error_summary"]):
        assert "Traceback" not in text and "pypdf" not in text and "/data" not in text


# --- Artifacts appear only with success -----------------------------------------------------------


class _CheckThenFail:
    """Runs after write_artifacts: checks no artifact is visible yet, then fails the job."""

    name = "after_artifacts"
    weight = 1

    def __init__(self, api, job_id_holder):
        self.api, self.job_id_holder, self.seen = api, job_id_holder, None

    def run(self, context):
        self.seen = self.api.get(f"/v1/jobs/{context.job.job_id}").json()["artifacts"]
        raise RuntimeError("fails after the artifact files were written")


def test_artifacts_are_not_visible_while_running_nor_after_a_failure(api, engine, run_job):
    job_id = submit(api, pdf_samples.report_pdf())
    late = _CheckThenFail(api, job_id)

    result = run_job(job_id, extra_stage=late)

    assert late.seen == []  # files were written, but nothing was listed while running
    assert result.error_class == "internal_error"
    assert api.get(f"/v1/jobs/{job_id}").json()["artifacts"] == []  # nor after the failure


# --- Misbehaving providers ------------------------------------------------------------------------


class _BrokenProvider:
    name, model = "broken", "broken-1"

    def summarize(self, text, options):
        raise ConnectionError("provider unreachable")  # has no error_class attribute


class _VerboseProvider:
    """Ignores max_output_chars, as a real model might."""

    name, model = "verbose", "verbose-1"

    def summarize(self, text, options):
        return SummaryResult(
            text="word " * 2000, provider=self.name, model=self.model, latency_ms=1
        )


def test_a_failing_provider_call_is_recorded_and_fails_the_job_safely(api, engine, run_job):
    job_id = submit(api, pdf_samples.report_pdf())

    result = run_job(job_id, provider=_BrokenProvider())

    assert result.error_class == "internal_error"
    with Session(engine) as session:
        [call] = session.scalars(
            select(ProviderCall).where(ProviderCall.job_id == uuid.UUID(job_id))
        ).all()
    assert (call.success, call.error_class, call.provider) == (False, "internal_error", "broken")
    assert "unreachable" not in api.get(f"/v1/jobs/{job_id}").json()["error_summary"]


def test_provider_output_is_capped_so_combining_always_finishes(api, engine, run_job):
    job_id = submit(api, pdf_samples.report_pdf(page_count=6))

    result = run_job(job_id, provider=_VerboseProvider())

    assert result.outcome == RunOutcome.SUCCEEDED
    with Session(engine) as session:
        calls = session.scalars(
            select(ProviderCall).where(ProviderCall.job_id == uuid.UUID(job_id))
        ).all()
    limits = {"chunk": 600, "combine": 2000}  # summary_chunk_max_chars, summary_final_max_chars
    assert all(c.output_chars <= limits[c.purpose] for c in calls)
